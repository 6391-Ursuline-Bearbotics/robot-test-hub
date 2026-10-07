"""Recorded Phoenix SDK diagnostics; never a physical acquisition-time mapping."""
import math
import re
from collections import Counter

from .analysis import Analyzer, Declaration, metric, result
from .wpilog import PROFILE
from .swerve import MODULE_POSITIONS

VERSION = '1'
OBSERVATION_PROFILE = 'phoenix6-status-observation-1'
SNAPSHOT_METHOD = 'scheduler_owned_cached_read_after_existing_refresh'
REFERENCE_LIMIT = 128
MAX_ROWS = 50000

# Java int and long both use AK/WPILOG int64. Units below are the explicit
# source-reviewed profile, not inferred from field suffixes or SDK valid flags.
COMMON = {
    'DiagnosticsPresent': ('boolean', None), 'DiagnosticsProfile': ('string', None),
    'SnapshotMethod': ('string', None), 'ObservationSequence': ('int64', None),
    'RobotObservationStartNs': ('int64', 'nanoseconds'),
    'RobotObservationEndNs': ('int64', 'nanoseconds'),
    'VendorObservationStartSeconds': ('double', 'seconds'),
    'VendorObservationEndSeconds': ('double', 'seconds'),
    'ObservationClockValid': ('boolean', None), 'ObservationClockRegressed': ('boolean', None),
    'PhysicalAcquisitionTimeQualified': ('boolean', None),
    'NativeTimestampAvailabilityQualified': ('boolean', None),
    'DriveGroupRefreshStatusCode': ('int64', None), 'DriveGroupRefreshStatusOk': ('boolean', None),
    'TurnGroupRefreshStatusCode': ('int64', None), 'TurnGroupRefreshStatusOk': ('boolean', None),
}
SIGNALS = {'DriveVelocity': 'rotations per second', 'TurnPosition': 'rotations'}
SIGNAL_FIELDS = {
    'RawValue': ('double', None), 'StatusCode': ('int64', None), 'StatusOk': ('boolean', None),
    'BestTimestampSeconds': ('double', 'seconds'), 'BestTimestampSource': ('int64', None),
    'BestTimestampValid': ('boolean', None),
    'SystemTimestampSeconds': ('double', 'seconds'), 'SystemTimestampValid': ('boolean', None),
    'CANivoreTimestampSeconds': ('double', 'seconds'), 'CANivoreTimestampValid': ('boolean', None),
    'DeviceTimestampSeconds': ('double', 'seconds'), 'DeviceTimestampValid': ('boolean', None),
    'AgeAtObservationStartSeconds': ('double', 'seconds'),
    'AgeAtObservationEndSeconds': ('double', 'seconds'),
    'ReceiptComparison': ('string', None), 'RawValueComparison': ('string', None),
    'BestTimestampSourceChanged': ('boolean', None), 'TimestampInFuture': ('boolean', None),
}
FIELDS = frozenset(f'/Drive/Module{i}/Phoenix{name}' for i in range(4)
    for name in (*COMMON, *(signal+suffix for signal in SIGNALS for suffix in SIGNAL_FIELDS)))


def _ns(value):
    if type(value) is int and 0 <= value < (1 << 63): return value
    if type(value) is str and re.fullmatch('0|[1-9][0-9]{0,18}', value) and int(value) < (1 << 63): return int(value)
    raise ValueError('invalid_nanosecond_value')


def _integer(value):
    if type(value) is int and -(1 << 63) <= value < (1 << 63): return value
    if type(value) is str and re.fullmatch('0|-?[1-9][0-9]{0,18}', value) and -(1 << 63) <= int(value) < (1 << 63): return int(value)
    raise ValueError('invalid_int64_value')


def _record(row, hashes):
    if row.get('source_sha256') not in hashes: return 'source_reference_unavailable'
    if type(row.get('record_index')) is not int or row['record_index'] < 0: return 'record_index_invalid'
    if type(row.get('entry_id')) is not int or row['entry_id'] <= 0: return 'entry_identity_invalid'
    if type(row.get('entry_generation')) is not int or row['entry_generation'] <= 0: return 'entry_generation_invalid'
    try: _ns(row['record_timestamp_ns'])
    except (KeyError, ValueError): return 'record_timestamp_invalid'
    return None


def _read(state, path, specification):
    row = state.get(path)
    if row is None: return None, None, 'missing_field'
    kind, unit = specification
    if row.get('validity') != 'valid' or row.get('category') != 'input' or row.get('type') != kind:
        return None, row, 'type_validity_or_category_invalid'
    if row.get('unit') not in (None, unit): return None, row, 'unit_incompatible'
    value = row.get('value')
    if kind == 'boolean': valid = type(value) is bool
    elif kind == 'string': valid = type(value) is str and len(value) <= 128 and '\x00' not in value
    elif kind == 'double': valid = type(value) in (int, float) and math.isfinite(value) and abs(value) < 1e19
    else:
        try: value = _integer(value); valid = True
        except ValueError: valid = False
    return (value if valid else None), row, (None if valid else 'value_invalid')


def _ref(row, cycle, unit=None):
    return {'source_sha256': row['source_sha256'], 'field': row['field'], 'type': row['type'],
        'record_index': row['record_index'], 'entry_id': row['entry_id'], 'entry_generation': row['entry_generation'],
        'record_timestamp_ns': row['record_timestamp_ns'], 'record_cycle_timestamp_ns': row.get('cycle_timestamp_ns'),
        'current_cycle_timestamp_ns': cycle['timestamp_ns'],
        'current_cycle_record_index': cycle.get('timestamp_record_index'),
        'cycle_source_sha256': cycle['source_sha256'],
        'updated_in_cycle': row.get('cycle_timestamp_ns') == cycle['timestamp_ns'],
        'before_run': row.get('before_run', False), 'record_unit': row.get('unit'),
        'interpreted_unit': unit, 'unit_basis': 'explicit_source_reviewed_phoenix_profile',
        'observation_profile': OBSERVATION_PROFILE}


def phoenix_status(evidence, configuration):
    if configuration: raise ValueError('No automatic physical/staleness limits are declared')
    if evidence.context.get('row_limit_reached') or len(evidence.rows) > MAX_ROWS:
        return result('insufficient_data', unavailable=['report_resource_limit_reached'])
    try:
        lo, hi = _ns(evidence.context['start_monotonic_ns']), _ns(evidence.context['end_monotonic_ns'])
        if lo > hi: raise ValueError()
    except (KeyError, ValueError): return result('insufficient_data', unavailable=['run_robot_interval_unavailable'])
    unavailable = set()
    if evidence.context.get('completeness') != 'complete': unavailable.add('complete_run_required_for_status_coverage')
    if evidence.context.get('source_nonadvancing_cycles'): unavailable.add('source_cycle_order_unqualified')
    state, indexes, cycle_times = {}, {}, {}
    summaries = [{'module_index': i, 'module_position': position,
        'physical_component_id': evidence.context.get('component_ids', {}).get(position),
        'profile_cycles': 0, 'status_cycles': 0, 'timing_cycles': 0, 'distinct_sdk_observations': 0,
        'held_snapshot_cycles': 0, 'sequence_regressions': 0, 'signal_observations': {},
        'latest_observation': None, 'unavailable_fields': {}, 'unavailable': set()} for i, position in enumerate(MODULE_POSITIONS)]
    warning_groups, warning_seen = {}, set()
    last_sequences, sampled_sequences = {}, set()
    expected_cycles, cycle_stamps = 0, set()

    def problem(summary, message):
        summary['unavailable'].add(message); unavailable.add(message+':'+summary['module_position'])

    def warning(summary, name, row, code, code_row, cycle, profile_row):
        identity = (row['source_sha256'], row['record_index'], row['field'])
        if identity in warning_seen: return
        warning_seen.add(identity)
        key = (summary['module_index'], name)
        finding = warning_groups.setdefault(key, {
            'kind': 'recorded_phoenix_group_refresh_status_not_ok' if 'GroupRefresh' in name else 'recorded_phoenix_signal_status_not_ok',
            'severity': 'warning', 'module_index': summary['module_index'], 'module_position': summary['module_position'],
            'signal': name, 'observed_behavior': 'Recorded or held Phoenix SDK '+name+' StatusOk is false; this is an SDK status observation, not a physical fault diagnosis',
            'criterion': 'exact_recorded_status_ok_false', 'criterion_revision': OBSERVATION_PROFILE,
            'physical_health_qualified': False, 'observed_false_status_records': 0,
            'source_references': [], 'supporting_references_limited': False,
            'next_check': 'Inspect the pinned SDK status code and source interval before a supervised hardware check'})
        finding['observed_false_status_records'] += 1
        if len(finding['source_references']) < REFERENCE_LIMIT:
            finding['source_references'].append({'status_ok': _ref(row, cycle), 'status_code': _ref(code_row, cycle) if code_row else None,
                'recorded_status_code': code, 'profile': _ref(profile_row, cycle)})
        else: finding['supporting_references_limited'] = True

    for row in evidence.rows:
        source = row.get('source_sha256')
        if source not in evidence.source_hashes:
            unavailable.add('source_reference_unavailable'); continue
        held = state.setdefault(source, {})
        if row.get('kind') in ('observation', 'control'):
            if row.get('kind') == 'observation' and row.get('field') not in FIELDS: continue
            if row.get('kind') == 'observation':
                error = _record(row, evidence.source_hashes)
                if error: unavailable.add(error); held.pop(row.get('field'), None); continue
            index = row.get('record_index')
            if type(index) is not int or index < 0 or index <= indexes.get(source, -1):
                unavailable.add('source_record_order_unqualified'); held.pop(row.get('field'), None); continue
            indexes[source] = index
            if row['kind'] == 'control':
                if row.get('control') in ('start', 'finish', 'metadata'):
                    for field, value in list(held.items()):
                        if field == row.get('field') or value.get('entry_id') == row.get('entry_id'):
                            held.pop(field, None)
                continue
            # Entry IDs can be replaced even in an incomplete standalone dataset.
            for field, value in list(held.items()):
                if field != row['field'] and value.get('entry_id') == row['entry_id']: held.pop(field, None)
            held[row['field']] = row
            continue
        if row.get('kind') != 'cycle': continue
        if type(row.get('timestamp_record_index')) is not int or row['timestamp_record_index'] < 0:
            unavailable.add('cycle_record_reference_invalid'); continue
        try: stamp = _ns(row['timestamp_ns'])
        except (KeyError, ValueError): unavailable.add('cycle_timestamp_invalid'); continue
        if stamp <= cycle_times.get(source, -1): unavailable.add('source_cycle_order_unqualified'); continue
        cycle_times[source] = stamp
        if not lo <= stamp <= hi: continue
        if stamp in cycle_stamps: unavailable.add('overlapping_source_cycle_intervals'); continue
        cycle_stamps.add(stamp); expected_cycles += 1
        for summary in summaries:
            prefix = f"/Drive/Module{summary['module_index']}/Phoenix"
            common, common_rows, errors = {}, {}, {}
            for name, spec in COMMON.items():
                value, reference, error = _read(held, prefix+name, spec)
                common[name], common_rows[name] = value, reference
                if error: errors[name] = error
            if common['DiagnosticsPresent'] is not True or common['DiagnosticsProfile'] != OBSERVATION_PROFILE:
                problem(summary, 'phoenix_diagnostics_profile_unavailable'); continue
            if common['SnapshotMethod'] != SNAPSHOT_METHOD:
                problem(summary, 'phoenix_snapshot_method_unsupported'); continue
            summary['profile_cycles'] += 1
            # False status remains observable even when clock metadata is missing.
            status_complete = True
            signals, signal_rows = {}, {}
            for name in ('DriveGroupRefresh', 'TurnGroupRefresh'):
                code, status = common[name+'StatusCode'], common[name+'StatusOk']
                if name+'StatusCode' in errors or code is None or not -(1<<31) <= code < (1<<31):
                    status_complete = False; problem(summary, 'phoenix_status_code_unavailable'); code = None
                if name+'StatusOk' in errors: status_complete = False; problem(summary, 'phoenix_status_ok_unavailable')
                elif status is False: warning(summary, name, common_rows[name+'StatusOk'], code, common_rows[name+'StatusCode'] if code is not None else None, row, common_rows['DiagnosticsProfile'])
            timing_errors = dict(errors)
            for signal, raw_unit in SIGNALS.items():
                values, references = {}, {}
                for suffix, spec in SIGNAL_FIELDS.items():
                    value, reference, error = _read(held, prefix+signal+suffix, (spec[0], raw_unit if suffix == 'RawValue' else spec[1]))
                    values[suffix], references[suffix] = value, reference
                    if error: timing_errors[signal+suffix] = error
                signals[signal], signal_rows[signal] = values, references
                code = values['StatusCode']
                if code is None or not -(1<<31) <= code < (1<<31): status_complete = False; problem(summary, 'phoenix_status_code_unavailable'); code = None
                if signal+'StatusOk' in timing_errors: status_complete = False; problem(summary, 'phoenix_status_ok_unavailable')
                elif values['StatusOk'] is False: warning(summary, signal, references['StatusOk'], code, references['StatusCode'] if code is not None else None, row, common_rows['DiagnosticsProfile'])
                if values['BestTimestampSource'] not in (0, 1, 2): timing_errors[signal+'BestTimestampSource'] = 'timestamp_source_invalid'
                if values['ReceiptComparison'] not in ('unknown', 'first', 'held', 'advanced', 'regressed', 'invalid'): timing_errors[signal+'ReceiptComparison'] = 'comparison_invalid'
                if values['RawValueComparison'] not in ('unknown', 'first', 'held', 'changed', 'invalid'): timing_errors[signal+'RawValueComparison'] = 'comparison_invalid'
                if values['BestTimestampValid'] is not True: timing_errors[signal+'BestTimestampValid'] = 'sdk_best_timestamp_unavailable'
            if status_complete: summary['status_cycles'] += 1
            sequence = common['ObservationSequence']
            if sequence is None or sequence <= 0: timing_errors['ObservationSequence'] = 'sequence_invalid'
            if common['PhysicalAcquisitionTimeQualified'] is not False or common['NativeTimestampAvailabilityQualified'] is not False:
                timing_errors['Qualification'] = 'physical_or_native_qualification_not_supported'
            if common['ObservationClockValid'] is not True or common['ObservationClockRegressed'] is not False:
                timing_errors['ObservationClock'] = 'recorded_bookend_clock_unqualified'
            try:
                start, end = _ns(common['RobotObservationStartNs']), _ns(common['RobotObservationEndNs'])
                if start > end or common['VendorObservationStartSeconds'] > common['VendorObservationEndSeconds']: raise ValueError()
            except (ValueError, TypeError): timing_errors['ObservationBookends'] = 'bookends_unavailable_or_regressed'
            for name, error in timing_errors.items():
                problem(summary, 'phoenix_timing_'+error)
                reference = common_rows.get(name)
                if reference is None:
                    for signal in SIGNALS:
                        if name.startswith(signal): reference = signal_rows[signal].get(name[len(signal):]); break
                summary['unavailable_fields'][name] = {'reason': error, 'source_reference': _ref(reference, row) if reference else None}
            if timing_errors: continue
            key = (source, summary['module_index'])
            previous = last_sequences.get(key)
            if previous is not None and sequence < previous:
                summary['sequence_regressions'] += 1; problem(summary, 'phoenix_observation_sequence_regressed'); continue
            if previous == sequence: summary['held_snapshot_cycles'] += 1
            last_sequences[key] = sequence
            summary['timing_cycles'] += 1
            identity = (*key, sequence)
            if identity in sampled_sequences: continue
            sampled_sequences.add(identity); summary['distinct_sdk_observations'] += 1
            summary['latest_observation'] = {'source_sha256': source, 'cycle_timestamp_ns': row['timestamp_ns'],
                'observation_sequence': sequence, 'robot_start_ns': str(start), 'robot_end_ns': str(end),
                'vendor_start_seconds': common['VendorObservationStartSeconds'], 'vendor_end_seconds': common['VendorObservationEndSeconds'],
                'robot_bookend_span_seconds': (end-start)/1e9,
                'vendor_bookend_span_seconds': common['VendorObservationEndSeconds']-common['VendorObservationStartSeconds'],
                'common_references': {name: _ref(reference, row, COMMON[name][1]) for name, reference in common_rows.items()}, 'signals': {}}
            for signal, values in signals.items():
                observation = summary['signal_observations'].setdefault(signal, {'receipt_comparisons': Counter(), 'raw_value_comparisons': Counter(),
                    'best_timestamp_sources': Counter(), 'sdk_timestamp_in_future_count': 0, 'best_timestamp_source_changed_count': 0,
                    'age_at_observation_start_seconds_min': None, 'age_at_observation_end_seconds_max': None})
                observation['receipt_comparisons'][values['ReceiptComparison']] += 1
                observation['raw_value_comparisons'][values['RawValueComparison']] += 1
                observation['best_timestamp_sources'][str(values['BestTimestampSource'])] += 1
                observation['sdk_timestamp_in_future_count'] += values['TimestampInFuture'] is True
                observation['best_timestamp_source_changed_count'] += values['BestTimestampSourceChanged'] is True
                a, b = values['AgeAtObservationStartSeconds'], values['AgeAtObservationEndSeconds']
                observation['age_at_observation_start_seconds_min'] = a if observation['age_at_observation_start_seconds_min'] is None else min(a, observation['age_at_observation_start_seconds_min'])
                observation['age_at_observation_end_seconds_max'] = b if observation['age_at_observation_end_seconds_max'] is None else max(b, observation['age_at_observation_end_seconds_max'])
                summary['latest_observation']['signals'][signal] = {'recorded_values': values, 'raw_value_unit': SIGNALS[signal],
                    'references': {suffix: _ref(reference, row, SIGNALS[signal] if suffix == 'RawValue' else SIGNAL_FIELDS[suffix][1]) for suffix, reference in signal_rows[signal].items()}}
    metrics = []
    for summary in summaries:
        summary['unavailable'] = sorted(summary['unavailable'])
        summary['expected_cycles'] = expected_cycles
        summary['status_coverage_fraction'] = summary['status_cycles']/expected_cycles if expected_cycles else 0
        summary['timing_coverage_fraction'] = summary['timing_cycles']/expected_cycles if expected_cycles else 0
        for signal, observation in summary['signal_observations'].items():
            for name in ('age_at_observation_start_seconds_min', 'age_at_observation_end_seconds_max'):
                m = metric(summary['module_position'].replace('-', '_')+'_'+signal+'_'+name, observation[name], 'seconds', sample_count=summary['distinct_sdk_observations'])
                m.update(module_index=summary['module_index'], signal=signal, interpretation='observed SDK timestamp age only; not calibrated physical acquisition freshness')
                metrics.append(m)
            for name in ('receipt_comparisons', 'raw_value_comparisons', 'best_timestamp_sources'): observation[name] = dict(observation[name])
    if not expected_cycles: unavailable.add('run_cycles_unavailable')
    elif min(cycle_stamps) != lo or max(cycle_stamps) != hi: unavailable.add('whole_run_cycle_endpoints_unavailable')
    if any(s['status_cycles'] != expected_cycles or s['timing_cycles'] != expected_cycles for s in summaries): unavailable.add('whole_run_all_four_modules_status_and_timing_coverage_unavailable')
    # SDK validity/availability flags are not native capability or physical timing qualification.
    complete = not unavailable
    unavailable.update(['physical_acquisition_time_not_qualified', 'native_timestamp_availability_not_qualified', 'sdk_age_fault_threshold_not_configured'])
    findings = list(warning_groups.values())
    return result('finding' if findings else 'evaluated_no_finding' if complete else 'insufficient_data', metrics=metrics, findings=findings, unavailable=sorted(unavailable),
        coverage={'scope': 'recorded_sdk_status_and_observation_timing_only', 'observation_profile': OBSERVATION_PROFILE,
            'module_observations': summaries, 'expected_cycles': expected_cycles,
            'status_criterion': 'exact_recorded_status_ok_false', 'criterion_revision': OBSERVATION_PROFILE,
            'sdk_age_fault_threshold_configured': False, 'physical_acquisition_time_qualified': False,
            'native_timestamp_availability_qualified': False, 'source_reference_limit_per_finding': REFERENCE_LIMIT})


def phoenix_status_analyzer():
    return Analyzer(Declaration('phoenix-status-observation', VERSION, (PROFILE,),
        eligible_windows='recorded SDK status and cached-read observations within run; physical freshness unknown',
        grouping_keys=('robot_id', 'boot_id', 'runtime_mode', 'mapping_revision')), phoenix_status)