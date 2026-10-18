"""Detect matches (auto followed by teleop with the match clock running) from runs.

Built from real 2026 logs: in an FMS or Driver Station practice match, the auto run
starts with MatchTime near 15-20 s and the teleop run starts a few seconds after
auto ends with MatchTime near 135-140 s, in the same boot. FMS adds the event name,
match type and number. Free driving has no running match clock and is not a match.
"""
from __future__ import annotations

MATCH_TYPES = {1: 'Practice', 2: 'Qualification', 3: 'Elimination'}
AUTO_CLOCK_MIN_S = 10.0
TELEOP_CLOCK_MIN_S = 60.0
MAX_GAP_S = 15.0


def _only(run, mode):
    return [p.get('mode') for p in run.get('phases') or []] == [mode]


def label(context):
    number, kind = context.get('match_number'), MATCH_TYPES.get(context.get('match_type'))
    if context.get('fms_attached') is True and kind and isinstance(number, int) and number > 0:
        event = context.get('event_name') or ''
        return f"{kind} {number}" + (f" · {event}" if event else '')
    return 'Practice match'


def detect(runs, contexts):
    """runs: catalog run dicts in time order; contexts: {run_id: context}. Returns {run_id: match}."""
    found = {}
    for index, auto in enumerate(runs):
        context = contexts.get(auto['run_id'], {})
        clock = context.get('match_time')
        if not _only(auto, 'AUTONOMOUS') or not isinstance(clock, (int, float)) or clock < AUTO_CLOCK_MIN_S:
            continue
        match = {'id': 'match-' + auto['run_id'][:16], 'label': label(context),
                 'fms': context.get('fms_attached') is True, 'event': context.get('event_name') or None,
                 'type': MATCH_TYPES.get(context.get('match_type')), 'number': context.get('match_number'),
                 'run_ids': [auto['run_id']], 'complete': False,
                 'basis': 'auto run with a running match clock, then teleop in the same boot'}
        found[auto['run_id']] = match
        for teleop in runs[index + 1:index + 3]:
            teleop_clock = contexts.get(teleop['run_id'], {}).get('match_time')
            # Measure from when auto was scheduled to end, so an auto disabled early still pairs.
            gap = (int(teleop['start_monotonic_ns']) - int(auto['start_monotonic_ns'])) / 1e9 - clock
            if (teleop.get('boot_id') == auto.get('boot_id') and _only(teleop, 'TELEOPERATED') and 0 <= gap <= MAX_GAP_S
                    and isinstance(teleop_clock, (int, float)) and teleop_clock >= TELEOP_CLOCK_MIN_S):
                match['run_ids'].append(teleop['run_id'])
                match['complete'] = True
                found[teleop['run_id']] = match
                break
    return found
