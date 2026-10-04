import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.importer import ArchiveConflict, Importer, install_schema
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE
from test_wpilog import FIXTURES, log_bytes, start, wire_record


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = open_catalog(self.root)
        self.addCleanup(lambda: self.db.close())
        with self.db:
            install_schema(self.db)
        self.importer = Importer(self.root, self.db)

    def import_fixture(self, name="alpha7-main.wpilog", **kwargs):
        return self.importer.import_file(FIXTURES / name, source_type="SYNTHETIC", **kwargs)

    def test_retained_bytes_idempotency_provenance_and_restart(self):
        source = FIXTURES / "alpha7-main.wpilog"
        original = source.read_bytes()
        job = self.import_fixture(expected_sha256=hashlib.sha256(original).hexdigest())
        self.assertEqual(job["state"], "succeeded")
        manifest = self.importer.read_manifest(job["id"])
        self.assertEqual((self.root / manifest["raw_path"]).read_bytes(), original)
        self.assertEqual(source.read_bytes(), original)
        renamed = self.root / "renamed.wpilog"
        renamed.write_bytes(original)
        duplicate = self.importer.import_file(renamed, source_type="SYNTHETIC")
        self.assertEqual(job, duplicate)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM import_artifacts").fetchone()[0], 1)
        self.assertEqual(len(self.importer.list_jobs()), 1)
        self.assertEqual({row[0] for row in self.db.execute("SELECT original_name FROM import_requests")}, {"alpha7-main.wpilog", "renamed.wpilog"})
        self.assertEqual(manifest["counts"]["cycle"], 7)
        self.assertEqual(manifest["metadata_last_values"]["/Metadata/BootId"], "synthetic-boot-a")
        rows = list(self.importer.iter_dataset(job["id"]))
        self.assertTrue(all(r["source_sha256"] == job["artifact_sha256"] and r["profile"] == PROFILE for r in rows))
        self.assertEqual(len(list(self.importer.iter_dataset(job["id"], "cycle"))), 7)
        self.db.close()
        self.db = open_catalog(self.root)
        self.importer = Importer(self.root, self.db)
        self.assertEqual(self.import_fixture(), job)
        self.assertEqual(self.importer.read_manifest(job["id"]), manifest)

    def test_corrupted_original_stays_archived_and_format_invalid(self):
        original = (FIXTURES / "alpha7-main.wpilog").read_bytes()[:-1]
        path = self.root / "truncated.wpilog"
        path.write_bytes(original)
        job = self.importer.import_file(path, expected_sha256=hashlib.sha256(original).hexdigest())
        self.assertEqual(job["state"], "invalid")
        self.assertEqual(job["error_code"], "invalid_format")
        artifact = self.db.execute("SELECT * FROM import_artifacts").fetchone()
        self.assertEqual(artifact["format_state"], "invalid")
        self.assertEqual((self.root / artifact["relative_path"]).read_bytes(), original)
        self.assertIsNone(job["dataset_path"])
        with self.assertRaises(ValueError):
            list(self.importer.iter_dataset(job["id"]))
        self.assertEqual(self.importer.import_file(path), job)

    def test_wrong_checksum_cannot_be_a_format_pass_and_retry_is_explicit(self):
        job = self.import_fixture(expected_sha256="0" * 64)
        self.assertEqual(job["state"], "checksum_mismatch")
        self.assertEqual(self.db.execute("SELECT format_state FROM import_artifacts").fetchone()[0], "unchecked")
        self.assertEqual(self.import_fixture(), job)
        retried = self.import_fixture(retry=True)
        self.assertEqual(retried["state"], "succeeded")
        with self.assertRaises(ArchiveConflict):
            self.import_fixture(expected_sha256="0" * 64)
        with self.assertRaises(ValueError):
            self.import_fixture(expected_sha256="bad")

    def test_unknown_profiles_and_mapping_are_persistent_and_do_not_block_good(self):
        unknown = self.import_fixture(profile="2026")
        self.assertEqual(unknown["state"], "unsupported")
        self.assertEqual(unknown["error_code"], "unsupported_profile")
        good = self.import_fixture()
        self.assertEqual(good["state"], "succeeded")
        mapping = self.import_fixture(mapping_revision="future")
        self.assertEqual(mapping["state"], "unsupported")
        self.assertEqual(len(self.importer.list_jobs()), 3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM import_artifacts").fetchone()[0], 1)

    def test_unknown_fields_are_retained_with_explicit_partial_support(self):
        path = self.root / "unknown-field.wpilog"
        path.write_bytes(log_bytes(start(2, "/Unknown", "proto:Thing"), wire_record(2, b"opaque")))
        job = self.importer.import_file(path)
        self.assertEqual(job["state"], "succeeded_with_unsupported")
        self.assertEqual(self.importer.read_manifest(job["id"])["unsupported_types"], ["proto:Thing"])
        observation = next(r for r in self.importer.iter_dataset(job["id"], "observation") if r["field"] == "/Unknown")
        self.assertEqual(observation["validity"], "unsupported")
        self.assertIsNone(observation["value"])
        self.assertEqual(bytes.fromhex(observation["raw_hex"]), b"opaque")

    def test_interrupted_running_job_restarts_without_duplicate_artifacts(self):
        job = self.import_fixture()
        with self.db:
            self.db.execute("UPDATE import_jobs SET state='running' WHERE id=?", (job["id"],))
        recovered = self.import_fixture()
        self.assertEqual(recovered["state"], "succeeded")
        self.assertEqual(recovered["attempts"], 2)
        self.assertEqual(len(self.importer.list_jobs()), 1)
        self.assertEqual(len(list((self.root / "derived").rglob("signals.jsonl"))), 1)

    def test_failure_after_dataset_publish_is_retryable(self):
        from robot_test_hub import importer as module
        original = module._publish
        def fail_manifest(temporary, target):
            if target.name == "manifest.json":
                raise OSError("injected disk failure")
            return original(temporary, target)
        with patch.object(module, "_publish", side_effect=fail_manifest):
            job = self.import_fixture()
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error_code"], "import_io_or_integrity")
        recovered = self.import_fixture(retry=True)
        self.assertEqual(recovered["state"], "succeeded")
        self.assertEqual(len(list((self.root / "derived").rglob("signals.jsonl"))), 1)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_raw_and_derived_integrity_conflicts_never_overwrite_original(self):
        job = self.import_fixture()
        manifest = self.importer.read_manifest(job["id"])
        raw = self.root / manifest["raw_path"]
        raw.write_bytes(b"tampered")
        with self.assertRaises(ArchiveConflict):
            self.importer.read_manifest(job["id"])
        with self.assertRaises(ArchiveConflict):
            self.import_fixture()
        self.assertEqual(raw.read_bytes(), b"tampered")
        raw.write_bytes((FIXTURES / "alpha7-main.wpilog").read_bytes())
        dataset = self.root / job["dataset_path"]
        dataset.write_bytes(b"tampered")
        with self.assertRaises(ArchiveConflict):
            self.import_fixture()
        with self.assertRaises(ArchiveConflict):
            list(self.importer.iter_dataset(job["id"]))
        self.assertEqual(dataset.read_bytes(), b"tampered")

    def test_schema_install_obeys_transaction_and_preserves_catalog_version(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("PRAGMA user_version=9")
        db.execute("BEGIN")
        install_schema(db)
        self.assertTrue(db.in_transaction)
        db.rollback()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'import_%'").fetchone()[0], 0)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)
        with db:
            install_schema(db)
            install_schema(db)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 9)

    def test_catalog_path_escape_is_rejected(self):
        job = self.import_fixture()
        with self.db:
            self.db.execute("UPDATE import_jobs SET dataset_path='../escape' WHERE id=?", (job["id"],))
        with self.assertRaises(ArchiveConflict):
            self.importer.read_manifest(job["id"])

    def test_verified_transfer_requires_and_checks_the_transfer_digest(self):
        source = FIXTURES / "alpha7-main.wpilog"
        with self.assertRaises(ValueError):
            self.importer.import_file(source, source_type="VERIFIED_TRANSFER")
        self.assertEqual(len(self.importer.list_jobs()), 0)
        bad = self.importer.import_file(source, source_type="VERIFIED_TRANSFER", expected_sha256="0" * 64)
        self.assertEqual(bad["state"], "checksum_mismatch")
        self.assertEqual(self.db.execute("SELECT source_type FROM import_artifacts").fetchone()[0], "TRANSFER_CHECKSUM_MISMATCH")
        # A new correctly claimed source never silently reinterprets an earlier mismatch.
        good = self.importer.import_file(source, source_type="VERIFIED_TRANSFER", expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), retry=True)
        self.assertEqual(good["state"], "succeeded")
        self.assertEqual(self.importer.read_manifest(good["id"])["source_type"], "VERIFIED_TRANSFER")
        claims = self.db.execute("SELECT expected_sha256 FROM import_requests").fetchall()
        self.assertEqual(len(claims), 2)

    def test_manual_cli_returns_persistent_states_and_decimal_timestamp_json(self):
        command = [sys.executable, "-m", "robot_test_hub.importer", str((FIXTURES / "alpha7-main.wpilog").resolve()),
                   "--data-dir", str(self.root / "cli"), "--profile", PROFILE, "--synthetic"]
        first = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        job = json.loads(first.stdout)
        self.assertEqual(job["state"], "succeeded")
        self.assertIsInstance(job["created_utc_ns"], str)
        duplicate = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(json.loads(duplicate.stdout), job)
        command[command.index(PROFILE)] = "2026"
        unsupported = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(unsupported.returncode, 1, unsupported.stderr)
        self.assertEqual(json.loads(unsupported.stdout)["state"], "unsupported")


if __name__ == "__main__":
    unittest.main()
