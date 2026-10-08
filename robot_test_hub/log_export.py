"""One-way publication of completed originals to an explicitly configured sync folder."""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import time

from .backup import _digest, _identity, _json, _owner, _path, _SHA, _sync_directory, _temporary, _write


class ExportError(ValueError):
    pass


class ExportInterrupted(ExportError):
    pass


def _metadata(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1
            or not isinstance(value.get("sha256"), str) or not _SHA.fullmatch(value["sha256"])
            or type(value.get("size_bytes")) is not int or value["size_bytes"] < 12
            or type(value.get("published_utc_ns")) is not int
            or not isinstance(value.get("original_name"), str)
            or value.get("wpilog") != value["sha256"] + ".wpilog"
            or path.name != value["sha256"] + ".json"):
        raise ExportError("Invalid original-log export metadata")
    return value


def verify(path):
    """Read every byte at home; success says nothing about another remote copy."""
    path = Path(path).absolute()
    metadata_path = _path(path.parent, path.stem + ".json")
    value = _metadata(metadata_path)
    original = _path(path.parent, value["wpilog"])
    if _digest(original) != (value["sha256"], value["size_bytes"]):
        raise ExportError("Shared original integrity mismatch")
    with original.open("rb") as stream:
        if stream.read(6) != b"WPILOG":
            raise ExportError("Shared original is not a WPILOG")
    return {"state": "verified", "sha256": value["sha256"], "size_bytes": value["size_bytes"],
            "cloud_upload_confirmed": False}


class LogExporter:
    """One dedicated collector. Receipts survive restarts; originals are never deleted."""
    def __init__(self, source, destination, *, stopping=lambda: False, publish=lambda value: None):
        self.source, self.destination = Path(source).resolve(), Path(destination).absolute()
        target = self.destination.resolve()
        if self.source == target or self.source.is_relative_to(target) or target.is_relative_to(self.source):
            raise ExportError("Export destination must be disjoint from local data")
        self.stopping, self.publish = stopping, publish
        key = hashlib.sha256(str(self.destination).encode()).hexdigest()
        self.receipts = _path(self.source, "exports/log-sharing/" + key)
        self.snapshot = {"schema_version": 1, "enabled": True, "state": "starting", "error_code": None,
                         "copied_files": 0, "pending_files": 0, "pending_bytes": 0, "skipped_files": 0,
                         "cloud_upload_confirmed": False}

    def _emit(self, **values):
        self.snapshot.update(values)
        self.publish(dict(self.snapshot))

    def _check_stop(self):
        if self.stopping():
            raise ExportInterrupted("Log publication interrupted")

    def _candidates(self):
        # Read-only committed catalog view; no migration or network/source contact.
        catalog = _path(self.source, "catalog.sqlite3")
        with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = []
            for row in db.execute("SELECT id,name,size,sha256 FROM files WHERE state='complete' ORDER BY id"):
                rows.append({"path": "archive/" + _identity(row["id"]) + ".logdata",
                             "sha256": row["sha256"], "size": row["size"], "name": row["name"]})
            for row in db.execute("SELECT * FROM import_artifacts ORDER BY sha256"):
                rows.append({"path": row["relative_path"], "sha256": row["sha256"],
                             "size": row["size_bytes"], "name": Path(row["relative_path"]).name})
        candidates = {}
        for row in rows:
            sha, size = row["sha256"], row["size"]
            if not isinstance(sha, str) or not _SHA.fullmatch(sha) or type(size) is not int or size < 0:
                raise ExportError("Invalid catalog identity")
            _path(self.source, row["path"])
            if sha in candidates and candidates[sha]["size"] != size:
                raise ExportError("Catalog original identities disagree")
            candidates.setdefault(sha, row)
        return list(candidates.values())

    def _copy(self, source, target, row):
        if target.exists():
            if _digest(target) != (row["sha256"], row["size"]):
                raise ExportError("Existing shared original differs; preserved")
            return
        temporary = _temporary(target)
        digest, size = hashlib.sha256(), 0
        with source.open("rb") as inp, temporary.open("wb") as out:
            while True:
                self._check_stop()
                block = inp.read(1024 * 1024)
                if not block:
                    break
                out.write(block)
                digest.update(block)
                size += len(block)
                if size > row["size"]:
                    raise ExportError("Original size changed")
            out.flush()
            os.fsync(out.fileno())
        if (digest.hexdigest(), size) != (row["sha256"], row["size"]):
            raise ExportError("Original integrity changed")
        self._check_stop()
        temporary.replace(target)
        _sync_directory(target.parent)
        if _digest(target) != (row["sha256"], row["size"]):
            raise ExportError("Destination integrity mismatch")

    def tick(self):
        rows = self._candidates()
        self._emit(state="publishing", error_code=None, copied_files=0,
                   pending_files=len(rows), pending_bytes=sum(row["size"] for row in rows), skipped_files=0)
        try:
            # Do not invent/recreate a missing Drive mount or destination folder.
            if not self.destination.is_dir() or self.destination.resolve() != self.destination:
                raise ExportError("Configured sync folder is unavailable or linked")
            self.receipts.mkdir(parents=True, exist_ok=True)
            with _owner(self.destination):
                index = []
                for row in rows:
                    self._check_stop()
                    sha = row["sha256"]
                    folder = _path(self.destination, "logs/" + sha[:2])
                    folder.mkdir(parents=True, exist_ok=True)
                    target = _path(folder, sha + ".wpilog")
                    metadata_path = _path(folder, sha + ".json")
                    receipt = _path(self.receipts, sha + ".json")
                    cached = _metadata(receipt) if receipt.exists() else None
                    if cached and cached["size_bytes"] != row["size"]:
                        raise ExportError("Local export receipt conflicts")
                    complete = (cached is not None and target.is_file() and target.stat().st_size == row["size"]
                                and metadata_path.is_file() and metadata_path.read_bytes() == _json(cached))
                    if not complete:
                        source = _path(self.source, row["path"])
                        with source.open("rb") as stream:
                            is_wpilog = row["size"] >= 12 and stream.read(6) == b"WPILOG"
                        if not is_wpilog:
                            self._emit(skipped_files=self.snapshot["skipped_files"] + 1,
                                       pending_files=self.snapshot["pending_files"] - 1,
                                       pending_bytes=self.snapshot["pending_bytes"] - row["size"])
                            continue
                        metadata = cached
                        if metadata_path.exists():
                            existing = _metadata(metadata_path)
                            if existing["sha256"] != sha or existing["size_bytes"] != row["size"]:
                                raise ExportError("Shared metadata conflicts; preserved")
                            if metadata is not None and metadata != existing:
                                raise ExportError("Shared metadata changed; preserved")
                            metadata = existing
                        if metadata is None:
                            metadata = {"schema_version": 1, "sha256": sha, "size_bytes": row["size"],
                                        "original_name": row["name"], "wpilog": sha + ".wpilog",
                                        "published_utc_ns": time.time_ns()}
                        self._copy(source, target, row)
                        self._check_stop()
                        if not metadata_path.exists():
                            _write(metadata_path, _json(metadata))
                        # Publish metadata last. Drive may upload in a different order;
                        # readers must download and verify both files.
                        _write(receipt, _json(metadata))
                        cached = metadata
                    name = cached["original_name"]
                    if name.lstrip().startswith(("=", "+", "-", "@")):
                        name = "'" + name
                    index.append((name, "logs/" + sha[:2] + "/" + sha + ".wpilog",
                                  sha, row["size"], str(cached["published_utc_ns"])))
                    self._emit(copied_files=self.snapshot["copied_files"] + 1,
                               pending_files=self.snapshot["pending_files"] - 1,
                               pending_bytes=self.snapshot["pending_bytes"] - row["size"])
                buffer = io.StringIO(newline="")
                writer = csv.writer(buffer)
                writer.writerow(("original_name", "wpilog", "sha256", "size_bytes", "published_utc_ns"))
                writer.writerows(index)
                encoded = buffer.getvalue().encode("utf-8")
                path = _path(self.destination, "logs.csv")
                if not path.exists() or path.read_bytes() != encoded:
                    _write(path, encoded)
            self._emit(state="idle")
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            self._emit(state="stopped" if isinstance(exc, ExportInterrupted) else "retry_wait",
                       error_code=type(exc).__name__)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("publish")
    export.add_argument("--data-dir", required=True)
    export.add_argument("--destination", required=True)
    check = sub.add_parser("verify")
    check.add_argument("wpilog", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            result = verify(args.wpilog)
        else:
            exporter = LogExporter(args.data_dir, args.destination)
            exporter.tick()
            result = exporter.snapshot
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"state": "error", "error_code": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
