"""Season-configurable game actions, cycle times and a score estimate (P1b 5-7).

A season file (robot_test_hub/seasons/<year>.json, reviewed and committed, no
private data) names actions and how to detect them from logged signals:

- "counter": a monotonic counter increased (preferred; robot-side PracticeEvents)
- "rising": a boolean went false -> true
- "enters": a value became equal to "value" (for example a goal state string)
- "threshold": a number rose above "above" after having been below "reset_below"

Timed tasks measure from the first "from" action after the previous "to" action
until the next "to" action, discarding anything longer than max_seconds so a missed
event cannot create a 90 s "cycle". Results are stored per run under the season
file's content hash, so editing a definition adds new results beside old ones.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import time

SEASONS = Path(__file__).parent / 'seasons'
DETECTORS = ('counter', 'rising', 'enters', 'threshold')


class SeasonError(ValueError):
    pass


def load(path):
    raw = Path(path).read_bytes()
    season = json.loads(raw)
    if season.get('schema_version') != 1 or not isinstance(season.get('actions'), dict) or not season['actions']:
        raise SeasonError('Season file needs schema_version 1 and at least one action')
    for name, action in season['actions'].items():
        if action.get('detect') not in DETECTORS or not isinstance(action.get('signal'), str):
            raise SeasonError(f'Action {name} needs a signal and a detect mode in {DETECTORS}')
        if action['detect'] == 'enters' and 'value' not in action:
            raise SeasonError(f'Action {name} needs "value"')
        if action['detect'] == 'threshold' and not all(isinstance(action.get(k), (int, float)) for k in ('above', 'reset_below')):
            raise SeasonError(f'Action {name} needs numeric "above" and "reset_below"')
    for name, task in (season.get('timed_tasks') or {}).items():
        if task.get('from') not in season['actions'] or task.get('to') not in season['actions'] \
                or not isinstance(task.get('max_seconds'), (int, float)) or task['max_seconds'] <= 0:
            raise SeasonError(f'Timed task {name} needs known from/to actions and positive max_seconds')
    season['version'] = f"{season.get('season')}-{hashlib.sha256(raw).hexdigest()[:12]}"
    return season


def available():
    return [load(path) for path in sorted(SEASONS.glob('*.json'))]


def for_profile(profile, seasons=None):
    for season in seasons if seasons is not None else available():
        if profile in season.get('profiles', []):
            return season
    return None


class Detector:
    def __init__(self, season):
        self.season = season
        self.by_signal = defaultdict(list)
        for name, action in season['actions'].items():
            self.by_signal[action['signal']].append((name, action))
        self.previous = {}
        self.armed = {}
        self.events = []

    def observe(self, field, value, t_ns):
        for name, action in self.by_signal.get(field, ()):
            key = (name, field)
            before = self.previous.get(key)
            mode, hit = action['detect'], False
            if mode == 'counter' and type(value) is int and type(before) is int:
                for _ in range(max(0, min(value - before, 1000))):
                    self.events.append((t_ns, name))
            elif mode == 'rising':
                hit = value is True and before is False
            elif mode == 'enters':
                hit = value == action['value'] and before != action['value'] and before is not None
            elif mode == 'threshold' and isinstance(value, (int, float)) and not isinstance(value, bool):
                if value < action['reset_below']:
                    self.armed[key] = True
                elif value > action['above'] and self.armed.get(key, False):
                    hit, self.armed[key] = True, False
            if hit:
                self.events.append((t_ns, name))
            self.previous[key] = value


def summarize(season, events):
    """Counts, cycles and score proxy from time-ordered (t_ns, action) events."""
    counts = {name: 0 for name in season['actions']}
    for _, name in events:
        counts[name] += 1
    tasks = {}
    for task_name, task in (season.get('timed_tasks') or {}).items():
        durations, pending = [], None
        for t, name in events:
            if name == task['from'] and pending is None:
                pending = t
            elif name == task['to']:
                if pending is not None and task.get('min_seconds', 0.1) <= (t - pending) / 1e9 <= task['max_seconds']:
                    durations.append((t - pending) / 1e9)
                pending = None
        tasks[task_name] = {'count': len(durations), 'durations_s': durations,
                            'average_s': statistics.mean(durations) if durations else None,
                            'best_s': min(durations) if durations else None}
    proxy = season.get('score_proxy') or {}
    score = sum(counts.get(name, 0) * points for name, points in (proxy.get('points') or {}).items())
    return {'counts': counts, 'tasks': tasks, 'score': score if proxy else None,
            'score_label': proxy.get('label'), 'events': [[str(t), name] for t, name in events]}


def install_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS season_results (
        run_id TEXT NOT NULL, season_version TEXT NOT NULL, result_json TEXT NOT NULL, computed_utc_ns INTEGER NOT NULL,
        PRIMARY KEY (run_id, season_version))""")


def has_schema(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='season_results'").fetchone() is not None


def step(root, db, document, *, seasons=None, max_jobs=1):
    """Detect season actions for runs that lack results for the current season file."""
    from .importer import Importer
    seasons = available() if seasons is None else seasons
    if not seasons:
        return 0
    with db:
        install_schema(db)
    importer = Importer(root, db)
    profiles = {job['id']: job['profile'] for job in importer.list_jobs()}
    pending = []
    for run in document['runs']:
        season = for_profile(profiles.get(run['segment_ids'][0]), seasons) if run['segment_ids'] else None
        if season is None:
            continue
        if db.execute('SELECT 1 FROM season_results WHERE run_id=? AND season_version=?',
                      (run['run_id'], season['version'])).fetchone() is None:
            pending.append((run, season))
    if not pending:
        return 0
    jobs = sorted({j for run, _ in pending for j in run['segment_ids']})[:max_jobs]
    chosen = [(run, season) for run, season in pending if set(run['segment_ids']) <= set(jobs)] or [pending[0]]
    jobs = sorted({j for run, _ in chosen for j in run['segment_ids']})
    # Robot time restarts in every file (boot), so spans are matched per dataset.
    spans_by_job = defaultdict(list)
    for run, _ in chosen:
        for job in run['segment_ids']:
            spans_by_job[job].append((int(run['start_monotonic_ns']), int(run['end_monotonic_ns']), run['run_id']))
    detectors = {run['run_id']: Detector(season) for run, season in chosen}
    signals = {s for d in detectors.values() for s in d.by_signal}
    for job in jobs:
        spans = sorted(spans_by_job[job])
        for row in importer.iter_dataset(job, kind='observation'):
            if row.get('field') not in signals or row.get('validity') != 'valid' or row.get('cycle_timestamp_ns') is None:
                continue
            t = int(row['cycle_timestamp_ns'])
            for start, end, run_id in spans:
                if start <= t <= end:
                    detectors[run_id].observe(row['field'], row['value'], t)
                    break
                # Track state before the run starts so the first change inside it is an edge.
                if t < start:
                    detectors[run_id].observe(row['field'], row['value'], None)
    now = time.time_ns()
    with db:
        for run, season in chosen:
            events = sorted((t, name) for t, name in detectors[run['run_id']].events if t is not None)
            result = dict(summarize(season, events), season=season.get('season'), game=season.get('game'))
            db.execute('INSERT OR REPLACE INTO season_results VALUES (?,?,?,?)',
                       (run['run_id'], season['version'], json.dumps(result, sort_keys=True), now))
    return len(chosen)


def result_for(db, run_id, season_version):
    if not has_schema(db):
        return None
    row = db.execute('SELECT result_json FROM season_results WHERE run_id=? AND season_version=?',
                     (run_id, season_version)).fetchone()
    return json.loads(row[0]) if row else None
