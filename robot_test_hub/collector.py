"""Idle-only, checkpointed collection of immutable files.

The source contract deliberately requires explicit closed-file identity and a fresh
robot status. A directory listing and a last-known disabled value are insufficient.
All collector methods must be serialized by the host (the server uses one lock).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable, Protocol


@dataclass(frozen=True)
class RobotStatus:
    enabled: bool | None
    observed_at: float  # local monotonic receipt time; never a remote wall clock
    boot_id: str
    generation: int  # changes on EVERY mode/connection transition


@dataclass(frozen=True)
class LogFile:
    id: str  # stable across filenames/reconnects; never reused for different content
    name: str
    size: int
    sha256: str
    created_at: float

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.id):
            raise ValueError("Unsafe or missing file identity")
        if self.size < 0 or not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise ValueError("Invalid closed-file size or checksum")
        if not math.isfinite(self.created_at):
            raise ValueError("Invalid file creation time")


class Source(Protocol):
    def status(self) -> RobotStatus: ...
    def list_closed_files(self) -> list[LogFile]: ...
    def read(self, file_id: str, offset: int, length: int) -> bytes:
        """Bounded read, no unbounded prefetch; throw OSError on disconnect."""
        ...


class Collector:
    def __init__(self, root: Path, source: Source, *, idle_delay: float = 10,
                 freshness: float = 1, chunk_size: int = 256 * 1024,
                 clock: Callable[[], float] = time.monotonic):
        if idle_delay < 0 or freshness <= 0 or chunk_size <= 0:
            raise ValueError("Invalid collector limits")
        self.root, self.source, self.clock = root, source, clock
        self.idle_delay, self.freshness, self.chunk_size = idle_delay, freshness, chunk_size
        self.partial = root / "partial"
        self.archive = root / "archive"
        self.partial.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "catalog.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, created_at REAL NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'queued', error TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        row = self.db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
        self.paused = row is not None and row[0] == "1"
        self.state, self.reason, self.error = "waiting", "Waiting for fresh robot status", None
        self.disabled_since: float | None = None
        self.status_token: tuple[str, int] | None = None
        self.last_discovery: float | None = None
        self.samples: list[tuple[float, int]] = []
        self.active_id: str | None = None
        self._recover()

    def _digest(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

    def _recover(self) -> None:
        # A crash can occur between disk fsync, checkpoint commit, and final rename.
        for row in self.db.execute("SELECT * FROM files").fetchall():
            final = self.archive / (row["id"] + ".logdata")
            part = self.partial / (row["id"] + ".part")
            if final.exists():
                if final.stat().st_size == row["size"] and self._digest(final) == row["sha256"]:
                    self.db.execute("UPDATE files SET offset=size,state='complete',error=NULL WHERE id=?", (row["id"],))
                    continue
                final.replace(self.partial / (row["id"] + ".invalid"))
            if row["state"] == "complete":
                self.db.execute("UPDATE files SET offset=0,state='queued' WHERE id=?", (row["id"],))
                offset = 0
            else:
                offset = row["offset"]
            actual = part.stat().st_size if part.exists() else 0
            if actual < offset:
                offset = 0
                self.db.execute("UPDATE files SET offset=0,state='queued' WHERE id=?", (row["id"],))
            if part.exists():
                with part.open("r+b") as f:
                    f.truncate(offset)
        self.db.commit()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('paused',?)", ("1" if paused else "0",))
        self.db.commit()
        self.disabled_since = None
        self.samples.clear()
        self.state = "paused" if paused else "waiting"
        self.reason = "Paused by operator" if paused else "Waiting for idle window"

    def retry_errors(self) -> None:
        self.db.execute("UPDATE files SET state='queued',error=NULL WHERE state='error'")
        self.db.commit()

    def _gate(self) -> bool:
        status = self.source.status()
        now = self.clock()
        token = (status.boot_id, status.generation)
        if token != self.status_token:
            self.disabled_since = None
            self.samples.clear()
            self.last_discovery = None
        self.status_token = token
        age = now - status.observed_at
        if self.paused:
            self.state, self.reason = "paused", "Paused by operator"
        elif not math.isfinite(age) or not 0 <= age <= self.freshness or status.enabled is None:
            self.state, self.reason = "paused", "Robot status unknown or stale"
        elif status.enabled:
            self.state, self.reason = "paused", "Robot enabled"
        else:
            if self.disabled_since is None:
                self.disabled_since = now
            remaining = self.idle_delay - (now - self.disabled_since)
            if remaining <= 0:
                return True
            self.state, self.reason = "waiting", f"Idle window begins in {math.ceil(remaining)} s"
            return False
        self.disabled_since = None
        self.samples.clear()
        return False

    def _discover(self) -> None:
        files = self.source.list_closed_files()
        # Validate the whole manifest before changing the persistent queue.
        seen: set[str] = set()
        for item in files:
            item.validate()
            if item.id in seen:
                raise ValueError("Duplicate file identity in manifest")
            seen.add(item.id)
            old = self.db.execute("SELECT size,sha256 FROM files WHERE id=?", (item.id,)).fetchone()
            if old and (old["size"] != item.size or old["sha256"] != item.sha256):
                raise ValueError(f"Closed file changed identity: {item.id}")
        with self.db:
            for item in files:
                self.db.execute("""INSERT INTO files(id,name,size,sha256,created_at) VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
                    (item.id, item.name, item.size, item.sha256, item.created_at))
        self.last_discovery = self.clock()

    def tick(self) -> None:
        """Attempt at most one bounded source read. Caller schedules subsequent ticks."""
        self.active_id = None
        try:
            if not self._gate():
                return
            self.error = None
            if self.last_discovery is None or self.clock() - self.last_discovery >= 5:
                self._discover()
            if not self._gate():
                return
            row = self.db.execute("SELECT * FROM files WHERE state='queued' ORDER BY created_at DESC,id LIMIT 1").fetchone()
            if row is None:
                errors = self.db.execute("SELECT COUNT(*) FROM files WHERE state='error'").fetchone()[0]
                self.state = "attention" if errors else "caught_up"
                self.reason = "Some files need retry or investigation" if errors else "All discovered closed files archived"
                self.samples.clear()
                return
            self.active_id = row["id"]
            self.state, self.reason = "downloading", "Collecting during idle time"
            part = self.partial / (row["id"] + ".part")
            offset = row["offset"]
            if offset < row["size"]:
                token = self.status_token
                start = self.clock()
                block = self.source.read(row["id"], offset, min(self.chunk_size, row["size"] - offset))
                elapsed = self.clock() - start
                if not self._gate() or token != self.status_token:
                    return  # abandon the in-flight block if permission changed
                if not block or len(block) > min(self.chunk_size, row["size"] - offset):
                    raise OSError("Source returned an empty or oversized block")
                with part.open("r+b" if part.exists() else "w+b") as f:
                    if f.seek(0, 2) < offset:
                        raise OSError("Partial file is shorter than its checkpoint; restart to recover")
                    f.seek(offset)
                    f.write(block)
                    f.truncate()
                    f.flush()
                    os.fsync(f.fileno())
                # Durably save bytes BEFORE advancing the catalog checkpoint.
                with self.db:
                    self.db.execute("UPDATE files SET offset=? WHERE id=?", (offset + len(block), row["id"]))
                duration = max(self.clock() - start, elapsed, 0.001)
                self.samples.append((duration, len(block)))
                while len(self.samples) > 1 and sum(s[0] for s in self.samples[1:]) >= 10:
                    self.samples.pop(0)
                offset += len(block)
            if offset == row["size"]:
                part.touch(exist_ok=True)
                self.state, self.reason = "verifying", "Verifying downloaded file locally"
                if self._digest(part) != row["sha256"]:
                    part.replace(self.partial / (row["id"] + ".invalid"))
                    with self.db:
                        self.db.execute("UPDATE files SET offset=0,state='error',error='Checksum mismatch' WHERE id=?", (row["id"],))
                    self.state, self.reason = "attention", "Checksum mismatch; original remains on source"
                    return
                part.replace(self.archive / (row["id"] + ".logdata"))
                with self.db:
                    self.db.execute("UPDATE files SET state='complete',error=NULL WHERE id=?", (row["id"],))
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.state, self.reason, self.error = "paused", "Collection interrupted; will retry", str(exc)
            self.disabled_since = None
            self.samples.clear()
            self.last_discovery = None

    def snapshot(self) -> dict:
        rows = [dict(row) for row in self.db.execute("SELECT * FROM files ORDER BY created_at DESC,id")]
        pending = [r for r in rows if r["state"] != "complete"]
        remaining = sum(r["size"] - r["offset"] for r in pending)
        duration = sum(s[0] for s in self.samples)
        rate = sum(s[1] for s in self.samples) / duration if duration >= 1 else None
        return {"state": self.state, "reason": self.reason, "error": self.error,
                "paused_by_operator": self.paused, "files": rows, "active_id": self.active_id,
                "remaining_bytes": remaining, "pending_files": len(pending),
                "completed_files": len(rows) - len(pending), "bytes_per_second": rate,
                "eta_seconds": remaining / rate if rate and not any(r["state"] == "error" for r in pending) else None,
                "eta_kind": "idle transfer time; excludes future runs, waiting, and verification",
                "discovery_complete": self.last_discovery is not None}

    def close(self) -> None:
        self.db.close()
