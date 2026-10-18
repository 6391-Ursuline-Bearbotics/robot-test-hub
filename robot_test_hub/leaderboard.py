"""Practice-match records and leaderboards (P1b 9-13).

A match record joins the detected match (matches.py), its crew (crew.py) and the
season results of its runs (season.py). Scores are the season file's estimate, not
official scores. A mentor can mark a match "not counted" with a reason; the match
stays visible and its data is kept. Boards favor personal bests and improvement.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import statistics
import time

MIN_CYCLES_FOR_AVERAGE = 3
CONSISTENCY_MATCHES = 5


def install_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS match_exclusions (
        match_id TEXT NOT NULL, revision INTEGER NOT NULL, counted INTEGER NOT NULL, reason TEXT NOT NULL,
        saved_utc_ns INTEGER NOT NULL, PRIMARY KEY (match_id, revision))""")


def set_counted(root, match_id, counted, reason):
    if not isinstance(match_id, str) or not match_id.startswith('match-') or len(match_id) > 64:
        raise ValueError('invalid_match')
    if not isinstance(reason, str) or (not counted and not reason.strip()) or len(reason) > 200:
        raise ValueError('reason_required')
    with closing(sqlite3.connect(Path(root).resolve() / 'catalog.sqlite3', timeout=2)) as db, db:
        install_schema(db)
        revision = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM match_exclusions WHERE match_id=?', (match_id,)).fetchone()[0]
        db.execute('INSERT INTO match_exclusions VALUES (?,?,?,?,?)', (match_id, revision, 1 if counted else 0, reason.strip(), time.time_ns()))
        return {'match_id': match_id, 'revision': revision, 'counted': bool(counted)}


def _exclusions(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='match_exclusions'").fetchone() is None:
        return {}
    out = {}
    for match_id, counted, reason in db.execute(
            'SELECT match_id, counted, reason FROM match_exclusions ORDER BY match_id, revision'):
        out[match_id] = (bool(counted), reason)
    return out


def records(root):
    """All detected matches, oldest first, with crew, season totals and counted state."""
    from . import crew, matches, run_metrics, season
    from .practice import _all_runs, _start_utc_ns
    path = Path(root).resolve() / 'catalog.sqlite3'
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='run_lookup_runs'").fetchone() is None:
            return []
        runs = sorted((r for r in _all_runs(db) if _start_utc_ns(r) is not None), key=_start_utc_ns)
        found = matches.detect(runs, {r['run_id']: run_metrics.context_for(db, r['run_id']) for r in runs})
        crews = crew.crew_for_runs(db, [(r['run_id'], _start_utc_ns(r)) for r in runs])
        names = crew.names(db)
        excluded = _exclusions(db)
        versions = {s['version']: s for s in season.available()}
        rows = {}
        if season.has_schema(db):
            for run_id, version, result in db.execute('SELECT run_id, season_version, result_json FROM season_results'):
                if version in versions:
                    rows[run_id] = json.loads(result)
        by_id = {r['run_id']: r for r in runs}
    out, seen = [], set()
    for run in runs:
        match = found.get(run['run_id'])
        if not match or match['id'] in seen:
            continue
        seen.add(match['id'])
        parts = [rows.get(run_id) for run_id in match['run_ids']]
        roles = (crews.get(match['run_ids'][0]) or {}).get('roles') or {}
        durations = [d for p in parts if p for d in p['tasks'].get('Cycle', {}).get('durations_s', [])]
        auto = parts[0]['counts'] if parts and parts[0] else None
        teleop = parts[1]['counts'] if len(parts) > 1 and parts[1] else None
        counted, reason = excluded.get(match['id'], (True, ''))
        out.append({'match_id': match['id'], 'label': match['label'], 'fms': match['fms'],
                    'start_utc_ns': str(_start_utc_ns(by_id[match['run_ids'][0]])), 'run_ids': match['run_ids'],
                    'complete': match['complete'], 'crew': {role: {'id': sid, 'name': names.get(sid, 'Removed student')}
                                                            for role, sid in roles.items()},
                    'scored': all(parts) and bool(parts),
                    'score': sum(p['score'] or 0 for p in parts if p) if any(parts) else None,
                    'score_label': next((p['score_label'] for p in parts if p), None),
                    'cycles': len(durations), 'cycle_average_s': statistics.mean(durations) if durations else None,
                    'cycle_best_s': min(durations) if durations else None,
                    'auto': auto, 'teleop': teleop, 'counted': counted, 'not_counted_reason': reason or None})
    return out


def _crew_key(record):
    driver, operator = record['crew'].get('driver'), record['crew'].get('operator')
    if not driver:
        return None
    return (driver['id'], operator['id'] if operator else None)


def _crew_label(record):
    driver, operator = record['crew'].get('driver'), record['crew'].get('operator')
    return driver['name'] + (' + ' + operator['name'] if operator else '')


def boards(records_, *, since_utc_ns=None, week_start_utc_ns=None):
    """Leaderboards by crew (driver + operator) from counted, scored matches."""
    eligible = [r for r in records_ if r['counted'] and r['scored'] and _crew_key(r)
                and (since_utc_ns is None or int(r['start_utc_ns']) >= since_utc_ns)]
    crews = {}
    for record in eligible:
        crews.setdefault(_crew_key(record), []).append(record)
    entries = []
    for key, items in crews.items():
        scores = [r['score'] for r in items]
        cycles = [r['cycles'] for r in items]
        averages = [r['cycle_average_s'] for r in items if r['cycle_average_s'] is not None and r['cycles'] >= MIN_CYCLES_FOR_AVERAGE]
        recent = scores[-CONSISTENCY_MATCHES:]
        improvement = None
        if week_start_utc_ns is not None:
            before = [r['score'] for r in items if int(r['start_utc_ns']) < week_start_utc_ns]
            during = [r['score'] for r in items if int(r['start_utc_ns']) >= week_start_utc_ns]
            if before and during:
                improvement = statistics.mean(during) - statistics.mean(before)
        entries.append({'crew': _crew_label(items[-1]), 'crew_key': list(key), 'matches': len(items),
                        'best_score': max(scores), 'most_cycles': max(cycles),
                        'fastest_average_cycle_s': min(averages) if averages else None,
                        'consistency_spread': (max(recent) - min(recent)) if len(recent) >= 3 else None,
                        'improvement': improvement})

    def ranked(field, reverse=True):
        rows = [e for e in entries if e[field] is not None]
        return sorted(rows, key=lambda e: (-e[field] if reverse else e[field], e['crew']))
    return {'best_score': ranked('best_score'), 'most_cycles': ranked('most_cycles'),
            'fastest_average_cycle': ranked('fastest_average_cycle_s', reverse=False),
            'most_improved': [e for e in ranked('improvement') if e['improvement'] > 0],
            'consistency': ranked('consistency_spread', reverse=False),
            'basis': {'min_cycles_for_average': MIN_CYCLES_FOR_AVERAGE, 'consistency_matches': CONSISTENCY_MATCHES,
                      'score': 'season file estimate, not an official score'}}


def latest_result(records_):
    """The newest match with a comparison to that crew's previous best."""
    if not records_:
        return None
    latest = records_[-1]
    key = _crew_key(latest)
    previous = [r for r in records_[:-1] if key and _crew_key(r) == key and r['counted'] and r['scored']]
    best = max((r['score'] for r in previous), default=None)
    best_cycles = max((r['cycles'] for r in previous), default=None)
    return dict(latest, crew_label=_crew_label(latest) if key else 'Unassigned crew',
                previous_best_score=best, previous_best_cycles=best_cycles,
                new_best=bool(latest['scored'] and best is not None and latest['score'] > best),
                first_match_for_crew=bool(key and not previous))
