"""Student-facing "Today's practice" projection: one card per run, plain words.

Read-only. Built from the run catalog and the latest immutable report per run.
Every sentence here is a summary of an existing check result or a descriptive
sibling comparison; precise caveats stay in the reports and the /admin pages.
"Nothing flagged" means the checks that could run found nothing, not that the
robot is healthy. Missing data is reported as "Couldn't check", never as OK.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sqlite3
import statistics

PAGE = 200
MAX_RUNS = 5000
SIBLING_RATIO = 2.0

# metric name -> (plain label, display unit, display scale, minimum absolute difference in metric units)
SIBLING_METRICS = {
    'recorded_settling_max_seconds': ('steering took {v} to settle', 's', 1, 0.10),
    'recorded_steering_rmse_rad': ('steering was off target by {v} on average', '°', 180 / math.pi, math.radians(2)),
    'recorded_oscillation_crossings': ('steering overshot {v} times', '', 1, 3),
    'steering_rmse_rad': ('steering was off target by {v} on average', '°', 180 / math.pi, math.radians(2)),
    'drive_rmse_mps': ('wheel speed was off by {v} on average', ' m/s', 1, 0.10),
    'longest_sustained_drive_error_seconds': ('wheel speed stayed off target for {v}', 's', 1, 0.25),
}
POSITION_WORDS = {'front-left': 'Front-left', 'front-right': 'Front-right',
                  'back-left': 'Back-left', 'back-right': 'Back-right'}
MODE_WORDS = {'AUTONOMOUS': 'Auto', 'TELEOP': 'Teleop', 'TELEOPERATED': 'Teleop', 'TEST': 'Test', 'UTILITY': 'Test'}


def _where(finding):
    position = finding.get('module_position')
    return POSITION_WORDS.get(position, position.replace('-', ' ').capitalize()) + ' ' if isinstance(position, str) and position else ''


def _cap(text):
    return text[:1].upper() + text[1:]


def _times(count):
    return 'once' if count == 1 else f'{count} times'


def finding_sentence(finding):
    """One plain-language sentence for an analyzer finding."""
    kind, where = finding.get('kind') or '', _where(finding)
    count = finding.get('count')
    if kind == 'recording_gap':
        return f"The log has {count} gap(s) where nothing was recorded."
    if kind == 'invalid_observations':
        return f"{count} logged value(s) were invalid (for example not-a-number)."
    if kind == 'stale_observations':
        return f"{count} logged value(s) stopped updating."
    if kind == 'disconnected_observations':
        return f"A device reported it was disconnected {_times(count)} during this run."
    if kind == 'invalid_cycle_timestamps':
        return f"The log's clock jumped or went backwards {_times(count)}."
    if kind == 'sustained_drive_tracking_error':
        return _cap(f"{where}wheel speed did not match the command for long stretches.")
    if kind == 'steering_tracking_error':
        return _cap(f"{where}steering stayed further off target than the limit allows.")
    if kind == 'tracking_regression_against_approved_cohort':
        return _cap(f"{where}tracking was worse than in the approved good runs.")
    if kind.startswith('recorded_phoenix_'):
        n = finding.get('observed_false_status_records') or 1
        return _cap(f"{where}motor controller reported a status error on {finding.get('signal', 'a signal')} {_times(n)}.")
    if kind == 'recorded_system_fault_true':
        return f"The robot controller reported a {finding.get('signal', 'system')} fault."
    if kind.startswith('recorded_system_fault_counter_'):
        return f"The robot controller's {finding.get('signal', 'fault')} counter went up during this run."
    summary = finding.get('observed_summary') or {}
    if kind == 'recorded_steering_settling_limit':
        seconds = summary.get('maximum_recorded_settling_seconds')
        detail = f" (slowest {seconds:.2f} s)" if isinstance(seconds, (int, float)) else ''
        return _cap(f"{where}steering was slow to settle{detail}.")
    if kind == 'recorded_steering_error_crossings':
        return _cap(f"{where}steering wobbled back and forth around its target.")
    if kind == 'recorded_steering_angle_jump':
        return _cap(f"{where}steering angle jumped suddenly.")
    if kind == 'recorded_steering_sustained_error':
        return _cap(f"{where}steering stayed off target for a while.")
    text = finding.get('observed_behavior') or kind.replace('_', ' ') or 'Something was flagged'
    return text[0].upper() + text[1:].split(';')[0].rstrip('.') + '.'


def _fmt(value, unit, scale):
    shown = value * scale
    text = f"{shown:.0f}" if unit in ('', '°') and abs(shown) >= 10 else f"{shown:.2f}" if abs(shown) < 10 else f"{shown:.1f}"
    return text + unit


def sibling_findings(metrics, *, ratio=SIBLING_RATIO):
    """Flag a module whose metric is far from its siblings' median (descriptive, no baseline)."""
    groups = {}
    for item in metrics:
        name, value, position = item.get('name'), item.get('value'), item.get('module_position')
        if name in SIBLING_METRICS and isinstance(position, str) and type(value) in (int, float) and math.isfinite(value):
            groups.setdefault(name, {})[position] = value
    results = []
    for name, values in groups.items():
        if len(values) < 3:
            continue
        label, unit, scale, floor = SIBLING_METRICS[name]
        for position, value in values.items():
            others = [v for p, v in values.items() if p != position]
            median = statistics.median(others)
            if value - median >= floor and value >= ratio * max(median, 1e-9):
                low, high = min(others), max(others)
                spread = _fmt(low, unit, scale) if low == high else f"{_fmt(low, unit, scale)}–{_fmt(high, unit, scale)}"
                sentence = (POSITION_WORDS.get(position, position) + ' ' + label.format(v=_fmt(value, unit, scale))
                            + f"; the other {len(others)} were {spread}.")
                results.append({'kind': 'sibling_outlier', 'metric': name, 'module_position': position, 'value': value,
                                'sibling_values': others, 'ratio_to_median': value / median if median else None,
                                'basis': 'descriptive comparison with sibling modules in the same run; not a health result',
                                'sentence': sentence, 'margin': (value - median) / floor})
    return sorted(results, key=lambda r: -r['margin'])


def card_status(report, siblings):
    """(chip, plain line, finding sentences) from the latest report for a run."""
    if report is None:
        return 'unchecked', "Checks haven't run on this run yet.", []
    sentences = []
    for check in report.get('checks', []):
        for finding in check.get('findings', []):
            sentences.append(finding_sentence(finding))
    sentences += [s['sentence'] for s in siblings]
    if sentences:
        more = f" (+{len(sentences) - 1} more)" if len(sentences) > 1 else ''
        return 'look', sentences[0] + more, sentences
    evaluated = sum(c.get('outcome') == 'evaluated_no_finding' for c in report.get('checks', []))
    if evaluated:
        total = len(report.get('checks', []))
        return 'ok', f"Nothing flagged by the {evaluated} check(s) that could run" + (f" ({total - evaluated} couldn't)." if total > evaluated else '.'), []
    return 'unchecked', "The log didn't have the data these checks need.", []


def _metric(report, name):
    for check in (report or {}).get('checks', []):
        for item in check.get('metrics', []):
            if item.get('name') == name and type(item.get('value')) in (int, float):
                return item['value']
    return None


def _all_metrics(report):
    return [item for check in (report or {}).get('checks', []) for item in check.get('metrics', [])]


def _local(ns, tz):
    return datetime.fromtimestamp(int(ns) / 1e9, tz) if ns is not None else None


def _start_utc_ns(run):
    starts = [int(i['utc_start_ns']) for i in run.get('utc_intervals') or [] if i.get('utc_start_ns') is not None]
    return min(starts) if starts else None


def _duration_seconds(run):
    try:
        return (int(run['end_monotonic_ns']) - int(run['start_monotonic_ns'])) / 1e9
    except (KeyError, TypeError, ValueError):
        return None


def _latest_report(db, run_id):
    from .report_api import RUN_FILTER
    row = db.execute('SELECT result_json FROM analysis_reports WHERE ' + RUN_FILTER +
                     ' ORDER BY CAST(created_utc_ns AS INTEGER) DESC, report_id DESC LIMIT 1', (run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _note_count(db, run_id):
    from . import run_notes
    try:
        result = run_notes.page(db, run_id, {})
        return result.get('total')
    except Exception:
        return None


def _all_runs(db):
    from .runs import RunCatalog
    catalog, offset, runs = RunCatalog(db), 0, []
    while offset is not None and len(runs) < MAX_RUNS:
        page = catalog.search(offset=offset, limit=PAGE)
        if page.get('availability', 'available') != 'available':
            raise RuntimeError(page.get('reason') or 'run_lookup_unavailable')
        runs += page['items']
        offset = page['next_offset']
    return runs


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def day(root, date=None, *, tz=None):
    """Cards for one local calendar day (default: most recent day with runs)."""
    tz = tz or datetime.now().astimezone().tzinfo
    path = Path(root).resolve() / 'catalog.sqlite3'
    if not path.exists():
        return {'schema_version': 1, 'date': date, 'days': [], 'cards': [], 'unknown_time_runs': 0}
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        runs = _all_runs(db) if _has_table(db, 'run_lookup_runs') else []
        dated, unknown = {}, 0
        for run in runs:
            start = _start_utc_ns(run)
            if start is None:
                unknown += 1
                continue
            dated.setdefault(_local(start, tz).date().isoformat(), []).append((start, run))
        days = sorted(dated, reverse=True)
        selected = date if date is not None else (days[0] if days else None)
        cards = []
        has_reports = _has_table(db, 'analysis_reports')
        for start, run in sorted(dated.get(selected, []), key=lambda pair: pair[0]):
            report = _latest_report(db, run['run_id']) if has_reports else None
            siblings = sibling_findings(_all_metrics(report))
            chip, line, sentences = card_status(report, siblings)
            modes = []
            for phase in run.get('phases') or []:
                word = MODE_WORDS.get(phase.get('mode'), (phase.get('mode') or 'Unknown').title())
                if not modes or modes[-1] != word:
                    modes.append(word)
            battery = _metric(report, 'recorded_battery_voltage_min')
            cards.append({'run_id': run['run_id'], 'local_start': _local(start, tz).isoformat(timespec='seconds'),
                          'duration_seconds': _duration_seconds(run), 'modes': modes,
                          'battery_min_volts': battery, 'notes': _note_count(db, run['run_id']),
                          'status': chip, 'headline': line, 'findings': sentences, 'siblings': siblings,
                          'source_types': run.get('source_types') or [],
                          'complete': run.get('completeness') == 'complete'})
    index = days.index(selected) if selected in days else None
    return {'schema_version': 1, 'date': selected, 'days': days[:60],
            'previous_day': days[index + 1] if index is not None and index + 1 < len(days) else None,
            'next_day': days[index - 1] if index else None,
            'cards': cards, 'unknown_time_runs': unknown,
            'counts': {k: sum(c['status'] == k for c in cards) for k in ('ok', 'look', 'unchecked')}}
