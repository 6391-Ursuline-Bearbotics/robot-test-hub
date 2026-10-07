"""Explicit human review, immutable baselines and revisioned component history.

Only local evidence/catalog records change. No robot, deployment, notification or
physical repair operation is performed by this module.
"""
from __future__ import annotations
import json
import math
import re
import statistics
import time
import threading
from functools import wraps
from .analysis import canonical, identity
from .swerve import BASELINE_KEYS

DISPOSITIONS = ('confirmed_hardware','confirmed_software','expected_behavior','false_alarm','insufficient_evidence','unresolved')
MAINTENANCE_TYPES = ('inspection','physical_repair','module_swap','battery_change','configuration_change','software_change')
KINDS = ('assignment','maintenance','baseline','finding_review','regression_bundle')


def install_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS review_records (
        kind TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL, created_utc_ns TEXT NOT NULL,
        PRIMARY KEY(kind,record_id,revision), CHECK(revision>0))''')


def _text(value, name, limit=2000):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or '\x00' in value:
        raise ValueError(name+' must be nonempty text within its length limit')
    return value


def _id(value, name='id'):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):
        raise ValueError(name+' must be a safe opaque identity')
    return value


def _ns(value, name):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,19}',value) or int(value)>(1<<63)-1:
        raise ValueError(name+' must be decimal-string UTC nanoseconds')
    return int(value)


def _overlap(a,b,c,d):
    return a < (d if d is not None else math.inf) and c < (b if b is not None else math.inf)


def _write_transaction(method):
    @wraps(method)
    def mutation(self,*args,**kwargs):
        with self._mutation_lock:
            owned=not self.db.in_transaction
            if owned:
                self.db.execute('BEGIN IMMEDIATE')
            try:
                output=method(self,*args,**kwargs)
                if owned:
                    self.db.commit()
                return output
            except BaseException:
                if owned:
                    self.db.rollback()
                raise
    return mutation


class ReviewStore:
    def __init__(self,db):
        self.db=db
        self._mutation_lock=threading.RLock()
        if db.in_transaction:
            install_schema(db)
        else:
            with db:
                install_schema(db)

    def _latest(self,kind,record_id):
        _id(record_id)
        row=self.db.execute('SELECT payload_json FROM review_records WHERE kind=? AND record_id=? ORDER BY revision DESC LIMIT 1',(kind,record_id)).fetchone()
        return json.loads(row[0]) if row else None

    def records(self,kind):
        if kind not in KINDS:
            raise ValueError('Unknown record kind')
        rows=self.db.execute('''SELECT r.payload_json FROM review_records r WHERE kind=? AND revision=(
            SELECT MAX(revision) FROM review_records x WHERE x.kind=r.kind AND x.record_id=r.record_id)
            ORDER BY created_utc_ns,record_id''',(kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _append(self,kind,payload,*,immutable=False):
        payload=json.loads(canonical(payload))
        record_id=_id(payload.get('id'))
        revision=payload.get('revision',1)
        if type(revision) is not int or revision<1:
            raise ValueError('revision must be a positive integer')
        previous=self._latest(kind,record_id)
        expected=payload.pop('expected_revision',None)
        payload['revision']=revision
        existing=self.db.execute('SELECT payload_json FROM review_records WHERE kind=? AND record_id=? AND revision=?',(kind,record_id,revision)).fetchone()
        if existing:
            if json.loads(existing[0])!=payload:
                raise ValueError('Conflicting content for an existing id/revision')
            return json.loads(existing[0])
        if immutable and previous:
            raise ValueError('This record is immutable; create a new identity/version')
        current=previous['revision'] if previous else 0
        if revision!=current+1 or (current and expected!=current):
            raise ValueError('Revision must follow expected current revision')
        self.db.execute('INSERT INTO review_records VALUES (?,?,?,?,?)',(kind,record_id,revision,canonical(payload),str(time.time_ns())))
        return payload

    @_write_transaction
    def assign_component(self,payload):
        payload=dict(payload)
        for key in ('id','component_id','robot_id'):
            _id(payload.get(key),key)
        for key in ('location','reviewer','rationale'):
            _text(payload.get(key),key)
        start=_ns(payload.get('start_utc_ns'),'start_utc_ns')
        end=_ns(payload['end_utc_ns'],'end_utc_ns') if payload.get('end_utc_ns') is not None else None
        if end is not None and end<=start:
            raise ValueError('Assignment interval must have positive duration')
        for old in self.records('assignment'):
            if old['id']==payload['id']:
                continue
            collision=(old['robot_id']==payload['robot_id'] and old['location']==payload['location']) or old['component_id']==payload['component_id']
            if collision and _overlap(start,end,int(old['start_utc_ns']),int(old['end_utc_ns']) if old.get('end_utc_ns') else None):
                raise ValueError('Component/location assignments overlap; close the prior interval with a revision')
        return self._append('assignment',payload)

    @_write_transaction
    def record_maintenance(self,payload):
        """Append a maintenance event/correction revision; prior revisions never change."""
        payload=dict(payload)
        _id(payload.get('id'))
        _id(payload.get('robot_id'),'robot_id')
        if payload.get('kind') not in MAINTENANCE_TYPES:
            raise ValueError('Unknown maintenance kind')
        for key in ('reviewer','rationale'):
            _text(payload.get(key),key)
        _ns(payload.get('effective_utc_ns'),'effective_utc_ns')
        if payload.get('component_id') is not None:
            _id(payload['component_id'],'component_id')
        payload['action_scope']='record_only'
        return self._append('maintenance',payload)

    def _job(self,job_id):
        _id(job_id,'job_id')
        row=self.db.execute('SELECT result_json FROM analyzer_jobs WHERE job_id=?',(job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown analysis job')
        return json.loads(row[0])

    def _assignments_cover(self,context):
        start=_ns(context.get('start_utc_ns'),'run start_utc_ns')
        end=_ns(context.get('end_utc_ns'),'run end_utc_ns')
        if end<=start:
            raise ValueError('Run UTC interval must have positive duration')
        components=context.get('component_ids')
        if not isinstance(components,dict) or not components:
            raise ValueError('Run must identify physical component assignments')
        assignments=self.records('assignment')
        selected_assignments=[]
        for location,component in components.items():
            covers=[a for a in assignments if a['robot_id']==context['robot_id'] and a['location']==location and a['component_id']==component
                and int(a['start_utc_ns'])<=start and (a.get('end_utc_ns') is None or int(a['end_utc_ns'])>=end)]
            if len(covers)!=1:
                raise ValueError('Component assignment does not cover the entire run interval')
            selected_assignments.append(covers[0])
        for event in self.records('maintenance'):
            if event['robot_id']==context['robot_id'] and start<int(event['effective_utc_ns'])<end:
                raise ValueError('Maintenance/configuration event splits a selected run')
        return selected_assignments

    @_write_transaction
    def approve_baseline(self,payload):
        payload=dict(payload)
        for key in ('id','label','reviewer','rationale'):
            (_id if key=='id' else _text)(payload.get(key),key)
        payload.setdefault('exclusions',{})
        previous=self._latest('baseline',payload['id'])
        if previous:
            keys=('id','label','reviewer','rationale','job_ids','exclusions')
            if all(previous.get(k)==payload.get(k) for k in keys) and payload.get('revision',1)==previous['revision']:
                return previous
            raise ValueError('An approved baseline is immutable; create a new identity/version')
        ids=payload.get('job_ids')
        exclusions=payload.get('exclusions',{})
        if (not isinstance(ids,list) or not all(isinstance(v,str) for v in ids) or len(ids)<3 or len(ids)>100 or len(set(ids))!=len(ids)
                or not isinstance(exclusions,dict) or set(ids)&set(exclusions)):
            raise ValueError('Select 3-100 unique jobs explicitly; excluded jobs must be separate')
        for key,reason in exclusions.items():
            self._job(key)
            _text(reason,'exclusion reason')
        jobs=[self._job(job_id) for job_id in ids]
        context=None
        source_type=None
        run_ids=set()
        distributions={}
        assignment_evidence=[]
        run_intervals=[]
        analysis_policy=None
        for job in jobs:
            if job['analyzer_id']!='swerve-tracking' or job['outcome']!='evaluated_no_finding':
                raise ValueError('Baseline selection requires qualified no-finding swerve jobs')
            current=job['provenance']['context']
            policy=(job['analyzer_version'],job['configuration'])
            if analysis_policy is not None and analysis_policy!=policy:
                raise ValueError('Selected jobs use incompatible analyzer versions or configurations')
            analysis_policy=policy
            if any(current.get(key) is None or current.get(key)=='' for key in BASELINE_KEYS):
                raise ValueError('Baseline comparison context is incomplete')
            selected={key:current[key] for key in BASELINE_KEYS}
            if context is not None and context!=selected:
                raise ValueError('Selected run hardware/config/test/surface/battery context is incompatible')
            context=selected
            kind=job['provenance']['source_type']
            if source_type is not None and source_type!=kind:
                raise ValueError('Source types cannot be mixed in an approved baseline')
            source_type=kind
            run_id=current.get('run_id')
            if not isinstance(run_id,str) or not run_id or run_id in run_ids:
                raise ValueError('At least three independent run identities required')
            run_ids.add(run_id)
            assignment_evidence.extend(self._assignments_cover(current))
            interval=(int(current['start_utc_ns']),int(current['end_utc_ns']))
            if any(_overlap(*interval,*old) for old in run_intervals):
                raise ValueError('Independent selected runs cannot have overlapping UTC intervals')
            run_intervals.append(interval)
            if job['coverage'].get('evaluated_modules')!=len(current['component_ids']):
                raise ValueError('All selected physical modules need qualified coverage')
            for value in job['metrics']:
                if current['component_ids'].get(value.get('module_position'))!=value.get('module_id'):
                    raise ValueError('Metric physical identity differs from run assignment context')
                if value.get('value') is None:
                    raise ValueError('Unavailable metrics cannot become healthy baseline values')
                key=canonical([value['module_id'],value['module_position'],value['name'],value['unit']])
                record=distributions.setdefault(key,{'module_id':value['module_id'],'module_position':value['module_position'],
                    'name':value['name'],'unit':value['unit'],'values':[],'run_ids':[]})
                if run_id in record['run_ids']:
                    raise ValueError('Duplicate module metric within selected run')
                record['values'].append(value['value'])
                record['run_ids'].append(run_id)
        # A repair/configuration boundary between runs changes the cohort even
        # when recorded context was not updated. Explicitly start a new cohort.
        cohort_start=min(start for start,end in run_intervals)
        cohort_end=max(end for start,end in run_intervals)
        for event in self.records('maintenance'):
            if (event['robot_id']==context['robot_id']
                    and event['kind']!='inspection'
                    and cohort_start<int(event['effective_utc_ns'])<cohort_end):
                raise ValueError('Maintenance/configuration event splits the selected cohort')
        for value in distributions.values():
            if len(value['run_ids'])!=len(run_ids):
                raise ValueError('Selected jobs do not share the same qualified metric set')
            center=statistics.median(value['values'])
            value['median']=center
            value['mad']=statistics.median(abs(v-center) for v in value['values'])
        assignment_evidence=list({(a['id'],a['revision']):a for a in assignment_evidence}.values())
        payload.update(assignment_evidence=assignment_evidence, approved=True,source_type=source_type,context=context,distributions=list(distributions.values()),
            cohort_start_utc_ns=str(cohort_start),cohort_end_utc_ns=str(cohort_end),
            independent_runs=sorted(run_ids),job_references=[{'job_id':j['job_id'],'analyzer_version':j['analyzer_version'],
            'source_hashes':j['provenance']['source_hashes'],'mapping_revision':j['provenance']['mapping_revision'],
            'configuration':j['configuration']} for j in jobs],approval_scope='explicit_human_cohort_selection')
        return self._append('baseline',payload,immutable=True)

    def get_baseline(self,version):
        value=self._latest('baseline',version)
        if not value:
            raise ValueError('Unknown baseline version')
        # The cohort remains immutable. A comparison additionally pins the
        # currently loaded history; storing this snapshot in Evidence.context
        # makes a later history correction produce a different analysis job.
        robot=value['context']['robot_id']
        history={kind:[r for r in self.records(kind) if r['robot_id']==robot]
            for kind in ('maintenance','assignment')}
        value['comparison_history']=history
        value['comparison_history_revision']=identity(history)
        return value

    @_write_transaction
    def review_finding(self,payload):
        payload=dict(payload)
        _id(payload.get('id'))
        for key in ('reviewer','rationale'):
            _text(payload.get(key),key)
        if payload.get('disposition') not in DISPOSITIONS:
            raise ValueError('Unknown finding disposition')
        job=self._job(payload.get('job_id'))
        index=payload.get('finding_index')
        if type(index) is not int or not 0<=index<len(job['findings']):
            raise ValueError('Unknown finding index')
        repair=payload.get('maintenance_id')
        if repair:
            event=self._latest('maintenance',repair)
            if event is None:
                raise ValueError('Unknown repair/maintenance record')
            if event['robot_id']!=job['provenance']['context'].get('robot_id'):
                raise ValueError('Maintenance robot differs from incident robot')
        validation=payload.get('validation_job_id')
        if validation:
            check=self._job(validation)
            if check['provenance']['context'].get('run_id')==job['provenance']['context'].get('run_id'):
                raise ValueError('Validation must use an independent later run')
            incident_end=_ns(job['provenance']['context'].get('end_utc_ns'),'incident end_utc_ns')
            validation_start=_ns(check['provenance']['context'].get('start_utc_ns'),'validation start_utc_ns')
            if validation_start<=incident_end:
                raise ValueError('Validation must be a later run with a known UTC interval')
            if check['provenance']['context'].get('robot_id')!=job['provenance']['context'].get('robot_id'):
                raise ValueError('Validation robot differs from incident robot')
        payload['finding_identity']=identity([job['job_id'],index,job['findings'][index]])
        payload['action_scope']='review_record_only'
        return self._append('finding_review',payload)

    @_write_transaction
    def regression_bundle(self,payload):
        payload=dict(payload)
        _id(payload.get('id'))
        _text(payload.get('reviewer'),'reviewer')
        review=self._latest('finding_review',payload.get('finding_review_id'))
        if not review:
            raise ValueError('An existing human finding review is required')
        incident=self._job(review['job_id'])
        validation=self._job(review['validation_job_id']) if review.get('validation_job_id') else None
        documents=[incident]+([validation] if validation else [])
        payload.update(bundle_version='regression-reference-1',finding_review=review,
            incident_finding=incident['findings'][review['finding_index']],
            maintenance=self._latest('maintenance',review['maintenance_id']) if review.get('maintenance_id') else None,
            evidence=[{'job_id':j['job_id'],'source_hashes':j['provenance']['source_hashes'],
                       'analyzer_id':j['analyzer_id'],'analyzer_version':j['analyzer_version'],
                       'importer_version':j['provenance']['importer_version'],'mapping_revision':j['provenance']['mapping_revision'],
                       'configuration':j['configuration'],'context':j['provenance']['context'],
                       'rows_digest':j['provenance']['rows_digest'],'source_type':j['provenance']['source_type']} for j in documents],
            replay_limitation='Recorded inputs remain fixed; physical repair effectiveness requires an independent supervised validation run',
            contains_raw_data=False,action_scope='reference_export_only')
        return self._append('regression_bundle',payload,immutable=True)

    def snapshot(self,limit=100):
        if type(limit) is not int or not 1<=limit<=500:
            raise ValueError('Invalid review snapshot limit')
        data={kind:self.records(kind)[-limit:] for kind in KINDS}
        jobs=self.db.execute('SELECT result_json FROM analyzer_jobs ORDER BY created_utc_ns DESC LIMIT ?',(limit,)).fetchall()
        data.update(schema_version=1,analysis_jobs=[json.loads(row[0]) for row in jobs],
                    dispositions=list(DISPOSITIONS),maintenance_types=list(MAINTENANCE_TYPES),
                    action_scope='local_review_records_only')
        return data
