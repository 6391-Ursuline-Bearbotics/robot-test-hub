"""Bounded full-source recording-quality reduction; physical health is not assessed."""
import hashlib
import json
import re

from .analysis import CONNECTION_FIELDS, metric, result

VERSION='stream-recording-quality-1'
MAX_GAP_NS=250000000
REFERENCE_LIMIT=128
WARNING_KINDS=('recording_gap','invalid_observations','stale_observations',
               'disconnected_observations','invalid_cycle_timestamps')


def _ns(value):
    if type(value) is int and 0<=value<(1<<63):return value
    if type(value) is str and re.fullmatch('0|[1-9][0-9]{0,18}',value) and int(value)<(1<<63):return int(value)
    raise ValueError('stream_quality_timestamp_invalid')


def _reference(row,**extra):
    reference={}
    for key in ('record_index','stream_source_ordinal','stream_line_ordinal'):
        value=row.get(key);reference[key]=value if type(value) is int and 0<=value<(1<<63) else None
    source=row.get('source_sha256')
    reference['source_sha256']=source if type(source) is str and re.fullmatch('[a-f0-9]{64}',source) else None
    reference['stream_scope']=row.get('stream_scope') if row.get('stream_scope') in ('before_run','in_run','after_run','unscoped') else 'unscoped'
    field=row.get('field')
    if type(field) is str and len(field.encode('utf-8'))<=1024:reference['field']=field
    elif type(field) is str:
        reference.update(field=None,field_sha256=hashlib.sha256(field.encode()).hexdigest(),field_omitted=True)
    reference.update(extra)
    return reference


def _unique(db,stream_id,generation,namespace,identity):
    key=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return db.execute('INSERT OR IGNORE INTO analysis_stream_unique(stream_id,generation,namespace,key) VALUES(?,?,?,?)',
        (stream_id,generation,namespace,key)).rowcount==1


class RecordingQualityReducer:
    """Checkpoint state grows only with the fixed example allowance, never run size."""
    reducer_id='recording-quality'
    version=VERSION

    def initial(self,context):
        lo,hi=_ns(context['start_monotonic_ns']),_ns(context['end_monotonic_ns'])
        if lo>hi:raise ValueError('stream_quality_interval_invalid')
        return dict(version=VERSION,lo=str(lo),hi=str(hi),complete_run=context.get('completeness')=='complete',
            cycle_count=0,valid_cycle_count=0,min_stamp=None,max_stamp=None,previous_source=None,previous_cycle=None,
            valid_ns='0',unknown_epoch_cycles=0,observation_count=0,known_connection_observations=0,
            explicit_connection_observations=0,freshness_observations=0,unknown_sensor_samples=0,known_sensor_samples=0,
            reference_budget_used=0,full_source_previous=None,full_source_source=None,
            source_order_hazards=dict(total=0,references=[],truncated=False,by_scope={}),
            warnings={kind:dict(total=0,references=[]) for kind in WARNING_KINDS})

    def _warning(self,state,kind,reference):
        warning=state['warnings'][kind];warning['total']+=1
        if state['reference_budget_used']<REFERENCE_LIMIT:
            warning['references'].append(reference);state['reference_budget_used']+=1

    def consume(self,db,stream_id,generation,state,row):
        scope=row.get('stream_scope','unscoped');source=row.get('source_sha256');kind=row.get('kind')
        if kind=='cycle':
            try:stamp=_ns(row.get('timestamp_ns'))
            except ValueError:stamp=None
            if source!=state['full_source_source']:
                state['full_source_source']=source;state['full_source_previous']=None
            previous=state['full_source_previous']
            if stamp is not None:
                if previous is not None and stamp<=int(previous):
                    hazards=state['source_order_hazards'];hazards['total']+=1
                    hazards['by_scope'][scope]=hazards['by_scope'].get(scope,0)+1
                    if state['reference_budget_used']<REFERENCE_LIMIT:
                        hazards['references'].append(_reference(row,previous_timestamp_ns=previous,timestamp_ns=str(stamp)));state['reference_budget_used']+=1
                    else:hazards['truncated']=True
                state['full_source_previous']=str(stamp)
            else:state['full_source_previous']=None
        if scope!='in_run':return state
        if kind=='cycle':
            state['cycle_count']+=1
            try:stamp=_ns(row.get('timestamp_ns'))
            except ValueError:
                self._warning(state,'invalid_cycle_timestamps',_reference(row));state['previous_cycle']=None;return state
            if not int(state['lo'])<=stamp<=int(state['hi']):raise ValueError('stream_quality_scope_mismatch')
            state['valid_cycle_count']+=1
            state['min_stamp']=str(stamp) if state['min_stamp'] is None else str(min(stamp,int(state['min_stamp'])))
            state['max_stamp']=str(stamp) if state['max_stamp'] is None else str(max(stamp,int(state['max_stamp'])))
            if source!=state['previous_source']:state['previous_cycle']=None;state['previous_source']=source
            previous=state['previous_cycle']
            if previous is not None:
                delta=stamp-int(previous)
                if delta<=0:self._warning(state,'invalid_cycle_timestamps',_reference(row,previous_timestamp_ns=previous,timestamp_ns=str(stamp)))
                elif delta>MAX_GAP_NS:self._warning(state,'recording_gap',_reference(row,start_monotonic_ns=previous,end_monotonic_ns=str(stamp)))
                else:state['valid_ns']=str(int(state['valid_ns'])+delta)
            state['previous_cycle']=str(stamp)
            if row.get('aliases',{}).get('epoch_valid',{}).get('value') is not True:state['unknown_epoch_cycles']+=1
        elif kind=='observation':
            state['observation_count']+=1
            if row.get('validity')!='valid':self._warning(state,'invalid_observations',_reference(row))
            if row.get('fresh') is False:self._warning(state,'stale_observations',_reference(row))
            if type(row.get('fresh')) is bool:state['freshness_observations']+=1
            if type(row.get('connected')) is bool:state['explicit_connection_observations']+=1
            if row.get('field') in CONNECTION_FIELDS and row.get('validity')=='valid' and row.get('type')=='boolean' and type(row.get('value')) is bool:
                state['known_connection_observations']+=1
            if row.get('connected') is False or row.get('field') in CONNECTION_FIELDS and row.get('validity')=='valid' and row.get('type')=='boolean' and row.get('value') is False:
                self._disconnected(db,stream_id,generation,state,row)
        elif kind=='sample':
            availability=row.get('sensor_availability')
            if availability in ('connected','disconnected'):state['known_sensor_samples']+=1
            if availability=='unknown':state['unknown_sensor_samples']+=1
            if availability=='disconnected':
                refs=row.get('connection_sources',{})
                false=[dict(ref,field=field,source_sha256=ref.get('source_sha256',source),stream_scope=scope)
                    for field,ref in refs.items() if ref.get('validity')=='valid' and ref.get('value') is False]
                for reference in false or [row]:self._disconnected(db,stream_id,generation,state,reference)
        return state

    def _disconnected(self,db,stream_id,generation,state,row):
        identity=[row.get('source_sha256'),row.get('record_index'),row.get('field')]
        if row.get('record_index') is None:identity+=['unqualified',row.get('stream_line_ordinal'),row.get('timestamp_ns')]
        if _unique(db,stream_id,generation,'recording-quality-disconnected',identity):
            self._warning(state,'disconnected_observations',_reference(row))

    def finish(self,state,descriptor):
        if descriptor.get('full_source_verified') is not True:
            return result('insufficient_data',unavailable=['stream_sources_not_fully_verified'])
        warnings=state['warnings'];cycles=state['cycle_count'];valid=state['valid_cycle_count']
        span=(int(state['max_stamp'])-int(state['min_stamp']))/1e9 if valid>1 else 0
        valid_seconds=int(state['valid_ns'])/1e9
        fraction=valid_seconds/span if span and valid_seconds<=span else None
        unavailable=['physical_sensor_freshness_not_qualified','usb_write_health_not_qualified']
        if state['unknown_epoch_cycles']:unavailable.append('wall_clock_invalid_or_unknown')
        if not (state['known_connection_observations'] or state['explicit_connection_observations'] or state['known_sensor_samples'] or warnings['disconnected_observations']['total']):unavailable.append('sensor_connection_status_not_recorded')
        if not state['freshness_observations']:unavailable.append('sensor_acquisition_freshness_not_recorded')
        if state['unknown_sensor_samples']:unavailable.append('sample_sensor_connection_status_unknown')
        structural=[]
        if valid<2:structural.append('at_least_two_run_cycles_required')
        if not state['complete_run']:structural.append('complete_run_required_for_quality_coverage')
        if state['min_stamp']!=state['lo'] or state['max_stamp']!=state['hi']:structural.append('run_endpoint_coverage_unavailable')
        if valid_seconds>span:structural.append('overlapping_source_cycle_coverage')
        if state['source_order_hazards']['total']:structural.append('full_source_clock_order_unqualified')
        traversal_hazards=descriptor.get('source_hazards',{}).get('total',0)
        if traversal_hazards:structural.append('full_source_order_or_overlap_unqualified')
        unavailable.extend(structural)
        findings=[]
        for kind,warning in warnings.items():
            if not warning['total']:continue
            refs=warning['references']
            finding=dict(kind=kind,severity='warning',observed_behavior='Recorded in-run '+kind.replace('_',' '),count=warning['total'],
                source_references=refs,source_references_total=warning['total'],source_references_available=len(refs),
                source_references_omitted=warning['total']-len(refs),source_references_truncated=warning['total']>len(refs),
                next_check='Inspect original source coverage and device observations before interpreting control metrics',physical_health_qualified=False)
            if kind=='recording_gap':
                finding.update(intervals_ns=[[ref['start_monotonic_ns'],ref['end_monotonic_ns']] for ref in refs],
                    intervals_ns_total=warning['total'],intervals_ns_available=len(refs),
                    intervals_ns_omitted=warning['total']-len(refs),intervals_ns_truncated=warning['total']>len(refs))
            findings.append(finding)
        metrics=[metric('recording_span_seconds',span,'seconds',sample_count=cycles),
            metric('valid_cycle_coverage',fraction,'fraction'),metric('gap_count',warnings['recording_gap']['total'],'count'),
            metric('invalid_observation_count',warnings['invalid_observations']['total'],'count'),
            metric('unknown_or_invalid_epoch_cycles',state['unknown_epoch_cycles'],'count')]
        return result('finding' if findings else 'insufficient_data' if structural else 'evaluated_no_finding',metrics=metrics,findings=findings,
            unavailable=unavailable,coverage=dict(scope='in_run_recorded_quality',traversal_scope='complete_pinned_sources',
                cycle_count=cycles,valid_seconds=valid_seconds,span_seconds=span,fraction=fraction,
                wall_clock_valid_cycles=valid-state['unknown_epoch_cycles'],known_connection_observations=state['known_connection_observations'],
                known_sensor_samples=state['known_sensor_samples'],
                disconnected_source_count=warnings['disconnected_observations']['total'],source_order_hazards=state['source_order_hazards'],
                source_traversal_hazards_total=traversal_hazards,source_traversal_references='pinned_stream_descriptor',
                full_source_verified=True,physical_acquisition_time_qualified=False,reference_limit_per_check=REFERENCE_LIMIT))
