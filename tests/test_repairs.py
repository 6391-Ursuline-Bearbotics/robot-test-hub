"""Before/after after a fix: same robot and modes, only the next few runs, append-only."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from robot_test_hub import repairs, run_metrics
from robot_test_hub.storage import open_catalog

S = 1_000_000_000
V = run_metrics.METRICS_VERSION


class RepairTests(unittest.TestCase):
    def test_before_after_shows_on_next_comparable_runs_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(open_catalog(root)) as db, db:
                run_metrics.install_schema(db)
                for i, (value, modes) in enumerate([(3.2, 'TELEOPERATED'), (0.4, 'TELEOPERATED'), (5.0, 'AUTONOMOUS')]):
                    db.execute('INSERT INTO run_metrics_runs VALUES (?,?,?,?,?,?,?,?)',
                               (f'r{i}', V, 'rio', str((i + 1) * 100 * S), json.dumps([modes]), 140.0, '[]', 0))
                    db.execute('INSERT INTO run_metrics VALUES (?,?,?,?,?,?,?)',
                               (f'r{i}', V, 'Drive/Module3', 'drive_speed_error_mps', value, 'm/s', 10))
            repairs.record(root, 'r0', 'Drive/Module3', 'drive_speed_error_mps', 'reseated back-right CAN cable', now_ns=150 * S)
            with self.assertRaises(ValueError):
                repairs.record(root, 'r0', 'Drive/Module3', 'drive_speed_error_mps', '  ')
            with closing(open_catalog(root)) as db:
                row = {'component': 'Drive/Module3', 'metric': 'drive_speed_error_mps', 'value': 0.4, 'unit': 'm/s'}
                tele = {'robot_id': 'rio', 'phases': [{'mode': 'TELEOPERATED'}]}
                [item] = repairs.before_after(db, tele, 200 * S, [row])
                self.assertEqual(item['sentence'], 'After the fix “reseated back-right CAN cable”: Back-right module '
                                                   'wheel speed off target by 3.2 m/s → 0.40 m/s.')
                self.assertEqual(repairs.before_after(db, tele, 120 * S, [row]), [])  # run before the fix
                auto = {'robot_id': 'rio', 'phases': [{'mode': 'AUTONOMOUS'}]}
                self.assertEqual(repairs.before_after(db, auto, 300 * S, [dict(row, value=5.0)]), [])  # other modes
                self.assertEqual(repairs.before_after(db, dict(tele, robot_id='other'), 200 * S, [row]), [])


if __name__ == '__main__':
    unittest.main()
