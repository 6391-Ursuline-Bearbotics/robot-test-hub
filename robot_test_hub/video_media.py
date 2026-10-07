"""Explicit, locally pinned media tools and asynchronous immutable clip preservation.

Media is a derivative, never a replacement for recording or alignment evidence.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from .video_investigation import (InvestigationError, VideoInvestigation, fields,
                                 identity, digest, integer, ns, strict_json, _safe)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def sha(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):value.update(block)
    return value.hexdigest()


class MediaError(InvestigationError):
    pass


@dataclass(frozen=True)
class MediaToolsConfig:
    ffmpeg: str
    ffprobe: str
    version: str
    ffmpeg_sha256: str
    ffprobe_sha256: str
    operation_timeout: float=600
    maximum_output_bytes: int=1073741824
    maximum_total_bytes: int=21474836480
    minimum_free_bytes: int=268435456
    maximum_jobs: int=100

    def __post_init__(self):
        for value in (self.ffmpeg,self.ffprobe):
            if (type(value) is not str or value.startswith(('\\\\','//'))
                    or not Path(value).is_absolute() or Path(value).is_symlink()):
                raise MediaError('invalid_media_tools_configuration')
        for value in (self.ffmpeg_sha256,self.ffprobe_sha256):digest(value)
        if type(self.version) is not str or not 1<=len(self.version)<=128 or any(c.isspace() for c in self.version):
            raise MediaError('invalid_media_tools_configuration')
        value=self.operation_timeout
        if type(value) not in (int,float) or not 1<=value<=3600 or not math.isfinite(value):
            raise MediaError('invalid_media_tools_configuration')
        integer(self.maximum_output_bytes,1024,8589934592)
        integer(self.maximum_total_bytes,self.maximum_output_bytes,107374182400)
        integer(self.minimum_free_bytes,0,107374182400)
        integer(self.maximum_jobs,1,1000)


def load_media_config(path):
    try:
        path=Path(path)
        if path.is_symlink() or path.stat().st_size>32768:raise ValueError()
        value=strict_json(path.read_bytes())
        if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value.pop('schema_version')!=1:raise ValueError()
        return MediaToolsConfig(**value)
    except (OSError,ValueError,TypeError,InvestigationError):
        raise MediaError('invalid_media_tools_configuration') from None


class NativeMediaAdapter:
    def __init__(self,config):self.config=config

    def _run(self,argv,stop,*,output=None,limit=16777216):
        if stop.is_set():raise MediaError('media_interrupted',503)
        process=None;reader=None;blocks=[];failure=[]
        def read():
            try:
                size=0
                while block:=process.stdout.read1(65536):
                    size+=len(block)
                    if size>limit:failure.append('media_probe_too_large');process.kill();break
                    blocks.append(block)
            except OSError:failure.append('media_process_failed')
        try:
            process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            reader=threading.Thread(target=read,name='hub-media-output');reader.start()
            deadline=time.monotonic()+self.config.operation_timeout
            while process.poll() is None:
                if stop.is_set():raise MediaError('media_interrupted',503)
                if time.monotonic()>=deadline:raise MediaError('media_timeout',503)
                if output is not None:
                    if output.exists() and output.stat().st_size>self.config.maximum_output_bytes:
                        raise MediaError('media_output_too_large',413)
                    if shutil.disk_usage(output.parent).free<self.config.minimum_free_bytes:
                        raise MediaError('media_disk_limit',503)
                stop.wait(.02)
            reader.join()
            if failure:raise MediaError(failure[0],503)
            if process.returncode:raise MediaError('media_process_failed',503)
            return b''.join(blocks)
        except (OSError,subprocess.SubprocessError):
            raise MediaError('media_process_failed',503) from None
        finally:
            # Retain service ownership through every native child/pipe cleanup.
            if process is not None:
                while True:
                    try:
                        if process.poll() is not None:break
                        process.kill();process.wait(timeout=.1)
                    except (OSError,subprocess.SubprocessError):time.sleep(.05)
                if reader is not None:
                    while reader.is_alive():reader.join(.05)
                if process.stdout is not None:
                    while True:
                        try:process.stdout.close();break
                        except Exception:time.sleep(.05)

    def validate(self,stop):
        for executable,expected,label in ((self.config.ffmpeg,self.config.ffmpeg_sha256,'ffmpeg'),
                                         (self.config.ffprobe,self.config.ffprobe_sha256,'ffprobe')):
            path=Path(executable)
            if path.is_symlink() or sha(path)!=expected:raise MediaError('media_tools_integrity',503)
            lines=self._run([str(path),'-version'],stop,limit=65536).decode('utf-8').splitlines()
            first=lines[0].split() if lines else []
            if len(first)<3 or first[:3]!=[label,'version',self.config.version]:
                raise MediaError('media_tools_version',503)

    def render(self,source,first,last,output,stop):
        self._run([self.config.ffmpeg,'-hide_banner','-n','-loglevel','error','-copyts',
            '-i',str(source),'-map','0:v:0','-an','-vf',
            f'trim=start_frame={first}:end_frame={last+1},setpts=PTS-STARTPTS,settb=1/1000000000',
            '-c:v','libx264','-pix_fmt','yuv420p','-preset','ultrafast','-bf','0',
            '-fps_mode','passthrough','-enc_time_base','1/1000000000',
            '-video_track_timescale','1000000000','-movflags','+faststart',str(output)],stop,output=output)

    def probe(self,path,stop):
        data=strict_json(self._run([self.config.ffprobe,'-v','error','-show_streams','-show_frames',
            '-show_entries','stream=codec_type,codec_name,pix_fmt,time_base:frame=best_effort_timestamp,duration,pkt_duration',
            '-of','json',str(path)],stop))
        if not isinstance(data,dict):raise MediaError('media_probe_mismatch',409)
        streams=data.get('streams',[])
        if not isinstance(streams,list) or not all(isinstance(stream,dict) for stream in streams):
            raise MediaError('media_probe_mismatch',409)
        if len(streams)!=1 or streams[0].get('codec_name')!='h264' or streams[0].get('pix_fmt')!='yuv420p':
            raise MediaError('media_probe_mismatch',409)
        try:
            tick=Fraction(streams[0]['time_base'])*1000000000
            if tick<=0:raise ValueError()
            frames=[]
            if not isinstance(data['frames'],list) or len(data['frames'])>10000:raise ValueError()
            for frame in data['frames']:
                if not isinstance(frame,dict):raise ValueError()
                pts=Fraction(int(frame['best_effort_timestamp']))*tick
                duration=Fraction(int(frame.get('duration',frame.get('pkt_duration'))))*tick
                if pts.denominator!=1 or duration.denominator!=1 or duration<=0:raise ValueError()
                frames.append((int(pts),int(duration)))
        except (ValueError,TypeError,KeyError,ZeroDivisionError,OverflowError):
            raise MediaError('media_probe_mismatch',409) from None
        return frames


def verify_frames(actual,expected):
    if actual!=[(int(f['derived_pts_ns']),int(f['duration_ns'])) for f in expected]:
        raise MediaError('media_probe_mismatch',409)


class MediaWorker:
    def __init__(self,service,config=None,*,adapter_factory=NativeMediaAdapter):
        self.service,self.config=service,config
        self.folder=service.root/'video-media'
        self.adapter=adapter_factory(config) if config is not None else None
        self.lock=threading.RLock();self.writer_lock=threading.Lock();self.jobs={};self.items={}
        self.reservation=0;self.reserved_working=None
        self.tools={'schema_version':1,'enabled':config is not None,'state':'starting' if config else 'disabled','error_code':None}

    def tools_snapshot(self):
        with self.lock:return copy.deepcopy(self.tools)

    def _path(self,name):return _safe(self.folder,name)

    def _publish(self,name,data,*,immutable=False,terminal=False):
        with self.service.settings_lock:
            if (self.service.stop.is_set() and not terminal) or self.service.closed:raise MediaError('service_stopping',503)
            self.folder.mkdir(parents=True,exist_ok=True)
            if self.config is not None:self._quota(additional=len(data))
            target=self._path(name);temporary=self._path('.writing-'+uuid.uuid4().hex)
            with temporary.open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            if immutable:
                try:os.link(temporary,target)
                except FileExistsError:
                    if target.read_bytes()!=data:raise MediaError('preservation_conflict',409) from None
            else:os.replace(temporary,target)
            if temporary.exists():temporary.unlink()

    def _validate(self,payload):
        fields(payload,('request_id','selection'));identity(payload['request_id'])
        selection=payload['selection']
        if not isinstance(selection,dict):raise MediaError('invalid_preservation')
        common=('kind','alignment_id','revision','sha256','candidate_index')
        kind=selection.get('kind')
        if kind=='interval':
            fields(selection,common+('robot_id','boot_id','start_robot_ns','end_robot_ns','event_id','note_revision'))
        elif kind=='context':
            fields(selection,common+('catalog_revision','context'))
        else:raise MediaError('invalid_preservation')
        identity(selection['alignment_id']);integer(selection['revision'],1,1000000);digest(selection['sha256'])
        integer(selection['candidate_index'],0,99)
        if kind=='interval':
            identity(selection['robot_id']);identity(selection['boot_id'])
            if int(ns(selection['start_robot_ns']))>int(ns(selection['end_robot_ns'])):raise MediaError('invalid_interval')
            if selection['event_id'] is not None or selection['note_revision'] is not None:
                identity(selection['event_id']);integer(selection['note_revision'],1,1000000)
            if selection['candidate_index']!=0:raise MediaError('invalid_candidate')
        else:
            if type(selection['catalog_revision']) is not str or not 1<=len(selection['catalog_revision'])<=128:
                raise MediaError('invalid_catalog_revision')
            context=selection['context']
            if not isinstance(context,dict):raise MediaError('invalid_context')
            if context.get('kind')=='note':
                fields(context,('kind','event_id','note_revision'))
                identity(context['event_id']);integer(context['note_revision'],1,1000000)
            elif context.get('kind')=='run':
                fields(context,('kind','run_id'));identity(context['run_id'])
            else:raise MediaError('invalid_context')
        if len(encoded(payload))>32768:raise MediaError('invalid_request_size',413)
        return hashlib.sha256(encoded(payload)).hexdigest()

    def submit(self,payload):
        request_hash=self._validate(payload)
        if self.config is None:raise MediaError('media_tools_disabled',503)
        with self.writer_lock:
            with self.lock:old=self.jobs.get(payload['request_id'])
            if old:
                if old['request_sha256']!=request_hash:raise MediaError('preservation_conflict',409)
                return dict(self._receipt(old),idempotent=True)
            with self.lock:
                if len(self.jobs)>=self.config.maximum_jobs:raise MediaError('media_queue_full',503)
            self._publish('request-'+payload['request_id']+'.json',encoded(payload),immutable=True)
            job=dict(request_id=payload['request_id'],request_sha256=request_hash,state='queued',
                     error_code=None,items=[],payload=copy.deepcopy(payload))
            with self.lock:self.jobs[job['request_id']]=job
            return dict(self._receipt(job),idempotent=False)

    def _receipt(self,job):
        return dict(schema_version=1,**{key:copy.deepcopy(job[key]) for key in
                    ('request_id','request_sha256','state','error_code','items')})

    def get(self,request_id):
        identity(request_id)
        with self.lock:
            if request_id not in self.jobs:raise MediaError('preservation_not_found',404)
            return self._receipt(self.jobs[request_id])

    def page(self,limit=20,cursor=None):
        integer(limit,1,100)
        if cursor is not None:identity(cursor)
        with self.lock:
            keys=sorted(k for k in self.jobs if cursor is None or k>cursor);selected=keys[:limit]
            return dict(schema_version=1,items=[self._receipt(self.jobs[k]) for k in selected],
                        next_cursor=selected[-1] if len(keys)>limit else None)

    def _state(self,job,state,error=None,items=None):
        with self.lock:
            job.update(state=state,error_code=error)
            if items is not None:job['items']=items

    def _resolve(self,payload):
        from .notebook import Notebook
        backend=VideoInvestigation(self.service,Notebook(self.service.root/'catalog.sqlite3'))
        selection=copy.deepcopy(payload['selection']);kind=selection.pop('kind');index=selection.pop('candidate_index')
        if kind=='context':
            evaluation=backend.associate(selection);candidates=evaluation['candidates']
        else:
            evaluation=backend.map(selection)
            candidates=[dict(clock_piece=None,start_robot_ns=selection['start_robot_ns'],
                end_robot_ns=selection['end_robot_ns'],clock_uncertainty_utc_ns=None,
                clock_coverage='explicit_interval',result=evaluation['result'])]
        if index>=len(candidates):raise MediaError('preservation_candidate_unavailable',409)
        candidate=candidates[index];groups=[];seen=set()
        for span in candidate['result'].get('spans',[]):
            current=[]
            for frame in span['actual_frames']:
                marker=(span['segment_id'],frame['frame_index'])
                if marker in seen:continue
                seen.add(marker)
                if frame['duration_ns'] is None:raise MediaError('media_unknown_duration',409)
                if current and (frame['frame_index']!=current[-1]['frame_index']+1 or
                        int(frame['pts_ns'])!=int(current[-1]['pts_ns'])+int(current[-1]['duration_ns'])):
                    groups.append((span,current));current=[]
                current.append(frame)
            if current:groups.append((span,current))
        if not groups:raise MediaError('preservation_no_frames',409)
        if len(groups)>100 or len(seen)>10000:raise MediaError('preservation_too_large',413)
        segments={s.segment_id:s for s in backend._segments(list(dict.fromkeys(s['segment_id'] for s,_ in groups)))}
        mapping,_=backend._load(selection['alignment_id'],selection['revision'],selection['sha256'])
        return backend,evaluation,candidate,groups,segments,mapping

    def _quota(self,additional=0):
        total=0;seen=set();count=0
        with os.scandir(self.folder) as entries:
            for entry in entries:
                count+=1
                if count>self.config.maximum_jobs*202+1000:raise MediaError('media_disk_limit',503)
                path=Path(entry.path)
                if path.is_symlink():raise MediaError('media_evidence_conflict',409)
                if path==self.reserved_working:continue
                if path.is_file():
                    stat=path.stat();key=(stat.st_dev,stat.st_ino)
                    if key not in seen:total+=stat.st_size;seen.add(key)
        if (total+self.reservation+additional>self.config.maximum_total_bytes or
                shutil.disk_usage(self.folder).free<self.config.minimum_free_bytes+self.reservation+additional):
            raise MediaError('media_disk_limit',503)

    def _log_references(self,backend,selection,evaluation,alignment,*,projection_version=2):
        """Retain catalog/import identities without exposing archive filenames."""
        ids=[];basis='unavailable'
        if selection['kind']=='context':
            from .video_context import load_context
            catalog,context=load_context(backend.notebook,selection['catalog_revision'],selection['context'])
            if context['kind']=='run':
                ids=context['run']['segment_ids'];basis='saved_run_import_job_references'
            else:
                basis='conditional_same_robot_boot_catalog_references'
                for run in catalog['runs']:
                    if run['known_boot'] and (run['robot_id'],run['boot_id'])==(alignment['robot_id'],alignment['boot_id']):
                        ids.extend(run['segment_ids'])
        ids=list(dict.fromkeys(ids))
        if len(ids)>100:raise MediaError('media_log_references_too_large',413)
        items=[]
        with backend.notebook._connection() as db:
            for job_id in ids:
                row=db.execute('SELECT j.id,j.state,j.artifact_sha256,a.size_bytes,a.source_type,a.format_state '
                    'FROM import_jobs j JOIN import_artifacts a ON a.sha256=j.artifact_sha256 WHERE j.id=?',(job_id,)).fetchone()
                item=dict(import_job_id=job_id,state='unavailable')
                if row is not None:
                    digest(row['artifact_sha256']);integer(row['size_bytes'],0,1<<63)
                    source=row['source_type']
                    labels=('SYNTHETIC','MANUAL','VERIFIED_TRANSFER','TRANSFER_CHECKSUM_MISMATCH') if projection_version==1 else (
                        'SYNTHETIC','MANUAL_LOCAL','VERIFIED_TRANSFER','TRANSFER_CHECKSUM_MISMATCH')
                    item.update(state='catalog_reference',sha256=row['artifact_sha256'],size_bytes=row['size_bytes'],
                        source_type=source if source in labels else 'other',
                        format_valid=row['format_state']=='valid',original_bytes_reverified_for_export=False)
                items.append(item)
        result=dict(basis=basis,items=items,download_available=False,
                    qualification='catalog_references_only' if items else 'unavailable')
        if projection_version==2:result['projection_version']=2
        return result

    def process(self,job,stop):
        try:return self._process(job,stop)
        finally:
            with self.service.settings_lock:
                self.reservation=0;self.reserved_working=None

    def _process(self,job,stop):
        self._state(job,'verifying')
        backend,evaluation,candidate,groups,segments,mapping=self._resolve(job['payload'])
        alignment=mapping.document()
        log_references=self._log_references(backend,job['payload']['selection'],evaluation,alignment)
        items=[]
        for group_index,(span,frames) in enumerate(groups):
            if stop.is_set():raise MediaError('media_interrupted',503)
            segment=segments[span['segment_id']]
            if segment.sha256!=span['source_sha256'] or segment.manifest_sha256!=span['manifest_sha256']:
                raise MediaError('media_evidence_conflict',409)
            frame_map=[dict(source_frame_index=f['frame_index'],source_pts_ns=f['pts_ns'],
                duration_ns=f['duration_ns'],derived_pts_ns=str(int(f['pts_ns'])-int(frames[0]['pts_ns']))) for f in frames]
            item_id=hashlib.sha256(encoded([job['request_sha256'],group_index,span['segment_id'],frame_map])).hexdigest()
            output=self._path(item_id+'.mp4');sidepath=self._path(item_id+'.json')
            reference=None
            for window in alignment['windows']:
                for anchor in window['anchors']:
                    for frame in frame_map:
                        if anchor['segment_id']==segment.segment_id and anchor['frame_index']==frame['source_frame_index']:
                            reference=dict(robot_ns=anchor['robot_ns'],source_pts_ns=frame['source_pts_ns'],derived_pts_ns=frame['derived_pts_ns'],
                                robot_uncertainty_ns=anchor['robot_uncertainty_ns'],video_uncertainty_ns=anchor['video_uncertainty_ns'])
            if reference is not None:reference.update(basis='operator_anchor')
            else:
                for fit in mapping._fits:
                    window,_,scale,origin,pts_origin,*_=fit
                    if segment.segment_id not in window.segment_ids:continue
                    value=origin+(Fraction(int(frame_map[0]['source_pts_ns']))-pts_origin)/scale
                    if not window.start_robot_ns<=value<=window.end_robot_ns:continue
                    robot=value.numerator//value.denominator
                    # Conditional inverse bound over the whole calibrated domain:
                    # convex forward allowance attains its maximum at an endpoint.
                    error=max(mapping._predict(fit,window.start_robot_ns)[1],
                              mapping._predict(fit,window.end_robot_ns)[1])
                    allowance=error/scale+1
                    uncertainty=-(-allowance.numerator//allowance.denominator)
                    reference=dict(robot_ns=str(robot),source_pts_ns=frame_map[0]['source_pts_ns'],derived_pts_ns='0',
                        basis='estimated_affine_frame_cue',robot_uncertainty_ns=str(uncertainty))
                    break
            sidecar=dict(schema_version=1,item_id=item_id,request_sha256=job['request_sha256'],
                selection=job['payload']['selection'],evaluation=evaluation,candidate=candidate,
                candidate_index=job['payload']['selection']['candidate_index'],group_index=group_index,
                segment_id=segment.segment_id,source_sha256=segment.sha256,manifest_sha256=segment.manifest_sha256,
                camera_id=segment.camera_id,session_id=segment.session_id,capture_id=segment.capture_id,
                source_type=segment.source_type,frames=frame_map,
                original_logs=log_references,
                split_semantics='one_source_contiguous_actual_frame_exposures',
                qualification='manual_unqualified',measured_camera_alignment=False,
                verified_frames=frame_map,
                tools=dict(version=self.config.version,ffmpeg_sha256=self.config.ffmpeg_sha256,ffprobe_sha256=self.config.ffprobe_sha256) if self.config else None,
                manual_advantagescope=dict(automatic_offset_supported=False,reference=reference,
                    qualification='manual_unqualified',instructions=[
                    'Open the corresponding original robot log without merged-log timestamp offsets.',
                    'Find the recorded robot cue and verify the corresponding cue at the derived video timestamp.',
                    'Lock the video manually in AdvantageScope; its cached frame numbers and nominal FPS are not original PTS identity.',
                    'Verify visible cue alignment; these tools do not measure camera accuracy or automatically set an offset.']))
            if len(encoded(sidecar))>4194304:raise MediaError('media_sidecar_too_large',413)
            if output.exists() or sidepath.exists():
                if not output.exists() or not sidepath.exists():raise MediaError('media_incomplete_output',409)
                if sidepath.stat().st_size>4194304:raise MediaError('media_sidecar_too_large',413)
                with sidepath.open('rb') as stream:
                    raw=stream.read(4194305)
                if len(raw)>4194304:raise MediaError('media_sidecar_too_large',413)
                old=strict_json(raw)
                old_logs=old.get('original_logs')
                if not isinstance(old_logs,dict):raise MediaError('media_evidence_conflict',409)
                if 'projection_version' not in old_logs:
                    sidecar['original_logs']=self._log_references(backend,job['payload']['selection'],evaluation,alignment,
                        projection_version=1)
                elif type(old_logs['projection_version']) is not int or old_logs['projection_version']!=2:
                    raise MediaError('media_evidence_conflict',409)
                tools=old.get('tools')
                fields(tools,('version','ffmpeg_sha256','ffprobe_sha256'))
                digest(tools['ffmpeg_sha256']);digest(tools['ffprobe_sha256'])
                if type(tools['version']) is not str or not 1<=len(tools['version'])<=128 or any(c.isspace() for c in tools['version']):
                    raise MediaError('media_evidence_conflict',409)
                integer(old.get('size_bytes'),1,8589934592);digest(old.get('derivative_sha256'))
                if self.config is None:sidecar['tools']=tools
                expected=dict(sidecar,derivative_sha256=old.get('derivative_sha256'),size_bytes=old.get('size_bytes'))
                if old!=expected or sha(output)!=old['derivative_sha256'] or output.stat().st_size!=old['size_bytes']:
                    raise MediaError('media_evidence_conflict',409)
                if self.adapter is not None:verify_frames(self.adapter.probe(output,stop),frame_map)
                sidecar=old
            else:
                if self.config is None:raise MediaError('media_tools_disabled',503)
                self.folder.mkdir(parents=True,exist_ok=True);self._quota()
                working=self._path('.working-'+uuid.uuid4().hex+'.mp4')
                with self.service.settings_lock:
                    budget=self.config.maximum_output_bytes+len(encoded(sidecar))+1048576
                    self._quota(additional=budget)
                    self.reservation=budget;self.reserved_working=working
                self._state(job,'rendering')
                self.adapter.render(Path(segment.folder)/segment.relative_path,frames[0]['frame_index'],frames[-1]['frame_index'],working,stop)
                if not working.is_file() or working.stat().st_size>self.config.maximum_output_bytes:
                    raise MediaError('media_output_too_large',413)
                verify_frames(self.adapter.probe(working,stop),frame_map)
                # Reopen all pinned evidence after encoding, before publication.
                self._resolve(job['payload'])
                sidecar.update(derivative_sha256=sha(working),size_bytes=working.stat().st_size)
                with working.open('r+b') as stream:os.fsync(stream.fileno())
                with self.service.settings_lock:
                    if stop.is_set() or self.service.closed:raise MediaError('service_stopping',503)
                    self._quota();os.link(working,output)
                self._publish(item_id+'.json',encoded(sidecar),immutable=True)
                working.unlink()
            item=dict(item_id=item_id,sha256=sidecar['derivative_sha256'],sidecar_sha256=sha(sidepath),
                size_bytes=sidecar['size_bytes'],frame_count=len(frame_map),candidate_index=sidecar['candidate_index'],
                segment_id=segment.segment_id,group_index=group_index)
            items.append(item)
            with self.service.settings_lock:
                self.reservation=0;self.reserved_working=None
        self._resolve(job['payload'])
        self._publish('state-'+job['request_id']+'.json',encoded(dict(self._receipt(job),state='ready',error_code=None,items=items)))
        with self.lock:
            for item in items:self.items[item['item_id']]=item
            self._state(job,'ready',items=items)

    def _terminal(self,job,state,error=None):
        self._state(job,state,error)
        self._publish('state-'+job['request_id']+'.json',encoded(self._receipt(job)),terminal=True)

    def run(self,stop):
        try:
            if self.adapter is not None:self.adapter.validate(stop)
            if not self.folder.exists() and self.config is None:return
            self.folder.mkdir(parents=True,exist_ok=True)
            names=[];count=0;maximum=self.config.maximum_jobs if self.config else 1000
            with os.scandir(self.folder) as entries:
                for entry in entries:
                    count+=1
                    if count>maximum*202+1000:raise MediaError('media_disk_limit',503)
                    path=Path(entry.path)
                    if path.name.startswith('request-') and path.name.endswith('.json'):names.append(path)
                    if len(names)>maximum:raise MediaError('media_queue_full',503)
            names.sort()
            if len(names)>(self.config.maximum_jobs if self.config else 1000):raise MediaError('media_queue_full',503)
            for path in names:
                if path.is_symlink() or path.stat().st_size>32768:raise MediaError('media_evidence_conflict',409)
                payload=strict_json(path.read_bytes());request_hash=self._validate(payload)
                if path.name!='request-'+payload['request_id']+'.json':raise MediaError('media_evidence_conflict',409)
                with self.lock:
                    self.jobs.setdefault(payload['request_id'],dict(request_id=payload['request_id'],request_sha256=request_hash,
                        state='queued',error_code=None,items=[],payload=payload))
                statepath=self._path('state-'+payload['request_id']+'.json')
                if statepath.exists():
                    if statepath.stat().st_size>1048576:raise MediaError('media_evidence_conflict',409)
                    saved=strict_json(statepath.read_bytes())
                    if saved.get('request_sha256')!=request_hash:raise MediaError('media_evidence_conflict',409)
                    if saved.get('state') in ('failed','interrupted','verifying','rendering'):
                        self._state(self.jobs[payload['request_id']],saved['state'],'media_previous_attempt_incomplete')
                        if saved['state'] in ('verifying','rendering'):
                            self._state(self.jobs[payload['request_id']],'interrupted','media_previous_attempt_incomplete')
                elif self.config is None:
                    self._state(self.jobs[payload['request_id']],'unavailable','media_tools_disabled')
            with self.lock:self.tools.update(state='ready' if self.config else 'disabled')
            while not stop.is_set():
                if self.service.video.snapshot()['recovery']['state']=='pending':stop.wait(.05);continue
                with self.lock:job=next((j for j in self.jobs.values() if j['state']=='queued'),None)
                if job is None:stop.wait(.05);continue
                try:
                    if self.config is not None:self._publish('state-'+job['request_id']+'.json',encoded(dict(self._receipt(job),state='verifying')))
                    self.process(job,stop)
                except (InvestigationError,OSError,ValueError,TypeError,KeyError,OverflowError) as exc:
                    self._terminal(job,'interrupted' if stop.is_set() else 'failed',
                        exc.code if isinstance(exc,InvestigationError) else 'media_operation_failed')
                    if stop.is_set():break
        except (InvestigationError,OSError,ValueError,TypeError,KeyError,OverflowError):
            with self.lock:self.tools.update(state='failed',error_code='media_tools_unavailable')
        finally:
            with self.lock:
                if self.tools['state'] not in ('failed','disabled'):self.tools.update(state='stopped')

    def open_item(self,item_id,sidecar=False):
        if self.service.stop.is_set() or self.service.closed:raise MediaError('service_stopping',503)
        digest(item_id)
        with self.lock:item=copy.deepcopy(self.items.get(item_id))
        if item is None:raise MediaError('media_not_ready',404)
        path=self._path(item_id+('.json' if sidecar else '.mp4'))
        expected=item['sidecar_sha256'] if sidecar else item['sha256']
        stream=path.open('rb')
        try:
            before=os.fstat(stream.fileno());bound=4194304 if sidecar else item['size_bytes']
            if before.st_size>bound or (not sidecar and before.st_size!=bound):raise MediaError('media_evidence_conflict',409)
            value=hashlib.sha256()
            while block:=stream.read(65536):value.update(block)
            after=os.fstat(stream.fileno());current=path.stat()
            if (value.hexdigest()!=expected or path.is_symlink() or
                    (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino) or
                    current.st_ino!=after.st_ino):raise MediaError('media_evidence_conflict',409)
            stream.seek(0);return stream,before.st_size
        except BaseException:
            stream.close();raise
