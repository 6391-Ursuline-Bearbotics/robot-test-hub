"""Bounded full-byte, source-pinned analysis streams, independent of tuple Evidence."""
from __future__ import annotations
from dataclasses import dataclass
from contextlib import contextmanager
import copy,hashlib,json,os,re,shutil,stat,time,sqlite3
from pathlib import Path
from .analysis import canonical,identity
from .report_lookup import atomic,_normalized
from .wpilog import PROFILE,EXTRACTOR_VERSION,MAPPING_REVISION
VERSION='stream-analysis-1'
INPUT_FORMAT='stream-evidence-1'
SELECTION='full_source_scoped_rows_1'
STATES=('pending','verifying','running','canceled','failed','ready')
PHASES=('scheduled','raw_verify','manifest_verify','dataset_verify','manifest_publish','complete','canceled','failed')
ERROR_CODES=tuple('stream_'+s for s in ('source_pins_invalid','source_changed','source_hash_mismatch','manifest_invalid','dataset_invalid','line_resource_limit','spool_resource_limit','free_space_unavailable','checkpoint_invalid','chunk_conflict','inventory_invalid','schema_invalid','reducer_failed','sql_resource_limit'))
INITIAL_CHAIN=hashlib.sha256(b'stream-selection-1\x00').hexdigest()
MAX_METADATA=1024*1024
MAX_MANIFEST=4*1024*1024
SQL_STEP_LIMIT=5_000_000
SQL={
'analysis_stream_jobs':'''CREATE TABLE IF NOT EXISTS analysis_stream_jobs (
 stream_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,generation INTEGER NOT NULL,state TEXT NOT NULL,phase TEXT NOT NULL,
 context_json TEXT NOT NULL,run_json TEXT NOT NULL,source_type TEXT NOT NULL,source_count INTEGER NOT NULL,
 source_ordinal INTEGER NOT NULL DEFAULT 0,verified_source_count INTEGER NOT NULL DEFAULT 0,
 verified_bytes INTEGER NOT NULL DEFAULT 0,replay_bytes INTEGER NOT NULL DEFAULT 0,
 scanned_rows INTEGER NOT NULL DEFAULT 0,selected_rows INTEGER NOT NULL DEFAULT 0,selected_bytes INTEGER NOT NULL DEFAULT 0,
 chunk_count INTEGER NOT NULL DEFAULT 0,selection_chain TEXT NOT NULL,spool_bytes INTEGER NOT NULL DEFAULT 0,
 error_code TEXT,descriptor_json TEXT,report_id TEXT,final_manifest_path TEXT,final_manifest_sha256 TEXT,final_manifest_size_bytes INTEGER,
 created_utc_ns INTEGER NOT NULL,updated_utc_ns INTEGER NOT NULL)''',
'analysis_stream_sources':'''CREATE TABLE IF NOT EXISTS analysis_stream_sources (
 stream_id TEXT NOT NULL,generation INTEGER NOT NULL,source_ordinal INTEGER NOT NULL,import_job_id TEXT NOT NULL,pins_json TEXT NOT NULL,
 traversal_ordinal INTEGER,phase TEXT NOT NULL DEFAULT 'raw_verify',raw_verified INTEGER NOT NULL DEFAULT 0,
 manifest_verified INTEGER NOT NULL DEFAULT 0,dataset_verified INTEGER NOT NULL DEFAULT 0,
 raw_bytes INTEGER NOT NULL DEFAULT 0,manifest_bytes INTEGER NOT NULL DEFAULT 0,dataset_bytes INTEGER NOT NULL DEFAULT 0,
 scan_offset INTEGER NOT NULL DEFAULT 0,line_ordinal INTEGER NOT NULL DEFAULT 0,manifest_json TEXT,
 PRIMARY KEY(stream_id,generation,source_ordinal))''',
'analysis_stream_chunks':'''CREATE TABLE IF NOT EXISTS analysis_stream_chunks (
 stream_id TEXT NOT NULL,generation INTEGER NOT NULL,ordinal INTEGER NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,size_bytes INTEGER NOT NULL,
 row_count INTEGER NOT NULL,source_ordinal INTEGER NOT NULL,start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,
 previous_chain TEXT NOT NULL,selection_chain TEXT NOT NULL,PRIMARY KEY(stream_id,generation,ordinal))''',
'analysis_stream_checkpoints':'''CREATE TABLE IF NOT EXISTS analysis_stream_checkpoints (
 stream_id TEXT NOT NULL,generation INTEGER NOT NULL,reducer_id TEXT NOT NULL,chunk_ordinal INTEGER NOT NULL,
 state_json TEXT NOT NULL,state_sha256 TEXT NOT NULL,PRIMARY KEY(stream_id,generation,reducer_id))''',
'analysis_stream_unique':'''CREATE TABLE IF NOT EXISTS analysis_stream_unique (
 stream_id TEXT NOT NULL,generation INTEGER NOT NULL,namespace TEXT NOT NULL,key TEXT NOT NULL,
 PRIMARY KEY(stream_id,generation,namespace,key))''',
'analysis_stream_reservations':'''CREATE TABLE IF NOT EXISTS analysis_stream_reservations (stream_id TEXT NOT NULL,generation INTEGER NOT NULL,path TEXT NOT NULL,size_bytes INTEGER NOT NULL,PRIMARY KEY(stream_id,generation,path))''',
'analysis_stream_active':'CREATE INDEX IF NOT EXISTS analysis_stream_active ON analysis_stream_jobs(state,report_id,created_utc_ns,stream_id)',
'analysis_stream_run':'CREATE INDEX IF NOT EXISTS analysis_stream_run ON analysis_stream_jobs(run_id,created_utc_ns,stream_id)'}
SCHEMA_SQL=SQL
TABLE_SQL={k:v for k,v in SQL.items() if v.startswith('CREATE TABLE')}
INDEX_SQL={k:v for k,v in SQL.items() if v.startswith('CREATE INDEX')}
@dataclass(frozen=True)
class Limits:
 max_spool_bytes:int=8*1024**3
 min_free_bytes:int=2*1024**3
 max_sources:int=128
 line_bytes:int=1024*1024
 chunk_bytes:int=1024*1024
 def __post_init__(self):
  for key in ('max_spool_bytes','max_sources','line_bytes','chunk_bytes'):
   if type(getattr(self,key)) is not int or getattr(self,key)<1:raise ValueError('invalid_stream_limits')
  if type(self.min_free_bytes) is not int or self.min_free_bytes<0 or self.max_sources>128 or self.line_bytes>1024*1024 or self.chunk_bytes>1024*1024:raise ValueError('invalid_stream_limits')
class StreamError(ValueError):
 def __init__(self,code):
  self.code=code if code in ERROR_CODES else 'stream_source_pins_invalid';super().__init__(self.code)
def has_schema(db):return db.execute("SELECT 1 FROM sqlite_master WHERE name='analysis_stream_jobs' AND type='table'").fetchone() is not None
def install_schema(db):
 for statement in SQL.values():db.execute(statement)
def validate_schema(db):
 for name,expected in SQL.items():
  row=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
  if row is None or row[0] is None or _normalized(row[0])!=_normalized(expected):raise StreamError('stream_schema_invalid')
def _json(raw):
 def unique(pairs):
  out={}
  for key,value in pairs:
   if key in out:raise ValueError('duplicate_key')
   out[key]=value
  return out
 return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
def _hash(value):return type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None
def _ns(value):
 if type(value) is int and 0<=value<1<<63:return value
 if type(value) is str and re.fullmatch('0|[1-9][0-9]{0,18}',value) and int(value)<1<<63:return int(value)
 raise StreamError('stream_dataset_invalid')
_WINDOWS_STAT=os.name=='nt'
def _sig(info):
 # Windows 3.12+ path stat exposes creation time as ctime, whereas fstat
 # exposes metadata change time. Birthtime is the same cross-API token.
 # Keep POSIX ctime, exact file identity, size and modification time intact.
 stamp=getattr(info,'st_birthtime_ns',info.st_ctime_ns) if _WINDOWS_STAT else info.st_ctime_ns
 return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,stamp]
def _row(db,sql,args=()):
 cursor=db.execute(sql,args);record=cursor.fetchone()
 return dict(zip([v[0] for v in cursor.description],record)) if record is not None else None
def _portable(pins):
 result=copy.deepcopy(pins)
 for pin in result:
  for source in pin['files'].values():
   source.pop('signature',None);source.pop('runtime_signature',None)
 return result

def _sql_bounded(method):
 def call(self,*args,**kwargs):
  if self.db.in_transaction:raise ValueError('stream_worker_requires_owned_transaction')
  steps=0;exhausted=False;self._active_stream=None;self._active_generation=None
  def progress():
   nonlocal steps,exhausted
   if exhausted:return 0
   steps+=1000;exhausted=steps>SQL_STEP_LIMIT
   return int(exhausted)
  self.db.set_progress_handler(progress,1000)
  error=None;value=None
  try:value=method(self,*args,**kwargs)
  except BaseException as caught:error=caught
  finally:self.db.set_progress_handler(None,0)
  if exhausted:
   if self._active_stream is not None and self.db.execute('SELECT 1 FROM analysis_stream_jobs WHERE stream_id=? AND generation=?',(self._active_stream,self._active_generation)).fetchone():
    self._failed(self._active_stream,self._active_generation,'stream_sql_resource_limit')
    return self.progress(self._active_stream)
   raise StreamError('stream_sql_resource_limit') from None
  if error is not None:raise error
  return value
 return call

class StreamRunner:
 def __init__(self,root,db,limits=None,reducer=None):
  self.root=Path(root).resolve();self.db=db;self.limits=limits if isinstance(limits,Limits) else Limits(**(limits or {}))
  self.reducer=reducer;self._runtime={};self._pending_handles=[];self.cleanup_pending=False;validate_schema(db)
 def _reducer_id(self):return getattr(self.reducer,'reducer_id','selection-only')+'@'+getattr(self.reducer,'version','1')
 def _path(self,relative):
  if type(relative) is not str or not relative or Path(relative).is_absolute():raise StreamError('stream_source_pins_invalid')
  path=self.root/relative
  if not path.resolve().is_relative_to(self.root):raise StreamError('stream_source_pins_invalid')
  current=path
  while current!=self.root:
   if current.is_symlink():raise StreamError('stream_source_changed')
   current=current.parent
  return path
 def _signature(self,path):
  info=path.stat()
  if not stat.S_ISREG(info.st_mode):raise StreamError('stream_source_changed')
  return _sig(info)
 def _close_handle(self,handle):
  try:handle.close()
  finally:
   if not handle.closed:
    self._pending_handles.append(handle);self.cleanup_pending=True
 @contextmanager
 def _file(self,path,mode):
  handle=path.open(mode)
  try:yield handle
  finally:self._close_handle(handle)
 def _pins(self,run):
  ids=run.get('segment_ids')
  if type(ids) is not list or not ids or len(ids)>self.limits.max_sources or len(set(ids))!=len(ids) or not all(_hash(v) for v in ids):raise StreamError('stream_source_pins_invalid')
  pins=[]
  for job_id in sorted(ids):
   job=_row(self.db,'SELECT * FROM import_jobs WHERE id=?',(job_id,))
   if not job or job['state'] not in ('succeeded','succeeded_with_unsupported') or (job['profile'],job['extractor_version'],job['mapping_revision'])!=(PROFILE,EXTRACTOR_VERSION,MAPPING_REVISION):raise StreamError('stream_source_pins_invalid')
   artifact=_row(self.db,'SELECT * FROM import_artifacts WHERE sha256=?',(job['artifact_sha256'],))
   if not artifact or not _hash(artifact['sha256']) or artifact['source_type'] not in ('SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER'):raise StreamError('stream_source_pins_invalid')
   expected=dict(raw=f"raw/{artifact['sha256'][:2]}/{artifact['sha256']}.wpilog",dataset=f'derived/{job_id}/signals.jsonl',manifest=f'derived/{job_id}/manifest.json')
   if (artifact['relative_path'],job['dataset_path'],job['manifest_path'])!=(expected['raw'],expected['dataset'],expected['manifest']):raise StreamError('stream_source_pins_invalid')
   pin=dict(import_job_id=job_id,source_sha256=artifact['sha256'],source_type=artifact['source_type'],profile=PROFILE,extractor_version=EXTRACTOR_VERSION,importer_version=EXTRACTOR_VERSION,mapping_revision=MAPPING_REVISION,files={})
   for kind,digest in (('raw',artifact['sha256']),('dataset',job['dataset_sha256']),('manifest',job['manifest_sha256'])):
    if not _hash(digest):raise StreamError('stream_source_pins_invalid')
    signature=self._signature(self._path(expected[kind]))
    if kind=='raw' and signature[2]!=artifact['size_bytes']:raise StreamError('stream_source_changed')
    if kind=='manifest' and signature[2]>MAX_MANIFEST:raise StreamError('stream_manifest_invalid')
    pin['files'][kind]=dict(path=expected[kind],sha256=digest,size_bytes=signature[2],signature=signature)
   pins.append(pin)
  return pins
 @_sql_bounded
 def schedule(self,run,context):
  if self.db.in_transaction:raise ValueError('stream_worker_requires_owned_transaction')
  validate_schema(self.db)
  if type(run) is not dict or type(context) is not dict or type(run.get('run_id')) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}',run['run_id']):raise StreamError('stream_source_pins_invalid')
  for key in ('run_id','robot_id','boot_id','runtime_mode','segment_ids','completeness','start_monotonic_ns','end_monotonic_ns'):
   if key in context and key in run and context[key]!=run[key]:raise StreamError('stream_source_pins_invalid')
  lo,hi=_ns(run.get('start_monotonic_ns')),_ns(run.get('end_monotonic_ns'))
  if lo>hi:raise StreamError('stream_source_pins_invalid')
  pins=self._pins(run)
  pipeline_version=getattr(self.reducer,'pipeline_version','automatic-reports-8')
  if pipeline_version not in ('automatic-reports-8','automatic-reports-9'):raise StreamError('stream_source_pins_invalid')
  if pipeline_version=='automatic-reports-9':
   versions=getattr(self.reducer,'context_versions',{})
   if any(key in context and context[key]!=value for key,value in versions.items()):raise StreamError('stream_source_pins_invalid')
   context={**context,**versions}
  combined={**run,**context,'pipeline_version':pipeline_version,'row_selection':SELECTION,'start_monotonic_ns':str(lo),'end_monotonic_ns':str(hi),'stream_reducer_id':self._reducer_id(),'stream_chunk_bytes':self.limits.chunk_bytes,'stream_line_bytes':self.limits.line_bytes}
  types={pin['source_type'] for pin in pins}
  source_type='synthetic' if 'SYNTHETIC' in types else 'simulation' if run.get('runtime_mode')=='SIM' else 'real' if run.get('runtime_mode')=='REAL' else 'historical'
  combined.update(original_import_source_types=sorted(types),stream_source_type_policy='import_origin_before_runtime_1')
  if len(canonical(combined).encode())>MAX_METADATA or len(canonical(run).encode())>MAX_METADATA:raise StreamError('stream_source_pins_invalid')
  stream_id=identity([VERSION,run,combined,_portable(pins)])
  if self.db.execute('SELECT 1 FROM analysis_stream_jobs WHERE stream_id=?',(stream_id,)).fetchone():return self.progress(stream_id)
  initial=self.reducer.initial(combined) if self.reducer is not None else {};state_json=canonical(initial)
  if len(state_json.encode())>MAX_METADATA:raise StreamError('stream_reducer_failed')
  now=time.time_ns()
  with atomic(self.db):
   self.db.execute("INSERT INTO analysis_stream_jobs(stream_id,run_id,generation,state,phase,context_json,run_json,source_type,source_count,selection_chain,created_utc_ns,updated_utc_ns) VALUES (?,?,1,'pending','scheduled',?,?,?,?,?,?,?)",(stream_id,run['run_id'],canonical(combined),canonical(run),source_type,len(pins),INITIAL_CHAIN,now,now))
   for ordinal,pin in enumerate(pins):self.db.execute('INSERT INTO analysis_stream_sources(stream_id,generation,source_ordinal,import_job_id,pins_json) VALUES (?,1,?,?,?)',(stream_id,ordinal,pin['import_job_id'],canonical(pin)))
   self.db.execute('INSERT INTO analysis_stream_checkpoints VALUES (?,1,?,-1,?,?)',(stream_id,self._reducer_id(),state_json,hashlib.sha256(state_json.encode()).hexdigest()))
  return self.progress(stream_id)
 def progress(self,stream_id):
  row=_row(self.db,'SELECT * FROM analysis_stream_jobs WHERE stream_id=?',(stream_id,))
  if row is None:raise KeyError('stream_not_found')
  public={k:row[k] for k in ('stream_id','run_id','generation','state','phase','source_type','source_count','source_ordinal','verified_source_count','verified_bytes','replay_bytes','scanned_rows','selected_rows','selected_bytes','chunk_count','selection_chain','spool_bytes','error_code','report_id','created_utc_ns','updated_utc_ns')}
  if row['state']=='ready' and row['descriptor_json'] is not None:public['descriptor']=_json(row['descriptor_json'])
  return public
 def inventory(self,stream_id,after=-1,limit=100):
  if type(after) is not int or after<-1 or type(limit) is not int or not 1<=limit<=100:raise ValueError('invalid_stream_inventory_query')
  job=_row(self.db,'SELECT generation,chunk_count,state FROM analysis_stream_jobs WHERE stream_id=?',(stream_id,))
  if job is None:raise KeyError('stream_not_found')
  cursor=self.db.execute('SELECT * FROM analysis_stream_chunks WHERE stream_id=? AND generation=? AND ordinal>? ORDER BY ordinal LIMIT ?',(stream_id,job['generation'],after,limit+1));names=[v[0] for v in cursor.description];rows=[dict(zip(names,row)) for row in cursor.fetchall()]
  return dict(items=rows[:limit],total=job['chunk_count'],complete=job['state']=='ready',next_after=rows[limit-1]['ordinal'] if len(rows)>limit else None)
 def _workspace(self,rt):
  relative=f"analysis/streams/{rt['id']}/g{rt['generation']}";path=self._path(relative)
  marker=path/'.owner';expected=canonical(dict(stream_id=rt['id'],generation=rt['generation'])).encode()
  if not path.exists():path.mkdir(parents=True)
  if not marker.exists():
   if any(path.iterdir()):raise StreamError('stream_chunk_conflict')
   self._reserve(rt,marker,len(expected))
   with self._file(marker,'xb') as output:output.write(expected);output.flush();os.fsync(output.fileno())
  elif self._signature(marker)[2]>1024 or self._read_small(marker,1024)!=expected:raise StreamError('stream_chunk_conflict')
  return relative,path
 def _reserve(self,rt,path,size):
  relative=path.relative_to(self.root).as_posix()
  old=self.db.execute('SELECT size_bytes FROM analysis_stream_reservations WHERE stream_id=? AND generation=? AND path=?',(rt['id'],rt['generation'],relative)).fetchone()
  delta=max(0,size-(old[0] if old else 0))
  actual=path.stat().st_size if path.exists() else 0
  self._quota(delta,max(0,size-actual))
  if delta or old is None:
   with atomic(self.db):
    self.db.execute('INSERT INTO analysis_stream_reservations VALUES (?,?,?,?) ON CONFLICT(stream_id,generation,path) DO UPDATE SET size_bytes=MAX(size_bytes,excluded.size_bytes)',(rt['id'],rt['generation'],relative,size))
    self.db.execute('UPDATE analysis_stream_jobs SET spool_bytes=spool_bytes+? WHERE stream_id=? AND generation=?',(delta,rt['id'],rt['generation']))
    self._quota()
 def _quota(self,extra=0,free_extra=None):
  used=self.db.execute('SELECT COALESCE(SUM(spool_bytes),0) FROM analysis_stream_jobs').fetchone()[0]
  pages=self.db.execute('PRAGMA page_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]
  if used+pages+extra>self.limits.max_spool_bytes:raise StreamError('stream_spool_resource_limit')
  if shutil.disk_usage(self.root).free-(extra if free_extra is None else free_extra)<self.limits.min_free_bytes:raise StreamError('stream_free_space_unavailable')
 def _read_small(self,path,limit):
  with self._file(path,'rb') as stream:return stream.read(limit+1)
 def _start(self,job):
  cursor=self.db.execute('SELECT * FROM analysis_stream_sources WHERE stream_id=? AND generation=? ORDER BY source_ordinal',(job['stream_id'],job['generation']))
  sources=[dict(zip([v[0] for v in cursor.description],row)) for row in cursor.fetchall()]
  if len(sources)!=job['source_count'] or len(sources)>self.limits.max_sources:raise StreamError('stream_source_pins_invalid')
  for source in sources:
   source['pin']=_json(source['pins_json'])
   for pin in source['pin']['files'].values():
    pin['runtime_signature']=self._signature(self._path(pin['path']))
    if pin['runtime_signature'][2]!=pin['size_bytes']:raise StreamError('stream_source_changed')
  run=_json(job['run_json']);context=_json(job['context_json'])
  if context.get('stream_chunk_bytes')!=self.limits.chunk_bytes or context.get('stream_line_bytes')!=self.limits.line_bytes:raise StreamError('stream_checkpoint_invalid')
  if identity([VERSION,run,context,_portable([source['pin'] for source in sources])])!=job['stream_id']:raise StreamError('stream_source_pins_invalid')
  checkpoint=_row(self.db,'SELECT * FROM analysis_stream_checkpoints WHERE stream_id=? AND generation=? AND reducer_id=?',(job['stream_id'],job['generation'],self._reducer_id()))
  if not checkpoint or hashlib.sha256(checkpoint['state_json'].encode()).hexdigest()!=checkpoint['state_sha256'] or checkpoint['chunk_ordinal']!=job['chunk_count']-1:raise StreamError('stream_checkpoint_invalid')
  rt=dict(id=job['stream_id'],generation=job['generation'],run=run,context=context,source_type=job['source_type'],sources=sources,
   phase='raw_verify',verify_index=0,source_index=0,offset=0,line=0,hash=hashlib.sha256(),buffer=bytearray(),raw_buffer=bytearray(),
   chunk=bytearray(),chunk_rows=0,chunk_start=0,chunk_end=0,chunk_line=0,ordinal=0,chain=INITIAL_CHAIN,
   committed=job['chunk_count'],checkpoint=checkpoint,state=_json(checkpoint['state_json']),scanned=0,selected_bytes=0,verified_bytes=0,replay_bytes=0,
   previous_cycle=None,past_run_end=False,hazards=dict(total=0,references=[],truncated=False))
  self._workspace(rt);self._runtime[job['stream_id']]=rt
  with atomic(self.db):
   self.db.execute("UPDATE analysis_stream_jobs SET state='verifying',phase='raw_verify',verified_source_count=0,verified_bytes=0,replay_bytes=0 WHERE stream_id=? AND generation=?",(rt['id'],rt['generation']))
   self.db.execute('UPDATE analysis_stream_sources SET raw_verified=0,manifest_verified=0,dataset_verified=0 WHERE stream_id=? AND generation=?',(rt['id'],rt['generation']))
  return rt
 def _hazard(self,rt,kind,**fields):
  hazards=rt['hazards'];hazards['total']+=1
  if len(hazards['references'])<128:hazards['references'].append(dict(kind=kind,**fields))
  else:hazards['truncated']=True
 def _scope(self,rt,row):
  value=row.get('timestamp_ns') if row.get('kind') in ('cycle','sample') else row.get('cycle_timestamp_ns')
  if value is None:return 'unscoped',None
  stamp=_ns(value);lo,hi=_ns(rt['run']['start_monotonic_ns']),_ns(rt['run']['end_monotonic_ns'])
  if stamp>hi:rt['past_run_end']=True
  return ('after_run' if rt['past_run_end'] else 'before_run' if stamp<lo else 'in_run'),stamp
 def _normalized(self,rt,row,source):
  if type(row) is not dict or type(row.get('kind')) is not str:raise StreamError('stream_dataset_invalid')
  if 'source_sha256' in row and row['source_sha256']!=source['pin']['source_sha256']:raise StreamError('stream_source_pins_invalid')
  scope,stamp=self._scope(rt,row)
  if row['kind']=='cycle':
   if stamp is None:raise StreamError('stream_dataset_invalid')
   if rt['previous_cycle'] is not None and stamp<=rt['previous_cycle']:
    self._hazard(rt,'nonadvancing_source_cycle',source_sha256=source['pin']['source_sha256'],record_index=(row.get('record_index',row.get('timestamp_record_index')) if type(row.get('record_index',row.get('timestamp_record_index'))) is int else None),timestamp_ns=str(stamp),previous_timestamp_ns=str(rt['previous_cycle']),stream_scope=scope)
   rt['previous_cycle']=stamp
  return {**row,'source_sha256':source['pin']['source_sha256'],'stream_source_ordinal':source['traversal_ordinal'],'stream_line_ordinal':rt['line'],'stream_scope':scope,'before_run':scope!='in_run'}
 def _active(self,rt):
  job=_row(self.db,'SELECT generation,state,context_json,run_json FROM analysis_stream_jobs WHERE stream_id=?',(rt['id'],))
  if not job or job['generation']!=rt['generation'] or job['state'] not in ('pending','running','verifying') or job['context_json']!=canonical(rt['context']) or job['run_json']!=canonical(rt['run']):raise StreamError('stream_checkpoint_invalid')
 def _final_inputs(self,rt):
  self._active(rt)
  current=_portable(self._pins(rt['run']))
  expected=sorted(_portable([source['pin'] for source in rt['sources']]),key=lambda pin:pin['import_job_id'])
  if current!=expected:raise StreamError('stream_source_pins_invalid')
  for source in rt['sources']:
   for pin in source['pin']['files'].values():
    path=self._path(pin['path'])
    if self._signature(path)!=pin['runtime_signature']:raise StreamError('stream_source_changed')
    with self._file(path,'rb') as stream:
     info=os.fstat(stream.fileno())
     if _sig(info)!=pin['runtime_signature'] or info.st_ctime_ns!=rt['handle_change_times'].get(pin['path']):raise StreamError('stream_source_changed')
    if self._signature(path)!=pin['runtime_signature']:raise StreamError('stream_source_changed')
 def _seal(self,rt,source):
  if not rt['chunk_rows']:return
  self._active(rt)
  data=bytes(rt['chunk']);digest=hashlib.sha256(data).hexdigest()
  meta=dict(ordinal=rt['ordinal'],source_ordinal=source['traversal_ordinal'],start_offset=rt['chunk_start'],end_offset=rt['chunk_end'],row_count=rt['chunk_rows'],size_bytes=len(data),sha256=digest)
  chain=hashlib.sha256(b'stream-selection-1\x00'+bytes.fromhex(rt['chain'])+canonical(meta).encode()).hexdigest()
  existing=_row(self.db,'SELECT * FROM analysis_stream_chunks WHERE stream_id=? AND generation=? AND ordinal=?',(rt['id'],rt['generation'],rt['ordinal']))
  if rt['ordinal']<rt['committed']:
   if not existing or any(existing[k]!=v for k,v in meta.items()) or existing['previous_chain']!=rt['chain'] or existing['selection_chain']!=chain:raise StreamError('stream_checkpoint_invalid')
   path=self._path(existing['path'])
   if self._signature(path)[2]!=len(data) or self._read_small(path,self.limits.chunk_bytes)!=data:raise StreamError('stream_chunk_conflict')
  else:
   if existing:raise StreamError('stream_checkpoint_invalid')
   relative,workspace=self._workspace(rt);path=workspace/f"{rt['ordinal']:012d}.jsonl";self._reserve(rt,path,len(data))
   if path.exists():
    if self._signature(path)[2]!=len(data) or self._read_small(path,self.limits.chunk_bytes)!=data:raise StreamError('stream_chunk_conflict')
   else:
    with self._file(path,'xb') as output:output.write(data);output.flush();os.fsync(output.fileno())
   state=copy.deepcopy(rt['state'])
   with atomic(self.db):
    if self.reducer is not None:
     try:
      for line in data.splitlines():state=self.reducer.consume(self.db,rt['id'],rt['generation'],state,_json(line))
     except sqlite3.Error:raise
     except Exception:raise StreamError('stream_reducer_failed') from None
    state_json=canonical(state)
    if len(state_json.encode())>MAX_METADATA:raise StreamError('stream_reducer_failed')
    self.db.execute('INSERT INTO analysis_stream_chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(rt['id'],rt['generation'],rt['ordinal'],path.relative_to(self.root).as_posix(),digest,len(data),rt['chunk_rows'],source['traversal_ordinal'],rt['chunk_start'],rt['chunk_end'],rt['chain'],chain))
    self.db.execute('UPDATE analysis_stream_checkpoints SET chunk_ordinal=?,state_json=?,state_sha256=? WHERE stream_id=? AND generation=? AND reducer_id=?',(rt['ordinal'],state_json,hashlib.sha256(state_json.encode()).hexdigest(),rt['id'],rt['generation'],self._reducer_id()))
    self.db.execute('UPDATE analysis_stream_jobs SET scanned_rows=?,selected_rows=?,selected_bytes=?,chunk_count=?,selection_chain=? WHERE stream_id=? AND generation=?',(rt['scanned'],rt['scanned'],rt['selected_bytes'],rt['ordinal']+1,chain,rt['id'],rt['generation']))
    self.db.execute('UPDATE analysis_stream_sources SET scan_offset=?,line_ordinal=? WHERE stream_id=? AND generation=? AND source_ordinal=?',(rt['chunk_end'],rt['chunk_line'],rt['id'],rt['generation'],source['source_ordinal']))
    self._quota()
   rt['state']=state
  rt['chain']=chain;rt['ordinal']+=1;rt['chunk']=bytearray();rt['chunk_rows']=0;rt['chunk_start']=rt['chunk_end']
 def _accept_line(self,rt,source,data,end):
  if len(data)>self.limits.line_bytes:raise StreamError('stream_line_resource_limit')
  try:row=_json(data)
  except (UnicodeError,ValueError,RecursionError):raise StreamError('stream_dataset_invalid') from None
  rt['line']+=1;row=self._normalized(rt,row,source);encoded=(canonical(row)+'\n').encode('utf-8')
  if len(encoded)>self.limits.chunk_bytes:raise StreamError('stream_line_resource_limit')
  if rt['chunk_rows'] and len(rt['chunk'])+len(encoded)>self.limits.chunk_bytes:self._seal(rt,source)
  rt['chunk'].extend(encoded);rt['chunk_rows']+=1;rt['chunk_end']=end;rt['chunk_line']=rt['line'];rt['scanned']+=1;rt['selected_bytes']+=len(encoded)
 def _read(self,rt,source,kind,budget,deadline,row_budget):
  pin=source['pin']['files'][kind];path=self._path(pin['path']);read=0;rows=0;eof=False
  if self._signature(path)!=pin['runtime_signature']:raise StreamError('stream_source_changed')
  with self._file(path,'rb') as stream:
   opened_info=os.fstat(stream.fileno())
   if _sig(opened_info)!=pin['runtime_signature']:raise StreamError('stream_source_changed')
   opened_change_time=opened_info.st_ctime_ns
   change_times=rt.setdefault('handle_change_times',{})
   if change_times.setdefault(pin['path'],opened_change_time)!=opened_change_time:raise StreamError('stream_source_changed')
   stream.seek(rt['offset'])
   while read<budget and rows<row_budget and time.monotonic()<deadline:
    block=stream.readline(min(budget-read,self.limits.line_bytes+1-len(rt['raw_buffer']))) if kind=='dataset' else stream.read(min(1024*1024,budget-read))
    if not block:
     if kind=='dataset' and rt['raw_buffer']:
      self._accept_line(rt,source,bytes(rt['raw_buffer']),rt['offset']);rt['raw_buffer'].clear();rows+=1
     eof=True;break
    old_offset=rt['offset'];read+=len(block);rt['offset']+=len(block);rt['hash'].update(block)
    if kind=='dataset':rt['replay_bytes']+=max(0,min(rt['offset'],source['scan_offset'])-old_offset)
    if kind=='manifest':
     rt['buffer'].extend(block)
     if len(rt['buffer'])>MAX_MANIFEST:raise StreamError('stream_manifest_invalid')
    if kind=='dataset':
     rt['raw_buffer'].extend(block)
     if len(rt['raw_buffer'])>self.limits.line_bytes:raise StreamError('stream_line_resource_limit')
     if block.endswith(b'\n'):
      self._accept_line(rt,source,bytes(rt['raw_buffer']),rt['offset']);rt['raw_buffer'].clear();rows+=1
   closing_info=os.fstat(stream.fileno())
   if _sig(closing_info)!=pin['runtime_signature'] or closing_info.st_ctime_ns!=opened_change_time:raise StreamError('stream_source_changed')
  if self._signature(path)!=pin['runtime_signature']:raise StreamError('stream_source_changed')
  if eof and (rt['offset']!=pin['size_bytes'] or rt['hash'].hexdigest()!=pin['sha256']):raise StreamError('stream_source_hash_mismatch')
  rt['verified_bytes']+=read
  return read,rows,eof
 def _manifest(self,rt,source):
  try:manifest=_json(bytes(rt['buffer']))
  except (ValueError,UnicodeError,RecursionError):raise StreamError('stream_manifest_invalid') from None
  pin=source['pin'];expected=dict(schema_version=1,job_id=pin['import_job_id'],source_sha256=pin['source_sha256'],source_size_bytes=pin['files']['raw']['size_bytes'],raw_path=pin['files']['raw']['path'],source_type=pin['source_type'],extractor_version=EXTRACTOR_VERSION,profile=PROFILE,mapping_revision=MAPPING_REVISION,dataset_path=pin['files']['dataset']['path'],dataset_sha256=pin['files']['dataset']['sha256'])
  if type(manifest) is not dict or any(manifest.get(k)!=v for k,v in expected.items()):raise StreamError('stream_manifest_invalid')
  _ns(manifest.get('first_cycle_timestamp_ns'));_ns(manifest.get('last_cycle_timestamp_ns'));source['manifest']={key:manifest[key] for key in ('first_cycle_timestamp_ns','last_cycle_timestamp_ns')}
  if manifest.get('unsupported_types'):self._hazard(rt,'unsupported_source_types_present',source_sha256=pin['source_sha256'],stream_scope='full_source')
  self.db.execute('UPDATE analysis_stream_sources SET manifest_json=? WHERE stream_id=? AND generation=? AND source_ordinal=?',(canonical(manifest),rt['id'],rt['generation'],source['source_ordinal']))
  self._quota()
 def _after_verification(self,rt,source,kind):
  with atomic(self.db):
   if kind=='raw':self.db.execute('UPDATE analysis_stream_sources SET raw_verified=1,raw_bytes=? WHERE stream_id=? AND generation=? AND source_ordinal=?',(rt['offset'],rt['id'],rt['generation'],source['source_ordinal']))
   elif kind=='manifest':
    self._manifest(rt,source)
    self.db.execute('UPDATE analysis_stream_sources SET manifest_verified=1,manifest_bytes=? WHERE stream_id=? AND generation=? AND source_ordinal=?',(rt['offset'],rt['id'],rt['generation'],source['source_ordinal']))
   else:
    self._seal(rt,source)
    self.db.execute('UPDATE analysis_stream_sources SET dataset_verified=1,dataset_bytes=? WHERE stream_id=? AND generation=? AND source_ordinal=?',(rt['offset'],rt['id'],rt['generation'],source['source_ordinal']))
  rt.update(offset=0,hash=hashlib.sha256(),buffer=bytearray(),raw_buffer=bytearray(),previous_cycle=None,past_run_end=False)
  if kind=='raw':rt['phase']='manifest_verify'
  elif kind=='manifest':
   rt['verify_index']+=1
   if rt['verify_index']<len(rt['sources']):rt['phase']='raw_verify'
   else:
    rt['sources'].sort(key=lambda item:(_ns(item['manifest']['first_cycle_timestamp_ns']),item['pin']['import_job_id']))
    ranges=[];lo,hi=_ns(rt['run']['start_monotonic_ns']),_ns(rt['run']['end_monotonic_ns'])
    with atomic(self.db):
     for ordinal,item in enumerate(rt['sources']):
      item['traversal_ordinal']=ordinal
      self.db.execute('UPDATE analysis_stream_sources SET traversal_ordinal=? WHERE stream_id=? AND generation=? AND source_ordinal=?',(ordinal,rt['id'],rt['generation'],item['source_ordinal']))
      a=max(lo,_ns(item['manifest']['first_cycle_timestamp_ns']));b=min(hi,_ns(item['manifest']['last_cycle_timestamp_ns']))
      if a<=b:
       if any(max(a,c)<=min(b,d) for c,d in ranges):self._hazard(rt,'overlapping_source_cycle_intervals',source_sha256=item['pin']['source_sha256'],stream_scope='full_source')
       ranges.append((a,b))
    rt['phase']='dataset_verify';rt['chunk_start']=0
  else:
   rt['source_index']+=1;rt['line']=0;rt['chunk_start']=0;rt['chunk_end']=0
   if rt['source_index']==len(rt['sources']):
    if rt['ordinal']<rt['committed']:raise StreamError('stream_checkpoint_invalid')
    rt['phase']='manifest_publish';rt['manifest_ordinal']=-1;rt['manifest_hash']=hashlib.sha256();rt['manifest_bytes']=0
 def _descriptor(self,rt):
  return dict(input_format=INPUT_FORMAT,framework=VERSION,profile=PROFILE,importer_version=EXTRACTOR_VERSION,extractor_version=EXTRACTOR_VERSION,
   mapping_revision=MAPPING_REVISION,stream_id=rt['id'],generation=rt['generation'],source_type=rt['source_type'],
   source_hashes=sorted({s['pin']['source_sha256'] for s in rt['sources']}),source_count=len(rt['sources']),
   source_pins=_portable([s['pin'] for s in rt['sources']]),context=rt['context'],run=rt['run'],selection_chain=rt['chain'],
   selected_rows=rt['scanned'],scanned_rows=rt['scanned'],selected_bytes=rt['selected_bytes'],chunk_count=rt['ordinal'],
   source_hazards=rt['hazards'],full_source_verified=True,original_import_source_types=sorted({s['pin']['source_type'] for s in rt['sources']}))
 def _publish_manifest(self,rt,max_bytes,max_rows,deadline):
  relative,workspace=self._workspace(rt);partial=workspace/'manifest.part';target=workspace/'manifest.jsonl'
  mode='r+b' if partial.exists() else 'x+b';used=0;rows=0;complete=False
  with self._file(partial,mode) as output:
   output.seek(rt['manifest_bytes'])
   while used<max_bytes and rows<max_rows and time.monotonic()<deadline:
    ordinal=rt['manifest_ordinal']
    if ordinal==-1:record=dict(kind=INPUT_FORMAT,stream_id=rt['id'],generation=rt['generation'],selection=SELECTION)
    elif ordinal<rt['ordinal']:
     record=_row(self.db,'SELECT * FROM analysis_stream_chunks WHERE stream_id=? AND generation=? AND ordinal=?',(rt['id'],rt['generation'],ordinal))
     if record is None:raise StreamError('stream_inventory_invalid')
    else:record=dict(kind='complete',selection_chain=rt['chain'],selected_rows=rt['scanned'],selected_bytes=rt['selected_bytes'],chunk_count=rt['ordinal'])
    line=(canonical(record)+'\n').encode()
    position=rt.get('manifest_line_position',0)
    fragment=line[position:position+max_bytes-used]
    if not fragment:break
    self._reserve(rt,partial,rt['manifest_bytes']+len(fragment))
    saved=output.read(len(fragment))
    if saved and saved!=fragment:raise StreamError('stream_inventory_invalid')
    if not saved:output.write(fragment)
    rt['manifest_hash'].update(fragment);rt['manifest_bytes']+=len(fragment);used+=len(fragment)
    position+=len(fragment);rt['manifest_line_position']=position
    if position==len(line):
     rows+=1;rt['manifest_ordinal']+=1;rt['manifest_line_position']=0
     if ordinal==rt['ordinal']:
      if output.read(1):raise StreamError('stream_inventory_invalid')
      complete=True;break
   output.flush();os.fsync(output.fileno())
  if not complete:return
  if target.exists():
   if _sig(target.stat())[:2]!=_sig(partial.stat())[:2]:raise StreamError('stream_inventory_invalid')
  else:os.link(partial,target)
  self._final_inputs(rt)
  descriptor=self._descriptor(rt)
  descriptor['final_manifest']=dict(path=target.relative_to(self.root).as_posix(),sha256=rt['manifest_hash'].hexdigest(),size_bytes=rt['manifest_bytes'])
  descriptor['input_digest']=identity(descriptor)
  if self.reducer is not None:
   try:descriptor['result']=self.reducer.finish(copy.deepcopy(rt['state']),descriptor)
   except Exception:raise StreamError('stream_reducer_failed') from None
  raw=canonical(descriptor)
  if len(raw.encode())>4*MAX_METADATA:raise StreamError('stream_reducer_failed')
  with atomic(self.db):
   self.db.execute("UPDATE analysis_stream_jobs SET state='ready',phase='complete',descriptor_json=?,final_manifest_path=?,final_manifest_sha256=?,final_manifest_size_bytes=?,updated_utc_ns=? WHERE stream_id=? AND generation=?",(raw,descriptor['final_manifest']['path'],descriptor['final_manifest']['sha256'],rt['manifest_bytes'],time.time_ns(),rt['id'],rt['generation']))
   self.db.execute('UPDATE analysis_stream_reservations SET path=? WHERE stream_id=? AND generation=? AND path=?',(target.relative_to(self.root).as_posix(),rt['id'],rt['generation'],partial.relative_to(self.root).as_posix()))
  partial.unlink();self._runtime.pop(rt['id'],None)
 @_sql_bounded
 def step(self,stream_id=None,*,max_rows=4096,max_bytes=4*1024*1024,max_seconds=.025):
  if self.db.in_transaction:raise ValueError('stream_worker_requires_owned_transaction')
  if type(max_rows) is not int or max_rows<1 or type(max_bytes) is not int or max_bytes<1 or type(max_seconds) not in (int,float) or not 0<max_seconds<=60:raise ValueError('invalid_stream_work_budget')
  validate_schema(self.db)
  if stream_id is None:
   job=_row(self.db,"SELECT * FROM analysis_stream_jobs WHERE (state='ready' AND report_id IS NULL) OR state IN ('pending','verifying','running') ORDER BY CASE WHEN state='ready' THEN 0 ELSE 1 END,created_utc_ns,stream_id LIMIT 1")
   if job is None:return dict(state='idle',phase='complete')
   stream_id=job['stream_id']
  else:job=_row(self.db,'SELECT * FROM analysis_stream_jobs WHERE stream_id=?',(stream_id,))
  if job is None:raise KeyError('stream_not_found')
  self._active_stream=stream_id;self._active_generation=job['generation']
  if job['state'] in ('ready','failed','canceled') or self.cleanup_pending:return self.progress(stream_id)
  deadline=time.monotonic()+max_seconds
  try:
   if stream_id in self._runtime and self._runtime[stream_id]['generation']!=job['generation']:
    self._runtime.pop(stream_id,None);return self.progress(stream_id)
   rt=self._runtime.get(stream_id) or self._start(job)
   if rt['phase']=='manifest_publish':self._publish_manifest(rt,max_bytes,max_rows,deadline)
   else:
    kind='raw' if rt['phase']=='raw_verify' else 'manifest' if rt['phase']=='manifest_verify' else 'dataset'
    source=rt['sources'][rt['source_index'] if kind=='dataset' else rt['verify_index']]
    read,rows,eof=self._read(rt,source,kind,max_bytes,deadline,max_rows)
    if eof:self._after_verification(rt,source,kind)
   if stream_id in self._runtime:
    with atomic(self.db):
     self.db.execute("UPDATE analysis_stream_jobs SET state=?,phase=?,source_ordinal=?,verified_source_count=(SELECT COUNT(*) FROM analysis_stream_sources WHERE stream_id=? AND generation=? AND dataset_verified=1),verified_bytes=?,replay_bytes=?,updated_utc_ns=? WHERE stream_id=? AND generation=?",('running' if rt['phase']=='dataset_verify' else 'verifying',rt['phase'],rt['source_index'],rt['id'],rt['generation'],rt['verified_bytes'],rt['replay_bytes'],time.time_ns(),rt['id'],rt['generation']))
  except StreamError as error:self._failed(stream_id,job['generation'],error.code)
  except (OSError,UnicodeError,ValueError,TypeError,KeyError,RecursionError):self._failed(stream_id,job['generation'],'stream_source_changed')
  return self.progress(stream_id)
 def _failed(self,stream_id,generation,code):
  self._runtime.pop(stream_id,None)
  with atomic(self.db):self.db.execute("UPDATE analysis_stream_jobs SET state='failed',phase='failed',error_code=?,updated_utc_ns=? WHERE stream_id=? AND generation=? AND state NOT IN ('ready','canceled')",(code,time.time_ns(),stream_id,generation))
 def cancel(self,stream_id):
  self._runtime.pop(stream_id,None)
  with atomic(self.db):self.db.execute("UPDATE analysis_stream_jobs SET state='canceled',phase='canceled',updated_utc_ns=? WHERE stream_id=? AND state NOT IN ('ready','failed')",(time.time_ns(),stream_id))
  return self.progress(stream_id)
 def resume(self,stream_id):
  self._runtime.pop(stream_id,None)
  with atomic(self.db):self.db.execute("UPDATE analysis_stream_jobs SET state='pending',phase='scheduled',updated_utc_ns=? WHERE stream_id=? AND state='canceled'",(time.time_ns(),stream_id))
  return self.progress(stream_id)
 def close(self):
  self._runtime.clear();remaining=[]
  for handle in self._pending_handles:
   try:handle.close()
   except Exception:pass
   if not handle.closed:remaining.append(handle)
  self._pending_handles=remaining;self.cleanup_pending=bool(remaining)
  return not self.cleanup_pending
