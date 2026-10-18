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
        rows=db.execute('SELECT '+','.join(COLUMNS)+' FROM analysis_stream_jobs WHERE run_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?',
            (identity,limit,offset)).fetchall()
        items=[_public(row,identity) for row in rows]
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
