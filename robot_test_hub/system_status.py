"""Source-reviewed SystemCore logger observations, never physical fault diagnosis."""
import math
import re
from .analysis import Analyzer,Declaration,metric,result
from .wpilog import PROFILE

VERSION='1'
OBSERVATION_PROFILE='akit-systemcore-system-stats-1'
MAX_ROWS=50000
REFERENCE_LIMIT=128
# Structural interval eligibility, matching the existing recording-quality
# default; this is neither a voltage limit nor physical event qualification.
MAX_CYCLE_GAP_NS=250000000
FAULTS=('Brownout','CANBusDown','CANBusUnavail','Display','IMU','IO','RSL','USB')
BATTERY='/SystemStats/BatteryVoltage'
SPECIFICATIONS={BATTERY:('double','volts'),
 **{'/SystemStats/Faults/'+name:('boolean',None) for name in FAULTS},
 **{'/SystemStats/FaultCounts/'+name:('int64',None) for name in FAULTS}}
FIELDS=frozenset(SPECIFICATIONS)
SOURCE_REVIEW=dict(artifact='org.littletonrobotics.akit:akit-java:27.0.0-alpha-6:sources',
 jar_sha256='d822d7625a9553c311526134721ceabac79035a5f296c3e88f6bac7bda3cc1ea',
 logged_system_stats_sha256='5527ab2217f86a53aff35c1e351b8dc1deb6a19891d52d3633a32618ba93e73c',
 conduit_api_sha256='8042bd2ac2539db0a1626a5b9d4cf156a64be76db9b1e92123f7d3052de2eab1',
 logger_sha256='7b294da27548c910e542f8a3685f3ca0494bdc21c464c6a9c3a6acc5d7fb161a')


def _ns(value):
 if type(value) is int and 0<=value<(1<<63):return value
 if type(value) is str and re.fullmatch('0|[1-9][0-9]{0,18}',value) and int(value)<(1<<63):return int(value)
 raise ValueError('invalid_nanosecond_reference')


def _positive(value):return type(value) is int and 0<value<(1<<63)


def _record(row):
 if 'before_run' in row and type(row['before_run']) is not bool:return 'before_run_metadata_invalid'
 if type(row.get('record_index')) is not int or not 0<=row['record_index']<(1<<63):return 'record_index_invalid'
 if not _positive(row.get('entry_id')) or not _positive(row.get('entry_generation')):return 'entry_identity_unavailable'
 try:_ns(row['record_timestamp_ns'])
 except (KeyError,ValueError):return 'record_timestamp_invalid'
 return None


def _value(row):
 kind,unit=SPECIFICATIONS[row['field']]
 if row.get('type')!=kind or row.get('unit')!=unit or row.get('validity')!='valid' or row.get('category')!='input':return None,'field_type_unit_or_validity_invalid'
 value=row.get('value')
 if kind=='boolean':valid=type(value) is bool
 elif kind=='int64':
  if type(value) is str and re.fullmatch('0|[1-9][0-9]{0,18}',value):value=int(value)
  valid=type(value) is int and 0<=value<(1<<63)
 else:
  try:valid=type(value) in (int,float) and math.isfinite(value)
  except OverflowError:valid=False
 return (value,None) if valid else (None,'recorded_value_invalid')


def _ref(observation,cycle):
 return dict(source_sha256=observation['source_sha256'],field=observation['field'],type=observation['type'],unit=observation.get('unit'),
  record_index=observation['record_index'],entry_id=observation['entry_id'],entry_generation=observation['entry_generation'],
  record_timestamp_ns=str(_ns(observation['record_timestamp_ns'])),record_cycle_timestamp_ns=observation.get('cycle_timestamp_ns'),
  current_cycle_timestamp_ns=str(_ns(cycle['timestamp_ns'])),current_cycle_record_index=cycle['timestamp_record_index'],
  before_run=observation.get('before_run',False),held_logger_state=observation.get('cycle_timestamp_ns')!=cycle['timestamp_ns'],
  physical_acquisition_time_qualified=False)


def system_status(evidence,configuration):
 if configuration:raise ValueError('No physical voltage or fault-counter thresholds are declared')
 if evidence.profile!=PROFILE:return result('unsupported',unavailable=['unsupported_system_status_profile'])
 if len(evidence.rows)>MAX_ROWS or evidence.context.get('row_limit_reached'):return result('insufficient_data',unavailable=['report_resource_limit_reached'])
 try:
  lo,hi=_ns(evidence.context['start_monotonic_ns']),_ns(evidence.context['end_monotonic_ns'])
  if lo>hi:raise ValueError()
 except (KeyError,ValueError):return result('insufficient_data',unavailable=['run_robot_interval_unavailable'])
 unavailable=set()
 if evidence.context.get('completeness')!='complete':unavailable.add('complete_run_required_for_status_coverage')
 if evidence.context.get('source_nonadvancing_cycles'):unavailable.add('source_cycle_order_unqualified')
 for hazard in evidence.context.get('swerve_source_hazards',[]):
  if any(word in str(hazard) for word in ('overlap','nonadvancing','duplicate_source_cycle')):unavailable.add('source_clock_overlap_or_order_unqualified')
 states={field:dict(field=field,covered_cycles=0,held_cycles=0,distinct_records=0,latest_reference=None) for field in sorted(FIELDS)}
 seen_records={field:set() for field in FIELDS}
 held={};entries={};retired={};baselines={};last_indexes={};last_cycle_index={};last_cycles={}
 current_source=None;seen_sources=set();global_last=None;previous_voltage=None;counter_floor=lo
 expected=complete_cycles=0;cycle_stamps=set();voltage_values=[];weighted=[];warnings={};warning_seen=set()

 def clear_entry(entry_id,field=None):
  for path,observation in list(held.items()):
   if observation['entry_id']==entry_id or path==field:
    held.pop(path,None);baselines.pop(path,None)

 def warning(kind,signal,identity,references,**extra):
  if identity in warning_seen:return
  warning_seen.add(identity)
  group=warnings.setdefault((kind,signal),dict(kind=kind,signal=signal,severity='warning',
   observed_behavior=('Recorded or held SystemCore '+signal+' fault flag is true; current/latched semantics and physical cause are unqualified' if kind=='recorded_system_fault_true' else 'Recorded SystemCore '+signal+' counter '+('increased' if kind.endswith('increased') else 'decreased')+' between compatible logger observations; this is not a physical event count or reset diagnosis'),
   criterion='exact_recorded_boolean_true' if kind=='recorded_system_fault_true' else 'qualified_recorded_int64_change',criterion_revision=OBSERVATION_PROFILE,
   observations=0,source_references=[],supporting_references_limited=False,physical_health_qualified=False,
   next_check='Inspect the original source interval and missing coverage before a supervised electrical check'))
  group['observations']+=1
  if len(group['source_references'])<REFERENCE_LIMIT:group['source_references'].append(dict(**references,**extra))
  else:group['supporting_references_limited']=True

 for row in evidence.rows:
  source=row.get('source_sha256')
  if source not in evidence.source_hashes:
   unavailable.add('source_reference_unavailable');held.clear();baselines.clear();previous_voltage=None;continue
  if source!=current_source:
   if source in seen_sources:unavailable.add('noncontiguous_source_order_unqualified')
   seen_sources.add(source);current_source=source;held.clear();entries.clear();retired.clear();baselines.clear();previous_voltage=None;counter_floor=lo
  kind=row.get('kind')
  if kind in ('observation','control'):
   if kind=='observation' and row.get('field') not in FIELDS:continue
   error=_record(row)
   index=row.get('record_index')
   if error or index<=last_indexes.get(source,-1):
    unavailable.add(error or 'source_record_order_unqualified');held.clear();baselines.clear();previous_voltage=None;continue
   last_indexes[source]=index
   entry=row['entry_id'];generation=row['entry_generation'];field=row.get('field')
   if kind=='control':
    if row.get('control') not in ('start','finish','metadata'):
     unavailable.add('entry_lifecycle_unavailable');held.clear();baselines.clear();previous_voltage=None;continue
    previous=entries.get(entry)
    if row['control']=='start' and previous is not None and (generation<previous[1] or generation==previous[1] and field!=previous[0]):
     unavailable.add('entry_generation_or_identity_conflict');clear_entry(entry,field);previous_voltage=None;continue
    breaks_voltage=field==BATTERY or BATTERY in held and held[BATTERY]['entry_id']==entry
    clear_entry(entry,field)
    if breaks_voltage:previous_voltage=None
    if row['control']=='finish':
     retired[entry]=max(retired.get(entry,0),generation);entries.pop(entry,None)
    else:entries[entry]=(field,generation)
    continue
   try:
    recorded=_ns(row['record_timestamp_ns'])
    if row.get('cycle_timestamp_ns') is not None:_ns(row['cycle_timestamp_ns'])
   except ValueError:
    unavailable.add('record_cycle_timestamp_invalid');clear_entry(entry,field);continue
   if recorded>hi or row.get('cycle_timestamp_ns') is not None and _ns(row['cycle_timestamp_ns'])>hi:continue
   prior_row=held.get(field)
   if prior_row is not None and (prior_row['entry_id'],prior_row['entry_generation'])==(entry,generation) and recorded<=_ns(prior_row['record_timestamp_ns']):
    unavailable.add('source_observation_clock_nonadvancing:'+field);clear_entry(entry,field);previous_voltage=None;continue
   previous=entries.get(entry)
   if generation<=retired.get(entry,0) or previous is not None and (generation<previous[1] or generation==previous[1] and previous[0]!=field):
    unavailable.add('entry_generation_or_identity_conflict');clear_entry(entry,field);continue
   if previous is not None and previous!=(field,generation):clear_entry(entry,field)
   # Replacing the same field under another entry must also break comparisons.
   if field in held and (held[field]['entry_id'],held[field]['entry_generation'])!=(entry,generation):baselines.pop(field,None)
   entries[entry]=(field,generation)
   value,error=_value(row)
   if error:
    unavailable.add(error+':'+field);held.pop(field,None);baselines.pop(field,None);continue
   held[field]=row
   continue
  if kind!='cycle':continue
  try:stamp=_ns(row['timestamp_ns'])
  except (KeyError,ValueError):unavailable.add('cycle_timestamp_invalid');baselines.clear();previous_voltage=None;continue
  if not lo<=stamp<=hi:continue
  expected+=1
  cycle_index=row.get('timestamp_record_index')
  if type(cycle_index) is not int or not 0<=cycle_index<(1<<63) or cycle_index<=last_cycle_index.get(source,-1):
   unavailable.add('cycle_record_reference_invalid');baselines.clear();previous_voltage=None;continue
  last_cycle_index[source]=cycle_index
  previous_stamp=last_cycles.get(source)
  if previous_stamp is not None and stamp<=previous_stamp or stamp in cycle_stamps:
   unavailable.add('source_cycle_order_or_overlap_unqualified');baselines.clear();previous_voltage=None;continue
  if global_last is not None and stamp<=global_last:unavailable.add('overlapping_source_clock_intervals')
  global_last=stamp;last_cycles[source]=stamp;cycle_stamps.add(stamp)
  if previous_stamp is not None and stamp-previous_stamp>MAX_CYCLE_GAP_NS:
   unavailable.add('logger_cycle_gap');baselines.clear();previous_voltage=None;counter_floor=stamp
  current={}
  for field in sorted(FIELDS):
   observation=held.get(field)
   if observation is None:
    unavailable.add('missing_system_status_field:'+field);continue
   record_stamp=_ns(observation['record_timestamp_ns'])
   if record_stamp>stamp:
    unavailable.add('observation_after_current_cycle:'+field);baselines.pop(field,None);continue
   value,error=_value(observation)
   if error:unavailable.add(error+':'+field);continue
   current[field]=(value,observation)
   summary=states[field];summary['covered_cycles']+=1
   if observation.get('cycle_timestamp_ns')!=row['timestamp_ns']:summary['held_cycles']+=1
   identity=(source,observation['record_index'],observation['entry_id'],observation['entry_generation'])
   if identity not in seen_records[field]:seen_records[field].add(identity);summary['distinct_records']+=1
   summary['latest_reference']=_ref(observation,row)
   if field.startswith('/SystemStats/Faults/') and value is True:
    warning('recorded_system_fault_true',field.rsplit('/',1)[1],('flag',field,*identity),{'observation':_ref(observation,row),'recorded_value':True})
   if field.startswith('/SystemStats/FaultCounts/'):
    prior=baselines.get(field)
    eligible=not observation.get('before_run',False) and record_stamp>=counter_floor
    if prior is not None and identity!=prior['identity'] and eligible:
     old=prior['row'];old_stamp=_ns(old['record_timestamp_ns'])
     compatible=(old['entry_id'],old['entry_generation'])==(observation['entry_id'],observation['entry_generation']) and old_stamp<record_stamp and observation['record_index']>old['record_index']
     if not compatible:unavailable.add('counter_observation_order_unqualified:'+field)
     elif value!=prior['value']:
      direction='increased' if value>prior['value'] else 'decreased'
      warning('recorded_system_fault_counter_'+direction,field.rsplit('/',1)[1],('counter',field,*identity),{'previous':prior['reference'],'observation':_ref(observation,row)},previous_recorded_value=str(prior['value']),recorded_value=str(value),recorded_change=str(value-prior['value']),physical_event_count_qualified=False,physical_reset_qualified=False)
    if eligible:baselines[field]=dict(identity=identity,value=value,row=observation,reference=_ref(observation,row))
    else:baselines.pop(field,None)
  if len(current)==len(FIELDS):complete_cycles+=1
  if BATTERY in current:
   value,observation=current[BATTERY];voltage_values.append(value)
   if previous_voltage is not None:
    old_stamp,old_value,old_observation=previous_voltage
    if 0<stamp-old_stamp<=MAX_CYCLE_GAP_NS and (old_observation['entry_id'],old_observation['entry_generation'])==(observation['entry_id'],observation['entry_generation']):weighted.append((old_value,stamp-old_stamp))
   previous_voltage=(stamp,value,observation)
  else:previous_voltage=None
 if expected<2:unavailable.add('at_least_two_logger_cycles_required')
 if not cycle_stamps or min(cycle_stamps)!=lo or max(cycle_stamps)!=hi:unavailable.add('run_endpoint_coverage_unavailable')
 complete=expected>=2 and complete_cycles==expected and not unavailable
 seconds=sum(weight for _,weight in weighted)/1e9
 mean=None
 if weighted:
  scale=max(abs(value) for value,_ in weighted);total=sum(weight for _,weight in weighted)
  mean=0 if scale==0 else scale*max(-1,min(1,math.fsum((value/scale)*(weight/total) for value,weight in weighted)))
 metrics=[metric('recorded_battery_voltage_min',min(voltage_values) if voltage_values else None,'volts',sample_count=len(voltage_values),eligible_seconds=seconds),
  metric('recorded_battery_voltage_max',max(voltage_values) if voltage_values else None,'volts',sample_count=len(voltage_values),eligible_seconds=seconds),
  metric('recorded_battery_voltage_time_weighted_mean',mean,'volts',sample_count=len(weighted),eligible_seconds=seconds)]
 findings=list(warnings.values())
 unavailable.update(('physical_acquisition_time_not_qualified','fault_current_latched_cumulative_reset_semantics_not_qualified','physical_voltage_fault_limits_not_configured'))
 return result('finding' if findings else 'evaluated_no_finding' if complete else 'insufficient_data',metrics=metrics,findings=findings,unavailable=sorted(unavailable),coverage=dict(scope='recorded_system_status_only',observation_profile=OBSERVATION_PROFILE,source_review=SOURCE_REVIEW,expected_cycles=expected,complete_cycles=complete_cycles,complete_recorded_profile=complete,fields=list(states.values()),voltage_mean_basis='left_held_logger_state_over_adjacent_eligible_cycles',maximum_cycle_gap_ns=str(MAX_CYCLE_GAP_NS),eligible_seconds=seconds,physical_acquisition_time_qualified=False,counter_semantics_qualified=False,voltage_fault_limits_configured=False,source_reference_limit_per_finding=REFERENCE_LIMIT))


def system_status_analyzer():
 return Analyzer(Declaration('system-status-observation',VERSION,(PROFILE,),eligible_windows='qualified recorded logger states within run; physical freshness and fault semantics unknown',grouping_keys=('robot_id','boot_id','runtime_mode','mapping_revision')),system_status)
