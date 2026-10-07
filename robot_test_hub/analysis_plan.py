"""Explicit human analysis policy and immutable review-snapshot resolution."""
import copy
import json
import math
import re

from .analysis import identity
from .swerve import MODULE_POSITIONS, SAMPLE_PROFILE, _config

MAX_REVIEW_RECORDS=10000


def validate_plan(value):
    if not isinstance(value,dict):raise ValueError('analysis_plan_object_required')
    required={'id','robot_id','start_utc_ns','end_utc_ns','reviewer','rationale','build_hash','config_hash',
              'test_id','surface','battery_id','wheel_radius_m','swerve_configuration'}
    optional={'revision','expected_revision','approved_baseline_id','ideal_simulation_cycle_policy'}
    if not required<=set(value) or set(value)-required-optional:raise ValueError('invalid_analysis_plan_fields')
    value=copy.deepcopy(value)
    for key in ('id','robot_id','test_id','battery_id'):
        if type(value[key]) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',value[key]):raise ValueError('invalid_analysis_plan_identity')
    for key in ('reviewer','rationale','surface'):
        if type(value[key]) is not str or not value[key].strip() or len(value[key])>2000 or '\x00' in value[key]:raise ValueError('invalid_analysis_plan_text')
    for key in ('build_hash','config_hash'):
        if type(value[key]) is not str or not re.fullmatch('[a-f0-9]{64}',value[key]):raise ValueError('invalid_analysis_plan_hash')
    for key in ('start_utc_ns','end_utc_ns'):
        text=value[key]
        if key=='end_utc_ns' and text is None:continue
        if type(text) is not str or not re.fullmatch('0|[1-9][0-9]{0,18}',text) or int(text)>=(1<<63):raise ValueError('invalid_analysis_plan_utc')
    if value['end_utc_ns'] is not None and int(value['end_utc_ns'])<=int(value['start_utc_ns']):raise ValueError('invalid_analysis_plan_interval')
    radius=value['wheel_radius_m']
    if type(radius) not in (int,float) or not 0<radius<=1 or not math.isfinite(radius):raise ValueError('invalid_analysis_plan_radius')
    config=value['swerve_configuration']
    if not isinstance(config,dict):raise ValueError('invalid_swerve_configuration')
    for key,number in config.items():
        if number is None or key=='threshold_revision':continue
        if type(number) not in (int,float) or not -1e19<number<1e19 or not math.isfinite(number):raise ValueError('invalid_swerve_configuration_number')
    config=_config(config)
    for key in ('max_gap_ns','max_sample_age_ns'):
        if config[key]>10000000000:raise ValueError('swerve_time_limit_exceeded')
    for key in ('minimum_eligible_seconds','sustained_error_seconds'):
        if config[key]>86400:raise ValueError('swerve_duration_limit_exceeded')
    if config['baseline_minimum_runs']>100:raise ValueError('swerve_baseline_limit_exceeded')
    if config['threshold_revision'] is not None and (type(config['threshold_revision']) is not str or
            not re.fullmatch('[A-Za-z0-9_-]{1,128}',config['threshold_revision'])):raise ValueError('invalid_threshold_revision')
    value['swerve_configuration']=config
    value.setdefault('revision',1);value.setdefault('expected_revision',None)
    if type(value['revision']) is not int or not 1<=value['revision']<=1000000:raise ValueError('invalid_plan_revision')
    previous=value['expected_revision']
    if previous is not None and (type(previous) is not int or not 1<=previous<=1000000):raise ValueError('invalid_plan_revision')
    value.setdefault('ideal_simulation_cycle_policy',False)
    if type(value['ideal_simulation_cycle_policy']) is not bool:raise ValueError('invalid_simulation_policy')
    value.setdefault('approved_baseline_id',None)
    baseline=value['approved_baseline_id']
    if baseline is not None and (type(baseline) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',baseline)):
        raise ValueError('invalid_selected_baseline')
    return value


def review_snapshot(db):
    count,size=db.execute('SELECT COUNT(*),COALESCE(SUM(length(CAST(payload_json AS BLOB))),0) FROM review_records r WHERE revision='
        '(SELECT MAX(revision) FROM review_records x WHERE x.kind=r.kind AND x.record_id=r.record_id)').fetchone()
    if count>MAX_REVIEW_RECORDS or size>16*1024*1024:raise ValueError('review_resource_limit')
    rows=db.execute('SELECT kind,record_id,revision,payload_json FROM review_records r WHERE revision='
        '(SELECT MAX(revision) FROM review_records x WHERE x.kind=r.kind AND x.record_id=r.record_id) '
        'ORDER BY kind,record_id LIMIT ?',(MAX_REVIEW_RECORDS+1,)).fetchall()
    if len(rows)>MAX_REVIEW_RECORDS:raise ValueError('review_resource_limit')
    values=[];size=0
    for row in rows:
        size+=len(row[3].encode())
        if size>16*1024*1024:raise ValueError('review_resource_limit')
        values.append(dict(kind=row[0],id=row[1],revision=row[2],payload=json.loads(row[3])))
    return {'revision':identity(values),'records':values}


def resolve_plan(run,snapshot):
    context={'review_history_revision':snapshot['revision'],'analysis_plan':None,'analysis_plan_unavailable':[],
             'assignment_references':[],'maintenance_references':[]}
    records=snapshot['records']
    robot=run['robot_id']
    history={kind:[r['payload'] for r in records if r['kind']==kind and r['payload'].get('robot_id')==robot]
             for kind in ('assignment','maintenance')}
    context['review_comparison_history']=copy.deepcopy(history)
    intervals=run.get('utc_intervals',[])
    if (run.get('known_boot') is not True or run.get('completeness')!='complete' or run.get('wall_clock_quality')!='anchored'
            or len(intervals)!=1 or intervals[0]['robot_start_ns']!=run['start_monotonic_ns']
            or intervals[0]['robot_end_ns']!=run['end_monotonic_ns']):
        context['analysis_plan_unavailable']=['complete_single_anchored_run_required'];return context,None
    interval=intervals[0];error=int(interval['uncertainty_ns'])
    start,end=int(interval['utc_start_ns'])-error,int(interval['utc_end_ns'])+error
    if not 0<=start<end<(1<<63):
        context['analysis_plan_unavailable']=['run_utc_envelope_invalid'];return context,None
    context.update(start_utc_ns=str(start),end_utc_ns=str(end),utc_interval_basis='anchored_interval_expanded_by_clock_uncertainty')
    plans=[r['payload'] for r in records if r['kind']=='analysis_plan' and r['payload'].get('robot_id')==robot]
    matches=[p for p in plans if int(p['start_utc_ns'])<=start and (p.get('end_utc_ns') is None or int(p['end_utc_ns'])>=end)]
    if len(matches)!=1:
        context['analysis_plan_unavailable']=['analysis_plan_missing_or_ambiguous'];return context,None
    plan=matches[0];context['analysis_plan']=copy.deepcopy(plan)
    for key in ('build_hash','config_hash','test_id','surface','battery_id','wheel_radius_m'):
        context[key]=plan[key]
    context['context_provenance']='human_recorded_analysis_plan'
    components={}
    for position in MODULE_POSITIONS:
        intersect=[a for a in history['assignment'] if a['location']==position and int(a['start_utc_ns'])<end and
                   (a.get('end_utc_ns') is None or int(a['end_utc_ns'])>start)]
        if len(intersect)!=1 or int(intersect[0]['start_utc_ns'])>start or (
                intersect[0].get('end_utc_ns') is not None and int(intersect[0]['end_utc_ns'])<end):
            context['analysis_plan_unavailable'].append('whole_run_component_assignment_required:'+position);continue
        assignment=intersect[0];components[position]=assignment['component_id']
        context['assignment_references'].append(copy.deepcopy(assignment))
    context['component_ids']=components
    if len(set(components.values()))!=4:context['analysis_plan_unavailable'].append('four_distinct_physical_components_required')
    for event in history['maintenance']:
        context['maintenance_references'].append(copy.deepcopy(event))
        if start<int(event['effective_utc_ns'])<end:context['analysis_plan_unavailable'].append('maintenance_splits_run')
    context.update(command_stage='final_io_optimized_cosine_desaturated',sample_profile=SAMPLE_PROFILE)
    selected=plan.get('approved_baseline_id')
    context['selected_baseline_id']=selected
    if selected is not None:
        matches=[r['payload'] for r in records if r['kind']=='baseline' and r['id']==selected]
        if len(matches)!=1 or matches[0].get('approved') is not True:
            context['analysis_plan_unavailable'].append('selected_approved_baseline_unavailable')
        else:
            baseline=copy.deepcopy(matches[0]);baseline['comparison_history']=history
            baseline['comparison_history_revision']=identity(history)
            context['approved_baseline']=baseline
            context['approved_baseline_version']=baseline['id']
            if not baseline.get('job_references') or any(j.get('analyzer_version')!='3' or _config(j.get('configuration',{}))!=plan['swerve_configuration']
                   for j in baseline.get('job_references',[])):
                context['analysis_plan_unavailable'].append('selected_baseline_analyzer_policy_incompatible')
    return context,plan
