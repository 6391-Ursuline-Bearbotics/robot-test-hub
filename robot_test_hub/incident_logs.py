"""Original log downloads restricted to a hash-verified preserved incident sidecar."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time

from .notebook import Notebook
from .video_investigation import InvestigationError, digest, identity, integer, strict_json
from .wpilog import PROFILE, EXTRACTOR_VERSION, MAPPING_REVISION

MAX_LOG_BYTES=16*1024**3
SOURCES=('SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER')
BASES=('saved_run_import_job_references','conditional_same_robot_boot_catalog_references','unavailable')


class LogError(InvestigationError):pass


def safe_path(root,sha256):
    path=root/'raw'/sha256[:2]/(sha256+'.wpilog')
    for part in (path,*path.parents):
        if part==root.parent:break
        if (part.is_symlink() or part.resolve()!=part.absolute() or
                (part.exists() and getattr(part.lstat(),'st_file_attributes',0)&0x400)):
            raise LogError('incident_log_path_conflict',409)
    return path


class VerifiedLog:
    def __init__(self,manager,stream,path,receipt,stamp):
        self.manager,self.stream,self.path,self.receipt,self.stamp=manager,stream,path,receipt,stamp
        self.deadline=manager.clock()+manager.stream_timeout

    def read(self,count):
        self.manager._check(self.deadline)
        count=integer(count,1,65536)
        self.check_identity()
        result=self.stream.read(count)
        self.check_identity()
        self.manager._check(self.deadline)
        return result

    def seek(self,offset):self.stream.seek(offset)

    def check_identity(self):
        self.manager._identity(self.stream,self.path,self.stamp)


class IncidentLogs:
    def __init__(self,service,*,clock=time.monotonic,verification_timeout=60,stream_timeout=300,slots=2):
        self.service,self.clock=service,clock
        self.verification_timeout,self.stream_timeout=verification_timeout,stream_timeout
        self.maximum=slots;self.condition=threading.Condition();self.active=0
        self.notebook=Notebook(service.root/'catalog.sqlite3')
        with service.settings_lock:
            if service.stop.is_set() or service.closed:raise LogError('service_stopping',503)
            service.io_lifetimes.append(self)

    def _check(self,deadline=None):
        if self.service.stop.is_set() or self.service.closed:raise LogError('service_stopping',503)
        if deadline is not None and self.clock()>=deadline:raise LogError('incident_log_timeout',503)

    @contextmanager
    def _slot(self):
        with self.service.settings_lock:
            self._check()
            with self.condition:
                if self.active>=self.maximum:raise LogError('incident_logs_busy',503)
                self.active+=1
        try:yield
        finally:
            with self.condition:self.active-=1;self.condition.notify_all()

    def join(self,timeout=None):
        with self.condition:self.condition.wait_for(lambda:self.active==0,timeout)

    def is_alive(self):
        with self.condition:return self.active!=0

    @staticmethod
    def _close(stream):
        if stream is not None:
            while True:
                try:stream.close();return
                except Exception:time.sleep(.05)

    def _references(self,item_id):
        digest(item_id);stream=None
        try:
            with self.service.media.lock:
                cached=self.service.media.items.get(item_id)
                if cached is None:raise LogError('media_not_ready',404)
                expected=digest(cached.get('sidecar_sha256'))
            stream,size=self.service.media.open_item(item_id,sidecar=True)
            if size>4194304:raise LogError('incident_sidecar_conflict',409)
            body=stream.read(4194305)
            if len(body)!=size or hashlib.sha256(body).hexdigest()!=expected:
                raise LogError('incident_sidecar_conflict',409)
            document=strict_json(body)
            if not isinstance(document,dict) or document.get('item_id')!=item_id:raise LogError('incident_sidecar_conflict',409)
            logs=document.get('original_logs')
            if not isinstance(logs,dict) or logs.get('basis') not in BASES:raise LogError('incident_sidecar_conflict',409)
            if 'projection_version' in logs and (type(logs['projection_version']) is not int or logs['projection_version']!=2):
                raise LogError('incident_sidecar_conflict',409)
            references=logs.get('items')
            if not isinstance(references,list) or len(references)>100:raise LogError('incident_sidecar_conflict',409)
            if logs['basis']=='unavailable' and references:raise LogError('incident_sidecar_conflict',409)
            seen=set();projected=[]
            for reference in references:
                if not isinstance(reference,dict):raise LogError('incident_sidecar_conflict',409)
                job_id=identity(reference.get('import_job_id'))
                if job_id in seen:raise LogError('incident_sidecar_conflict',409)
                seen.add(job_id)
                pin=dict(import_job_id=job_id,sha256=None,size_bytes=None,source_type=None,format_valid=False)
                if reference.get('state')=='catalog_reference':
                    pin.update(sha256=digest(reference.get('sha256')),
                        size_bytes=integer(reference.get('size_bytes'),1,MAX_LOG_BYTES),
                        source_type=reference.get('source_type'),format_valid=reference.get('format_valid') is True)
                    if type(pin['source_type']) is not str or pin['source_type'] not in SOURCES+('other','MANUAL','TRANSFER_CHECKSUM_MISMATCH'):
                        pin['source_type']='unknown'
                projected.append(pin)
            return logs['basis'],projected
        finally:self._close(stream)

    def _candidate(self,pin):
        result=dict(pin,state='unavailable',error_code='incident_log_reference_unavailable',
                    original_bytes_reverified_for_export=False)
        if pin['sha256'] is None or pin['source_type'] not in SOURCES or not pin['format_valid']:return result
        with self.notebook._connection() as db:
            row=db.execute('SELECT j.artifact_sha256,j.state,j.profile,j.extractor_version,j.mapping_revision,'
                'a.sha256,a.size_bytes,a.relative_path,a.source_type,a.format_state '
                'FROM import_jobs j JOIN import_artifacts a ON a.sha256=j.artifact_sha256 WHERE j.id=?',
                (pin['import_job_id'],)).fetchone()
        if row is None:return result
        canonical='raw/'+pin['sha256'][:2]+'/'+pin['sha256']+'.wpilog'
        valid=(row['artifact_sha256']==pin['sha256'] and row['sha256']==pin['sha256'] and
            row['size_bytes']==pin['size_bytes'] and row['relative_path']==canonical and
            row['source_type']==pin['source_type'] and row['format_state']=='valid' and
            row['state'] in ('succeeded','succeeded_with_unsupported') and row['profile']==PROFILE and
            row['extractor_version']==EXTRACTOR_VERSION and row['mapping_revision']==MAPPING_REVISION)
        if not valid:
            result['error_code']='incident_log_catalog_conflict';return result
        try:
            path=safe_path(self.service.root,pin['sha256'])
            if not stat.S_ISREG(path.stat().st_mode):raise OSError()
        except (OSError,LogError):
            result['error_code']='incident_log_unavailable';return result
        return dict(result,state='download_candidate',error_code=None)

    def metadata(self,item_id):
        with self._slot():
            basis,pins=self._references(item_id)
            items=[self._candidate(pin) for pin in pins]
            self._check()
            return dict(schema_version=1,item_id=item_id,basis=basis,qualification='pinned_catalog_references',items=items)

    @staticmethod
    def _stamp(info):return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

    def _identity(self,stream,path,stamp):
        safe_path(self.service.root,path.stem)
        opened=os.fstat(stream.fileno());current=path.stat()
        if (not stat.S_ISREG(opened.st_mode) or self._stamp(opened)!=stamp or self._stamp(current)!=stamp):
            raise LogError('incident_log_evidence_conflict',409)

    @contextmanager
    def open(self,item_id,job_id):
        identity(job_id)
        with self._slot():
            stream=None
            try:
                _,pins=self._references(item_id)
                pin=next((pin for pin in pins if pin['import_job_id']==job_id),None)
                if pin is None:raise LogError('incident_log_not_referenced',404)
                candidate=self._candidate(pin)
                if candidate['state']!='download_candidate':raise LogError(candidate['error_code'],409)
                path=safe_path(self.service.root,pin['sha256'])
                descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
                try:stream=os.fdopen(descriptor,'rb')
                except BaseException:os.close(descriptor);raise
                info=os.fstat(stream.fileno());stamp=self._stamp(info)
                self._identity(stream,path,stamp)
                if info.st_size!=pin['size_bytes']:raise LogError('incident_log_evidence_conflict',409)
                deadline=self.clock()+self.verification_timeout;value=hashlib.sha256();size=0
                while True:
                    self._check(deadline);block=stream.read(65536)
                    if not block:break
                    size+=len(block)
                    if size>pin['size_bytes']:raise LogError('incident_log_evidence_conflict',409)
                    value.update(block)
                self._check(deadline);self._identity(stream,path,stamp)
                if size!=pin['size_bytes'] or value.hexdigest()!=pin['sha256']:
                    raise LogError('incident_log_evidence_conflict',409)
                if self._candidate(pin)['state']!='download_candidate':raise LogError('incident_log_catalog_conflict',409)
                stream.seek(0)
                yield VerifiedLog(self,stream,path,pin,stamp)
            finally:self._close(stream)


def byte_range(value,size):
    if value is None:return 0,size-1,200
    import re
    if len(value)>128:raise LogError('invalid_incident_log_range',416)
    match=re.fullmatch(r'bytes=([0-9]*)-([0-9]*)',value)
    if match is None or not any(match.groups()):raise LogError('invalid_incident_log_range',416)
    a,b=match.groups()
    if a:start=int(a);end=min(int(b),size-1) if b else size-1
    else:
        count=int(b)
        if count==0:raise LogError('invalid_incident_log_range',416)
        start=max(0,size-count);end=size-1
    if start>=size or end<start:raise LogError('invalid_incident_log_range',416)
    return start,end,206
