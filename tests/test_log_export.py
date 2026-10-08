from pathlib import Path
import contextlib
import csv
import hashlib
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from robot_test_hub.config import Config, ConfigError
from robot_test_hub.demo import DemoSource
from robot_test_hub.log_export import LogExporter, ExportError, ExportInterrupted, main, verify
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog

FIXTURE = Path(__file__).parent / "fixtures/synthetic/alpha7-main.wpilog"


class LogExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root, self.destination = self.base / "source", self.base / "drive"
        self.root.mkdir()
        self.destination.mkdir()
        self.db = open_catalog(self.root)
        self.addCleanup(self.db.close)
        self.payload = FIXTURE.read_bytes()
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.original = self.add_file("a", self.payload)
        self.target = self.destination / "logs" / self.sha[:2] / (self.sha + ".wpilog")
        self.exporter = LogExporter(self.root, self.destination)

    def add_file(self, identity, payload, state="complete", name="practice.wpilog"):
        path = self.root / "archive" / (identity + ".logdata")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(payload)
        with self.db:
            self.db.execute("INSERT INTO files(id,name,size,sha256,created_at,offset,state) VALUES(?,?,?,?,0,?,?)",
                            (identity, name, len(payload), hashlib.sha256(payload).hexdigest(), len(payload), state))
        return path

    def test_exact_original_dedup_import_and_restart_no_recopy(self):
        self.add_file("b", self.payload)
        with self.db:
            self.db.execute("INSERT INTO import_artifacts(sha256,size_bytes,relative_path,source_type,created_utc_ns) VALUES(?,?,?,?,1)",
                            (self.sha,len(self.payload),"archive/a.logdata","synthetic"))
        self.exporter.tick()
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(self.exporter.snapshot["copied_files"], 1)
        self.assertFalse(verify(self.target)["cloud_upload_confirmed"])
        manifest = self.target.with_suffix(".json").read_bytes()
        before = self.target.stat().st_mtime_ns
        # Immutable local receipt avoids re-reading gigabytes on every poll.
        restarted = LogExporter(self.root, self.destination)
        with patch.object(restarted, "_copy", side_effect=AssertionError("recopy")):
            restarted.tick()
        self.assertEqual(self.target.stat().st_mtime_ns, before)
        self.assertEqual(self.target.with_suffix(".json").read_bytes(), manifest)
        self.assertEqual(self.original.read_bytes(), self.payload)
        self.assertFalse((self.destination / "catalog.sqlite3").exists())

    def test_open_partial_and_demo_bytes_are_not_published(self):
        self.add_file("partial", self.payload+b"partial", state="copying")
        self.add_file("opaque", b"synthetic opaque demonstration")
        self.exporter.tick()
        self.assertEqual(len(list(self.destination.rglob("*.wpilog"))), 1)
        self.assertEqual(self.exporter.snapshot["skipped_files"], 1)
        self.assertEqual(self.exporter.snapshot["pending_files"], 0)

    def test_unavailable_destination_is_not_created_and_retry_succeeds(self):
        missing = self.base / "missing-drive-folder"
        exporter = LogExporter(self.root, missing)
        with self.assertRaises(ExportError):
            exporter.tick()
        self.assertFalse(missing.exists())
        self.assertEqual(exporter.snapshot["state"], "retry_wait")
        missing.mkdir()
        exporter.tick()
        self.assertEqual(exporter.snapshot["copied_files"], 1)

    def test_corrupt_source_never_publishes_finished_log_or_metadata(self):
        self.original.write_bytes(self.payload[:-1]+b"x")
        with self.assertRaises(ExportError):
            self.exporter.tick()
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix(".json").exists())
        self.assertTrue(self.target.with_name(self.target.name+".writing").exists())

    def test_existing_corrupt_destination_is_preserved(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"preserve mismatched existing file")
        with self.assertRaises(ExportError):
            self.exporter.tick()
        self.assertEqual(self.target.read_bytes(), b"preserve mismatched existing file")
        self.assertFalse(self.target.with_suffix(".json").exists())

    def test_interruption_and_manifest_publication_failure_retry(self):
        calls = [0]
        def stop():
            calls[0] += 1
            return calls[0] >= 3
        exporter = LogExporter(self.root, self.destination, stopping=stop)
        with self.assertRaises(ExportInterrupted):
            exporter.tick()
        self.assertFalse(self.target.with_suffix(".json").exists())
        self.exporter.tick()
        self.assertEqual(verify(self.target)["sha256"], self.sha)
        # A lost local receipt after publication is adopted after full verification.
        receipt = self.exporter.receipts / (self.sha+".json")
        receipt.unlink()  # fixture-only fault injection
        self.exporter.tick()
        self.assertTrue(receipt.exists())

    def test_metadata_write_failure_reuses_existing_bytes_on_retry(self):
        from robot_test_hub import log_export
        original = log_export._write
        def fail_metadata(path, data):
            if path == self.target.with_suffix(".json"):
                raise OSError("private path and credentials must not reach status")
            return original(path, data)
        with patch.object(log_export, "_write", side_effect=fail_metadata):
            with self.assertRaises(OSError):
                self.exporter.tick()
        self.assertTrue(self.target.exists())
        self.assertFalse(self.target.with_suffix(".json").exists())
        self.assertNotIn("credentials", json.dumps(self.exporter.snapshot))
        before = self.target.stat().st_mtime_ns
        self.exporter.tick()
        self.assertEqual(self.target.stat().st_mtime_ns, before)
        self.assertEqual(verify(self.target)["sha256"], self.sha)

    def test_destination_loss_recopies_and_home_verification_detects_corruption(self):
        self.exporter.tick()
        self.target.unlink()  # fixture-only simulated lost sync destination
        self.exporter.tick()
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.target.write_bytes(self.payload[:-1]+b"x")
        with self.assertRaises(ExportError):
            verify(self.target)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["verify", str(self.target)]), 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "ExportError")

    def test_manifest_tamper_is_preserved_and_not_marked_complete(self):
        self.exporter.tick()
        path = self.target.with_suffix(".json")
        metadata = json.loads(path.read_text())
        metadata["size_bytes"] += 1
        path.write_text(json.dumps(metadata))
        with self.assertRaises(ExportError):
            self.exporter.tick()
        self.assertEqual(json.loads(path.read_text())["size_bytes"], len(self.payload)+1)

    def test_index_is_readable_and_spreadsheet_safe(self):
        with self.db:
            self.db.execute("UPDATE files SET name='=untrusted()' WHERE id='a'")
        self.exporter.tick()
        rows = list(csv.DictReader(io.StringIO((self.destination/"logs.csv").read_text())))
        self.assertEqual(rows[0]["original_name"], "'=untrusted()")
        self.assertEqual(rows[0]["wpilog"], "logs/"+self.sha[:2]+"/"+self.sha+".wpilog")

    def test_catalog_path_escape_and_link_refused(self):
        with self.db:
            self.db.execute("UPDATE files SET id='../escape'")
        with self.assertRaises(ValueError):
            self.exporter.tick()
        self.assertFalse((self.destination/"logs").exists())

    def test_opt_in_validation(self):
        self.assertIsNone(Config().log_export_destination)
        for value in ("", True, {}, str(self.root), str(self.root/"nested"), "bad\x00path"):
            with self.assertRaises(ConfigError):
                Config(data_dir=str(self.root), log_export_destination=value)
        for value in (0, True, -1, float("inf"), float("nan"), "60"):
            with self.assertRaises(ConfigError):
                Config(log_export_interval=value)
        config = Config(data_dir=str(self.root), log_export_destination=str(self.destination))
        self.assertEqual(config.log_export_interval, 60)
        with self.assertRaises(ExportError):
            LogExporter(self.root, self.root/"nested")

    def test_service_background_export_status_is_cached_and_redacted(self):
        config = Config(data_dir=str(self.root), log_export_destination=str(self.destination), log_export_interval=.05)
        service = HubService(config, DemoSource())
        service.set_paused(True)  # Local export does not contact the robot.
        server = create_http_server(service, service.source, 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":.01})
        thread.start()
        try:
            service.start()
            deadline = time.monotonic()+3
            while service.snapshot()["log_export"].get("copied_files") != 1:
                if time.monotonic()>deadline:
                    self.fail("Export worker did not publish")
                time.sleep(.01)
            connection = http.client.HTTPConnection("127.0.0.1",server.server_address[1],timeout=2)
            try:
                connection.request("GET","/api/v1/log-export")
                response = connection.getresponse()
                self.assertEqual(response.status,200)
                result = json.loads(response.read())
                self.assertFalse(result["cloud_upload_confirmed"])
                self.assertNotIn("destination",result)
                self.assertNotIn(str(self.destination),json.dumps(result))
                self.assertTrue(service.snapshot()["paused_by_operator"])
            finally:
                connection.close()
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()
            self.assertTrue(service.close())

    def test_disabled_service_does_not_create_export_receipts(self):
        service = HubService(Config(data_dir=str(self.root)), DemoSource())
        try:
            self.assertEqual(service.snapshot()["log_export"]["state"], "disabled")
            service.start()
        finally:
            self.assertTrue(service.close())
        self.assertFalse((self.root/"exports/log-sharing").exists())


if __name__ == "__main__":
    unittest.main()
