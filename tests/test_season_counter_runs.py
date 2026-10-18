"""Season counters across runs in one log, and runs without clock time on the home page."""
import contextlib
import io
import json
from pathlib import Path
from contextlib import closing
import tempfile
import unittest

from robot_test_hub import import_folder, practice, season
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE_AKIT26
from akit26_fixture import Writer

COUNTER_SEASON = {'schema_version': 1, 'season': 2099, 'profiles': [PROFILE_AKIT26],
                  'actions': {'Shoot': {'signal': '/RealOutputs/PracticeEvents/ShootCount', 'detect': 'counter'}}}


def two_runs(epoch_valid=True):
    w, t, ds = Writer(), 1_000_000, {}
    count = 0

    def cycle(enabled, auto, shoot=False):
        nonlocal t, count
        w.value('/Timestamp', 'int64', t, t)
        for name, state in (('Enabled', enabled), ('Autonomous', auto), ('Test', False)):
            if ds.get(name) != state:
                w.value('/DriverStation/' + name, 'boolean', t, state)
                ds[name] = state
        w.value('/SystemStats/EpochTimeMicros', 'int64', t, (1_789_000_000_000_000 if epoch_valid else 0) + t)
        if shoot:
            count += 1
            w.value('/RealOutputs/PracticeEvents/ShootCount', 'int64', t, count)
        t += 20_000
    for i in range(10): cycle(False, False)
    for i in range(50): cycle(True, True, shoot=i in (10, 20))
    for i in range(10): cycle(False, False)
    for i in range(50): cycle(True, False, shoot=i == 30)
    for i in range(10): cycle(False, False)
    return bytes(w.data)


class SeasonCounterRunTests(unittest.TestCase):
    def imported(self, data):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        (base / 'usb').mkdir()
        (base / 'usb' / 'log.wpilog').write_bytes(data)
        with contextlib.redirect_stdout(io.StringIO()):
            import_folder.main([str(base / 'usb'), '--data-dir', str(base / 'data'), '--profile', PROFILE_AKIT26, '--synthetic', '--json'])
        return base

    def test_counter_rise_in_an_earlier_run_is_not_recounted(self):
        base = self.imported(two_runs())
        (base / 'season.json').write_text(json.dumps(COUNTER_SEASON))
        definition = season.load(base / 'season.json')
        data = base / 'data'
        with closing(open_catalog(data)) as db:
            document = RunCatalog(db).current()
            while season.step(data, db, document, seasons=[definition]):
                pass
            counts = [season.result_for(db, run['run_id'], definition['version'])['counts']['Shoot'] for run in document['runs']]
        self.assertEqual(counts, [2, 1])

    def test_runs_without_clock_time_get_their_own_day(self):
        data = self.imported(two_runs(epoch_valid=False)) / 'data'
        result = practice.day(data)
        self.assertEqual((result['date'], result['days'], len(result['cards'])), ('undated', ['undated'], 2))
        self.assertIsNone(result['cards'][0]['local_start'])
        self.assertEqual(result['cards'][0]['modes'], ['Auto'])
        self.assertEqual(practice.card_for(data, result['cards'][1]['run_id'])['run_id'], result['cards'][1]['run_id'])


if __name__ == '__main__':
    unittest.main()
