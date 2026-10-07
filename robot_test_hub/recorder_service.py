"""Independent opt-in recording worker and bounded redacted archive projections.

Private camera configuration is never persisted in ordinary hub settings or
exported through these APIs. Unfinished originals are preserved, never resumed
or silently promoted to verified footage.
"""
from __future__ import annotations
import base64
import copy
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

from .recorder import FFmpegConfig, Recorder, RecordingError, _hash, _id, _safe, _publish

MAX_CONFIG_BYTES=32768
MAX_METADATA_BYTES=16*1024*1024
MAX_ARCHIVE_ENTRIES=20000
MAX_SEGMENTS=10000
SUMMARY_KEYS=('segment_id','camera_id','session_id','capture_id','source_type','state',
              'sha256','size_bytes','container','codec','time_base','start_pts_ns',
              'end_pts_ns','utc_basis','utc_uncertainty_ns')


def _signed_ns(value):
    if not isinstance(value,str) or not re.fullmatch('(?:0|[1-9][0-9]{0,18}|-[1-9][0-9]{0,18})',value):
        raise RecordingError('Canonical video nanoseconds required')
    result=int(value)
    if not -(1<<63)<=result<(1<<63):
        raise RecordingError('Video nanoseconds outside signed64 range')
    return result


def load_video_config(path):
    """Explicit private config; errors intentionally omit path/input/content."""
    try:
        with Path(path).open('rb') as stream:
            encoded=stream.read(MAX_CONFIG_BYTES+1)
        if len(encoded)>MAX_CONFIG_BYTES:
            raise RecordingError('Video configuration size limit exceeded')
        value=json.loads(encoded)
        if (not isinstance(value,dict) or type(value.get('schema_version')) is not int
                or value.pop('schema_version')!=1
                or value.keys()-FFmpegConfig.__dataclass_fields__.keys()):
            raise RecordingError('Video configuration schema invalid')
        return FFmpegConfig(**value)
    except (OSError,TypeError,ValueError,OverflowError,RecursionError):
        raise RecordingError('Invalid private video configuration; check schema, explicit input and bounds') from None


class RecorderWorker:
    def __init__(self,hub_root,config=None,*,recorder_factory=Recorder,publish=lambda health:None,
                 tick_interval=.25,clock=time.monotonic):
        if config is not None and not isinstance(config,FFmpegConfig):
            raise RecordingError('Explicit validated video configuration required')
        if type(tick_interval) not in (int,float) or not math.isfinite(tick_interval) or not 0<tick_interval<=60:
            raise RecordingError('Video tick interval must be finite positive seconds')
        self.hub_root=Path(hub_root).resolve()
        self.config=config
        self.recorder_factory,self.publish=recorder_factory,publish
        self.tick_interval=tick_interval
        self.clock=clock
        self._health_observed_at=self.clock()
        self.lock=threading.RLock()
        self.recorder=None
        self._active_processed=0
        self.records={}
        self.capture_states={}
        self.recovery={'state':'pending','verified_segments':0,'unfinished_captures':0,
                       'unverified_files':0,'errors':0}
        self.health={'schema_version':1,'enabled':config is not None,
            'state':'starting' if config else 'disabled','error_code':None,
            'camera_id':config.camera_id if config else None,
            'source_type':config.source_type if config else None,'capture_id':None,
            'session_id':None,'frames':None,'dropped_frames':None,'last_frame_age_seconds':None,
            'closed_segments':0,'active_closed_segments':0,'live_camera_qualified':False,
            'backup_state':'not_backed_up','utc_alignment':'requires_camera_specific_mapping'}

    def _metadata(self,path):
        with path.open('rb') as stream:
            encoded=stream.read(MAX_METADATA_BYTES+1)
        if len(encoded)>MAX_METADATA_BYTES:
            raise RecordingError('Video metadata size limit exceeded')
        value=json.loads(encoded)
        if not isinstance(value,dict):
            raise RecordingError('Video metadata object required')
        return value

    def _summary(self,segment):
        for key in ('segment_id','camera_id','session_id','capture_id'):
            _id(segment[key])
        if (type(segment.get('schema_version')) is not int or segment['schema_version']!=1
                or segment.get('state')!='closed_verified'
                or segment.get('source_type') not in ('REAL','SYNTHETIC')
                or not isinstance(segment.get('sha256'),str)
                or not re.fullmatch('[a-f0-9]{64}',segment['sha256'])
                or type(segment.get('size_bytes')) is not int or segment['size_bytes']<1
                or not isinstance(segment.get('frames'),list) or not segment['frames']
                or len(segment['frames'])>1000000):
            raise RecordingError('Video segment evidence invalid')
        previous=None
        previous_end=None
        for frame in segment['frames']:
            current=_signed_ns(frame['pts_ns'])
            duration=_signed_ns(frame['duration_ns'])
            if duration<=0 or current+duration>=(1<<63):
                raise RecordingError('Known positive video frame duration required')
            if previous is not None and current<=previous:
                raise RecordingError('Video frame timestamps not advancing')
            if previous_end is not None and current<previous_end:
                raise RecordingError('Video frame durations overlap')
            previous=current
            previous_end=current+duration
        start,end=_signed_ns(segment['start_pts_ns']),_signed_ns(segment['end_pts_ns'])
        if (segment['frames'][0]['pts_ns']!=segment['start_pts_ns'] or end<=start
                or end!=_signed_ns(segment['frames'][-1]['pts_ns'])+_signed_ns(segment['frames'][-1]['duration_ns'])):
            raise RecordingError('Video segment interval invalid')
        result={key:segment[key] for key in SUMMARY_KEYS}
        # Project the actual recorder contract, never arbitrary archive strings
        # that could expose a filesystem/device path through a metadata field.
        if (result['container']!='matroska' or result['utc_basis']!='unmapped_recording_pts'
                or not isinstance(result['codec'],str)
                or not re.fullmatch('[A-Za-z0-9_.-]{1,128}',result['codec'])
                or not isinstance(result['time_base'],str)
                or not re.fullmatch('[1-9][0-9]{0,18}/[1-9][0-9]{0,18}',result['time_base'])
                or any(int(part)>=(1<<63) for part in result['time_base'].split('/'))):
            raise RecordingError('Video segment format metadata invalid')
        if result['utc_uncertainty_ns'] is not None:
            if _signed_ns(result['utc_uncertainty_ns'])<0:
                raise RecordingError('Video uncertainty invalid')
        result.update(frame_count=len(segment['frames']),backup_state='not_backed_up')
        return result

    def recover(self):
        """Verify immutable closed manifests; retain unmanifested video as unknown."""
        root=_safe(self.hub_root,'video')
        recovered={}
        recovery={'state':'complete','verified_segments':0,'unfinished_captures':0,
                  'unverified_files':0,'errors':0}
        visited=0
        if root.exists():
            def entries(folder):
                nonlocal visited
                for path in folder.iterdir():
                    visited+=1
                    if visited>MAX_ARCHIVE_ENTRIES:
                        raise RecordingError('Video archive discovery limit exceeded')
                    relative=path.relative_to(root).as_posix()
                    yield _safe(root,relative)
            try:
                for camera in entries(root):
                    if not camera.is_dir():
                        continue
                    _id(camera.name)
                    for session in entries(camera):
                        if not session.is_dir():
                            continue
                        _id(session.name)
                        for capture in entries(session):
                            if not capture.is_dir():
                                continue
                            _id(capture.name)
                            unfinished=False
                            capture_state='historical_lifecycle_unknown'
                            for path in entries(capture):
                                if re.fullmatch('segment-[0-9]{6}\\.mkv',path.name):
                                    if not path.with_name(path.name+'.json').exists():
                                        recovery['unverified_files']+=1
                                        unfinished=True
                                    continue
                                if not re.fullmatch('segment-[0-9]{6}\\.mkv\\.json',path.name):
                                    continue
                                try:
                                    segment=self._metadata(path)
                                    summary=self._summary(segment)
                                    name=path.name[:-5]
                                    if (segment['relative_path']!=name or summary['camera_id']!=camera.name
                                            or summary['session_id']!=session.name or summary['capture_id']!=capture.name):
                                        raise RecordingError('Video archive identity conflict')
                                    source=_safe(capture,name)
                                    if source.stat().st_size!=summary['size_bytes'] or _hash(source)!=summary['sha256']:
                                        raise RecordingError('Video original integrity conflict')
                                    if summary['segment_id'] in recovered or len(recovered)>=MAX_SEGMENTS:
                                        raise RecordingError('Video archive identity/count conflict')
                                    recovered[summary['segment_id']]=summary
                                except (OSError,ValueError,KeyError,TypeError,OverflowError):
                                    recovery['errors']+=1
                                    unfinished=True
                            if unfinished:
                                recovery['unfinished_captures']+=1
                                capture_state='unfinished_preserved'
                            else:
                                receipt=_safe(capture,'capture-lifecycle.json')
                                if receipt.exists():
                                    try:
                                        state=self._metadata(receipt)
                                        if (type(state.get('schema_version')) is not int or state['schema_version']!=1
                                                or state.get('capture_id')!=capture.name or state.get('session_id')!=session.name
                                                or state.get('camera_id')!=camera.name
                                                or state.get('state') not in ('stopped','failed')
                                                or type(state.get('finalization_complete')) is not bool):
                                            raise RecordingError('Video lifecycle receipt invalid')
                                        capture_state='stopped' if state['finalization_complete'] and state['state']=='stopped' else 'failed_preserved'
                                    except (OSError,ValueError,KeyError,TypeError):
                                        recovery['errors']+=1
                            self.capture_states[capture.name]=capture_state
                            if capture_state in ('historical_lifecycle_unknown','failed_preserved'):
                                recovery['unfinished_captures']+=1
            except (OSError,ValueError,KeyError,TypeError,OverflowError):
                recovery['errors']+=1
                recovery['state']='partial'
        recovery['verified_segments']=len(recovered)
        if recovery['errors'] or recovery['unverified_files'] or recovery['unfinished_captures']:
            recovery['state']='partial'
        with self.lock:
            self.records=recovered
            self.recovery=recovery
        self._publish()

    def _publish(self,health=None):
        with self.lock:
            if health:
                self._health_observed_at=self.clock()
                for key in ('state','error_code','frames','dropped_frames','last_frame_age_seconds'):
                    self.health[key]=health.get(key)
                self.health['active_closed_segments']=health.get('closed_segments',0)
            if self.recorder and self.recorder.folder is not None:
                self.health['capture_id']=self.recorder.capture
                self.health['session_id']=self.recorder.session_id
                self.capture_states[self.recorder.capture]=self.health['state']
            self.health['closed_segments']=len(self.records)
            value=self.snapshot()
        self.publish(value)

    def _update_segments(self):
        # The recorder is owned by this worker thread. Project each newly
        # finalized segment once, avoiding repeated copies of historical frames.
        for segment in self.recorder.segments[self._active_processed:]:
            summary=self._summary(segment)
            with self.lock:
                old=self.records.get(summary['segment_id'])
                if old is not None and old!=summary:
                    raise RecordingError('Immutable video summary conflict')
                if old is None and len(self.records)>=MAX_SEGMENTS:
                    raise RecordingError('Video segment catalog limit exceeded')
                self.records[summary['segment_id']]=summary
                self._active_processed+=1

    def run(self,stopping):
        try:
            self.recover()
            if self.config is None or stopping.is_set():
                return
            root=_safe(self.hub_root,'video')
            self.recorder=self.recorder_factory(root,self.config)
            self._publish(self.recorder.start(uuid.uuid4().hex))
            while not stopping.is_set():
                health=self.recorder.tick()
                self._update_segments()
                self._publish(health)
                if health['state']=='error':
                    return
                stopping.wait(self.tick_interval)
        except Exception:
            with self.lock:
                self.health.update(state='error',error_code='video_worker_failed')
            self._publish()
        finally:
            if self.recorder is not None:
                while True:
                    try:
                        health=self.recorder.stop()
                        self._update_segments()
                        if self.health['state']=='error' and health['state']=='stopped':
                            health={**health,'state':'error','error_code':self.health['error_code']}
                        self._publish(health)
                        if not self.recorder.process_started:
                            if self.recorder.folder is not None:
                                _publish(_safe(self.recorder.folder,'capture-lifecycle.json'),{
                                    'schema_version':1,'camera_id':self.config.camera_id,
                                    'capture_id':self.recorder.capture,'session_id':self.recorder.session_id,
                                    'state':'stopped' if health['state']=='stopped' else 'failed',
                                    'finalization_complete':health['state']=='stopped' and health['error_code'] is None})
                            break
                    except Exception:
                        with self.lock:
                            self.health.update(state='error',error_code='video_shutdown_failed')
                        self._publish()
                        if not self.recorder.process_started:
                            break
                    # Stop is already requested; keep cleanup bounded per call
                    # and retain the hub owner until the child is actually gone.
                    threading.Event().wait(.1)

    def snapshot(self):
        with self.lock:
            value=copy.deepcopy({**self.health,'recovery':self.recovery})
            elapsed=max(0.,self.clock()-self._health_observed_at)
            value['health_age_seconds']=elapsed
            if value['last_frame_age_seconds'] is not None:
                value['last_frame_age_seconds']+=elapsed
            return value

    def segment_page(self,*,limit=20,cursor=None):
        if type(limit) is not int or not 1<=limit<=100:
            raise RecordingError('Invalid video page limit')
        after=None
        if cursor is not None:
            try:
                if not isinstance(cursor,str) or len(cursor)>256:
                    raise ValueError()
                after=base64.b64decode(cursor,altchars=b'-_',validate=True).decode('ascii')
                _id(after)
            except (ValueError,UnicodeError):
                raise RecordingError('Invalid video cursor') from None
        with self.lock:
            identities=sorted(self.records)
            if after is not None:
                identities=[identity for identity in identities if identity>after]
            selected=identities[:limit]
            items=[copy.deepcopy(self.records[identity]) for identity in selected]
            for item in items:
                item['capture_state']=self.capture_states.get(item['capture_id'],'historical_lifecycle_unknown')
            next_cursor=base64.urlsafe_b64encode(selected[-1].encode()).decode() if len(identities)>limit else None
            return {'schema_version':1,'items':items,'next_cursor':next_cursor,
                    'recovery':copy.deepcopy(self.recovery),'backup_state':'not_backed_up'}
