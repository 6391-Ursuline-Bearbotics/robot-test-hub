"""Get-the-data bundles: unchanged original, AdvantageScope layout and README."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from robot_test_hub import evidence, import_folder, practice
from robot_test_hub.wpilog import PROFILE_AKIT26
from akit26_fixture import match_log


class EvidenceTests(unittest.TestCase):
    def test_bundle_holds_original_layout_and_readme(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'usb').mkdir()
            original = match_log()
            (base / 'usb' / 'invented match.wpilog').write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()):
                import_folder.main([str(base / 'usb'), '--data-dir', str(base / 'data'), '--profile', PROFILE_AKIT26,
                                    '--synthetic', '--json'])
            card = practice.day(base / 'data')['cards'][1]
            self.assertTrue(any(s.startswith('Back-right') for s in card['findings']))
            self.assertEqual(practice.card_for(base / 'data', card['run_id'])['run_id'], card['run_id'])
            path, name = evidence.build(base / 'data', card['run_id'], card)
            try:
                self.assertTrue(name.startswith('robot-run-') and name.endswith('.zip'))
                with zipfile.ZipFile(path) as bundle:
                    self.assertEqual(sorted(bundle.namelist()), ['README.txt', 'advantagescope-layout.json', 'invented_match.wpilog'])
                    self.assertEqual(bundle.read('invented_match.wpilog'), original)
                    layout = json.loads(bundle.read('advantagescope-layout.json'))
                    readme = bundle.read('README.txt').decode()
            finally:
                path.unlink()
            tabs = layout['hubs'][0]['state']['tabs']['tabs']
            graphs = {t['title']: t for t in tabs if t['type'] == 1}
            module = graphs['Back-right module']['controller']
            self.assertEqual([s['logKey'] for s in module['leftSources']],
                             [f'/Drive/Module{i}/DriveCurrentAmps' for i in range(4)])
            self.assertTrue(all(s['logType'] == 'Number' for s in module['leftSources']))
            self.assertEqual(module['discreteSources'][0]['logKey'], '/DriverStation/Enabled')
            self.assertIn('Battery and power', graphs)
            self.assertIn('Look at log time 2.4 s to 4.4 s', readme)
            self.assertIn('cannot jump there for you', readme)

    def test_unknown_run(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'catalog.sqlite3').touch()
            with self.assertRaises(Exception):
                evidence.build(folder, 'nope', {})


if __name__ == '__main__':
    unittest.main()
