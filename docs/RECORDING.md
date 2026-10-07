# Practice recording core (T16 increment)

`robot_test_hub.recorder` implements an opt-in, standalone recording core and an FFmpeg process adapter. Importing it never starts a camera. There is no recorder service/configuration/API/browser wiring in this increment. The hub does not yet automatically record practice or request footage when a note is saved.

The core accepts an explicit `FFmpegConfig` containing absolute FFmpeg and FFprobe executable paths, an exact version token, camera identity/input, input format, and `REAL` or `SYNTHETIC` provenance. Supported command shapes are DirectShow (`dshow`), Linux Video4Linux (`v4l2`), explicit RTSP (`rtsp`), or generated FFmpeg input (`lavfi`, necessarily synthetic). No executable search, package installation, camera discovery, automatic network connection, or default external camera is performed. A configured RTSP feed must be on the practice camera network, independently of the robot radio.

Both executables must report the exact configured version token. The session records the token and both executable SHA-256 values. Native availability/version validation is a prerequisite to capture, not evidence that the selected build, input device, encoder or storage has passed commissioning. The implementation requires the selected build to supply the Matroska/segment muxers and `libx264`; codec availability has not been qualified here. Missing tools/version mismatch fail startup visibly.

Use the core from an independent worker:

```python
from robot_test_hub.recorder import FFmpegConfig, Recorder

# All values are explicitly selected by the operator; no repository defaults
# name a camera, media installation, or user storage destination.
config = FFmpegConfig(
    executable=ffmpeg_absolute_path,
    probe_executable=ffprobe_absolute_path,
    version_pin=installed_exact_version_token,
    camera_id="overview",
    input_format=selected_input_format,
    camera_input=selected_input,
    source_type="REAL",
)
recorder = Recorder(selected_video_root, config)
try:
    recorder.start("practice-session")
    # Call tick repeatedly on this recorder's worker, independently of robot
    # status, transfer permission and HTTP requests. Health never enables a robot.
    recorder.tick()
finally:
    recorder.stop()
```

`start` opens one continuous FFmpeg process. Segment keyframes are requested at the configured target interval, normally 60 seconds, with a validated maximum target of one hour. FFmpeg CSV notifications identify segments closed by the muxer; file-size stability alone never proves closure. FFprobe reads each closed segment's actual frame PTS, duration, codec and time base. FFmpeg preserves variable timing with `fps_mode=passthrough`; no `frame_index / nominal_fps` conversion is used. The final frame must have a known duration. Missing/nonmonotonic PTS, duplicate PTS, missing final duration and a segment exceeding the configured target by more than two seconds fail artifact finalization rather than inventing usable coverage. This bound is checked on completed artifacts; a stalled producer is stopped by the independent frame-progress watchdog. These checks do not qualify real-time OS scheduling or camera buffering.

Each capture receives a new UUID namespace and retains:

```text
<video-root>/<camera>/<session>/<capture-uuid>/
  session.json
  segment-000000.mkv
  segment-000000.mkv.json
  segments.csv
  progress.txt
  native-errors.log
  failure-<uuid>.json
  pins/<request-id>.json
  clips/<request-id>/clip-000000.mkv
  clips/<request-id>/manifest.json
```

The namespace plus monotonically allocated muxer segment index is the opaque segment identity. Closed media bytes are flushed before publishing the immutable segment manifest. Original file hash/size, camera/session/capture IDs, source type and per-frame PTS/duration decimal strings remain explicit. The session's process-launch UTC is a coarse observation with **unknown exposure uncertainty**; segment PTS is **unmapped recording time**. Process launch does not establish first sensor exposure time, and this increment makes no robot/video alignment guarantee. Camera-specific validated mappings and revisions belong to T17.

`health()` reports `stopped`, `starting`, `recording` or `error`, closed-segment count, observed frame count, frame-progress age, dropped-frame count when FFmpeg reports it, source type and a safe error code. Progress must show advancing frames before `recording`; a living process alone is insufficient. Stopped processes, absent/stalled frame progress, low storage reserve and invalid/corrupt artifacts are visible errors. Failure records preserve UTC observation time and the exception class only. Native stderr remains a private local diagnostic file and is never included in health output; it may contain device names or credential-bearing camera endpoints and must not be published. Failure may leave an unfinished original, which is retained and never promoted to verified footage.

Shutdown first asks FFmpeg to close via `q`, then kills on its bounded wait timeout. If shutdown itself fails, the core reports `camera_shutdown_failed` and retains ownership until the process can be stopped. No source files, originals, pins or failed artifacts are deleted. Session IDs and relative artifact paths are validated, links are rejected, and process execution uses argument arrays with `shell=False` and hidden process windows on Windows.

`preserve(request_id, start_pts_ns, end_pts_ns, event_id=..., run_id=...)` publishes an immutable pin request. The requested interval is recording PTS in integer nanoseconds; convert a note/run time only through that camera's separately validated mapping. Each pin references original SHA-256 values and exact overlapping frame indexes. It reports `preserved`, `partial` or `unavailable` with all unavailable PTS intervals. Frame-duration gaps, missing footage and footage not yet finalized are unavailable; they are never filled with nominal frames. A late note can still be saved independently of this result. Retries with identical request content return the original immutable pin; changed content under the same ID is a conflict. To request a fresh assessment after more footage closes, use a new request identity. Pins are durable artifact references for future retention integration; no retention/deletion policy is implemented here.

`render_pin(request_id)` explicitly creates derivatives, one clip per original segment span, retaining gap information instead of silently concatenating discontinuous footage. It trims by actual selected source PTS and records derivative frame PTS, original hashes and clip hashes separately. Selection includes any source frame overlapping the requested interval, so a derived clip may begin or end up to that frame's duration outside the request. Both the selected frame count and actual relative PTS/gap pattern are independently probed before the derivative manifest is published. Source files are never rewritten. Existing unpublished conflicting derivatives are preserved and require a fresh request identity; retry is idempotent after a verified derivative manifest exists.

Qualification on October 7 uses a project-local, ignored FFmpeg/FFprobe 9.0.2 essentials build. Both tools report `9.0.2-essentials_build-www.gyan.dev`; their respective SHA-256 values are `3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec` and `f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52`. The downloaded ZIP matched publisher checksum `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba` before extraction. No global package was installed or real camera/network input contacted.

Twenty deterministic recorder tests pass. Their fake bytes test the protocol and do not establish native encoding. The separate opt-in `test_recorder_native.py` passes against actual encoded lavfi footage: known 10 Hz input drops source frames 2 and 6 per second, retains actual variable frame PTS, reports the corresponding gaps, finalizes the shutdown tail, reaps the process, preserves original hashes, and generates idempotent derivatives whose independently probed PTS preserve those gaps. This qualifies the named build and generated-input profile, not live camera exposure or UTC alignment. General adapter health still reports live camera qualification false.

Native qualification requires explicit local executable paths:

```powershell
$env:ROBOT_HUB_FFMPEG = 'ABSOLUTE-PATH-TO-ffmpeg.exe'
$env:ROBOT_HUB_FFPROBE = 'ABSOLUTE-PATH-TO-ffprobe.exe'
$env:ROBOT_HUB_FFMPEG_VERSION = '9.0.2-essentials_build-www.gyan.dev'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_recorder_native.py -v
```

The core bounds version/probe output, parsed frame count, and progress/error log growth. Exceeding a bound fails recording visibly; growth checks occur on worker ticks and are not filesystem quotas. This protects memory/diagnostics but does not establish camera throughput or season-scale retention.

Command design references: [FFmpeg segment muxer](https://ffmpeg.org/ffmpeg-formats.html#segment_002c-stream_005fsegment_002c-ssegment), [FFmpeg command options](https://ffmpeg.org/ffmpeg.html), and [FFprobe frame/stream inspection](https://ffmpeg.org/ffprobe.html). These current upstream references describe the candidate command shape; an actual pinned local build must still be qualified.

Remaining T16/T17 work: qualify device disconnection/restart and disk interruption, measure camera exposure/PTS/UTC relationships and alignment residuals (V16), integrate the recorder worker/configuration/API/browser with note/run mapping, validate restart discovery/recovery of unfinished captures. This increment keeps raw manifests on disk but does not load prior sessions into the in-memory `Recorder` automatically. Automatic footage backup is also outside the current T19 catalog-reference contract. Real camera operation, season-scale storage, dropped-frame measurement and DS resource contention remain unqualified.
