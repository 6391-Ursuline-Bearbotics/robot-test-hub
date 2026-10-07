"""Manual, camera/capture/boot-specific alignment. No native process or media rewriting."""
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re


class AlignmentError(ValueError):
    pass


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise AlignmentError('Invalid alignment identity')
    return value


def _ns(value):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
        number = int(value)
    else:
        raise AlignmentError('Nanoseconds require an integer or canonical decimal string')
    if not 0 <= number < 1 << 63:
        raise AlignmentError('Nanoseconds outside signed integer range')
    return number


def _fraction(value):
    if not isinstance(value, str) or len(value) > 100:
        raise AlignmentError('Explicit finite decimal clock assumption required')
    try:
        decimal = Decimal(value)
        if (not decimal.is_finite() or abs(decimal.as_tuple().exponent) > 100
                or abs(decimal.adjusted()) > 100):
            raise ValueError()
        return Fraction(decimal)
    except (ValueError, ArithmeticError):
        raise AlignmentError('Invalid decimal clock assumption') from None


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise AlignmentError('Invalid immutable evidence digest')
    return value


def _path(folder, relative):
    if (not isinstance(relative, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', relative)
            or relative in ('.', '..') or relative.endswith('.')):
        raise AlignmentError('Only a flat recording artifact path is allowed')
    root = Path(folder).absolute()
    path = root / relative
    if any(parent.is_symlink() for parent in (path, *path.parents)) or path.resolve() != path.absolute():
        raise AlignmentError('Recording links or path aliases are unsupported')
    return path


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _floor(value):
    return value.numerator // value.denominator


def _ceil(value):
    return -((-value.numerator) // value.denominator)


def _decimal(value):
    with localcontext() as context:
        context.prec = 50
        return str(Decimal(value.numerator) / Decimal(value.denominator))


@dataclass(frozen=True)
class VideoSegment:
    segment_id: str
    camera_id: str
    session_id: str
    capture_id: str
    source_type: str
    folder: str
    relative_path: str
    sha256: str
    size_bytes: int
    manifest_sha256: str
    frames: tuple

    @classmethod
    def from_manifest(cls, folder, manifest):
        if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
                or manifest['schema_version'] != 1 or manifest.get('state') != 'closed_verified'
                or manifest.get('source_type') not in ('REAL', 'SYNTHETIC')):
            raise AlignmentError('Verified recording manifest required')
        identities = [_id(manifest.get(key)) for key in ('segment_id', 'camera_id', 'session_id', 'capture_id')]
        path = _path(folder, manifest.get('relative_path'))
        digest = _sha(manifest.get('sha256'))
        size = manifest.get('size_bytes')
        if type(size) is not int or size < 0:
            raise AlignmentError('Invalid recording size')
        raw = manifest.get('frames')
        if not isinstance(raw, list) or not 1 <= len(raw) <= 1_000_000:
            raise AlignmentError('Bounded actual frame metadata required')
        frames = []
        for frame in raw:
            if not isinstance(frame, dict):
                raise AlignmentError('Invalid frame metadata')
            pts = _ns(frame.get('pts_ns'))
            duration = None if frame.get('duration_ns') is None else _ns(frame['duration_ns'])
            if duration == 0 or duration is not None and pts + duration >= 1 << 63:
                raise AlignmentError('Invalid frame duration')
            frames.append((pts, duration))
        if any(b[0] <= a[0] or a[1] is not None and a[0] + a[1] > b[0]
               for a, b in zip(frames, frames[1:])):
            raise AlignmentError('Frame PTS overlap or discontinuity requires separate evidence')
        if (frames[-1][1] is None or _ns(manifest.get('start_pts_ns')) != frames[0][0]
                or _ns(manifest.get('end_pts_ns')) != sum(frames[-1])):
            raise AlignmentError('Manifest PTS bounds disagree with actual frames')
        segment = cls(*identities, manifest['source_type'], str(path.parent), path.name,
                      digest, size, hashlib.sha256(_encoded(manifest)).hexdigest(), tuple(frames))
        if not segment.verify():
            raise AlignmentError('Original recording unavailable')
        return segment

    def verify(self):
        path = _path(self.folder, self.relative_path)
        if not path.exists():
            return False
        if not path.is_file() or path.stat().st_size != self.size_bytes or _hash(path) != self.sha256:
            raise AlignmentError('Original recording integrity conflict')
        return True

    def evidence(self):
        return {'segment_id': self.segment_id, 'relative_path': self.relative_path,
                'source_sha256': self.sha256, 'manifest_sha256': self.manifest_sha256,
                'source_type': self.source_type}


@dataclass(frozen=True)
class ManualAnchor:
    robot_ns: int
    segment_id: str
    source_sha256: str
    frame_index: int
    robot_uncertainty_ns: int
    video_uncertainty_ns: int
    evidence_label: str

    def __post_init__(self):
        for key in ('robot_ns', 'robot_uncertainty_ns', 'video_uncertainty_ns'):
            object.__setattr__(self, key, _ns(getattr(self, key)))


@dataclass(frozen=True)
class CalibrationWindow:
    continuity_id: str
    start_robot_ns: int
    end_robot_ns: int
    anchors: tuple
    segment_ids: tuple
    model_uncertainty_ns: int = 0
    assumed_scale: str = None
    assumed_drift_uncertainty_ppm: str = None

    def __post_init__(self):
        for key in ('start_robot_ns', 'end_robot_ns', 'model_uncertainty_ns'):
            object.__setattr__(self, key, _ns(getattr(self, key)))
        object.__setattr__(self, 'anchors', tuple(self.anchors))
        object.__setattr__(self, 'segment_ids', tuple(self.segment_ids))


class ManualAlignment:
    """Immutable snapshot with explicit continuity windows. Construct a new object for edits."""
    def __init__(self, robot_id, boot_id, camera_id, session_id, capture_id, segments, windows):
        self._identity = tuple(_id(value) for value in (robot_id, boot_id, camera_id, session_id, capture_id))
        self._segments = tuple(segments)
        if (not self._segments or len(self._segments) > 10000
                or any(not isinstance(s, VideoSegment) for s in self._segments)
                or len({s.segment_id for s in self._segments}) != len(self._segments)):
            raise AlignmentError('Unique bounded recording evidence required')
        if any((s.camera_id, s.session_id, s.capture_id) != self._identity[2:] for s in self._segments):
            raise AlignmentError('Camera/session/capture boundaries require separate mappings')
        if len({_path(s.folder, s.relative_path) for s in self._segments}) != len(self._segments):
            raise AlignmentError('Recording artifact paths cannot represent multiple segment identities')
        self._windows = tuple(windows)
        if not 1 <= len(self._windows) <= 1000 or any(not isinstance(w, CalibrationWindow) for w in self._windows):
            raise AlignmentError('Bounded calibration windows required')
        ordered = sorted(self._windows, key=lambda w: w.start_robot_ns)
        if len({_id(w.continuity_id) for w in ordered}) != len(ordered):
            raise AlignmentError('Duplicate continuity window')
        if any(a.end_robot_ns >= b.start_robot_ns for a, b in zip(ordered, ordered[1:])):
            raise AlignmentError('Robot calibration windows overlap')
        self._fits = tuple(self._fit(window) for window in ordered)
        self._document = _encoded(self._describe())

    def _fit(self, window):
        start, end = _ns(window.start_robot_ns), _ns(window.end_robot_ns)
        model = _ns(window.model_uncertainty_ns)
        if start > end or not 1 <= len(window.anchors) <= 1000:
            raise AlignmentError('Invalid calibration interval or anchor count')
        points = []
        evidence = []
        segments = {s.segment_id: s for s in self._segments}
        if (not window.segment_ids or len(set(window.segment_ids)) != len(window.segment_ids)
                or any(_id(identity) not in segments for identity in window.segment_ids)):
            raise AlignmentError('Explicit continuity-window segment evidence required')
        spans = sorted((segments[identity].frames[0][0], sum(segments[identity].frames[-1]))
                       for identity in window.segment_ids)
        if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise AlignmentError('Overlapping recording PTS requires separate continuity windows')
        for anchor in window.anchors:
            if not isinstance(anchor, ManualAnchor):
                raise AlignmentError('Typed manual frame anchors required')
            robot = _ns(anchor.robot_ns)
            segment = segments.get(_id(anchor.segment_id))
            if (segment is None or anchor.segment_id not in window.segment_ids
                    or _sha(anchor.source_sha256) != segment.sha256
                    or type(anchor.frame_index) is not int or not 0 <= anchor.frame_index < len(segment.frames)):
                raise AlignmentError('Anchor does not match immutable frame evidence')
            if not isinstance(anchor.evidence_label, str) or not 1 <= len(anchor.evidence_label) <= 1024:
                raise AlignmentError('Manual anchor evidence label required')
            robot_error, video_error = _ns(anchor.robot_uncertainty_ns), _ns(anchor.video_uncertainty_ns)
            pts = segment.frames[anchor.frame_index][0]
            points.append((robot, pts, robot_error, video_error))
            evidence.append(dict(segment.evidence(), frame_index=anchor.frame_index, robot_ns=str(robot),
                pts_ns=str(pts), robot_uncertainty_ns=str(robot_error), video_uncertainty_ns=str(video_error),
                evidence_label=anchor.evidence_label))
        points.sort()
        evidence.sort(key=lambda item: int(item['robot_ns']))
        if len({p[0] for p in points}) != len(points):
            raise AlignmentError('Robot anchor times must be unique')
        if len(points) == 1:
            scale = _fraction(window.assumed_scale)
            drift_error = _fraction(window.assumed_drift_uncertainty_ppm)
            if scale <= 0 or drift_error < 0 or not start <= points[0][0] <= end:
                raise AlignmentError('Single anchor requires explicit positive scale and drift bound')
            origin, pts_origin = map(Fraction, points[0][:2])
            mean, spread = origin, Fraction(0)
        else:
            if window.assumed_scale is not None or window.assumed_drift_uncertainty_ppm is not None:
                raise AlignmentError('Multiple anchors fit scale rather than assume it')
            if start < points[0][0] or end > points[-1][0]:
                raise AlignmentError('Calibration cannot extrapolate beyond observed anchors')
            if any(b[1] <= a[1] for a, b in zip(points, points[1:])):
                raise AlignmentError('PTS reset requires a separate continuity window')
            mean = sum(Fraction(p[0]) for p in points) / len(points)
            pts_mean = sum(Fraction(p[1]) for p in points) / len(points)
            spread = sum((p[0] - mean) ** 2 for p in points)
            scale = sum((p[0] - mean) * (p[1] - pts_mean) for p in points) / spread
            origin, pts_origin, drift_error = mean, pts_mean, Fraction(0)
        residuals = tuple(Fraction(p[1]) - (pts_origin + scale * (p[0] - origin)) for p in points)
        if len(points) > 1 and points[-1][0] - points[0][0] <= points[-1][2] + points[0][2]:
            raise AlignmentError('Anchor time uncertainty leaves clock scale unbounded')
        return (window, tuple(points), scale, origin, pts_origin, mean, spread, drift_error, residuals, tuple(evidence), model)

    @staticmethod
    def _predict(fit, robot_ns):
        window, points, scale, origin, pts_origin, mean, spread, drift_error, residuals, evidence, model = fit
        value = pts_origin + scale * (robot_ns - origin)
        allowance = max(map(abs, residuals)) + model
        if len(points) == 1:
            error_scale = scale + drift_error / 1_000_000
            error = points[0][3] + error_scale * points[0][2] + abs(robot_ns - origin) * drift_error / 1_000_000
        else:
            # Bound robot-side anchor error using a possible true slope, not
            # merely the fitted slope. Endpoints bound affine clock scale.
            first, last = points[0], points[-1]
            error_scale = max(scale, Fraction(last[1] - first[1] + last[3] + first[3] + 2 * allowance)
                              / (last[0] - first[0] - last[2] - first[2]))
            weights = [Fraction(1, len(points)) + (robot_ns - mean) * (p[0] - mean) / spread for p in points]
            error = sum(abs(weight) * (p[3] + error_scale * p[2]) for weight, p in zip(weights, points))
        return value, error + allowance

    def _describe(self):
        windows = []
        for fit in self._fits:
            window, points, scale, origin, pts_origin, mean, spread, drift_error, residuals, evidence, model = fit
            windows.append({'continuity_id': window.continuity_id,
                'segment_ids': list(window.segment_ids),
                'start_robot_ns': str(window.start_robot_ns), 'end_robot_ns': str(window.end_robot_ns),
                'scale': {'numerator': str(scale.numerator), 'denominator': str(scale.denominator)},
                'drift_ppm': _decimal((scale - 1) * 1_000_000),
                'residuals_ns': [_decimal(value) for value in residuals],
                'max_abs_residual_ns': str(_ceil(max(map(abs, residuals)))),
                'model_uncertainty_ns': str(model), 'anchors': list(evidence),
                'assumption': 'explicit_single_anchor_scale' if len(points) == 1 else 'affine_clock_within_calibrated_window',
                'assumed_scale': window.assumed_scale, 'assumed_drift_uncertainty_ppm': window.assumed_drift_uncertainty_ppm})
        return dict(zip(('robot_id', 'boot_id', 'camera_id', 'session_id', 'capture_id'), self._identity),
                    schema_version=1, method='manual-actual-frame-pts-1', windows=windows,
                    sources=[s.evidence() for s in self._segments], qualification='manual_unqualified',
                    measured_camera_alignment=False, utc_launch_used=False)

    def document(self):
        return json.loads(self._document)

    def map_interval(self, robot_id, boot_id, start_robot_ns, end_robot_ns, *, event_id=None, run_id=None):
        start, end = _ns(start_robot_ns), _ns(end_robot_ns)
        if end < start:
            raise AlignmentError('Event interval is reversed')
        for identity in (event_id, run_id):
            if identity is not None:
                _id(identity)
        result = dict(zip(('robot_id', 'boot_id', 'camera_id', 'session_id', 'capture_id'), self._identity),
            state='unavailable', reason='clock_domain_mismatch', event_id=event_id, run_id=run_id,
            mapping_sha256=hashlib.sha256(self._document).hexdigest(), spans=[], windows=[],
            unmapped_robot_intervals=[], qualification='manual_unqualified', measured_camera_alignment=False)
        if (robot_id, boot_id) != self._identity[:2]:
            return result
        coverage = []
        for fit in self._fits:
            window = fit[0]
            a, b = max(start, window.start_robot_ns), min(end, window.end_robot_ns)
            if a > b:
                continue
            coverage.append((a, b))
            low, low_error = self._predict(fit, a)
            high, high_error = self._predict(fit, b)
            # The uncertainty envelope is convex; its extrema over a closed
            # interval occur at endpoints. Round outward, preserving integer ns.
            pts_start = min(_floor(low - low_error), _floor(high - high_error))
            pts_end = max(_ceil(low + low_error), _ceil(high + high_error))
            if not -(1 << 63) <= pts_start < (1 << 63) or not -(1 << 63) <= pts_end < (1 << 63):
                raise AlignmentError('Mapped uncertainty exceeds signed nanosecond range')
            if pts_start == pts_end:
                pts_end += 1  # Exact point events select their containing frame.
                if pts_end >= 1 << 63:
                    raise AlignmentError('Mapped point interval exceeds signed nanosecond range')
            covered = []
            for segment in self._segments:
                if segment.segment_id not in window.segment_ids:
                    continue
                if not segment.verify():
                    continue
                indexes = []
                for index, (pts, duration) in enumerate(segment.frames):
                    if duration is not None and pts < pts_end and pts + duration > pts_start:
                        indexes.append(index)
                        covered.append((max(pts, pts_start), min(pts + duration, pts_end)))
                if indexes:
                    result['spans'].append(dict(segment.evidence(), continuity_id=window.continuity_id,
                        frame_indexes=indexes, start_pts_ns=str(pts_start), end_pts_ns=str(pts_end),
                        actual_frames=[{'frame_index': index, 'pts_ns': str(segment.frames[index][0]),
                                        'duration_ns': str(segment.frames[index][1])} for index in indexes]))
            gaps = _gaps(pts_start, pts_end, covered)
            result['windows'].append({'continuity_id': window.continuity_id,
                'start_robot_ns': str(a), 'end_robot_ns': str(b),
                'start_pts_ns': str(pts_start), 'end_pts_ns': str(pts_end),
                'uncertainty_ns': str(_ceil(max(low_error, high_error))), 'unavailable_pts_intervals': gaps})
        result['unmapped_robot_intervals'] = _gaps(start, end, coverage)
        if not coverage:
            result.update(state='outside', reason='outside_calibrated_windows')
        elif not result['spans']:
            result.update(state='gap', reason='no_available_frame_span')
        elif result['unmapped_robot_intervals'] or any(w['unavailable_pts_intervals'] for w in result['windows']):
            result.update(state='partial', reason='calibration_or_recording_gap')
        else:
            result.update(state='mapped', reason='actual_frames_selected_with_uncertainty')
        return result


def _gaps(start, end, covered):
    cursor, result = start, []
    for a, b in sorted(covered):
        if a > cursor:
            result.append([str(cursor), str(a)])
        cursor = max(cursor, b)
    if cursor < end:
        result.append([str(cursor), str(end)])
    return result


class AlignmentRevisions:
    """Immutable append-only files; serialize writers externally, like recorder ownership."""
    def __init__(self, folder, alignment_id):
        self.folder = Path(folder).absolute()
        self.alignment_id = _id(alignment_id)

    def _validate_parent(self, revision, payload, previous):
        try:
            if previous.stat().st_size > 64 * 1024 * 1024:
                raise AlignmentError('Alignment parent exceeds bounded revision size')
            prior = json.loads(previous.read_bytes())
            if (prior['alignment_id'] != self.alignment_id or type(prior['revision']) is not int
                    or prior['revision'] != revision-1 or type(prior['schema_version']) is not int
                    or prior['schema_version'] != 1):
                raise AlignmentError('Alignment parent identity mismatch')
            if any(prior.get(key) != payload.get(key) for key in
                   ('robot_id', 'boot_id', 'camera_id', 'session_id', 'capture_id')):
                raise AlignmentError('Clock-domain changes require a separate alignment identity')
            known = {item['segment_id']: item for item in prior['sources']}
            paths = {item['relative_path']: item for item in prior['sources']}
            if any((item['segment_id'] in known and item != known[item['segment_id']])
                   or (item['relative_path'] in paths and item != paths[item['relative_path']])
                   for item in payload['sources']):
                raise AlignmentError('Immutable alignment source evidence changed')
        except (KeyError, TypeError, json.JSONDecodeError, RecursionError):
            raise AlignmentError('Invalid alignment parent revision') from None

    def append(self, revision, alignment, *, expected_previous_sha256=None):
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise AlignmentError('Invalid alignment revision')
        payload = dict(alignment.document(), alignment_id=self.alignment_id, revision=revision,
                       previous_sha256=expected_previous_sha256)
        encoded = _encoded(payload)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = _path(self.folder, f'{self.alignment_id}-{revision:06d}.json')
        if revision == 1:
            if expected_previous_sha256 is not None:
                raise AlignmentError('First revision has no parent')
        else:
            previous = _path(self.folder, f'{self.alignment_id}-{revision-1:06d}.json')
            if not previous.is_file() or _hash(previous) != _sha(expected_previous_sha256):
                raise AlignmentError('Alignment revision parent conflict')
            self._validate_parent(revision, payload, previous)
        if path.exists():
            if path.read_bytes() != encoded:
                raise AlignmentError('Immutable alignment revision conflict')
        else:
            temporary = _path(self.folder, f'{self.alignment_id}-{revision:06d}.writing')
            created = False
            try:
                with temporary.open('xb') as stream:
                    created = True
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Hard linking is an atomic create without replacing a revision.
                os.link(temporary, path)
            finally:
                if created:
                    temporary.unlink(missing_ok=True)
        return {'revision': revision, 'sha256': hashlib.sha256(encoded).hexdigest(), 'path': str(path)}

    def load(self, revision, segments, *, expected_sha256):
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise AlignmentError('Invalid alignment revision')
        path = _path(self.folder, f'{self.alignment_id}-{revision:06d}.json')
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024 or _hash(path) != _sha(expected_sha256):
            raise AlignmentError('Alignment revision unavailable or digest mismatch')
        try:
            payload = json.loads(path.read_bytes())
            if (payload['alignment_id'] != self.alignment_id or type(payload['revision']) is not int
                    or payload['revision'] != revision):
                raise AlignmentError('Alignment revision identity mismatch')
            if revision == 1:
                if payload['previous_sha256'] is not None:
                    raise AlignmentError('First revision has no parent')
            else:
                previous = _path(self.folder, f'{self.alignment_id}-{revision-1:06d}.json')
                if not previous.is_file() or _hash(previous) != _sha(payload['previous_sha256']):
                    raise AlignmentError('Alignment parent digest mismatch')
                self._validate_parent(revision, payload, previous)
            windows = []
            for item in payload['windows']:
                anchors = tuple(ManualAnchor(anchor['robot_ns'], anchor['segment_id'], anchor['source_sha256'],
                    anchor['frame_index'], anchor['robot_uncertainty_ns'], anchor['video_uncertainty_ns'],
                    anchor['evidence_label']) for anchor in item['anchors'])
                windows.append(CalibrationWindow(item['continuity_id'], item['start_robot_ns'], item['end_robot_ns'],
                    anchors, tuple(item['segment_ids']), item['model_uncertainty_ns'], item['assumed_scale'],
                    item['assumed_drift_uncertainty_ppm']))
            mapping = ManualAlignment(*(payload[key] for key in
                ('robot_id', 'boot_id', 'camera_id', 'session_id', 'capture_id')), segments, windows)
            document = {key: value for key, value in payload.items()
                        if key not in ('alignment_id', 'revision', 'previous_sha256')}
            if _encoded(document) != _encoded(mapping.document()):
                raise AlignmentError('Stored fit or recording evidence differs from reconstructed mapping')
            return mapping
        except (KeyError, TypeError, json.JSONDecodeError, RecursionError):
            raise AlignmentError('Invalid stored alignment revision') from None
