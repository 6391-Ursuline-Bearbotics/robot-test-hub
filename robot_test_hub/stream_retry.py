"""Explicit exact-attempt retries. HTTP queues metadata; the owned worker applies."""
import hashlib
import json
import re
import sqlite3
import time
from .analysis import canonical,identity
from .report_lookup import atomic,_normalized
from .stream_analysis import SQL as STREAM_SQL,INITIAL_CHAIN,VERSION,_portable

PIN_FIELDS=('stream_id','expected_generation','expected_state','expected_updated_utc_ns')
FIELDS=('id','stream_id','run_id','expected_generation','expected_state','expected_updated_utc_ns',
        'state','error_code','created_utc_ns','updated_utc_ns','result_generation','result_state','result_updated_utc_ns')
REQUEST_COLUMNS=','.join(FIELDS)
RETRY_CODES=('stream_free_space_unavailable','stream_spool_resource_limit')
STATES=('pending','running','completed','failed','superseded')
MAX_METADATA=1024*1024
MAX_PINS=4*1024*1024
JOB_TEXT_LIMITS={'stream_id':64,'run_id':128,'state':32,'phase':64,'context_json':MAX_METADATA,'run_json':MAX_METADATA,'source_type':32,'selection_chain':64,'error_code':128,'descriptor_json':MAX_METADATA,'report_id':64,'final_manifest_path':512,'final_manifest_sha256':64}
JOB_INTEGERS=('generation','source_count','source_ordinal','verified_source_count','verified_bytes','replay_bytes','scanned_rows','selected_rows','selected_bytes','chunk_count','spool_bytes','created_utc_ns','updated_utc_ns')
_HISTORY_BASE=STREAM_SQL['analysis_stream_jobs'].replace('analysis_stream_jobs','analysis_stream_history').replace('stream_id TEXT PRIMARY KEY','stream_id TEXT NOT NULL')
SCHEMA_SQL={
 'analysis_stream_retry_requests':'''CREATE TABLE IF NOT EXISTS analysis_stream_retry_requests (
 id TEXT PRIMARY KEY NOT NULL,stream_id TEXT NOT NULL,run_id TEXT NOT NULL,
 expected_generation INTEGER NOT NULL,expected_state TEXT NOT NULL,expected_updated_utc_ns TEXT NOT NULL,
 attempt_sha256 TEXT NOT NULL,state TEXT NOT NULL,error_code TEXT,
 created_utc_ns TEXT NOT NULL,updated_utc_ns TEXT NOT NULL,
 result_generation INTEGER,result_state TEXT,result_updated_utc_ns TEXT,
 UNIQUE(stream_id,expected_generation))''',
 'analysis_stream_history':_HISTORY_BASE[:-1]+''',request_id TEXT NOT NULL UNIQUE,
 job_json TEXT NOT NULL,job_sha256 TEXT NOT NULL,pins_sha256 TEXT NOT NULL,
 PRIMARY KEY(stream_id,generation))''',
 'analysis_stream_retry_pending':'CREATE INDEX IF NOT EXISTS analysis_stream_retry_pending ON analysis_stream_retry_requests(state,created_utc_ns,id)',
 'analysis_stream_retry_run':'CREATE INDEX IF NOT EXISTS analysis_stream_retry_run ON analysis_stream_retry_requests(run_id,stream_id)',
 'analysis_stream_history_update':"CREATE TRIGGER IF NOT EXISTS analysis_stream_history_update BEFORE UPDATE ON analysis_stream_history BEGIN SELECT RAISE(ABORT,'stream_history_immutable'); END",
 'analysis_stream_history_delete':"CREATE TRIGGER IF NOT EXISTS analysis_stream_history_delete BEFORE DELETE ON analysis_stream_history BEGIN SELECT RAISE(ABORT,'stream_history_immutable'); END",
}

class RetryError(ValueError):
    def __init__(self,status,code):super().__init__(code);self.status=status;self.code=code

def install_schema(db):
    for sql in SCHEMA_SQL.values():db.execute(sql)

def has_schema(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_stream_retry_requests'").fetchone() is not None

def validate_schema(db):
    for name,sql in SCHEMA_SQL.items():
        row=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        if row is None or row[0] is None or _normalized(row[0])!=_normalized(sql):raise ValueError('stream_retry_schema_invalid')

def _sha(value):return type(value) is str and re.fullmatch('[a-f0-9]{64}',value) is not None

def _ns(value):return type(value) is str and re.fullmatch('0|[1-9][0-9]{0,18}',value) is not None and int(value)<1<<63

def _request_id(value):return type(value) is str and re.fullmatch('[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',value) is not None

def eligibility(job):
    try:
        generation=job['generation'];updated=str(job['updated_utc_ns']);state=job['state'];sid=job['stream_id']
        if not _sha(sid) or type(generation) is not int or not 1<=generation<(1<<31)-1 or not _ns(updated):raise ValueError()
        pins=dict(stream_id=sid,expected_generation=generation,expected_state=state,expected_updated_utc_ns=updated)
        if job['report_id'] is not None:reason='report_already_published'
        elif state not in ('canceled','failed'):reason='state_not_retryable'
        elif state=='failed' and job['error_code'] not in RETRY_CODES:reason='error_not_retryable'
        else:reason='eligible'
        return dict(eligible=reason=='eligible',reason=reason,pins=pins)
    except (KeyError,TypeError,ValueError):return dict(eligible=False,reason='invalid_job_metadata',pins=None)

def project_request(row):
    result={key:row[key] for key in FIELDS}
    if (not _request_id(result['id']) or not _sha(result['stream_id']) or type(result['run_id']) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',result['run_id'])
        or type(result['expected_generation']) is not int or not 1<=result['expected_generation']<(1<<31)-1
        or result['expected_state'] not in ('failed','canceled') or result['state'] not in STATES
        or any(not _ns(result[key]) for key in ('expected_updated_utc_ns','created_utc_ns','updated_utc_ns'))
        or result['result_generation'] not in (None,result['expected_generation']+1)
        or result['result_state'] not in (None,'ready','failed','canceled')
        or (result['result_updated_utc_ns'] is not None and not _ns(result['result_updated_utc_ns']))):
        raise RetryError(503,'stream_retry_storage_unavailable')
    state=result['state'];assigned=result['result_generation'] is not None
    has_result=result['result_state'] is not None and result['result_updated_utc_ns'] is not None
    if ((state=='pending' and (assigned or result['error_code'] is not None or result['result_state'] is not None or result['result_updated_utc_ns'] is not None))
        or (state=='running' and (not assigned or result['error_code'] is not None or result['result_state'] is not None or result['result_updated_utc_ns'] is not None))
        or (state=='completed' and (not assigned or not has_result or result['result_state']!='ready' or result['error_code'] is not None))
        or (state=='failed' and (not assigned or not has_result or result['result_state'] not in ('failed','canceled') or result['error_code'] is None))
        or (state=='superseded' and (result['error_code'] is None or result['result_state'] is not None or result['result_updated_utc_ns'] is not None))):
        raise RetryError(503,'stream_retry_storage_unavailable')
    allowed=set(RETRY_CODES)|{'stream_retry_attempt_changed','stream_retry_pins_invalid','stream_retry_canceled'}
    from .stream_analysis import ERROR_CODES
    allowed.update(ERROR_CODES)
    if result['error_code'] is not None and result['error_code'] not in allowed:result['error_code']='stream_retry_storage_failed'
    return result

def _payload(stream_id,value):
    if (type(value) is not dict or set(value)!={'request_id',*PIN_FIELDS} or value['stream_id']!=stream_id
        or not _sha(stream_id) or not _request_id(value['request_id'])
        or type(value['expected_generation']) is not int or not 1<=value['expected_generation']<(1<<31)-1
        or value['expected_state'] not in ('canceled','failed') or not _ns(value['expected_updated_utc_ns'])):
        raise RetryError(400,'invalid_stream_retry')
    return value

def _job(db,sid):
    for field,limit in JOB_TEXT_LIMITS.items():
        if db.execute('SELECT 1 FROM analysis_stream_jobs WHERE stream_id=? AND length(CAST('+field+' AS BLOB))>?',(sid,limit)).fetchone():raise ValueError('stream_retry_metadata_limit')
    for field in JOB_INTEGERS:
        if db.execute("SELECT 1 FROM analysis_stream_jobs WHERE stream_id=? AND typeof("+field+")!='integer'",(sid,)).fetchone():raise ValueError('stream_retry_metadata_invalid')
    row=db.execute('SELECT length(CAST(context_json AS BLOB)),length(CAST(run_json AS BLOB)),length(CAST(descriptor_json AS BLOB)) FROM analysis_stream_jobs WHERE stream_id=?',(sid,)).fetchone()
    if row is None:return None
    if any(value is not None and value>MAX_METADATA for value in row):raise ValueError('stream_retry_metadata_limit')
    return dict(db.execute('SELECT * FROM analysis_stream_jobs WHERE stream_id=?',(sid,)).fetchone())

def _sources(db,sid,generation):
    size=db.execute('SELECT COALESCE(SUM(length(CAST(pins_json AS BLOB))),0),COUNT(*) FROM analysis_stream_sources WHERE stream_id=? AND generation=?',(sid,generation)).fetchone()
    if size[0]>MAX_PINS or not 1<=size[1]<=128:raise ValueError('stream_retry_sources_invalid')
    if db.execute('SELECT 1 FROM analysis_stream_sources WHERE stream_id=? AND generation=? AND length(CAST(import_job_id AS BLOB))>64 LIMIT 1',(sid,generation)).fetchone():raise ValueError('stream_retry_sources_invalid')
    rows=[dict(row) for row in db.execute('SELECT source_ordinal,import_job_id,pins_json FROM analysis_stream_sources WHERE stream_id=? AND generation=? ORDER BY source_ordinal LIMIT 129',(sid,generation))]
    if any(row['source_ordinal']!=index for index,row in enumerate(rows)):raise ValueError('stream_retry_sources_invalid')
    return rows

def _pins_digest(sources):return identity([dict(import_job_id=row['import_job_id'],source_ordinal=row['source_ordinal'],pins_json=row['pins_json']) for row in sources])

def _attempt(job,sources):return identity([job,_pins_digest(sources)])

def _unpublished(job):return all(job[key] is None for key in ('report_id','descriptor_json','final_manifest_path','final_manifest_sha256','final_manifest_size_bytes'))

def enqueue(db,stream_id,payload,*,run_id=None):
    """Queue only: exact metadata CAS, no artifact reads or worker construction."""
    payload=_payload(stream_id,payload)
    try:
        validate_schema(db)
        with atomic(db):
            row=db.execute('SELECT * FROM analysis_stream_retry_requests WHERE id=?',(payload['request_id'],)).fetchone()
            deduplicated=False
            if row is None:
                row=db.execute('SELECT * FROM analysis_stream_retry_requests WHERE stream_id=? AND expected_generation=?',(stream_id,payload['expected_generation'])).fetchone();deduplicated=row is not None
            if row is not None:
                row=dict(row)
                if any(row[key]!=payload[key] for key in PIN_FIELDS) or (run_id is not None and row['run_id']!=run_id):raise RetryError(409,'stream_retry_conflict')
                return dict(schema_version=1,request=project_request(row),idempotent=True,deduplicated=deduplicated)
            job=_job(db,stream_id)
            if job is None or (run_id is not None and job['run_id']!=run_id):raise RetryError(404,'stream_not_found')
            policy=eligibility(job)
            if not policy['eligible'] or policy['pins']!={key:payload[key] for key in PIN_FIELDS} or not _unpublished(job):raise RetryError(409,'stream_retry_stale_or_ineligible')
            sources=_sources(db,stream_id,job['generation']);now=str(time.time_ns())
            db.execute("INSERT INTO analysis_stream_retry_requests(id,stream_id,run_id,expected_generation,expected_state,expected_updated_utc_ns,attempt_sha256,state,created_utc_ns,updated_utc_ns) VALUES(?,?,?,?,?,?,?,'pending',?,?)",(payload['request_id'],stream_id,job['run_id'],payload['expected_generation'],payload['expected_state'],payload['expected_updated_utc_ns'],_attempt(job,sources),now,now))
            row=dict(db.execute('SELECT * FROM analysis_stream_retry_requests WHERE id=?',(payload['request_id'],)).fetchone())
            return dict(schema_version=1,request=project_request(row),idempotent=False,deduplicated=False)
    except (sqlite3.Error,ValueError,TypeError,KeyError) as error:
        if isinstance(error,RetryError):raise
        raise RetryError(503,'stream_retry_storage_unavailable') from None

def _terminal_request(db,request,state,code,job=None):
    db.execute('UPDATE analysis_stream_retry_requests SET state=?,error_code=?,updated_utc_ns=?,result_state=?,result_updated_utc_ns=? WHERE id=?',
               (state,code,str(time.time_ns()),job['state'] if job else None,str(job['updated_utc_ns']) if job else None,request['id']))

def _validate_identity(job,sources):
    from .reports import _stream_reducer
    context=json.loads(job['context_json']);run=json.loads(job['run_json']);pins=[json.loads(row['pins_json']) for row in sources]
    if len(sources)!=job['source_count'] or [pin['import_job_id'] for pin in pins]!=[row['import_job_id'] for row in sources]:raise ValueError('stream_retry_pins_invalid')
    if identity([VERSION,run,context,_portable(pins)])!=job['stream_id']:raise ValueError('stream_retry_pins_invalid')
    reducer=_stream_reducer(context['pipeline_version'])
    if context.get('stream_reducer_id')!=reducer.reducer_id+'@'+reducer.version or any(context.get(key)!=value for key,value in getattr(reducer,'context_versions',{}).items()):raise ValueError('stream_retry_pins_invalid')
    return reducer,context

def step(db,runner,*,claim=True):
    """One claim or reconciliation, owned by the existing indexer thread."""
    if not has_schema(db):return None
    validate_schema(db)
    row=db.execute("SELECT * FROM analysis_stream_retry_requests WHERE state IN ('pending','running') ORDER BY CASE state WHEN 'running' THEN 0 ELSE 1 END,created_utc_ns,id LIMIT 1").fetchone()
    if row is None:return None
    request=dict(row)
    if request['state']=='pending' and not claim:return None
    if request['state']=='pending' and runner.close() is not True:return dict(state='cleanup_pending',request_id=request['id'])
    with atomic(db):
        request=dict(db.execute('SELECT * FROM analysis_stream_retry_requests WHERE id=?',(request['id'],)).fetchone())
        job=_job(db,request['stream_id'])
        if request['state']=='running':
            if job is None or job['generation']!=request['result_generation']:
                _terminal_request(db,request,'superseded','stream_retry_attempt_changed')
            elif job['state']=='ready' and job['report_id'] is not None:_terminal_request(db,request,'completed',None,job)
            elif job['state'] in ('failed','canceled'):_terminal_request(db,request,'failed',job['error_code'] if job['state']=='failed' else 'stream_retry_canceled',job)
        elif request['state']=='pending':
            policy=eligibility(job or {})
            if job is None or not policy['eligible'] or policy['pins']!={key:request[key] for key in PIN_FIELDS} or not _unpublished(job):
                _terminal_request(db,request,'superseded','stream_retry_attempt_changed')
            else:
                try:
                    sources=_sources(db,job['stream_id'],job['generation'])
                    if _attempt(job,sources)!=request['attempt_sha256']:raise ValueError('stream_retry_pins_invalid')
                    reducer,context=_validate_identity(job,sources)
                    state=canonical(reducer.initial(context))
                    if len(state.encode())>MAX_METADATA:raise ValueError('stream_retry_pins_invalid')
                except (ValueError,TypeError,KeyError):
                    _terminal_request(db,request,'superseded','stream_retry_pins_invalid')
                else:
                    _apply(db,request,job,sources,reducer,state)
        return project_request(dict(db.execute('SELECT * FROM analysis_stream_retry_requests WHERE id=?',(request['id'],)).fetchone()))

def _apply(db,request,job,sources,reducer,state):
    """One atomic lineage transition; old generation children are never modified."""
    raw=canonical(job);columns=list(job)
    db.execute('INSERT INTO analysis_stream_history('+','.join(columns)+',request_id,job_json,job_sha256,pins_sha256) VALUES('+','.join('?' for _ in range(len(columns)+4))+')',(*job.values(),request['id'],raw,hashlib.sha256(raw.encode()).hexdigest(),_pins_digest(sources)))
    generation=job['generation']+1;now=max(time.time_ns(),job['updated_utc_ns']+1)
    changed=db.execute("UPDATE analysis_stream_jobs SET generation=?,state='pending',phase='scheduled',source_ordinal=0,verified_source_count=0,verified_bytes=0,replay_bytes=0,scanned_rows=0,selected_rows=0,selected_bytes=0,chunk_count=0,selection_chain=?,spool_bytes=0,error_code=NULL,report_id=NULL,descriptor_json=NULL,final_manifest_path=NULL,final_manifest_sha256=NULL,final_manifest_size_bytes=NULL,created_utc_ns=?,updated_utc_ns=? WHERE stream_id=? AND generation=? AND state=? AND updated_utc_ns=?",(generation,INITIAL_CHAIN,now,now,job['stream_id'],job['generation'],request['expected_state'],int(request['expected_updated_utc_ns']))).rowcount
    if changed!=1:raise ValueError('stream_retry_attempt_changed')
    for row in sources:
        db.execute('INSERT INTO analysis_stream_sources(stream_id,generation,source_ordinal,import_job_id,pins_json) VALUES(?,?,?,?,?)',(job['stream_id'],generation,row['source_ordinal'],row['import_job_id'],row['pins_json']))
    db.execute('INSERT INTO analysis_stream_checkpoints VALUES(?,?,?,-1,?,?)',(job['stream_id'],generation,reducer.reducer_id+'@'+reducer.version,state,hashlib.sha256(state.encode()).hexdigest()))
    db.execute("UPDATE analysis_stream_retry_requests SET state='running',updated_utc_ns=?,result_generation=? WHERE id=?",(str(now),generation,request['id']))


def _validate_children(db,job):
    """Bind immutable attempt counters/chain to its retained committed rows."""
    sid=job['stream_id'];generation=job['generation'];previous=INITIAL_CHAIN
    count=rows=selected_bytes=0
    for row in db.execute('SELECT * FROM analysis_stream_chunks WHERE stream_id=? AND generation=? ORDER BY ordinal',(sid,generation)):
        chunk=dict(row)
        if (chunk['ordinal']!=count or chunk['previous_chain']!=previous or not _sha(chunk['sha256'])
            or type(chunk['row_count']) is not int or chunk['row_count']<=0
            or type(chunk['size_bytes']) is not int or chunk['size_bytes']<=0
            or type(chunk['source_ordinal']) is not int or not 0<=chunk['source_ordinal']<job['source_count']
            or type(chunk['start_offset']) is not int or type(chunk['end_offset']) is not int or not 0<=chunk['start_offset']<chunk['end_offset']
            or chunk['path']!=f"analysis/streams/{sid}/g{generation}/{count:012d}.jsonl"):
            raise ValueError('stream_retry_history_chunks_invalid')
        meta={key:chunk[key] for key in ('ordinal','source_ordinal','start_offset','end_offset','row_count','size_bytes','sha256')}
        previous=hashlib.sha256(b'stream-selection-1\x00'+bytes.fromhex(previous)+canonical(meta).encode()).hexdigest()
        if previous!=chunk['selection_chain']:raise ValueError('stream_retry_history_chunks_invalid')
        reservation=db.execute('SELECT size_bytes FROM analysis_stream_reservations WHERE stream_id=? AND generation=? AND path=?',(sid,generation,chunk['path'])).fetchone()
        if reservation is None or reservation[0]<chunk['size_bytes']:raise ValueError('stream_retry_reservation_invalid')
        count+=1;rows+=chunk['row_count'];selected_bytes+=chunk['size_bytes']
    if (count!=job['chunk_count'] or rows!=job['selected_rows'] or rows!=job['scanned_rows']
        or selected_bytes!=job['selected_bytes'] or previous!=job['selection_chain']):raise ValueError('stream_retry_history_chunks_invalid')
    if db.execute('SELECT 1 FROM analysis_stream_checkpoints WHERE stream_id=? AND generation=? AND length(CAST(state_json AS BLOB))>? LIMIT 1',(sid,generation,MAX_METADATA)).fetchone():raise ValueError('stream_retry_history_checkpoint_invalid')
    checkpoints=db.execute('SELECT reducer_id,chunk_ordinal,length(CAST(state_json AS BLOB)),state_json,state_sha256 FROM analysis_stream_checkpoints WHERE stream_id=? AND generation=?',(sid,generation)).fetchall()
    if len(checkpoints)!=1:raise ValueError('stream_retry_history_checkpoint_invalid')
    checkpoint=checkpoints[0]
    if (checkpoint[0]!=json.loads(job['context_json'])['stream_reducer_id'] or checkpoint[1]!=count-1
        or checkpoint[2]>MAX_METADATA or hashlib.sha256(checkpoint[3].encode()).hexdigest()!=checkpoint[4]):raise ValueError('stream_retry_history_checkpoint_invalid')


def validate_catalog(db):
    """Read-only lineage checks, including plain SQLite backup connections."""
    previous=db.row_factory
    try:
        db.row_factory=sqlite3.Row
        _validate_catalog(db)
    finally:db.row_factory=previous


def _validate_catalog(db):
    """Read-only lineage checks in a coherent backup; no repair or source IO."""
    validate_schema(db)
    # Reject oversized forged metadata before materializing JSON or strings.
    if db.execute('SELECT 1 FROM analysis_stream_history WHERE length(CAST(job_json AS BLOB))>? OR length(CAST(context_json AS BLOB))>? OR length(CAST(run_json AS BLOB))>? OR length(CAST(descriptor_json AS BLOB))>? LIMIT 1',(3*MAX_METADATA,MAX_METADATA,MAX_METADATA,MAX_METADATA)).fetchone():raise ValueError('stream_retry_history_invalid')
    limits={'id':128,'stream_id':64,'run_id':128,'expected_state':32,'expected_updated_utc_ns':19,'attempt_sha256':64,'state':32,'error_code':128,'created_utc_ns':19,'updated_utc_ns':19,'result_state':32,'result_updated_utc_ns':19}
    for field,limit in limits.items():
        if db.execute('SELECT 1 FROM analysis_stream_retry_requests WHERE length(CAST('+field+' AS BLOB))>? LIMIT 1',(limit,)).fetchone():raise ValueError('stream_retry_request_invalid')
    for field,limit in {**JOB_TEXT_LIMITS,'request_id':128,'job_sha256':64,'pins_sha256':64}.items():
        if db.execute('SELECT 1 FROM analysis_stream_history WHERE length(CAST('+field+' AS BLOB))>? LIMIT 1',(limit,)).fetchone():raise ValueError('stream_retry_history_invalid')
    for field in JOB_INTEGERS:
        if db.execute("SELECT 1 FROM analysis_stream_history WHERE typeof("+field+")!='integer' LIMIT 1").fetchone():raise ValueError('stream_retry_history_invalid')
    for field,limit in {'path':512,'sha256':64,'previous_chain':64,'selection_chain':64}.items():
        if db.execute('SELECT 1 FROM analysis_stream_chunks c JOIN analysis_stream_history h ON h.stream_id=c.stream_id AND h.generation=c.generation WHERE length(CAST(c.'+field+' AS BLOB))>? LIMIT 1',(limit,)).fetchone():raise ValueError('stream_retry_history_chunks_invalid')
    job_columns=[row[1] for row in db.execute('PRAGMA table_info(analysis_stream_jobs)')]
    previous_sid=None;previous_generation=None
    for row in db.execute('SELECT * FROM analysis_stream_history ORDER BY stream_id,generation'):
        history=dict(row)
        if len(history['job_json'].encode())>3*MAX_METADATA:raise ValueError('stream_retry_history_invalid')
        job=json.loads(history['job_json'])
        if set(job)!=set(job_columns) or canonical(job)!=history['job_json'] or identity(job)!=history['job_sha256'] or any(job[key]!=history[key] for key in job_columns):raise ValueError('stream_retry_history_invalid')
        if not eligibility(job)['eligible'] or not _unpublished(job):raise ValueError('stream_retry_history_invalid')
        sources=_sources(db,job['stream_id'],job['generation'])
        _validate_identity(job,sources)
        if _pins_digest(sources)!=history['pins_sha256']:raise ValueError('stream_retry_history_invalid')
        _validate_children(db,job)
        requestrow=db.execute('SELECT * FROM analysis_stream_retry_requests WHERE id=?',(history['request_id'],)).fetchone()
        if requestrow is None:raise ValueError('stream_retry_lineage_invalid')
        request=dict(requestrow);project_request(request)
        if (request['stream_id']!=job['stream_id'] or request['run_id']!=job['run_id'] or request['expected_generation']!=job['generation'] or request['expected_state']!=job['state'] or request['expected_updated_utc_ns']!=str(job['updated_utc_ns']) or request['attempt_sha256']!=_attempt(job,sources) or request['result_generation']!=job['generation']+1 or request['state']=='pending'):raise ValueError('stream_retry_lineage_invalid')
        if previous_sid==job['stream_id'] and previous_generation+1!=job['generation']:raise ValueError('stream_retry_lineage_invalid')
        nextrow=db.execute('SELECT * FROM analysis_stream_history WHERE stream_id=? AND generation=?',(job['stream_id'],job['generation']+1)).fetchone()
        if nextrow is None:
            nextrow=_job(db,job['stream_id'])
            if nextrow is not None and nextrow['generation']!=job['generation']+1:nextrow=None
        if nextrow is None:raise ValueError('stream_retry_lineage_invalid')
        nextjob=dict(nextrow)
        if any(nextjob[key]!=job[key] for key in ('stream_id','run_id','context_json','run_json','source_type','source_count')) or _pins_digest(_sources(db,job['stream_id'],job['generation']+1))!=history['pins_sha256']:raise ValueError('stream_retry_lineage_invalid')
        previous_sid=job['stream_id'];previous_generation=job['generation']
    for requestrow in db.execute('SELECT * FROM analysis_stream_retry_requests ORDER BY id'):
        request=dict(requestrow);project_request(request)
        if not _sha(request['attempt_sha256']):raise ValueError('stream_retry_request_invalid')
        if request['result_generation'] is not None and db.execute('SELECT 1 FROM analysis_stream_history WHERE request_id=?',(request['id'],)).fetchone() is None:raise ValueError('stream_retry_lineage_invalid')
        if request['state'] in ('completed','failed'):
            resultjob=_job(db,request['stream_id'])
            if resultjob is not None and resultjob['generation']!=request['result_generation']:resultjob=None
            if resultjob is None:resultjob=db.execute('SELECT * FROM analysis_stream_history WHERE stream_id=? AND generation=?',(request['stream_id'],request['result_generation'])).fetchone()
            if resultjob is None or resultjob['state']!=request['result_state'] or str(resultjob['updated_utc_ns'])!=request['result_updated_utc_ns']:raise ValueError('stream_retry_result_invalid')
            if request['state']=='completed' and (resultjob['report_id'] is None or resultjob['descriptor_json'] is None or db.execute('SELECT 1 FROM analysis_reports WHERE report_id=?',(resultjob['report_id'],)).fetchone() is None):raise ValueError('stream_retry_result_invalid')
            if request['state']=='failed' and request['error_code']!=(resultjob['error_code'] if resultjob['state']=='failed' else 'stream_retry_canceled'):raise ValueError('stream_retry_result_invalid')

    for table in ('analysis_stream_sources','analysis_stream_chunks','analysis_stream_checkpoints','analysis_stream_unique','analysis_stream_reservations'):
        if db.execute('SELECT 1 FROM '+table+' c WHERE NOT EXISTS(SELECT 1 FROM analysis_stream_jobs j WHERE j.stream_id=c.stream_id AND j.generation=c.generation) AND NOT EXISTS(SELECT 1 FROM analysis_stream_history h WHERE h.stream_id=c.stream_id AND h.generation=c.generation) LIMIT 1').fetchone():raise ValueError('stream_retry_orphan_generation')
    for job in db.execute('SELECT stream_id,generation,spool_bytes FROM analysis_stream_history'):
        total=db.execute('SELECT COALESCE(SUM(size_bytes),0) FROM analysis_stream_reservations WHERE stream_id=? AND generation=?',(job['stream_id'],job['generation'])).fetchone()[0]
        if total!=job['spool_bytes']:raise ValueError('stream_retry_reservation_invalid')
        prefix=f"analysis/streams/{job['stream_id']}/g{job['generation']}/"
        for path,size in db.execute('SELECT path,size_bytes FROM analysis_stream_reservations WHERE stream_id=? AND generation=?',(job['stream_id'],job['generation'])):
            if type(path) is not str or not path.startswith(prefix) or re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',path[len(prefix):]) is None or path[len(prefix):] in ('.','..') or type(size) is not int or size<0:raise ValueError('stream_retry_reservation_invalid')
