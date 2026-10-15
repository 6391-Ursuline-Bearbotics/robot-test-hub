"""Prior Windows CI regression: canonical private export fixture roots."""
import ctypes
import importlib.util
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# Existing columnar fixtures use discovery-style top-level helper imports.
# Make only this import consistent for targeted and discovery invocation,
# then restore the caller's import path immediately.
_fixture_path = str(Path(__file__).resolve().parent)
_fixture_path_added = _fixture_path not in sys.path
if _fixture_path_added:
    sys.path.insert(0, _fixture_path)
try:
    from tests import test_columnar
finally:
    if _fixture_path_added:
        sys.path.remove(_fixture_path)


@unittest.skipUnless(os.name == "nt", "Windows 8.3 path qualification")
@unittest.skipUnless(importlib.util.find_spec("duckdb"), "Install optional analytics extra")
class ExportFixtureWindowsAliasTests(unittest.TestCase):
    def test_summary_exports_under_actual_short_name_temp_ancestor(self):
        with tempfile.TemporaryDirectory(prefix="hub summary CI long ancestor ") as directory:
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
            names = (
                "test_summary_conflict_preserved_then_retry",
                "test_summary_failure_does_not_block_original_publication",
                "test_summary_sharing_optin_only_small_artifacts",
            )
            # The original fixture failed all three here before it canonicalized
            # its common root. Run their full behavior assertions, not mocks of
            # link detection or skipped exporters.
            with patch.object(tempfile, "tempdir", str(alias)):
                for name in names:
                    with self.subTest(case=name):
                        result = unittest.TestResult()
                        test_columnar.ColumnarTests(name).run(result)
                        self.assertEqual(result.testsRun, 1)
                        self.assertEqual(result.skipped, [])
                        self.assertEqual(result.errors, [])
                        self.assertEqual(result.failures, [])
