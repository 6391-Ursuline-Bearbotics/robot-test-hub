"""Opt-in, resumable catalog/artifact generations; no deletion or robot access."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import time

from .config import Config
from .storage import DataRootOwner

SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")


class BackupError(ValueError):
    pass


class BackupInterrupted(BackupError):
    pass


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(path)
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    _sync_directory(path.parent)


def _temporary(path):
    temporary = path.with_name(path.name + ".writing")
    if temporary.is_symlink() or temporary.resolve() != temporary.absolute():
        raise BackupError("Temporary artifact links are not supported")
    return temporary


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise BackupError("Generation must be an opaque identifier")
    return value


def _path(root, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative
            or PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).drive
            or any(part in ("", ".", "..") or ":" in part or part.endswith((".", " "))
                   or part.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}
                   for part in relative.split("/"))):
        raise BackupError("Artifact path must stay within its root")
    candidate = root.joinpath(*relative.split("/"))
    # Reject symlinks/junctions even when their current resolved target is inside.
    for path in (candidate, *candidate.parents):
        if path == root.parent:
            break
        if path.is_symlink() or path.resolve() != path.absolute():
            raise BackupError("Artifact links are not supported")
    if not candidate.resolve().is_relative_to(root):
        raise BackupError("Artifact path escaped its root")
    return candidate


def _digest(path, length=None):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while length is None or size < length:
            block = stream.read(1024 * 1024 if length is None else min(1024 * 1024, length - size))
            if not block:
                break
            digest.update(block)
            size += len(block)
    if length is not None and size != length:
        raise BackupError("Committed artifact bytes are missing")
    return digest.hexdigest(), size


def _copy(source, target, expected_sha256, size):
    if target.exists():
        if _digest(target) != (expected_sha256, size):
            raise BackupError("Existing generation artifact differs; preserve it for inspection")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(target)
    digest, copied = hashlib.sha256(), 0
    with source.open("rb") as inp, temporary.open("wb") as out:
        while copied < size:
            block = inp.read(min(1024 * 1024, size - copied))
            if not block:
                raise BackupError("Committed artifact bytes are missing")
            digest.update(block)
            copied += len(block)
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    if digest.hexdigest() != expected_sha256:
        raise BackupError("Artifact integrity changed during backup")
    # A generation has one owner; replace only the unpublished temporary name.
    temporary.replace(target)
    _sync_directory(target.parent)


@contextmanager
def _open(path, *, immutable=True):
    db = sqlite3.connect(path.as_uri() + ("?mode=ro&immutable=1" if immutable else "?mode=ro"), uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _integrity(db):
    if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
        raise BackupError("Catalog integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupError("Catalog reference integrity check failed")
    from .storage import SCHEMA_VERSION as supported
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if not 1 <= version <= supported:
        raise BackupError("Catalog schema is unsupported")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"files", "settings", "schema_migrations"}
    if version >= 2:
        required.update({"transfer_meta", "manifest_snapshots", "manifest_items", "verification_jobs", "transfer_events"})
    if version >= 3:
        required.update({"annotation_revisions", "import_jobs", "import_artifacts", "import_requests", "run_catalog_revisions", "run_catalog_state"})
    if version >= 4:
        required.update({"analyzer_jobs", "analysis_reports", "review_records"})
    if version >= 5:
        required.update({"columnar_jobs","columnar_artifacts","summary_reports","summary_artifacts","summary_run_cache"})
    if not required <= tables:
        raise BackupError("Catalog schema is incomplete")
    return version


def _references(db):
    """All current on-disk catalog contracts; hashes pin snapshot references."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    refs = {}

    def add(path, sha, size=None, kind="raw", prefix=False):
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise BackupError("Catalog artifact digest is invalid")
        if size is not None and (type(size) is not int or size < 0):
            raise BackupError("Catalog artifact size is invalid")
        item = {"path": path, "sha256": sha, "size": size, "kind": kind, "prefix": prefix}
        if path in refs and refs[path] != item:
            raise BackupError("Catalog artifact references disagree")
        refs[path] = item

    if "files" in tables:
        for row in db.execute("SELECT * FROM files ORDER BY id"):
            identity = _identity(row["id"])
            if row["state"] == "complete":
                add(f"archive/{identity}.logdata", row["sha256"], row["size"])
            elif row["offset"]:
                if "transfer_meta" in tables:
                    conflict = db.execute("SELECT error_code FROM transfer_meta WHERE file_id=?", (identity,)).fetchone()
                    if conflict and conflict["error_code"] == "identity_conflict":
                        continue  # These bytes are preserved as quarantine, never a resumable checkpoint.
                if "transfer_meta" not in tables:
                    raise BackupError("Partial checkpoints lack integrity metadata")
                meta = db.execute("SELECT checkpoint_offset,checkpoint_digest FROM transfer_meta WHERE file_id=?", (identity,)).fetchone()
                if meta is None or meta["checkpoint_offset"] != row["offset"]:
                    raise BackupError("Partial checkpoint metadata disagrees")
                add(f"partial/{identity}.part", meta["checkpoint_digest"], row["offset"], "committed_partial", True)
    if "import_artifacts" in tables:
        for row in db.execute("SELECT * FROM import_artifacts ORDER BY sha256"):
            add(row["relative_path"], row["sha256"], row["size_bytes"])
    if "import_jobs" in tables:
        for row in db.execute("SELECT * FROM import_jobs WHERE state IN ('succeeded','succeeded_with_unsupported') ORDER BY id"):
            for name in ("dataset", "manifest"):
                add(row[name + "_path"], row[name + "_sha256"], kind="derived")
    for table, parent, key in (("columnar_artifacts","columnar_jobs","job_id"),("summary_artifacts","summary_reports","report_id")):
        if table in tables:
            for row in db.execute("SELECT a.* FROM "+table+" a JOIN "+parent+" p ON p.id=a."+key+" WHERE p.state='succeeded'"):
                add(row["path"],row["sha256"],row["size_bytes"],kind="derived")
    return [refs[key] for key in sorted(refs)]


def _load_manifest(root):
    try:
        value = json.loads(_path(root, "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError("Generation manifest is unavailable or invalid") from None
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != SCHEMA_VERSION:
        raise BackupError("Backup manifest schema is unsupported")
    _identity(value.get("generation"))
    if type(value.get("catalog_schema_version")) is not int:
        raise BackupError("Backup catalog schema version must be an integer")
    if not isinstance(value.get("files"), list) or not value["files"]:
        raise BackupError("Backup manifest contains no files")
    paths = set()
    for item in value["files"]:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256", "size", "kind", "prefix"}
                or not isinstance(item["sha256"], str) or not _SHA.fullmatch(item["sha256"])
                or type(item["size"]) is not int or item["size"] < 0
                or type(item["prefix"]) is not bool
                or item["kind"] not in ("catalog", "raw", "derived", "committed_partial", "quarantine", "configuration")):

            raise BackupError("Backup manifest file metadata is invalid")
        _path(root, item["path"])
        if item["path"] in paths or item["path"] in ("manifest.json", ".owner.lock") or item["path"].endswith(".writing"):
            raise BackupError("Backup manifest has conflicting paths")
        paths.add(item["path"])
    if "catalog.sqlite3" not in paths:
        raise BackupError("Backup manifest lacks its catalog")
    for item in value["files"]:
        if item["path"] in ("catalog.sqlite3", "hub-config.json"):
            expected = "catalog" if item["path"] == "catalog.sqlite3" else "configuration"
            if item["kind"] != expected or item["prefix"]:
                raise BackupError("Backup manifest assigns an invalid reserved file role")
    return value


def verify(generation_dir):
    """Read-only verification; does not repair or migrate a backup."""
    root = Path(generation_dir).resolve()
    manifest = _load_manifest(root)
    indexed = {item["path"]: item for item in manifest["files"]}
    # Never publish unverified leftovers from a previous staging attempt, including
    # SQLite sidecars that a normal restored catalog connection would consume.
    expected = set(indexed) | {"manifest.json"}
    directories = {parent.as_posix() for name in expected for parent in PurePosixPath(name).parents
                   if parent != PurePosixPath(".")}
    for folder, subdirectories, filenames in os.walk(root, followlinks=False):
        for name in subdirectories + filenames:
            relative = (Path(folder) / name).relative_to(root).as_posix()
            candidate = _path(root, relative)
            if relative not in (directories if candidate.is_dir() else expected):
                raise BackupError("Generation contains unexpected files or directories")
    for item in manifest["files"]:
        if _digest(_path(root, item["path"])) != (item["sha256"], item["size"]):
            raise BackupError("Backup artifact integrity failed")
    with _open(_path(root, "catalog.sqlite3")) as db:
        if _integrity(db) != manifest.get("catalog_schema_version"):
            raise BackupError("Manifest and catalog schema disagree")
        references = _references(db)
        allowed = {"catalog.sqlite3", "hub-config.json"} | {reference["path"] for reference in references}
        allowed.update(item["path"] for item in manifest["files"] if item["kind"] == "quarantine"
                       and (item["path"].startswith("quarantine/") or item["path"].startswith("partial/") and item["path"].endswith(".invalid"))
                       and not item["prefix"])
        if indexed.keys() - allowed:
            raise BackupError("Manifest includes unsupported unreferenced files")
        if "hub-config.json" in indexed:
            Config.load(_path(root, "hub-config.json"))
        for reference in references:
            item = indexed.get(reference["path"])
            if (item is None or item["sha256"] != reference["sha256"] or item["kind"] != reference["kind"]
                    or item["prefix"] != reference["prefix"]
                    or reference["size"] is not None and item["size"] != reference["size"]):
                raise BackupError("Manifest does not reproduce catalog artifact references")
    return {"generation": manifest["generation"], "state": "verified", "file_count": len(indexed),
            "bytes": sum(item["size"] for item in indexed.values()),
            "manifest_sha256": hashlib.sha256(_json(manifest)).hexdigest(),
            "failure_domain_qualified": False}


class Backup:
    """One explicit destination per job. Existing generations and staging are retained."""
    def __init__(self, source_root, destination_root, *, stopping=lambda: False, clock_ns=time.time_ns):
        self.stopping, self.clock_ns = stopping, clock_ns
        self.source = Path(source_root).resolve()
        self.destination = Path(destination_root).resolve()
        if self.source == self.destination or self.source.is_relative_to(self.destination) or self.destination.is_relative_to(self.source):
            raise BackupError("Backup destination must be disjoint from the source data root")
        if not self.source.is_dir() or not (self.source / "catalog.sqlite3").is_file():
            raise BackupError("Source archive has no catalog")
        self.jobs = self.source / "exports" / "backup-jobs"

    def _check_stop(self, *unused):
        if self.stopping():
            raise BackupInterrupted("Backup stopped; staged generation retained")

    def status(self, generation=None):
        if generation is not None:
            folder = _path(self.jobs, _identity(generation))
            try:
                return json.loads(_path(folder, "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise BackupError("Backup job is unknown") from None
        if not self.jobs.exists():
            return []
        return [self.status(path.name) for path in sorted(self.jobs.iterdir()) if path.is_dir() and _ID.fullmatch(path.name)]

    def create(self, generation, *, configuration=None):
        generation = _identity(generation)
        folder = _path(self.jobs, generation)
        folder.mkdir(parents=True, exist_ok=True)
        with _owner(folder):
            job = self.status(generation) if (folder / "job.json").exists() else {
                "schema_version": 1, "generation": generation, "created_utc_ns": str(self.clock_ns()),
                "state": "capturing", "destination": str(self.destination), "error_code": None,
                "failure_domain_qualified": False}
            if job["destination"] != str(self.destination):
                raise BackupError("Retry must use the original explicit backup destination")
            if configuration is not None and not isinstance(configuration, Config):
                raise BackupError("Configuration snapshot must be a validated Config")
            pinned_config = _path(folder, "configuration.json")
            if configuration is not None:
                encoded_config = _json(configuration.as_dict())
                if pinned_config.exists() and pinned_config.read_bytes() != encoded_config:
                    raise BackupError("Generation configuration is immutable")
                if not pinned_config.exists() and (folder / "image/manifest.json").exists():
                    raise BackupError("Cannot add configuration to a captured generation")
                if not pinned_config.exists():
                    _write(pinned_config, encoded_config)
            try:
                _write(folder / "job.json", _json(job))
                image = _path(folder, "image")
                image.mkdir(exist_ok=True)
                catalog = _path(image, "catalog.sqlite3")
                if not catalog.exists():
                    temporary = _temporary(catalog)
                    # SQLite online backup copies a coherent WAL-aware database image.
                    with _open(_path(self.source, "catalog.sqlite3"), immutable=False) as source, closing(sqlite3.connect(temporary)) as target:
                        source.backup(target, pages=128, sleep=0.01, progress=self._check_stop)
                        target.execute("PRAGMA journal_mode=DELETE")
                        target.execute("PRAGMA synchronous=FULL")
                        _integrity(target)
                    with temporary.open("r+b") as stream:
                        os.fsync(stream.fileno())
                    temporary.replace(catalog)
                    _sync_directory(image)
                if not (image / "manifest.json").exists():
                    with _open(catalog) as db:
                        version, references = _integrity(db), _references(db)
                    files = [{"path": "catalog.sqlite3", "sha256": _digest(catalog)[0], "size": catalog.stat().st_size,
                              "kind": "catalog", "prefix": False}]
                    for reference in references:
                        self._check_stop()
                        source = _path(self.source, reference["path"])
                        if not source.exists() and reference["kind"] == "committed_partial":
                            source = _path(self.source, "archive/" + source.stem + ".logdata")
                        sha, size = _digest(source, reference["size"] if reference["prefix"] else None)
                        if sha != reference["sha256"] or reference["size"] is not None and size != reference["size"]:
                            raise BackupError("Catalog-referenced source artifact changed")
                        reference = dict(reference, size=size)
                        _copy(source, _path(image, reference["path"]), sha, size)
                        files.append(reference)
                    # Failed integrity/identity evidence is preserved separately and never
                    # labeled verified/resumable; these files have no structured path refs yet.
                    preserved = []
                    if (self.source / "partial").is_dir():
                        preserved.extend((self.source / "partial").glob("*.invalid"))
                    if (self.source / "quarantine").is_dir():
                        preserved.extend((self.source / "quarantine").rglob("*"))
                    for candidate in sorted(preserved):
                        self._check_stop()
                        relative = candidate.relative_to(self.source).as_posix()
                        source = _path(self.source, relative)
                        if not source.is_file():
                            continue
                        sha, size = _digest(source)
                        _copy(source, _path(image, relative), sha, size)
                        files.append({"path": relative, "sha256": sha, "size": size, "kind": "quarantine", "prefix": False})
                    if pinned_config.exists():
                        configuration = Config.load(pinned_config)
                        # Config contains only the hub's reviewed, secret-free supported fields.
                        config_path = image / "hub-config.json"
                        if config_path.exists() and config_path.read_bytes() != _json(configuration.as_dict()):
                            raise BackupError("Generation configuration is immutable")
                        _write(config_path, _json(configuration.as_dict()))
                        sha, size = _digest(config_path)
                        files.append({"path": "hub-config.json", "sha256": sha, "size": size, "kind": "configuration", "prefix": False})
                    manifest = {"schema_version": 1, "generation": generation, "created_utc_ns": job["created_utc_ns"],
                                "catalog_schema_version": version, "files": sorted(files, key=lambda item: item["path"]),
                                "source_scope": "catalog_references_committed_partials_and_quarantine", "failure_domain_qualified": False}
                    _write(image / "manifest.json", _json(manifest))
                elif configuration is not None:
                    config_path = image / "hub-config.json"
                    if not config_path.exists() or config_path.read_bytes() != _json(configuration.as_dict()):
                        raise BackupError("Generation configuration is immutable")
                result = verify(image)
                job.update(state="replicating", error_code=None, **{key: result[key] for key in ("file_count", "bytes", "manifest_sha256")})
                _write(folder / "job.json", _json(job))
                self.destination.mkdir(parents=True, exist_ok=True)
                with _owner(self.destination):
                    target = _path(self.destination, generation)
                    target.mkdir(exist_ok=True)
                    manifest = _load_manifest(image)
                    for item in manifest["files"]:
                        self._check_stop()
                        _copy(_path(image, item["path"]), _path(target, item["path"]), item["sha256"], item["size"])
                    target_manifest = _path(target, "manifest.json")
                    if target_manifest.exists() and target_manifest.read_bytes() != _json(manifest):
                        raise BackupError("Existing generation manifest differs")
                    _write(target_manifest, _json(manifest))
                    result = verify(target)
                job.update(state="complete", completed_utc_ns=str(self.clock_ns()), error_code=None)
                _write(folder / "job.json", _json(job))
                return dict(job)
            except (OSError, sqlite3.Error, BackupError) as exc:
                job.update(state="error", error_code=type(exc).__name__)
                _write(folder / "job.json", _json(job))
                raise


class _owner:
    def __init__(self, root):
        self.root = root
    def __enter__(self):
        _path(self.root, ".owner.lock")
        self.owner = DataRootOwner(self.root)
        return self.owner
    def __exit__(self, *args):
        self.owner.close()


def _restore(generation_dir, destination_root):
    """Explicit clean-directory restore. Refuse source/backup overlap and all existing contents."""
    generation = Path(generation_dir).resolve()
    destination = Path(destination_root).resolve()
    if destination == generation or destination.is_relative_to(generation) or generation.is_relative_to(destination):
        raise BackupError("Restore destination must be disjoint from its backup")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise BackupError("Restore destination must be empty")
    result = verify(generation)
    manifest = _load_manifest(generation)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / (".restore-" + destination.name + "-" + manifest["generation"])
    # The staging directory can only contain this exact generation; repeat safely.
    staging.mkdir(exist_ok=True)
    with _owner(staging):
        marker = _path(staging, "restore-plan.json")
        plan = {"destination": str(destination), "manifest_sha256": result["manifest_sha256"]}
        if marker.exists() and marker.read_bytes() != _json(plan):
            raise BackupError("Restore staging belongs to another generation")
        _write(marker, _json(plan))
        image = _path(staging, "image")
        image.mkdir(exist_ok=True)
        for item in manifest["files"]:
            _copy(_path(generation, item["path"]), _path(image, item["path"]), item["sha256"], item["size"])
        _write(image / "manifest.json", _json(manifest))
        verify(image)
        if destination.exists() and any(destination.iterdir()):
            raise BackupError("Restore destination became nonempty")
        if not destination.exists():
            image.rename(destination)
        else:
            # No destructive directory removal: publish into the explicitly empty root.
            # This path is resumable only through verification/new clean destination on a copy fault.
            for path in sorted(image.iterdir()):
                path.rename(destination / path.name)
        _sync_directory(destination.parent)
    return dict(result, state="restored", destination=str(destination), checkpoint_policy="committed_prefix_only")


def restore(generation_dir, destination_root):
    """Serialize all generations targeting this explicit empty destination."""
    destination = Path(destination_root).resolve()
    # Validate before creating even the operation's sibling lock directory.
    generation = Path(generation_dir).resolve()
    if destination == generation or destination.is_relative_to(generation) or generation.is_relative_to(destination):
        raise BackupError("Restore destination must be disjoint from its backup")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise BackupError("Restore destination must be empty")
    verify(generation)
    lock_name = ".restore-lock-" + hashlib.sha256(str(destination).encode()).hexdigest()[:24]
    with _owner(destination.parent / lock_name):
        return _restore(generation, destination)


class BackupScheduler:
    """Explicitly configured background scheduler. Durable generations are its queue."""
    def __init__(self, root, destination, *, interval=3600, retry=60, stopping=lambda: False,
                 clock_ns=time.time_ns, monotonic=time.monotonic, publish=lambda snapshot: None, configuration=None):
        for value in (interval, retry):
            if type(value) not in (int, float) or not 0 < value <= 86400 or not math.isfinite(value):
                raise BackupError("Backup scheduling intervals must be finite positive seconds")
        self.backup = Backup(root, destination, stopping=stopping, clock_ns=clock_ns)
        self.interval, self.retry, self.configuration = interval, retry, configuration
        self.clock_ns, self.monotonic, self.publish = clock_ns, monotonic, publish
        self.next_attempt = 0
        self.snapshot = {"schema_version": 1, "enabled": True, "state": "starting", "error_code": None,
                         "failure_domain_qualified": False, "last_generation": None,
                         "last_capture_utc_ns": None, "last_completed_utc_ns": None,
                         "pending_generations": 0, "retry_in_seconds": None}

    def tick(self):
        now = self.monotonic()
        if now < self.next_attempt:
            return False
        jobs = [job for job in self.backup.status() if job.get("destination") == str(self.backup.destination)]
        jobs.sort(key=lambda job: (int(job["created_utc_ns"]), job["generation"]))
        completed = [job for job in jobs if job["state"] == "complete"]
        pending = [job for job in jobs if job["state"] != "complete"]
        latest = completed[-1] if completed else None
        if latest:
            self.snapshot.update(last_generation=latest["generation"], last_capture_utc_ns=latest["created_utc_ns"],
                                 last_completed_utc_ns=latest["completed_utc_ns"])
        self.snapshot["pending_generations"] = len(pending)
        utc = self.clock_ns()
        due = latest is None or (utc - int(latest["created_utc_ns"])) / 1e9 >= self.interval
        if not pending and not due:
            self.snapshot.update(state="idle", error_code=None, retry_in_seconds=None)
            self.next_attempt = now + min(1, self.interval)
            self.publish(dict(self.snapshot))
            return False
        generation = pending[0]["generation"] if pending else "scheduled-" + str(utc)
        self.snapshot.update(state="running", active_generation=generation, pending_generations=max(1, len(pending)),
                             retry_in_seconds=None, error_code=None)
        self.publish(dict(self.snapshot))
        try:
            result = self.backup.create(generation, configuration=None if pending else self.configuration)
            self.snapshot.update(state="idle", active_generation=None, last_generation=generation,
                                 last_capture_utc_ns=result["created_utc_ns"], last_completed_utc_ns=result["completed_utc_ns"],
                                 pending_generations=max(0, len(pending) - 1), error_code=None, retry_in_seconds=None)
            self.next_attempt = self.monotonic() + min(self.interval, 1)
        except (BackupError, OSError, sqlite3.Error, RuntimeError) as exc:
            self.snapshot.update(state="retry_wait", error_code=type(exc).__name__, retry_in_seconds=self.retry)
            self.next_attempt = self.monotonic() + self.retry
        self.publish(dict(self.snapshot))
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--data-dir", required=True)
    create.add_argument("--destination", required=True)
    create.add_argument("--generation", required=True)
    create.add_argument("--config")
    status = commands.add_parser("status")
    status.add_argument("--data-dir", required=True)
    status.add_argument("--destination", required=True)
    status.add_argument("--generation")
    check = commands.add_parser("verify")
    check.add_argument("generation_dir")
    recover = commands.add_parser("restore")
    recover.add_argument("generation_dir")
    recover.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = Backup(args.data_dir, args.destination).create(args.generation, configuration=Config.load(Path(args.config)) if args.config else None)
        elif args.command == "status":
            result = Backup(args.data_dir, args.destination).status(args.generation)
        elif args.command == "verify":
            result = verify(args.generation_dir)
        else:
            result = restore(args.generation_dir, args.destination)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BackupError, OSError, sqlite3.Error, RuntimeError) as exc:
        # Exception class only: path/credential-bearing OS details never become diagnostics.
        print(json.dumps({"state": "error", "error_code": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
