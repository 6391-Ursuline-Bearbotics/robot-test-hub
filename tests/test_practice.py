import contextlib
import io
import math
from pathlib import Path
import shutil
import tempfile
import unittest
from datetime import timezone

from robot_test_hub import import_folder, practice

FIXTURES = Path(__file__).parent / "fixtures/synthetic"


def steering(position, settle):
    return {'name': 'recorded_settling_max_seconds', 'value': settle, 'unit': 'seconds', 'module_position': position}


class SiblingTests(unittest.TestCase):
    def test_flags_single_slow_module_with_plain_sentence(self):
        metrics = [steering('front-left', .42), steering('front-right', .12),
                   steering('back-left', .15), steering('back-right', .13)]
        [finding] = practice.sibling_findings(metrics)
        self.assertEqual(finding['module_position'], 'front-left')
        self.assertEqual(finding['sentence'], 'Front-left steering took 0.42s to settle; the other 3 were 0.12s–0.15s.')
        self.assertIn('not a health result', finding['basis'])

    def test_small_absolute_differences_and_missing_values_are_not_flagged(self):
        quick = [steering('front-left', .04), steering('front-right', .01),
                 steering('back-left', .01), steering('back-right', .01)]
        self.assertEqual(practice.sibling_findings(quick), [])
        missing = [steering('front-left', .9), steering('front-right', None), steering('back-left', .1)]
        self.assertEqual(practice.sibling_findings(missing), [])
        nonfinite = [steering('front-left', math.nan), steering('front-right', .1),
                     steering('back-left', .1), steering('back-right', .1)]
        self.assertEqual(practice.sibling_findings(nonfinite), [])

    def test_steering_error_shown_in_degrees(self):
        metrics = [{'name': 'steering_rmse_rad', 'value': math.radians(v), 'module_position': p}
                   for p, v in (('front-left', 12), ('front-right', 2), ('back-left', 3), ('back-right', 2.5))]
        [finding] = practice.sibling_findings(metrics)
        self.assertIn('off target by 12° on average', finding['sentence'])


class StatusTests(unittest.TestCase):
    def test_chip_rules_never_turn_missing_data_into_ok(self):
        self.assertEqual(practice.card_status(None, [])[0], 'unchecked')
        nothing = {'checks': [{'outcome': 'insufficient_data', 'findings': []}]}
        self.assertEqual(practice.card_status(nothing, [])[0], 'unchecked')
        clean = {'checks': [{'outcome': 'evaluated_no_finding', 'findings': []},
                            {'outcome': 'insufficient_data', 'findings': []}]}
        chip, line, _ = practice.card_status(clean, [])
        self.assertEqual(chip, 'ok')
        self.assertIn("(1 couldn't)", line)
        flagged = {'checks': [{'outcome': 'finding', 'findings': [{'kind': 'recording_gap', 'count': 2},
                                                                  {'kind': 'disconnected_observations', 'count': 1}]}]}
        chip, line, sentences = practice.card_status(flagged, [])
        self.assertEqual(chip, 'look')
        self.assertEqual(line, 'The log has 2 gap(s) where nothing was recorded. (+1 more)')
        self.assertEqual(len(sentences), 2)

    def test_every_known_finding_kind_gets_a_sentence_without_jargon(self):
        kinds = ['recording_gap', 'invalid_observations', 'stale_observations', 'disconnected_observations',
                 'invalid_cycle_timestamps', 'sustained_drive_tracking_error', 'steering_tracking_error',
                 'tracking_regression_against_approved_cohort', 'recorded_phoenix_signal_status_not_ok',
                 'recorded_phoenix_group_refresh_status_not_ok', 'recorded_system_fault_true',
                 'recorded_system_fault_counter_increased', 'recorded_steering_settling_limit',
                 'recorded_steering_error_crossings', 'recorded_steering_angle_jump', 'recorded_steering_sustained_error']
        banned = ('manifest', 'digest', 'lease', 'schema', 'projection', 'snapshot', 'bounded', 'qualified', 'cohort')
        for kind in kinds:
            text = practice.finding_sentence({'kind': kind, 'count': 3, 'module_position': 'back-right', 'signal': 'Brownout'})
            self.assertTrue(not text[0].islower() and text.endswith('.'), (kind, text))
            self.assertFalse(any(word in text.lower() for word in banned), (kind, text))


class DayTests(unittest.TestCase):
    def test_groups_imported_runs_by_local_day_with_cards(self):
        with tempfile.TemporaryDirectory() as folder:
            usb, data = Path(folder) / 'usb', Path(folder) / 'data'
            usb.mkdir()
            shutil.copy(FIXTURES / 'alpha7-main.wpilog', usb / 'a.wpilog')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(import_folder.main([str(usb), '--data-dir', str(data), '--synthetic', '--json']), 0)
            result = practice.day(data, tz=timezone.utc)
            self.assertEqual(result['date'], '2026-10-03')
            self.assertEqual(result['days'], ['2026-10-03'])
            self.assertTrue(result['cards'])
            card = result['cards'][0]
            self.assertIn(card['status'], ('ok', 'look', 'unchecked'))
            self.assertEqual(card['source_types'], ['SYNTHETIC'])
            self.assertEqual(card['modes'], ['Auto', 'Teleop'])
            self.assertEqual(practice.day(data, '2026-10-04', tz=timezone.utc)['cards'], [])

    def test_empty_data_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(practice.day(Path(folder))['cards'], [])


if __name__ == '__main__':
    unittest.main()
