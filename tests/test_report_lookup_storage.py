"""Schema-7 migration and coherent backup of resumable report lookup state."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import report_lookup, storage
from robot_test_hub.backup import Backup, BackupError, restore, verify
from robot_test_hub.storage import SCHEMA_VERSION, SchemaError, open_catalog


SOURCE = "a" * 64
LOOKUP_TABLES = ("report_lookup_state", "report_lookup_jobs", "report_source_refs")


def report_body(identity):
    return json.dumps({"report_id": identity, "run_id": "run-" + identity,
                       "checks": [{"provenance": {"source_hashes": [SOURCE]}}]},
                      sort_keys=True, separators=(",", ":"))


def insert_report(db, identity, body=None):
    db.execute("INSERT INTO analysis_reports VALUES (?, '{}', ?, '1')",
               (identity, report_body(identity) if body is None else body))


def remove_lookup_schema(db):
    for name in report_lookup.TRIGGER_SQL:
        db.execute('DROP TRIGGER "' + name + '"')
    for name in reversed(LOOKUP_TABLES):
        db.execute('DROP TABLE "' + name + '"')
    db.execute("DELETE FROM schema_migrations WHERE version>=7")
    db.execute("PRAGMA user_version=6")


def lookup_snapshot(db):
    return {table: [tuple(row) for row in db.execute('SELECT * FROM "' + table + '" ORDER BY 1,2')]
            for table in LOOKUP_TABLES}


class ReportLookupStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hub schema seven ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.sequence = 0

    def fresh(self):
        self.sequence += 1
        root = self.base / ("source-" + str(self.sequence))
        root.mkdir()
        with closing(open_catalog(root)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        return root

    def legacy(self, reports=()):
        root = self.fresh()
        with closing(sqlite3.connect(root / "catalog.sqlite3")) as db, db:
            remove_lookup_schema(db)
            for identity, body in reports:
                insert_report(db, identity, body)
            db.execute("INSERT INTO settings VALUES ('migration-preserve','original')")
        return root

    def test_fresh_schema_and_legacy_upgrade_never_read_report_bodies(self):
        root = self.legacy((("legacy-invalid", "not JSON: preserve exact original bytes"),))
        original_connect = sqlite3.connect
        denied = []
        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            def authorize(action, table, column, *_):
                if action == sqlite3.SQLITE_READ and table == "analysis_reports" and column in ("result_json", "provenance_json"):
                    denied.append((table, column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        with patch.object(storage.sqlite3, "connect", side_effect=connect):
            with closing(open_catalog(root)) as db:
                self.assertEqual(denied, [])
                # Python 3.10 does not support clearing this callback with None.
                db.set_authorizer(lambda *_: sqlite3.SQLITE_OK)
                self.assertEqual(db.execute("SELECT result_json FROM analysis_reports").fetchone()[0],
                                 "not JSON: preserve exact original bytes")
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='migration-preserve'").fetchone()[0], "original")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=7").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT discovery_complete FROM report_lookup_state").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM report_lookup_jobs").fetchone()[0], 0)
                report_lookup.validate_schema(db)
                with db:
                    insert_report(db, "new-report")
                self.assertEqual(db.execute("SELECT state FROM report_lookup_jobs WHERE report_id='new-report'").fetchone()[0], "pending")

    def test_failed_schema_seven_migration_rolls_back_all_ddl_and_retries(self):
        root = self.legacy((("legacy", report_body("legacy")),))
        install = report_lookup.install_schema
        def fail(db):
            install(db)
            db.execute("CREATE TABLE accidental_seven_partial(value TEXT)")
            raise RuntimeError("Injected schema seven migration failure")
        with patch.object(report_lookup, "install_schema", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "Injected"):
                open_catalog(root)
        with closing(sqlite3.connect(root / "catalog.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'report_lookup_%' OR name IN ('report_source_refs','report_source_sha','accidental_seven_partial')").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version>=7").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT result_json FROM analysis_reports").fetchone()[0], report_body("legacy"))
        with closing(open_catalog(root)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(report_lookup.coverage(db)["state"], "incomplete")

    def corrupt(self, db, variant):
        if variant == "missing-table":
            db.execute("DROP TABLE report_source_refs")
        elif variant == "wrong-columns":
            db.execute("ALTER TABLE report_lookup_jobs RENAME COLUMN run_id TO wrong_run_id")
        elif variant == "missing-index":
            db.execute("DROP INDEX report_source_sha")
        elif variant == "wrong-index":
            db.execute("DROP INDEX report_source_sha")
            db.execute("CREATE INDEX report_source_sha ON report_source_refs(report_id,source_sha256)")
        elif variant in ("missing-trigger", "wrong-trigger", "inert-trigger", "case-altered-trigger"):
            name = next(iter(report_lookup.TRIGGER_SQL))
            original = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            db.execute('DROP TRIGGER "' + name + '"')
            if variant == "wrong-trigger":
                db.execute('CREATE TRIGGER "' + name + '" AFTER INSERT ON analysis_reports BEGIN SELECT 1; END')
            elif variant == "inert-trigger":
                db.execute(original.replace("BEGIN", "WHEN 0 BEGIN", 1))
            elif variant == "case-altered-trigger":
                db.execute(original.replace("'pending'", "'PENDING'"))
        elif variant == "missing-state":
            db.execute("DELETE FROM report_lookup_state")
        else:
            self.fail("Unknown corruption fixture")

    def test_schema_seven_missing_or_altered_contracts_are_rejected_without_repair(self):
        for variant in ("missing-table", "wrong-columns", "missing-index", "wrong-index",
                        "missing-trigger", "wrong-trigger", "inert-trigger", "case-altered-trigger", "missing-state"):
            with self.subTest(variant=variant):
                root = self.fresh()
                with closing(sqlite3.connect(root / "catalog.sqlite3")) as db, db:
                    self.corrupt(db, variant)
                original = (root / "catalog.sqlite3").read_bytes()
                with self.assertRaisesRegex(SchemaError, "report lookup"):
                    open_catalog(root)
                self.assertEqual((root / "catalog.sqlite3").read_bytes(), original)

    def test_trigger_whitespace_normalization_accepts_known_semantics(self):
        root = self.fresh()
        with closing(sqlite3.connect(root / "catalog.sqlite3")) as db, db:
            for name in report_lookup.TRIGGER_SQL:
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                db.execute('DROP TRIGGER "' + name + '"')
                db.execute(sql.replace("CREATE TRIGGER", "CREATE    TRIGGER").replace("\n", "   "))
        with closing(open_catalog(root)) as db:
            report_lookup.validate_schema(db)
            with db:
                insert_report(db, "new-report")
            self.assertEqual(db.execute("SELECT state FROM report_lookup_jobs").fetchone()[0], "pending")

    def test_backup_restore_preserves_mid_backfill_generations_failures_and_resumes(self):
        root = self.legacy((("legacy-a", report_body("legacy-a")), ("legacy-b", "not JSON"),
                            ("legacy-c", report_body("legacy-c"))))
        with closing(open_catalog(root)) as db:
            report_lookup.step(db, discovery_limit=1)
            report_lookup.step(db, discovery_limit=1)
            with db:
                insert_report(db, "new-pending")
            self.assertEqual(db.execute("SELECT discovery_complete FROM report_lookup_state").fetchone()[0], 0)
            self.assertEqual({row[0] for row in db.execute("SELECT state FROM report_lookup_jobs")}, {"ready", "failed", "pending"})
            expected = lookup_snapshot(db)
            expected_reports = [tuple(row) for row in db.execute("SELECT * FROM analysis_reports ORDER BY report_id")]
        destination = self.base / "backup"
        Backup(root, destination).create("mid-backfill")
        image = destination / "mid-backfill"
        self.assertEqual(verify(image)["file_count"], 1, "Lookup data belongs entirely to the catalog")
        manifest = json.loads((image / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["catalog_schema_version"], 7)
        restored = self.base / "restored"
        restore(image, restored)
        self.assertEqual(hashlib.sha256((restored / "catalog.sqlite3").read_bytes()).hexdigest(), manifest["files"][0]["sha256"])
        with closing(open_catalog(restored)) as db:
            self.assertEqual(lookup_snapshot(db), expected)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM analysis_reports ORDER BY report_id")], expected_reports)
            report_lookup.validate_schema(db)
            for _ in range(6):
                report_lookup.step(db, discovery_limit=1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_lookup_jobs WHERE state='pending'").fetchone()[0], 0)
            self.assertEqual(report_lookup.coverage(db)["state"], "failed", "Failed originals are not silently converted to successful reports")
            with db:
                db.execute("UPDATE analysis_reports SET result_json=? WHERE report_id='legacy-b'", (report_body("legacy-b"),))
            self.assertEqual(db.execute("SELECT state FROM report_lookup_jobs WHERE report_id='legacy-b'").fetchone()[0], "pending", "Restored invalidation triggers remain active")
            for _ in range(6):
                if report_lookup.step(db, discovery_limit=1)["complete"]:
                    break
            self.assertTrue(report_lookup.coverage(db)["complete"])
            rows = db.execute("SELECT j.report_id,j.body_sha256,r.source_sha256 FROM report_lookup_jobs j JOIN report_source_refs r ON j.report_id=r.report_id AND j.generation=r.generation ORDER BY j.report_id").fetchall()
            self.assertEqual(len(rows), 4)
            for identity, digest, source in rows:
                self.assertEqual(source, SOURCE)
                self.assertEqual(digest, hashlib.sha256(report_body(identity).encode()).hexdigest())

    def test_backup_rejects_lookup_corruption_even_with_rehashed_catalog(self):
        root = self.fresh()
        for variant in ("missing-table", "wrong-columns", "missing-index", "wrong-index", "missing-trigger", "inert-trigger", "case-altered-trigger", "missing-state"):
            with self.subTest(variant=variant):
                generation = "bad-" + variant
                destination = self.base / "backup"
                Backup(root, destination).create(generation)
                image = destination / generation
                with closing(sqlite3.connect(image / "catalog.sqlite3")) as db, db:
                    self.corrupt(db, variant)
                manifest = json.loads((image / "manifest.json").read_text(encoding="utf-8"))
                payload = (image / "catalog.sqlite3").read_bytes()
                manifest["files"][0].update(sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
                (image / "manifest.json").write_bytes(json.dumps(manifest).encode())
                with self.assertRaisesRegex(BackupError, "report lookup"):
                    verify(image)
                with self.assertRaises(BackupError):
                    restore(image, self.base / ("never-restore-" + variant))
                self.assertFalse((self.base / ("never-restore-" + variant)).exists())

    def test_schema_six_backup_stays_readable_and_upgrades_only_on_open(self):
        root = self.legacy((("legacy", report_body("legacy")),))
        destination = self.base / "backup"
        Backup(root, destination).create("schema-six")
        image = destination / "schema-six"
        self.assertEqual(verify(image)["file_count"], 1)
        restored = self.base / "restored-six"
        restore(image, restored)
        with closing(sqlite3.connect(restored / "catalog.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='report_lookup_state'").fetchone())
        with closing(open_catalog(restored)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(report_lookup.coverage(db)["state"], "incomplete")

    def test_oldest_supported_backup_remains_readable_before_catalog_upgrade(self):
        root = self.base / "schema-one-source"
        root.mkdir()
        with closing(sqlite3.connect(root / "catalog.sqlite3")) as db, db:
            db.execute("CREATE TABLE files (id TEXT PRIMARY KEY,name TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,created_at REAL NOT NULL,offset INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'queued',error TEXT)")
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY,applied_utc TEXT NOT NULL)")
            db.execute("INSERT INTO schema_migrations VALUES (1,'historical')")
            db.execute("INSERT INTO settings VALUES ('oldest-preserved','yes')")
            db.execute("PRAGMA user_version=1")
        destination = self.base / "oldest-backup"
        Backup(root, destination).create("schema-one")
        image = destination / "schema-one"
        self.assertEqual(verify(image)["file_count"], 1)
        self.assertEqual(json.loads((image / "manifest.json").read_text(encoding="utf-8"))["catalog_schema_version"], 1)
        restored = self.base / "restored-one"
        restore(image, restored)
        with closing(sqlite3.connect(restored / "catalog.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        with closing(open_catalog(restored)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='oldest-preserved'").fetchone()[0], "yes")
            self.assertTrue(report_lookup.coverage(db)["complete"])
