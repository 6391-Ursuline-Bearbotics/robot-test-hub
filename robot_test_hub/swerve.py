"""Swerve tracking from an explicit post-optimization, time-aligned SI sample contract.

This module never guesses log aliases, hardware IDs, command stages, or current
comparisons. Real recording adapters must supply the declared mapping/provenance.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import dataclass
import math
from .analysis import Analyzer, Declaration, metric, result

SAMPLE_PROFILE = 'swerve-final-io-si-1'
REQUIRED_CONTEXT = ('run_id','boot_id','robot_id','config_hash','test_id','surface','battery_id','mapping_revision')
BASELINE_KEYS = ('robot_id','build_hash','config_hash','mapping_revision','test_id','surface','battery_id','component_ids','wheel_radius_m','command_stage','sample_profile')


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def weighted_percentile(values, percentile):
    if not values:
        return None
    target = sum(weight for _,weight in values) * percentile
    cumulative = 0.0
    for value,weight in sorted(values):
        cumulative += weight
        if cumulative >= target:
            return value
    return max(v for v,_ in values)


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _sample_reason(row, config):
    if row.get('enabled') is not True:
        return 'disabled_or_unknown'
    if row.get('connected') is not True:
        return 'disconnected_or_unknown'
    if row.get('fresh') is not True:
        return 'stale_or_unknown'
    if row.get('validity') != 'valid':
        return 'invalid_or_unknown'
    if row.get('command_stage') != 'final_io_optimized_cosine_desaturated':
        return 'command_stage_unqualified'
    if row.get('sample_profile') != SAMPLE_PROFILE:
        return 'sample_profile_unsupported'
    if row.get('speed_unit') != 'meters per second' or row.get('angle_unit') != 'radians':
        return 'units_unsupported'
    for key in ('command_speed_mps','measured_speed_mps','command_angle_rad','measured_angle_rad'):
        if not _number(row.get(key)):
            return 'missing_or_nonfinite_values'
    stamp = row.get('timestamp_ns')
    for key in ('command_timestamp_ns','measurement_timestamp_ns'):
        value = row.get(key)
        if type(value) is not int or value < 0 or value > stamp or stamp-value > config['max_sample_age_ns']:
            return 'unaligned_or_stale_timestamps'
    if not isinstance(row.get('source_reference'),dict) or not row['source_reference'].get('source_hash'):
        return 'missing_source_reference'
    if abs(row['command_speed_mps']) < config['min_demand_mps']:
        return 'near_zero_demand'
    if config['minimum_voltage_v'] is not None:
        voltage = row.get('battery_voltage_v')
        if not _number(voltage):
            return 'voltage_unknown'
        if voltage < config['minimum_voltage_v']:
            return 'low_voltage_window'
    return None


def _config(configuration):
    config = {'max_gap_ns':250_000_000, 'max_sample_age_ns':100_000_000,
              'min_demand_mps':0.1,'minimum_eligible_seconds':1.0,'minimum_coverage':0.8,
              'minimum_voltage_v':None,'drive_error_limit_mps':None,
              'steering_error_limit_rad':None,'sustained_error_seconds':0.25,
              'threshold_revision':None, 'baseline_mad_multiplier':6.,
              'baseline_minimum_runs':3, 'baseline_drive_floor_mps':.1,
              'baseline_steering_floor_rad':.05}
    if set(configuration) - set(config):
        raise ValueError('Unknown swerve configuration fields')
    config.update(configuration)
    for key in ('max_gap_ns','max_sample_age_ns'):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(key + ' must be positive integer nanoseconds')
    for key in ('min_demand_mps','minimum_eligible_seconds','sustained_error_seconds'):
        if not _number(config[key]) or config[key] <= 0:
            raise ValueError(key + ' must be positive')
    if not _number(config['minimum_coverage']) or not 0 <= config['minimum_coverage'] <= 1:
        raise ValueError('minimum_coverage must be a fraction')
    for key in ('minimum_voltage_v','drive_error_limit_mps','steering_error_limit_rad'):
        if config[key] is not None and (not _number(config[key]) or config[key] <= 0):
            raise ValueError(key + ' must be positive or null')
    for key in ('baseline_mad_multiplier','baseline_drive_floor_mps','baseline_steering_floor_rad'):
        if not _number(config[key]) or config[key]<=0:
            raise ValueError(key+' must be positive')
    if type(config['baseline_minimum_runs']) is not int or config['baseline_minimum_runs']<3:
        raise ValueError('baseline_minimum_runs must be at least three independent runs')
    if any(config[k] is not None for k in ('drive_error_limit_mps','steering_error_limit_rad')) and not config['threshold_revision']:
        raise ValueError('Configured finding limits require explicit threshold_revision')
    return config


def swerve_tracking(evidence, configuration):
    config = _config(configuration)
    rows = [r for r in evidence.rows if r.get('kind') == 'swerve_sample']
    if not rows:
        return result('insufficient_data', unavailable=['explicit_final_io_swerve_mapping_unavailable'])
    context_missing = [k for k in REQUIRED_CONTEXT if not evidence.context.get(k)]
    if context_missing:
        return result('insufficient_data', unavailable=['missing_context:' + k for k in context_missing])
    groups = defaultdict(list)
    unqualified = []
    for row in rows:
        if (type(row.get('timestamp_ns')) is not int or row['timestamp_ns'] < 0
                or not isinstance(row.get('module_id'),str) or not row['module_id']
                or not isinstance(row.get('module_position'),str) or not row['module_position']):
            return result('insufficient_data', unavailable=['sample_timestamp_or_component_identity_invalid'])
        reference = row.get('source_reference')
        if not isinstance(reference, dict):
            return result('unsupported', unavailable=['sample_reference_not_in_immutable_input'])
        hashes = [reference.get('source_hash')]
        hashes.extend(reference[key] for key in ('command_source_hash','measured_source_hash','cycle_source_hash') if key in reference)
        hashes.extend(reference.get('connection_source_hashes', []))
        if any(digest not in evidence.source_hashes for digest in hashes):
            return result('unsupported', unavailable=['sample_reference_not_in_immutable_input'])
        if row.get('command_stage') != 'final_io_optimized_cosine_desaturated' or row.get('sample_profile') != SAMPLE_PROFILE:
            unqualified.append('explicit_final_io_sample_contract_required')
        groups[(row['module_id'],row['module_position'])].append(row)
    if unqualified:
        return result('unsupported', unavailable=sorted(set(unqualified)))
    metrics, findings, unavailable, module_reports = [], [], [], []
    for (component,position), samples in sorted(groups.items()):
        # Do not sort malformed/out-of-order inputs into apparently valid coverage.
        errors, angle_errors, commands = [], [], []
        exclusions = Counter()
        seconds, expected = 0.0, 0.0
        consecutive, largest_consecutive = 0.0, 0.0
        sustained_intervals, interval_start = [], None
        trace = []
        count = 0
        for previous,current in zip(samples,samples[1:]):
            delta_ns = current['timestamp_ns']-previous['timestamp_ns']
            if delta_ns <= 0:
                exclusions['nonadvancing_timestamp'] += 1
                consecutive, interval_start = 0, None
                continue
            dt = delta_ns / 1e9
            # Coverage includes lost enabled time, so gaps cannot improve a score.
            if previous.get('enabled') is True:
                expected += dt
            reason = 'recording_gap' if delta_ns > config['max_gap_ns'] else (_sample_reason(previous,config) or _sample_reason(current,config))
            if reason:
                exclusions[reason] += 1
                consecutive, interval_start = 0, None
                continue
            seconds += dt
            count += 1
            error = previous['measured_speed_mps']-previous['command_speed_mps']
            angle = wrap(previous['measured_angle_rad']-previous['command_angle_rad'])
            errors.append((abs(error),dt))
            angle_errors.append((abs(angle),dt))
            commands.append((previous['command_speed_mps'],dt))
            trace.append({'start_monotonic_ns':str(previous['timestamp_ns']),
                          'end_monotonic_ns':str(current['timestamp_ns']),
                          'drive_error_mps':error,'steering_error_rad':angle,'weight_seconds':dt,
                          'motion':previous.get('motion','unclassified'),
                          'source_reference':previous['source_reference']})
            limit = config['drive_error_limit_mps']
            if limit is not None and abs(error) > limit:
                if interval_start is None:
                    interval_start = previous['timestamp_ns']
                consecutive += dt
                largest_consecutive = max(largest_consecutive,consecutive)
                if consecutive >= config['sustained_error_seconds']:
                    if sustained_intervals and sustained_intervals[-1][0] == interval_start:
                        sustained_intervals[-1][1] = current['timestamp_ns']
                    else:
                        sustained_intervals.append([interval_start,current['timestamp_ns']])
            else:
                consecutive, interval_start = 0, None
        fraction = seconds/expected if expected else 0
        eligible = seconds >= config['minimum_eligible_seconds'] and fraction >= config['minimum_coverage']
        rmse = math.sqrt(sum(v*v*w for v,w in errors)/seconds) if seconds else None
        angle_rmse = math.sqrt(sum(v*v*w for v,w in angle_errors)/seconds) if seconds else None
        demand_rms = math.sqrt(sum(v*v*w for v,w in commands)/seconds) if seconds else None
        values = {'drive_rmse_mps':(rmse,'meters per second'),
                  'drive_p95_absolute_error_mps':(weighted_percentile(errors,.95),'meters per second'),
                  'steering_rmse_rad':(angle_rmse,'radians'),
                  'steering_p95_absolute_error_rad':(weighted_percentile(angle_errors,.95),'radians'),
                  'normalized_drive_rmse':(rmse/demand_rms if demand_rms else None,'fraction'),
                  'longest_sustained_drive_error_seconds':(largest_consecutive,'seconds')}
        for name,(value,unit) in values.items():
            item = metric(name,value if eligible else None,unit,sample_count=count,eligible_seconds=seconds)
            item.update(module_id=component,module_position=position,
                        unavailable_reason=None if eligible else 'minimum_duration_or_coverage_not_met')
            metrics.append(item)
        report = {'module_id':component,'module_position':position,'eligible_seconds':seconds,
                  'expected_enabled_seconds':expected,'fraction':fraction,'sample_count':count,
                  'exclusions':dict(exclusions),'eligible':eligible,'evidence_trace':trace}
        module_reports.append(report)
        if not eligible:
            unavailable.append('module_coverage_insufficient:' + component)
            continue
        if sustained_intervals:
            findings.append({'kind':'sustained_drive_tracking_error','severity':'warning',
                'module_id':component,'module_position':position,
                'observed_behavior':'Measured wheel velocity differs from the final IO command over sustained eligible intervals',
                'intervals_ns':[[str(a),str(b)] for a,b in sustained_intervals],
                'sample_count':count,'eligible_seconds':seconds,'source_hashes':list(evidence.source_hashes),
                'threshold_revision':config['threshold_revision'],'baseline_version':None,
                'comparison_cohort':None,'suggested_next_check':'Review command timing, voltage, surface and physical module, then repeat the same supervised test',
                'evidence_quality':'explicit time-aligned synthetic or recorded evidence; physical cause unconfirmed'})
        if config['steering_error_limit_rad'] is not None and angle_rmse > config['steering_error_limit_rad']:
            findings.append({'kind':'steering_tracking_error','severity':'warning','module_id':component,
                'module_position':position,'observed_behavior':'Wrapped steering RMSE exceeds the configured engineering limit',
                'intervals_ns':[[trace[0]['start_monotonic_ns'],trace[-1]['end_monotonic_ns']]],
                'sample_count':count,'eligible_seconds':seconds,'threshold_revision':config['threshold_revision'],
                'baseline_version':None,'source_hashes':list(evidence.source_hashes),
                'suggested_next_check':'Check signal timing and encoder/module calibration, then repeat the same supervised test'})
    supported_modules = sum(r['eligible'] for r in module_reports)
    outcome = 'finding' if findings else ('evaluated_no_finding' if supported_modules else 'insufficient_data')
    comparisons, baseline_findings, baseline_unavailable = compare_baseline(metrics, evidence.context,
        evidence.source_type, evidence.context.get('approved_baseline'), config)
    for finding in baseline_findings:
        finding['source_hashes']=list(evidence.source_hashes)
        trace=next((m['evidence_trace'] for m in module_reports if m['module_id']==finding['module_id'] and m['module_position']==finding['module_position']),[])
        finding['intervals_ns']=[[t['start_monotonic_ns'],t['end_monotonic_ns']] for t in trace]
    findings.extend(baseline_findings)
    unavailable.extend(baseline_unavailable)
    if baseline_findings:
        outcome='finding'
    engineering_criteria = any(config[k] is not None for k in ('drive_error_limit_mps','steering_error_limit_rad'))
    baseline_criteria = bool(config['threshold_revision'] and comparisons)
    if supported_modules and not engineering_criteria and not baseline_criteria:
        # Computing tracking error does not evaluate whether that error is
        # acceptable. Keep usable metrics, but never label an unconfigured
        # detector as a no-finding job eligible for a healthy baseline.
        outcome = 'insufficient_data'
        unavailable.append('tracking_finding_criteria_not_configured_or_available')
    return result(outcome, metrics=metrics, findings=findings, unavailable=unavailable,
                  coverage={'modules':module_reports,'evaluated_modules':supported_modules,
                            'fraction':min((r['fraction'] for r in module_reports),default=0),
                            'engineering_finding_criteria_configured':engineering_criteria,
                            'baseline_finding_criteria_evaluated':baseline_criteria,
                            'thresholds_qualified_on_hardware':False,'baseline_comparisons':comparisons})


def swerve_analyzer(profiles):
    return Analyzer(Declaration('swerve-tracking','3',tuple(profiles),
        eligible_windows='enabled, connected, fresh, final optimized IO, meaningful demand, aligned samples',
        grouping_keys=BASELINE_KEYS), swerve_tracking)

ROBOT_MAPPING_VERSION = '6391-alpha7-final-swerve-2'
ROBOT_FORMAT_PROFILE = 'wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6'
ROBOT_FIELDS = {'commands':'/RealOutputs/SwerveStates/SetpointsOptimized',
                'measured':'/RealOutputs/SwerveStates/Measured'}
MODULE_POSITIONS = ('front-left','front-right','back-left','back-right')


def adapt_robot_swerve(evidence, component_ids, *, ideal_simulation_cycle_policy=False):
    """Explicit source-reviewed mapping; REAL acquisition freshness stays unknown.

    component_ids maps integer module index to physical component identity. It is
    configuration/maintenance evidence, never invented from a front-left label.
    Unchanged AK values retain their original record references. A logging-cycle
    timestamp is a receipt time, not a hardware acquisition time.
    """
    from dataclasses import replace
    if evidence.profile != ROBOT_FORMAT_PROFILE:
        raise ValueError('Only the source-reviewed Alpha7/AK Alpha6 mapping is supported')
    if set(component_ids) != {0,1,2,3} or any(not isinstance(v,str) or not v for v in component_ids.values()):
        raise ValueError('Four explicit physical module component identities required')
    if type(ideal_simulation_cycle_policy) is not bool:
        raise ValueError('Simulation freshness policy must be boolean')
    if ideal_simulation_cycle_policy and evidence.source_type != 'simulation':
        raise ValueError('Ideal-cycle freshness can only be applied to explicit simulation evidence')
    schemas = {r.get('field'):r.get('value') for r in evidence.rows if r.get('kind')=='observation' and r.get('type')=='structschema'}
    expected = {'/.schema/struct:SwerveModuleVelocity':'double velocity;Rotation2d angle',
                '/.schema/struct:Rotation2d':'double value'}
    for name,value in expected.items():
        actual = schemas.get(name)
        if not isinstance(actual,str) or ''.join(actual.split()).rstrip(';') != ''.join(value.split()).rstrip(';'):
            raise ValueError('Recorded swerve schema missing or differs from installed source mapping')
    state, output, unavailable = {}, [], []
    def source_hash(row):
        # Real importer datasets always record this field. The single-artifact
        # fallback supports explicit standalone sample contracts, never chooses
        # an arbitrary artifact when several are present.
        return row.get('source_sha256', evidence.source_hashes[0] if len(evidence.source_hashes)==1 else None)
    for row in evidence.rows:
        if row.get('kind') == 'control' and row.get('control') in ('start','finish'):
            for field, held in list(state.items()):
                if field == row.get('field') or (row.get('entry_id') is not None
                        and held.get('entry_id') == row['entry_id']):
                    state.pop(field, None)
            continue
        if row.get('kind') == 'observation':
            state[row.get('field')] = row
            continue
        if row.get('kind') != 'cycle':
            continue
        timestamp = int(row['timestamp_ns'])
        command, measured = state.get(ROBOT_FIELDS['commands']),state.get(ROBOT_FIELDS['measured'])
        if not command or not measured:
            unavailable.append('final_command_or_measured_array_missing')
            continue
        if (command.get('type') != 'struct:SwerveModuleVelocity[]' or measured.get('type') != 'struct:SwerveModuleVelocity[]'
                or command.get('validity') != 'valid' or measured.get('validity') != 'valid'):
            unavailable.append('swerve_array_type_or_validity_unsupported')
            continue
        commands, measurements = command.get('value'),measured.get('value')
        if not isinstance(commands,list) or not isinstance(measurements,list) or len(commands)!=4 or len(measurements)!=4:
            unavailable.append('swerve_array_length_not_four')
            continue
        enabled_ref = row.get('aliases',{}).get('enabled',{})
        enabled = enabled_ref.get('value') if enabled_ref.get('validity') == 'valid' else None
        for index,(target,actual) in enumerate(zip(commands,measurements)):
            if not isinstance(target,dict) or not isinstance(actual,dict):
                unavailable.append('swerve_array_element_invalid')
                continue
            connections = [state.get(f'/Drive/Module{index}/'+name) for name in ('DriveConnected','TurnConnected','TurnEncoderConnected')]
            connected = all(v is not None and v.get('validity')=='valid' and v.get('value') is True for v in connections)
            target_angle = target.get('angle',{}).get('value') if isinstance(target.get('angle'),dict) else None
            actual_angle = actual.get('angle',{}).get('value') if isinstance(actual.get('angle'),dict) else None
            values = (target.get('velocity'),actual.get('velocity'),target_angle,actual_angle)
            output.append({'kind':'swerve_sample','timestamp_ns':timestamp,
                'module_id':component_ids[index],'module_position':MODULE_POSITIONS[index],
                'enabled':enabled,'connected':connected,'fresh':ideal_simulation_cycle_policy,
                'validity':'valid' if all(_number(v) for v in values) else 'missing_or_invalid',
                'command_speed_mps':values[0],'measured_speed_mps':values[1],
                'command_angle_rad':values[2],'measured_angle_rad':values[3],
                'command_timestamp_ns':timestamp if ideal_simulation_cycle_policy else None,
                'measurement_timestamp_ns':timestamp if ideal_simulation_cycle_policy else None,
                'command_stage':'final_io_optimized_cosine_desaturated','sample_profile':SAMPLE_PROFILE,
                'speed_unit':'meters per second','angle_unit':'radians','motion':'unclassified',
                'unit_source':'installed_alpha7_struct_and_robot_source_mapping',
                'freshness_policy':'ideal_simulation_cycle' if ideal_simulation_cycle_policy else 'physical_acquisition_time_unknown',
                'source_reference':{'source_hash':source_hash(command),
                    'command_source_hash':source_hash(command), 'measured_source_hash':source_hash(measured),
                    'cycle_source_hash':source_hash(row),
                    'connection_source_hashes':[source_hash(v) for v in connections if v],
                    'mapping_version':ROBOT_MAPPING_VERSION,'cycle_record_index':row.get('timestamp_record_index'),
                    'command_record_index':command.get('record_index'),'measured_record_index':measured.get('record_index'),
                    'command_record_timestamp_ns':command.get('record_timestamp_ns'),
                    'measured_record_timestamp_ns':measured.get('record_timestamp_ns'),
                    'command_updated_in_cycle':command.get('cycle_timestamp_ns')==row['timestamp_ns'],
                    'measured_updated_in_cycle':measured.get('cycle_timestamp_ns')==row['timestamp_ns'],
                    'connection_record_indices':[v.get('record_index') if v else None for v in connections]}})
    context = {**evidence.context,'swerve_mapping_version':ROBOT_MAPPING_VERSION,
               'swerve_mapping_unavailable':sorted(set(unavailable)),
               'swerve_freshness_policy':'ideal_simulation_cycle' if ideal_simulation_cycle_policy else 'physical_acquisition_time_unknown',
               'component_ids':{MODULE_POSITIONS[i]:component_ids[i] for i in range(4)}}
    return replace(evidence,rows=tuple(evidence.rows)+tuple(output),context=context)


def compare_baseline(metrics, context, source_type, baseline, configuration=None):
    """Median/MAD comparison to an explicitly approved immutable cohort.

    Engineering floors are provisional. Findings require a configured threshold
    revision and never claim a unique hardware cause or calibrated confidence.
    """
    import statistics
    config=_config(configuration or {})
    if not isinstance(baseline,dict) or baseline.get('approved') is not True:
        return [],[],['approved_baseline_comparison_unavailable']
    version=baseline.get('id')
    if not isinstance(version,str) or not version or baseline.get('source_type')!=source_type:
        return [],[],['baseline_source_or_version_incompatible']
    recorded=baseline.get('context',{})
    missing=[key for key in BASELINE_KEYS if context.get(key) is None or context.get(key)=='' or recorded.get(key) is None]
    if missing:
        return [],[],['baseline_context_missing:'+key for key in missing]
    if any(context[key]!=recorded[key] for key in BASELINE_KEYS):
        return [],[],['baseline_context_incompatible']
    history=baseline.get('comparison_history')
    from .analysis import identity
    if (not isinstance(history,dict) or not isinstance(history.get('maintenance'),list)
            or not isinstance(history.get('assignment'),list)
            or baseline.get('comparison_history_revision')!=identity(history)):
        return [],[],['baseline_maintenance_assignment_history_unavailable']
    try:
        cohort_end=int(baseline['cohort_end_utc_ns'])
        start,end=int(context['start_utc_ns']),int(context['end_utc_ns'])
        if not 0<=cohort_end<=start<end:
            return [],[],['baseline_comparison_interval_unqualified']
        for event in history['maintenance']:
            if (event['robot_id']==context['robot_id'] and event['kind']!='inspection'
                    and cohort_end<=int(event['effective_utc_ns'])<end):
                return [],[],['baseline_maintenance_configuration_boundary']
        approved_assignments=baseline.get('assignment_evidence',[])
        for position,component in context['component_ids'].items():
            covering=[a for a in history['assignment'] if a['robot_id']==context['robot_id']
                and a['location']==position and a['component_id']==component
                and int(a['start_utc_ns'])<=start
                and (a.get('end_utc_ns') is None or int(a['end_utc_ns'])>=end)]
            if len(covering)!=1:
                return [],[],['baseline_observed_assignment_unqualified']
            if not any(a['id']==covering[0]['id']
                       and a['component_id']==component and a['location']==position
                       and a['start_utc_ns']==covering[0]['start_utc_ns']
                       for a in approved_assignments):
                return [],[],['baseline_component_assignment_boundary']
    except (KeyError,ValueError,TypeError,OverflowError):
        return [],[],['baseline_comparison_history_invalid']
    runs=baseline.get('independent_runs',[])
    if not isinstance(runs,list) or len(set(runs))<config['baseline_minimum_runs']:
        return [],[],['baseline_independent_runs_insufficient']
    comparisons, findings, unavailable=[],[],[]
    for observed in metrics:
        if observed['name'] not in ('drive_rmse_mps','steering_rmse_rad') or observed['value'] is None:
            continue
        choices=[m for m in baseline.get('distributions',[]) if
            (m.get('module_id'),m.get('module_position'),m.get('name'),m.get('unit'))==
            (observed.get('module_id'),observed.get('module_position'),observed['name'],observed['unit'])]
        if len(choices)!=1:
            unavailable.append('baseline_metric_missing:'+observed['name']+':'+observed['module_id'])
            continue
        reference=choices[0]
        values=reference.get('values',[])
        independent=reference.get('run_ids',[])
        if (not isinstance(values,list) or len(values)!=len(independent)
                or len(set(independent))<config['baseline_minimum_runs'] or len(set(independent))!=len(independent)
                or not all(_number(v) for v in values)):
            unavailable.append('baseline_metric_coverage_invalid:'+observed['name'])
            continue
        median=statistics.median(values)
        mad=statistics.median(abs(v-median) for v in values)
        floor=config['baseline_drive_floor_mps'] if observed['name']=='drive_rmse_mps' else config['baseline_steering_floor_rad']
        limit=median+max(config['baseline_mad_multiplier']*mad,floor)
        comparison={'name':observed['name'],'module_id':observed['module_id'],
            'module_position':observed['module_position'],'unit':observed['unit'],'observed':observed['value'],
            'baseline_median':median,'baseline_mad':mad,'difference':observed['value']-median,
            'engineering_upper_limit':limit,'independent_run_count':len(independent),
            'baseline_version':version,'threshold_revision':config['threshold_revision'],
            'thresholds_qualified_on_hardware':False,'statistical_confidence_interval':False}
        comparisons.append(comparison)
        if config['threshold_revision'] and observed['value']>limit:
            findings.append({'kind':'tracking_regression_against_approved_cohort','severity':'warning',
                'module_id':observed['module_id'],'module_position':observed['module_position'],
                'observed_behavior':'Tracking error exceeds a provisional engineering limit above the approved cohort median/MAD',
                'metric':comparison,'baseline_version':version,'comparison_cohort':baseline['independent_runs'],
                'sample_count':observed['sample_count'],'eligible_seconds':observed['eligible_seconds'],
                'intervals_ns':[],'threshold_revision':config['threshold_revision'],
                'suggested_next_check':'Review timing, hardware/configuration and test context, then repeat the same supervised test',
                'physical_cause':'unconfirmed'})
    if not config['threshold_revision']:
        unavailable.append('baseline_finding_threshold_revision_unconfigured')
    return comparisons,findings,sorted(set(unavailable))
