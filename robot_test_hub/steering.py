"""Descriptive steering dynamics in source-pinned logger states.

These intervals describe recorded/held state, not physical acquisition times.
Structural defaults define reproducible windows, not mechanical acceptance limits.
"""
from collections import Counter, defaultdict
import math
from .analysis import Analyzer, Declaration, metric, result
from .swerve import (ROBOT_FORMAT_PROFILE, ROBOT_MAPPING_VERSION, ROBOT_FIELDS,
                     SAMPLE_PROFILE, REQUIRED_CONTEXT, MODULE_POSITIONS, wrap,
                     weighted_percentile)

MAX_ROWS = 50000
EVENT_LIMIT = 128
LIMITS = ('settling_limit_seconds', 'oscillation_crossing_limit',
          'recorded_jump_limit_rad', 'sustained_error_limit_rad')


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def steering_configuration(mapping):
    """Return validated defaults; finding limits are opt-in revisioned criteria."""
    config = dict(max_gap_ns=250000000, min_demand_mps=.1,
        minimum_eligible_seconds=1., minimum_coverage=.8, command_step_rad=.15,
        target_stability_rad=.02, settle_band_rad=.05, settle_dwell_seconds=.1,
        minimum_stable_seconds=.5, sustained_error_seconds=.5,
        settling_limit_seconds=None, oscillation_crossing_limit=None,
        recorded_jump_limit_rad=None, sustained_error_limit_rad=None,
        threshold_revision=None)
    if not isinstance(mapping, dict) or set(mapping)-set(config):
        raise ValueError('Unknown steering configuration')
    config.update(mapping)
    if type(config['max_gap_ns']) is not int or not 0 < config['max_gap_ns'] < (1 << 63):
        raise ValueError('max_gap_ns requires positive integer nanoseconds')
    for key in ('min_demand_mps', 'minimum_eligible_seconds', 'command_step_rad',
                'target_stability_rad', 'settle_band_rad', 'settle_dwell_seconds',
                'minimum_stable_seconds', 'sustained_error_seconds'):
        if not _number(config[key]) or config[key] <= 0:
            raise ValueError(key+' requires a positive finite value')
    if not _number(config['minimum_coverage']) or not 0 <= config['minimum_coverage'] <= 1:
        raise ValueError('minimum_coverage requires a fraction')
    if not config['target_stability_rad'] < config['command_step_rad'] <= math.pi:
        raise ValueError('target stability must be below step magnitude, at most pi')
    if not 0 < config['settle_band_rad'] < math.pi/2:
        raise ValueError('settle band must be below pi/2')
    for key in LIMITS:
        value = config[key]
        if value is None:
            continue
        if key == 'oscillation_crossing_limit':
            if type(value) is not int or not 0 < value <= MAX_ROWS:
                raise ValueError('oscillation_crossing_limit requires a positive integer')
        elif not _number(value) or value <= 0:
            raise ValueError(key+' requires a positive finite value or null')
    revision = config['threshold_revision']
    if revision is not None and (not isinstance(revision,str) or not revision.strip() or len(revision)>256):
        raise ValueError('threshold_revision requires a nonempty bounded string')
    if any(config[key] is not None for key in LIMITS) and revision is None:
        raise ValueError('Finding limits require threshold_revision')
    return config


def _ns(value):
    if type(value) is int and 0 <= value < (1 << 63):
        return value
    if isinstance(value,str) and value.isascii() and value.isdecimal() and len(value)<=19:
        converted=int(value)
        if 0 <= converted < (1 << 63):
            return converted
    raise ValueError('invalid_monotonic_timestamp')


def _index(value):
    return type(value) is int and 0 <= value < (1 << 63)


def _source(row, evidence):
    return row.get('source_sha256', evidence.source_hashes[0] if len(evidence.source_hashes)==1 else None)


def _raw_index(evidence):
    records={}; cycles={}; epochs=Counter(); cycle_order=Counter(); duplicate=False
    entries={};signatures={}
    relevant=set(ROBOT_FIELDS.values()) | {
        f'/Drive/Module{i}/{field}' for i in range(4)
        for field in ('DriveConnected','TurnConnected','TurnEncoderConnected')}
    for row in evidence.rows:
        source=_source(row,evidence)
        field=row.get('field');entry=row.get('entry_id')
        if row.get('kind')=='control':
            if (field in relevant or str(field or '').startswith('/.schema/')
                    or entries.get((source,entry)) in relevant):epochs[source]+=1
            if row.get('control') in ('start','finish'):entries.pop((source,entry),None)
        if row.get('kind')=='observation' and field in relevant:
            signature=(entry,row.get('entry_generation'))
            if (source,field) in signatures and signatures[source,field]!=signature:epochs[source]+=1
            signatures[source,field]=signature
            if _index(entry):entries[source,entry]=field
        if row.get('kind') in ('observation','control') and _index(row.get('record_index')):
            key=(source,row['record_index'])
            if key in records:duplicate=True
            records[key]=row
        if row.get('kind')=='cycle' and _index(row.get('timestamp_record_index')):
            key=(source,row['timestamp_record_index'])
            if key in cycles:duplicate=True
            cycles[key]=(row,epochs[source],cycle_order[source])
            cycle_order[source]+=1
    return records,cycles,duplicate


def _reference(row, evidence, records, cycles):
    """Resolve immutable original/held records, never synthesize acquisition time."""
    ref=row.get('source_reference')
    if not isinstance(ref,dict) or ref.get('mapping_version')!=ROBOT_MAPPING_VERSION:
        return None,'source_mapping_reference_unqualified'
    hashes=[ref.get(key) for key in ('source_hash','command_source_hash','measured_source_hash','cycle_source_hash')]
    connections=ref.get('connection_source_hashes')
    indices=ref.get('connection_record_indices')
    if (not isinstance(connections,list) or len(connections)!=3 or not isinstance(indices,list) or len(indices)!=3
            or any(digest not in evidence.source_hashes for digest in hashes+connections)):
        return None,'sample_reference_not_in_immutable_input'
    keys=('command_record_index','measured_record_index','cycle_record_index')
    if any(not _index(ref.get(key)) for key in keys) or any(not _index(v) for v in indices):
        return None,'source_record_index_invalid'
    cycle=cycles.get((ref['cycle_source_hash'],ref['cycle_record_index']))
    command=records.get((ref['command_source_hash'],ref['command_record_index']))
    measured=records.get((ref['measured_source_hash'],ref['measured_record_index']))
    if not cycle or not command or not measured:
        return None,'sample_reference_not_in_immutable_input'
    try:
        stamp=_ns(row['timestamp_ns'])
        if _ns(cycle[0]['timestamp_ns'])!=stamp:
            raise ValueError()
        for name,original in (('command',command),('measured',measured)):
            record_stamp=_ns(original['record_timestamp_ns'])
            if record_stamp!=_ns(ref[name+'_record_timestamp_ns']) or record_stamp>stamp:
                raise ValueError()
            if (original.get('field')!=ROBOT_FIELDS['commands' if name=='command' else 'measured']
                    or original.get('type')!='struct:SwerveModuleVelocity[]' or original.get('validity')!='valid'
                    or type(ref.get(name+'_updated_in_cycle')) is not bool
                    or ref[name+'_updated_in_cycle']!=(original.get('cycle_timestamp_ns')==cycle[0]['timestamp_ns'])):
                raise ValueError()
        module=MODULE_POSITIONS.index(row['module_position'])
        enabled=cycle[0].get('aliases',{}).get('enabled',{})
        if enabled.get('validity')!='valid' or enabled.get('value') is not row.get('enabled'):
            raise ValueError()
        for original,prefix in ((command,'command'),(measured,'measured')):
            values=original.get('value')
            if not isinstance(values,list) or len(values)!=4:raise ValueError()
            value=values[module]
            if (not isinstance(value,dict) or not isinstance(value.get('angle'),dict)
                    or not _number(value.get('velocity')) or not _number(value['angle'].get('value'))
                    or value.get('velocity')!=row.get(prefix+'_speed_mps')
                    or value['angle'].get('value')!=row.get(prefix+'_angle_rad')):raise ValueError()
        for source,index,field in zip(connections,indices,('DriveConnected','TurnConnected','TurnEncoderConnected')):
            observation=records.get((source,index))
            if (not observation or observation.get('field')!=f'/Drive/Module{module}/'+field
                    or observation.get('type')!='boolean' or observation.get('value') is not True
                    or observation.get('validity')!='valid' or _ns(observation['record_timestamp_ns'])>stamp):
                raise ValueError()
    except (KeyError,ValueError,TypeError):
        return None,'source_record_content_or_timestamp_unqualified'
    if ref['source_hash'] != ref['command_source_hash'] or len(set(hashes+connections)) != 1:
        return None,'cross_source_held_state_unqualified'
    # Raw lifecycle epoch and cycle order prevent bridging a missing mapped cycle.
    return (ref['cycle_source_hash'],cycle[1],cycle[2]),None


def _reason(row, config):
    if row.get('enabled') is not True:return 'disabled_or_unknown'
    if row.get('connected') is not True:return 'disconnected_or_unknown'
    if row.get('validity')!='valid':return 'invalid_or_unknown'
    if row.get('command_stage')!='final_io_optimized_cosine_desaturated' or row.get('sample_profile')!=SAMPLE_PROFILE:
        return 'sample_contract_unsupported'
    if row.get('speed_unit')!='meters per second' or row.get('angle_unit')!='radians':return 'units_unsupported'
    if any(not _number(row.get(key)) for key in ('command_speed_mps','measured_speed_mps','command_angle_rad','measured_angle_rad')):
        return 'missing_or_nonfinite_values'
    if abs(row['command_speed_mps'])<config['min_demand_mps']:return 'near_zero_demand'
    return None


def _advance(previous, current, field, speed, config):
    delta=wrap(wrap(current[field])-wrap(previous[field]))
    # Exact optimized representation reversal is the same requested wheel vector.
    # Suppress only a pi angle change accompanied by a signed velocity reversal.
    if (previous[speed]<0<current[speed] or current[speed]<0<previous[speed]) and abs(abs(delta)-math.pi)<=config['target_stability_rad']:
        delta=wrap(delta-math.copysign(math.pi,delta))
    return delta


def steering_observation(evidence, configuration):
    config=steering_configuration(configuration)
    qualifiers=dict(physical_acquisition_time_qualified=False,physical_health_qualified=False,
        baseline_eligible=False,observation_basis='recorded_or_held_logger_state',
        source_type=evidence.source_type,thresholds_qualified_on_hardware=False)
    def unavailable(reason,outcome='insufficient_data'):
        return result(outcome,unavailable=[reason],coverage=qualifiers.copy())
    if evidence.profile!=ROBOT_FORMAT_PROFILE:return unavailable('unsupported_steering_profile','unsupported')
    if len(evidence.rows)>MAX_ROWS or evidence.context.get('row_limit_reached'):return unavailable('report_resource_limit_reached')
    missing=[key for key in REQUIRED_CONTEXT if not isinstance(evidence.context.get(key),str) or not evidence.context[key].strip()]
    if missing:return unavailable('missing_steering_context:'+','.join(missing))
    assignments=evidence.context.get('component_ids')
    if (not isinstance(assignments,dict) or set(assignments)!=set(MODULE_POSITIONS)
            or any(not isinstance(v,str) or not v for v in assignments.values()) or len(set(assignments.values()))!=4):
        return unavailable('four_explicit_component_assignments_required')
    try:
        lo,hi=(_ns(evidence.context[key]) for key in ('start_monotonic_ns','end_monotonic_ns'))
        if lo>=hi:raise ValueError()
    except (KeyError,ValueError):return unavailable('run_robot_interval_unavailable')
    if evidence.context.get('completeness')!='complete':return unavailable('complete_run_required_for_steering_coverage')
    records,cycles,duplicate=_raw_index(evidence)
    if duplicate:return unavailable('duplicate_source_record_or_cycle')
    if evidence.context.get('source_nonadvancing_cycles') or evidence.context.get('swerve_source_hazards'):
        return unavailable('source_cycle_order_or_boundary_unqualified')
    groups=defaultdict(list)
    for row in evidence.rows:
        if row.get('kind')!='swerve_sample':continue
        position=row.get('module_position')
        if position not in assignments or row.get('module_id')!=assignments[position]:
            return unavailable('sample_component_assignment_mismatch','unsupported')
        if type(row.get('timestamp_ns')) is not int or not lo<=row['timestamp_ns']<=hi:
            return unavailable('sample_timestamp_outside_run')
        groups[position].append(row)
    if not groups:return unavailable('explicit_recorded_swerve_mapping_unavailable')
    metrics=[]; findings=[]; modules=[]; reasons=set()
    finding_reference_total=0;finding_reference_stored=0
    if evidence.context.get('swerve_mapping_unavailable'):reasons.add('source_mapping_has_unavailable_cycles')
    configured=any(config[key] is not None for key in LIMITS)
    for position,component in assignments.items():
        rows=groups[position]; exclusions=Counter(); segments=[]; segment=[]
        expected=0.; previous=None; previous_token=None
        for row in rows:
            token,error=_reference(row,evidence,records,cycles)
            error=error or _reason(row,config)
            if previous is not None:
                dt=(row['timestamp_ns']-previous['timestamp_ns'])/1e9
                if dt<=0:return unavailable('nonadvancing_module_timestamps')
                if previous.get('enabled') is True and row.get('enabled') is True:expected+=dt
                if not error and not previous_error:
                    if dt*1e9>config['max_gap_ns']:error='gap_exceeds_bound'
                    elif token[:2]!=previous_token[:2] or token[2]!=previous_token[2]+1:error='source_lifecycle_or_missing_cycle_boundary'
            if error:
                exclusions[error]+=1
                if segment:segments.append((segment,'gap_or_invalid'));segment=[]
                # A boundary row can seed the next segment when its own sample is valid.
                if token and not _reason(row,config) and error in ('gap_exceeds_bound','source_lifecycle_or_missing_cycle_boundary'):
                    segment=[row];error=None
            elif previous is None or previous_error:
                segment=[row]
            else:segment.append(row)
            previous,previous_token,previous_error=row,token,error
        if segment:segments.append((segment,'run_end'))
        errors=[]; seconds=0.; trace=[]; trace_total=0; held=0; unique=set()
        settling=[]; crossings=[]; jumps=[]; sustained=[]
        criteria_hits=defaultdict(list);criteria_counts=Counter();criteria_reference_totals=Counter();settled_max=None;settling_evaluated=0
        stored_event_count=0;stored_event_references=0
        event_storage={name:dict(total=0,stored=0,omitted=0,references_total=0,references_stored=0,references_omitted=0)
            for name in ('settling_events','oscillation_events','recorded_jump_events','sustained_error_intervals')}
        totals=Counter(); maxjump=None; maxrate=None; longest=0.; all_crossings=0; stable_seconds=0.
        def event(collection,name,value):
            nonlocal settled_max,settling_evaluated,stored_event_count,stored_event_references
            totals[name]+=1
            reference_total=value.get('source_references_total',len(value['source_references']))
            kind=None
            if name=='settling_events':
                value_seconds=value['settling_seconds']
                totals['settled_responses' if value_seconds is not None else 'censored_responses']+=1
                if value_seconds is not None:settled_max=value_seconds if settled_max is None else max(settled_max,value_seconds)
                limit=config['settling_limit_seconds']
                if limit is not None and (value_seconds is not None or value['observed_seconds']>=max(config['minimum_stable_seconds'],limit)):
                    settling_evaluated+=1
                if limit is not None and ((value_seconds is not None and value_seconds>limit)
                    or (value['state']=='censored' and value['observed_seconds']>=max(config['minimum_stable_seconds'],limit)
                        and value['censor_reason']!='stable_window_too_short')):
                    kind='recorded_steering_settling_limit'
            elif name=='oscillation_events':
                limit=config['oscillation_crossing_limit']
                if limit is not None and value['crossings']>=limit:kind='recorded_steering_error_crossings'
            elif name=='recorded_jump_events':kind='recorded_steering_angle_jump'
            elif name=='sustained_error_intervals':kind='recorded_steering_sustained_error'
            if kind:
                criteria_counts[kind]+=1
                criteria_reference_totals[kind]+=reference_total
                if len(criteria_hits[kind])<EVENT_LIMIT:criteria_hits[kind].append(value)
            # Thresholds and exact aggregates above use the original event. Only
            # the stored display copy spends the shared module budgets below.
            storage=event_storage[name]
            storage['total']+=1;storage['references_total']+=reference_total
            if stored_event_count<EVENT_LIMIT:
                retained=value['source_references'][:EVENT_LIMIT-stored_event_references]
                collection.append(dict(value,source_references=retained,source_references_total=reference_total,
                    source_references_omitted=reference_total-len(retained),source_references_truncated=reference_total>len(retained)))
                stored_event_count+=1;stored_event_references+=len(retained)
                storage['stored']+=1;storage['references_stored']+=len(retained)
            storage['omitted']=storage['total']-storage['stored']
            storage['references_omitted']=storage['references_total']-storage['references_stored']
        for segment,ending in segments:
            if len(segment)<2:continue
            target=wrap(segment[0]['command_angle_rad']); actual=wrap(segment[0]['measured_angle_rad'])
            pending=None; band_start=None; stable_start=segment[0]['timestamp_ns']; stable_target=target; sign=0
            stable_crossings=0; stable_refs=[]; sustained_start=None
            def finish_step(end,reason,end_reference):
                if pending is None:return
                duration=(end-pending['start'])/1e9
                settled=pending.get('settled') if duration>=config['minimum_stable_seconds'] else None
                event(settling,'settling_events',dict(start_monotonic_ns=str(pending['start']),end_monotonic_ns=str(end),
                    state='settled' if settled is not None else 'censored',
                    censor_reason=None if settled is not None else ('stable_window_too_short' if duration<config['minimum_stable_seconds'] else reason),
                    settling_seconds=settled,observed_seconds=duration,observation_end_reason=reason,
                    stable_window_qualified=duration>=config['minimum_stable_seconds'],
                    source_references=pending['refs']+[end_reference]))
            def finish_stable(end):
                nonlocal stable_seconds,all_crossings
                duration=(end-stable_start)/1e9
                if duration>=config['minimum_stable_seconds']:
                    stable_seconds+=duration;all_crossings+=stable_crossings
                    if stable_crossings:event(crossings,'oscillation_events',dict(start_monotonic_ns=str(stable_start),end_monotonic_ns=str(end),
                        crossings=stable_crossings,stable_seconds=duration,source_references=stable_refs,source_references_total=stable_crossings,source_references_truncated=stable_crossings>len(stable_refs)))
            for index,(left,right) in enumerate(zip(segment,segment[1:])):
                dt=(right['timestamp_ns']-left['timestamp_ns'])/1e9
                if index:
                    prior=segment[index-1]
                    change=_advance(prior,left,'command_angle_rad','command_speed_mps',config)
                    target+=change
                    actual+=_advance(prior,left,'measured_angle_rad','measured_speed_mps',config)
                    if abs(wrap(target-stable_target))>config['target_stability_rad']:
                        finish_stable(left['timestamp_ns']);finish_step(left['timestamp_ns'],'target_changed',left['source_reference'])
                        pending=None;band_start=None
                        stable_start=left['timestamp_ns'];stable_target=target;sign=0;stable_crossings=0;stable_refs=[]
                    if abs(change)>=config['command_step_rad']:
                        pending=dict(start=left['timestamp_ns'],refs=[left['source_reference']]);band_start=None
                error=wrap(actual-target); absolute=abs(error)
                errors.append((absolute,dt));seconds+=dt
                trace_total+=1
                ref=left['source_reference'];rightref=right['source_reference']
                unique.add((ref['measured_source_hash'],ref['measured_record_index']))
                held+=int(ref['measured_updated_in_cycle'] is False)
                if len(trace)<EVENT_LIMIT:trace.append(dict(start_monotonic_ns=str(left['timestamp_ns']),end_monotonic_ns=str(right['timestamp_ns']),
                    wrapped_error_rad=error,source_reference=ref,end_source_reference=rightref))
                if pending is not None:
                    if absolute<=config['settle_band_rad']:
                        if band_start is None:band_start=left['timestamp_ns']
                        if (right['timestamp_ns']-band_start)/1e9>=config['settle_dwell_seconds'] and 'settled' not in pending:
                            pending['settled']=(band_start-pending['start'])/1e9
                            pending['refs'].extend([ref,rightref])
                    else:band_start=None
                new_sign=1 if error>config['settle_band_rad'] else -1 if error < -config['settle_band_rad'] else 0
                if new_sign:
                    if sign and new_sign!=sign:
                        stable_crossings+=1
                        if len(stable_refs)<EVENT_LIMIT:stable_refs.append(ref)
                    sign=new_sign
                delta=_advance(left,right,'measured_angle_rad','measured_speed_mps',config)
                jump=abs(delta);rate=jump/dt
                maxjump=jump if maxjump is None else max(maxjump,jump)
                maxrate=rate if maxrate is None else max(maxrate,rate)
                if config['recorded_jump_limit_rad'] is not None and jump>config['recorded_jump_limit_rad']:
                    event(jumps,'recorded_jump_events',dict(start_monotonic_ns=str(left['timestamp_ns']),end_monotonic_ns=str(right['timestamp_ns']),
                        wrapped_jump_rad=jump,recorded_rate_rad_per_second=rate,source_references=[ref,rightref]))
                limit=config['sustained_error_limit_rad']
                if limit is not None and absolute>limit:
                    if sustained_start is None:sustained_start=left['timestamp_ns']
                    duration=(right['timestamp_ns']-sustained_start)/1e9;longest=max(longest,duration)
                else:
                    if sustained_start is not None and (left['timestamp_ns']-sustained_start)/1e9>=config['sustained_error_seconds']:
                        event(sustained,'sustained_error_intervals',dict(start_monotonic_ns=str(sustained_start),end_monotonic_ns=str(left['timestamp_ns']),source_references=[ref]))
                    sustained_start=None
            end=segment[-1]['timestamp_ns']
            # The final endpoint has no weighted duration, but can still begin a
            # step that must be explicitly censored rather than disappear.
            last_change=_advance(segment[-2],segment[-1],'command_angle_rad','command_speed_mps',config)
            if abs(wrap(target+last_change-stable_target))>config['target_stability_rad']:
                finish_step(end,'target_changed',segment[-1]['source_reference']);finish_stable(end)
                pending=None
                stable_start=end
                if abs(last_change)>=config['command_step_rad']:
                    pending=dict(start=end,refs=[segment[-1]['source_reference']])
            finish_step(end,ending,segment[-1]['source_reference']);finish_stable(end)
            if sustained_start is not None and (end-sustained_start)/1e9>=config['sustained_error_seconds']:
                event(sustained,'sustained_error_intervals',dict(start_monotonic_ns=str(sustained_start),end_monotonic_ns=str(end),source_references=[segment[-1]['source_reference']]))
        for reason in exclusions:
            if any(word in reason for word in ('source','reference','record_index','timestamp_unqualified')):
                reasons.add(reason+':'+component)
        fraction=seconds/expected if expected else 0.
        eligible=seconds>=config['minimum_eligible_seconds'] and fraction>=config['minimum_coverage']
        values={'recorded_steering_rmse_rad':(math.sqrt(sum(v*v*w for v,w in errors)/seconds) if seconds else None,'radians'),
            'recorded_steering_p95_absolute_error_rad':(weighted_percentile(errors,.95),'radians'),
            'recorded_settling_max_seconds':(settled_max,'seconds'),
            'recorded_oscillation_crossings':(all_crossings if stable_seconds else None,'crossings'),
            'recorded_angle_jump_max_rad':(maxjump,'radians'),
            'recorded_angle_rate_max_rad_per_second':(maxrate,'radians per second'),
            'longest_recorded_sustained_error_seconds':(longest if config['sustained_error_limit_rad'] is not None and seconds else None,'seconds')}
        for name,(value,unit) in values.items():
            item=metric(name,value if eligible else None,unit,sample_count=trace_total,eligible_seconds=seconds)
            item.update(module_id=component,module_position=position,physical_sample_count=None,
                unavailable_reason=None if eligible and value is not None else 'coverage_or_event_unavailable')
            metrics.append(item)
        report=dict(module_id=component,module_position=position,eligible=eligible,eligible_seconds=seconds,expected_enabled_seconds=expected,
            fraction=fraction,recorded_interval_count=trace_total,physical_sample_count=None,unique_measured_record_count=len(unique),
            held_measured_interval_count=held,exclusions=dict(exclusions),evidence_trace=trace,
            settling_events=settling,oscillation_events=crossings,recorded_jump_events=jumps,sustained_error_intervals=sustained,
            event_totals=dict(totals),event_storage=event_storage,
            stored_event_count=stored_event_count,event_count_total=sum(v['total'] for v in event_storage.values()),
            event_count_omitted=sum(v['omitted'] for v in event_storage.values()),
            event_references_total=sum(v['references_total'] for v in event_storage.values()),
            event_references_stored=stored_event_references,
            event_references_omitted=sum(v['references_omitted'] for v in event_storage.values()),
            event_storage_limit=EVENT_LIMIT,event_reference_storage_limit=EVENT_LIMIT,
            events_truncated={name:value['omitted']>0 for name,value in event_storage.items()},
            event_references_truncated=any(value['references_omitted'] for value in event_storage.values()),
            evidence_trace_total=trace_total,evidence_trace_truncated=trace_total>EVENT_LIMIT,stable_seconds=stable_seconds,**qualifiers)
        modules.append(report)
        if not eligible:reasons.add('module_coverage_insufficient:'+component);continue
        if config['settling_limit_seconds'] is not None:
            if not totals['settling_events']:reasons.add('command_step_events_unavailable:'+component)
            elif not settling_evaluated:reasons.add('settling_responses_censored_before_criterion_evaluable:'+component)
        if config['oscillation_crossing_limit'] is not None and not stable_seconds:
            reasons.add('stable_command_windows_unavailable:'+component)
        descriptions={
            'recorded_steering_settling_limit':('Recorded steering step settling exceeds the selected time limit, or an unsettled recorded step remains censored beyond that limit', 'settling_limit_seconds','seconds'),
            'recorded_steering_error_crossings':('Recorded signed steering error repeatedly crosses the hysteresis band during a stable command window', 'oscillation_crossing_limit','crossings per stable window'),
            'recorded_steering_angle_jump':('Adjacent recorded steering angles have a wrapped change above the selected limit; this is not a physical encoder fault diagnosis', 'recorded_jump_limit_rad','radians'),
            'recorded_steering_sustained_error':('Recorded wrapped steering error remains above the selected angle limit for the selected sustained duration', 'sustained_error_limit_rad','radians')}
        for kind,events in criteria_hits.items():
            description,criterion,unit=descriptions[kind]
            available=EVENT_LIMIT-finding_reference_stored
            retained_references=[]
            for event_value in events:
                retained_references.extend(event_value['source_references'][:available-len(retained_references)])
                if len(retained_references)>=available:break
            reference_total=criteria_reference_totals[kind]
            finding_reference_total+=reference_total;finding_reference_stored+=len(retained_references)
            findings.append(dict(kind=kind,severity='warning',module_id=component,module_position=position,
                observed_behavior=description+'; physical cause is unconfirmed',
                criterion=criterion,configured_limit=config[criterion],criterion_unit=unit,
                observed_summary=dict(event_count=criteria_counts[kind],maximum_recorded_settling_seconds=settled_max,
                    stable_window_crossings=all_crossings,maximum_recorded_angle_change_rad=maxjump,
                    longest_sustained_error_seconds=longest),
                intervals_ns=[[e['start_monotonic_ns'],e['end_monotonic_ns']] for e in events[:EVENT_LIMIT]],
                source_references=retained_references,source_references_total=reference_total,
                source_references_omitted=reference_total-len(retained_references),
                supporting_references_limited=reference_total>len(retained_references),
                interval_count=criteria_counts[kind],intervals_truncated=criteria_counts[kind]>len(events),
                intervals_ns_total=criteria_counts[kind],intervals_ns_truncated=criteria_counts[kind]>len(events),
                event_count=criteria_counts[kind],events_truncated=criteria_counts[kind]>EVENT_LIMIT,threshold_revision=config['threshold_revision'],baseline_version=None,comparison_cohort=None,
                eligible_seconds=seconds,sample_count=trace_total,suggested_next_check='Inspect original timing and module observations, then repeat the same supervised test',**qualifiers))
    if not configured:reasons.add('steering_finding_criteria_not_configured')
    outcome='finding' if findings else 'evaluated_no_finding' if configured and modules and all(m['eligible'] for m in modules) and not reasons else 'insufficient_data'
    return result(outcome,metrics=metrics,findings=findings,unavailable=sorted(reasons),coverage=dict(modules=modules,
        evaluated_modules=sum(m['eligible'] for m in modules),fraction=min((m['fraction'] for m in modules),default=0),
        engineering_finding_criteria_configured=configured,
        finding_reference_storage_limit=EVENT_LIMIT,finding_references_total=finding_reference_total,
        finding_references_stored=finding_reference_stored,
        finding_references_omitted=finding_reference_total-finding_reference_stored,**qualifiers))


def steering_analyzer():
    return Analyzer(Declaration('recorded-steering-observation','1',(ROBOT_FORMAT_PROFILE,),
        eligible_windows='enabled connected valid source-pinned recorded logger states with meaningful demand',
        grouping_keys=REQUIRED_CONTEXT),steering_observation)
