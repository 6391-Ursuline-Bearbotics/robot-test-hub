"""Pipeline-10 full-source tracking with exact disk-backed integer-weight p95."""
import copy
import math
from .analysis import metric,result,identity
from . import stream_motion_mapper as motion
from . import stream_tracking_store as work
from .stream_status import RecordedStatusReducer
from .swerve import MODULE_POSITIONS,REQUIRED_CONTEXT,_config,_sample_reason,wrap,compare_baseline

VERSION='stream-swerve-tracking-1'
PIPELINE='automatic-reports-10'
NUMERICS='scaled-weighted-second-moment-ns-1'
ENDPOINT_POLICY='enabled_motion_with_known_disabled_terminal_cycle_1'
EXAMPLES=128

def add_reason(state,reason):
    if reason not in state['unavailable']:state['unavailable'].append(reason)

def _moment(state,value,weight):
    old=int(state['weight']);total=old+weight;scale=max(state['scale'],abs(value))
    state['mean']=(state['mean']*(state['scale']/scale)**2*(old/total)+(value/scale)**2*(weight/total)) if scale else 0.
    state.update(scale=scale,weight=str(total))

def _rms(state):
    return state['scale']*math.sqrt(max(0.,min(1.,state['mean']))) if int(state['weight']) else None

def initial_tracking(context):
    source=context.get('stream_motion_measurement_source','measured_array')
    if source not in ('measured_array','module_io_if_array_absent'):raise ValueError('stream_motion_measurement_source_invalid')
    config=_config((context.get('analysis_plan') or {}).get('swerve_configuration',{}))
    state=dict(context=copy.deepcopy(context),configuration=config,mapper=motion.initial(),work=work.initial(),
               cycles=0,first=None,last=None,trace_retained=0,interval_retained=0,unavailable=list(context.get('analysis_plan_unavailable',[])),modules=[])
    if context.get('analysis_plan') is None:add_reason(state,'analysis_plan_missing_or_ambiguous')
    for key in REQUIRED_CONTEXT:
        if not context.get(key):add_reason(state,'missing_context:'+key)
    components=context.get('component_ids',{})
    if set(components)!=set(MODULE_POSITIONS) or len(set(components.values()))!=4:
        add_reason(state,'four_distinct_physical_components_required')
    for index,position in enumerate(MODULE_POSITIONS):
        state['modules'].append(dict(index=index,module_id=components.get(position),module_position=position,previous=None,
            expected_ns='0',eligible_ns='0',sample_count=0,exclusions={},trace=[],trace_total=0,
            drive=dict(scale=0.,mean=0.,weight='0'),steering=dict(scale=0.,mean=0.,weight='0'),demand=dict(scale=0.,mean=0.,weight='0'),
            consecutive_ns='0',longest_ns='0',interval_start=None,intervals=[],interval_total=0,active_interval=False,
            first=None,last=None,mapped_cycles=0,required_enabled_cycles=0))
    return state

def consume_sample(db,sid,gen,state,sample,row,chunk):
    """Explicit normalized sample contract; ordinary stream input comes via motion mapper."""
    module=state['modules'][sample['module_index']];previous=module['previous'];config=state['configuration']
    stamp=sample['timestamp_ns']
    if type(sample.get('enabled')) is not bool:add_reason(state,'enabled_status_unknown_or_invalid')
    module['first']=str(stamp) if module['first'] is None else module['first'];module['last']=str(stamp)
    if sample.get('enabled') is True:
        module['required_enabled_cycles']+=1
        if sample.get('validity')=='valid':module['mapped_cycles']+=1
    if previous is not None:
        delta=stamp-previous['timestamp_ns']
        if delta>0 and previous.get('enabled') is True:module['expected_ns']=str(int(module['expected_ns'])+delta)
        reason=('nonadvancing_timestamp' if delta<=0 else 'recording_gap' if delta>config['max_gap_ns'] else
                'source_lifecycle_boundary' if previous['boundary']!=sample['boundary'] else
                _sample_reason(previous,config) or _sample_reason(sample,config))
        error=angle=None
        if reason is None:
            error=previous['measured_speed_mps']-previous['command_speed_mps']
            angle=wrap(wrap(previous['measured_angle_rad'])-wrap(previous['command_angle_rad']))
            if not math.isfinite(error) or not math.isfinite(angle):reason='nonfinite_error_difference'
        if reason:
            module['exclusions'][reason]=module['exclusions'].get(reason,0)+1
            module.update(consecutive_ns='0',interval_start=None,active_interval=False)
        else:
            module['eligible_ns']=str(int(module['eligible_ns'])+delta);module['sample_count']+=1
            _moment(module['drive'],error,delta);_moment(module['steering'],angle,delta);_moment(module['demand'],previous['command_speed_mps'],delta)
            for name,value in (('drive',abs(error)),('steering',abs(angle))):
                work.contribute(db,sid,gen,state['work'],chunk,row,sample['module_index'],name,value,delta)
            trace=dict(start_monotonic_ns=str(previous['timestamp_ns']),end_monotonic_ns=str(stamp),
                       drive_error_mps=error,steering_error_rad=angle,weight_seconds=delta/1e9,weight_ns=str(delta),
                       source_reference=previous['source_reference'])
            module['trace_total']+=1
            if state['trace_retained']<EXAMPLES:
                module['trace'].append(trace);state['trace_retained']+=1
            limit=config['drive_error_limit_mps']
            if limit is not None and abs(error)>limit:
                if module['interval_start'] is None:module['interval_start']=str(previous['timestamp_ns'])
                module['consecutive_ns']=str(int(module['consecutive_ns'])+delta)
                module['longest_ns']=str(max(int(module['longest_ns']),int(module['consecutive_ns'])))
                if int(module['consecutive_ns'])/1e9>=config['sustained_error_seconds']:
                    if not module['active_interval']:
                        module['interval_total']+=1;module['active_interval']=True
                        if state['interval_retained']<EXAMPLES:
                            module['intervals'].append([module['interval_start'],str(stamp)]);state['interval_retained']+=1
                    elif module['intervals'] and module['intervals'][-1][0]==module['interval_start']:
                        module['intervals'][-1][1]=str(stamp)
            else:module.update(consecutive_ns='0',interval_start=None,active_interval=False)
    module['previous']=copy.deepcopy(sample)

def finish_tracking(state,descriptor,final):
    context=state['context'];config=state['configuration'];reasons=list(state['unavailable'])
    reasons+=state['mapper']['errors']
    hazards=descriptor.get('source_hazards',{})
    if hazards.get('total',0)>len(hazards.get('references',[])) or any(r.get('kind')!='unsupported_source_types_present' for r in hazards.get('references',[])):
        reasons.append('full_source_order_or_reference_unqualified')
    if descriptor.get('full_source_verified') is not True:reasons.append('stream_sources_not_fully_verified')
    if context.get('completeness')!='complete':reasons.append('complete_run_required_for_tracking')
    if state['first']!=context['start_monotonic_ns'] or state['last']!=context['end_monotonic_ns']:
        reasons.append('whole_run_swerve_state_coverage_required')
    source_type=context['stream_source_type']
    ideal=(context.get('analysis_plan') or {}).get('ideal_simulation_cycle_policy') is True and source_type=='simulation'
    if not ideal:reasons.append('physical_acquisition_time_not_qualified')
    if (context.get('analysis_plan') or {}).get('ideal_simulation_cycle_policy') and not ideal:
        reasons.append('ideal_simulation_policy_not_applicable_to_source')
    qualification=tuple(reasons)
    reports=[];metrics=[];findings=[]
    for module in state['modules']:
        seconds=int(module['eligible_ns'])/1e9;expected=int(module['expected_ns'])/1e9
        fraction=seconds/expected if expected else 0.
        eligible=bool(not qualification and seconds>=config['minimum_eligible_seconds'] and fraction>=config['minimum_coverage'] and module['mapped_cycles']==module['required_enabled_cycles'])
        if not eligible:reasons_for_module=['module_coverage_insufficient:'+str(module['module_id'])]
        else:reasons_for_module=[]
        drive,steering,demand=(_rms(module[name]) for name in ('drive','steering','demand'))
        normalized=drive/demand if demand else None
        if normalized is not None and not math.isfinite(normalized):
            eligible=False;normalized=None;reasons_for_module.append('normalized_drive_rmse_nonfinite')
        values={'drive_rmse_mps':(drive,'meters per second'),
                'drive_p95_absolute_error_mps':(final['quantiles'][str(module['index'])+':drive'],'meters per second'),
                'steering_rmse_rad':(steering,'radians'),
                'steering_p95_absolute_error_rad':(final['quantiles'][str(module['index'])+':steering'],'radians'),
                'normalized_drive_rmse':(normalized,'fraction'),
                'longest_sustained_drive_error_seconds':(int(module['longest_ns'])/1e9,'seconds')}
        for name,(value,unit) in values.items():
            item=metric(name,value if eligible else None,unit,sample_count=module['sample_count'],eligible_seconds=seconds)
            item.update(module_id=module['module_id'],module_position=module['module_position'],unavailable_reason=None if eligible else 'minimum_duration_coverage_or_qualification_not_met')
            metrics.append(item)
        count=module['trace_total'];retained=len(module['trace'])
        reports.append(dict(module_id=module['module_id'],module_position=module['module_position'],eligible=eligible,
            eligible_seconds=seconds,expected_enabled_seconds=expected,fraction=fraction,sample_count=module['sample_count'],
            mapped_enabled_cycles=module['mapped_cycles'],required_enabled_cycles=module['required_enabled_cycles'],
            exclusions=module['exclusions'],evidence_trace=module['trace'],evidence_trace_total=count,
            evidence_trace_retained=retained,evidence_trace_omitted=count-retained,evidence_trace_truncated=count>retained,
            evidence_trace_basis='bounded_examples_only; streamed_incident_proof_unavailable'))
        if eligible:
            common=dict(severity='warning',module_id=module['module_id'],module_position=module['module_position'],
                sample_count=module['sample_count'],eligible_seconds=seconds,source_hashes=descriptor['source_hashes'],
                threshold_revision=config['threshold_revision'],baseline_version=None,physical_cause='unconfirmed',
                suggested_next_check='Review timing, voltage, test conditions and original records; repeat a supervised test')
            if module['interval_total']:
                findings.append(dict(common,kind='sustained_drive_tracking_error',
                    observed_behavior='Measured wheel velocity differs from final IO command over sustained qualified intervals',
                    intervals_ns=module['intervals'],intervals_total=module['interval_total'],intervals_retained=len(module['intervals']),
                    intervals_omitted=module['interval_total']-len(module['intervals']),intervals_truncated=module['interval_total']>len(module['intervals'])))
            if config['steering_error_limit_rad'] is not None and steering>config['steering_error_limit_rad']:
                findings.append(dict(common,kind='steering_tracking_error',observed_behavior='Wrapped steering RMSE exceeds configured engineering limit',intervals_ns=[[module['first'],module['last']]]))
        for reason in reasons_for_module:
            if reason not in reasons:reasons.append(reason)
    supported=sum(m['eligible'] for m in reports)
    comparisons,baseline_findings,baseline_reasons=compare_baseline(metrics,context,source_type,context.get('approved_baseline'),config)
    # Historical tuple baselines are a different numerical/selection policy.
    selected=(context.get('analysis_plan') or {}).get('approved_baseline_id')
    if selected:
        reasons.append('selected_baseline_stream_tracking_policy_incompatible')
        comparisons=[];baseline_findings=[]
    findings+=baseline_findings;reasons+=baseline_reasons
    engineering=config['drive_error_limit_mps'] is not None or config['steering_error_limit_rad'] is not None
    baseline_criteria=bool(config['threshold_revision'] and comparisons)
    if not engineering and not baseline_criteria:reasons.append('tracking_finding_criteria_not_configured_or_available')
    if supported!=4:reasons.append('all_four_modules_require_qualified_coverage')
    outcome='finding' if findings else 'evaluated_no_finding' if supported==4 and (engineering or baseline_criteria) else 'insufficient_data'
    if selected or supported!=4:outcome='insufficient_data'
    return result(outcome,metrics=metrics,findings=findings,unavailable=sorted(set(reasons)),coverage=dict(modules=reports,
        evaluated_modules=supported,fraction=min((m['fraction'] for m in reports),default=0),
        engineering_finding_criteria_configured=engineering,baseline_finding_criteria_evaluated=baseline_criteria,baseline_comparisons=comparisons,
        physical_acquisition_freshness_qualified=False,ideal_simulation_cycle_policy_applied=ideal,
        thresholds_qualified_on_hardware=False,quantile_policy='first_error_with_100_cumulative_ns_ge_95_total_ns; ties_by_contribution_sequence',
        numerical_policy=NUMERICS,endpoint_policy=ENDPOINT_POLICY,motion_mapper_version=motion.VERSION,measurement_contract=motion.MEASUREMENT_CONTRACT,pre_run_motion_invalidations=state['mapper']['pre_run_invalidations'],streamed_incident_proof_available=False))

class TrackingReducer:
    reducer_id='recorded-status-tracking'
    version='stream-recorded-status-tracking-1'
    pipeline_version=PIPELINE
    @property
    def context_versions(self):
        return {**RecordedStatusReducer().context_versions,'stream_motion_mapper_version':motion.VERSION,
                'stream_tracking_version':VERSION,'stream_tracking_numerics':NUMERICS,'stream_tracking_work_version':work.VERSION,'stream_motion_measurement_contract':motion.MEASUREMENT_CONTRACT,'stream_motion_endpoint_policy':ENDPOINT_POLICY}
    def initial(self,context):
        for key,value in self.context_versions.items():
            if context.get(key)!=value:raise ValueError('stream_tracking_version_mismatch')
        return dict(version=self.version,status=RecordedStatusReducer().initial(context),tracking=initial_tracking(context))
    def consume_chunk(self,db,sid,gen,state,row,chunk):
        RecordedStatusReducer().consume(db,sid,gen,state['status'],row)
        tracking=state['tracking'];snapshot=motion.consume(db,sid,gen,tracking['mapper'],row)
        if snapshot is None:return state
        tracking['cycles']+=1;stamp=snapshot['cycle']['timestamp_ns']
        tracking['first']=stamp if tracking['first'] is None else tracking['first'];tracking['last']=stamp
        samples,reasons=motion.samples(snapshot,tracking['context'],tracking['context']['stream_source_type'])
        for reason in reasons:add_reason(tracking,reason)
        for sample in samples:consume_sample(db,sid,gen,tracking,sample,row,chunk)
        return state
    def validation_initial(self):return work.validation_initial()
    def validate_step(self,db,sid,gen,state,validation,limit,deadline,clock):
        return work.validate_step(db,sid,gen,state['tracking']['work'],validation,limit,deadline,clock)
    def finalize_step(self,db,sid,gen,state,limit,deadline,clock):
        return work.finalize_step(db,sid,gen,state['tracking']['work'],limit,deadline,clock)
    def publication_check(self,db,sid,gen,state):
        work.assert_revision(db,sid,gen,state['tracking']['work'])
    def finish(self,state,descriptor):
        output=RecordedStatusReducer().finish(state['status'],descriptor)
        output['stream_result_format']='stream-tracking-results-1'
        output['checks']['swerve-tracking']=finish_tracking(state['tracking'],descriptor,state['finalization'])
        return output
