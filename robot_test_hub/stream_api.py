"""Read-only, bounded live progress for source-verified full-log analysis."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3

MAX_VM_STEPS=5_000_000
MAX_NS=(1<<63)-1
COLUMNS=('stream_id','run_id','generation','state','phase','source_type','source_count',
    'source_ordinal','verified_source_count','verified_bytes','replay_bytes','scanned_rows',
    'selected_rows','selected_bytes','chunk_count','spool_bytes','error_code','report_id',
    'created_utc_ns','updated_utc_ns')
COUNTERS=('generation','source_count','source_ordinal','verified_source_count','verified_bytes',
    'replay_bytes','scanned_rows','selected_rows','selected_bytes','chunk_count','spool_bytes')
STATES=frozenset(('pending','running','verifying','selecting','finalizing','ready','failed','canceled'))
SOURCES=frozenset(('synthetic','real','simulation','historical','unknown','unqualified'))
ATTEMPT_LIMIT=3
REQUEST_FIELDS=('id','stream_id','run_id','expected_generation','expected_state','expected_updated_utc_ns',
    'state','error_code','created_utc_ns','updated_utc_ns','result_generation','result_state','result_updated_utc_ns')
PUBLIC_COLUMNS=','.join((f"CASE WHEN {key} IS NULL OR length(CAST({key} AS BLOB))<=128 THEN {key} ELSE X'00' END AS {key}"
    if key in ('stream_id','run_id','state','phase','source_type','error_code','report_id') else key) for key in COLUMNS)


def _unavailable(identity,limit,offset):
    return dict(schema_version=1,run_id=identity,availability='unavailable',
        reason='stream_progress_unavailable',items=[],total=None,returned=0,
        limit=limit,offset=offset,next_offset=None,basis='live_worker_progress')


def _number(value,minimum,maximum):
    if type(value) is not str or not re.fullmatch('0|[1-9][0-9]{0,18}',value):
        raise ValueError('invalid_stream_progress_query')
    value=int(value)
    if not minimum<=value<=maximum:raise ValueError('invalid_stream_progress_query')
    return value


def _public(row,identity):
    item=dict(zip(COLUMNS,row))
    if (item['run_id']!=identity or type(item['stream_id']) is not str
        or not re.fullmatch('[a-f0-9]{64}',item['stream_id'])
        or item['state'] not in STATES or item['source_type'] not in SOURCES
        or type(item['phase']) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}',item['phase'])):
        raise ValueError('invalid_stream_progress')
    for key in COUNTERS:
        value=item[key]
        if type(value) is not int or not 0<=value<=MAX_NS:raise ValueError('invalid_stream_progress')
        item[key]=str(value)
    for key in ('created_utc_ns','updated_utc_ns'):
        value=item[key]
        if type(value) is not int or not 0<=value<=MAX_NS:raise ValueError('invalid_stream_progress')
        item[key]=str(value)
    if int(item['source_count'])>128 or any(int(item[key])>int(item['source_count']) for key in ('source_ordinal','verified_source_count')):
        raise ValueError('invalid_stream_progress')
    if item['state']=='ready' and (item['error_code'] is not None or item['phase']!='complete' or int(item['source_count'])==0 or item['verified_source_count']!=item['source_count']):
        raise ValueError('invalid_stream_progress')
    report=item['report_id']
    if report is not None and item['state']!='ready':raise ValueError('invalid_stream_progress')
    if report is not None and (type(report) is not str or not re.fullmatch('[a-f0-9]{64}',report)):
        raise ValueError('invalid_stream_progress')
    # Detailed source paths/context and arbitrary worker exceptions never enter
    # this projection. Errors receive a fixed public explanation.
    from .stream_analysis import ERROR_CODES
    code=item.pop('error_code')
    item['reason']=code if code in ERROR_CODES else 'stream_processing_failed' if code is not None else None
    item['report_available']=report is not None
    item['source_verification_complete']=item['state']=='ready' and int(item['source_count'])>0 and item['verified_source_count']==item['source_count']
    return item


def _retry_projection(db,raw,public,available):
    """Bounded metadata only: never decode saved source/context documents."""
    public.update(retry_availability='unavailable',retry=dict(eligible=False,reason='stream_retry_unavailable',pins=None),
                  previous_attempts=[],previous_attempt_total=None,retry_requests=[],retry_request_total=None,
                  attempt_display_limit=ATTEMPT_LIMIT)
    if not available:return
    from .stream_retry import eligibility,project_request
    policy=eligibility(raw)
    if policy.get('pins') is not None:
        policy={**policy,'pins':{**policy['pins'],'expected_generation':str(policy['pins']['expected_generation'])}}
    sid=raw['stream_id']
    total=db.execute('SELECT COUNT(*) FROM analysis_stream_history WHERE stream_id=?',(sid,)).fetchone()[0]
    old=db.execute('SELECT '+PUBLIC_COLUMNS+' FROM analysis_stream_history WHERE stream_id=? ORDER BY generation DESC LIMIT ?',
                   (sid,ATTEMPT_LIMIT)).fetchall()
    history=[_public(row,raw['run_id']) for row in old]
    if any(int(item['generation'])>=raw['generation'] or item['state'] not in ('failed','canceled') for item in history):
        raise ValueError('invalid_stream_retry_history')
    request_total=db.execute('SELECT COUNT(*) FROM analysis_stream_retry_requests WHERE stream_id=?',(sid,)).fetchone()[0]
    select=','.join((key if key in ('expected_generation','result_generation') else
        f"CASE WHEN {key} IS NULL OR length(CAST({key} AS BLOB))<=128 THEN {key} ELSE X'00' END AS {key}") for key in REQUEST_FIELDS)
    cursor=db.execute('SELECT '+select+' FROM analysis_stream_retry_requests WHERE stream_id=? ORDER BY created_utc_ns DESC,id DESC LIMIT ?',
                      (sid,ATTEMPT_LIMIT))
    requests=[]
    for row in cursor.fetchall():
        receipt=project_request(dict(zip(REQUEST_FIELDS,row)))
        if receipt['stream_id']!=sid or receipt['run_id']!=raw['run_id']:raise ValueError('invalid_stream_retry_receipt')
        for key in ('expected_generation','result_generation'):
            if receipt[key] is not None:receipt[key]=str(receipt[key])
        requests.append(receipt)
    public.update(retry_availability='available',retry=policy,previous_attempts=history,previous_attempt_total=total,
                  retry_requests=requests,retry_request_total=request_total)


def page(db,identity,query):
    if type(identity) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',identity):
        raise ValueError('invalid_stream_progress_query')
    if set(query)-{'limit','offset'}:raise ValueError('invalid_stream_progress_query')
    limit=_number(query.get('limit','5'),1,20)
    offset=_number(query.get('offset','0'),0,MAX_NS)
    steps=0;granularity=min(1000,max(1,MAX_VM_STEPS))
    def guard():
        nonlocal steps
        steps+=granularity
        return int(steps>MAX_VM_STEPS)
    db.set_progress_handler(guard,granularity)
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_stream_jobs'").fetchone() is None:
            return _unavailable(identity,limit,offset)
        from .stream_analysis import validate_schema
        validate_schema(db)
        total=db.execute('SELECT COUNT(*) FROM analysis_stream_jobs WHERE run_id=?',(identity,)).fetchone()[0]
        if not total:
            # Keep unknown identities distinct from known runs awaiting work.
            from .run_lookup import select_run
            select_run(db,identity,budget=False)
        rows=db.execute('SELECT '+PUBLIC_COLUMNS+' FROM analysis_stream_jobs WHERE run_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?',
            (identity,limit,offset)).fetchall()
        retry_available=False
        if db.execute('PRAGMA user_version').fetchone()[0]>=12:
            from .stream_retry import validate_schema as validate_retry
            validate_retry(db)
            retry_available=True
        items=[]
        for row in rows:
            item=_public(row,identity)
            _retry_projection(db,dict(zip(COLUMNS,row)),item,retry_available)
            items.append(item)
        return dict(schema_version=1,run_id=identity,availability='available',reason=None,
            items=items,total=total,returned=len(items),limit=limit,offset=offset,
            next_offset=offset+limit if offset+limit<total else None,basis='live_worker_progress')
    except (sqlite3.Error,ImportError,TypeError,KeyError,RecursionError):
        return _unavailable(identity,limit,offset)
    except ValueError as error:
        from .run_lookup import LookupMissing,LookupUnavailable
        if isinstance(error,LookupMissing):raise
        if isinstance(error,LookupUnavailable):return _unavailable(identity,limit,offset)
        return _unavailable(identity,limit,offset)
    finally:db.set_progress_handler(None,0)


def get(root,path,query):
    match=re.fullmatch('/api/v1/runs/([A-Za-z0-9_-]{1,128})/stream-progress',path)
    if match is None:raise ValueError('invalid_stream_progress_query')
    try:
        with closing(sqlite3.connect((Path(root).resolve()/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            return page(db,match[1],query)
    except sqlite3.Error:
        # Parse pagination even on a missing catalog; invalid queries stay invalid.
        if set(query)-{'limit','offset'}:raise ValueError('invalid_stream_progress_query')
        return _unavailable(match[1],_number(query.get('limit','5'),1,20),_number(query.get('offset','0'),0,MAX_NS))
