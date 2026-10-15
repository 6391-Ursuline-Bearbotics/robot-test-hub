"""Exercise the real Windows scripts with Python startup interception, never pip/network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "windows"
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Requires native Windows PowerShell")
class WindowsSetupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hub windows & $ ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "source with spaces & $"
        shutil.copytree(SCRIPTS, self.checkout / "scripts" / "windows")
        (self.checkout / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        bridge = self.checkout / "tools" / "status_bridge"
        bridge.mkdir(parents=True)
        (bridge / "source-fixture.txt").write_text("full source bridge", encoding="utf-8")
        self.profile = self.root / "private & $(touch SHOULD_NOT_EXIST)" / "launch.json"
        self.record = self.root / "calls.jsonl"
        hook = self.root / "startup"
        hook.mkdir()
        (hook / "sitecustomize.py").write_text(
            "import json, os, pathlib, sys\n"
            "args = sys.orig_argv[1:]\n"
            "if os.environ.get('HUB_TEST_OLD_PYTHON'): sys.version_info = (3, 9)\n"
            "if '-m' in args:\n"
            "    module = args[args.index('-m') + 1]\n"
            "    if module in ('venv', 'pip', 'robot_test_hub.launcher'):\n"
            "        with open(os.environ['HUB_TEST_RECORD'], 'a', encoding='utf-8') as f:\n"
            "            f.write(json.dumps({'args': args, 'cwd': os.getcwd(), 'bridge': pathlib.Path('tools/status_bridge/source-fixture.txt').is_file()}) + '\\n')\n"
            "        if module == 'venv':\n"
            "            import venv\n"
            "            venv.EnvBuilder(with_pip=False).create(args[-1])\n"
            "        os._exit(int(os.environ.get('HUB_TEST_EXIT', '0')) if module != 'venv' else 0)\n",
            encoding="utf-8",
        )
        self.env = dict(os.environ, PYTHONPATH=str(hook), PYTHONUTF8="1",
                        HUB_TEST_RECORD=str(self.record), LOCALAPPDATA=str(self.root / "local app data"))

    def run_script(self, action="Setup", profile=True, **env):
        args = [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(self.checkout / "scripts/windows/SetupHub.ps1"), "-Action", action]
        if action == "Setup":
            args += ["-PythonPath", sys.executable]
        if profile:
            args += ["-Profile", str(self.profile)]
        return subprocess.run(args, cwd=self.root, env=dict(self.env, **env),
                              capture_output=True, encoding="utf-8", timeout=40)

    def calls(self):
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def setup_environment(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_setup_creates_per_install_venv_and_installs_only_requested_extras(self):
        self.setup_environment()
        calls = self.calls()
        self.assertEqual(calls[0]["args"], ["-m", "venv", str(self.checkout / ".venv")])
        self.assertEqual(calls[1]["args"], ["-m", "pip", "install", ".[sftp,analytics]"])
        self.assertEqual(Path(calls[1]["cwd"]), self.checkout)
        self.assertFalse(self.profile.exists())
        self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())

    def test_setup_reuses_venv_and_preserves_private_profile_bytes(self):
        self.setup_environment()
        self.profile.parent.mkdir()
        original = b'{"private": "do not overwrite"}\n'
        self.profile.write_bytes(original)
        self.setup_environment()
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertEqual(sum(call["args"][1] == "venv" for call in self.calls()), 1)

    def test_actions_forward_profile_as_one_literal_argument(self):
        self.setup_environment()
        for action in ("Run", "Check", "Stop", "Open"):
            with self.subTest(action=action):
                result = self.run_script(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls()[-1]["args"],
                                 ["-m", "robot_test_hub.launcher", action.lower(), "--profile", str(self.profile)])
                self.assertEqual(Path(self.calls()[-1]["cwd"]), self.checkout)
                self.assertTrue(self.calls()[-1]["bridge"])

    def test_default_profile_uses_localappdata_and_never_creates_it(self):
        self.setup_environment()
        result = self.run_script("Check", profile=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = Path(self.env["LOCALAPPDATA"]) / "RobotTestHub/config/launch.json"
        self.assertEqual(self.calls()[-1]["args"][-1], str(expected))
        self.assertFalse(expected.exists())

    def test_checkout_profile_rejected_before_any_python_process(self):
        self.profile = self.checkout / "private.json"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the source checkout", result.stderr)
        self.assertFalse(self.record.exists())

    def test_old_python_rejected_before_venv_or_install(self):
        result = self.run_script(HUB_TEST_OLD_PYTHON="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.record.exists())

    def test_missing_environment_and_install_failure_are_visible(self):
        result = self.run_script("Run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SetupHub.cmd first", result.stderr)
        result = self.run_script(HUB_TEST_EXIT="7")
        self.assertEqual(result.returncode, 7)
        self.assertIn("exit 7", result.stderr)
        self.assertNotIn("Setup complete", result.stdout)

    def test_cmd_wrapper_preserves_quoted_profile_and_exit_failure(self):
        self.setup_environment()
        wrapper = self.checkout / "scripts/windows/CheckHub.cmd"
        command = '""' + str(wrapper) + '" -Profile "' + str(self.profile) + '""'
        result = subprocess.run('"' + os.environ.get("COMSPEC", "cmd.exe") + '" /d /s /c ' + command,
                                env=dict(self.env, HUB_TEST_EXIT="9"), cwd=self.root,
                                capture_output=True, encoding="utf-8", timeout=40)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(self.calls()[-1]["args"][-1], str(self.profile), result.stdout + result.stderr)
        self.assertEqual(Path(self.calls()[-1]["cwd"]), self.checkout)
        self.assertTrue(self.calls()[-1]["bridge"])


if __name__ == "__main__":
    unittest.main()
