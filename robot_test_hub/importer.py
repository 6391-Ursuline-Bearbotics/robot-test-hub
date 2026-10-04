"""Manual immutable WPILOG ingestion with persistent, versioned import jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from .storage import DataRootOwner, open_catalog
from .wpilog import ALIASES, ODOMETRY_ARRAY_TIMESTAMPS, EXTRACTOR_VERSION, MAPPING_REVISION, PROFILE, FormatError, ResourceLimit, UnsupportedProfile, UnsupportedType, extract


class ArchiveConflict(RuntimeError):
    pass


def install_schema(db):
    """Install inside the caller's migration transaction; never commit/change user_version."""
    db.execute("""CREATE TABLE IF NOT EXISTS import_artifacts (
        sha256 TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, relative_path TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL, format_state TEXT NOT NULL DEFAULT 'unchecked',
        created_utc_ns INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS import_jobs (
        id TEXT PRIMARY KEY, artifact_sha256 TEXT NOT NULL REFERENCES import_artifacts(sha256),
        extractor_version TEXT NOT NULL, profile TEXT NOT NULL, mapping_revision TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT, error TEXT,
        dataset_path TEXT, dataset_sha256 TEXT, manifest_path TEXT, manifest_sha256 TEXT,
        created_utc_ns INTEGER NOT NULL, updated_utc_ns INTEGER NOT NULL,
        UNIQUE(artifact_sha256, extractor_version, profile, mapping_revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS import_requests (
        artifact_sha256 TEXT NOT NULL REFERENCES import_artifacts(sha256),
        original_name TEXT NOT NULL, source_type TEXT NOT NULL,
        expected_sha256 TEXT NOT NULL DEFAULT '', created_utc_ns INTEGER NOT NULL,
        UNIQUE(artifact_sha256,original_name,source_type,expected_sha256))""")


def _digest(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _publish(temporary, target):
    """Publish without replacing any existing immutable artifact (including concurrent creation)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(temporary, target)
    except FileExistsError:
        if _digest(temporary) != _digest(target):
            raise ArchiveConflict("Existing content-addressed artifact differs; preserve it for investigation") from None
    finally:
        temporary.unlink(missing_ok=True)
    if os.name != "nt":
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _temporary(root, suffix):
    root.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".import-", suffix=suffix, dir=root)
    return Path(name), os.fdopen(descriptor, "wb")


def _write_json(path, obj):
    encoded = json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    with path.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


class Importer:
    """Uses a catalog already opened/owned by the caller. No network/source reads."""
    def __init__(self, root: Path, db: sqlite3.Connection):
        self.root, self.db = Path(root).resolve(), db
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, relative):
        result = (self.root / relative).resolve()
        if not result.is_relative_to(self.root):
            raise ArchiveConflict("Catalog artifact path escapes the data root")
        return result

    def list_jobs(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM import_jobs ORDER BY created_utc_ns,id")]

    def get_job(self, job_id):
        row = self.db.execute("SELECT * FROM import_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def _check_dataset(self, job):
        if job["state"] not in ("succeeded", "succeeded_with_unsupported"):
            raise ValueError("Import has no successful dataset")
        artifact = self.db.execute("SELECT * FROM import_artifacts WHERE sha256=?", (job["artifact_sha256"],)).fetchone()
        raw = self._path(artifact["relative_path"])
        if not raw.is_file() or _digest(raw) != (artifact["sha256"], artifact["size_bytes"]):
            raise ArchiveConflict("Immutable source artifact is missing or changed")
        for path_key, hash_key in (("dataset_path", "dataset_sha256"), ("manifest_path", "manifest_sha256")):
            path = self._path(job[path_key])
            if not path.is_file() or _digest(path)[0] != job[hash_key]:
                raise ArchiveConflict("Immutable derived artifact is missing or changed")

    def read_manifest(self, job_id):
        job = self.get_job(job_id)
        self._check_dataset(job)
        return json.loads(self._path(job["manifest_path"]).read_text(encoding="utf-8"))

    def iter_dataset(self, job_id, kind=None):
        job = self.get_job(job_id)
        self._check_dataset(job)
        with self._path(job["dataset_path"]).open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if kind is None or row["kind"] == kind:
                    yield row

    def _archive(self, source, source_type, expected_sha256):
        if source_type not in ("SYNTHETIC", "MANUAL_LOCAL", "VERIFIED_TRANSFER"):
            raise ValueError("Unknown importer source provenance")
        temporary, output = _temporary(self.root / "raw", ".wpilog.tmp")
        try:
            digest, size = hashlib.sha256(), 0
            with source.open("rb") as stream, output:
                before = os.fstat(stream.fileno())
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
                    output.write(block)
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
                    raise ArchiveConflict("Source changed during manual snapshot; no closed-original claim")
                output.flush()
                os.fsync(output.fileno())
            digest = digest.hexdigest()
            if source_type == "VERIFIED_TRANSFER" and digest != expected_sha256:
                source_type = "TRANSFER_CHECKSUM_MISMATCH"
            relative = f"raw/{digest[:2]}/{digest}.wpilog"
            _publish(temporary, self._path(relative))
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO import_artifacts (sha256,size_bytes,relative_path,source_type,created_utc_ns) VALUES (?,?,?,?,?)",
                                (digest, size, relative, source_type, time.time_ns()))
                if source_type == "VERIFIED_TRANSFER":
                    self.db.execute("UPDATE import_artifacts SET source_type='VERIFIED_TRANSFER' WHERE sha256=? AND source_type='TRANSFER_CHECKSUM_MISMATCH'", (digest,))
            stored = self.db.execute("SELECT * FROM import_artifacts WHERE sha256=?", (digest,)).fetchone()
            if stored["size_bytes"] != size or stored["relative_path"] != relative:
                raise ArchiveConflict("Catalog identity disagrees with immutable raw artifact")
            return digest, size, relative, stored["source_type"]
        finally:
            output.close()
            temporary.unlink(missing_ok=True)

    def import_file(self, path: Path, *, profile=PROFILE, expected_sha256=None,
                    source_type="MANUAL_LOCAL", mapping_revision=MAPPING_REVISION, retry=False):
        if not isinstance(profile, str) or not profile or len(profile) > 256:
            raise ValueError("Profile must be a nonempty bounded string")
        if not isinstance(mapping_revision, str) or not mapping_revision or len(mapping_revision) > 128:
            raise ValueError("Mapping revision must be a nonempty bounded string")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise ValueError("Expected SHA-256 must be 64 lowercase hexadecimal characters")
        if source_type == "VERIFIED_TRANSFER" and expected_sha256 is None:
            raise ValueError("VERIFIED_TRANSFER requires the verified transfer's expected SHA-256")
        request_source_type = source_type
        digest, size, raw_path, source_type = self._archive(Path(path), source_type, expected_sha256)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO import_requests (artifact_sha256,original_name,source_type,expected_sha256,created_utc_ns) VALUES (?,?,?,?,?)",
                            (digest, Path(path).name, request_source_type, expected_sha256 or "", time.time_ns()))
        identity = json.dumps([digest, EXTRACTOR_VERSION, profile, mapping_revision], separators=(",", ":"))
        job_id = hashlib.sha256(identity.encode()).hexdigest()
        now = time.time_ns()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO import_jobs (id,artifact_sha256,extractor_version,profile,mapping_revision,state,created_utc_ns,updated_utc_ns) VALUES (?,?,?,?,?,'pending',?,?)",
                            (job_id, digest, EXTRACTOR_VERSION, profile, mapping_revision, now, now))
        job = self.get_job(job_id)
        if expected_sha256 is not None and digest != expected_sha256:
            # A failed expectation must never turn an existing valid import into a success response.
            if job["state"] in ("succeeded", "succeeded_with_unsupported"):
                raise ArchiveConflict("Expected source checksum differs from the existing verified artifact")
            return self._finish(job_id, "checksum_mismatch", "checksum_mismatch", "Source bytes do not match the expected checksum")
        if job["state"] in ("succeeded", "succeeded_with_unsupported"):
            self._check_dataset(job)
            return job
        if job["state"] not in ("pending", "running") and not retry:
            return job
        with self.db:
            self.db.execute("UPDATE import_jobs SET state='running',attempts=attempts+1,error_code=NULL,error=NULL,updated_utc_ns=? WHERE id=?", (now, job_id))
        temporary = None
        try:
            if mapping_revision != MAPPING_REVISION:
                raise UnsupportedProfile("Unsupported alias mapping revision")
            temporary, output = _temporary(self.root / "derived", ".jsonl.tmp")
            counts, unsupported, metadata, timestamp_min, timestamp_max = {}, set(), {}, None, None
            with output:
                for row in extract(self._path(raw_path), profile):
                    row["source_sha256"] = digest
                    row["extractor_version"] = EXTRACTOR_VERSION
                    row["profile"] = profile
                    row["mapping_revision"] = mapping_revision
                    counts[row["kind"]] = counts.get(row["kind"], 0) + 1
                    if row["kind"] == "observation":
                        if row["category"] == "metadata":
                            metadata[row["field"]] = row["value"]
                        if not row["decoded"]:
                            unsupported.add(row["type"])
                    if row["kind"] == "cycle":
                        timestamp_min = timestamp_min or row["timestamp_ns"]
                        timestamp_max = row["timestamp_ns"]
                    output.write(json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
                output.flush()
                os.fsync(output.fileno())
            dataset_path = f"derived/{job_id}/signals.jsonl"
            dataset_digest = _digest(temporary)[0]
            _publish(temporary, self._path(dataset_path))
            temporary = None
            manifest = {"schema_version": 1, "job_id": job_id, "source_sha256": digest,
                        "source_size_bytes": size, "raw_path": raw_path, "source_type": source_type,
                        "extractor_version": EXTRACTOR_VERSION, "profile": profile,
                        "profile_selection": "explicit_caller_profile", "mapping_revision": mapping_revision,
                        "aliases": ALIASES, "odometry_array_timestamps": ODOMETRY_ARRAY_TIMESTAMPS,
                        "record_header_unit": "microseconds", "record_timestamp_unit": "nanoseconds",
                        "timestamp_payload_unit": "nanoseconds", "epoch_payload_unit": "microseconds",
                        "counts": counts, "unsupported_types": sorted(unsupported), "metadata_last_values": metadata,
                        "first_cycle_timestamp_ns": timestamp_min, "last_cycle_timestamp_ns": timestamp_max,
                        "bootstrap_snapshot": "not_recorded", "dataset_path": dataset_path,
                        "dataset_sha256": dataset_digest, "coverage": "physical_records_only; terminal event and clock validity are separate"}
            temporary, output = _temporary(self.root / "derived", ".manifest.tmp")
            output.close()
            _write_json(temporary, manifest)
            manifest_digest = _digest(temporary)[0]
            manifest_path = f"derived/{job_id}/manifest.json"
            _publish(temporary, self._path(manifest_path))
            temporary = None
            with self.db:
                self.db.execute("UPDATE import_artifacts SET format_state='valid' WHERE sha256=?", (digest,))
                self.db.execute("UPDATE import_jobs SET state=?,error_code=NULL,error=NULL,dataset_path=?,dataset_sha256=?,manifest_path=?,manifest_sha256=?,updated_utc_ns=? WHERE id=?",
                                ("succeeded_with_unsupported" if unsupported else "succeeded", dataset_path, dataset_digest, manifest_path, manifest_digest, time.time_ns(), job_id))
            return self.get_job(job_id)
        except UnsupportedProfile as error:
            return self._finish(job_id, "unsupported", "unsupported_profile", str(error))
        except UnsupportedType as error:
            return self._finish(job_id, "unsupported", "unsupported_schema", str(error))
        except FormatError as error:
            with self.db:
                self.db.execute("UPDATE import_artifacts SET format_state='invalid' WHERE sha256=?", (digest,))
            return self._finish(job_id, "invalid", "invalid_format", str(error))
        except ResourceLimit as error:
            return self._finish(job_id, "failed", "resource_limit", str(error))
        except (OSError, sqlite3.Error, ArchiveConflict) as error:
            return self._finish(job_id, "failed", "import_io_or_integrity", type(error).__name__)
        except Exception as error:
            return self._finish(job_id, "failed", "extractor_failure", type(error).__name__)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _finish(self, job_id, state, code, error):
        with self.db:
            self.db.execute("UPDATE import_jobs SET state=?,error_code=?,error=?,updated_utc_ns=? WHERE id=?", (state, code, error, time.time_ns(), job_id))
        return self.get_job(job_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--profile", required=True, help="Exact supported library/schema profile; never inferred")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    owner = DataRootOwner(args.data_dir)
    try:
        db = open_catalog(owner.root)
        try:
            with db:
                install_schema(db)
            job = Importer(owner.root, db).import_file(args.file, profile=args.profile, expected_sha256=args.expected_sha256,
                                                     source_type="SYNTHETIC" if args.synthetic else "MANUAL_LOCAL", retry=args.retry)
            # Nanosecond catalog timestamps follow the public JSON integer contract.
            print(json.dumps({k: str(v) if k.endswith("_utc_ns") and v is not None else v for k, v in job.items()}, sort_keys=True))
            return 0 if job["state"] in ("succeeded", "succeeded_with_unsupported") else 1
        finally:
            db.close()
    finally:
        owner.close()


if __name__ == "__main__":
    raise SystemExit(main())
