"""AdvantageKit 26 historical profile, run metrics and student findings on invented logs."""
from contextlib import closing
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from robot_test_hub import import_folder, practice, run_metrics
from robot_test_hub.importer import Importer
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE, PROFILE_AKIT26, FormatError, extract
from akit26_fixture import match_log


class Akit26Tests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.log = self.base / 'usb' / 'akit_invented.wpilog'
        self.log.parent.mkdir()
        self.log.write_bytes(match_log())

    def imported(self):
        data = self.base / 'data'
        with contextlib.redirect_stdout(io.StringIO()):
            code = import_folder.main([str(self.log.parent), '--data-dir', str(data), '--profile', PROFILE_AKIT26,
                                       '--synthetic', '--json'])
        self.assertEqual(code, 0)
        return data

    def test_extract_cycles_derived_mode_epoch_and_truncated_tail(self):
        rows = list(extract(self.log, PROFILE_AKIT26))
        cycles = [r for r in rows if r['kind'] == 'cycle']
        self.assertEqual(len(cycles), 180)
        self.assertEqual(cycles[0]['timestamp_ns'], '1000000000')
        auto = cycles[10]['aliases']
        self.assertEqual(auto['robot_mode']['value'], 'AUTONOMOUS')
        self.assertTrue(auto['robot_mode']['derived'])
        self.assertTrue(auto['epoch_valid']['value'])
        self.assertEqual(auto['epoch_us']['uncertainty_ns'], 200_000_000)
        self.assertEqual(cycles[80]['aliases']['robot_mode']['value'], 'TELEOPERATED')
        timestamp = next(r for r in rows if r['kind'] == 'observation' and r['field'] == '/Timestamp')
        self.assertEqual((timestamp['unit'], timestamp['unit_source']), ('microseconds', 'explicit_profile_mapping'))
        [tail] = [r for r in rows if r['kind'] == 'quality' and 'truncated_tail_bytes' in r]
        self.assertEqual(tail['truncated_tail_bytes'], 11)

    def test_alpha7_profile_still_rejects_truncated_framing(self):
        from robot_test_hub.wpilog import records
        with self.log.open('rb') as stream, self.assertRaises(FormatError):
            list(records(stream))
        with self.log.open('rb') as stream:
            tail = {}
            list(records(stream, truncated_tail=tail))
        self.assertEqual(tail['bytes'], 11)

    def test_read_ahead_chunk_boundaries_do_not_change_records(self):
        from unittest.mock import patch
        from robot_test_hub import wpilog
        data = self.log.read_bytes()

        class Trickle(io.BytesIO):  # short record reads like a pipe, not only at EOF
            def read(self, size=-1):
                return super().read(min(size, 3) if self.tell() >= 24 and size and size > 0 else size)

        def parse(stream):
            tail = {}
            return [tuple(r) for r in wpilog.records(stream, truncated_tail=tail)], tail
        expected = parse(io.BytesIO(data))
        for chunk in (1, 5, 17, 64):
            with self.subTest(chunk=chunk), patch.object(wpilog, 'READ_CHUNK_BYTES', chunk):
                self.assertEqual(parse(io.BytesIO(data)), expected)
                self.assertEqual(parse(Trickle(data)), expected)
        self.assertEqual(expected[1]['offset'] + expected[1]['bytes'], len(data))

    def test_runs_metrics_and_student_findings(self):
        data = self.imported()
        with closing(open_catalog(data)) as db:
            job = Importer(data, db).list_jobs()[0]
            self.assertEqual(job['mapping_revision'], 'akit26-aliases-1')
            runs = RunCatalog(db).current()['runs']
            self.assertEqual([[p['mode'] for p in r['phases']] for r in runs], [['AUTONOMOUS'], ['TELEOPERATED']])
            self.assertEqual(runs[0]['wall_clock_quality'], 'anchored')
            metrics = {(m['component'], m['metric']): m['value'] for m in run_metrics.for_run(db, runs[1]['run_id'])}
        self.assertAlmostEqual(metrics[('robot', 'battery_min_v')], 6.5)
        self.assertAlmostEqual(metrics[('robot', 'battery_time_below_9v_s')], 2.0, places=1)
        self.assertEqual(metrics[('robot', 'loop_overruns')], 100)
        self.assertAlmostEqual(metrics[('Drive/Module3', 'drive_speed_error_mps')], 3.0)
        self.assertAlmostEqual(metrics[('Drive/Module3/Drive', 'current_mean_a')], 0.5)
        cards = practice.day(data)['cards']
        self.assertEqual([c['status'] for c in cards], ['look', 'look'])
        teleop = cards[1]['findings']
        self.assertIn('Back-right drive motor: average current 0.50 A; the other 3: 19.8 A.', teleop)
        self.assertTrue(any(s.startswith('Back-right module: wheel speed off target by 3.0 m/s') for s in teleop))
        self.assertIn('Battery dropped to 6.5 V (below 7 V the robot is close to browning out).', teleop)
        self.assertFalse(any(s.startswith(('Front-left', 'Front-right', 'Back-left')) for s in teleop))
        self.assertEqual(cards[1]['battery_min_volts'], 6.5)


class TrendTests(unittest.TestCase):
    def row(self, value, metric='current_mean_a', component='Drive/Module3/Drive', unit='A'):
        return {'component': component, 'metric': metric, 'value': value, 'unit': unit}

    def test_flags_only_far_outside_recent_runs_in_the_worse_direction(self):
        history = {('Drive/Module3/Drive', 'current_mean_a'): [18, 19, 20, 21, 22, 19.5, 20.5]}
        [finding] = practice.trend_findings([self.row(40.0)], history)
        self.assertEqual(finding['sentence'], 'Unusual compared with recent runs: Back-right drive motor average current 40 A (usually 19–21 A).')
        self.assertEqual(practice.trend_findings([self.row(20.7)], history), [])
        self.assertEqual(practice.trend_findings([self.row(4.0)], history), [])
        battery = {('robot', 'battery_min_v'): [11.0, 11.1, 10.9, 11.0, 11.2]}
        [low] = practice.trend_findings([self.row(9.0, 'battery_min_v', 'robot', 'V')], battery)
        self.assertEqual(low['sentence'], 'Unusual compared with recent runs: battery low point 9.0 V (usually 11.0–11.1 V).')
        self.assertEqual(practice.trend_findings([self.row(12.5, 'battery_min_v', 'robot', 'V')], battery), [])

    def test_needs_enough_history_and_an_absolute_floor(self):
        self.assertEqual(practice.trend_findings([self.row(4.0)], {('Drive/Module3/Drive', 'current_mean_a'): [20, 20, 20]}), [])
        tight = {('Drive/Module3/Drive', 'current_mean_a'): [20.0, 20.0, 20.0, 20.0, 20.0, 20.0]}
        self.assertEqual(practice.trend_findings([self.row(21.0)], tight), [])

    def test_pair_reported_once_and_sibling_labels(self):
        rows = [self.row(40, 'temp_rise_c', 'Shooter/Left', '°C'), self.row(10, 'temp_rise_c', 'Shooter/Right', '°C')]
        found = [f['sentence'] for f in practice.measured_findings(rows)]
        self.assertEqual(found, ['Shooter left: heated up by 40 °C; the other: 10.0 °C.'])


if __name__ == '__main__':
    unittest.main()
