"""Single-writer ownership and explicit SQLite schema migrations."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3

SCHEMA_VERSION = 2


class OwnershipError(RuntimeError):
    pass


class SchemaError(RuntimeError):
    pass


class DataRootOwner:
    """OS-held advisory lock. Never unlink: contenders must lock the same inode."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = (self.root / ".owner.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise OwnershipError("Data directory is already owned by another hub process; stop it or choose --data-dir") from None
        self.file.seek(0)
        self.file.truncate()
        self.file.write(f"pid={os.getpid()}\n".encode())
        self.file.flush()

    def close(self):
        if self.file.closed:
            return
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def open_catalog(root: Path) -> sqlite3.Connection:
    db = sqlite3.connect(root / "catalog.sqlite3", check_same_thread=False, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < 0 or version > SCHEMA_VERSION:
            raise SchemaError("Catalog schema is newer than this hub; use the matching newer hub version")
        db.execute("BEGIN IMMEDIATE")
        with db:
            if version == 0:
                # v0 is the original prototype: preserve both tables verbatim.
                db.execute("""CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, created_at REAL NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'queued', error TEXT)""")
                db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("""CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY, applied_utc TEXT NOT NULL)""")
                db.execute("INSERT INTO schema_migrations VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
                db.execute("PRAGMA user_version=1")
            for table, columns in (("files", {"id", "name", "size", "sha256", "created_at", "offset", "state", "error"}),
                                   ("settings", {"key", "value"}), ("schema_migrations", {"version", "applied_utc"})):
                actual = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                if not columns <= actual:
                    raise SchemaError("Catalog schema is incomplete; preserve the data directory and restore a valid catalog")
            if version <= 1:
                from .transfer_schema import migrate_transfer
                migrate_transfer(db)
            for table in ('transfer_meta', 'manifest_snapshots', 'manifest_items', 'verification_jobs', 'transfer_events'):
                if not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    raise SchemaError("Catalog transfer schema is incomplete; preserve the data directory")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db
    except BaseException:
        db.close()
        raise
