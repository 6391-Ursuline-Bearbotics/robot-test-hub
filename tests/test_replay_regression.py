"""Replay regression comparison on invented logs (the replay run itself needs the robot repo)."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools'))
import replay_regression  # noqa: E402
from akit26_fixture import Writer  # noqa: E402


def log(path, category, speeds, extra=None):
    w = Writer()
    for i, speed in enumerate(speeds):
        t = 1_000_000 + i * 20_000
        w.value(f'/{category}/Drive/CommandedSpeed', 'double', t, speed)
        w.value(f'/{category}/LoggedRobot/FullCycleMS', 'double', t, 10.0 + i)
        w.value(f'/{category}/Indexer/Goal', 'string', t, 'FEED' if i == 3 else 'IDLE')
        if extra:
            w.value(f'/{category}/{extra}', 'double', t, 1.0)
    Path(path).write_bytes(bytes(w.data))
    return path


class ReplayCompareTests(unittest.TestCase):
    def test_identical_replay_and_detected_change(self):
        with tempfile.TemporaryDirectory() as folder:
            original = log(Path(folder) / 'a.wpilog', 'RealOutputs', [0, 1, 2, 3])
            same = log(Path(folder) / 'b.wpilog', 'ReplayOutputs', [0, 1, 2, 3])
            changed = log(Path(folder) / 'c.wpilog', 'ReplayOutputs', [0, 1, 2.5, 3.5], extra='New/Output')
            ok = replay_regression.summarize(replay_regression.compare(
                replay_regression.outputs(original, 'RealOutputs'), replay_regression.outputs(same, 'ReplayOutputs')))
            self.assertEqual((ok['keys'], ok['same'], ok['changed']), (2, 2, {}))  # timing output ignored
            diff = replay_regression.summarize(replay_regression.compare(
                replay_regression.outputs(same, 'ReplayOutputs'), replay_regression.outputs(changed, 'ReplayOutputs')))
            item = diff['changed']['Drive/CommandedSpeed']
            self.assertEqual((item['different_samples'], item['first_difference_s'], item['largest_numeric_gap']), (2, 1.04, 0.5))
            self.assertEqual(diff['only_one_side'], ['New/Output'])
            self.assertNotIn('Indexer/Goal', diff['changed'])

    def test_prefix_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            original = log(Path(folder) / 'a.wpilog', 'RealOutputs', [0, 1])
            self.assertEqual(sorted(replay_regression.outputs(original, 'RealOutputs', ['Drive/'])), ['Drive/CommandedSpeed'])


if __name__ == '__main__':
    unittest.main()
