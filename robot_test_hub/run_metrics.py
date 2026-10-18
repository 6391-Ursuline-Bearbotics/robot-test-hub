"""Per-run scalar metrics table (P2.2): one row per (run, component, metric).

Computed by streaming each imported dataset once and assigning observations to
runs by cycle timestamp. Field names follow the AdvantageKit IO templates used by
both the 2026 and 2027 robots; anything a log does not contain is simply absent
(never zero). Values are descriptive measurements of what was recorded, not health
judgments. Rows are keyed by run and METRICS_VERSION, so a new version adds rows
beside the old ones instead of overwriting them.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import re
import time

METRICS_VERSION = "run-metrics-1"
MODULE_POSITIONS = ('front-left', 'front-right', 'back-left', 'back-right')
LOOP_PERIOD_MS = 20.0
OVERRUN_MS = 25.0
LOW_BATTERY_V = 9.0
SWERVE_MOVING_MPS = 0.5
SWERVE_STEERING_MPS = 0.3

TEMP = re.compile(r'^/(?P<component>.+?)/?(?P<motor>[A-Za-z]*)TempCelsius$')
CURRENT = re.compile(r'^/(?P<component>.+?)/?(?P<motor>[A-Za-z]*)CurrentAmps$')
MODULE = re.compile(r'^Drive/Module(\d)/(Drive|Turn)$')
CAN_COUNTERS = {'/SystemStats/CANBus/ReceiveErrorCount': 'can_receive_errors',
                '/SystemStats/CANBus/TransmitErrorCount': 'can_transmit_errors',
                '/SystemStats/CANBus/OffCount': 'can_bus_off_events',
                '/SystemStats/CANBus/TxFullCount': 'can_tx_full_events'}
SWERVE_SETPOINTS = ('/RealOutputs/SwerveStates/SetpointsOptimized',)
SWERVE_MEASURED = ('/RealOutputs/SwerveStates/Measured',)


def install_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS run_metrics (
        run_id TEXT NOT NULL, metrics_version TEXT NOT NULL, component TEXT NOT NULL,
        metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, sample_count INTEGER NOT NULL,
        PRIMARY KEY (run_id, metrics_version, component, metric))""")
    db.execute("""CREATE TABLE IF NOT EXISTS run_metrics_runs (
        run_id TEXT NOT NULL, metrics_version TEXT NOT NULL, robot_id TEXT, start_utc_ns TEXT,
        modes TEXT NOT NULL, duration_seconds REAL, dataset_sha256s TEXT NOT NULL,
        computed_utc_ns INTEGER NOT NULL, PRIMARY KEY (run_id, metrics_version))""")


def has_schema(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_metrics_runs'").fetchone() is not None


def _component_name(match):
    component, motor = match.group('component'), match.group('motor')
    return component + ('/' + motor if motor else '')


def display_component(component):
    """Human label: Drive/Module0/Drive -> Front-left drive motor."""
    match = MODULE.match(component)
    if match:
        position = MODULE_POSITIONS[int(match.group(1))] if int(match.group(1)) < 4 else 'module ' + match.group(1)
        return f"{position.capitalize()} {'drive' if match.group(2) == 'Drive' else 'steering'} motor"
    if re.fullmatch(r'Drive/Module\d', component):
        index = int(component[-1])
        return (MODULE_POSITIONS[index] if index < 4 else 'module ' + str(index)).capitalize() + ' module'
    if component == 'robot':
        return 'Robot'
    component = re.sub(r'(Stator|Torque)$', '', component).rstrip('/')
    return ' '.join(re.sub(r'(?<!^)(?=[A-Z])', ' ', part).lower() for part in component.split('/')).capitalize()


def sibling_group(component):
    """Components that should behave alike: same subsystem path with a different index/side."""
    match = MODULE.match(component)
    if match:
        return 'Drive/Module*/' + match.group(2)
    if re.fullmatch(r'Drive/Module\d', component):
        return 'Drive/Module*'
    for pair in ('Left|Right', 'Top|Bottom', 'Upper|Lower'):
        if re.search(pair, component):
            return re.sub(pair, '<' + pair + '>', component)
    return None


class _Accumulator:
    def __init__(self):
        self.values = defaultdict(list)          # (component, metric) -> list of floats
        self.first, self.last = {}, {}
        self.cycles, self.previous_cycle = 0, None
        self.below = 0.0
        self.battery = None
        self.setpoint, self.measured = None, None
        self.swerve_cycle = None

    def add(self, key, value):
        if type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value):
            self.values[key].append(float(value))

    def cycle(self, timestamp):
        if self.previous_cycle is not None and self.battery is not None and self.battery < LOW_BATTERY_V:
            self.below += (timestamp - self.previous_cycle) / 1e9
        self.previous_cycle = timestamp
        self.cycles += 1
        self._swerve()

    def _swerve(self):
        if self.setpoint is None or self.measured is None:
            return
        for index, (want, have) in enumerate(zip(self.setpoint, self.measured)):
            try:
                speed, actual = float(want['speed']), float(have['speed'])
                angle, actual_angle = float(want['angle']['value']), float(have['angle']['value'])
            except (KeyError, TypeError, ValueError):
                continue
            component = f'Drive/Module{index}'
            if abs(speed) > SWERVE_MOVING_MPS:
                self.add((component, 'drive_speed_error_mps'), abs(abs(actual) - abs(speed)))
            if abs(speed) > SWERVE_STEERING_MPS:
                error = (angle - actual_angle + math.pi) % (2 * math.pi) - math.pi
                # A module may legally run reversed (optimized), which is a 180 degree flip.
                error = min(abs(error), abs(abs(error) - math.pi))
                self.add((component, 'steering_error_deg'), math.degrees(error))
        self.setpoint = self.measured = None

    def observe(self, row):
        field, value = row['field'], row.get('value')
        if row.get('validity') != 'valid':
            return
        # Motor temperatures/currents come from IO inputs; output copies would double count.
        is_input = row.get('category') == 'input'
        if field == '/SystemStats/BatteryVoltage':
            self.battery = value if type(value) in (int, float) else self.battery
            self.add(('robot', 'battery_voltage'), value)
        elif field == '/SystemStats/BrownedOut':
            if value is True:
                self.add(('robot', 'brownout_records'), 1)
        elif field == '/RealOutputs/LoggedRobot/FullCycleMS':
            self.add(('robot', 'loop_ms'), value)
        elif field == '/SystemStats/CANBus/Utilization':
            self.add(('robot', 'can_utilization'), value)
        elif field in CAN_COUNTERS and type(value) is int:
            self.first.setdefault(('robot', CAN_COUNTERS[field]), value)
            self.last[('robot', CAN_COUNTERS[field])] = value
        elif field in SWERVE_SETPOINTS and isinstance(value, list):
            self.setpoint = value
        elif field in SWERVE_MEASURED and isinstance(value, list):
            self.measured = value
        elif field == '/SystemStats/CPUTempCelsius':
            self.add(('robot', 'cpu_temp'), value)
        elif field.endswith('TempCelsius') and is_input:
            match = TEMP.match(field)
            if match and type(value) in (int, float) and math.isfinite(value) and value > 0:
                key = (_component_name(match), 'temp')
                self.first.setdefault(key, value)
                self.last[key] = value
                self.add(key, value)
        elif field.endswith('CurrentAmps') and is_input:
            match = CURRENT.match(field)
            if match and type(value) in (int, float):
                self.add((_component_name(match), 'current'), abs(value))

    def metrics(self):
        out = []

        def put(component, metric, value, unit, count):
            if value is not None and math.isfinite(value):
                out.append((component, metric, float(value), unit, count))
        for (component, kind), values in self.values.items():
            if not values:
                continue
            n = len(values)
            if kind == 'battery_voltage':
                put(component, 'battery_min_v', min(values), 'V', n)
                put(component, 'battery_time_below_9v_s', self.below, 's', n)
            elif kind == 'brownout_records':
                put(component, 'brownout_records', n, 'count', n)
            elif kind == 'loop_ms':
                put(component, 'loop_worst_ms', max(values), 'ms', n)
                put(component, 'loop_overruns', sum(v > OVERRUN_MS for v in values), 'count', n)
            elif kind == 'can_utilization':
                scale = 100 if max(values) <= 1.0 else 1
                put(component, 'can_utilization_max_pct', max(values) * scale, '%', n)
                put(component, 'can_utilization_mean_pct', sum(values) / n * scale, '%', n)
            elif kind == 'cpu_temp':
                put(component, 'cpu_temp_max_c', max(values), '°C', n)
            elif kind == 'temp':
                put(component, 'temp_max_c', max(values), '°C', n)
                put(component, 'temp_rise_c', self.last[(component, kind)] - self.first[(component, kind)], '°C', n)
            elif kind == 'current':
                put(component, 'current_mean_a', sum(values) / n, 'A', n)
                put(component, 'current_max_a', max(values), 'A', n)
            elif kind == 'drive_speed_error_mps':
                put(component, 'drive_speed_error_mps', sum(values) / n, 'm/s', n)
            elif kind == 'steering_error_deg':
                put(component, 'steering_error_deg', sum(values) / n, '°', n)
        for key, first in self.first.items():
            if key[1].startswith('can_') and key in self.last:
                put(key[0], key[1], self.last[key] - first, 'count', 2)
        return out


def compute(importer, job_ids, runs):
    """Stream each dataset once; return {run_id: [(component, metric, value, unit, count)]}."""
    windows = defaultdict(list)
    for run in runs:
        for job_id in run['segment_ids']:
            windows[job_id].append((int(run['start_monotonic_ns']), int(run['end_monotonic_ns']), run['run_id']))
    accumulators = {run['run_id']: _Accumulator() for run in runs}
    for job_id in job_ids:
        spans = sorted(windows.get(job_id, []))
        if not spans:
            continue
        cache = {}

        def target(t):
            if t not in cache:
                cache.clear()
                cache[t] = next((accumulators[r] for s, e, r in spans if s <= t <= e), None)
            return cache[t]
        # A cycle's observations precede its cycle row, so route each row by its own cycle time.
        for row in importer.iter_dataset(job_id):
            kind = row.get('kind')
            if kind == 'cycle':
                acc = target(int(row['timestamp_ns']))
                if acc is not None:
                    acc.cycle(int(row['timestamp_ns']))
            elif kind == 'observation' and row.get('cycle_timestamp_ns') is not None:
                acc = target(int(row['cycle_timestamp_ns']))
                if acc is not None:
                    acc.observe(row)
    return {run_id: acc.metrics() for run_id, acc in accumulators.items()}


def _start_utc(run):
    starts = [int(i['utc_start_ns']) for i in run.get('utc_intervals') or [] if i.get('utc_start_ns')]
    return str(min(starts)) if starts else None


def step(root, db, document, *, max_jobs=1):
    """Compute metrics for runs that lack them, one dataset (job) per call."""
    from .importer import Importer
    with db:
        install_schema(db)
    done = {row[0] for row in db.execute('SELECT run_id FROM run_metrics_runs WHERE metrics_version=?', (METRICS_VERSION,))}
    pending = [run for run in document['runs'] if run['run_id'] not in done]
    if not pending:
        return 0
    importer = Importer(root, db)
    jobs = sorted({job for run in pending for job in run['segment_ids']})[:max_jobs]
    runs = [run for run in pending if set(run['segment_ids']) <= set(jobs)]
    if not runs:
        # Multi-segment runs: include every segment they need.
        runs = [pending[0]]
        jobs = sorted(set(pending[0]['segment_ids']))
    results = compute(importer, jobs, runs)
    digests = {job: importer.get_job(job)['dataset_sha256'] for job in jobs}
    now = time.time_ns()
    with db:
        for run in runs:
            for component, metric, value, unit, count in results.get(run['run_id'], []):
                db.execute('INSERT OR REPLACE INTO run_metrics VALUES (?,?,?,?,?,?,?)',
                           (run['run_id'], METRICS_VERSION, component, metric, value, unit, count))
            modes = sorted({p['mode'] for p in run.get('phases') or []})
            duration = (int(run['end_monotonic_ns']) - int(run['start_monotonic_ns'])) / 1e9
            db.execute('INSERT OR REPLACE INTO run_metrics_runs VALUES (?,?,?,?,?,?,?,?)',
                       (run['run_id'], METRICS_VERSION, run.get('robot_id'), _start_utc(run), json.dumps(modes), duration,
                        json.dumps(sorted(digests[j] for j in run['segment_ids'])), now))
    return len(runs)


def for_run(db, run_id):
    if not has_schema(db):
        return None
    if db.execute('SELECT 1 FROM run_metrics_runs WHERE run_id=? AND metrics_version=?', (run_id, METRICS_VERSION)).fetchone() is None:
        return None
    return [dict(component=c, metric=m, value=v, unit=u, sample_count=n) for c, m, v, u, n in db.execute(
        'SELECT component,metric,value,unit,sample_count FROM run_metrics WHERE run_id=? AND metrics_version=? ORDER BY component,metric',
        (run_id, METRICS_VERSION))]


def history(db, robot_id, before_utc_ns, modes, metric_keys, *, limit=20, duration_seconds=None):
    """Values of each (component, metric) over the previous comparable runs.

    Comparable means the same robot, the same modes and, when given, a length
    between half and twice this run's length (a 4 s auto is not a 20 s auto).
    """
    low, high = (duration_seconds / 2, duration_seconds * 2) if duration_seconds else (0, 1e18)
    rows = db.execute('''SELECT run_id FROM run_metrics_runs WHERE metrics_version=? AND robot_id IS ? AND modes=?
        AND start_utc_ns IS NOT NULL AND CAST(start_utc_ns AS INTEGER) < ? AND duration_seconds BETWEEN ? AND ?
        ORDER BY CAST(start_utc_ns AS INTEGER) DESC LIMIT ?''',
                      (METRICS_VERSION, robot_id, modes, int(before_utc_ns), low, high, limit)).fetchall()
    ids = [row[0] for row in rows]
    result = defaultdict(list)
    if not ids:
        return result
    marks = ','.join('?' * len(ids))
    for run_id, component, metric, value in db.execute(
            f'SELECT run_id,component,metric,value FROM run_metrics WHERE metrics_version=? AND run_id IN ({marks})',
            [METRICS_VERSION] + ids):
        if (component, metric) in metric_keys:
            result[(component, metric)].append(value)
    return result
