"""Bounded recorded-state steering dynamics for fixed streaming policy 11.

One left-held endpoint per module; display examples never determine aggregates.
No physical acquisition-time or hardware-health qualification is inferred.
"""
import copy
import math
from .analysis import metric, result, canonical
from .steering import steering_configuration, LIMITS, EVENT_LIMIT, _reason, _advance
from .swerve import MODULE_POSITIONS, REQUIRED_CONTEXT, wrap
from .stream_tracking import _moment, _rms

VERSION = 'stream-recorded-steering-1'
NUMERICS = 'integer-ns-left-held-optimization-normalized-steering-1'
ENDPOINT_POLICY = 'both_endpoints_enabled_last_eligible_boundary_1'
AVAILABILITY_POLICY = 'hard_source_authority_soft_motion_payload_coverage_and_no_clean_pass_1'
KINDS = ('settling_events', 'oscillation_events', 'recorded_jump_events', 'sustained_error_intervals')
CRITERIA = {
 'settling_events': ('recorded_steering_settling_limit', 'settling_limit_seconds', 'seconds'),
 'oscillation_events': ('recorded_steering_error_crossings', 'oscillation_crossing_limit', 'crossings per stable window'),
 'recorded_jump_events': ('recorded_steering_angle_jump', 'recorded_jump_limit_rad', 'radians'),
 'sustained_error_intervals': ('recorded_steering_sustained_error', 'sustained_error_limit_rad', 'radians')}


def initial_steering(context):
    config = steering_configuration((context.get('analysis_plan') or {}).get('steering_configuration', {}))
    missing = ['missing_steering_context:'+key for key in REQUIRED_CONTEXT if not context.get(key)]
    components = context.get('component_ids', {})
    if set(components) != set(MODULE_POSITIONS) or len(set(components.values())) != 4:
        missing.append('four_explicit_component_assignments_required')
    return dict(version=VERSION,context=copy.deepcopy(context),configuration=config,unavailable=missing,soft_unavailable=[],
        first=None,last=None,flushed=False,reference_budget=dict(count=0,bytes=0),modules=[dict(index=i,module_id=components.get(p),module_position=p,
        previous=None,active=None,forced_boundary=False,expected_ns='0',eligible_ns='0',moment=dict(scale=0.,mean=0.,weight='0'),
        recorded_interval_count=0,unique_measured_record_count=0,held_measured_interval_count=0,
        exclusions={},trace=[],events={k:[] for k in KINDS},
        event_storage={k:dict(total=0,stored=0,omitted=0,references_total=0,references_stored=0,references_omitted=0) for k in KINDS},
        stored_event_count=0,event_references_stored=0,event_totals={},criteria={k:dict(count=0,references_total=0,intervals=[],refs=[]) for k in KINDS},
        stable_ns='0',all_crossings=0,settled_max=None,settling_evaluated=0,maxjump=None,maxrate=None,longest_ns='0',
        mapped_enabled_cycles=0,required_enabled_cycles=0) for i,p in enumerate(MODULE_POSITIONS)])


def _increment(values, key, amount=1):
    values[key] = values.get(key, 0)+amount


def _retain(budget, refs):
    """One check-wide bound across traces, active windows, events and findings."""
    retained=[]
    for ref in refs:
        size=len(canonical(ref).encode())
        if budget['count']>=128 or budget['bytes']+size>384*1024:break
        retained.append(ref);budget['count']+=1;budget['bytes']+=size
    return retained


def _event(module, config, name, value, budget):
    refs=value['source_references'];total=value.get('source_references_total',len(refs))
    _increment(module['event_totals'],name)
    hit=False
    if name=='settling_events':
        seconds=value['settling_seconds'];limit=config['settling_limit_seconds']
        _increment(module['event_totals'],'settled_responses' if seconds is not None else 'censored_responses')
        if seconds is not None:module['settled_max']=seconds if module['settled_max'] is None else max(module['settled_max'],seconds)
        evaluable=seconds is not None or value['observed_seconds']>=max(config['minimum_stable_seconds'],limit or 0)
        if limit is not None and evaluable:module['settling_evaluated']+=1
        hit=limit is not None and ((seconds is not None and seconds>limit) or
            (seconds is None and evaluable and value['censor_reason']!='stable_window_too_short'))
    elif name=='oscillation_events':
        limit=config['oscillation_crossing_limit'];hit=limit is not None and value['crossings']>=limit
    else:hit=True
    if hit:
        criterion=module['criteria'][name];criterion['count']+=1;criterion['references_total']+=total
        if len(criterion['intervals'])<EVENT_LIMIT:criterion['intervals'].append([value['start_monotonic_ns'],value['end_monotonic_ns']])
        criterion['refs'].extend(_retain(budget,refs[:EVENT_LIMIT-len(criterion['refs'])]))
    storage=module['event_storage'][name];storage['total']+=1;storage['references_total']+=total
    if module['stored_event_count']<EVENT_LIMIT:
        retained=_retain(budget,refs[:EVENT_LIMIT-module['event_references_stored']])
        module['events'][name].append(dict(value,source_references=retained,source_references_total=total,
            source_references_omitted=total-len(retained),source_references_truncated=total>len(retained)))
        storage['stored']+=1;storage['references_stored']+=len(retained)
        module['stored_event_count']+=1;module['event_references_stored']+=len(retained)
    storage['omitted']=storage['total']-storage['stored']
    storage['references_omitted']=storage['references_total']-storage['references_stored']


def _finish_windows(module, config, end, reason, ref, budget):
    active=module['active']
    if active is None:return
    pending=active['pending']
    if pending is not None:
        duration=(end-pending['start'])/1e9;qualified=duration>=config['minimum_stable_seconds']
        settled=pending.get('settled') if qualified else None
        _event(module,config,'settling_events',dict(start_monotonic_ns=str(pending['start']),end_monotonic_ns=str(end),
            state='settled' if settled is not None else 'censored',censor_reason=None if settled is not None else
            ('stable_window_too_short' if not qualified else reason),settling_seconds=settled,observed_seconds=duration,
            observation_end_reason=reason,stable_window_qualified=qualified,source_references=pending['refs']+[ref],source_references_total=pending['reference_total']+1),budget)
    duration=end-active['stable_start']
    if duration/1e9>=config['minimum_stable_seconds']:
        module['stable_ns']=str(int(module['stable_ns'])+duration);module['all_crossings']+=active['crossings']
        if active['crossings']:
            _event(module,config,'oscillation_events',dict(start_monotonic_ns=str(active['stable_start']),end_monotonic_ns=str(end),
                crossings=active['crossings'],stable_seconds=duration/1e9,source_references=active['crossing_refs'],source_references_total=active['crossings']),budget)
    active.update(pending=None,band_start=None,stable_start=end,stable_target=active['target'],sign=0,crossings=0,crossing_refs=[])


def _finish_sustained(module, config, end, ref, budget):
    active=module['active']
    if active and active['sustained_start'] is not None:
        start=active['sustained_start']
        if (end-start)/1e9>=config['sustained_error_seconds']:
            _event(module,config,'sustained_error_intervals',dict(start_monotonic_ns=str(start),end_monotonic_ns=str(end),source_references=[ref]),budget)
        active['sustained_start']=None


def _close(module, config, reason, budget):
    previous=module['previous']
    if module['active'] is not None and previous is not None:
        end=previous['timestamp_ns'];ref=previous['source_reference']
        _finish_windows(module,config,end,reason,ref,budget);_finish_sustained(module,config,end,ref,budget)
    module['active']=None


def boundary(state, reason):
    """Censor immediately even when a malformed original row emits no cycle."""
    for module in state['modules']:
        _close(module,state['configuration'],reason,state['reference_budget']);module['forced_boundary']=True
        _increment(module['exclusions'],reason)


def _seed(module, sample):
    target=wrap(sample['command_angle_rad'])
    module['active']=dict(target=target,actual=wrap(sample['measured_angle_rad']),stable_target=target,
        stable_start=sample['timestamp_ns'],sign=0,crossings=0,crossing_refs=[],pending=None,band_start=None,sustained_start=None)


def _move_endpoint(module, sample, config, budget):
    active=module['active'];previous=module['previous']
    change=_advance(previous,sample,'command_angle_rad','command_speed_mps',config)
    active['target']+=change;active['actual']+=_advance(previous,sample,'measured_angle_rad','measured_speed_mps',config)
    if abs(wrap(active['target']-active['stable_target']))>config['target_stability_rad']:
        _finish_windows(module,config,sample['timestamp_ns'],'target_changed',sample['source_reference'],budget)
    if abs(change)>=config['command_step_rad']:
        active['pending']=dict(start=sample['timestamp_ns'],refs=_retain(budget,[sample['source_reference']]),reference_total=1);active['band_start']=None


def measurement_identity(sample):
    ref=sample['source_reference'];rows=ref.get('module_measurement_references')
    if rows:
        return [ref['source_hash'],[[r.get(k) for k in ('field','record_index','entry_id','entry_generation')] for r in rows]]
    return [ref.get('measured_source_hash',ref.get('source_hash')),ref.get('measured_record_index')]


def consume_sample(state, sample, contribute, unique):
    """Callbacks persist an exact weighted error and a unique original record/pair."""
    if state['flushed']:raise ValueError('steering_already_flushed')
    module=state['modules'][sample['module_index']];config=state['configuration'];previous=module['previous']
    budget=state['reference_budget'];stamp=sample['timestamp_ns'];state['first']=str(stamp) if state['first'] is None else state['first'];state['last']=str(stamp)
    if type(sample.get('enabled')) is not bool and 'enabled_status_unknown_or_invalid' not in state['unavailable']:
        state['unavailable'].append('enabled_status_unknown_or_invalid')
    if sample.get('enabled') is True:
        module['required_enabled_cycles']+=1
        if sample.get('validity')=='valid':module['mapped_enabled_cycles']+=1
    current_reason=_reason(sample,config)
    if current_reason in ('invalid_or_unknown','disconnected_or_unknown','missing_or_nonfinite_values'):
        if current_reason not in state['soft_unavailable']:state['soft_unavailable'].append(current_reason)
    elif current_reason in ('sample_contract_unsupported','units_unsupported'):
        if current_reason not in state['unavailable']:state['unavailable'].append(current_reason)
    if previous is None:
        if current_reason is None:_seed(module,sample)
    else:
        delta=stamp-previous['timestamp_ns']
        if delta>0 and previous.get('enabled') is True and sample.get('enabled') is True:
            module['expected_ns']=str(int(module['expected_ns'])+delta)
        reason=('source_lifecycle_or_missing_cycle_boundary' if module['forced_boundary'] else 'nonadvancing_timestamp' if delta<=0 else 'gap_exceeds_bound' if delta>config['max_gap_ns'] else
            'source_lifecycle_or_missing_cycle_boundary' if previous['boundary']!=sample['boundary'] else
            _reason(previous,config) or current_reason)
        if reason:
            _increment(module['exclusions'],reason);_close(module,config,'gap_or_invalid',budget)
            if current_reason is None:_seed(module,sample)
        else:
            if module['active'] is None:_seed(module,previous)
            active=module['active'];error=wrap(active['actual']-active['target']);absolute=abs(error)
            module['eligible_ns']=str(int(module['eligible_ns'])+delta);module['recorded_interval_count']+=1
            _moment(module['moment'],error,delta);contribute(module['index'],absolute,delta)
            module['unique_measured_record_count']+=int(unique(module['index'],measurement_identity(previous)))
            ref=previous['source_reference'];rightref=sample['source_reference'];origins=ref.get('module_measurement_references')
            held=any(str(r.get('cycle_timestamp_ns'))!=str(previous['timestamp_ns']) for r in origins) if origins else ref.get('measured_updated_in_cycle') is False
            module['held_measured_interval_count']+=int(held)
            retained=_retain(budget,[ref,rightref]) if len(module['trace'])<EVENT_LIMIT else []
            if len(retained)==2:module['trace'].append(dict(start_monotonic_ns=str(previous['timestamp_ns']),
                end_monotonic_ns=str(stamp),wrapped_error_rad=error,source_reference=retained[0],end_source_reference=retained[1],weight_ns=str(delta)))
            pending=active['pending']
            if pending is not None:
                if absolute<=config['settle_band_rad']:
                    if active['band_start'] is None:active['band_start']=previous['timestamp_ns']
                    if (stamp-active['band_start'])/1e9>=config['settle_dwell_seconds'] and 'settled' not in pending:
                        pending['settled']=(active['band_start']-pending['start'])/1e9;pending['refs'].extend(_retain(budget,[ref,rightref]));pending['reference_total']+=2
                else:active['band_start']=None
            sign=1 if error>config['settle_band_rad'] else -1 if error < -config['settle_band_rad'] else 0
            if sign:
                if active['sign'] and sign!=active['sign']:
                    active['crossings']+=1
                    if len(active['crossing_refs'])<EVENT_LIMIT:active['crossing_refs'].extend(_retain(budget,[ref]))
                active['sign']=sign
            jump=abs(_advance(previous,sample,'measured_angle_rad','measured_speed_mps',config));rate=jump/(delta/1e9)
            module['maxjump']=jump if module['maxjump'] is None else max(module['maxjump'],jump)
            module['maxrate']=rate if module['maxrate'] is None else max(module['maxrate'],rate)
            if config['recorded_jump_limit_rad'] is not None and jump>config['recorded_jump_limit_rad']:
                _event(module,config,'recorded_jump_events',dict(start_monotonic_ns=str(previous['timestamp_ns']),end_monotonic_ns=str(stamp),
                    wrapped_jump_rad=jump,recorded_rate_rad_per_second=rate,source_references=[ref,rightref]),budget)
            limit=config['sustained_error_limit_rad']
            if limit is not None and absolute>limit:
                if active['sustained_start'] is None:active['sustained_start']=previous['timestamp_ns']
                module['longest_ns']=str(max(int(module['longest_ns']),stamp-active['sustained_start']))
            else:_finish_sustained(module,config,previous['timestamp_ns'],ref,budget)
            _move_endpoint(module,sample,config,budget)
    module['forced_boundary']=False
    module['previous']=copy.deepcopy(sample)


def flush(state):
    """Checkpoint this bounded EOF subphase before percentile finalization."""
    if not state['flushed']:
        for module in state['modules']:_close(module,state['configuration'],'run_end',state['reference_budget'])
        state['flushed']=True


def finish_steering(state, descriptor, final_quantiles):
    if not state['flushed']:raise ValueError('steering_final_endpoint_not_flushed')
    context=state['context'];config=state['configuration'];reasons=list(state['unavailable'])
    if descriptor.get('full_source_verified') is not True:reasons.append('stream_sources_not_fully_verified')
    if context.get('completeness')!='complete':reasons.append('complete_run_required_for_steering_coverage')
    if state['first']!=context['start_monotonic_ns'] or state['last']!=context['end_monotonic_ns']:reasons.append('whole_run_swerve_state_coverage_required')
    hazards=descriptor.get('source_hazards',{})
    if hazards.get('total',0)>len(hazards.get('references',[])) or any(r.get('kind')!='unsupported_source_types_present' for r in hazards.get('references',[])):
        reasons.append('full_source_order_or_reference_unqualified')
    qualifiers=dict(physical_acquisition_time_qualified=False,physical_health_qualified=False,baseline_eligible=False,
        observation_basis='recorded_or_held_logger_state',source_type=context['stream_source_type'],thresholds_qualified_on_hardware=False)
    qualification=tuple(reasons);reasons+=state['soft_unavailable'];configured=any(config[k] is not None for k in LIMITS)
    modules=[];metrics=[];findings=[];finding_total=0;finding_stored=0
    for module in state['modules']:
        seconds=int(module['eligible_ns'])/1e9;expected=int(module['expected_ns'])/1e9;fraction=seconds/expected if expected else 0.
        eligible=not qualification and seconds>=config['minimum_eligible_seconds'] and fraction>=config['minimum_coverage']
        values={'recorded_steering_rmse_rad':(_rms(module['moment']),'radians'),
            'recorded_steering_p95_absolute_error_rad':(final_quantiles[str(module['index'])+':recorded_steering'],'radians'),
            'recorded_settling_max_seconds':(module['settled_max'],'seconds'),
            'recorded_oscillation_crossings':(module['all_crossings'] if int(module['stable_ns']) else None,'crossings'),
            'recorded_angle_jump_max_rad':(module['maxjump'],'radians'),
            'recorded_angle_rate_max_rad_per_second':(module['maxrate'],'radians per second'),
            'longest_recorded_sustained_error_seconds':(int(module['longest_ns'])/1e9 if config['sustained_error_limit_rad'] is not None and seconds else None,'seconds')}
        for name,(value,unit) in values.items():
            item=metric(name,value if eligible else None,unit,sample_count=module['recorded_interval_count'],eligible_seconds=seconds)
            item.update(module_id=module['module_id'],module_position=module['module_position'],physical_sample_count=None,
                unavailable_reason=None if eligible and value is not None else 'coverage_or_event_unavailable');metrics.append(item)
        storage=module['event_storage'];total=sum(v['total'] for v in storage.values());refs=sum(v['references_total'] for v in storage.values())
        modules.append(dict(module_id=module['module_id'],module_position=module['module_position'],eligible=bool(eligible),eligible_seconds=seconds,
            expected_enabled_seconds=expected,fraction=fraction,mapped_enabled_cycles=module['mapped_enabled_cycles'],required_enabled_cycles=module['required_enabled_cycles'],recorded_interval_count=module['recorded_interval_count'],physical_sample_count=None,
            unique_measured_record_count=module['unique_measured_record_count'],held_measured_interval_count=module['held_measured_interval_count'],
            measured_identity_basis='original_measurement_record_or_scalar_velocity_angle_pair',exclusions=module['exclusions'],evidence_trace=module['trace'],
            evidence_trace_total=module['recorded_interval_count'],evidence_trace_retained=len(module['trace']),
            evidence_trace_omitted=module['recorded_interval_count']-len(module['trace']),evidence_trace_truncated=module['recorded_interval_count']>len(module['trace']),
            evidence_trace_basis='bounded_examples_only; streamed_incident_proof_unavailable',event_totals=module['event_totals'],event_storage=storage,
            stored_event_count=module['stored_event_count'],event_count_total=total,event_count_omitted=total-module['stored_event_count'],
            event_references_total=refs,event_references_stored=module['event_references_stored'],event_references_omitted=refs-module['event_references_stored'],
            event_storage_limit=EVENT_LIMIT,event_reference_storage_limit=EVENT_LIMIT,events_truncated={k:v['omitted']>0 for k,v in storage.items()},
            event_references_truncated=refs>module['event_references_stored'],stable_seconds=int(module['stable_ns'])/1e9,**module['events'],**qualifiers))
        if not eligible:reasons.append('module_coverage_insufficient:'+str(module['module_id']));continue
        if config['settling_limit_seconds'] is not None:
            if not module['event_totals'].get('settling_events'):reasons.append('command_step_events_unavailable:'+module['module_id'])
            elif not module['settling_evaluated']:reasons.append('settling_responses_censored_before_criterion_evaluable:'+module['module_id'])
        if config['oscillation_crossing_limit'] is not None and not int(module['stable_ns']):reasons.append('stable_command_windows_unavailable:'+module['module_id'])
        for name,criterion in module['criteria'].items():
            if not criterion['count']:continue
            kind,key,unit=CRITERIA[name];retained=criterion['refs'][:EVENT_LIMIT-finding_stored]
            finding_stored+=len(retained);finding_total+=criterion['references_total']
            findings.append(dict(kind=kind,severity='warning',module_id=module['module_id'],module_position=module['module_position'],
                observed_behavior='Recorded steering observations exceed the selected criterion; physical cause is unconfirmed',criterion=key,
                configured_limit=config[key],criterion_unit=unit,intervals_ns=criterion['intervals'],interval_count=criterion['count'],
                intervals_ns_total=criterion['count'],intervals_ns_truncated=criterion['count']>len(criterion['intervals']),intervals_truncated=criterion['count']>len(criterion['intervals']),
                event_count=criterion['count'],events_truncated=criterion['count']>len(criterion['intervals']),source_references=retained,
                source_references_total=criterion['references_total'],source_references_omitted=criterion['references_total']-len(retained),
                supporting_references_limited=criterion['references_total']>len(retained),threshold_revision=config['threshold_revision'],baseline_version=None,
                comparison_cohort=None,eligible_seconds=seconds,sample_count=module['recorded_interval_count'],
                observed_summary=dict(event_count=criterion['count'],maximum_recorded_settling_seconds=module['settled_max'],stable_window_crossings=module['all_crossings'],
                    maximum_recorded_angle_change_rad=module['maxjump'],longest_sustained_error_seconds=int(module['longest_ns'])/1e9),
                suggested_next_check='Inspect original timing and module observations, then repeat the same supervised test',**qualifiers))
    if not configured:reasons.append('steering_finding_criteria_not_configured')
    outcome='finding' if findings else 'evaluated_no_finding' if configured and all(m['eligible'] for m in modules) and not reasons else 'insufficient_data'
    return result(outcome,metrics=metrics,findings=findings,unavailable=sorted(set(reasons)),coverage=dict(modules=modules,
        evaluated_modules=sum(m['eligible'] for m in modules),fraction=min((m['fraction'] for m in modules),default=0),
        engineering_finding_criteria_configured=configured,finding_reference_storage_limit=EVENT_LIMIT,
        finding_references_total=finding_total,finding_references_stored=finding_stored,finding_references_omitted=finding_total-finding_stored,
        availability_policy=AVAILABILITY_POLICY,reference_storage_limit=128,reference_byte_storage_limit=384*1024,numerical_policy=NUMERICS,endpoint_policy=ENDPOINT_POLICY,streamed_incident_proof_available=False,**qualifiers))
