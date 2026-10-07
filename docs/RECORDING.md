# Practice recording (T16)

The hub has an opt-in recording worker, private configuration, cached health/segment APIs and a Practice video page. `robot_test_hub.recorder` supplies the recording core and FFmpeg adapter. Importing modules or starting the hub without `--video-config` never starts a camera. Saving a note does not yet request footage automatically; mapping note/run times and selecting clips remains T17 integration work.

For a camera awaiting installation, use the [camera setup worksheet](CAMERA_SETUP.md) to identify its confirmed input route and commissioning measurements. Camera-specific compatibility requires the exact model and an actual capture test.

## Configure the independent worker

Save private video settings under ignored `data/`. The JSON must include `schema_version: 1` plus the `FFmpegConfig` fields. Executable paths must be absolute and are not discovered automatically. This example uses generated footage only; replace tool paths with your explicitly selected local installation:

```json
{
  "schema_version": 1,
  "executable": "C:\\Tools\\ffmpeg\\bin\\ffmpeg.exe",
  "probe_executable": "C:\\Tools\\ffmpeg\\bin\\ffprobe.exe",
  "version_pin": "9.0.2-essentials_build-www.gyan.dev",
  "camera_id": "generated-overview",
  "input_format": "lavfi",
  "camera_input": "testsrc2=size=64x64:rate=10",
  "source_type": "SYNTHETIC",
  "segment_seconds": 60
}
```

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.server --data-dir data/video-practice --video-config data/video-config.json
```

Open `http://127.0.0.1:6391/video`. The same option can accompany the explicit live-transfer launch. Capture runs independently of enabled/disconnected/stale status and the operator's transfer pause. Notes and cached HTTP views do not wait for media probing. Do not put RTSP credentials in ordinary hub configuration, Git, or shared diagnostic reports: the separate private file is bounded to 32 KiB and its camera input is omitted from API/configuration/backup snapshots.

The worker stores footage below `<hub-data>/video`, assigning a new capture and session namespace for each start. Restart verifies hashes and manifests of closed recordings, retains unfinished/unverified originals, and reports partial recovery or unknown previous lifecycle instead of presenting them as usable coverage. A graceful finalization receipt distinguishes confirmed clean shutdown from an unknown prior process exit. Recovery is bounded to 20,000 discovered entries and 10,000 projected segments; hitting limits is visible, not complete recovery. Parent-process crash recovery of orphan native encoders remains unqualified.

`GET /api/v1/video` exposes redacted cached health, including `health_age_seconds` and frame age that continues advancing while probing or shutdown is blocked. `GET /api/v1/video/segments?limit=20` pages verified summaries with opaque cursors (limit 1–100). These views omit file paths, camera URLs, native stderr and full frame arrays. The browser shows source provenance, capture progress, startup history and recording integrity; hub loss clears its current capture/frame claims. Raw media preview and alignment editing are not integrated yet.

Video is explicitly **not backed up** by the current hub backup worker. Copying a hub catalog alone does not preserve footage. Shutdown retains hub ownership until recording and auxiliary native-process cleanup finish; source originals and incomplete captures are never automatically deleted.

## Recording and preservation core

The core accepts an explicit `FFmpegConfig` containing absolute FFmpeg and FFprobe executable paths, an exact version token, camera identity/input, input format, and `REAL` or `SYNTHETIC` provenance. Supported command shapes are DirectShow (`dshow`), Linux Video4Linux (`v4l2`), explicit RTSP (`rtsp`), or generated FFmpeg input (`lavfi`, necessarily synthetic). No executable search, package installation, camera discovery, automatic network connection, or default external camera is performed. A configured RTSP feed must be on the practice camera network, independently of the robot radio.

Both executables must report the exact configured version token. The session records the token and both executable SHA-256 values. Native availability/version validation is a prerequisite to capture, not evidence that the selected input device or storage has passed commissioning. The named generated-media qualification verifies Matroska/segment muxers and `libx264` on that build; other builds require their own checks. Missing tools/version mismatch fail capture visibly.

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

Twenty-eight focused recorder tests pass. They include synthetic protocol bytes and actual local child-process pipe tests for sustained progress/error output, bounded retention, malformed/oversized records, storage failure, receive-time freshness and retained cleanup ownership. These fixtures do not establish native encoding. The separate opt-in `test_recorder_native.py` passes against actual encoded lavfi footage: known 10 Hz input drops source frames 2 and 6 per second, retains actual variable frame PTS, reports the corresponding gaps, finalizes the shutdown tail, reaps the process, preserves original hashes, and generates idempotent derivatives whose independently probed PTS preserve those gaps. This qualifies the named build and generated-input profile, not live camera exposure or UTC alignment. General adapter health still reports live camera qualification false.

Native qualification requires explicit local executable paths:

```powershell
$env:ROBOT_HUB_FFMPEG = 'ABSOLUTE-PATH-TO-ffmpeg.exe'
$env:ROBOT_HUB_FFPROBE = 'ABSOLUTE-PATH-TO-ffprobe.exe'
$env:ROBOT_HUB_FFMPEG_VERSION = '9.0.2-essentials_build-www.gyan.dev'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_recorder_native.py -v
```

The core bounds version/probe output and parsed frame count. Native capture continuously drains progress from stdout and diagnostics from stderr, including while the recording worker is probing a closed segment. `maximum_progress_bytes` bounds each complete progress block and the retained sanitized latest snapshot in `progress.txt`; an incomplete/oversized block, invalid counter or truncated record fails visibly. The retained snapshot contains frame/drop counters and progress state. `maximum_error_log_bytes` bounds the rolling private tail in `native-errors.log`; individual diagnostic records are bounded too. Ordinary cumulative producer output can exceed these limits throughout a capture without ending it. Storage/write errors remain explicit failures. These native bounds apply during draining; generic protocol adapters retain their existing file-growth checks on worker ticks.

Only advancing valid frame progress refreshes the watchdog, using its monotonic receive time rather than the later worker-consumption time. Duplicate frames, stderr activity and malformed records do not establish fresh frames. Cleanup retains ownership until the capture child is reaped, both drain threads finish and pipes/files close. Private cumulative byte counters and reader counts support qualification; native diagnostic content is absent from public health/API views.

The separate opt-in `test_recorder_stream_native.py` passed in 10.397 seconds on the pinned build, with profile `lavfi-10hz-512-byte-progress-retention-v1`. Actual 10 Hz generated footage used a 512-byte progress limit and 1024-byte error limit: 10 closed segments, 84 observed frames, 1669 progress bytes drained, 36 retained progress bytes, zero stderr bytes and zero live readers after stop. Independently probed PTS, shutdown tail and original hashes remained intact. Run it with the same explicit executable/version environment above:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_recorder_stream_native.py -v
```

This brief generated-input result verifies lifetime output exceeding retained capacity; it does not qualify multi-hour sessions, physical cameras, disk throughput or season-scale retention.

Command design references: [FFmpeg segment muxer](https://ffmpeg.org/ffmpeg-formats.html#segment_002c-stream_005fsegment_002c-ssegment), [FFmpeg command options](https://ffmpeg.org/ffmpeg.html), and [FFprobe frame/stream inspection](https://ffmpeg.org/ffprobe.html). These current upstream references describe the candidate command shape; an actual pinned local build must still be qualified.

Fourteen worker/service tests and a separate actual generated-media service test verify cached redacted HTTP, responsive notes, recovery, lifecycle receipts, independent recording while transfer is paused, and shutdown ownership. The native test recovered seven verified originals with unchanged hashes and retained one unfinished fragment as unverified. Browser checks verified capture health, 20-row pagination, hub-loss state and recovery of 338 generated segments with capture disabled. These are synthetic/local results.

Remaining T16/T17 work: physical device disconnection/restart and disk interruption, measured exposure/PTS/UTC relationships and alignment residuals (V16), note/run mapping and clip workflow, media preview/export, and automatic footage backup. Archive recovery currently hashes historical recordings before starting capture, so a large archive can delay coverage. Active segment metadata and recovered history grow with archive/session size; existing discovery/projection limits are not season-scale qualification. Real camera operation, parent-crash native orphan cleanup, season-scale storage, dropped-frame measurement and DS resource contention remain unqualified.
