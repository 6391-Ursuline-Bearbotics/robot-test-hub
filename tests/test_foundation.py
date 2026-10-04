import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from robot_test_hub.collector import Collector, LogFile, RobotStatus
from robot_test_hub.config import Config, ConfigError
from robot_test_hub.diagnostics import Diagnostics, redact
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import DataRootOwner, OwnershipError, SchemaError, open_catalog


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for condition")


class SmallSource:
    def __init__(self, blocked=None, fail=None):
        self.enabled = False
        self.polls = 0
        self.blocked = blocked
        self.fail = fail
        self.entered = threading.Event()
        self.release = threading.Event()
        self.data = b"abcdefgh"
        self.file = LogFile("fixture", "fixture.demo", len(self.data), hashlib.sha256(self.data).hexdigest(), 1)

    def status(self):
        self.polls += 1
        if self.fail == "status":
            raise RuntimeError("password=hunter-secret sftp://operator:other-secret@host/")
        return RobotStatus(self.enabled, time.monotonic(), "boot", int(self.enabled))

    def _stall(self, operation):
        if self.blocked == operation:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("Test failed to release blocked I/O")

    def list_closed_files(self):
        self._stall("discovery")
        return [self.file]

    def read(self, identity, offset, length):
        self._stall("read")
        if self.fail == "read":
            raise RuntimeError("unlabelled-hunter-secret")
        return self.data[offset:offset + length]

    def cancel(self):
        self.release.set()

    def snapshot(self):
        return {"enabled": self.enabled, "connected": True, "stale": False, "speed_mib": 1}

    def configure(self, action, value):
        if action == "enabled" and type(value) is bool:
            self.enabled = value
        else:
            raise ValueError("Invalid demo action")


class ConfigTests(unittest.TestCase):
    def test_defaults_and_file_overrides(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"schema_version": 1, "port": 6500, "idle_delay": 10}))
            config = Config.load(path, port=6501)
            self.assertEqual(config.port, 6501)
            self.assertEqual(config.idle_delay, 10)
            self.assertEqual(config.as_dict()["schema_version"], 1)

    def test_invalid_values_have_field_errors(self):
        for field, values in {"schema_version": [True, 0, 2], "port": [True, 0, 65536, 1.0],
                "chunk_size": [True, 0, 16 * 1024 * 1024 + 1], "idle_delay": [True, -1, float("nan"), float("inf")],
                "freshness": [0, True, float("nan"), 10**400], "tick_interval": [0, threading.TIMEOUT_MAX + 1],
                "status_interval": [0, 10**400], "shutdown_timeout": [0, threading.TIMEOUT_MAX + 1],
                "data_dir": ["", "\x00", None]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ConfigError, field):
                    Config(**{field: value})

    def test_invalid_file_does_not_echo_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for content in ('{"password":"sensitive"}', '["sensitive"]', '{"password":"sensitive",'):
                path.write_text(content)
                with self.assertRaises(ConfigError) as caught:
                    Config.load(path)
                self.assertNotIn("sensitive", str(caught.exception))


class StorageTests(unittest.TestCase):
    def test_fresh_and_legacy_migration_preserve_every_row(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = sqlite3.connect(root / "catalog.sqlite3")
            db.executescript("""CREATE TABLE files (id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL, created_at REAL NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'queued', error TEXT);
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);""")
            digest = hashlib.sha256(b"abcdefgh").hexdigest()
            rows = [("partial", "original.demo", 8, digest, 1.0, 4, "queued", None),
                    ("complete", "done.demo", 8, digest, 2.0, 8, "complete", None),
                    ("failed", "failed.demo", 8, digest, 3.0, 0, "error", "Checksum mismatch")]
            db.executemany("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)", rows)
            db.execute("INSERT INTO settings VALUES ('paused','1')")
            db.commit()
            db.close()
            owner = DataRootOwner(root)
            try:
                db = open_catalog(root)
                self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM files ORDER BY created_at")], rows)
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0], "1")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 1)
                db.close()
            finally:
                owner.close()
            (root / "partial").mkdir()
            (root / "archive").mkdir()
            (root / "partial/partial.part").write_bytes(b"abcd")
            (root / "archive/complete.logdata").write_bytes(b"abcdefgh")
            collector = Collector(root, SmallSource())
            try:
                snapshot = collector.snapshot()
                self.assertTrue(snapshot["paused_by_operator"])
                migrated = {row["id"]: row for row in snapshot["files"]}
                self.assertEqual(migrated["partial"]["offset"], 4)
                self.assertEqual(migrated["complete"]["state"], "complete")
                self.assertEqual(migrated["failed"]["state"], "error")
            finally:
                collector.close()
        with tempfile.TemporaryDirectory() as folder:
            db = open_catalog(Path(folder))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM files").fetchone()[0], 0)
            db.close()

    def test_future_schema_rejected_without_database_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = sqlite3.connect(root / "catalog.sqlite3")
            db.execute("PRAGMA user_version=99")
            db.close()
            original = (root / "catalog.sqlite3").read_bytes()
            with self.assertRaisesRegex(SchemaError, "newer"):
                open_catalog(root)
            self.assertEqual((root / "catalog.sqlite3").read_bytes(), original)

    def test_failed_migration_rolls_back_ddl_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = sqlite3.connect(root / "catalog.sqlite3")
            db.execute("CREATE TABLE files (id TEXT)")
            db.close()
            for _ in range(2):
                with self.assertRaises(SchemaError):
                    open_catalog(root)
                db = sqlite3.connect(root / "catalog.sqlite3")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("files",)])
                db.close()

    def test_owner_blocks_second_process_without_changing_any_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            owner = DataRootOwner(root)
            try:
                (root / "evidence.part").write_bytes(b"important")
                def fingerprint():
                    return {path.name: (path.stat().st_size, path.stat().st_mtime_ns) if path.name == ".owner.lock"
                            else path.read_bytes() for path in root.iterdir()}
                before = fingerprint()
                code = "from pathlib import Path; import sys; from robot_test_hub.storage import DataRootOwner; DataRootOwner(Path(sys.argv[1]))"
                result = subprocess.run([sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already owned", result.stderr)
                self.assertEqual(fingerprint(), before)
            finally:
                owner.close()
            second = DataRootOwner(root)
            second.close()

    def test_failed_collector_initialization_releases_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = sqlite3.connect(root / "catalog.sqlite3")
            db.execute("PRAGMA user_version=99")
            db.close()
            with self.assertRaises(SchemaError):
                Collector(root, SmallSource())
            owner = DataRootOwner(root)
            owner.close()

    def test_directory_failure_releases_open_database_and_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "partial").write_bytes(b"not a directory")
            with self.assertRaises(OSError):
                Collector(root, SmallSource())
            owner = DataRootOwner(root)
            owner.close()
            db = open_catalog(root)
            db.close()


class ServiceTests(unittest.TestCase):
    def exercise_blocked_io(self, operation):
        with tempfile.TemporaryDirectory() as folder:
            source = SmallSource(blocked=operation)
            factory = Collector
            if operation == "digest":
                class BlockedDigestCollector(Collector):
                    def _digest(self, path):
                        source._stall("digest")
                        return super()._digest(path)
                factory = BlockedDigestCollector
            service = HubService(Config(data_dir=folder, idle_delay=0, chunk_size=8), source, collector_factory=factory)
            httpd = create_http_server(service, source, 0)
            server_thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
            server_thread.start()
            service.start()
            try:
                self.assertTrue(source.entered.wait(5))
                state = service.snapshot()["state"]
                self.assertEqual(state, {"read": "downloading", "discovery": "discovering", "digest": "verifying"}[operation])
                polls = source.polls
                wait_for(lambda: source.polls > polls)
                connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=1)
                connection.request("GET", "/api/status")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())["source_status"]["fresh"])
                connection.request("POST", "/api/control", json.dumps({"action": "paused", "value": True}),
                    {"Content-Type": "application/json", "X-Hub-Request": "1"})
                self.assertEqual(connection.getresponse().status, 200)
                connection.close()
                db = sqlite3.connect(Path(folder) / "catalog.sqlite3")
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0], "1")
                db.close()
                self.assertTrue(service.snapshot()["paused_by_operator"])
                source.enabled = True
                wait_for(lambda: service.snapshot()["source_status"]["enabled"] is True)
                self.assertTrue(service.close())
                self.assertTrue(all(not thread.is_alive() for thread in service.threads))
            finally:
                source.release.set()
                service.close()
                httpd.shutdown()
                server_thread.join(2)
                httpd.server_close()

    def test_api_and_status_are_responsive_during_read(self):
        self.exercise_blocked_io("read")

    def test_api_and_status_are_responsive_during_discovery(self):
        self.exercise_blocked_io("discovery")

    def test_api_and_status_are_responsive_during_hash(self):
        self.exercise_blocked_io("digest")

    def test_pause_is_durable_while_startup_recovery_hashes_second_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "archive").mkdir()
            source = SmallSource(blocked="digest")
            db = open_catalog(root)
            with db:
                for identity in ("first", "second"):
                    (root / "archive" / (identity + ".logdata")).write_bytes(source.data)
                    db.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?)", (
                        identity, identity + ".demo", len(source.data), source.file.sha256,
                        1, len(source.data), "complete", None))
            db.close()

            class BlockSecondRecoveryDigest(Collector):
                def _digest(self, path):
                    if path.name == "second.logdata":
                        source._stall("digest")
                    return super()._digest(path)

            service = HubService(Config(data_dir=folder), source, collector_factory=BlockSecondRecoveryDigest)
            httpd = create_http_server(service, source, 0)
            server_thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
            server_thread.start()
            service.start()
            try:
                self.assertTrue(source.entered.wait(5))
                polls = source.polls
                wait_for(lambda: source.polls > polls)
                connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=1)
                try:
                    connection.request("POST", "/api/control", json.dumps({"action": "paused", "value": True}),
                        {"Content-Type": "application/json", "X-Hub-Request": "1"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                finally:
                    connection.close()
                db = sqlite3.connect(root / "catalog.sqlite3")
                try:
                    self.assertEqual(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0], "1")
                finally:
                    db.close()
                self.assertTrue(service.snapshot()["paused_by_operator"])
            finally:
                source.release.set()
                service.close()
                httpd.shutdown()
                server_thread.join(2)
                httpd.server_close()

    def test_worker_failure_visible_and_exception_secrets_absent(self):
        for failure in ("read", "status"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                service = HubService(Config(data_dir=folder, idle_delay=0), SmallSource(fail=failure))
                service.start()
                try:
                    worker = "collector" if failure == "read" else "status"
                    wait_for(lambda: service.snapshot()["workers"][worker]["state"] == "failed")
                    self.assertEqual(service.snapshot()["state"], "attention")
                    export = json.dumps(service.diagnostics.snapshot()) + json.dumps(service.snapshot())
                    self.assertNotIn("hunter-secret", export)
                    self.assertNotIn("other-secret", export)
                    self.assertIn("worker_failed", export)
                finally:
                    self.assertTrue(service.close())
                self.assertNotIn("hunter-secret", (Path(folder) / "diagnostics/service.jsonl").read_text())

    def test_caught_up_is_replaced_before_blocked_rediscovery(self):
        with tempfile.TemporaryDirectory() as folder:
            source = SmallSource()
            advance = [0]
            class RediscoverCollector(Collector):
                def tick(self):
                    if self.state == "caught_up":
                        source.blocked = "discovery"
                        advance[0] += 6
                    super().tick()
            def factory(*args, **kwargs):
                return RediscoverCollector(*args, **kwargs, clock=lambda: time.monotonic() + advance[0])
            service = HubService(Config(data_dir=folder, idle_delay=0, freshness=100), source, collector_factory=factory)
            service.start()
            try:
                self.assertTrue(source.entered.wait(5))
                snapshot = service.snapshot()
                self.assertEqual(snapshot["state"], "discovering")
                self.assertFalse(snapshot["discovery_complete"])
                self.assertIsNone(snapshot["eta_seconds"])
            finally:
                self.assertTrue(service.close())

    def test_service_preference_generation_preserves_all_transitions(self):
        with tempfile.TemporaryDirectory() as folder:
            service = HubService(Config(data_dir=folder), SmallSource())
            try:
                self.assertEqual(service._preference(), (False, 0))
                service.set_paused(True)
                service.set_paused(False)
                self.assertEqual(service._preference(), (False, 2))
                service.set_paused(False)
                self.assertEqual(service._preference(), (False, 2))
            finally:
                self.assertTrue(service.close())

    def test_shutdown_timeout_keeps_owner_until_worker_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = SmallSource(blocked="read")
            source.cancel = lambda: None
            service = HubService(Config(data_dir=folder, idle_delay=0, shutdown_timeout=0.05), source)
            service.start()
            try:
                self.assertTrue(source.entered.wait(5))
                self.assertFalse(service.close())
                with self.assertRaises(OwnershipError):
                    DataRootOwner(Path(folder))
                self.assertIn("shutdown_timeout", json.dumps(service.diagnostics.snapshot()))
            finally:
                source.release.set()
                wait_for(lambda: all(not thread.is_alive() for thread in service.threads))
                self.assertTrue(service.close())
            owner = DataRootOwner(Path(folder))
            owner.close()

    def test_redaction_and_diagnostic_rotation(self):
        self.assertEqual(redact({"credential_reference": "private", "url": "sftp://person:pass@host/path", "nested": ["token=abc Bearer def"]}),
                         {"credential_reference": "[redacted]", "url": "sftp://[redacted]@host/path", "nested": ["token=[redacted] Bearer [redacted]"]})
        with tempfile.TemporaryDirectory() as folder:
            diagnostic = Diagnostics(Path(folder))
            diagnostic.handler.maxBytes = 128
            for index in range(110):
                diagnostic.record("test", "Safe event")
            self.assertEqual(len(diagnostic.snapshot()["events"]), 100)
            diagnostic.close()
            self.assertLessEqual(len(list((Path(folder) / "diagnostics").iterdir())), 4)


class SubprocessLifecycleTests(unittest.TestCase):
    def test_foreground_signal_shutdown_preserves_checkpoint_pause_and_releases_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
            process = subprocess.Popen([sys.executable, "-m", "robot_test_hub.server", "--port", str(port),
                "--data-dir", folder, "--idle-delay", "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
            try:
                def status():
                    try:
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.3)
                        connection.request("GET", "/api/status")
                        result = json.loads(connection.getresponse().read())
                        connection.close()
                        return result
                    except (OSError, ValueError):
                        return {}
                wait_for(lambda: any(row["offset"] > 0 for row in status().get("files", [])), timeout=10)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                connection.request("POST", "/api/control", json.dumps({"action": "paused", "value": True}),
                    {"Content-Type": "application/json", "X-Hub-Request": "1"})
                self.assertEqual(connection.getresponse().status, 200)
                connection.close()
                before = status()
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn("DEMO ONLY", stdout)
                db = sqlite3.connect(Path(folder) / "catalog.sqlite3")
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0], "1")
                offsets = dict(db.execute("SELECT id,offset FROM files"))
                self.assertTrue(any(offset > 0 for offset in offsets.values()))
                for row in before["files"]:
                    self.assertGreaterEqual(offsets[row["id"]], row["offset"])
                db.close()
                owner = DataRootOwner(Path(folder))
                owner.close()
                source = SmallSource()
                collector = Collector(Path(folder), source)
                try:
                    self.assertTrue(collector.snapshot()["paused_by_operator"])
                    for row in collector.snapshot()["files"]:
                        self.assertEqual(row["offset"], offsets[row["id"]])
                finally:
                    collector.close()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
