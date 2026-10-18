"""Maintained per-revision run lookup; HTTP never parses original catalogs."""
from contextlib import contextmanager
import hashlib,json,re,sqlite3
from .report_lookup import atomic,_normalized
MAX_RUN_BYTES=1024*1024
MAX_CATALOG_BYTES=64*1024*1024
MAX_BATCH_BYTES=4*1024*1024
MAX_PAGE_BYTES=4*1024*1024
MAX_INTERVALS=256
MAX_VM_STEPS=5_000_000
MIN_NS=-(1<<63)
MAX_NS=(1<<63)-1
SOURCE_SQL={'run_catalog_revisions':'CREATE TABLE IF NOT EXISTS run_catalog_revisions (id TEXT PRIMARY KEY, document TEXT NOT NULL)',
'run_catalog_state':'CREATE TABLE IF NOT EXISTS run_catalog_state (id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL REFERENCES run_catalog_revisions(id))'}
TABLE_SQL={
'run_lookup_state':'''CREATE TABLE IF NOT EXISTS run_lookup_state (id INTEGER PRIMARY KEY CHECK(id=1),generation INTEGER NOT NULL DEFAULT 0,discovery_after TEXT NOT NULL DEFAULT '',discovery_complete INTEGER NOT NULL DEFAULT 0,error_code TEXT)''',
'run_lookup_jobs':'''CREATE TABLE IF NOT EXISTS run_lookup_jobs (revision TEXT PRIMARY KEY,source_rowid INTEGER NOT NULL,generation INTEGER NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','running','partial','ready','failed')),payload_sha256 TEXT,catalog_bytes INTEGER,run_count INTEGER,next_ordinal INTEGER NOT NULL DEFAULT 0,error_code TEXT)''',
'run_lookup_runs':'''CREATE TABLE IF NOT EXISTS run_lookup_runs (revision TEXT NOT NULL,generation INTEGER NOT NULL,ordinal INTEGER NOT NULL,run_id TEXT NOT NULL,robot_id TEXT NOT NULL,raw_json TEXT NOT NULL,run_sha256 TEXT NOT NULL,interval_count INTEGER NOT NULL,PRIMARY KEY(revision,generation,ordinal),UNIQUE(revision,generation,run_id))''',
'run_lookup_intervals':'''CREATE TABLE IF NOT EXISTS run_lookup_intervals (revision TEXT NOT NULL,generation INTEGER NOT NULL,ordinal INTEGER NOT NULL,piece INTEGER NOT NULL,expanded_start_ns INTEGER NOT NULL,expanded_end_ns INTEGER NOT NULL,PRIMARY KEY(revision,generation,ordinal,piece))'''}
INDEX_SQL={
'run_lookup_robot':'CREATE INDEX IF NOT EXISTS run_lookup_robot ON run_lookup_runs(revision,generation,robot_id,ordinal)',
'run_lookup_utc':'CREATE INDEX IF NOT EXISTS run_lookup_utc ON run_lookup_intervals(revision,generation,expanded_start_ns,expanded_end_ns,ordinal)',
'run_lookup_pending':'CREATE INDEX IF NOT EXISTS run_lookup_pending ON run_lookup_jobs(state,revision)'}
TRIGGER_SQL={}
for operation in ('insert','update','delete'):
 old='DELETE FROM run_lookup_runs WHERE revision IS OLD.id; DELETE FROM run_lookup_intervals WHERE revision IS OLD.id; DELETE FROM run_lookup_jobs WHERE revision IS OLD.id;' if operation!='insert' else ''
 new="DELETE FROM run_lookup_runs WHERE revision IS NEW.id; DELETE FROM run_lookup_intervals WHERE revision IS NEW.id; DELETE FROM run_lookup_jobs WHERE revision IS NEW.id; INSERT INTO run_lookup_jobs(revision,source_rowid,generation,state) SELECT NEW.id,NEW.rowid,generation,'pending' FROM run_lookup_state WHERE id=1;" if operation!='delete' else ''
 name='run_lookup_source_'+operation
 TRIGGER_SQL[name]=f'CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation.upper()} ON run_catalog_revisions BEGIN UPDATE run_lookup_state SET generation=generation+1 WHERE id=1; {old} {new} END'
for table in ('run_lookup_runs','run_lookup_intervals'):
 for operation in ('insert','update','delete'):
  refs=['OLD.revision'] if operation=='delete' else ['NEW.revision'] if operation=='insert' else ['OLD.revision','NEW.revision']
  name=table+'_'+operation+'_guard'
  TRIGGER_SQL[name]=f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation.upper()} ON {table} BEGIN UPDATE run_lookup_jobs SET state='failed',error_code='run_lookup_derived_modified' WHERE revision IN ({','.join(refs)}) AND state NOT IN ('running','pending'); END"
SCHEMA_SQL={**TABLE_SQL,**INDEX_SQL,**TRIGGER_SQL}

class LookupUnavailable(ValueError):
 status=503
 def __init__(self,code='run_lookup_unavailable',availability='unavailable'):
  super().__init__(code);self.code=code;self.availability=availability
class LookupMissing(ValueError):
 status=404;code='run_not_found'
 def __init__(self):super().__init__(self.code)

def has_schema(db):return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_lookup_state'").fetchone() is not None

def install_schema(db):
 """Schema only; caller owns transaction. Historical bodies are not read."""
 for sql in SCHEMA_SQL.values():db.execute(sql)
 db.execute("INSERT OR IGNORE INTO run_lookup_state(id,discovery_complete) SELECT 1,CASE WHEN EXISTS(SELECT 1 FROM run_catalog_revisions) THEN 0 ELSE 1 END")

def validate_schema(db):
 for name,expected in {**SOURCE_SQL,**SCHEMA_SQL}.items():
  row=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
  if row is None or row[0] is None or _normalized(row[0])!=_normalized(expected):raise LookupUnavailable('run_lookup_schema_invalid')
 row=db.execute('SELECT generation,typeof(discovery_after),discovery_complete,error_code FROM run_lookup_state WHERE id=1').fetchone()
 if (row is None or type(row[0]) is not int or not 0<=row[0]<=MAX_NS or row[1]!='text' or type(row[2]) is not int or row[2] not in (0,1) or row[3] not in (None,'run_lookup_discovery_failed')):raise LookupUnavailable('run_lookup_state_invalid')

@contextmanager
def _bounded(db,maximum=MAX_VM_STEPS):
 if type(maximum) is not int or maximum<1:raise LookupUnavailable('run_lookup_resource_limit')
 granularity=min(1000,maximum);steps=0;exhausted=False
 def progress():
  nonlocal steps,exhausted
  # Interrupt once; allow transaction/savepoint cleanup to finish afterwards.
  if exhausted:return 0
  steps+=granularity
  exhausted=steps>maximum
  return int(exhausted)
 db.set_progress_handler(progress,granularity)
 try:yield
 except sqlite3.Error:
  if exhausted:raise LookupUnavailable('run_lookup_resource_limit') from None
  raise
 finally:db.set_progress_handler(None,0)

@contextmanager
def _snapshot(db):
 owned=not db.in_transaction
 if owned:db.execute('BEGIN')
 try:yield
 finally:
  if owned:db.rollback()

def _id(value):return type(value) is str and re.fullmatch('[A-Za-z0-9_-]{1,128}',value) is not None

def _json(raw):
 def unique(pairs):
  output={}
  for key,value in pairs:
   if key in output:raise ValueError('duplicate_json_key')
   output[key]=value
  return output
 return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))

def _integer(value):
 if type(value) is not str or not re.fullmatch('-?(0|[1-9][0-9]{0,18})',value) or value=='-0':raise ValueError('invalid_run_timestamp')
 result=int(value)
 if not MIN_NS<=result<=MAX_NS:raise ValueError('invalid_run_timestamp')
 return result

def _prepare(raw):
 if type(raw) is not str or not 0<len(raw.encode('utf-8'))<=MAX_RUN_BYTES:raise ValueError('run_lookup_run_resource_limit')
 run=_json(raw)
 if type(run) is not dict or not _id(run.get('run_id')) or type(run.get('robot_id')) is not str or not run['robot_id'] or len(run['robot_id'])>256:raise ValueError('run_lookup_run_invalid')
 intervals=run.get('utc_intervals')
 if type(intervals) is list and len(intervals)>MAX_INTERVALS:raise ValueError('run_lookup_intervals_resource_limit')
 normalized=[]
 # Clock metadata can be unavailable while the selected original run remains
 # valid evidence. One malformed piece invalidates the entire clock mapping;
 # never search a convenient valid subset or replace the stored run payload.
 if type(intervals) is list:
  try:
   for item in intervals:
    if type(item) is not dict:raise ValueError('run_clock_unavailable')
    start,end,uncertainty=(_integer(item[key]) for key in ('utc_start_ns','utc_end_ns','uncertainty_ns'))
    if end<start or uncertainty<0:raise ValueError('run_clock_unavailable')
    normalized.append((max(MIN_NS,start-uncertainty),min(MAX_NS,end+uncertainty)))
  except (ValueError,KeyError,TypeError):normalized=[]
 return run,normalized,hashlib.sha256(raw.encode('utf-8')).hexdigest()

def _store(db,revision,generation,ordinal,raw):
 run,intervals,digest=_prepare(raw)
 db.execute('INSERT INTO run_lookup_runs VALUES (?,?,?,?,?,?,?,?)',(revision,generation,ordinal,run['run_id'],run['robot_id'],raw,digest,len(intervals)))
 db.executemany('INSERT INTO run_lookup_intervals VALUES (?,?,?,?,?,?)',[(revision,generation,ordinal,index,start,end) for index,(start,end) in enumerate(intervals)])

def index_document(db,revision,document):
 """Atomically index already-derived canonical source; original bytes stay exact."""
 if not _id(revision) or type(document) is not dict or document.get('revision')!=revision or type(document.get('runs')) is not list:raise ValueError('run_lookup_document_invalid')
 raw=json.dumps(document,sort_keys=True,separators=(',',':'),allow_nan=False)
 with atomic(db):
  validate_schema(db)
  source=db.execute('SELECT rowid,document=? FROM run_catalog_revisions WHERE id=?',(raw,revision)).fetchone()
  if source is None or source[1]!=1:raise ValueError('run_lookup_source_document_mismatch')
  generation=db.execute('SELECT generation FROM run_lookup_jobs WHERE revision=?',(revision,)).fetchone()
  if generation is None:
   db.execute('UPDATE run_lookup_state SET generation=generation+1 WHERE id=1')
   generation=(db.execute('SELECT generation FROM run_lookup_state WHERE id=1').fetchone()[0],)
   db.execute("INSERT INTO run_lookup_jobs(revision,source_rowid,generation,state) VALUES (?,?,?,'running')",(revision,source[0],generation[0]))
  else:db.execute("UPDATE run_lookup_jobs SET state='running' WHERE revision=?",(revision,))
  db.execute('DELETE FROM run_lookup_runs WHERE revision=?',(revision,));db.execute('DELETE FROM run_lookup_intervals WHERE revision=?',(revision,))
  for ordinal,run in enumerate(document['runs']):_store(db,revision,generation[0],ordinal,json.dumps(run,sort_keys=True,separators=(',',':'),allow_nan=False))
  encoded=raw.encode('utf-8')
  db.execute("UPDATE run_lookup_jobs SET source_rowid=?,state='ready',payload_sha256=?,catalog_bytes=?,run_count=?,next_ordinal=?,error_code=NULL WHERE revision=?",(source[0],hashlib.sha256(encoded).hexdigest(),len(encoded),len(document['runs']),len(document['runs']),revision))

def publish_document(db,document):
 """Publish derived source/current pointer and lookup in one writer transaction."""
 revision=document['revision'];raw=json.dumps(document,sort_keys=True,separators=(',',':'),allow_nan=False)
 with atomic(db):
  for sql in SOURCE_SQL.values():db.execute(sql)
  if not has_schema(db):install_schema(db)
  validate_schema(db)
  db.execute('INSERT OR IGNORE INTO run_catalog_revisions VALUES (?,?)',(revision,raw))
  index_document(db,revision,document)
  db.execute('INSERT INTO run_catalog_state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision',(revision,))
 return document

def coverage(db):
 validate_schema(db)
 state=db.execute('SELECT discovery_complete,error_code FROM run_lookup_state WHERE id=1').fetchone()
 counts=dict(db.execute('SELECT state,COUNT(*) FROM run_lookup_jobs GROUP BY state'))
 missing=db.execute('SELECT EXISTS(SELECT 1 FROM run_catalog_revisions c LEFT JOIN run_lookup_jobs j ON j.revision=c.id AND j.source_rowid=c.rowid WHERE j.revision IS NULL)').fetchone()[0]
 complete=bool(state[0]) and not missing and not state[1] and not any(counts.get(k,0) for k in ('pending','running','partial','failed'))
 return dict(complete=complete,availability='available' if complete else 'unavailable' if state[1] or counts.get('failed') else 'partial' if counts.get('partial') else 'pending',discovery_complete=bool(state[0]),ready=counts.get('ready',0),pending=counts.get('pending',0)+counts.get('running',0),partial=counts.get('partial',0),failed=counts.get('failed',0),error_code=state[1])

def _tick_metadata(db,limit):
 with _bounded(db):
  validate_schema(db)
  with atomic(db):
   # Current searches must not wait behind cumulative historical revisions.
   current=db.execute('SELECT c.id,c.rowid,j.source_rowid FROM run_catalog_state s JOIN run_catalog_revisions c ON c.id=s.revision LEFT JOIN run_lookup_jobs j ON j.revision=c.id WHERE s.id=1').fetchone()
   stale=current if current and current[1]!=current[2] else db.execute('SELECT c.id,c.rowid,j.source_rowid FROM run_catalog_revisions c LEFT JOIN run_lookup_jobs j ON j.revision=c.id WHERE j.revision IS NULL OR j.source_rowid<>c.rowid LIMIT 1').fetchone()
   if stale:
    revision,rowid,_=stale
    db.execute('UPDATE run_lookup_state SET generation=generation+1 WHERE id=1')
    generation=db.execute('SELECT generation FROM run_lookup_state WHERE id=1').fetchone()[0]
    db.execute('DELETE FROM run_lookup_jobs WHERE revision IS ?',(revision,))
    db.execute('DELETE FROM run_lookup_runs WHERE revision IS ?',(revision,));db.execute('DELETE FROM run_lookup_intervals WHERE revision IS ?',(revision,))
    if _id(revision):db.execute("INSERT INTO run_lookup_jobs(revision,source_rowid,generation,state) VALUES (?,?,?,'pending')",(revision,rowid,generation))
    else:db.execute("UPDATE run_lookup_state SET error_code='run_lookup_discovery_failed' WHERE id=1")
   state=db.execute('SELECT discovery_after,discovery_complete FROM run_lookup_state WHERE id=1').fetchone()
   if not state[1]:
    discovered=db.execute('SELECT id,rowid FROM run_catalog_revisions WHERE id>? ORDER BY id LIMIT ?',(state[0],limit)).fetchall()
    generation=db.execute('SELECT generation FROM run_lookup_state WHERE id=1').fetchone()[0]
    for revision,rowid in discovered:db.execute("INSERT OR IGNORE INTO run_lookup_jobs(revision,source_rowid,generation,state) VALUES (?,?,?,'pending')",(revision,rowid,generation))
    db.execute('UPDATE run_lookup_state SET discovery_after=?,discovery_complete=? WHERE id=1',(discovered[-1][0] if discovered else state[0],int(len(discovered)<limit)))
   job=db.execute("SELECT j.revision,j.generation,j.state,j.next_ordinal,j.run_count,j.source_rowid FROM run_lookup_jobs j JOIN run_catalog_revisions c ON c.id=j.revision AND c.rowid=j.source_rowid WHERE j.state IN ('pending','running','partial') ORDER BY CASE WHEN j.revision=(SELECT revision FROM run_catalog_state WHERE id=1) THEN 0 ELSE 1 END,j.revision LIMIT 1").fetchone()
   if job is None:return None
   return tuple(job)

def _process_job(db,job,limit,max_vm_steps):
 with _bounded(db,max_vm_steps):
  with atomic(db):
   revision,generation,state,after,count,source_rowid=job
   valid=db.execute("SELECT 1 FROM run_lookup_jobs j JOIN run_catalog_revisions c ON c.id=j.revision AND c.rowid=j.source_rowid WHERE j.revision=? AND j.generation=? AND j.source_rowid=? AND j.state IN ('pending','running','partial')",(revision,generation,source_rowid)).fetchone()
   if valid is None:return
   try:
    if state=='pending':
     size=db.execute('SELECT length(CAST(document AS BLOB)) FROM run_catalog_revisions WHERE id=?',(revision,)).fetchone()[0]
     if type(size) is not int or not 0<size<=MAX_CATALOG_BYTES:raise ValueError('run_lookup_catalog_resource_limit')
     if db.execute('SELECT json_valid(document) FROM run_catalog_revisions WHERE id=?',(revision,)).fetchone()[0]!=1:raise ValueError('run_lookup_document_invalid')
     header=db.execute("SELECT json_valid(document),json_type(document,'$.runs'),json_extract(document,'$.revision'),json_array_length(document,'$.runs') FROM run_catalog_revisions WHERE id=?",(revision,)).fetchone()
     if header[0]!=1 or header[1]!='array' or header[2]!=revision:raise ValueError('run_lookup_document_invalid')
     body=db.execute('SELECT CAST(document AS BLOB) FROM run_catalog_revisions WHERE id=?',(revision,)).fetchone()[0]
     _json(body)
     count=header[3]
     db.execute("UPDATE run_lookup_jobs SET state='running',catalog_bytes=?,payload_sha256=?,run_count=? WHERE revision=?",(size,hashlib.sha256(body).hexdigest(),count,revision))
    else:db.execute("UPDATE run_lookup_jobs SET state='running' WHERE revision=?",(revision,))
    # SQL serialization matches the historical selected-run cursor digest.
    batch=db.execute("SELECT CAST(r.key AS INTEGER),r.type,length(CAST(r.value AS BLOB)),CASE WHEN length(CAST(r.value AS BLOB))<=? THEN r.value END FROM run_catalog_revisions c,json_each(c.document,'$.runs') r WHERE c.id=? AND CAST(r.key AS INTEGER)>=? ORDER BY CAST(r.key AS INTEGER) LIMIT ?",(MAX_RUN_BYTES,revision,after,limit))
    consumed=0;processed=0
    for ordinal,kind,size,raw in batch:
     if kind!='object' or raw is None:raise ValueError('run_lookup_run_resource_limit')
     if processed and consumed+size>MAX_BATCH_BYTES:break
     _store(db,revision,generation,ordinal,raw)
     after=ordinal+1;consumed+=size;processed+=1
    if after>count or not processed and after<count:raise ValueError('run_lookup_progress_invalid')
    db.execute("UPDATE run_lookup_jobs SET state=?,next_ordinal=?,error_code=NULL WHERE revision=?",('ready' if after==count else 'partial',after,revision))
   except (ValueError,TypeError,KeyError,RecursionError,sqlite3.IntegrityError):db.execute("UPDATE run_lookup_jobs SET state='failed',error_code='run_lookup_document_invalid' WHERE revision=?",(revision,))

def _fail_resource(db,job):
 """Terminally fail only the captured source generation after work rollback."""
 revision,generation,_,_,_,source_rowid=job
 with _bounded(db):
  with atomic(db):
   validate_schema(db)
   db.execute("UPDATE run_lookup_jobs SET state='failed',error_code='run_lookup_resource_limit' WHERE revision=? AND generation=? AND source_rowid=? AND state IN ('pending','running','partial') AND EXISTS(SELECT 1 FROM run_catalog_revisions c WHERE c.id=run_lookup_jobs.revision AND c.rowid=run_lookup_jobs.source_rowid)",(revision,generation,source_rowid))
  return coverage(db)


def tick(db,limit=50,*,max_vm_steps=MAX_VM_STEPS):
 """Isolate a selected legacy job's work budget without stopping other work."""
 # SQLite interruption can abort an enclosing transaction despite savepoints.
 # This worker must own its transaction; never risk a caller's unrelated work.
 if db.in_transaction:raise ValueError('run_lookup_worker_requires_owned_transaction')
 if type(limit) is not int or not 1<=limit<=100:raise ValueError('invalid_run_lookup_limit')
 if type(max_vm_steps) is not int or max_vm_steps<1:raise ValueError('invalid_run_lookup_work_budget')
 # Metadata/discovery has its own budget and cannot nominate a failed job.
 job=_tick_metadata(db,limit)
 if job is not None:
  try:_process_job(db,job,limit,max_vm_steps)
  except LookupUnavailable as error:
   if error.code!='run_lookup_resource_limit':raise
   return _fail_resource(db,job)
 with _bounded(db):return coverage(db)


def _ready(db,revision=None):
 validate_schema(db)
 if revision is None:
  current=db.execute('SELECT revision FROM run_catalog_state WHERE id=1').fetchone()
  if current is None:
   if db.execute('SELECT EXISTS(SELECT 1 FROM run_catalog_revisions)').fetchone()[0]:raise LookupUnavailable('run_lookup_current_missing')
   state=coverage(db)
   if state['complete']:return None,None
   raise LookupUnavailable('run_lookup_pending',state['availability'])
  revision=current[0]
 if not _id(revision):raise LookupUnavailable('run_lookup_revision_invalid')
 row=db.execute('SELECT j.generation,j.state,j.run_count,j.next_ordinal,j.payload_sha256 FROM run_catalog_revisions c LEFT JOIN run_lookup_jobs j ON j.revision=c.id AND j.source_rowid=c.rowid WHERE c.id=?',(revision,)).fetchone()
 if row is None:raise LookupMissing()
 if row[0] is None:raise LookupUnavailable('run_lookup_pending','pending')
 generation,state,count,after,digest=row
 if state!='ready':
  availability='partial' if state=='partial' else 'pending' if state in ('pending','running') else 'unavailable'
  raise LookupUnavailable('run_lookup_'+availability,availability)
 if type(generation) is not int or generation<0 or type(count) is not int or count<0 or after!=count or not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):raise LookupUnavailable('run_lookup_generation_invalid')
 # Cheap indexed generation witnesses reject rewritten progress/generation
 # metadata without recounting or decoding a season-sized catalog.
 first=db.execute('SELECT ordinal FROM run_lookup_runs WHERE revision=? AND generation=? ORDER BY ordinal LIMIT 1',(revision,generation)).fetchone()
 last=db.execute('SELECT ordinal FROM run_lookup_runs WHERE revision=? AND generation=? ORDER BY ordinal DESC LIMIT 1',(revision,generation)).fetchone()
 if (count==0 and (first is not None or last is not None)) or (count>0 and (first is None or last is None or first[0]!=0 or last[0]!=count-1)):
  raise LookupUnavailable('run_lookup_generation_invalid')
 return revision,generation

def _selected(db,identity,revision):
 if not _id(identity):raise ValueError('invalid_run_id')
 revision,generation=_ready(db,revision)
 if revision is None:raise LookupMissing()
 row=db.execute('SELECT CASE WHEN length(CAST(raw_json AS BLOB))<=? THEN raw_json END,run_sha256 FROM run_lookup_runs WHERE revision=? AND generation=? AND run_id=?',(MAX_RUN_BYTES,revision,generation,identity)).fetchone()
 if row is None:raise LookupMissing()
 if row[0] is None or hashlib.sha256(row[0].encode('utf-8')).hexdigest()!=row[1]:raise LookupUnavailable('run_lookup_payload_invalid')
 run=_json(row[0])
 if type(run) is not dict or run.get('run_id')!=identity:raise LookupUnavailable('run_lookup_payload_invalid')
 return run,row[1],revision

def select_run(db,identity,revision=None,*,budget=True):
 if budget:
  with _bounded(db),_snapshot(db):return _selected(db,identity,revision)
 return _selected(db,identity,revision)

def search(db,*,utc_from_ns=None,utc_to_ns=None,robot=None,include_unknown=False,offset=0,limit=50,max_vm_steps=MAX_VM_STEPS):
 if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or not 0<=offset<=MAX_NS:raise ValueError('Invalid pagination')
 if (utc_from_ns is None)!=(utc_to_ns is None):raise ValueError('Both interval endpoints are required')
 if utc_from_ns is not None and (type(utc_from_ns) is not int or type(utc_to_ns) is not int or not MIN_NS<=utc_from_ns<=utc_to_ns<=MAX_NS):raise ValueError('Invalid UTC interval')
 if robot is not None and (type(robot) is not str or not robot or len(robot)>256):raise ValueError('Invalid robot filter')
 if type(include_unknown) is not bool:raise ValueError('Invalid unknown-clock policy')
 revision=None
 try:
  with _bounded(db,max_vm_steps),_snapshot(db):
   revision,generation=_ready(db)
   if revision is None:return dict(revision=None,items=[],total=0,next_offset=None,availability='available')
   clauses=['r.revision=?','r.generation=?'];parameters=[revision,generation]
   if robot is not None:clauses.append('r.robot_id=?');parameters.append(robot)
   if utc_from_ns is not None:
    intervals='r.ordinal IN (SELECT ordinal FROM run_lookup_intervals WHERE revision=? AND generation=? AND expanded_start_ns<=? AND expanded_end_ns>=?)'
    clauses.append('('+intervals+(' OR r.interval_count=0' if include_unknown else '')+')');parameters.extend((revision,generation,utc_to_ns,utc_from_ns))
   where=' AND '.join(clauses)
   total=db.execute('SELECT COUNT(*) FROM run_lookup_runs r WHERE '+where,parameters).fetchone()[0]
   sizes=db.execute('SELECT COALESCE(SUM(length(CAST(raw_json AS BLOB))),0),COALESCE(MAX(length(CAST(raw_json AS BLOB))),0) FROM (SELECT r.raw_json FROM run_lookup_runs r WHERE '+where+' ORDER BY r.ordinal LIMIT ? OFFSET ?)',parameters+[limit,offset]).fetchone()
   if sizes[0]>MAX_PAGE_BYTES or sizes[1]>MAX_RUN_BYTES:raise LookupUnavailable('run_lookup_page_resource_limit')
   selected=db.execute('SELECT CASE WHEN length(CAST(r.raw_json AS BLOB))<=? THEN r.raw_json END,r.run_sha256 FROM run_lookup_runs r WHERE '+where+' ORDER BY r.ordinal LIMIT ? OFFSET ?', [MAX_RUN_BYTES]+parameters+[limit,offset])
   items=[];used=0
   for raw,digest in selected:
    if raw is None:raise LookupUnavailable('run_lookup_run_resource_limit')
    used+=len(raw.encode('utf-8'))
    if used>MAX_PAGE_BYTES:raise LookupUnavailable('run_lookup_page_resource_limit')
    if hashlib.sha256(raw.encode('utf-8')).hexdigest()!=digest:raise LookupUnavailable('run_lookup_payload_invalid')
    item=_json(raw)
    if type(item) is not dict:raise LookupUnavailable('run_lookup_payload_invalid')
    items.append(item)
   return dict(revision=revision,items=items,total=total,next_offset=offset+limit if offset+limit<total else None,availability='available')
 except LookupUnavailable as error:return dict(revision=revision,items=[],total=None,next_offset=None,availability=error.availability,reason=error.code)
 except (sqlite3.Error,ValueError,TypeError,KeyError,RecursionError):return dict(revision=revision,items=[],total=None,next_offset=None,availability='unavailable',reason='run_lookup_unavailable')
