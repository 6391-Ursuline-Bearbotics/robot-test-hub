"""Bounded read-only note candidates pinned to an immutable run/time snapshot.

Annotation storage is append-only: ROWID fences retain the latest saved revision
as of the first page. Schema/VACUUM changes invalidate cursors; unsupported manual
row rewrites are not an alternate annotation editing API.
"""
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

MAX_CATALOG_BYTES=64*1024*1024
MAX_RUN_BYTES=1024*1024
MAX_NOTE_BYTES=16384
MAX_VM_STEPS=5_000_000
MAX_SELECTION_VM_STEPS=5_000_000
MAX_INTERVALS=256
MAX_RESPONSE_BYTES=2*1024*1024
BASIS='intended_utc_overlap_any_mapped_interval'


class RunNotesError(ValueError):
    def __init__(self,status,code):
        super().__init__(code);self.status=status;self.code=code


def _invalid():return RunNotesError(400,'invalid_run_notes_query')
def _unavailable():return RunNotesError(503,'run_notes_unavailable')
def _changed():return RunNotesError(409,'run_notes_snapshot_changed')


def _id(value):
    return type(value) is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',value) is not None


def _integer(value,minimum=0,maximum=(1<<63)-1):
    return type(value) is int and minimum<=value<=maximum


def _json(raw):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError()
            value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


def _encode(state):
    return base64.urlsafe_b64encode(json.dumps(state,sort_keys=True,separators=(',',':')).encode()).decode()


def _decode(token,identity,limit):
    try:
        if type(token) is not str or not 0<len(token)<=4096:raise ValueError()
        state=_json(base64.b64decode(token,altchars=b'-_',validate=True))
        if (type(state) is not dict or set(state)!={'v','run_id','limit','revision','watermark','schema','run_sha256','witness','after','total'}
            or type(state['v']) is not int or state['v']!=1 or state['run_id']!=identity or not _integer(state['limit'],1,100) or state['limit']!=limit
            or not _id(state['revision']) or not _integer(state['watermark']) or not _integer(state['schema'])
            or type(state['run_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',state['run_sha256'])
            or not _integer(state['total']) or type(state['after']) is not list or len(state['after'])!=3
            or not _id(state['after'][0]) or not _integer(state['after'][1],1,1000000) or not _integer(state['after'][2],1)
            or (state['watermark']==0 and state['witness'] is not None)
            or (state['watermark']>0 and (type(state['witness']) is not list or len(state['witness'])!=3
                or not _id(state['witness'][0]) or not _integer(state['witness'][1],1,1000000)
                or not _integer(state['witness'][2],-(1<<63))))):raise ValueError()
        return state
    except (ValueError,TypeError,KeyError,OverflowError,RecursionError):raise _invalid() from None


def _intervals(run):
    raw=run.get('utc_intervals')
    if type(raw) is list and len(raw)>MAX_INTERVALS:raise _unavailable()
    if type(raw) is not list or not raw:return None
    intervals=[]
    for item in raw:
        if type(item) is not dict:return None
        values=[]
        for key in ('utc_start_ns','utc_end_ns','uncertainty_ns'):
            value=item.get(key)
            if type(value) is not str or not re.fullmatch('-?(0|[1-9][0-9]{0,18})',value) or value=='-0':return None
            value=int(value)
            if not -(1<<63)<=value<(1<<63):return None
            values.append(value)
        if values[1]<values[0] or values[2]<0:return None
        intervals.append(values[:2])
    return intervals


def _run(db,identity,revision,pinned):
    from .run_lookup import select_run,LookupMissing,LookupUnavailable
    try:
        run,digest,_=select_run(db,identity,revision,budget=False)
        return run,digest
    except LookupMissing:
        raise _changed() if pinned else RunNotesError(404,'run_not_found') from None
    except LookupUnavailable:
        raise _unavailable() from None



CANDIDATES="""WITH latest AS MATERIALIZED (
 SELECT a.rowid AS saved_rowid,a.* FROM annotation_revisions a
 WHERE a.rowid<=:watermark AND NOT EXISTS (
   SELECT 1 FROM annotation_revisions newer WHERE newer.event_id=a.event_id
   AND newer.revision>a.revision AND newer.rowid<=:watermark)),
 candidates AS MATERIALIZED (
 SELECT * FROM latest a WHERE a.event_utc_start_ns IS NOT NULL
 AND EXISTS(SELECT 1 FROM json_each(:intervals) i
   WHERE a.event_utc_start_ns<=json_extract(i.value,'$[1]')
   AND a.event_utc_end_ns>=json_extract(i.value,'$[0]'))),
 selected AS (SELECT * FROM candidates WHERE (:after IS NULL OR event_id>:after)
 ORDER BY event_id LIMIT :fetch)
 SELECT (SELECT COUNT(*) FROM candidates),
 EXISTS(SELECT 1 FROM latest WHERE typeof(event_id)<>'text' OR length(CAST(event_id AS BLOB)) NOT BETWEEN 1 AND 100
   OR typeof(revision)<>'integer' OR revision NOT BETWEEN 1 AND 1000000
   OR typeof(hub_received_utc_ns)<>'integer'
   OR (event_utc_start_ns IS NULL)<>(event_utc_end_ns IS NULL)
   OR (event_utc_start_ns IS NOT NULL AND (typeof(event_utc_start_ns)<>'integer' OR typeof(event_utc_end_ns)<>'integer' OR event_utc_end_ns<event_utc_start_ns))
   OR (submitted_utc_ns IS NOT NULL AND typeof(submitted_utc_ns)<>'integer')),
 (:after IS NULL OR EXISTS(SELECT 1 FROM candidates WHERE event_id=:after AND revision=:after_revision AND saved_rowid=:after_rowid)),
 s.saved_rowid,CASE WHEN length(CAST(s.event_id AS BLOB))<=100 THEN s.event_id END,s.revision,
 CASE WHEN length(CAST(s.payload_json AS BLOB))<=:max_note THEN s.payload_json END,
 s.submitted_utc_ns,s.event_utc_start_ns,s.event_utc_end_ns,s.hub_received_utc_ns
 FROM (SELECT 1) seed LEFT JOIN selected s ON 1 ORDER BY s.event_id"""


def _page(db,identity,query,*,revision=None,with_run=False):
    """Use a caller-owned read-only connection; never install or repair storage."""
    if not _id(identity) or type(query) is not dict or set(query)-{'limit','cursor'}:raise _invalid()
    value=query.get('limit','20')
    if type(value) is not str or not re.fullmatch('[1-9][0-9]{0,2}',value) or not 1<=int(value)<=100:raise _invalid()
    limit=int(value);state=_decode(query['cursor'],identity,limit) if 'cursor' in query else None
    pinned=state is not None
    schema=db.execute('PRAGMA schema_version').fetchone()[0]
    if pinned:
        if state['schema']!=schema:raise _changed()
        revision=state['revision']
    elif revision is None:
        current=db.execute('SELECT revision FROM run_catalog_state WHERE id=1').fetchone()
        if current is None:raise RunNotesError(404,'run_not_found')
        revision=current[0]
    if not _id(revision):raise _unavailable()
    run,run_digest=_run(db,identity,revision,pinned)
    intervals=_intervals(run)
    if state is None:
        watermark=db.execute('SELECT COALESCE(MAX(rowid),0) FROM annotation_revisions').fetchone()[0]
        state=dict(v=1,run_id=identity,limit=limit,revision=revision,watermark=watermark,schema=schema,run_sha256=run_digest,witness=None,after=None,total=0)
    if state['run_sha256']!=run_digest:raise _changed()
    witness=None
    if state['watermark']:
        row=db.execute('SELECT CASE WHEN length(CAST(event_id AS BLOB))<=100 THEN event_id END,revision,hub_received_utc_ns FROM annotation_revisions WHERE rowid=?',(state['watermark'],)).fetchone()
        if row is None:raise _changed()
        witness=list(row)
        if not _id(witness[0]) or not _integer(witness[1],1,1000000) or not _integer(witness[2],-(1<<63)):raise _unavailable()
    if pinned and state['witness']!=witness:raise _changed()
    state['witness']=witness
    envelope=dict(schema_version=1,run_id=identity,run_catalog_revision=revision,annotation_watermark=str(state['watermark']),
        annotations=[],total=None,returned=0,limit=limit,next_cursor=None,candidate_basis=BASIS,clock_uncertainty_applied=False,
        availability='unavailable' if intervals is None else 'available',reason='run_clock_unavailable' if intervals is None else None)
    if intervals is None:
        if pinned:raise _changed()
        return (run,envelope) if with_run else envelope
    after=state['after']
    params=dict(watermark=state['watermark'],intervals=json.dumps(intervals,separators=(',',':')),after=after[0] if after else None,
        after_revision=after[1] if after else None,after_rowid=after[2] if after else None,fetch=limit+1,max_note=MAX_NOTE_BYTES)
    rows=db.execute(CANDIDATES,params).fetchall()
    total,bad,boundary=rows[0][:3]
    if bad:raise _unavailable()
    if not boundary:raise _invalid()
    if pinned and total!=state['total']:raise _changed()
    state['total']=total
    records=[row for row in rows if row[3] is not None]
    from .notebook import validate
    for row in records[:limit]:
        if row[6] is None:raise _unavailable()
        payload=_json(row[6]);normalized,times=validate(payload)
        if (payload!=normalized or payload['event_id']!=row[4] or payload['revision']!=row[5]
            or (times['submitted_utc_ns'],times['event_utc_start_ns'],times['event_utc_end_ns'])!=tuple(row[7:10])):raise _unavailable()
        envelope['annotations'].append(dict(payload,hub_received_utc_ns=str(row[10]),storage_state='saved_in_hub',delivery_state='historical_hub_only'))
    if len(records)>limit:
        last=records[limit-1];state['after']=[last[4],last[5],last[3]]
        envelope['next_cursor']=_encode(state)
    envelope.update(total=total,returned=len(envelope['annotations']))
    if len(json.dumps(envelope,allow_nan=False,separators=(',',':')).encode())>MAX_RESPONSE_BYTES:raise _unavailable()
    return (run,envelope) if with_run else envelope


def _bounded_query(db,maximum,operation):
    if maximum<1:raise _unavailable()
    steps=0
    def progress():
        nonlocal steps
        steps+=1000
        return int(steps>maximum)
    db.set_progress_handler(progress,1000)
    try:return operation()
    except RunNotesError:raise
    except (sqlite3.Error,ValueError,TypeError,KeyError,OverflowError,RecursionError):raise _unavailable() from None
    finally:db.set_progress_handler(lambda:0,1000)


def select_run(db,identity):
    """Independently validate run evidence before optional note lookup.

    Callers keep the same read transaction through candidate lookup. A notes
    failure can therefore never excuse missing or corrupt selected-run data.
    """
    if not _id(identity):raise _invalid()
    def selected():
        row=db.execute('SELECT revision FROM run_catalog_state WHERE id=1').fetchone()
        if row is None:raise RunNotesError(404,'run_not_found')
        revision=row[0]
        if not _id(revision):raise _unavailable()
        run,_=_run(db,identity,revision,False)
        return run,revision
    return _bounded_query(db,MAX_SELECTION_VM_STEPS,selected)


def page(db,identity,query,*,revision=None,with_run=False):
    return _bounded_query(db,MAX_VM_STEPS,lambda:_page(db,identity,query,revision=revision,with_run=with_run))


def get(root,path,query):
    match=re.fullmatch('/api/v1/runs/([A-Za-z0-9_-]{1,128})/annotations',path)
    if match is None:raise _invalid()
    try:
        with closing(sqlite3.connect((Path(root).resolve()/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            try:return page(db,match[1],query)
            finally:db.rollback()
    except RunNotesError:raise
    except (sqlite3.Error,ValueError,TypeError,KeyError,OverflowError,RecursionError):raise _unavailable() from None
