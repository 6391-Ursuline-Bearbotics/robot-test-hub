"""Replay regression (P4.1): did a robot-code change alter what the code would have done?

Runs AdvantageKit replay of one recorded log through robot code at one or two git
commits and compares logged outputs:

- original vs replay (same commit as on the robot): replay determinism check. Outputs
  that differ here cannot be trusted for comparing commits.
- replay at commit A vs replay at commit B: what the change would have done
  differently with the same recorded inputs.

Each commit is checked out into a temporary git worktree (the robot repository's own
checkout is not touched) and built with its `replayLog` Gradle task (robot code that
predates that task cannot be replayed this way). Human review is still required before
deployment.

    python tools/replay_regression.py --robot-repo C:\\FRC\\2027-6391-Rebuilt-V3 --log incident.wpilog \\
        --commit abc123 [--compare-commit def456] [--prefix Drive/ --prefix Indexer/]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from robot_test_hub.wpilog import records, _control  # noqa: E402

JAVA_HOME = r'C:\Users\Public\wpilib\2027_alpha7\jdk'
# AdvantageKit's own timing/console outputs and the Test Hub status bridge (live clock,
# sequence numbers) always differ between runs; they are not robot behavior.
IGNORED = ('LoggedRobot/', 'Logger/', 'Console', 'TestHub/')


def outputs(path, category, prefixes=()):
    """{key: {timestamp_us: (type, payload)}} for /<category>/<key> fields."""
    head = f'/{category}/'
    active, generations, out = {}, {}, {}
    with open(path, 'rb') as stream:
        for record in records(stream, truncated_tail={}):
            if record.entry == 0:
                _control(record, active, generations)
                continue
            entry = active.get(record.entry)
            if entry is None or not entry['field'].startswith(head):
                continue
            key = entry['field'][len(head):]
            if (prefixes and not key.startswith(tuple(prefixes))) or key.startswith(IGNORED):
                continue
            out.setdefault(key, {})[record.timestamp_us] = (entry['type'], record.payload)
    return out


def _number(kind, payload):
    if kind == 'double' and len(payload) == 8:
        return struct.unpack('<d', payload)[0]
    if kind == 'float' and len(payload) == 4:
        return struct.unpack('<f', payload)[0]
    if kind == 'int64' and len(payload) == 8:
        return struct.unpack('<q', payload)[0]
    return None


def compare(left, right, *, tolerance=1e-9):
    """Per key: samples at shared timestamps, how many differ, first difference, largest numeric gap."""
    report = {}
    for key in sorted(set(left) | set(right)):
        a, b = left.get(key, {}), right.get(key, {})
        if not a or not b:
            report[key] = {'status': 'only_in_' + ('left' if a else 'right')}
            continue
        shared = sorted(set(a) & set(b))
        differ, first, gap = 0, None, 0.0
        for t in shared:
            (ka, pa), (kb, pb) = a[t], b[t]
            na, nb = _number(ka, pa), _number(kb, pb)
            same = abs(na - nb) <= tolerance if na is not None and nb is not None else pa == pb
            if not same:
                differ += 1
                first = t if first is None else first
                if na is not None and nb is not None:
                    gap = max(gap, abs(na - nb))
        report[key] = {'status': 'same' if differ == 0 else 'different', 'shared_samples': len(shared),
                       'different_samples': differ, 'first_difference_s': first / 1e6 if first is not None else None,
                       'largest_numeric_gap': gap if differ else 0.0}
    return report


def replay(robot_repo, commit, log, workdir):
    """Replay `log` at `commit`; return the path of the replay output log."""
    tree = Path(workdir) / ('tree-' + commit[:12])
    subprocess.run(['git', '-C', str(robot_repo), 'worktree', 'add', '--detach', str(tree), commit], check=True,
                   capture_output=True)
    try:
        copy = Path(workdir) / f'{commit[:12]}.wpilog'
        shutil.copyfile(log, copy)
        env = dict(os.environ, JAVA_HOME=os.environ.get('JAVA_HOME', JAVA_HOME))
        gradle = tree / ('gradlew.bat' if os.name == 'nt' else 'gradlew')
        result = subprocess.run([str(gradle), 'replayLog', f'-PreplayLog={copy}', '--console=plain'], cwd=tree, env=env,
                                capture_output=True, text=True, timeout=1800)
        produced = copy.with_name(copy.stem + '_sim.wpilog')
        if not produced.is_file():
            raise RuntimeError(f'Replay at {commit} produced no output log:\n' + result.stdout[-2000:] + result.stderr[-2000:])
        return produced
    finally:
        subprocess.run(['git', '-C', str(robot_repo), 'worktree', 'remove', '--force', str(tree)], capture_output=True)


def summarize(report):
    changed = {k: v for k, v in report.items() if v['status'] == 'different'}
    one_side = sorted(k for k, v in report.items() if v['status'].startswith('only_in'))
    compared = len(report) - len(one_side)
    return {'keys': compared, 'same': compared - len(changed), 'changed': changed, 'only_one_side': one_side}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--robot-repo', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--compare-commit')
    parser.add_argument('--prefix', action='append', default=[], help='Only compare outputs under this key prefix')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix='replay-') as work:
        first = replay(args.robot_repo, args.commit, args.log, work)
        result = {'determinism': summarize(compare(outputs(args.log, 'RealOutputs', args.prefix),
                                                   outputs(first, 'ReplayOutputs', args.prefix)))}
        if args.compare_commit:
            second = replay(args.robot_repo, args.compare_commit, args.log, work)
            result['change'] = summarize(compare(outputs(first, 'ReplayOutputs', args.prefix),
                                                 outputs(second, 'ReplayOutputs', args.prefix)))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        d = result['determinism']
        print(f"Replay vs recording at {args.commit}: {d['same']} of {d['keys']} outputs identical.")
        for key, item in list(d['changed'].items())[:20]:
            print(f"  not reproduced: {key} ({item['status']}, {item.get('different_samples', '-')} samples)")
        if 'change' in result:
            c = result['change']
            print(f"{args.commit} vs {args.compare_commit}: {len(c['changed'])} of {c['keys']} outputs differ.")
            for key, item in list(c['changed'].items())[:40]:
                print(f"  {key}: {item['status']}, first at {item.get('first_difference_s')} s, "
                      f"{item.get('different_samples')} samples, largest gap {item.get('largest_numeric_gap')}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
