"""Opt-in standalone recording core; no camera, robot, or service starts on import."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from .storage import DataRootOwner


class RecordingError(ValueError):
    pass


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise RecordingError("Invalid opaque recording identity")
    return value


def _ns(value):
    try:
        number = Decimal(str(value)) * 1_000_000_000
        if not number.is_finite():
            raise RecordingError("Invalid presentation timestamp")
        return int(number)
    except (InvalidOperation, ValueError, TypeError):
        raise RecordingError("Invalid presentation timestamp") from None


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _safe(root, relative):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", relative) or any(
            part in (".", "..") or part.endswith(".") for part in relative.split("/")):
        raise RecordingError("Invalid recording artifact path")
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink() or parent.resolve() != parent.absolute():
            raise RecordingError("Recording links are unsupported")
    return path


def _publish(path, payload):
    encoded = _json(payload)
    if path.exists():
        if path.read_bytes() != encoded:
            raise RecordingError("Immutable recording metadata conflict")
        return
    temporary = _safe(path.parent, path.name + ".writing")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(path)


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class FFmpegConfig:
    executable: str
    probe_executable: str
    version_pin: str
    camera_id: str
    input_format: str
    camera_input: str
    source_type: str
    segment_seconds: float = 60
    stalled_seconds: float = 15
    minimum_free_bytes: int = 256 * 1024 * 1024
    operation_timeout: float = 30
    maximum_probe_output_bytes: int = 16 * 1024 * 1024
    maximum_version_output_bytes: int = 64 * 1024
    maximum_frames_per_segment: int = 100_000
    maximum_progress_bytes: int = 1024 * 1024
    maximum_error_log_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        _id(self.camera_id)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.version_pin):
            raise RecordingError("Exact FFmpeg version pin required")
        if self.input_format not in ("dshow", "v4l2", "rtsp", "lavfi"):
            raise RecordingError("Unsupported explicitly configured input format")
        if not isinstance(self.camera_input, str) or not self.camera_input or "\x00" in self.camera_input:
            raise RecordingError("Explicit camera input required")
        if self.source_type not in ("REAL", "SYNTHETIC") or self.input_format == "lavfi" and self.source_type != "SYNTHETIC":
            raise RecordingError("Recording source provenance is invalid")
        for value, maximum in ((self.segment_seconds, 3600), (self.stalled_seconds, 3600), (self.operation_timeout, 300)):
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= maximum:
                raise RecordingError("Recording bounds must be finite positive seconds")
        if type(self.minimum_free_bytes) is not int or self.minimum_free_bytes < 0:
            raise RecordingError("Storage reserve must be nonnegative bytes")
        for name in ('maximum_probe_output_bytes','maximum_version_output_bytes',
                     'maximum_frames_per_segment','maximum_progress_bytes','maximum_error_log_bytes'):
            value=getattr(self,name)
            if type(value) is not int or not 0<value<=256*1024*1024:
                raise RecordingError('Recording resource limits must be positive bounded integers')
        for executable in (self.executable, self.probe_executable):
            if not isinstance(executable, str) or not Path(executable).is_absolute():
                raise RecordingError("Explicit absolute executable paths required")


class FFmpegAdapter:
    """Argument-array FFmpeg adapter. Native/media qualification is separate."""
    qualification = "native_unqualified"

    def __init__(self, config):
        self.config = config
        self.process = None
        self.log = None

    def _run(self, arguments, *, maximum_output_bytes=None):
        limit=maximum_output_bytes or self.config.maximum_probe_output_bytes
        process=None
        reader=None
        output=bytearray()
        overflow=threading.Event()
        read_failed=threading.Event()
        try:
            process=subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     shell=False, creationflags=_flags())
            def read_output():
                try:
                    while True:
                        block=process.stdout.read1(min(65536,limit-len(output)+1))
                        if not block:
                            return
                        if len(output)+len(block)>limit:
                            overflow.set()
                            process.kill()
                            return
                        output.extend(block)
                except OSError:
                    read_failed.set()
            reader=threading.Thread(target=read_output,name='recording-bounded-output',daemon=True)
            reader.start()
            process.wait(timeout=self.config.operation_timeout)
            reader.join(timeout=self.config.operation_timeout)
            if overflow.is_set():
                raise RecordingError('Native recording output limit exceeded')
            if reader.is_alive() or read_failed.is_set() or process.returncode!=0:
                raise RecordingError('Native recording command failed')
            return bytes(output)
        except (OSError, subprocess.SubprocessError):
            raise RecordingError("Native recording command failed") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=self.config.operation_timeout)
                if reader:
                    reader.join(timeout=self.config.operation_timeout)
                if process.stdout and (reader is None or not reader.is_alive()):
                    process.stdout.close()

    def validate(self):
        for executable, tool in ((self.config.executable, "ffmpeg"), (self.config.probe_executable, "ffprobe")):
            if not Path(executable).is_file():
                raise RecordingError("Configured recording executable unavailable")
            lines = self._run([executable, "-version"],maximum_output_bytes=self.config.maximum_version_output_bytes).decode("utf-8", "replace").splitlines()
            first=lines[0] if lines else ''
            if not first.startswith(tool + " version " + self.config.version_pin + " "):
                raise RecordingError("Recording executable version differs from exact pin")
        return {"adapter": "ffmpeg", "version": self.config.version_pin,
                "executable_sha256": _hash(Path(self.config.executable)),
                "probe_executable_sha256": _hash(Path(self.config.probe_executable)),
                "qualification": self.qualification}

    def start(self, folder):
        config = self.config
        arguments = [config.executable, "-hide_banner", "-n", "-loglevel", "error"]
        if config.source_type == "SYNTHETIC":
            arguments.append("-re")
        arguments += ["-f", config.input_format, "-i", config.camera_input, "-map", "0:v:0", "-an",
                      "-c:v", "libx264", "-preset", "ultrafast", "-fps_mode", "passthrough",
                      "-force_key_frames", f"expr:gte(t,n_forced*{config.segment_seconds})",
                      "-f", "segment", "-segment_time", str(config.segment_seconds),
                      "-segment_list", str(folder / "segments.csv"), "-segment_list_type", "csv",
                      "-reset_timestamps", "0", "-progress", str(folder / "progress.txt"),
                      "-stats_period", "1", str(folder / "segment-%06d.mkv")]
        self.log = _safe(folder, "native-errors.log").open("xb")
        try:
            self.process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=self.log, shell=False, creationflags=_flags())
        except OSError:
            self.log.close()
            raise RecordingError("Camera process could not start") from None

    def poll(self):
        return self.process.poll()

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(b"q\n")
                self.process.stdin.flush()
                self.process.wait(timeout=self.config.operation_timeout)
            except (OSError, subprocess.SubprocessError):
                self.process.kill()
                self.process.wait(timeout=self.config.operation_timeout)
        if self.process and self.process.stdin:
            self.process.stdin.close()
        if self.log:
            self.log.close()

    def probe(self, path):
        payload = self._run([self.config.probe_executable, "-v", "error", "-select_streams", "v:0",
                             "-show_frames", "-show_streams", "-show_entries",
                             "frame=best_effort_timestamp_time,pkt_duration_time,duration_time:stream=codec_name,time_base,width,height",
                             "-of", "json", str(path)])
        try:
            data = json.loads(payload)
            if not isinstance(data['frames'],list) or len(data['frames'])>self.config.maximum_frames_per_segment:
                raise RecordingError('Native frame count limit exceeded')
            frames = [{"pts_ns": _ns(frame["best_effort_timestamp_time"]),
                       "duration_ns": _ns(frame.get("duration_time", frame.get("pkt_duration_time")))
                       if "duration_time" in frame or "pkt_duration_time" in frame else None}
                      for frame in data["frames"]]
            return {"frames": frames, "codec": data["streams"][0]["codec_name"],
                    "time_base": data["streams"][0]["time_base"]}
        except (KeyError, IndexError, TypeError, ValueError):
            raise RecordingError("Native video PTS metadata unavailable") from None

    def render(self, source, destination, start_ns, end_ns):
        self._run([self.config.executable, "-hide_banner", "-n", "-loglevel", "error", "-copyts", "-i", str(source),
                   "-map", "0:v:0", "-an", "-vf",
                   f"trim=start={Decimal(start_ns)/1_000_000_000}:end={Decimal(end_ns)/1_000_000_000},setpts=PTS-STARTPTS",
                   "-c:v", "libx264", "-fps_mode", "passthrough", str(destination)])


def _flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class Recorder:
    """One owned recording session; call tick on an independent worker, never the HTTP thread."""
    def __init__(self, root, config, *, adapter=None, monotonic=time.monotonic, clock_ns=time.time_ns,
                 free_bytes=lambda root: shutil.disk_usage(root).free):
        self.root = Path(root).resolve()
        self.config = config
        self.adapter = adapter or FFmpegAdapter(config)
        self.monotonic, self.clock_ns, self.free_bytes = monotonic, clock_ns, free_bytes
        self.folder = self.owner = None
        self.segments = []
        self.process_started = False
        self.state = "stopped"
        self.error_code = None
        self.last_frame = None
        self.last_progress = None
        self.dropped_frames = None

    def start(self, session_id):
        if self.owner is not None:
            raise RecordingError("Recording session is already owned")
        _id(session_id)
        self.root.mkdir(parents=True, exist_ok=True)
        capture = uuid.uuid4().hex
        self.folder = _safe(self.root, f"{self.config.camera_id}/{session_id}/{capture}")
        self.folder.mkdir(parents=True)
        self.owner = DataRootOwner(self.folder)
        self.segments = []
        self.last_frame = self.last_progress = self.dropped_frames = None
        self.capture = capture
        self.session_id = session_id
        self.started_utc_ns = self.clock_ns()
        self.last_activity = self.monotonic()
        self.error_code = None
        try:
            self.provenance = self.adapter.validate()
            if self.free_bytes(self.folder) < self.config.minimum_free_bytes:
                raise RecordingError("Storage reserve unavailable")
            _publish(self.folder / "session.json", {"schema_version": 1, "capture_id": capture,
                     "camera_id": self.config.camera_id, "session_id": session_id,
                     "source_type": self.config.source_type, "started_utc_ns": str(self.started_utc_ns),
                     "utc_basis": "process_launch_estimate", "utc_uncertainty_ns": None,
                     "input_configuration_sha256": hashlib.sha256(self.config.camera_input.encode()).hexdigest(),
                     "segment_target_seconds": self.config.segment_seconds, "provenance": self.provenance})
            self.adapter.start(self.folder)
            self.process_started = True
            self.state = "starting"
        except (OSError, RecordingError) as exc:
            self._fail("startup_failed", exc)
            raise
        return self.health()

    def _fail(self, code, exc=None):
        self.state, self.error_code = "error", code
        if self.process_started:
            try:
                self.adapter.stop()
                self.process_started = False
            except (OSError, RecordingError, subprocess.SubprocessError):
                self.error_code = "camera_shutdown_failed"
        if self.folder:
            try:
                _publish(self.folder / ("failure-" + uuid.uuid4().hex + ".json"), {
                    "schema_version": 1, "error_code": code, "exception_class": type(exc).__name__ if exc else None,
                    "observed_utc_ns": str(self.clock_ns()), "coverage_after_last_closed_segment": "unavailable"})
            except (OSError, RecordingError):
                # A full/unwritable archive cannot durably report its own
                # failure. Keep the in-memory health failure and stop capture.
                pass
            finally:
                if self.owner and not self.process_started:
                    self.owner.close()
                    self.owner = None

    def _finalize(self):
        listing = _safe(self.folder, "segments.csv")
        if not listing.exists():
            return
        # FFmpeg only emits CSV rows after closing the segment. Ignore a final
        # incomplete row while the producer is still writing it.
        with listing.open("r", encoding="utf-8", newline="") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    break
                row = next(csv.reader([line]))
                if len(row) != 3:
                    raise RecordingError("Invalid closed-segment notification")
                name = Path(row[0]).name
                if not re.fullmatch(r"segment-\d{6}\.mkv", name) or row[0] not in (name, str(self.folder / name)):
                    raise RecordingError("Closed-segment path escaped recording root")
                identity = self.capture + "-" + name[8:14]
                if any(segment["segment_id"] == identity for segment in self.segments):
                    continue
                source = _safe(self.folder, name)
                metadata = self.adapter.probe(source)
                frames = metadata["frames"]
                if len(frames)>self.config.maximum_frames_per_segment:
                    raise RecordingError('Recording frame count limit exceeded')
                if not frames or any(type(frame.get("pts_ns")) is not int for frame in frames):
                    raise RecordingError("Actual frame PTS is required")
                pts = [frame["pts_ns"] for frame in frames]
                if any(right <= left for left, right in zip(pts, pts[1:])):
                    raise RecordingError("Frame PTS is discontinuous or duplicate")
                durations = [frame.get("duration_ns") for frame in frames]
                if any(value is not None and (type(value) is not int or value <= 0) for value in durations):
                    raise RecordingError("Invalid frame duration")
                if durations[-1] is None:
                    raise RecordingError("Final frame duration unavailable; coverage is uncertain")
                start, end = pts[0], pts[-1] + durations[-1]
                if end - start > (self.config.segment_seconds + 2) * 1e9:
                    raise RecordingError("Segment exceeded configured duration bound")
                with source.open("r+b") as video:
                    os.fsync(video.fileno())
                artifact = {"schema_version": 1, "segment_id": identity, "camera_id": self.config.camera_id,
                            "session_id": self.session_id, "capture_id": self.capture, "relative_path": name,
                            "source_type": self.config.source_type, "state": "closed_verified",
                            "sha256": _hash(source), "size_bytes": source.stat().st_size,
                            "container": "matroska", "codec": metadata["codec"], "time_base": metadata["time_base"],
                            "start_pts_ns": str(start), "end_pts_ns": str(end),
                            "frames": [{"pts_ns": str(frame["pts_ns"]), "duration_ns": str(frame["duration_ns"]) if frame.get("duration_ns") is not None else None} for frame in frames],
                            "dropped_frames": None, "utc_basis": "unmapped_recording_pts", "utc_uncertainty_ns": None,
                            "provenance": self.provenance}
                _publish(_safe(self.folder, name + ".json"), artifact)
                self.segments.append(artifact)

    def tick(self):
        if not self.process_started:
            return self.health()
        try:
            for name,limit in (('progress.txt',self.config.maximum_progress_bytes),
                               ('native-errors.log',self.config.maximum_error_log_bytes)):
                artifact=_safe(self.folder,name)
                if artifact.exists() and artifact.stat().st_size>limit:
                    self._fail('recording_log_size_limit_exceeded')
                    return self.health()
            if self.free_bytes(self.folder) < self.config.minimum_free_bytes:
                self._fail("storage_reserve_exhausted")
                return self.health()
            self._finalize()
            progress = _safe(self.folder, "progress.txt")
            if progress.exists():
                with progress.open("rb") as stream:
                    stream.seek(max(0, progress.stat().st_size - 65536))
                    text = stream.read(65536).decode("utf-8", "replace")
                blocks = text.split("progress=")
                if len(blocks) > 1:
                    values = dict(line.split("=", 1) for line in blocks[-2].splitlines() if "=" in line)
                    frame = int(values.get("frame", "0"))
                    if self.last_frame is None or frame > self.last_frame:
                        if frame > 0:
                            self.state = "recording"
                            self.last_activity = self.monotonic()
                        self.last_frame = frame
                    drop = values.get("drop_frames")
                    self.dropped_frames = int(drop) if drop is not None else None
            if self.adapter.poll() is not None:
                self._fail("camera_process_stopped")
            elif self.monotonic() - self.last_activity >= self.config.stalled_seconds:
                self._fail("camera_frames_stalled")
        except (OSError, RecordingError, ValueError, KeyError, TypeError) as exc:
            self._fail("recording_artifact_failure", exc)
        return self.health()

    def stop(self):
        if self.process_started:
            try:
                self.adapter.stop()
                self.process_started = False
                self._finalize()
                self.state = "stopped"
            except (OSError, RecordingError, subprocess.SubprocessError) as exc:
                self._fail("finalization_failed", exc)
        if self.owner and not self.process_started:
            self.owner.close()
            self.owner = None
        return self.health()

    def health(self):
        return {"schema_version": 1, "state": self.state, "error_code": self.error_code,
                "camera_id": self.config.camera_id, "source_type": self.config.source_type,
                "closed_segments": len(self.segments), "frames": self.last_frame,
                "dropped_frames": self.dropped_frames, "live_camera_qualified": False,
                "last_frame_age_seconds": self.monotonic() - self.last_activity if self.folder else None}

    def list_segments(self):
        return json.loads(json.dumps(self.segments))

    def preserve(self, request_id, start_pts_ns, end_pts_ns, *, event_id=None, run_id=None):
        """Pin verified originals and actual PTS frame spans; unknown/open/gap context is explicit."""
        _id(request_id)
        if event_id is not None:
            _id(event_id)
        if run_id is not None:
            _id(run_id)
        if type(start_pts_ns) is not int or type(end_pts_ns) is not int or end_pts_ns <= start_pts_ns:
            raise RecordingError("Preservation requires a nonempty recording-PTS interval")
        if self.folder is None:
            raise RecordingError("No recording session exists")
        pins = _safe(self.folder, "pins")
        pins.mkdir(exist_ok=True)
        pin_path = _safe(pins, request_id + ".json")
        if pin_path.exists():
            existing = json.loads(pin_path.read_bytes())
            expected = (request_id, str(start_pts_ns), str(end_pts_ns), event_id, run_id)
            if tuple(existing[key] for key in ("request_id", "start_pts_ns", "end_pts_ns", "event_id", "run_id")) != expected:
                raise RecordingError("Immutable preservation request conflict")
            return existing
        spans = []
        covered = []
        for segment in self.segments:
            source = _safe(self.folder, segment["relative_path"])
            if not source.is_file():
                continue  # Retention/external loss is unavailable context, never synthetic success.
            if source.stat().st_size != segment["size_bytes"] or _hash(source) != segment["sha256"]:
                raise RecordingError("Original recording integrity conflict")
            indexes = []
            for index, frame in enumerate(segment["frames"]):
                start = int(frame["pts_ns"])
                duration = int(frame["duration_ns"]) if frame["duration_ns"] is not None else None
                end = start + duration if duration is not None else start
                if start < end_pts_ns and end > start_pts_ns:
                    indexes.append(index)
                    covered.append((max(start, start_pts_ns), min(end, end_pts_ns)))
            if indexes:
                spans.append({"segment_id": segment["segment_id"], "relative_path": segment["relative_path"],
                              "source_sha256": segment["sha256"], "frame_indexes": indexes,
                              "start_pts_ns": str(start_pts_ns), "end_pts_ns": str(end_pts_ns)})
        cursor = start_pts_ns
        gaps = []
        for start, end in sorted(covered):
            if start > cursor:
                gaps.append([str(cursor), str(start)])
            cursor = max(cursor, end)
        if cursor < end_pts_ns:
            gaps.append([str(cursor), str(end_pts_ns)])
        value = {"schema_version": 1, "request_id": request_id, "capture_id": self.capture,
                 "event_id": event_id, "run_id": run_id, "start_pts_ns": str(start_pts_ns),
                 "end_pts_ns": str(end_pts_ns), "state": "preserved" if not gaps else "partial" if spans else "unavailable",
                 "unavailable_pts_intervals": gaps, "spans": spans, "policy": "pin_originals_no_deletion",
                 "utc_alignment": "requires_camera_specific_mapping", "source_type": self.config.source_type}
        _publish(pin_path, value)
        return value

    def render_pin(self, request_id):
        """Explicit derivative operation; one clip per original span, preserving gaps."""
        _id(request_id)
        if self.folder is None:
            raise RecordingError("No recording session exists")
        pin = json.loads(_safe(self.folder, "pins/" + request_id + ".json").read_bytes())
        if not pin["spans"]:
            raise RecordingError("Requested footage unavailable")
        output = _safe(self.folder, "clips/" + request_id)
        output.mkdir(parents=True, exist_ok=True)
        metadata_path = _safe(output, "manifest.json")
        if metadata_path.exists():
            manifest = json.loads(metadata_path.read_bytes())
            for artifact in manifest["artifacts"]:
                if _hash(_safe(output, artifact["relative_path"])) != artifact["sha256"]:
                    raise RecordingError("Derived clip integrity conflict")
            return manifest
        derivative_provenance = self.adapter.validate()
        artifacts = []
        for number, span in enumerate(pin["spans"]):
            source = _safe(self.folder, span["relative_path"])
            if _hash(source) != span["source_sha256"]:
                raise RecordingError("Original recording integrity conflict")
            segment = next(item for item in self.segments if item["segment_id"] == span["segment_id"])
            frames = segment["frames"]
            first, last = span["frame_indexes"][0], span["frame_indexes"][-1]
            start = int(frames[first]["pts_ns"])
            end = int(frames[last]["pts_ns"]) + int(frames[last]["duration_ns"])
            destination = _safe(output, f"clip-{number:06d}.mkv")
            if destination.exists():
                raise RecordingError("Unpublished clip already exists; preserve it and use a new request identity")
            self.adapter.render(source, destination, start, end)
            observed = self.adapter.probe(destination)
            if len(observed["frames"]) != len(span["frame_indexes"]):
                raise RecordingError("Derived clip did not reproduce selected actual frames")
            # Frame count alone cannot prove variable-rate timing/gaps survived
            # re-encoding. Allow one declared output time-base tick of
            # quantization, never a nominal-FPS reconstruction.
            try:
                numerator,denominator=observed['time_base'].split('/')
                quantum=Decimal(numerator)/Decimal(denominator)*1_000_000_000
                if not quantum.is_finite() or quantum<=0:
                    raise RecordingError('Derived clip time base invalid')
                actual=[frame['pts_ns'] for frame in observed['frames']]
                expected=[int(frames[index]['pts_ns'])-start for index in span['frame_indexes']]
                if (any(type(pts) is not int for pts in actual)
                        or any(b<=a for a,b in zip(actual,actual[1:]))
                        or any(abs(pts-reference)>quantum for pts,reference in zip(actual,expected))):
                    raise RecordingError('Derived clip did not preserve actual frame PTS gaps')
            except RecordingError:
                raise
            except (KeyError,TypeError,ValueError,InvalidOperation,ZeroDivisionError):
                raise RecordingError('Derived clip PTS metadata invalid') from None
            with destination.open("r+b") as stream:
                os.fsync(stream.fileno())
            artifacts.append({"relative_path": destination.name, "sha256": _hash(destination),
                              "size_bytes": destination.stat().st_size, "source_sha256": span["source_sha256"],
                              "source_start_pts_ns": str(start), "source_end_pts_ns": str(end),
                              "actual_frame_pts_ns": [str(frame["pts_ns"]) for frame in observed["frames"]]})
        manifest = {"schema_version": 1, "request_id": request_id, "source_type": self.config.source_type,
                    "state": "derived", "coverage": pin["state"], "unavailable_pts_intervals": pin["unavailable_pts_intervals"],
                    "provenance": derivative_provenance, "capture_provenance": self.provenance,
                    "artifacts": artifacts}
        _publish(metadata_path, manifest)
        return manifest
