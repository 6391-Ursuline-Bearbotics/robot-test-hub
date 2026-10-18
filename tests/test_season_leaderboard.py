"""Season action detection, cycles, versioned results and leaderboards on invented data."""
import json
from pathlib import Path
import tempfile
import unittest

from robot_test_hub import leaderboard, season

S = 1_000_000_000
DEFINITION = {
    'schema_version': 1, 'season': 2099, 'profiles': ['invented'],
    'actions': {
        'Intake': {'signal': '/Intake/Goal', 'detect': 'enters', 'value': 'INTAKE'},
        'Shoot': {'signal': '/Indexer/Goal', 'detect': 'enters', 'value': 'FEED'},
        'Climb': {'signal': '/Climb/Done', 'detect': 'rising'},
        'Score': {'signal': '/Practice/Events/ScoreCount', 'detect': 'counter'},
        'Hot': {'signal': '/Shooter/Temp', 'detect': 'threshold', 'above': 60, 'reset_below': 50},
    },
    'timed_tasks': {'Cycle': {'from': 'Intake', 'to': 'Shoot', 'min_seconds': 2, 'max_seconds': 30}},
    'score_proxy': {'label': 'points', 'points': {'Shoot': 2, 'Climb': 10}},
}


def write(definition):
    folder = tempfile.TemporaryDirectory()
    path = Path(folder.name) / 'season.json'
    path.write_text(json.dumps(definition))
    return folder, path


class SeasonTests(unittest.TestCase):
    def detect(self, observations):
        folder, path = write(DEFINITION)
        self.addCleanup(folder.cleanup)
        definition = season.load(path)
        detector = season.Detector(definition)
        for t, field, value in observations:
            detector.observe(field, value, t * S)
        return definition, season.summarize(definition, sorted(detector.events))

    def test_each_detector_mode(self):
        _, result = self.detect([
            (0, '/Intake/Goal', 'IDLE'), (1, '/Intake/Goal', 'INTAKE'), (2, '/Intake/Goal', 'INTAKE'),
            (0, '/Climb/Done', False), (5, '/Climb/Done', True), (6, '/Climb/Done', True),
            (3, '/Practice/Events/ScoreCount', 2),
            (0, '/Shooter/Temp', 40.0), (4, '/Shooter/Temp', 61.0), (5, '/Shooter/Temp', 62.0),
            (6, '/Shooter/Temp', 45.0), (7, '/Shooter/Temp', 65.0)])
        self.assertEqual(result['counts'], {'Intake': 1, 'Shoot': 0, 'Climb': 1, 'Score': 2, 'Hot': 2})
        self.assertEqual(result['score'], 10)

    def test_cycles_use_first_intake_and_discard_too_short_or_long(self):
        _, result = self.detect([
            (0, '/Indexer/Goal', 'IDLE'), (0, '/Intake/Goal', 'IDLE'),
            (10, '/Intake/Goal', 'INTAKE'), (11, '/Intake/Goal', 'IDLE'), (12, '/Intake/Goal', 'INTAKE'),
            (16, '/Indexer/Goal', 'FEED'), (17, '/Indexer/Goal', 'IDLE'),
            (20, '/Intake/Goal', 'IDLE'), (20.5, '/Intake/Goal', 'INTAKE'), (21, '/Indexer/Goal', 'FEED'),
            (22, '/Indexer/Goal', 'IDLE'), (30, '/Intake/Goal', 'IDLE'), (31, '/Intake/Goal', 'INTAKE'),
            (80, '/Indexer/Goal', 'FEED')])
        cycle = result['tasks']['Cycle']
        self.assertEqual(cycle['durations_s'], [6.0])
        self.assertEqual(result['counts']['Shoot'], 3)
        self.assertEqual(result['score'], 6)

    def test_first_value_is_not_an_edge_and_version_follows_content(self):
        definition, result = self.detect([(0, '/Indexer/Goal', 'FEED')])
        self.assertEqual(result['counts']['Shoot'], 0)
        changed = dict(DEFINITION, score_proxy={'points': {'Shoot': 3}})
        folder, path = write(changed)
        self.addCleanup(folder.cleanup)
        self.assertNotEqual(season.load(path)['version'], definition['version'])

    def test_rejects_invalid_definitions(self):
        for bad in ({**DEFINITION, 'schema_version': 2},
                    {**DEFINITION, 'actions': {'X': {'signal': '/a', 'detect': 'guess'}}},
                    {**DEFINITION, 'timed_tasks': {'C': {'from': 'Intake', 'to': 'Nope', 'max_seconds': 5}}}):
            folder, path = write(bad)
            self.addCleanup(folder.cleanup)
            with self.assertRaises(season.SeasonError):
                season.load(path)

    def test_shipped_2026_season_file_is_valid(self):
        definition = season.for_profile('wpilib-2026_akit-26')
        self.assertEqual(definition['game'], 'REBUILT')
        self.assertTrue(definition['version'].startswith('2026-'))


def record(n, crew, score, cycles, average, counted=True):
    driver, operator = crew
    return {'match_id': f'match-{n}', 'label': f'Practice match {n}', 'start_utc_ns': str(n * 1000 * S),
            'crew': {'driver': {'id': driver, 'name': driver.title()}, 'operator': {'id': operator, 'name': operator.title()}},
            'scored': True, 'score': score, 'score_label': 'points', 'cycles': cycles, 'cycle_average_s': average,
            'cycle_best_s': average, 'counted': counted, 'run_ids': []}


class LeaderboardTests(unittest.TestCase):
    def test_boards_rank_crews_and_respect_not_counted_and_minimum_cycles(self):
        ab, cd = ('ava', 'ben'), ('cleo', 'dev')
        items = [record(1, ab, 10, 4, 9.0), record(2, cd, 30, 6, 7.0, counted=False), record(3, cd, 12, 2, 5.0),
                 record(4, ab, 14, 5, 8.0), record(5, ab, 13, 3, 8.5)]
        boards = leaderboard.boards(items, week_start_utc_ns=3500 * S)
        self.assertEqual([(e['crew'], e['best_score']) for e in boards['best_score']], [('Ava + Ben', 14), ('Cleo + Dev', 12)])
        self.assertEqual([e['crew'] for e in boards['fastest_average_cycle']], ['Ava + Ben'])
        self.assertEqual(boards['most_improved'][0]['crew'], 'Ava + Ben')
        self.assertEqual(boards['consistency'][0]['consistency_spread'], 4)

    def test_latest_result_compares_with_crew_best(self):
        ab = ('ava', 'ben')
        latest = leaderboard.latest_result([record(1, ab, 10, 4, 9.0), record(2, ab, 12, 4, 9.0)])
        self.assertTrue(latest['new_best'])
        self.assertEqual(latest['previous_best_score'], 10)
        first = leaderboard.latest_result([record(1, ab, 10, 4, 9.0)])
        self.assertTrue(first['first_match_for_crew'])

    def test_not_counted_needs_a_reason_and_is_revisioned(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                leaderboard.set_counted(folder, 'match-x', False, '')
            self.assertEqual(leaderboard.set_counted(folder, 'match-x', False, 'robot fault')['revision'], 1)
            self.assertEqual(leaderboard.set_counted(folder, 'match-x', True, 'fixed')['revision'], 2)


if __name__ == '__main__':
    unittest.main()
