"""Private streamed regression cases; source-to-analysis under retained context."""
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from functools import wraps
from .wpilog import ResourceLimit
from . import regression as old
from .analysis import canonical,identity
from .stream_regression_resources import ResourceLimits,WorkBudget,limits as resource_limits
from . import stream_regression_proof as proof

FORMAT='offline-stream-analyzer-regression-1'
SCOPE=proof.SCOPE
CHUNK_COLUMNS=('ordinal','path','sha256','size_bytes','row_count','source_ordinal','start_offset','end_offset','previous_chain','selection_chain')
META=('case.json','descriptor.json','saved-job.json')
TRUST='Original/import origins, review authority, human context and global catalog/clock revisions are retained declarations; hashes prove internal reproducibility, not historical authenticity'


def _errors(function):
    @wraps(function)
    def call(*args,**kwargs):
        try:return function(*args,**kwargs)
        except old.RegressionError:raise
        except ResourceLimit:old._fail('regression_resource_limit')
        except sqlite3.OperationalError as error:
            old._fail('regression_resource_limit' if 'interrupted' in str(error) else 'regression_evidence_unavailable')
        except OSError:old._fail('regression_evidence_unavailable')
        except Exception:old._fail('regression_operation_failed')
    return call


def _parse_file(path,bounds):return old._parse(old._read(path,bounds.metadata_bytes))
def _signature(path):
    value=old._local(path).stat()
    return [value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,getattr(value,'st_birthtime_ns',value.st_ctime_ns)]
def _item(path,name,pin=None,maximum=None):
    path=old._local(path);size=path.stat().st_size
    cap=16*1024**3 if maximum is None else maximum
    if size>cap:old._fail('regression_resource_limit')
    if pin is not None and size!=pin['size_bytes']:old._fail('regression_integrity')
    digest,actual=old._hash(path,size)
    return dict(path=name,sha256=digest,size_bytes=actual)

def _line(value,bounds):return old._json_bytes(value,bounds.inventory_line_bytes)
def _write(path,value,bounds,guard):
    encoded=old._json_bytes(value,bounds.metadata_bytes);guard.before_write('metadata',len(encoded));old._write(path,encoded)


def copy_file(source,target,pin,bounds,guard,*,wpilog=False):
    maximum=min(bounds.max_original_bytes if wpilog else bounds.max_bundle_bytes,pin['size_bytes'])
    guard.before_write('copy',min(maximum,1024*1024))
    source=old._local(source);signature=_signature(source);digest=hashlib.sha256();total=0
    with source.open('rb') as incoming,target.open('xb') as output:
        before=os.fstat(incoming.fileno())
        if (before.st_dev,before.st_ino)!=(signature[0],signature[1]):old._fail('regression_integrity')
        while True:
            block=incoming.read(min(1024*1024,maximum-total+1))
            if not block:break
            if wpilog and not total and not block.startswith(b'WPILOG'):old._fail('regression_integrity')
            total+=len(block)
            if total>maximum:old._fail('regression_resource_limit')
            guard.before_write('copy',len(block));output.write(block);digest.update(block)
        output.flush();os.fsync(output.fileno())
        after=os.fstat(incoming.fileno())
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):old._fail('regression_integrity')
    if _signature(source)!=signature or total!=pin['size_bytes'] or digest.hexdigest()!=pin['sha256']:old._fail('regression_integrity')


def _publish_manifest(folder,value,bounds,guard):
    encoded=old._json_bytes(value,bounds.metadata_bytes);guard.before_write('manifest',2*len(encoded))
    partial=folder/'.manifest.part';old._write(partial,encoded)
    if os.name!='nt':
        for directory in (folder/'chunks',folder/'originals',folder):
            descriptor=os.open(directory,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
    os.link(partial,folder/'manifest.json');partial.unlink()
    if os.name!='nt':
        descriptor=os.open(folder,os.O_RDONLY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)


def _inventory(folder,bounds):
    path=old._local(folder/'chunk-inventory.jsonl')
    with path.open('rb') as stream:
        ordinal=0
        while True:
            line=stream.readline(bounds.inventory_line_bytes+1)
            if not line:break
            if len(line)>bounds.inventory_line_bytes or not line.endswith(b'\n'):old._fail('regression_resource_limit')
            row=old._parse(line)
            if (type(row) is not dict or set(row)!={'transport_path',*CHUNK_COLUMNS} or row['ordinal']!=ordinal
                or row['transport_path']!=f'chunks/{ordinal:08d}.jsonl' or not old._sha(row['sha256'])
                or any(not old._integer(row[key],(1<<63)-1) for key in ('ordinal','size_bytes','row_count','source_ordinal','start_offset','end_offset'))
                or row['size_bytes']>1024*1024 or row['size_bytes']==0 or not old._sha(row['previous_chain']) or not old._sha(row['selection_chain'])):old._fail('regression_integrity')
            if ordinal>=bounds.max_chunks:old._fail('regression_resource_limit')
            ordinal+=1;yield row


def _validate(case,descriptor,saved,bounds):
    from . import reports
    from .wpilog import PROFILE,EXTRACTOR_VERSION,MAPPING_REVISION
    fields={'schema_version','format','scope','trust_boundary','review','review_links','expectations','job_id','analyzer_id','analyzer_version','imports'}
    if type(case) is not dict or set(case)!=fields or case['schema_version']!=1 or case['format']!=FORMAT or case['scope']!=SCOPE or case['trust_boundary']!=TRUST:old._fail()
    old._expectations(case['expectations'])
    if type(saved) is not dict or set(saved)!=old.JOB_FIELDS:old._fail('regression_integrity')
    if type(descriptor) is not dict or descriptor.get('input_format')!='stream-evidence-1' or descriptor.get('full_source_verified') is not True:old._fail('regression_integrity')
    context=descriptor.get('context',{});policy=context.get('pipeline_version')
    if policy not in proof.PROOFS:old._fail('regression_evidence_unsupported')
    if (descriptor.get('framework')!=reports.STREAM_FRAMEWORK or descriptor.get('profile')!=PROFILE or descriptor.get('importer_version')!=EXTRACTOR_VERSION
        or descriptor.get('mapping_revision')!=MAPPING_REVISION):old._fail('regression_evidence_unsupported')
    if proof._versions(policy).get(case['analyzer_id'])!=case['analyzer_version']:old._fail('regression_evidence_unsupported')
    if not old._integer(descriptor.get('generation'),1000000) or descriptor['generation']<1:old._fail('regression_integrity')
    sid=descriptor.get('stream_id')
    if not old._sha(sid):old._fail('regression_integrity')
    final=descriptor.get('final_manifest',{})
    if final.get('path')!=f"analysis/streams/{sid}/g{descriptor['generation']}/manifest.jsonl" or not old._sha(final.get('sha256')) or not old._integer(final.get('size_bytes'),(1<<63)-1):old._fail('regression_integrity')
    if final['size_bytes']>bounds.max_bundle_bytes:old._fail('regression_resource_limit')
    reducer=proof.PROOFS[policy]()
    if context.get('stream_reducer_id')!=reducer.reducer_id+'@'+reducer.version or any(context.get(k)!=v for k,v in getattr(reducer,'context_versions',{}).items()):old._fail('regression_evidence_unsupported')
    if descriptor.get('input_digest')!=identity({k:v for k,v in descriptor.items() if k not in ('input_digest','result')}):old._fail('regression_integrity')
    if saved['provenance']!={k:v for k,v in descriptor.items() if k!='result'}:old._fail('regression_integrity')
    for key in ('job_id','analyzer_id','analyzer_version'):
        if saved[key]!=case[key]:old._fail('regression_integrity')
    if identity([reports.STREAM_FRAMEWORK,saved['declaration'],saved['provenance'],saved['configuration']])!=case['job_id']:old._fail('regression_integrity')
    review=case['review'];index=review.get('finding_index') if isinstance(review,dict) else None
    if (not isinstance(review,dict) or not old._id(review.get('id')) or type(review.get('revision')) is not int or not 1<=review['revision']<=1000000
        or review.get('job_id')!=case['job_id'] or review.get('action_scope')!='review_record_only' or not old._text(review.get('reviewer')) or not old._text(review.get('rationale'))
        or not old._integer(index,len(saved['findings'])-1) or review.get('finding_identity')!=identity([case['job_id'],index,saved['findings'][index]])
        or case['review_links']!=old._review_links(review)):old._fail('regression_integrity')
    from .review import DISPOSITIONS
    if review.get('disposition') not in DISPOSITIONS:old._fail('regression_integrity')
    pins=descriptor.get('source_pins')
    if not isinstance(pins,list) or not 1<=len(pins)<=bounds.max_sources or len(case['imports'])!=len(pins):old._fail('regression_resource_limit')
    ids=[];digests=[];total=0
    for pin,imported in zip(pins,case['imports']):
        job=pin.get('import_job_id');sha=pin.get('source_sha256')
        if not old._sha(job) or job in ids or not old._sha(sha) or pin.get('source_type') not in ('SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER'):old._fail('regression_integrity')
        ids.append(job);digests.append(sha)
        if (pin.get('profile'),pin.get('extractor_version'),pin.get('mapping_revision'))!=(PROFILE,EXTRACTOR_VERSION,MAPPING_REVISION):old._fail('regression_evidence_unsupported')
        expected={'raw':f'raw/{sha[:2]}/{sha}.wpilog','dataset':f'derived/{job}/signals.jsonl','manifest':f'derived/{job}/manifest.json'}
        if set(pin['files'])!=set(expected):old._fail('regression_integrity')
        for kind,path in expected.items():
            value=pin['files'][kind]
            if value.get('path')!=path or not old._sha(value.get('sha256')) or not old._integer(value.get('size_bytes'),(1<<63)-1):old._fail('regression_integrity')
            if value['size_bytes']>bounds.max_bundle_bytes:old._fail('regression_resource_limit')
        raw=pin['files']['raw']
        if raw['sha256']!=sha or raw['size_bytes']>bounds.max_original_bytes:old._fail('regression_resource_limit')
        total+=raw['size_bytes']
        required=dict(id=job,artifact_sha256=sha,profile=PROFILE,extractor_version=EXTRACTOR_VERSION,mapping_revision=MAPPING_REVISION,
            dataset_sha256=pin['files']['dataset']['sha256'],manifest_sha256=pin['files']['manifest']['sha256'],source_type=pin['source_type'])
        if not isinstance(imported,dict) or set(imported)!={*required,'state'} or any(imported[k]!=v for k,v in required.items()) or imported['state'] not in ('succeeded','succeeded_with_unsupported'):old._fail('regression_integrity')
    if total>bounds.max_original_total:old._fail('regression_resource_limit')
    if sorted(set(digests))!=descriptor.get('source_hashes') or descriptor.get('source_count')!=len(pins):old._fail('regression_integrity')
    if context.get('row_selection')!='full_source_scoped_rows_1' or descriptor['run'].get('segment_ids')!=sorted(descriptor['run'].get('segment_ids',[])):
        old._fail('regression_evidence_unsupported')


def _load(folder,bounds,*,manifest=None,partial=False):
    folder=old._local(folder);manifest=manifest if manifest is not None else _parse_file(folder/'manifest.json',bounds)
    if type(manifest) is not dict or set(manifest)!={'schema_version','format','files','chunk_count','total_bytes'} or manifest['format']!=FORMAT or manifest['schema_version']!=1 or not isinstance(manifest['files'],list):old._fail()
    if len(manifest['files'])>bounds.max_sources+5:old._fail('regression_resource_limit')
    files={};total=0
    for entry in manifest['files']:
        if type(entry) is not dict or set(entry)!={'path','sha256','size_bytes'} or not old._sha(entry['sha256']):old._fail()
        name=entry['path']
        if name not in (*META,'chunk-inventory.jsonl','stream-manifest.jsonl') and not (isinstance(name,str) and old.re.fullmatch('originals/[a-f0-9]{64}\\.wpilog',name)):old._fail('unsafe_regression_path')
        if name in files:old._fail()
        maximum=bounds.max_original_bytes if name.startswith('originals/') else bounds.metadata_bytes if name in META else bounds.max_bundle_bytes
        if not old._integer(entry['size_bytes'],maximum):old._fail('regression_resource_limit')
        if total+entry['size_bytes']>bounds.max_bundle_bytes:old._fail('regression_resource_limit')
        actual=_item(folder/name,name,entry,maximum)
        if actual!=entry:old._fail('regression_integrity')
        files[name]=entry;total+=entry['size_bytes']
    if not set((*META,'chunk-inventory.jsonl','stream-manifest.jsonl'))<=set(files):old._fail('regression_integrity')
    case,descriptor,saved=(_parse_file(folder/name,bounds) for name in META)
    _validate(case,descriptor,saved,bounds)
    expected_originals={'originals/'+p['source_sha256']+'.wpilog' for p in descriptor['source_pins']}
    if {name for name in files if name.startswith('originals/')}!=expected_originals:old._fail('regression_integrity')
    count=0
    for chunk in _inventory(folder,bounds):
        if chunk['path']!=f"analysis/streams/{descriptor['stream_id']}/g{descriptor['generation']}/{chunk['ordinal']:012d}.jsonl":old._fail('unsafe_regression_path')
        if _item(folder/chunk['transport_path'],chunk['transport_path'],chunk,1024*1024)!=dict(path=chunk['transport_path'],sha256=chunk['sha256'],size_bytes=chunk['size_bytes']):old._fail('regression_integrity')
        total+=chunk['size_bytes'];count+=1
        if total>bounds.max_bundle_bytes:old._fail('regression_resource_limit')
    total+=len(old._json_bytes(manifest,bounds.metadata_bytes))
    if total>bounds.max_bundle_bytes:old._fail('regression_resource_limit')
    if count!=manifest['chunk_count'] or count!=descriptor['chunk_count'] or total!=manifest['total_bytes']:old._fail('regression_integrity')
    for directory,names in ((folder,set((*META,'chunk-inventory.jsonl','stream-manifest.jsonl','originals','chunks'))|({'manifest.json'} if not partial else {'.capture.jsonl'})),
                            (folder/'originals',{n.split('/')[1] for n in expected_originals})):
        actual=set()
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name not in names:old._fail('regression_integrity')
                old._local(Path(entry.path));actual.add(entry.name)
        if actual!=names:old._fail('regression_integrity')
    actual_count=0
    with os.scandir(folder/'chunks') as entries:
        for entry in entries:
            if not old.re.fullmatch('[0-9]{8}\\.jsonl',entry.name) or int(entry.name[:8])>=count or not old._local(Path(entry.path)).is_file():old._fail('regression_integrity')
            actual_count+=1
            if actual_count>bounds.max_chunks:old._fail('regression_resource_limit')
    if actual_count!=count:old._fail('regression_integrity')
    return dict(folder=folder,case=case,descriptor=descriptor,saved=saved,manifest=manifest,total_bytes=total+(folder/'.capture.jsonl').stat().st_size if partial else total)


def compare_chunks(bundle,root,db,sid,generation,bounds):
    ordinal=-1
    for expected in _inventory(bundle['folder'],bounds):
        cursor=db.execute('SELECT '+','.join(CHUNK_COLUMNS)+' FROM analysis_stream_chunks WHERE stream_id=? AND generation=? AND ordinal=?',(sid,generation,expected['ordinal']))
        row=cursor.fetchone()
        if row is None or dict(zip(CHUNK_COLUMNS,row))!={k:expected[k] for k in CHUNK_COLUMNS}:old._fail('regression_integrity')
        # Equal hashes are independently recomputed over complete fresh+case bytes.
        if _item(root/expected['path'],expected['transport_path'],expected,1024*1024)!=_item(bundle['folder']/expected['transport_path'],expected['transport_path'],expected,1024*1024):old._fail('regression_integrity')
        ordinal=expected['ordinal']
    if db.execute('SELECT COUNT(*) FROM analysis_stream_chunks WHERE stream_id=? AND generation=?',(sid,generation)).fetchone()[0]!=ordinal+1:old._fail('regression_integrity')
    final=bundle['descriptor']['final_manifest']
    if _item(root/final['path'],'stream-manifest.jsonl',final,bounds.max_bundle_bytes)!=_item(bundle['folder']/'stream-manifest.jsonl','stream-manifest.jsonl',final,bounds.max_bundle_bytes):old._fail('regression_integrity')


def _receipt(bundle):
    case=bundle['case']
    return dict(schema_version=1,format=FORMAT,case_id=identity(case),envelope_id=identity(bundle['manifest']),scope=SCOPE,trust_boundary=TRUST,
        analyzer_id=case['analyzer_id'],stored_analyzer_version=case['analyzer_version'],source_type=bundle['descriptor']['source_type'],
        reference_scope='incident_evidence_only',review_links=case['review_links'],robot_replay_executed=False,overall_health='not_assessed',hardware_qualified=False,
        original_count=len(bundle['descriptor']['source_pins']),chunk_count=bundle['manifest']['chunk_count'],bundle_bytes=bundle['manifest']['total_bytes'])


@_errors
def verify_case(folder,*,limits=None):
    bounds=resource_limits(limits);bundle=_load(folder,bounds);actual=proof.execute(bundle,bounds)
    again=_load(folder,bounds)
    if identity(again['case'])!=identity(bundle['case']) or identity(again['manifest'])!=identity(bundle['manifest']):old._fail('regression_integrity')
    return dict(_receipt(bundle),verified=True,stored_input_digest=actual['input_digest'],proof_code_identity=actual['code_identity'])


@_errors
def run_case(folder,*,candidate=None,allow_analyzer_version_change=False,limits=None):
    if type(allow_analyzer_version_change) is not bool or candidate is not None and not old._text(candidate,512):old._fail()
    bounds=resource_limits(limits);bundle=_load(folder,bounds);stored=proof.execute(bundle,bounds)
    receipt=dict(_receipt(bundle),state='unavailable',code='regression_analyzer_unavailable',checks=[],stored_input_digest=stored['input_digest'],proof_code_identity=stored['code_identity'])
    name=candidate or bundle['descriptor']['context']['pipeline_version']+':'+bundle['case']['analyzer_id']
    selected=proof.CANDIDATES.get(name)
    if selected is None:return receipt
    if (selected.analyzer_id!=bundle['case']['analyzer_id'] or selected.pipeline_version!=bundle['descriptor']['context']['pipeline_version']
        or selected.input_contract!=proof.input_contract(selected.pipeline_version)):
        old._fail('regression_evidence_unsupported')
    changed=selected.analyzer_version!=bundle['case']['analyzer_version']
    receipt.update(candidate=name,current_analyzer_version=selected.analyzer_version,analyzer_version_changed=changed)
    if changed and not allow_analyzer_version_change:return dict(receipt,code='regression_analyzer_version_changed')
    again=_load(folder,bounds)
    if identity(again['case'])!=identity(bundle['case']) or identity(again['manifest'])!=identity(bundle['manifest']):old._fail('regression_integrity')
    current=proof.execute(again,bounds,candidate=selected)
    final=_load(folder,bounds)
    if identity(final['case'])!=identity(bundle['case']) or identity(final['manifest'])!=identity(bundle['manifest']):old._fail('regression_integrity')
    result=current['check'];checks=[];expected=bundle['case']['expectations']
    for check in expected['checks']:
        if check['kind']=='metric':
            matches=[m for m in result['metrics'] if all(type(m.get(k)) is type(v) and m.get(k)==v for k,v in check['selector'].items())]
            value=matches[0].get('value') if len(matches)==1 else None;valid=old._finite(value)
        else:value=sum(f.get('kind')==check['finding_kind'] for f in result['findings']);valid=True
        passed=valid and ('min' not in check or value>=check['min']) and ('max' not in check or value<=check['max'])
        checks.append(dict(expectation=check,state='passed' if passed else 'failed',actual=value if valid else None))
    passed=result['outcome']==expected['expected_outcome'] and all(c['state']=='passed' for c in checks)
    return dict(receipt,state='passed' if passed else 'failed',code='regression_expectations_passed' if passed else 'regression_expectations_failed',checks=checks,
        current_job_id=result['job_id'],current_outcome=result['outcome'],expected_outcome=expected['expected_outcome'],candidate_input_digest=current['input_digest'],candidate_code_identity=current['code_identity'])


@_errors
def export_case(root,*,review_id,review_revision,expectations,output,limits=None):
    """Snapshot metadata first; release WAL reader before copying immutable files."""
    from .regression_evidence import _bounded_row
    bounds=resource_limits(limits);root=old._local(root);output=old._local(output,exists=False)
    repository=Path(__file__).resolve().parents[1]
    if (output.exists() or any((ancestor/'.git').exists() for ancestor in output.parents)
        or any(output==base or output.is_relative_to(base) or base.is_relative_to(output) for base in (root,repository))):old._fail('unsafe_regression_output')
    if not old._id(review_id) or type(review_revision) is not int or not 1<=review_revision<=1000000:old._fail('invalid_regression_reference')
    expectations=old._expectations(_parse_file(Path(expectations),bounds) if isinstance(expectations,(str,Path)) else expectations)
    output.mkdir(mode=0o700);(output/'originals').mkdir();(output/'chunks').mkdir();guard=WorkBudget(output,bounds)
    guard.limits=ResourceLimits(**{**bounds.__dict__,'max_temp_bytes':bounds.max_bundle_bytes})
    sources=[]
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
        db.execute('PRAGMA query_only=ON');steps=0
        def progress():
            nonlocal steps
            steps+=1000;return steps>bounds.sql_steps
        db.set_progress_handler(progress,1000);db.execute('BEGIN')
        review=old._parse(_bounded_row(db,'review_records',('payload_json',),"kind='finding_review' AND record_id=? AND revision=?",(review_id,review_revision),limit=bounds.metadata_bytes)['payload_json'])
        columns=('job_id','framework_version','analyzer_id','analyzer_version','declaration_json','input_json','configuration_json','outcome','result_json')
        savedrow=_bounded_row(db,'analyzer_jobs',columns,'job_id=?',(review.get('job_id'),),limit=bounds.metadata_bytes)
        saved=old._parse(savedrow['result_json']);provenance=old._parse(savedrow['input_json']);sid=provenance.get('stream_id')
        job=_bounded_row(db,'analysis_stream_jobs',('stream_id','generation','state','descriptor_json','chunk_count','report_id'),'stream_id=?',(sid,),limit=bounds.metadata_bytes)
        if job['state']!='ready' or not job['report_id']:old._fail('regression_evidence_unavailable')
        descriptor=old._parse(job['descriptor_json']);imports=[]
        for pin in descriptor.get('source_pins',[]):
            if len(imports)>=bounds.max_sources:old._fail('regression_resource_limit')
            imported=_bounded_row(db,'import_jobs',('id','artifact_sha256','profile','extractor_version','mapping_revision','dataset_sha256','manifest_sha256','state'),'id=?',(pin['import_job_id'],),limit=16384)
            artifact=_bounded_row(db,'import_artifacts',('sha256','size_bytes','relative_path','source_type'),'sha256=?',(pin['source_sha256'],),limit=16384)
            if artifact['relative_path']!=pin['files']['raw']['path'] or artifact['size_bytes']!=pin['files']['raw']['size_bytes']:old._fail('regression_integrity')
            imported['source_type']=artifact['source_type'];imports.append(imported)
            for item in pin['files'].values():sources.append((root/item['path'],_signature(root/item['path'])))
        case=dict(schema_version=1,format=FORMAT,scope=SCOPE,trust_boundary=TRUST,review=review,review_links=old._review_links(review),expectations=expectations,
            job_id=saved['job_id'],analyzer_id=saved['analyzer_id'],analyzer_version=saved['analyzer_version'],imports=imports)
        _validate(case,descriptor,saved,bounds)
        if (savedrow['framework_version']!=descriptor['framework'] or any(savedrow[k]!=saved[k] for k in ('job_id','analyzer_id','analyzer_version','outcome'))
            or old._parse(savedrow['declaration_json'])!=saved['declaration'] or old._parse(savedrow['configuration_json'])!=saved['configuration']):old._fail('regression_integrity')
        for name,value in zip(META,(case,descriptor,saved)):_write(output/name,value,bounds,guard)
        ordinal=-1;count=0
        with (output/'.capture.jsonl').open('xb') as inventory:
            while True:
                rows=db.execute('SELECT '+','.join(CHUNK_COLUMNS)+' FROM analysis_stream_chunks WHERE stream_id=? AND generation=? AND ordinal>? ORDER BY ordinal LIMIT ?',
                    (sid,job['generation'],ordinal,bounds.page_rows)).fetchall()
                if not rows:break
                for row in rows:
                    chunk=dict(zip(CHUNK_COLUMNS,row))
                    if chunk['ordinal']!=count or count>=bounds.max_chunks:old._fail('regression_resource_limit')
                    if chunk['path']!=f"analysis/streams/{sid}/g{job['generation']}/{count:012d}.jsonl":old._fail('unsafe_regression_path')
                    chunk['transport_path']=f'chunks/{count:08d}.jsonl';chunk['capture_signature']=_signature(root/chunk['path'])
                    encoded=_line(chunk,bounds);guard.before_write('inventory',len(encoded));inventory.write(encoded);count+=1;ordinal=chunk['ordinal']
            inventory.flush();os.fsync(inventory.fileno())
        if count!=job['chunk_count'] or count!=descriptor['chunk_count']:old._fail('regression_integrity')
        final=descriptor['final_manifest'];sources.append((root/final['path'],_signature(root/final['path'])))
    # The source read transaction is closed here; copies use pinned bytes/identities.
    for pin in descriptor['source_pins']:
        copy_file(root/pin['files']['raw']['path'],output/('originals/'+pin['source_sha256']+'.wpilog'),pin['files']['raw'],bounds,guard,wpilog=True)
    with (output/'.capture.jsonl').open('rb') as incoming,(output/'chunk-inventory.jsonl').open('xb') as inventory:
        for line in incoming:
            chunk=old._parse(line);signature=chunk.pop('capture_signature')
            if _signature(root/chunk['path'])!=signature:old._fail('regression_integrity')
            copy_file(root/chunk['path'],output/chunk['transport_path'],chunk,bounds,guard)
            encoded=_line(chunk,bounds);guard.before_write('inventory',len(encoded));inventory.write(encoded)
        inventory.flush();os.fsync(inventory.fileno())
    copy_file(root/final['path'],output/'stream-manifest.jsonl',final,bounds,guard)
    names=[*META,'chunk-inventory.jsonl','stream-manifest.jsonl']+['originals/'+p['source_sha256']+'.wpilog' for p in descriptor['source_pins']]
    files=[_item(output/name,name) for name in names]
    total=sum(item['size_bytes'] for item in files)+sum(c['size_bytes'] for c in _inventory(output,bounds))
    manifest=dict(schema_version=1,format=FORMAT,files=sorted(files,key=lambda i:i['path']),chunk_count=count,total_bytes=total)
    for _ in range(12):
        actual_total=total+len(old._json_bytes(manifest,bounds.metadata_bytes))
        if actual_total==manifest['total_bytes']:break
        manifest['total_bytes']=actual_total
    else:old._fail('regression_integrity')
    bundle=_load(output,bounds,manifest=manifest,partial=True);actual=proof.execute(bundle,bounds)
    for path,signature in sources:
        if _signature(path)!=signature:old._fail('regression_integrity')
    with (output/'.capture.jsonl').open('rb') as incoming:
        for line in incoming:
            chunk=old._parse(line)
            if _signature(root/chunk['path'])!=chunk['capture_signature']:old._fail('regression_integrity')
    _load(output,bounds,manifest=manifest,partial=True)
    (output/'.capture.jsonl').unlink()
    _publish_manifest(output,manifest,bounds,guard)
    return dict(_receipt(bundle),verified=True,stored_input_digest=actual['input_digest'],proof_code_identity=actual['code_identity'])
