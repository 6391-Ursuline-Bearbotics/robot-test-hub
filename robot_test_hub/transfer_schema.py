"""Version 2 tables leave the original files/settings columns intact."""


def migrate_transfer(db):
    statements = (
        """CREATE TABLE transfer_meta (
            file_id TEXT PRIMARY KEY REFERENCES files(id), attempts INTEGER NOT NULL DEFAULT 0,
            next_retry REAL NOT NULL DEFAULT 0, error_code TEXT, priority INTEGER NOT NULL DEFAULT 0,
            urgent INTEGER NOT NULL DEFAULT 0, available INTEGER NOT NULL DEFAULT 1,
            checkpoint_digest TEXT, checkpoint_offset INTEGER NOT NULL DEFAULT 0,
            format_status TEXT NOT NULL DEFAULT 'not_checked', selection_reason TEXT,
            last_selected REAL)""",
        """CREATE TABLE manifest_snapshots (
            snapshot_id INTEGER PRIMARY KEY, revision TEXT, complete INTEGER NOT NULL DEFAULT 0,
            observed_utc_ns TEXT NOT NULL, observed_monotonic_ns TEXT NOT NULL,
            open_bytes INTEGER NOT NULL DEFAULT 0, pending_digest_bytes INTEGER NOT NULL DEFAULT 0,
            error_code TEXT)""",
        """CREATE TABLE manifest_items (
            snapshot_id INTEGER NOT NULL REFERENCES manifest_snapshots(snapshot_id),
            file_id TEXT NOT NULL, metadata_json TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,file_id))""",
        """CREATE TABLE verification_jobs (
            file_id TEXT PRIMARY KEY REFERENCES files(id), state TEXT NOT NULL,
            digest TEXT, error_code TEXT, elapsed_seconds REAL,
            verified_utc_ns TEXT, format_status TEXT NOT NULL DEFAULT 'not_checked')""",
        """CREATE TABLE transfer_events (
            event_id INTEGER PRIMARY KEY, file_id TEXT REFERENCES files(id),
            code TEXT NOT NULL, monotonic_ns TEXT NOT NULL, detail TEXT)""",
        "CREATE INDEX transfer_retry ON transfer_meta(next_retry,available)",
        "INSERT INTO transfer_meta(file_id) SELECT id FROM files",
        "INSERT INTO schema_migrations VALUES (2,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        "PRAGMA user_version=2",
    )
    for statement in statements:
        db.execute(statement)
