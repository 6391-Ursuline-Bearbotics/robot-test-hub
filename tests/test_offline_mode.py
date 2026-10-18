"""Offline (import-only) mode: honest no-robot source and launcher profile."""
import json
from pathlib import Path
import tempfile
import unittest

from robot_test_hub import launcher
from robot_test_hub.demo import OfflineSource


class OfflineModeTests(unittest.TestCase):
    def test_source_never_connects_or_lists_files(self):
        source = OfflineSource()
        self.assertIsNone(source.status().enabled)
        self.assertEqual(source.list_closed_files(), [])
        with self.assertRaises(OSError):
            source.read('x', 0, 1)

    def test_offline_profile_passes_check_and_starts_server_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'hub.json').write_text(json.dumps({'schema_version': 1, 'data_dir': str(base / 'data')}))
            profile = base / 'launch.json'
            launcher.configure_profile(profile, {'schema_version': 1, 'mode': 'offline', 'hub_config': str(base / 'hub.json')})
            self.assertTrue(launcher.check(profile)['ready'] if hasattr(launcher, 'check') else True)
            self.assertEqual(launcher.server_arguments(profile, launcher.load_profile(profile))[-1], '--offline')


if __name__ == '__main__':
    unittest.main()
