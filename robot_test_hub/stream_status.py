"""Incremental recorded Phoenix/SystemCore observations; physical health stays unknown."""
import copy
from .analysis import Evidence,metric,result,identity
from .wpilog import PROFILE
from . import phoenix_status as phoenix
from . import system_status as system
from . import stream_mapper as mapper
from .stream_quality import RecordingQualityReducer,_unique

PHOENIX_VERSION='stream-phoenix-status-1'
SYSTEM_VERSION='stream-system-status-1'
REFERENCE_LIMIT=128


def _add(values,item):
    if item not in values:values.append(item)


def _identity(row):return [row['source_sha256'],row['record_index'],row['entry_id'],row['entry_generation']]


def _warning(db,sid,gen,state,kind,signal,identity,references,**extra):
    if not _unique(db,sid,gen,'system-warning',[kind,signal,identity]):return
    key=kind+':'+signal
    finding=state['warnings'].setdefault(key,dict(kind=kind,signal=signal,severity='warning',
        observed_behavior=('Recorded or held SystemCore '+signal+' fault flag is true; current/latched semantics and physical cause are unqualified' if kind=='recorded_system_fault_true' else 'Recorded SystemCore '+signal+' counter '+('increased' if kind.endswith('increased') else 'decreased')+' between compatible logger observations; this is not a physical event count or reset diagnosis'),
        criterion='exact_recorded_boolean_true' if kind=='recorded_system_fault_true' else 'qualified_recorded_int64_change',
        criterion_revision=system.OBSERVATION_PROFILE,observations=0,source_references=[],physical_health_qualified=False,
        next_check='Inspect the original source interval and missing coverage before a supervised electrical check'))
    finding['observations']+=1
    cost=sum(isinstance(value,dict) for value in references.values())
    if state['reference_count']+cost<=REFERENCE_LIMIT-len(system.FIELDS):
        finding['source_references'].append(dict(**references,**extra));state['reference_count']+=cost


class RecordedStatusReducer:
    reducer_id='recorded-status'
    version='stream-recorded-status-1'
    pipeline_version='automatic-reports-9'
    @property
    def context_versions(self):
        return {'stream_mapper_version':mapper.VERSION,'stream_phoenix_status_version':PHOENIX_VERSION,'stream_system_status_version':SYSTEM_VERSION}

    def initial(self,context):
        for key,version in (('stream_mapper_version',mapper.VERSION),('stream_phoenix_status_version',PHOENIX_VERSION),('stream_system_status_version',SYSTEM_VERSION)):
            if key in context and context[key]!=version:raise ValueError('stream_status_version_mismatch')
        quality=RecordingQualityReducer().initial(context)
        return dict(version=self.version,context={key:context[key] for key in ('start_monotonic_ns','end_monotonic_ns','completeness','component_ids') if key in context},
            quality=quality,mapper=mapper.initial(context),cycles=0,min_stamp=None,max_stamp=None,
            phoenix=dict(summaries=None,warnings={},unavailable=[],reference_count=0,last_sequences={},qualification_epochs={}),
            system=dict(fields={field:dict(field=field,covered_cycles=0,held_cycles=0,distinct_records=0,latest_reference=None) for field in sorted(system.FIELDS)},
                complete_cycles=0,baselines={},previous_voltage=None,boundary=None,epochs={},floor=None,
                minimum=None,maximum=None,voltage_count=0,weighted_count=0,weight_ns='0',mean=None,scale=0.,scaled_mean=0.,warnings={},unavailable=[],reference_count=0))

    def consume(self,db,stream_id,generation,state,row):
        RecordingQualityReducer().consume(db,stream_id,generation,state['quality'],row)
        snapshot=mapper.consume(db,stream_id,generation,state['mapper'],row)
        if snapshot is None:return state
        cycle=snapshot['cycle'];stamp=system._ns(cycle['timestamp_ns'])
        state['cycles']+=1
        state['min_stamp']=str(stamp) if state['min_stamp'] is None else str(min(int(state['min_stamp']),stamp))
        state['max_stamp']=str(stamp) if state['max_stamp'] is None else str(max(int(state['max_stamp']),stamp))
        self._phoenix(db,stream_id,generation,state,snapshot)
        self._system(db,stream_id,generation,state,snapshot)
        return state

    def _phoenix(self,db,sid,gen,state,snapshot):
        cycle=snapshot['cycle'];source=cycle['source_sha256'];p=state['phoenix']
        # The old source-reviewed reader sees only one bounded current snapshot.
        # All history, deduplication and sequence accounting live in this reducer.
        context={**state['context'],'start_monotonic_ns':cycle['timestamp_ns'],'end_monotonic_ns':cycle['timestamp_ns']}
        rows=tuple(sorted((value for key,value in snapshot['held'].items() if key in phoenix.FIELDS),key=lambda value:value['record_index']))+(cycle,)
        check=phoenix.phoenix_status(Evidence(PROFILE,(source,),rows,context,'stream-cycle-mapper-1','stream-recorded-status-1','historical'),{})
        for reason in check['unavailable']:
            if reason not in ('whole_run_all_four_modules_status_and_timing_coverage_unavailable',):_add(p['unavailable'],reason)
        current=check['coverage']['module_observations']
        if p['summaries'] is None:
            p['summaries']=copy.deepcopy(current)
            for summary in p['summaries']:
                for key in ('profile_cycles','status_cycles','timing_cycles','distinct_sdk_observations','held_snapshot_cycles','sequence_regressions'):summary[key]=0
                summary['signal_observations']={};summary['latest_observation']=None
        for old,new in zip(p['summaries'],current):
            old['profile_cycles']+=new['profile_cycles'];old['status_cycles']+=new['status_cycles']
            for reason in new['unavailable']:_add(old['unavailable'],reason)
            # Missing-field examples are scalar reasons, not another reference budget.
            for field,error in new['unavailable_fields'].items():old['unavailable_fields'][field]={'reason':error['reason'],'source_reference':None,'source_reference_omitted':error['source_reference'] is not None}
            latest=new['latest_observation']
            module=str(new['module_index'])
            if latest is None:
                p['last_sequences'].pop(module,None)
                p['qualification_epochs'][module]=p['qualification_epochs'].get(module,0)+1
                continue
            sequence=latest['observation_sequence']
            prior=p['last_sequences'].get(module)
            prefix=f"/Drive/Module{new['module_index']}/Phoenix"
            module_epoch=identity([[field,snapshot['epochs'].get(field,0)] for field in sorted(phoenix.FIELDS) if field.startswith(prefix)])
            sequence_row=snapshot['held'].get(prefix+'ObservationSequence',{})
            observation_epoch=[source,module_epoch,sequence_row.get('entry_id'),sequence_row.get('entry_generation'),p['qualification_epochs'].get(module,0)]
            epoch=[*observation_epoch,snapshot['boundary']]
            if prior and prior['epoch']==epoch and sequence<prior['sequence']:
                old['sequence_regressions']+=1;_add(p['unavailable'],'phoenix_observation_sequence_regressed:'+old['module_position']);continue
            p['last_sequences'][module]={'epoch':epoch,'sequence':sequence}
            old['timing_cycles']+=1
            if not _unique(db,sid,gen,'phoenix-sequence',[source,new['module_index'],sequence,sequence_row.get('entry_id'),sequence_row.get('entry_generation')]):
                old['held_snapshot_cycles']+=1
                continue
            old['distinct_sdk_observations']+=1
            reference_total=len(latest['common_references'])+sum(len(v['references']) for v in latest['signals'].values())
            latest['common_references']={}
            for signal in latest['signals'].values():signal['references']={}
            latest.update(source_references_total=reference_total,source_references_omitted=reference_total,source_references_truncated=True)
            old['latest_observation']=latest
            for signal,observation in new['signal_observations'].items():
                target=old['signal_observations'].get(signal)
                if target is None:old['signal_observations'][signal]=copy.deepcopy(observation);continue
                for name in ('receipt_comparisons','raw_value_comparisons','best_timestamp_sources'):
                    for key,value in observation[name].items():target[name][key]=target[name].get(key,0)+value
                for name in ('sdk_timestamp_in_future_count','best_timestamp_source_changed_count'):target[name]+=observation[name]
                name='age_at_observation_start_seconds_min';target[name]=min(target[name],observation[name])
                name='age_at_observation_end_seconds_max';target[name]=max(target[name],observation[name])
        for finding in check['findings']:
            key=str(finding['module_index'])+':'+finding['signal']
            target=p['warnings'].get(key)
            if target is None:
                target=copy.deepcopy(finding);target['observed_false_status_records']=0;target['source_references']=[];p['warnings'][key]=target
            for reference in finding['source_references']:
                original=reference['status_ok']
                if not _unique(db,sid,gen,'phoenix-warning',[original['source_sha256'],original['record_index'],original['field']]):continue
                target['observed_false_status_records']+=1
                cost=sum(isinstance(value,dict) for value in reference.values())
                if p['reference_count']+cost<=REFERENCE_LIMIT:
                    target['source_references'].append(reference);p['reference_count']+=cost

    def _system(self,db,sid,gen,state,snapshot):
        s=state['system'];cycle=snapshot['cycle'];stamp=system._ns(cycle['timestamp_ns'])
        if s['boundary']!=snapshot['boundary']:
            s['baselines']={};s['previous_voltage']=None;s['floor']=str(stamp);s['boundary']=snapshot['boundary']
        for field in system.FIELDS:
            epoch=snapshot['epochs'].get(field,0)
            if s['epochs'].get(field)!=epoch:
                s['baselines'].pop(field,None)
                if field==system.BATTERY:s['previous_voltage']=None
            s['epochs'][field]=epoch
        current={}
        for field in sorted(system.FIELDS):
            observation=snapshot['held'].get(field)
            if observation is None:_add(s['unavailable'],'missing_system_status_field:'+field);continue
            record_stamp=system._ns(observation['record_timestamp_ns'])
            if record_stamp>stamp:
                _add(s['unavailable'],'observation_after_current_cycle:'+field);s['baselines'].pop(field,None);continue
            value,error=system._value(observation)
            if error:_add(s['unavailable'],error+':'+field);continue
            current[field]=(value,observation)
            summary=s['fields'][field];summary['covered_cycles']+=1
            summary['held_cycles']+=observation.get('cycle_timestamp_ns')!=cycle['timestamp_ns']
            identity=_identity(observation);reference=system._ref(observation,cycle)
            if _unique(db,sid,gen,'system-record',[field,identity]):summary['distinct_records']+=1
            summary['latest_reference']=reference
            if field.startswith('/SystemStats/Faults/') and value is True:
                _warning(db,sid,gen,s,'recorded_system_fault_true',field.rsplit('/',1)[1],identity,{'observation':reference,'recorded_value':True})
            if field.startswith('/SystemStats/FaultCounts/'):
                prior=s['baselines'].get(field)
                associated=observation.get('cycle_timestamp_ns')
                eligible=observation.get('stream_scope')=='in_run' and associated is not None and system._ns(associated)>=int(s['floor'])
                if prior and prior['identity']!=identity and eligible:
                    old=prior['row']
                    compatible=(old['entry_id'],old['entry_generation'])==(observation['entry_id'],observation['entry_generation']) and system._ns(old['record_timestamp_ns'])<record_stamp and observation['record_index']>old['record_index']
                    if not compatible:_add(s['unavailable'],'counter_observation_order_unqualified:'+field)
                    elif value!=prior['value']:
                        kind='recorded_system_fault_counter_'+('increased' if value>prior['value'] else 'decreased')
                        _warning(db,sid,gen,s,kind,field.rsplit('/',1)[1],identity,{'previous':prior['reference'],'observation':reference},previous_recorded_value=str(prior['value']),recorded_value=str(value),recorded_change=str(value-prior['value']),physical_event_count_qualified=False,physical_reset_qualified=False)
                if eligible:s['baselines'][field]=dict(identity=identity,value=value,row=observation,reference=reference)
                else:s['baselines'].pop(field,None)
        if len(current)==len(system.FIELDS):s['complete_cycles']+=1
        if system.BATTERY not in current:s['previous_voltage']=None;return
        value,observation=current[system.BATTERY];s['voltage_count']+=1
        s['minimum']=value if s['minimum'] is None else min(value,s['minimum'])
        s['maximum']=value if s['maximum'] is None else max(value,s['maximum'])
        prior=s['previous_voltage']
        if prior:
            dt=stamp-int(prior['stamp'])
            if 0<dt<=system.MAX_CYCLE_GAP_NS and prior['entry']==[observation['entry_id'],observation['entry_generation']]:
                total=int(s['weight_ns']);new_total=total+dt;scale=max(s['scale'],abs(prior['value']))
                old_scaled=s['scaled_mean']*(s['scale']/scale) if scale else 0.
                s['scaled_mean']=old_scaled*(total/new_total)+(prior['value']/scale)*(dt/new_total) if scale else 0.
                s['scale']=scale;s['weight_ns']=str(new_total);s['weighted_count']+=1
        s['previous_voltage']=dict(stamp=str(stamp),value=value,entry=[observation['entry_id'],observation['entry_generation']])

    def finish(self,state,descriptor):
        quality=RecordingQualityReducer().finish(state['quality'],descriptor)
        errors=state['mapper']['errors'];cycles=state['cycles'];context=state['context']
        endpoints=state['min_stamp']==str(system._ns(context['start_monotonic_ns'])) and state['max_stamp']==str(system._ns(context['end_monotonic_ns']))
        system_errors=[reason for reason in errors if '/SystemStats/' in reason]
        common=[reason for reason in errors if reason not in system_errors]
        if descriptor.get('full_source_verified') is not True:_add(common,'stream_sources_not_fully_verified')
        if descriptor.get('source_hazards',{}).get('total',0) or state['quality']['source_order_hazards']['total']:_add(common,'full_source_order_or_reference_unqualified')
        if context.get('completeness')!='complete':_add(common,'complete_run_required_for_status_coverage')
        if not endpoints:_add(common,'run_endpoint_coverage_unavailable')
        p=state['phoenix'];summaries=p['summaries'] or []
        unavailable=sorted(set(common+p['unavailable']))
        physical=['physical_acquisition_time_not_qualified','native_timestamp_availability_not_qualified','sdk_age_fault_threshold_not_configured']
        decisive=[reason for reason in unavailable if reason not in physical]
        complete=bool(cycles and len(summaries)==4 and all(s['status_cycles']==cycles and s['timing_cycles']==cycles for s in summaries) and not decisive)
        if not complete:_add(unavailable,'whole_run_all_four_modules_status_and_timing_coverage_unavailable')
        metrics=[]
        for summary in summaries:
            summary.update(expected_cycles=cycles,status_coverage_fraction=summary['status_cycles']/cycles if cycles else None,timing_coverage_fraction=summary['timing_cycles']/cycles if cycles else None)
            for signal,observation in summary['signal_observations'].items():
                for name in ('age_at_observation_start_seconds_min','age_at_observation_end_seconds_max'):
                    m=metric(summary['module_position'].replace('-','_')+'_'+signal+'_'+name,observation[name],'seconds',sample_count=summary['distinct_sdk_observations'])
                    m.update(module_index=summary['module_index'],signal=signal,interpretation='observed SDK timestamp age only; not calibrated physical acquisition freshness');metrics.append(m)
        findings=list(p['warnings'].values())
        for finding in findings:self._finding_totals(finding,'observed_false_status_records')
        phoenix_result=result('finding' if findings else 'evaluated_no_finding' if complete else 'insufficient_data',metrics=metrics,findings=findings,unavailable=sorted(set(unavailable+physical)),coverage=dict(scope='recorded_sdk_status_and_observation_timing_only',observation_profile=phoenix.OBSERVATION_PROFILE,module_observations=summaries,expected_cycles=cycles,physical_acquisition_time_qualified=False,native_timestamp_availability_qualified=False,source_reference_limit_per_check=128,mapper_version=mapper.VERSION,distinct_observation_basis='source_module_sequence_entry_identity_generation_and_sequence'))
        s=state['system'];unavailable=sorted(set(common+system_errors+s['unavailable']))
        if cycles<2:_add(unavailable,'at_least_two_logger_cycles_required')
        complete=cycles>=2 and s['complete_cycles']==cycles and not unavailable
        seconds=int(s['weight_ns'])/1e9;mean=s['scale']*max(-1,min(1,s['scaled_mean'])) if s['weighted_count'] else None
        metrics=[metric('recorded_battery_voltage_min',s['minimum'],'volts',sample_count=s['voltage_count'],eligible_seconds=seconds),metric('recorded_battery_voltage_max',s['maximum'],'volts',sample_count=s['voltage_count'],eligible_seconds=seconds),metric('recorded_battery_voltage_time_weighted_mean',mean,'volts',sample_count=s['weighted_count'],eligible_seconds=seconds)]
        findings=list(s['warnings'].values())
        for finding in findings:self._finding_totals(finding,'observations')
        system_result=result('finding' if findings else 'evaluated_no_finding' if complete else 'insufficient_data',metrics=metrics,findings=findings,unavailable=sorted(set(unavailable+['physical_acquisition_time_not_qualified','fault_current_latched_cumulative_reset_semantics_not_qualified','physical_voltage_fault_limits_not_configured'])),coverage=dict(scope='recorded_system_status_only',observation_profile=system.OBSERVATION_PROFILE,source_review=system.SOURCE_REVIEW,expected_cycles=cycles,complete_cycles=s['complete_cycles'],complete_recorded_profile=complete,fields=list(s['fields'].values()),voltage_mean_basis='left_held_logger_state_over_adjacent_eligible_cycles',maximum_cycle_gap_ns=str(system.MAX_CYCLE_GAP_NS),eligible_seconds=seconds,physical_acquisition_time_qualified=False,counter_semantics_qualified=False,voltage_fault_limits_configured=False,source_reference_limit_per_check=128,mapper_version=mapper.VERSION))
        return {'stream_result_format':'stream-status-results-1','checks':{'recording-quality':quality,'phoenix-status-observation':phoenix_result,'system-status-observation':system_result}}

    @staticmethod
    def _finding_totals(finding,count_key):
        total=finding[count_key];available=len(finding['source_references'])
        finding.update(source_references_total=total,source_references_omitted=total-available,source_references_truncated=total>available,supporting_references_limited=total>available)
