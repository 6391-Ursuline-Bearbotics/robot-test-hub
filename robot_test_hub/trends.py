"""Trend page data (P2.4): each measurement over recent comparable runs.

Series are split by robot and run modes (auto runs compare with auto runs). Each
point is checked against the runs before it with the same rule as the home page
(practice.trend_findings), so a highlighted point here is exactly a point that
was called "Unusual compared with recent runs" there. A run without the
measurement is a gap, never zero.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from . import practice, run_metrics

MAX_RUNS = 60


def series(root, *, component=None, limit=MAX_RUNS):
    path = Path(root).resolve() / 'catalog.sqlite3'
    if not path.exists():
        return {'groups': [], 'components': []}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        if not run_metrics.has_schema(db):
            return {'groups': [], 'components': []}
        runs = db.execute('''SELECT run_id, robot_id, modes, start_utc_ns, duration_seconds FROM run_metrics_runs
            WHERE metrics_version=? AND start_utc_ns IS NOT NULL ORDER BY CAST(start_utc_ns AS INTEGER) DESC LIMIT ?''',
                          (run_metrics.METRICS_VERSION, limit)).fetchall()
        runs = list(reversed(runs))
        ids = [r[0] for r in runs]
        values = {}
        if ids:
            marks = ','.join('?' * len(ids))
            for run_id, comp, metric, value, unit in db.execute(
                    f'SELECT run_id, component, metric, value, unit FROM run_metrics WHERE metrics_version=? AND run_id IN ({marks})',
                    [run_metrics.METRICS_VERSION] + ids):
                values[(run_id, comp, metric)] = (value, unit)
    components = sorted({c for _, c, _ in values})
    groups = {}
    for run_id, robot, modes, start, seconds in runs:
        key = (robot, modes)
        groups.setdefault(key, []).append((run_id, int(start), seconds))
    out = []
    for (robot, modes), members in groups.items():
        names = [practice.MODE_WORDS.get(m, m.title()) for m in json.loads(modes)]
        metrics = sorted({(c, m) for (r, c, m) in values if any(r == run for run, _, _ in members)
                          and m in practice.TREND_METRICS and (component is None or c == component)})
        for comp, metric in metrics:
            points, unit = [], None
            for index, (run_id, start, seconds) in enumerate(members):
                found = values.get((run_id, comp, metric))
                if found is None:
                    points.append({'run_id': run_id, 'start_utc_ns': str(start), 'value': None, 'unusual': False})
                    continue
                value, unit = found
                past = [values[(r, comp, metric)][0] for r, s, d in members[max(0, index - practice.TREND_WINDOW):index]
                        if (r, comp, metric) in values and seconds and d and seconds / 2 <= d <= seconds * 2]
                unusual = bool(seconds and seconds >= practice.TREND_MIN_SECONDS and practice.trend_findings(
                    [{'component': comp, 'metric': metric, 'value': value, 'unit': unit}], {(comp, metric): past}))
                points.append({'run_id': run_id, 'start_utc_ns': str(start), 'value': value, 'unusual': unusual})
            if sum(p['value'] is not None for p in points) < 2:
                continue
            out.append({'robot': robot, 'modes': names, 'component': comp,
                        'label': run_metrics.display_component(comp), 'metric': metric,
                        'metric_label': practice.ROBOT_WORDS.get(metric) or practice.METRIC_SIBLINGS.get(metric, (metric.replace('_', ' '),))[0],
                        'unit': unit, 'points': points, 'unusual_count': sum(p['unusual'] for p in points)})
    out.sort(key=lambda g: (-g['unusual_count'], g['label'], g['metric'], g['modes']))
    return {'groups': out, 'components': components,
            'basis': 'same robot and modes; each point compared with the runs before it, as on the home page'}
