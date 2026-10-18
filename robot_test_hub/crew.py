"""Local roster, crew selections and stick time (P1b "who is doing what").

The roster is private: it lives only in the local catalog, never in Git, and is not
part of Drive exports. A crew selection is a time interval that starts when it is
saved and lasts until the next one. Corrections for a run are new revisions; the
original selection and its save time are kept. Time with no crew is "Unassigned",
never credited to anyone.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import time
import uuid

ROLES = ('driver', 'operator', 'coach')
MAX_NAME = 40
MAX_ROSTER = 200


class CrewError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def install_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS roster (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_utc_ns INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS crew_selections (
        id TEXT PRIMARY KEY, start_utc_ns INTEGER NOT NULL, roles_json TEXT NOT NULL, saved_utc_ns INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS crew_corrections (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, roles_json TEXT NOT NULL, reason TEXT NOT NULL,
        saved_utc_ns INTEGER NOT NULL, revision INTEGER NOT NULL, UNIQUE(run_id, revision))""")


def _connect(root, write):
    path = Path(root).resolve() / 'catalog.sqlite3'
    if write:
        db = sqlite3.connect(path, timeout=2)
    else:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        db.execute('PRAGMA query_only=ON')
    db.row_factory = sqlite3.Row
    return db


def _has(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crew_selections'").fetchone() is not None


def _roles(payload, roster_ids):
    if not isinstance(payload, dict) or payload.keys() - set(ROLES):
        raise CrewError('invalid_roles')
    roles = {}
    for role in ROLES:
        value = payload.get(role)
        if value in (None, ''):
            continue
        if value not in roster_ids:
            raise CrewError('unknown_student')
        roles[role] = value
    return roles


def roster(root):
    if not (Path(root) / 'catalog.sqlite3').exists():
        return []
    with closing(_connect(root, False)) as db:
        if not _has(db):
            return []
        return [dict(row) for row in db.execute('SELECT id,name,active FROM roster ORDER BY active DESC, lower(name)')]


def add_student(root, name):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME or any(ord(c) < 32 for c in name):
        raise CrewError('invalid_name')
    name = name.strip()
    with closing(_connect(root, True)) as db, db:
        install_schema(db)
        if db.execute('SELECT count(*) FROM roster').fetchone()[0] >= MAX_ROSTER:
            raise CrewError('roster_full')
        existing = db.execute('SELECT id FROM roster WHERE lower(name)=lower(?) AND active=1', (name,)).fetchone()
        if existing:
            return {'id': existing[0], 'name': name, 'active': 1}
        identity = 'student-' + uuid.uuid4().hex[:12]
        db.execute('INSERT INTO roster VALUES (?,?,1,?)', (identity, name, time.time_ns()))
        return {'id': identity, 'name': name, 'active': 1}


def set_active(root, student_id, active):
    with closing(_connect(root, True)) as db, db:
        install_schema(db)
        if db.execute('UPDATE roster SET active=? WHERE id=?', (1 if active else 0, student_id)).rowcount != 1:
            raise CrewError('unknown_student', 404)


def select_crew(root, payload, *, now_ns=None):
    """Start a new crew interval now. An empty selection means nobody is assigned."""
    now_ns = now_ns or time.time_ns()
    with closing(_connect(root, True)) as db, db:
        install_schema(db)
        ids = {row[0] for row in db.execute('SELECT id FROM roster')}
        roles = _roles(payload, ids)
        identity = 'crew-' + uuid.uuid4().hex[:12]
        db.execute('INSERT INTO crew_selections VALUES (?,?,?,?)', (identity, now_ns, json.dumps(roles, sort_keys=True), now_ns))
        return {'id': identity, 'start_utc_ns': str(now_ns), 'roles': roles}


def current(root):
    if not (Path(root) / 'catalog.sqlite3').exists():
        return None
    with closing(_connect(root, False)) as db:
        if not _has(db):
            return None
        row = db.execute('SELECT * FROM crew_selections ORDER BY start_utc_ns DESC, saved_utc_ns DESC LIMIT 1').fetchone()
        return None if row is None else {'id': row['id'], 'start_utc_ns': str(row['start_utc_ns']), 'roles': json.loads(row['roles_json'])}


def correct_run(root, run_id, payload, reason=''):
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        raise CrewError('invalid_run')
    if not isinstance(reason, str) or len(reason) > 200:
        raise CrewError('invalid_reason')
    now = time.time_ns()
    with closing(_connect(root, True)) as db, db:
        install_schema(db)
        roles = _roles(payload, {row[0] for row in db.execute('SELECT id FROM roster')})
        revision = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM crew_corrections WHERE run_id=?', (run_id,)).fetchone()[0]
        db.execute('INSERT INTO crew_corrections VALUES (?,?,?,?,?,?)',
                   ('fix-' + uuid.uuid4().hex[:12], run_id, json.dumps(roles, sort_keys=True), reason, now, revision))
        return {'run_id': run_id, 'revision': revision, 'roles': roles}


def crew_for_runs(db, runs):
    """{run_id: {'roles', 'source', 'revision'}} using corrections first, then the selection at run start."""
    if not _has(db):
        return {}
    selections = [(row[0], json.loads(row[1])) for row in
                  db.execute('SELECT start_utc_ns, roles_json FROM crew_selections ORDER BY start_utc_ns, saved_utc_ns')]
    result = {}
    for run_id, start in runs:
        fix = db.execute('SELECT roles_json, revision FROM crew_corrections WHERE run_id=? ORDER BY revision DESC LIMIT 1',
                         (run_id,)).fetchone()
        if fix:
            result[run_id] = {'roles': json.loads(fix[0]), 'source': 'corrected', 'revision': fix[1]}
            continue
        if start is None:
            continue
        chosen = None
        for selection_start, roles in selections:
            if selection_start <= start:
                chosen = roles
            else:
                break
        if chosen is not None:
            result[run_id] = {'roles': chosen, 'source': 'selected', 'revision': None}
    return result


def names(db):
    if not _has(db):
        return {}
    return {row[0]: row[1] for row in db.execute('SELECT id, name FROM roster')}


def stick_time(root, *, since_utc_ns=None):
    """Per student and role: enabled seconds, match seconds, runs, matches, last practiced."""
    from .practice import _all_runs, _start_utc_ns, _duration_seconds
    from . import matches, run_metrics
    path = Path(root).resolve() / 'catalog.sqlite3'
    empty = {'students': [], 'unassigned': {'seconds': 0.0, 'runs': 0}}
    if not path.exists():
        return empty
    with closing(_connect(root, False)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='run_lookup_runs'").fetchone() is None:
            return empty
        runs = [r for r in _all_runs(db) if _start_utc_ns(r) is not None
                and (since_utc_ns is None or _start_utc_ns(r) >= since_utc_ns)]
        runs.sort(key=_start_utc_ns)
        contexts = {r['run_id']: run_metrics.context_for(db, r['run_id']) for r in runs}
        in_match = matches.detect(runs, contexts)
        crews = crew_for_runs(db, [(r['run_id'], _start_utc_ns(r)) for r in runs])
        who = names(db)
    stats, unassigned = {}, {'seconds': 0.0, 'runs': 0}
    for run in runs:
        seconds = _duration_seconds(run) or 0.0
        roles = (crews.get(run['run_id']) or {}).get('roles') or {}
        if not roles:
            unassigned['seconds'] += seconds
            unassigned['runs'] += 1
            continue
        match = in_match.get(run['run_id'])
        for role, student in roles.items():
            entry = stats.setdefault((student, role), {'student_id': student, 'name': who.get(student, 'Removed student'),
                                                       'role': role, 'seconds': 0.0, 'match_seconds': 0.0,
                                                       'runs': 0, 'matches': set(), 'last_utc_ns': None})
            entry['seconds'] += seconds
            entry['runs'] += 1
            if match:
                entry['match_seconds'] += seconds
                entry['matches'].add(match['id'])
            entry['last_utc_ns'] = str(_start_utc_ns(run))
    students = []
    for entry in stats.values():
        entry['matches'] = len(entry['matches'])
        students.append(entry)
    students.sort(key=lambda e: (-e['seconds'], e['name'], e['role']))
    return {'students': students, 'unassigned': unassigned}


def week_start_utc_ns(now=None):
    now = now or datetime.now().astimezone()
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1e9)
