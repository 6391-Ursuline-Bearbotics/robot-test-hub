"""Real Windows 8.3 ancestor regression for source setup and daily wrappers."""
import ctypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_windows_setup, test_windows_setup_review


@unittest.skipUnless(os.name == "nt" and test_windows_setup.POWERSHELL,
                     "Requires native Windows PowerShell")
class WindowsSetupAliasReviewTests(unittest.TestCase):
    def test_setup_actions_default_profile_and_all_cmds_under_short_temp_ancestor(self):
        with tempfile.TemporaryDirectory(prefix="hub Windows setup long ancestor ") as directory:
            output = ctypes.create_unicode_buffer(32768)
            get_short_name = ctypes.windll.kernel32.GetShortPathNameW
            get_short_name.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
            get_short_name.restype = ctypes.c_uint32
            length = get_short_name(directory, output, len(output))
            if not length or length >= len(output):
                raise ctypes.WinError()
            alias = Path(output.value)
            if alias.absolute() == alias.resolve():
                self.skipTest("Temporary filesystem does not supply distinct 8.3 aliases")
            self.assertTrue(alias.is_dir())
            cases = (
                (test_windows_setup.WindowsSetupTest,
                 "test_setup_creates_per_install_venv_and_installs_only_requested_extras"),
                (test_windows_setup.WindowsSetupTest,
                 "test_actions_forward_profile_as_one_literal_argument"),
                (test_windows_setup.WindowsSetupTest,
                 "test_default_profile_uses_localappdata_and_never_creates_it"),
                (test_windows_setup_review.WindowsSetupIndependentReviewTest,
                 "test_all_daily_cmd_wrappers_anchor_source_and_preserve_failure"),
            )
            # These retain the complete author/reviewer behavior assertions and
            # execute actual PowerShell/cmd children, with Python interception
            # still preventing pip installation and hardware/launcher execution.
            with patch.object(tempfile, "tempdir", str(alias)):
                for case_type, name in cases:
                    with self.subTest(case=name):
                        result = unittest.TestResult()
                        case_type(name).run(result)
                        self.assertEqual(result.testsRun, 1)
                        self.assertEqual(result.skipped, [])
                        self.assertEqual(result.errors, [])
                        self.assertEqual(result.failures, [])
