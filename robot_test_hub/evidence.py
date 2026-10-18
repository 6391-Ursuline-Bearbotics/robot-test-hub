"""Per-run "Get the data" bundle (P1.3): original log + AdvantageScope layout + notes.

The zip holds the unchanged original WPILOG(s) of the run, an AdvantageScope
layout JSON with line graphs for the fields behind the run's findings, and a
README naming the time range to look at. The layout follows the structure of a
layout saved by AdvantageScope 26 (hubs -> state -> tabs). Opening it and seeking
to the run are manual steps: automatic seeking is not claimed.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile

from .wpilog import records, _control

COLORS = ('#2b66a2', '#e5b31b', '#af2437', '#3f9e5b', '#80588e', '#d27c2c', '#4fb3bf', '#c9c9c9')
LAYOUT_VERSION = '26.0.0'


class EvidenceError(ValueError):
    def __init__(self, code, status=404):
        super().__init__(code)
        self.code, self.status = code, status


def _fields(paths):
    """Field name -> WPILOG type, from control records only (payloads are skipped)."""
    found = {}
    for path in paths:
        active, generations = {}, {}
        with open(path, 'rb') as stream:
            for record in records(stream, truncated_tail={}):
                if record.entry == 0:
                    control = _control(record, active, generations)
                    if control['control'] == 'start':
                        found[control['field']] = control['type']
    return found


def _log_type(kind):
    return {'double': 'Number', 'float': 'Number', 'int64': 'Number', 'boolean': 'Boolean', 'string': 'String'}.get(kind)


def _source(key, kind, index, discrete=False):
    return {'type': 'stripes' if discrete else 'stepped', 'logKey': key, 'logType': _log_type(kind), 'visible': True,
            'options': {'color': COLORS[index % len(COLORS)]} if discrete else
            {'color': COLORS[index % len(COLORS)], 'size': 'normal'}}


def _graph(title, fields, left, right=(), discrete=('/DriverStation/Enabled',)):
    def pick(keys, is_discrete=False):
        chosen = [k for k in keys if k in fields and _log_type(fields[k])]
        return [_source(k, fields[k], i, is_discrete) for i, k in enumerate(chosen)]
    controller = {'leftSources': pick(left), 'rightSources': pick(right), 'discreteSources': pick(discrete, True),
                  'leftLockedRange': None, 'rightLockedRange': None,
                  'leftUnitConversion': {'autoTarget': None, 'preset': None},
                  'rightUnitConversion': {'autoTarget': None, 'preset': None}, 'leftFilter': 0, 'rightFilter': 0}
    if not controller['leftSources'] and not controller['rightSources']:
        return None
    uid = hashlib.sha256(title.encode()).hexdigest()[:32]
    return {'type': 1, 'title': title[:40], 'controller': controller, 'controllerUUID': uid, 'renderer': None,
            'controlsHeight': 200}


def _component_keys(component, fields):
    """Logged numeric fields for a run_metrics component such as Drive/Module3/Drive or Shooter/Left."""
    module = re.fullmatch(r'Drive/Module(\d)(?:/(Drive|Turn))?', component)
    if module:
        part = module.group(2) or 'Drive'
        # Left: the same current on all four modules for comparison. Right: the flagged module's volts and speed.
        return ([f'/Drive/Module{i}/{part}CurrentAmps' for i in range(4)],
                [f'/Drive/Module{module.group(1)}/{part}{name}' for name in ('AppliedVolts', 'VelocityRadPerSec')])
    prefix = '/' + component
    keys = sorted(k for k in fields if k.startswith(prefix)
                  and re.fullmatch(r'/?(Stator|Supply)?(CurrentAmps|TempCelsius|AppliedVolts|VelocityRadPerSec)', k[len(prefix):]))
    return [k for k in keys if k.endswith('CurrentAmps')], [k for k in keys if not k.endswith('CurrentAmps')]


def layout(card, fields):
    tabs = [{'type': 0, 'title': '', 'controller': None, 'controllerUUID': 'hub-home', 'renderer': None, 'controlsHeight': 0}]
    seen = set()
    findings = [f for f in (card.get('siblings') or []) + (card.get('trends') or []) if isinstance(f, dict)]
    for finding in findings:
        component = finding.get('component')
        if not component or component in seen or component == 'robot':
            continue
        seen.add(component)
        from .run_metrics import display_component
        left, right = _component_keys(component, fields)
        graph = _graph(display_component(component), fields, left, right)
        if graph:
            tabs.append(graph)
    tabs.append(_graph('Battery and power', fields, ['/SystemStats/BatteryVoltage'],
                       ['/SystemStats/BatteryCurrent', '/PowerDistribution/TotalCurrent'],
                       ('/DriverStation/Enabled', '/SystemStats/BrownedOut')))
    tabs.append(_graph('Loop time and CAN', fields, ['/RealOutputs/LoggedRobot/FullCycleMS', '/RealOutputs/LoggedRobot/UserCodeMS'],
                       ['/SystemStats/CANBus/Utilization']))
    tabs = [t for t in tabs if t]
    return {'hubs': [{'x': 0, 'y': 0, 'width': 1600, 'height': 1000,
                      'state': {'sidebar': {'width': 300, 'expanded': []},
                                'tabs': {'selected': 1 if len(tabs) > 1 else 0, 'tabs': tabs}}}],
            'satellites': [], 'version': LAYOUT_VERSION}


def _readme(card, run, originals):
    start, end = int(run['start_monotonic_ns']) / 1e9, int(run['end_monotonic_ns']) / 1e9
    lines = ['Robot Test Hub - data for one run', '',
             f"Run: {card.get('local_start', 'time unknown')} ({', '.join(card.get('modes') or [])})",
             f"Look at log time {start:.1f} s to {end:.1f} s (AdvantageScope timeline, seconds since the robot program started).",
             '']
    if card.get('match'):
        lines.insert(3, f"Match: {card['match']['label']}")
    lines += ['What the hub noticed:'] + [f'  - {s}' for s in card.get('findings') or ['Nothing stood out.']] + ['']
    lines += ['How to look:',
              '  1. Open AdvantageScope and open the .wpilog file in this folder (File > Open Log).',
              '  2. Load advantagescope-layout.json (File > Import Layout). It adds graphs for the parts named above.',
              '  3. Drag the timeline to the time range above. (The hub cannot jump there for you.)', '',
              'Files:'] + [f'  - {name}: unchanged original, SHA-256 {sha}' for name, sha in originals] + [
              '', 'These are descriptive comparisons and rules of thumb, not health results. Physical causes are unconfirmed.']
    return '\n'.join(lines) + '\n'


def build(root, run_id, card):
    """Write the bundle to a temporary file and return (path, download name). Caller deletes it."""
    from .incident_logs import safe_path
    root = Path(root).resolve()
    with closing(sqlite3.connect((root / 'catalog.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        from . import run_notes
        try:
            run, _ = run_notes.select_run(db, run_id)
        except Exception:
            raise EvidenceError('run_not_found') from None
        originals = []
        for job_id in run['segment_ids']:
            row = db.execute('SELECT artifact_sha256 FROM import_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise EvidenceError('original_not_found')
            name = db.execute('SELECT original_name FROM import_requests WHERE artifact_sha256=? ORDER BY created_utc_ns LIMIT 1',
                              (row[0],)).fetchone()
            originals.append((row[0], name[0] if name else row[0] + '.wpilog'))
    paths = [safe_path(root, sha) for sha, _ in originals]
    if not all(p.is_file() for p in paths):
        raise EvidenceError('original_not_found')
    fields = _fields(paths)
    handle = tempfile.NamedTemporaryFile(prefix='bundle-', suffix='.zip', delete=False)
    handle.close()
    used = set()
    with zipfile.ZipFile(handle.name, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        listed = []
        for (sha, name), path in zip(originals, paths):
            clean = re.sub(r'[^A-Za-z0-9._-]', '_', Path(name).name) or sha + '.wpilog'
            if clean in used:
                clean = sha[:12] + '-' + clean
            used.add(clean)
            bundle.write(path, clean)
            listed.append((clean, sha))
        bundle.writestr('advantagescope-layout.json', json.dumps(layout(card, fields), indent=2))
        bundle.writestr('README.txt', _readme(card, run, listed))
    stamp = (card.get('local_start') or 'run')[:16].replace(':', '').replace('T', '-')
    return Path(handle.name), f'robot-run-{stamp}.zip'
