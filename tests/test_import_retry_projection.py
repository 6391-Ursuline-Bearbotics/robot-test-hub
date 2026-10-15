"""Read-only bounded projections for exact, explicit local import retries."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import import_retry, processing_api
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE, EXTRACTOR_VERSION, MAPPING_REVISION

STAMP = 9007199254740993123


class ImportRetryProjectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="hub retry projection ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.db = open_catalog(self.root)
        self.addCleanup(self.db.close)
        self.sequence = 0

    def job(self, *, state="failed", error="import_io_or_integrity", profile=PROFILE,
            extractor=EXTRACTOR_VERSION, mapping=MAPPING_REVISION, source=None, attempts=1, stamp=STAMP):
        self.sequence += 1
        identity = "job-" + str(self.sequence)
        sha = source or hashlib.sha256(identity.encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO import_artifacts(sha256,size_bytes,relative_path,source_type,created_utc_ns) VALUES (?,12,?,'MANUAL_LOCAL',1)", (sha, 'raw/' + sha + '.wpilog'))
            self.db.execute("INSERT INTO import_jobs(id,artifact_sha256,extractor_version,profile,mapping_revision,state,attempts,error_code,error,created_utc_ns,updated_utc_ns) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                            (identity, sha, extractor, profile, mapping, state, attempts, error, "PRIVATE_ERROR_DO_NOT_PROJECT", stamp))
        return identity, sha

    def page(self, sha, **query):
        return processing_api.get(self.root, processing_api.PREFIX + "/" + sha,
                                  dict(section="imports", limit="20", **query), {"state": "running"})

    def test_current_retryable_failures_only_and_exact_nanosecond_pins(self):
        cases = [({}, True, "eligible"), ({"error": "extractor_failure"}, True, "eligible"),
                 ({"error": "resource_limit"}, False, "error_not_retryable"),
                 ({"state": "invalid"}, False, "state_not_retryable"),
                 ({"state": "unsupported"}, False, "state_not_retryable"),
                 ({"state": "checksum_mismatch"}, False, "state_not_retryable"),
                 ({"state": "succeeded"}, False, "state_not_retryable"),
                 ({"state": "running"}, False, "state_not_retryable"),
                 ({"state": "pending"}, False, "state_not_retryable"),
                 ({"extractor": "old-extractor"}, False, "unsupported_version"),
                 ({"profile": "old-profile"}, False, "unsupported_version")]
        for options, eligible, reason in cases:
            with self.subTest(options=options):
                identity, sha = self.job(**options)
                item = self.page(sha)["items"][0]
                self.assertEqual(item["retry"]["eligible"], eligible)
                self.assertEqual(item["retry"]["reason"], reason)
                self.assertEqual(item["updated_utc_ns"], str(STAMP))
                self.assertIsInstance(item["updated_utc_ns"], str)
                self.assertEqual(item["retry"]["pins"]["expected_updated_utc_ns"], str(STAMP))
                self.assertEqual(item["retry"]["pins"]["job_id"], identity)
                self.assertEqual(item["retry"]["pins"]["source_sha256"], sha)
                self.assertIsNone(item["latest_retry_request"])

    def test_get_projection_is_readonly_and_never_constructs_importer_or_reads_original(self):
        identity, sha = self.job()
        original_connect = sqlite3.connect
        writes = []
        def connect(database, *args, **kwargs):
            self.assertIn("?mode=ro", str(database))
            self.assertTrue(kwargs.get("uri"))
            db = original_connect(database, *args, **kwargs)
            # SQLite initializes eponymous JSON virtual-table metadata lazily;
            # prepare those read-only builtins before denying user DDL.
            db.execute("SELECT value FROM json_each('[]')").fetchall()
            db.execute("SELECT value FROM json_tree('{}')").fetchall()
            forbidden = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                         sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TRIGGER,
                         sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TRIGGER,
                         sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
            def authorize(action, *_):
                if action in forbidden:
                    writes.append(action)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        with patch.object(processing_api.sqlite3, "connect", side_effect=connect), \
             patch("robot_test_hub.importer.Importer.__init__", side_effect=AssertionError("Constructor during GET")), \
             patch.object(import_retry, "install_schema", side_effect=AssertionError("DDL during GET")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("Original read during GET")), \
             patch.object(Path, "open", side_effect=AssertionError("Original read during GET")):
            page = self.page(sha)
        self.assertTrue(page["read_only"])
        self.assertEqual(writes, [])
        self.assertEqual(page["items"][0]["id"], identity)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM import_retry_requests").fetchone()[0], 0)
        self.assertFalse((self.root / "raw/never-read.wpilog").exists())

    def test_latest_persistent_request_and_failed_outcome_remain_separate_from_job(self):
        identity, sha = self.job()
        pins = self.page(sha)["items"][0]["retry"]["pins"]
        request = import_retry.enqueue(self.db, identity, dict(request_id="a" * 32, **pins))
        item = self.page(sha)["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertEqual(item["latest_retry_request"]["state"], "pending")
        self.assertEqual(item["latest_retry_request"]["id"], request["request"]["id"])
        self.assertEqual(item["latest_retry_request"]["expected_updated_utc_ns"], str(STAMP))
        with self.db:
            self.db.execute("UPDATE import_retry_requests SET state='completed',result_state='failed',result_attempts=2,result_updated_utc_ns=?,error_code='extractor_failure' WHERE id=?", (str(STAMP + 1), "a" * 32))
        item = self.page(sha)["items"][0]
        self.assertEqual(item["state"], "failed")
        self.assertEqual(item["attempts"], 1)
        self.assertEqual(item["latest_retry_request"]["state"], "completed")
        self.assertEqual(item["latest_retry_request"]["result_state"], "failed")
        self.assertEqual(item["latest_retry_request"]["result_attempts"], 2)
        self.assertEqual(item["latest_retry_request"]["result_updated_utc_ns"], str(STAMP + 1))

    def test_projection_redacts_arbitrary_errors_and_private_version_tokens(self):
        identity, sha = self.job(profile=r"C:\PRIVATE_PATH_DO_NOT_PROJECT")
        page = self.page(sha)
        self.assertEqual(page["items"][0]["profile"], "unavailable")
        self.assertFalse(page["items"][0]["retry"]["eligible"])
        encoded = json.dumps(page)
        self.assertNotIn("PRIVATE_PATH", encoded)
        self.assertNotIn("PRIVATE_ERROR", encoded)
        identity, sha = self.job()
        pins = self.page(sha)["items"][0]["retry"]["pins"]
        import_retry.enqueue(self.db, identity, dict(request_id="b" * 32, **pins))
        with self.db:
            self.db.execute("UPDATE import_retry_requests SET error_code=? WHERE id=?", ("PRIVATE_ERROR_" * 10000, "b" * 32))
        self.assertNotIn("PRIVATE_ERROR", json.dumps(self.page(sha)))

    def test_import_reference_paging_and_latest_receipt_use_bounded_indexed_metadata(self):
        source = hashlib.sha256(b"same original across old profiles").hexdigest()
        for number in range(25):
            self.job(source=source, extractor="old-" + str(number))
        first = self.page(source)
        self.assertEqual(first["returned"], 20)
        self.assertEqual(first["total"], 25)
        second = self.page(source, cursor=first["next_cursor"])
        self.assertEqual(second["returned"], 5)
        self.assertFalse(set(item["id"] for item in first["items"]) & set(item["id"] for item in second["items"]))
        plan = " ".join(str(tuple(row)) for row in self.db.execute("EXPLAIN QUERY PLAN SELECT id FROM import_retry_requests WHERE job_id=? ORDER BY rowid DESC LIMIT 1", ("job-1",)))
        self.assertIn("import_retry_job", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_invalid_attempt_metadata_cannot_become_an_action(self):
        for options in ({"attempts": -1}, {"stamp": "not-a-timestamp"}):
            with self.subTest(options=options):
                _, sha = self.job(**options)
                item = self.page(sha)["items"][0]
                self.assertFalse(item["retry"]["eligible"])
                self.assertEqual(item["retry"]["reason"], "invalid_job_metadata")
                self.assertIsNone(item["retry"]["pins"])
