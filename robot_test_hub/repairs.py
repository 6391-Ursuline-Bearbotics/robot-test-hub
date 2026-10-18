"""Before/after for fixes (P4.2): link a repair to a finding, then compare later runs.

A student or mentor marks a measured finding as fixed with a short description. The
next comparable runs (same robot and modes, after the repair was saved) show the same
measurement before and after. This is a descriptive comparison of recorded values,
not proof that the repair caused the change. Repairs are append-only records.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import uuid

SHOW_RUNS_AFTER = 3


def install_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS repairs (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, component TEXT NOT NULL, metric TEXT NOT NULL,
        description TEXT NOT NULL, saved_utc_ns INTEGER NOT NULL)""")


def _has(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='repairs'").fetchone() is not None


def record(root, run_id, component, metric, description, *, now_ns=None):
    for value, limit in ((run_id, 128), (component, 128), (metric, 64)):
        if not isinstance(value, str) or not value or len(value) > limit:
            raise ValueError('invalid_repair')
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise ValueError('description_required')
    with closing(sqlite3.connect(Path(root).resolve() / 'catalog.sqlite3', timeout=2)) as db, db:
        install_schema(db)
        identity = 'fix-' + uuid.uuid4().hex[:12]
        db.execute('INSERT INTO repairs VALUES (?,?,?,?,?,?)',
                   (identity, run_id, component, metric, description.strip(), now_ns or time.time_ns()))
        return {'id': identity}


def before_after(db, run, start_ns, rows):
    """Sentences for repairs that this run follows. rows: this run's run_metrics rows."""
    from . import run_metrics
    from .practice import _mfmt, METRIC_SIBLINGS, ROBOT_WORDS
    if not _has(db) or not rows or not run_metrics.has_schema(db):
        return []
    mine = {(r['component'], r['metric']): r for r in rows}
    modes = json.dumps(sorted({p['mode'] for p in run.get('phases') or []}))
    out = []
    for repair_id, fixed_run, component, metric, description, saved in db.execute(
            'SELECT id, run_id, component, metric, description, saved_utc_ns FROM repairs ORDER BY saved_utc_ns DESC'):
        if saved >= start_ns or (component, metric) not in mine:
            continue
        before = db.execute('SELECT value FROM run_metrics WHERE run_id=? AND metrics_version=? AND component=? AND metric=?',
                            (fixed_run, run_metrics.METRICS_VERSION, component, metric)).fetchone()
        fixed_info = db.execute('SELECT robot_id, modes FROM run_metrics_runs WHERE run_id=? AND metrics_version=?',
                                (fixed_run, run_metrics.METRICS_VERSION)).fetchone()
        if before is None or fixed_info is None or fixed_info[0] != run.get('robot_id') or fixed_info[1] != modes:
            continue
        later = db.execute('''SELECT COUNT(*) FROM run_metrics_runs WHERE metrics_version=? AND robot_id IS ? AND modes=?
            AND CAST(start_utc_ns AS INTEGER) > ? AND CAST(start_utc_ns AS INTEGER) < ?''',
                           (run_metrics.METRICS_VERSION, run.get('robot_id'), modes, saved, start_ns)).fetchone()[0]
        if later >= SHOW_RUNS_AFTER:
            continue
        row = mine[(component, metric)]
        label = '' if component == 'robot' else run_metrics.display_component(component) + ' '
        words = ROBOT_WORDS.get(metric) or METRIC_SIBLINGS.get(metric, (metric.replace('_', ' '),))[0]
        out.append({'repair_id': repair_id, 'component': component, 'metric': metric, 'before': before[0],
                    'after': row['value'], 'description': description,
                    'basis': 'same robot and modes; recorded values only, cause not established',
                    'sentence': f"After the fix “{description}”: {label}{words} "
                                f"{_mfmt(before[0], row['unit'])} → {_mfmt(row['value'], row['unit'])}."})
    return out
