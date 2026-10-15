"""Actual process loss and restart with synthetic transfer checkpoints only."""
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from robot_test_hub import launcher

ROOT = Path(__file__).resolve().parents[1]
CRASH_CHILD = """
import os, pathlib, sys, threading, time
from robot_test_hub import server
trigger = pathlib.Path(sys.argv[1])
def crash():
    while not trigger.exists():
        time.sleep(.02)
    os._exit(73)
threading.Thread(target=crash, daemon=True).start()
raise SystemExit(server.main(sys.argv[2:]))
"""


class ApplicationCrashTest(unittest.TestCase):
    def wait_until(self, condition, child, output, message, *, timeout=15, diagnostics=None):
        deadline = time.monotonic() + timeout
        while not condition():
            if child.poll() is not None or time.monotonic() >= deadline:
                output.seek(0)
                detail = ""
                if diagnostics is not None:
                    try:
                        detail = "\nSynthetic transfer state: " + json.dumps(diagnostics(), sort_keys=True)
                    except Exception as exc:
                        detail = "\nSynthetic diagnostics unavailable: " + type(exc).__name__
                self.fail(message + ": " + output.read().decode("utf-8", errors="replace") + detail)
            time.sleep(.03)

    def request(self, port, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            headers = {"X-Hub-Request": "1", "Content-Type": "application/json"} if body else {}
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            self.assertEqual(response.status, 200, raw)
            return json.loads(raw)
        finally:
            conn.close()

    def test_abrupt_exit_recovers_runtime_pause_and_transfer_checkpoint(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config = root / "hub.json"
            config.write_text(json.dumps(dict(schema_version=1, port=port,
                data_dir="archive", idle_delay=0, chunk_size=262144)), encoding="utf-8")
            profile = root / "launch.json"
            launcher.configure_profile(profile, dict(schema_version=1, mode="demo", hub_config="hub.json"))
            runtime = launcher.runtime_path(profile)
            trigger = root / "crash-now"
            arguments = launcher.server_arguments(profile, launcher.load_profile(profile))
            env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(ROOT))
            children = []
            with (root / "output.txt").open("w+b") as output:
                try:
                    first = subprocess.Popen([sys.executable, "-X", "utf8", "-c", CRASH_CHILD,
                        str(trigger), *arguments], cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
                    children.append(first)
                    self.wait_until(runtime.is_file, first, output, "Synthetic hub did not start")
                    record = launcher._runtime(profile)
                    self.assertEqual(record["schema_version"], 2)
                    self.assertEqual(launcher._control(record, "GET")["state"], "running")
                    catalog = root / "archive" / "catalog.sqlite3"
                    selected = []
                    def checkpoint():
                        with closing(sqlite3.connect(catalog, timeout=2)) as db:
                            if not selected:
                                row = db.execute("SELECT id,offset FROM files WHERE offset>0 ORDER BY offset DESC LIMIT 1").fetchone()
                                if row:
                                    selected.append(row[0])
                                    return row[1]
                                return 0
                            row = db.execute("SELECT offset FROM files WHERE id=?", (selected[0],)).fetchone()
                        return row[0] if row else 0
                    self.wait_until(lambda: checkpoint() >= 65536, first, output,
                                    "Synthetic transfer did not commit a checkpoint")
                    self.request(port, "POST", "/api/control", dict(action="paused", value=True))
                    offset = checkpoint()
                    self.assertGreaterEqual(offset, 65536)
                    original = runtime.read_bytes()
                    trigger.write_bytes(b"test-only crash")
                    self.assertEqual(first.wait(timeout=15), 73)
                    self.assertEqual(runtime.read_bytes(), original)
                    second = subprocess.Popen([sys.executable, "-X", "utf8", "-m",
                        "robot_test_hub.launcher", "run", "--profile", str(profile)],
                        cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
                    children.append(second)
                    self.wait_until(lambda: runtime.is_file() and runtime.read_bytes() != original,
                                    second, output, "Runtime recovery did not finish")
                    successor = launcher._runtime(profile)
                    self.assertNotEqual(successor["instance_id"], record["instance_id"])
                    self.assertEqual(launcher._control(successor, "GET")["state"], "running")
                    status = self.request(port, "GET", "/api/status")
                    self.assertTrue(status["paused_by_operator"])
                    self.assertGreaterEqual(checkpoint(), offset)
                    # Preserved partial bytes are still exactly the original
                    # synthetic recording, not an appended/restarted duplicate.
                    partials = list((root / "archive" / "partial").iterdir())
                    data_files = [p for p in partials if p.is_file() and p.stat().st_size >= offset]
                    expected_byte = int(selected[0].rsplit("-", 1)[1])
                    self.assertTrue(any(p.read_bytes()[:offset] == bytes([expected_byte]) * offset for p in data_files))
                    self.request(port, "POST", "/api/control", dict(action="speed_mib", value=32))
                    self.request(port, "POST", "/api/control", dict(action="paused", value=False))
                    def complete():
                        with closing(sqlite3.connect(catalog, timeout=2)) as db:
                            row = db.execute("SELECT state,offset,size FROM files WHERE id=?", (selected[0],)).fetchone()
                        return row is not None and row[0] == "complete" and row[1] == row[2]
                    def completion_diagnostics():
                        with closing(sqlite3.connect(catalog, timeout=2)) as db:
                            rows = db.execute("SELECT id,state,offset,size,error FROM files ORDER BY id LIMIT 10").fetchall()
                        return {"selected": selected[0], "files": [list(row) for row in rows]}
                    # This exercises eventual recovery of 36 MiB with a durable
                    # fsync per 256 KiB chunk, not a hosted-runner speed contract.
                    # Keep startup/control deadlines and exact completion/hash
                    # assertions; give only the disk-heavy phase more headroom.
                    self.wait_until(complete, second, output,
                        "Recovered transfer did not resume to verified completion",
                        timeout=45, diagnostics=completion_diagnostics)
                    final = root / "archive" / "archive" / (selected[0] + ".logdata")
                    with closing(sqlite3.connect(catalog, timeout=2)) as db:
                        row = db.execute("SELECT size,sha256 FROM files WHERE id=?", (selected[0],)).fetchone()
                        count = db.execute("SELECT COUNT(*) FROM files WHERE id=?", (selected[0],)).fetchone()[0]
                    self.assertEqual(count, 1)
                    self.assertEqual(final.stat().st_size, row[0])
                    self.assertEqual(hashlib.sha256(final.read_bytes()).hexdigest(), row[1])
                    self.assertTrue(launcher.stop_profile(profile))
                    self.assertEqual(second.wait(timeout=15), 0)
                    self.assertFalse(runtime.exists())
                finally:
                    if any(child.poll() is None for child in children) and runtime.exists():
                        try:
                            launcher.stop_profile(profile)
                        except launcher.LaunchError:
                            pass
                    for child in children:
                        if child.poll() is None:
                            try:
                                child.wait(timeout=15)
                            except subprocess.TimeoutExpired:
                                child.terminate()
                                child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
