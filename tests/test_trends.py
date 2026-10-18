"""Trend series: same robot and modes, gaps stay gaps, highlights match the home-page rule."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import time
import unittest

from robot_test_hub import run_metrics, trends
from robot_test_hub.storage import open_catalog

S = 1_000_000_000


class TrendSeriesTests(unittest.TestCase):
    def test_series_split_by_modes_with_gaps_and_unusual_points(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(open_catalog(root)) as db, db:
                run_metrics.install_schema(db)
                values = [20, 21, 19, 20, 22, None, 20, 45]
                for i, value in enumerate(values):
                    run_id = f'tele-{i}'
                    db.execute('INSERT INTO run_metrics_runs VALUES (?,?,?,?,?,?,?,?)',
                               (run_id, run_metrics.METRICS_VERSION, 'rio', str((i + 1) * 1000 * S), json.dumps(['TELEOPERATED']),
                                140.0, '[]', time.time_ns()))
                    if value is not None:
                        db.execute('INSERT INTO run_metrics VALUES (?,?,?,?,?,?,?)',
                                   (run_id, run_metrics.METRICS_VERSION, 'Drive/Module3/Drive', 'current_mean_a', value, 'A', 10))
                db.execute('INSERT INTO run_metrics_runs VALUES (?,?,?,?,?,?,?,?)',
                           ('auto-0', run_metrics.METRICS_VERSION, 'rio', str(500 * S), json.dumps(['AUTONOMOUS']), 20.0, '[]', 0))
                db.execute('INSERT INTO run_metrics VALUES (?,?,?,?,?,?,?)',
                           ('auto-0', run_metrics.METRICS_VERSION, 'Drive/Module3/Drive', 'current_mean_a', 5.0, 'A', 10))
            result = trends.series(root)
            [group] = result['groups']
            self.assertEqual(group['modes'], ['Teleop'])
            self.assertEqual([p['value'] for p in group['points']], values)
            self.assertEqual([p['unusual'] for p in group['points']], [False] * 7 + [True])
            self.assertEqual(group['label'], 'Back-right drive motor')
            self.assertEqual(trends.series(root, component='robot')['groups'], [])


if __name__ == '__main__':
    unittest.main()
