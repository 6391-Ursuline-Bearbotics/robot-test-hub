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

from . import run_metrics

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


# Measured-metric comparisons (run_metrics table). metric -> (words, direction, ratio, floor)
# direction: 'high' flags only larger values; 'both' also flags a part doing much less.
METRIC_SIBLINGS = {
    'current_mean_a': ('average current', 'both', 2.0, 3.0),
    'drive_speed_error_mps': ('wheel speed off target by', 'high', 1.5, 0.3),
    'steering_error_deg': ('steering off target by', 'high', 2.0, 3.0),
    'temp_rise_c': ('heated up by', 'high', 2.0, 5.0),
}
METRIC_UNITS = {'A': (' A', 1), 'm/s': (' m/s', 1), '°': ('°', 1), '°C': (' °C', 1), 'V': (' V', 1),
                's': (' s', 1), 'ms': (' ms', 1), '%': ('%', 1), 'count': ('', 1)}
# Robot-wide measurements that are worth a look on their own. Thresholds are plain
# engineering rules of thumb stated in the sentence, not approved health limits.
ROBOT_RULES = (
    ('battery_min_v', lambda v: v < 7.0, "Battery dropped to {v:.1f} V (below 7 V the robot is close to browning out)."),
    ('battery_time_below_9v_s', lambda v: v >= 3.0, "Battery was below 9 V for {v:.0f} s."),
    ('brownout_records', lambda v: v > 0, "The robot browned out (the controller cut power to motors)."),
    ('loop_overruns', lambda v: v >= 25, "Robot code ran slow: {v:.0f} loops took longer than 25 ms."),
    ('can_utilization_max_pct', lambda v: v >= 95, "The CAN bus was {v:.0f}% busy at its peak."),
)
TREND_MIN_RUNS = 5
TREND_MIN_SECONDS = 10.0
# Only the worse direction is unusual: lower is worse for the battery low point.
LOWER_IS_WORSE = {'battery_min_v'}
ROBOT_WORDS = {'battery_min_v': 'battery low point', 'battery_time_below_9v_s': 'time with battery below 9 V',
               'loop_worst_ms': 'slowest loop', 'loop_overruns': 'number of slow loops',
               'can_utilization_mean_pct': 'average CAN bus load'}
# A robot-wide rule that fires in this share of a day's runs is shown once for the day.
COMMON_SHARE = 0.6
COMMON_MIN_RUNS = 3
TREND_WINDOW = 20
TREND_K = 3.5
TREND_METRICS = {'battery_min_v': 0.5, 'battery_time_below_9v_s': 3.0, 'loop_worst_ms': 20.0, 'loop_overruns': 10,
                 'can_utilization_mean_pct': 5.0, 'temp_rise_c': 5.0, 'current_mean_a': 3.0,
                 'drive_speed_error_mps': 0.3, 'steering_error_deg': 3.0}


def _digits(value):
    return 0 if abs(value) >= 20 else 1 if abs(value) >= 1 else 2


def _mfmt(value, unit, digits=None):
    suffix, _ = METRIC_UNITS.get(unit, (' ' + unit, 1))
    return f"{value:.{_digits(value) if digits is None else digits}f}" + suffix


def _span(values, unit):
    low, high = min(values), max(values)
    digits = _digits(max(abs(low), abs(high)))
    if _mfmt(low, unit, digits) == _mfmt(high, unit, digits):
        return _mfmt(low, unit, digits)
    return f"{_mfmt(low, unit, digits).split(' ')[0].rstrip('%°')}–{_mfmt(high, unit, digits)}"


def _comparable(component):
    """Supply current mirrors stator current for the same motor; compare one of them."""
    return not component.endswith('Supply')


def measured_findings(rows):
    """Sibling and robot-wide findings from one run's run_metrics rows."""
    from .run_metrics import display_component, sibling_group
    found = []
    groups = {}
    for row in rows:
        if not _comparable(row['component']):
            continue
        if row['metric'] in METRIC_SIBLINGS:
            group = sibling_group(row['component'])
            if group:
                groups.setdefault((group, row['metric']), {})[row['component']] = row
    for (group, metric), members in groups.items():
        if len(members) < 2:
            continue
        words, direction, ratio, floor = METRIC_SIBLINGS[metric]
        for component, row in members.items():
            others = [r['value'] for c, r in members.items() if c != component]
            # With three or more parts, compare with the median of all of them so one
            # broken part cannot make a healthy sibling look like the outlier.
            median = statistics.median(others if len(members) == 2 else [r['value'] for r in members.values()])
            value, unit = row['value'], row['unit']
            if metric == 'current_mean_a' and max(value, median) < floor:
                continue
            high = value - median >= floor and value >= ratio * max(median, 1e-9)
            low = direction == 'both' and median - value >= floor and median >= ratio * max(value, 1e-9)
            if len(members) == 2 and (low or (high and component > min(members))):
                continue  # a pair is reported once, from the larger side
            if high or low:
                other = 'the other' if len(others) == 1 else f'the other {len(others)}'
                found.append({'kind': 'sibling_outlier', 'component': component, 'metric': metric, 'value': value,
                              'sibling_values': others, 'direction': 'high' if high else 'low',
                              'basis': 'descriptive comparison with matching parts in the same run; not a health result',
                              'sentence': f"{display_component(component)}: {words} {_mfmt(value, unit)}; {other}: {_span(others, unit)}.",
                              'margin': abs(value - median) / floor})
    robot = {row['metric']: row['value'] for row in rows if row['component'] == 'robot'}
    for metric, rule, text in ROBOT_RULES:
        if metric in robot and rule(robot[metric]):
            sentence = text.format(v=robot[metric])
            if metric == 'loop_overruns' and 'loop_worst_ms' in robot:
                sentence = sentence[:-1] + f" (slowest {robot['loop_worst_ms']:.0f} ms)."
            if metric == 'can_utilization_max_pct' and 'can_utilization_mean_pct' in robot:
                sentence = sentence[:-1] + f" (average {robot['can_utilization_mean_pct']:.0f}%)."
            found.append({'kind': 'robot_measurement', 'component': 'robot', 'metric': metric, 'value': robot[metric],
                          'basis': 'stated rule of thumb on a recorded measurement; not an approved health limit',
                          'sentence': sentence, 'margin': 1.0})
    return sorted(found, key=lambda f: -f['margin'])


def trend_findings(rows, history):
    """Flag metrics far outside the recent comparable runs (median +/- k * scaled MAD)."""
    from .run_metrics import display_component
    found = []
    for row in rows:
        floor = TREND_METRICS.get(row['metric'])
        past = history.get((row['component'], row['metric']), [])
        if floor is None or len(past) < TREND_MIN_RUNS or not _comparable(row['component']):
            continue
        median = statistics.median(past)
        mad = statistics.median(abs(v - median) for v in past) * 1.4826
        distance = (median - row['value']) if row['metric'] in LOWER_IS_WORSE else (row['value'] - median)
        ordered = sorted(past)
        q1, q3 = ordered[len(ordered) // 4], ordered[(3 * len(ordered)) // 4]
        outside = row['value'] < q1 if row['metric'] in LOWER_IS_WORSE else row['value'] > q3
        if distance >= floor and distance > TREND_K * mad and outside:
            if row['component'] == 'robot':
                label, words = '', ROBOT_WORDS.get(row['metric'], row['metric'].replace('_', ' '))
            else:
                label = display_component(row['component']) + ' '
                words = METRIC_SIBLINGS.get(row['metric'], (row['metric'].replace('_', ' '),))[0]
            found.append({'kind': 'trend_outlier', 'component': row['component'], 'metric': row['metric'],
                          'value': row['value'], 'recent_values': past, 'recent_median': median,
                          'basis': f'compared with the last {len(past)} runs with the same robot and modes; descriptive, not a health result',
                          'sentence': (f"Unusual compared with recent runs: {label}{words} {_mfmt(row['value'], row['unit'])} "
                                       f"(usually {_span([q1, q3], row['unit'])})."),
                          'margin': distance / max(floor, 1e-9)})
    return sorted(found, key=lambda f: -f['margin'])


def _short_run(db, run, start, seconds):
    """A run that ended far earlier than this robot's usual runs in the same mode."""
    if not run_metrics.has_schema(db):
        return []
    modes = json.dumps(sorted({p['mode'] for p in run.get('phases') or []}))
    past = [row[0] for row in db.execute('''SELECT duration_seconds FROM run_metrics_runs WHERE metrics_version=?
        AND robot_id IS ? AND modes=? AND start_utc_ns IS NOT NULL AND CAST(start_utc_ns AS INTEGER) < ?
        AND duration_seconds IS NOT NULL ORDER BY CAST(start_utc_ns AS INTEGER) DESC LIMIT ?''',
        (run_metrics.METRICS_VERSION, run.get('robot_id'), modes, int(start), TREND_WINDOW))]
    if len(past) < 3:
        return []
    usual = statistics.median(past)
    if seconds >= usual / 2 or usual - seconds < 5:
        return []
    mode = ' and '.join(MODE_WORDS.get(m, m.title()) for m in json.loads(modes)) or 'This run'
    return [{'kind': 'trend_outlier', 'component': 'robot', 'metric': 'duration_seconds', 'value': seconds,
             'recent_median': usual, 'basis': 'run length compared with recent runs in the same mode',
             'sentence': f"{mode} ended after {seconds:.0f} s; it usually lasts {usual:.0f} s. Was the robot disabled early?",
             'margin': usual / max(seconds, 1)}]


COMMON_WORDS = {
    'battery_min_v': 'Battery dropped below 7 V',
    'battery_time_below_9v_s': 'Battery spent 3 s or more below 9 V',
    'brownout_records': 'The robot browned out',
    'loop_overruns': 'Robot code ran slow (25+ loops over 25 ms)',
    'can_utilization_max_pct': 'The CAN bus hit 95%+ busy',
}


def _common_robot_findings(measured_by_run):
    """Robot-wide rules that fired in most of the day's runs: {metric: run count}."""
    counts = {}
    for measured in measured_by_run:
        for metric in {f['metric'] for f in measured if f['kind'] == 'robot_measurement'}:
            counts[metric] = counts.get(metric, 0) + 1
    runs = len(measured_by_run)
    return {m: c for m, c in counts.items() if runs >= COMMON_MIN_RUNS and c >= COMMON_SHARE * runs}


def card_status(report, siblings, measured=None, trends=None, metrics_available=False):
    """(chip, plain line, finding sentences) from the latest report and measured metrics for a run."""
    sentences = []
    for check in (report or {}).get('checks', []):
        for finding in check.get('findings', []):
            sentences.append(finding_sentence(finding))
    sentences = [f['sentence'] for f in (measured or [])] + sentences + [s['sentence'] for s in siblings]
    sentences += [f['sentence'] for f in (trends or [])]
    if sentences:
        more = f" (+{len(sentences) - 1} more)" if len(sentences) > 1 else ''
        return 'look', sentences[0] + more, sentences
    evaluated = sum(c.get('outcome') == 'evaluated_no_finding' for c in (report or {}).get('checks', []))
    if metrics_available:
        return 'ok', "Nothing stood out in the recorded measurements" + (f" or the {evaluated} check(s) that could run." if evaluated else "."), []
    if report is None:
        return 'unchecked', "Checks haven't run on this run yet.", []
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
        cards, pending = [], []
        has_reports = _has_table(db, 'analysis_reports')
        for start, run in sorted(dated.get(selected, []), key=lambda pair: pair[0]):
            report = _latest_report(db, run['run_id']) if has_reports else None
            siblings = sibling_findings(_all_metrics(report))
            rows = run_metrics.for_run(db, run['run_id'])
            measured = measured_findings(rows) if rows else []
            trends = []
            seconds = _duration_seconds(run)
            if rows and seconds and seconds >= TREND_MIN_SECONDS:
                modes_key = json.dumps(sorted({p['mode'] for p in run.get('phases') or []}))
                history = run_metrics.history(db, run.get('robot_id'), start, modes_key,
                                              {(r['component'], r['metric']) for r in rows}, limit=TREND_WINDOW,
                                              duration_seconds=seconds)
                trends = trend_findings(rows, history)
            if seconds is not None:
                trends = _short_run(db, run, start, seconds) + trends
            pending.append((run, start, report, siblings, measured, trends, rows))
        common = _common_robot_findings([item[4] for item in pending])
        for run, start, report, siblings, measured, trends, rows in pending:
            measured = [f for f in measured if not (f['kind'] == 'robot_measurement' and f['metric'] in common)]
            chip, line, sentences = card_status(report, siblings, measured, trends, metrics_available=bool(rows))
            modes = []
            for phase in run.get('phases') or []:
                word = MODE_WORDS.get(phase.get('mode'), (phase.get('mode') or 'Unknown').title())
                if not modes or modes[-1] != word:
                    modes.append(word)
            battery = _metric(report, 'recorded_battery_voltage_min')
            if battery is None and rows:
                battery = next((r['value'] for r in rows if r['component'] == 'robot' and r['metric'] == 'battery_min_v'), None)
            cards.append({'run_id': run['run_id'], 'local_start': _local(start, tz).isoformat(timespec='seconds'),
                          'duration_seconds': _duration_seconds(run), 'modes': modes,
                          'battery_min_volts': battery, 'notes': _note_count(db, run['run_id']),
                          'status': chip, 'headline': line, 'findings': sentences,
                          'siblings': siblings + [f for f in measured if f['kind'] == 'sibling_outlier'], 'trends': trends,
                          'source_types': run.get('source_types') or [],
                          'complete': run.get('completeness') == 'complete'})
    index = days.index(selected) if selected in days else None
    return {'schema_version': 1, 'date': selected, 'days': days[:60],
            'previous_day': days[index + 1] if index is not None and index + 1 < len(days) else None,
            'next_day': days[index - 1] if index else None,
            'cards': cards, 'unknown_time_runs': unknown,
            'seen_all_day': [{'metric': metric, 'runs': count, 'of': len(cards), 'sentence': COMMON_WORDS.get(metric, metric)}
                             for metric, count in sorted(common.items(), key=lambda kv: -kv[1])] if cards else [],
            'counts': {k: sum(c['status'] == k for c in cards) for k in ('ok', 'look', 'unchecked')}}
