"""Original-to-stream proof and explicitly registered fresh candidate execution."""
from contextlib import closing
from dataclasses import dataclass
import copy
import hashlib
import marshal
from pathlib import Path
import platform
import shutil
import tempfile
from .analysis import canonical,identity
from . import reports
from . import regression as legacy
from .stream_analysis import StreamRunner
from .stream_regression_resources import WorkBudget,preflight
from .storage import DataRootOwner,open_catalog
from .importer import Importer

POLICIES=('automatic-reports-8','automatic-reports-9','automatic-reports-10','automatic-reports-11')
SCOPE='source_to_analysis_reproducibility_under_retained_run_context'
NUMERICAL_KEYS=frozenset(('stream_tracking_version','stream_tracking_numerics','stream_tracking_work_version','stream_steering_version','stream_steering_numerics'))
PROOFS={policy:(lambda p=policy:reports._stream_reducer(p)) for policy in POLICIES}

@dataclass(frozen=True)
class Candidate:
    pipeline_version:str
    analyzer_version:str
    input_contract:str
    factory:object
    analyzer_id:str=None


def input_contract(policy):
    # Separate fixed admission/mapping/layout policies;11 does not reinterpret10.
    if policy not in POLICIES:legacy._fail('regression_evidence_unsupported')
    return 'fixed-stream-input-contract:'+policy


def _versions(policy):
    from .stream_quality import VERSION as quality
    from .stream_status import PHOENIX_VERSION,SYSTEM_VERSION
    from .stream_tracking import VERSION as tracking
    from .stream_steering import VERSION as steering
    out={'recording-quality':quality}
    if policy in POLICIES[1:]:out.update({'phoenix-status-observation':PHOENIX_VERSION,'system-status-observation':SYSTEM_VERSION})
    if policy in POLICIES[2:]:out['swerve-tracking']=tracking
    if policy==POLICIES[3]:out['recorded-steering-observation']=steering
    return out

CANDIDATES={policy+':'+analyzer:Candidate(policy,version,input_contract(policy),PROOFS[policy],analyzer)
            for policy in POLICIES for analyzer,version in _versions(policy).items()}


def code_identity(reducer):
    import importlib
    names=('analysis','wpilog','importer','reports','stream_analysis','stream_quality','stream_mapper','stream_status',
           'stream_motion_mapper','stream_tracking','stream_tracking_store','stream_steering','stream_motion','stream_regression','stream_regression_resources','stream_regression_proof')
    modules={name:hashlib.sha256(Path(importlib.import_module('.'+name,__package__).__file__).read_bytes()).hexdigest() for name in names}
    loaded={}
    for name in ('initial','consume','consume_chunk','validate_step','finalize_step','finish'):
        method=getattr(reducer,name,None)
        if hasattr(method,'__code__'):loaded[name]=hashlib.sha256(marshal.dumps(method.__code__)).hexdigest()
    return dict(python_version=platform.python_version(),evaluator_module_sha256=modules,loaded_evaluator_code_sha256=loaded)


# Retain actual ownership when a close is incomplete; no detached cleanup worker.
_RETAINED_WORK={}

class _Workspace:
    def __init__(self,bounds,external_bytes):
        self.root=Path(tempfile.mkdtemp(prefix='hub-stream-regression-work-')).resolve()
        self.owner=None;self.db=None;self.runner=None;self.guard=WorkBudget(self.root,bounds)
        self.guard.external_bytes=external_bytes
    def __enter__(self):
        try:
            self.guard.check();self.owner=DataRootOwner(self.root);self.db=open_catalog(self.root)
            return self
        except BaseException:
            if not self.retire():_RETAINED_WORK[self.root.name]=self
            raise
    def retire(self):
        try:
            if self.runner is not None and self.runner.close() is not True:return False
            if self.db is not None:self.db.close();self.db=None
            if self.owner is not None:self.owner.close();self.owner=None
            if self.root.exists():
                if self.root.resolve()!=self.root or not self.root.name.startswith('hub-stream-regression-work-'):return False
                old_path=legacy._local(self.root)
                if old_path!=self.root:return False
                shutil.rmtree(self.root)
            return True
        except Exception:return False
    def __exit__(self,*exc):
        if not self.retire():
            _RETAINED_WORK[self.root.name]=self
            legacy._fail('regression_cleanup_pending')


def cleanup_retained_work():
    for key,workspace in list(_RETAINED_WORK.items()):
        if workspace.retire():_RETAINED_WORK.pop(key,None)
    return not _RETAINED_WORK


def retained_work_status():
    return dict(cleanup_pending=bool(_RETAINED_WORK),owned_workspace_count=len(_RETAINED_WORK))


def execute(bundle,bounds,*,candidate=None):
    """Reimport and reduce in private owned work; never consume transported work."""
    from . import stream_regression as transport
    descriptor=bundle['descriptor'];policy=descriptor['context']['pipeline_version'];case=bundle['case']
    if policy not in PROOFS:legacy._fail('regression_evidence_unsupported')
    if _RETAINED_WORK:legacy._fail('regression_cleanup_pending')
    if candidate is not None and (candidate.pipeline_version!=policy or candidate.input_contract!=input_contract(policy) or candidate.analyzer_id!=case['analyzer_id']):legacy._fail('regression_evidence_unsupported')
    factory=PROOFS[policy] if candidate is None else candidate.factory
    reducer=factory()
    if getattr(reducer,'pipeline_version',POLICIES[0])!=policy:legacy._fail('regression_evidence_unsupported')
    context=copy.deepcopy(descriptor['context']);run=copy.deepcopy(descriptor['run'])
    if candidate is not None:
        if candidate.pipeline_version!=policy or candidate.input_contract!=input_contract(policy):legacy._fail('regression_evidence_unsupported')
        for key,value in getattr(reducer,'context_versions',{}).items():
            if key in context and context[key]!=value and key not in NUMERICAL_KEYS:legacy._fail('regression_evidence_unsupported')
            context[key]=value
    with _Workspace(bounds,bundle['total_bytes']) as workspace:
        root=workspace.root;db=workspace.db;guard=workspace.guard
        importer=Importer(root,db,resource_guard=guard)
        for pin,imported in zip(descriptor['source_pins'],case['imports']):
            original=bundle['folder']/('originals/'+pin['source_sha256']+'.wpilog')
            incoming=root/'incoming.wpilog'
            transport.copy_file(original,incoming,pin['files']['raw'],bounds,guard,wpilog=True)
            try:
                preflight(incoming,bounds);guard.begin_decode()
                decoded=importer.import_file(incoming,profile=pin['profile'],expected_sha256=pin['source_sha256'],source_type=pin['source_type'],mapping_revision=pin['mapping_revision'])
            finally:incoming.unlink(missing_ok=True)
            if decoded['state']=='failed' and decoded['error_code']=='resource_limit':legacy._fail('regression_resource_limit')
            if decoded['state'] not in ('succeeded','succeeded_with_unsupported') or decoded['id']!=pin['import_job_id'] or decoded['state']!=imported['state']:legacy._fail('regression_integrity')
            for kind,column in (('dataset','dataset_sha256'),('manifest','manifest_sha256')):
                if decoded[column]!=pin['files'][kind]['sha256']:legacy._fail('regression_integrity')
            guard.check()
        runner=StreamRunner(root,db,reducer=reducer,limits=dict(min_free_bytes=bounds.min_free_bytes,
            max_spool_bytes=bounds.max_temp_bytes,chunk_bytes=context['stream_chunk_bytes'],line_bytes=context['stream_line_bytes']))
        workspace.runner=runner
        sid=runner.schedule(run,context)['stream_id']
        generation=descriptor['generation']
        if generation!=1:
            with db:
                for table in ('analysis_stream_jobs','analysis_stream_sources','analysis_stream_checkpoints'):
                    db.execute('UPDATE '+table+' SET generation=? WHERE stream_id=?',(generation,sid))
        while True:
            guard.check(2*context['stream_chunk_bytes'])
            progress=runner.step(sid,max_rows=1000,max_bytes=1024*1024,max_seconds=.025)
            if progress['state'] in ('failed','canceled'):
                legacy._fail('regression_resource_limit' if 'resource' in str(progress.get('error_code')) or 'space' in str(progress.get('error_code')) else 'regression_integrity')
            if progress['state']=='ready':break
        actual=progress['descriptor']
        if candidate is None:
            guard.before_write('report',48*len(canonical(actual).encode('utf-8')))
            report=reports.publish_stream_report(db,sid,actual)
            guard.check()
            check=next((c for c in report['checks'] if c['analyzer_id']==case['analyzer_id']),None)
        else:
            output=actual['result'] if policy==POLICIES[0] and case['analyzer_id']=='recording-quality' else actual['result'].get('checks',{}).get(case['analyzer_id'])
            if not isinstance(output,dict):legacy._fail('regression_analyzer_unavailable')
            declaration=copy.deepcopy(bundle['saved']['declaration']);declaration['version']=candidate.analyzer_version
            provenance={k:v for k,v in actual.items() if k!='result'};configuration=copy.deepcopy(bundle['saved']['configuration'])
            check=copy.deepcopy(output)
            check.update(job_id=identity([reports.STREAM_FRAMEWORK,declaration,provenance,configuration]),
                analyzer_id=case['analyzer_id'],analyzer_version=candidate.analyzer_version,provenance=provenance,configuration=configuration,declaration=declaration)
            if set(check)!=legacy.JOB_FIELDS:legacy._fail('regression_result_invalid')
            legacy._json_bytes(check,bounds.metadata_bytes)
        if check is None:legacy._fail('regression_evidence_unsupported')
        if candidate is None:
            if canonical(actual)!=canonical(descriptor) or canonical(check)!=canonical(bundle['saved']):legacy._fail('regression_integrity')
            transport.compare_chunks(bundle,root,db,sid,generation,bounds)
        return dict(check=check,input_digest=actual['input_digest'],stream_id=sid,code_identity=code_identity(reducer))
