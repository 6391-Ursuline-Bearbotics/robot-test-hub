"""Native offline wrapper checks; Python is intercepted, no exercise/hardware runs."""
import json
import os
from pathlib import Path
import subprocess
import unittest

from tests import test_windows_setup


@unittest.skipUnless(os.name == "nt" and test_windows_setup.POWERSHELL, "Requires native Windows PowerShell")
class WindowsSelftestTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_windows_setup.WindowsSetupTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setup_environment()
        self.fixture.record.unlink()
        self.script = self.fixture.checkout / "scripts/windows/SetupHub.ps1"
        self.wrapper = self.fixture.checkout / "scripts/windows/TestHub.cmd"
        hook = self.fixture.root / "startup/sitecustomize.py"
        text = hook.read_text(encoding="utf-8")
        text = text.replace("('venv', 'pip', 'robot_test_hub.launcher')", "('venv', 'pip', 'robot_test_hub.launcher', 'robot_test_hub.selftest')")
        text = text.replace("        os._exit(int(os.environ.get('HUB_TEST_EXIT', '0')) if module != 'venv' else 0)\n",
                            "        if module == 'robot_test_hub.selftest':\n"
                            "            print(json.dumps({'schema_version': 1, 'success': os.environ.get('HUB_TEST_EXIT', '0') == '0', 'source_type': 'SYNTHETIC', 'network_contacted': False, 'active_archive_opened': False}), flush=True)\n"
                            "        os._exit(int(os.environ.get('HUB_TEST_EXIT', '0')) if module != 'venv' else 0)\n")
        hook.write_bytes(text.encode("utf-8"))
        self.env = dict(self.fixture.env)
        self.env.pop("LOCALAPPDATA", None)
        self.env.update(JAVA_HOME=r"Z:\NO_JAVA_FOR_OFFLINE_TEST", ROBOT_TEST_HUB_PROFILE=r"Z:\NO_PROFILE_FOR_OFFLINE_TEST")

    def call(self, *options, action="Test", wrapper=False, **env):
        environment = dict(self.env, **env)
        if wrapper:
            # Keep cmd.exe's native quoting; subprocess.list2cmdline targets a
            # different command-line parser and corrupts quoted &/$ source paths.
            command = '""' + str(self.wrapper) + '"' + (" " + " ".join(options) if options else "") + '"'
            args = '"' + os.environ.get("COMSPEC", "cmd.exe") + '" /d /s /c ' + command
        else:
            args = [test_windows_setup.POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script), "-Action", action, *options]
        return subprocess.run(args, cwd=self.fixture.root, env=environment, capture_output=True,
                              encoding="utf-8", timeout=40)

    def calls(self):
        return self.fixture.calls() if self.fixture.record.exists() else []

    def test_test_action_anchors_full_source_without_profile_localappdata_or_path_discovery(self):
        environment_before = dict(os.environ)
        result = self.call(PATH="")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]["args"], ["-m", "robot_test_hub.selftest"])
        self.assertEqual(Path(self.calls()[0]["cwd"]), self.fixture.checkout)
        self.assertTrue(self.calls()[0]["bridge"])
        self.assertEqual(json.loads(result.stdout)["source_type"], "SYNTHETIC")
        self.assertEqual(os.environ, environment_before, "Child execution must not mutate the calling environment")

    def test_cmd_forwards_only_explicit_json_and_analytics_flags(self):
        cases = [((), []), (("-Json",), ["--json"]), (("-Analytics",), ["--analytics"]),
                 (("-Json", "-Analytics"), ["--json", "--analytics"])]
        for options, expected in cases:
            with self.subTest(options=options):
                result = self.call(*options, wrapper=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.calls()[-1]["args"], ["-m", "robot_test_hub.selftest", *expected])
                self.assertEqual(Path(self.calls()[-1]["cwd"]), self.fixture.checkout)
                self.assertFalse((self.fixture.root / "SHOULD_NOT_EXIST").exists())
        self.assertTrue(all(call["args"][1] == "robot_test_hub.selftest" for call in self.calls()))

    def test_failure_preserves_module_json_and_exact_exit_code_without_fallback(self):
        result = self.call("-Json", "-Analytics", wrapper=True, HUB_TEST_EXIT="17")
        self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["success"])
        self.assertFalse(report["network_contacted"])
        self.assertFalse(report["active_archive_opened"])
        self.assertIn("exit 17", " ".join(result.stderr.split()))
        self.assertEqual([call["args"] for call in self.calls()], [["-m", "robot_test_hub.selftest", "--json", "--analytics"]])
        self.assertNotIn("NO_PROFILE_FOR_OFFLINE_TEST", result.stdout + result.stderr)
        self.assertNotIn("NO_JAVA_FOR_OFFLINE_TEST", result.stdout + result.stderr)

    def test_profile_and_interpreter_inputs_are_rejected_without_reading_or_echoing_them(self):
        private = self.fixture.root / "PRIVATE_SECRET_$(touch SHOULD_NOT_EXIST).json"
        private.write_bytes(b"do not inspect private profile bytes\xff")
        for option in ("-Profile", "-PythonPath"):
            with self.subTest(option=option):
                result = self.call(option, str(private))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("accepts no profile or interpreter override", " ".join(result.stderr.split()))
                self.assertNotIn("PRIVATE_SECRET", result.stdout + result.stderr)
                self.assertEqual(private.read_bytes(), b"do not inspect private profile bytes\xff")
                self.assertEqual(self.calls(), [])

    def test_test_flags_rejected_for_other_actions_before_any_profile_requirement(self):
        for action in ("Setup", "Check", "Run", "Open", "Stop"):
            for option in ("-Json", "-Analytics"):
                with self.subTest(action=action, option=option):
                    result = self.call(option, action=action)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("supported only by TestHub.cmd", " ".join(result.stderr.split()))
                    self.assertEqual(self.calls(), [])

    def test_missing_environment_requires_setup_and_never_creates_or_discovers_one(self):
        python = self.fixture.checkout / ".venv/Scripts/python.exe"
        python.rename(python.with_suffix(".exe-preserved"))
        result = self.call(PATH="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Run SetupHub.cmd first", " ".join(result.stderr.split()))
        self.assertFalse(python.exists())
        self.assertEqual(self.calls(), [])

    def test_existing_default_profile_is_untouched_and_complete_source_is_required(self):
        local = self.fixture.root / "local private config"
        private = local / "RobotTestHub/config/launch.json"
        private.parent.mkdir(parents=True)
        private.write_bytes(b"PRIVATE_SAVED_PROFILE_NOT_JSON\xff")
        result = self.call("-Json", LOCALAPPDATA=str(local))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(private.read_bytes(), b"PRIVATE_SAVED_PROFILE_NOT_JSON\xff")
        self.assertNotIn("PRIVATE_SAVED_PROFILE", result.stdout + result.stderr)
        self.assertEqual(self.calls()[-1]["args"], ["-m", "robot_test_hub.selftest", "--json"])
        self.fixture.record.unlink()
        (self.fixture.checkout / "pyproject.toml").unlink()
        result = self.call()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("complete source release", " ".join(result.stderr.split()))
        self.assertEqual(self.calls(), [])
