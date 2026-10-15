"""Independent native Windows setup boundary checks; intercepted Python only."""
import os
from pathlib import Path
import subprocess
import unittest

from tests import test_windows_setup as setup_tests


@unittest.skipUnless(os.name == 'nt' and setup_tests.POWERSHELL, 'Requires native Windows PowerShell')
class WindowsSetupIndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.fixture = setup_tests.WindowsSetupTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_missing_explicit_interpreter_does_not_create_environment(self):
        fixture = self.fixture
        args = [setup_tests.POWERSHELL, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                str(fixture.checkout / 'scripts/windows/SetupHub.ps1'), '-Action', 'Setup',
                '-PythonPath', str(fixture.root / 'missing python.exe'), '-Profile', str(fixture.profile)]
        result = subprocess.run(args, env=fixture.env, cwd=fixture.root, capture_output=True, encoding='utf-8', timeout=40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('installed Python executable', result.stderr)
        self.assertFalse((fixture.checkout / '.venv').exists())
        self.assertFalse(fixture.record.exists())

    def test_missing_full_release_rejected_before_environment_or_profile_write(self):
        fixture = self.fixture
        (fixture.checkout / 'pyproject.toml').unlink()
        result = fixture.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('complete source release', result.stderr)
        self.assertFalse(fixture.record.exists())
        self.assertFalse(fixture.profile.exists())

    def test_existing_incompatible_environment_preserved_and_not_installed(self):
        fixture = self.fixture
        fixture.setup_environment()
        before = fixture.record.read_bytes()
        marker = fixture.checkout / '.venv' / 'preserve-local-file.txt'
        marker.write_text('keep existing environment')
        result = fixture.run_script(HUB_TEST_OLD_PYTHON='1')
        self.assertEqual(result.returncode, 3)
        self.assertNotIn('Setup complete', result.stdout)
        self.assertEqual(fixture.record.read_bytes(), before)
        self.assertEqual(marker.read_text(), 'keep existing environment')

    def test_failed_reinstall_preserves_private_configuration_and_existing_environment(self):
        fixture = self.fixture
        fixture.setup_environment()
        fixture.profile.parent.mkdir()
        fixture.profile.write_bytes(b'private configuration preserved')
        marker = fixture.checkout / '.venv' / 'preserve-local-file.txt'
        marker.write_text('existing environment')
        result = fixture.run_script(HUB_TEST_EXIT='17')
        self.assertEqual(result.returncode, 17)
        self.assertEqual(fixture.profile.read_bytes(), b'private configuration preserved')
        self.assertEqual(marker.read_text(), 'existing environment')
        self.assertNotIn('Setup complete', result.stdout)
        self.assertEqual(sum(call['args'][1] == 'venv' for call in fixture.calls()), 1)

    def test_all_daily_cmd_wrappers_anchor_source_and_preserve_failure(self):
        fixture = self.fixture
        fixture.setup_environment()
        for name, action in [('StartHub', 'run'), ('CheckHub', 'check'), ('StopHub', 'stop'), ('OpenHub', 'open')]:
            with self.subTest(name=name):
                wrapper = fixture.checkout / 'scripts' / 'windows' / (name + '.cmd')
                command = '\"\"' + str(wrapper) + '\" -Profile \"' + str(fixture.profile) + '\"\"'
                result = subprocess.run('\"' + os.environ.get('COMSPEC', 'cmd.exe') + '\" /d /s /c ' + command,
                    env=dict(fixture.env, HUB_TEST_EXIT='19'), cwd=fixture.root,
                    capture_output=True, encoding='utf-8', timeout=40)
                self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
                call = fixture.calls()[-1]
                self.assertEqual(call['args'], ['-m', 'robot_test_hub.launcher', action, '--profile', str(fixture.profile)])
                self.assertEqual(Path(call['cwd']), fixture.checkout)
                self.assertTrue(call['bridge'])
                self.assertFalse((fixture.root / 'SHOULD_NOT_EXIST').exists())
