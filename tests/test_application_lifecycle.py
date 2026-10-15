"""Real foreground launch/stop qualification with synthetic data only."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from robot_test_hub import launcher


ROOT = Path(__file__).resolve().parents[1]


class ApplicationLifecycleTest(unittest.TestCase):
    def test_launcher_subprocess_duplicate_start_and_graceful_stop(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / "private" / "hub.json"
            config.parent.mkdir()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config.write_text(json.dumps(dict(schema_version=1, port=port,
                data_dir="../archive", tick_interval=.05)), encoding="utf-8")
            profile = root / "launch.json"
            launcher.configure_profile(profile, dict(schema_version=1, mode="demo",
                hub_config="private/hub.json"))
            command = [sys.executable, "-X", "utf8", "-m",
                       "robot_test_hub.launcher", "run", "--profile", str(profile)]
            env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(ROOT))
            runtime = launcher.runtime_path(profile)
            with (root / "console.txt").open("w+b") as output:
                child = subprocess.Popen(command, cwd=root, env=env,
                    stdout=output, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 15
                    while not runtime.exists():
                        if child.poll() is not None or time.monotonic() >= deadline:
                            output.seek(0)
                            self.fail("Synthetic launcher startup failed: " +
                                      output.read().decode("utf-8", errors="replace"))
                        time.sleep(.05)
                    original = runtime.read_bytes()
                    record = launcher._runtime(profile)
                    # Windows virtualenv launchers may spawn the actual Python PID.
                    self.assertGreater(record["pid"], 0)
                    identity = launcher._control(record, "GET")
                    self.assertEqual(identity["instance_id"], record["instance_id"])
                    self.assertEqual(identity["state"], "running")
                    duplicate = subprocess.run(command, cwd=root, env=env,
                        capture_output=True, timeout=10)
                    self.assertEqual(duplicate.returncode, 2)
                    self.assertEqual(runtime.read_bytes(), original)
                    self.assertTrue(launcher.stop_profile(profile))
                    self.assertEqual(child.wait(timeout=15), 0)
                    self.assertFalse(runtime.exists())
                    self.assertTrue((root / "archive" / "catalog.sqlite3").is_file())
                    self.assertFalse((root / "private" / "archive").exists())
                    self.assertFalse(launcher.stop_profile(profile))
                    from robot_test_hub.storage import DataRootOwner
                    owner = DataRootOwner(root / "archive")
                    owner.close()
                finally:
                    if child.poll() is None and runtime.exists():
                        try:
                            launcher.stop_profile(profile)
                            child.wait(timeout=15)
                        except (launcher.LaunchError, subprocess.TimeoutExpired):
                            pass
                    if child.poll() is None:
                        # Only this test-created synthetic child may be killed
                        # on a failed assertion; production Stop never does so.
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
