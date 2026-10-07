"""Explicit manual video investigation over verified local recording evidence."""
import base64
from bisect import bisect_left, bisect_right
import copy
import hashlib
import json
from pathlib import Path
import re
import threading

from .recorder import _safe
from .recorder_service import MAX_METADATA_BYTES, SUMMARY_KEYS
from .video_alignment import (AlignmentError, AlignmentRevisions, CalibrationWindow,
                              ManualAlignment, ManualAnchor, VideoSegment, _ceil, _floor)

MAX_BODY = 32768
MAX_DOCUMENT = 1024 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_TOTAL_METADATA = 64 * 1024 * 1024
MAX_REVISIONS = 10000
MAX_SELECTED_FRAMES = 10000
IDENTITIES = ('robot_id', 'boot_id', 'camera_id', 'session_id', 'capture_id')


class InvestigationError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def identity(value):
    if type(value) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', value):
        raise InvestigationError('invalid_identity')
    return value


def digest(value):
    if type(value) is not str or not re.fullmatch('[a-f0-9]{64}', value):
        raise InvestigationError('invalid_digest')
    return value


def ns(value):
    if (type(value) is not str or not re.fullmatch('0|[1-9][0-9]{0,18}', value)
            or int(value) >= 1 << 63):
        raise InvestigationError('invalid_nanoseconds')
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise InvestigationError('invalid_bounds')
    return value


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise InvestigationError('invalid_fields')


def identities(value, maximum=100):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise InvestigationError('invalid_segment_count')
    result = [identity(item) for item in value]
    if len(set(result)) != len(result):
        raise InvestigationError('duplicate_segment')
    return result


def strict_json(encoded):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvestigationError('duplicate_json_field')
            result[key] = value
        return result
    def constant(_):
        raise InvestigationError('invalid_json')
    try:
        return json.loads(encoded, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvestigationError('invalid_json') from None


def _read(path, bound):
    with path.open('rb') as stream:
        data = stream.read(bound + 1)
    if len(data) > bound:
        raise InvestigationError('evidence_too_large', 409)
    return data


def _public(value):
    if isinstance(value,dict):
        return {key:_public(item) for key,item in value.items() if key not in ('relative_path','path','folder')}
    if isinstance(value,list):
        return [_public(item) for item in value]
    return value


def _response(value):
    result=_public(value)
    if len(json.dumps(result,allow_nan=False).encode())>MAX_RESPONSE:
        raise InvestigationError('oversized_response',413)
    return result


class VideoInvestigation:
    def __init__(self, service, notebook):
        self.service, self.notebook = service, notebook
        self.folder = Path(service.root) / 'video-alignments'
        self.writer_lock = threading.Lock()

    def _revision_path(self, alignment_id, revision):
        identity(alignment_id);integer(revision, 1, 1000000)
        return _safe(self.folder, f'{alignment_id}-{revision:06d}.json')

    def _revision(self, alignment_id, revision, sha256=None):
        path = self._revision_path(alignment_id, revision)
        try:
            encoded = _read(path, MAX_DOCUMENT)
        except FileNotFoundError:
            raise InvestigationError('alignment_not_found', 404) from None
        actual = hashlib.sha256(encoded).hexdigest()
        if sha256 is not None and actual != digest(sha256):
            raise InvestigationError('alignment_digest_conflict', 409)
        document = strict_json(encoded)
        if (not isinstance(document, dict) or document.get('alignment_id') != alignment_id
                or type(document.get('revision')) is not int or document['revision'] != revision
                or type(document.get('schema_version')) is not int or document['schema_version'] != 1
                or document.get('qualification') != 'manual_unqualified'
                or document.get('measured_camera_alignment') is not False):
            raise InvestigationError('alignment_evidence_conflict', 409)
        for key in IDENTITIES:
            identity(document.get(key))
        return document, actual

    def _segments(self, segment_ids):
        wanted = identities(segment_ids)
        # Copy the small identity summaries only. Native IO, hashing and parsing
        # never run under the worker's health/record lock.
        with self.service.video.lock:
            summaries = [copy.deepcopy(self.service.video.records.get(item)) for item in wanted]
        segments = [];metadata_bytes=0
        for segment_id, summary in zip(wanted, summaries):
            if summary is None:
                raise InvestigationError('recording_not_found', 404)
            try:
                camera, session, capture = (identity(summary[key]) for key in ('camera_id', 'session_id', 'capture_id'))
                if summary['segment_id'] != segment_id:
                    raise ValueError()
                match = re.fullmatch(re.escape(capture) + '-([0-9]{6})', segment_id)
                if match is None:
                    raise ValueError()
                name = 'segment-' + match[1] + '.mkv'
                folder = _safe(Path(self.service.root), f'video/{camera}/{session}/{capture}')
                manifest_path = _safe(folder, name + '.json')
                encoded=_read(manifest_path,MAX_METADATA_BYTES)
                metadata_bytes+=len(encoded)
                if metadata_bytes>MAX_TOTAL_METADATA:
                    raise InvestigationError('recording_evidence_too_large',409)
                manifest = strict_json(encoded)
                if not isinstance(manifest,dict):raise ValueError()
                if (manifest.get('relative_path') != name
                        or any(manifest.get(key) != summary.get(key) for key in SUMMARY_KEYS)
                        or len(manifest.get('frames', [])) != summary.get('frame_count')):
                    raise ValueError()
                if not _safe(folder, name).exists():
                    raise InvestigationError('recording_unavailable', 409)
                segments.append(VideoSegment.from_manifest(folder, manifest))
            except InvestigationError:
                raise
            except FileNotFoundError:
                raise InvestigationError('recording_unavailable', 409) from None
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                raise InvestigationError('recording_evidence_conflict', 409) from None
        return segments

    def frames(self, segment_id, *, offset=0, limit=50):
        integer(offset, 0, 1000000);integer(limit, 1, 100)
        segment = self._segments([segment_id])[0]
        if offset > len(segment.frames):
            raise InvestigationError('invalid_bounds')
        stop = min(len(segment.frames), offset + limit)
        return _response(dict(schema_version=1, segment_id=segment.segment_id, camera_id=segment.camera_id,
            session_id=segment.session_id, capture_id=segment.capture_id,
            source_sha256=segment.sha256, manifest_sha256=segment.manifest_sha256, source_type=segment.source_type,
            frames=[dict(frame_index=index, pts_ns=str(segment.frames[index][0]),
                duration_ns=str(segment.frames[index][1]) if segment.frames[index][1] is not None else None)
                for index in range(offset, stop)], total=len(segment.frames), next_offset=stop if stop<len(segment.frames) else None))

    def list(self, *, limit=20, cursor=None):
        integer(limit, 1, 100)
        after = None
        if cursor is not None:
            if type(cursor) is not str or len(cursor)>512:
                raise InvestigationError('invalid_cursor')
            try:
                after = strict_json(base64.b64decode(cursor, altchars=b'-_', validate=True))
                if not isinstance(after, list) or len(after) != 2:
                    raise ValueError()
                identity(after[0]);integer(after[1], 1, 1000000)
                after = tuple(after)
            except (ValueError, TypeError):
                raise InvestigationError('invalid_cursor') from None
        entries = []
        if self.folder.exists():
            _safe(self.folder, 'checked.json')
            for count, path in enumerate(self.folder.iterdir(), 1):
                if count > MAX_REVISIONS:
                    raise InvestigationError('alignment_catalog_too_large', 409)
                match = re.fullmatch('([A-Za-z0-9_-]{1,128})-([0-9]{6,7})\\.json', path.name)
                if match is not None:
                    revision = int(match[2]);integer(revision, 1, 1000000)
                    if path.name != f'{match[1]}-{revision:06d}.json':
                        raise InvestigationError('alignment_evidence_conflict', 409)
                    entries.append((match[1], revision))
        selected = sorted(item for item in entries if after is None or item>after)[:limit+1]
        items = []
        for alignment_id, revision in selected[:limit]:
            document, sha = self._revision(alignment_id, revision)
            items.append(dict((key, document[key]) for key in
                ('alignment_id', 'revision', *IDENTITIES, 'qualification', 'measured_camera_alignment')) | {'sha256':sha})
        following = base64.urlsafe_b64encode(json.dumps(selected[limit-1]).encode()).decode() if len(selected)>limit else None
        return _response(dict(schema_version=1, items=items, next_cursor=following))

    def create(self, payload):
        fields(payload, ('alignment_id','revision','expected_previous_sha256',*IDENTITIES,'segment_ids','windows'))
        alignment_id = identity(payload['alignment_id']);revision = integer(payload['revision'],1,1000000)
        previous = payload['expected_previous_sha256']
        if previous is not None:digest(previous)
        domain = [identity(payload[key]) for key in IDENTITIES]
        segment_ids = identities(payload['segment_ids'])
        raw_windows = payload['windows']
        if not isinstance(raw_windows, list) or not 1<=len(raw_windows)<=20:
            raise InvestigationError('invalid_window_count')
        windows = [];count=0
        for item in raw_windows:
            fields(item, ('continuity_id','start_robot_ns','end_robot_ns','segment_ids','model_uncertainty_ns',
                          'anchors','assumed_scale','assumed_drift_uncertainty_ppm'))
            selected = identities(item['segment_ids'])
            if not set(selected)<=set(segment_ids):raise InvestigationError('invalid_window_segments')
            raw_anchors = item['anchors']
            if not isinstance(raw_anchors,list) or not raw_anchors:
                raise InvestigationError('invalid_anchor_count')
            count += len(raw_anchors)
            if count>100:raise InvestigationError('invalid_anchor_count')
            anchors=[]
            for anchor in raw_anchors:
                fields(anchor, ('robot_ns','segment_id','source_sha256','frame_index','robot_uncertainty_ns',
                                'video_uncertainty_ns','evidence_label'))
                label = anchor['evidence_label']
                if type(label) is not str or not 1<=len(label)<=256 or any(ord(c)<32 for c in label):
                    raise InvestigationError('invalid_evidence_label')
                anchors.append(ManualAnchor(ns(anchor['robot_ns']),identity(anchor['segment_id']),
                    digest(anchor['source_sha256']),integer(anchor['frame_index'],0,999999),
                    ns(anchor['robot_uncertainty_ns']),ns(anchor['video_uncertainty_ns']),label))
            for key in ('assumed_scale','assumed_drift_uncertainty_ppm'):
                if item[key] is not None and (type(item[key]) is not str or len(item[key])>100):
                    raise InvestigationError('invalid_clock_assumption')
            windows.append(CalibrationWindow(identity(item['continuity_id']),ns(item['start_robot_ns']),
                ns(item['end_robot_ns']),anchors,selected,ns(item['model_uncertainty_ns']),
                item['assumed_scale'],item['assumed_drift_uncertainty_ppm']))
        segments = self._segments(segment_ids)
        try:
            mapping = ManualAlignment(*domain, segments, windows)
        except AlignmentError:
            raise InvestigationError('invalid_alignment') from None
        document=mapping.document()
        stored=dict(document,alignment_id=alignment_id,revision=revision,previous_sha256=previous)
        if len((json.dumps(stored,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode())>MAX_DOCUMENT:
            raise InvestigationError('alignment_document_too_large',413)
        response=_response(dict(schema_version=1,alignment_id=alignment_id,revision=revision,
            sha256='0'*64,document=document))
        with self.writer_lock:
            with self.service.settings_lock:
                if self.service.stop.is_set() or self.service.closed:
                    raise InvestigationError('service_stopping',503)
                try:
                    _safe(self.folder, 'checked.json')
                    result=AlignmentRevisions(self.folder,alignment_id).append(revision,mapping,
                        expected_previous_sha256=previous)
                except AlignmentError:
                    raise InvestigationError('alignment_revision_conflict',409) from None
        response['sha256']=result['sha256']
        return response

    def _load(self, alignment_id, revision, sha256):
        document, actual = self._revision(alignment_id,revision,digest(sha256))
        sources = document.get('sources')
        if not isinstance(sources,list) or not 1<=len(sources)<=100:
            raise InvestigationError('alignment_evidence_conflict',409)
        segments = self._segments([source['segment_id'] for source in sources])
        try:
            mapping = AlignmentRevisions(self.folder,alignment_id).load(revision,segments,expected_sha256=actual)
        except AlignmentError:
            raise InvestigationError('alignment_evidence_conflict',409) from None
        return mapping, actual

    def get(self, alignment_id, revision, sha256):
        mapping, actual = self._load(alignment_id,revision,sha256)
        return _response(dict(schema_version=1,alignment_id=alignment_id,revision=revision,sha256=actual,document=mapping.document()))

    def _selection_bound(self, mapping, robot_id, boot_id, start, end):
        document=mapping.document()
        if (robot_id,boot_id)!=(document['robot_id'],document['boot_id']):return 0
        selected=0
        for fit in mapping._fits:
            window=fit[0]
            a,b=max(start,window.start_robot_ns),min(end,window.end_robot_ns)
            if a>b:continue
            low,low_error=mapping._predict(fit,a);high,high_error=mapping._predict(fit,b)
            first=min(_floor(low-low_error),_floor(high-high_error))
            last=max(_ceil(low+low_error),_ceil(high+high_error))
            if first==last:last+=1
            for segment in mapping._segments:
                if segment.segment_id in window.segment_ids:
                    left=max(0,bisect_right(segment.frames,first,key=lambda frame:frame[0])-1)
                    right=bisect_left(segment.frames,last,key=lambda frame:frame[0])
                    selected+=max(0,right-left)
                    if selected>MAX_SELECTED_FRAMES:
                        raise InvestigationError('oversized_interval',413)
        return selected

    def associate(self,payload):
        from .video_context import ContextError,associate_context,load_context
        fields(payload,('alignment_id','revision','sha256','catalog_revision','context'))
        if self.service.stop.is_set() or self.service.closed:
            raise InvestigationError('service_stopping',503)
        alignment_id=identity(payload['alignment_id']);revision=integer(payload['revision'],1,1000000)
        try:
            catalog,context=load_context(self.notebook,payload['catalog_revision'],payload['context'])
            mapping,actual=self._load(alignment_id,revision,payload['sha256'])
            association=associate_context(mapping,catalog,context,self._selection_bound)
        except ContextError as exc:
            raise InvestigationError(exc.code,exc.status) from None
        except AlignmentError:
            raise InvestigationError('alignment_evidence_conflict',409) from None
        return _response(dict(schema_version=1,alignment_id=alignment_id,revision=revision,sha256=actual,
                              catalog_revision=payload['catalog_revision'],**association))

    def map(self, payload):
        fields(payload, ('alignment_id','revision','sha256','robot_id','boot_id','start_robot_ns',
                         'end_robot_ns','event_id','note_revision'))
        alignment_id=identity(payload['alignment_id']);revision=integer(payload['revision'],1,1000000)
        robot_id=identity(payload['robot_id']);boot_id=identity(payload['boot_id'])
        start,end=int(ns(payload['start_robot_ns'])),int(ns(payload['end_robot_ns']))
        if end<start:raise InvestigationError('invalid_interval')
        event,selected_note=payload['event_id'],payload['note_revision']
        annotation=None
        if event is not None or selected_note is not None:
            identity(event);integer(selected_note,1,1000000)
            with self.notebook._connection() as db:
                row=db.execute('SELECT * FROM annotation_revisions WHERE event_id=? AND revision=?',
                               (event,selected_note)).fetchone()
            if row is None:raise InvestigationError('annotation_revision_not_found',404)
            annotation=self.notebook._annotation(row)
        mapping,actual=self._load(alignment_id,revision,payload['sha256'])
        self._selection_bound(mapping,robot_id,boot_id,start,end)
        try:
            result=mapping.map_interval(robot_id,boot_id,start,end,event_id=event)
        except AlignmentError:
            raise InvestigationError('alignment_evidence_conflict',409) from None
        return _response(dict(schema_version=1,alignment_id=alignment_id,revision=revision,sha256=actual,
            time_basis='explicit_manual_robot_interval',annotation=annotation,result=result))
