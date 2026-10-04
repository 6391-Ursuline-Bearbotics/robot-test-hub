# Run context, notes, clocks, and video

## Clock model

There are at least four domains: robot monotonic time, collector monotonic time, UTC, and each recording's video presentation time. Browser clocks add another domain. Preserve raw anchors and uncertainty rather than storing only one guessed offset.

For a continuous valid interval fit `utc_ns = scale * (robot_ns - origin_ns) + utc_origin_ns`. Centering avoids floating-point loss on epoch-size values. Normally scale is near one. Use piecewise mappings when UTC jumps or a boot changes. Reject invalid epoch samples and flag implausible discontinuities; do not smear a clock correction across the whole run.

The pinned AdvantageKit logs epoch microseconds; multiply by 1000 to normalize to ns after validating metadata/version. The Alpha 7 official reader returns record time in ns, but the on-disk record header is still microseconds. A raw decoder converts that header once; code consuming the official reader must not convert again. The AdvantageKit `/Timestamp` value itself is ns. Unknown format profiles are unsupported until inspected. A filename date is at most an uncertain fallback.

Collector clock exchanges can provide supporting anchors with round-trip uncertainty; use midpoint estimates only with an explicit error bound. Prefer trusted robot epoch samples when valid and cross-check them. Cache mappings per boot. If no valid mapping exists, index by robot-relative time and show wall-clock unavailable. When later evidence permits a mapping, create a new revision and reindex without changing raw observations.

Search accepts an exact date/time or an interval. Convert timezone-aware local input to UTC before interval-overlap queries. For `6:52 PM last Tuesday`, the UI must resolve and display the calendar date/timezone and default to a neighborhood (e.g. ±2 minutes), not claim the driver's memory has second-level accuracy. During a DST overlap ask which occurrence or return both with clear offsets; reject nonexistent local times rather than guessing.

## Run segmentation and context

Prefer logged run IDs. For legacy logs derive transitions from version-specific driver-station state with completeness flags. Do not merge two enabled intervals because disable was brief. Auto-to-teleop can be phase boundaries within one enabled interval. Preserve disabled intervals for startup, fault aftermath, and maintenance context.

A run references all overlapping raw segments. Querying an interval returns adjacent context, notes, findings, video, build/config, and battery. Open or incomplete runs are searchable with provisional endpoints. Reboots, gaps, and unknown state are visible in the timeline.

## Notebook interaction

Primary action: **Mark event**. One action durably stores the current client event and requests a clip window. An operator can attach text after stopping. Optional shortcuts: now, 10/30 seconds ago, exact time/range; drive/shooter/intake/vision/other; severity optional. A controller/button binding can create a robot-timestamped marker with no typing.

Record author/device, submission timestamp, intended event interval, original user input, clock quality, tags/text, run candidates, and marker delivery state. Do not force a confident run assignment where mapping is ambiguous.

The browser client needs an offline outbox (e.g. IndexedDB) before claiming offline note support. Persist UUID and payload before HTTP send. Server idempotency prevents duplicates. After hub acceptance, the hub independently queues optional robot delivery. UI states: `saved on device`, `saved in hub`, `robot acknowledged`, `historical/hub-only`, `needs time clarification`. Local success is not robot acknowledgment.

Edits append revisions; historical logs remain untouched. Repeated retries preserve the original submitted/event times. Retrospective notes must remain possible when the robot is powered off. A note submitted while connected to a different boot must retain its original event context.

Suggested bounds: 4 KiB text, 20 tags, explicit request/body limits, debounced hardware button. Store richer attachments in the archive rather than sending them through NetworkTables. Escape notes in HTML and report exports.

## Recording design

Start with one fixed overview camera connected to a practice computer. Capture independently of the robot radio. Choose an existing camera first; camera model/protocol, audio needs, exposure/lighting, and storage are open commissioning questions.

Record the practice session continuously in recoverable, bounded segments. Enable/disable events and notes define clips, not the only moments when acquisition runs. Retain footage before and after runs and collisions. A replay buffer is optional for quick incident saving, but it does not replace sufficient continuous retention for late observations.

Adapter interface: start/stop session, list recording segments, request preservation of a time range, get capture health, report file/PTS metadata and dropped frames. OBS WebSocket is the first prototype candidate; FFmpeg can create/share clips. Use the installed version's documented API. Do not assume transport start time equals first sensor exposure time.

Store original videos plus generated clip artifacts separately. Track camera ID, container/codec, duration, presentation-time basis, estimated recording UTC, gaps, and recording statistics. Use actual frame PTS rather than `frame_index / nominal_fps`, especially for variable-rate or dropped-frame footage.

## Synchronization

1. Establish a coarse UTC association from recorder and robot clocks.
2. Log a distinct robot LED sequence or equivalent observable cue with event ID and robot monotonic timestamps.
3. Detect the cue in the video and pair it with robot events; allow manual anchor placement.
4. Use multiple well-separated anchors to estimate drift and detect dropped/discontinuous recording intervals.
5. Save a revisioned mapping from robot time to video PTS with residuals, uncertainty, method, and operator overrides.

The LED output command time itself has actuation/sampling delay. Measure cue timing error with the actual camera/LED; do not call the result frame-accurate without validation. Aiming target for basic practice review is <100 ms measured alignment error under tested conditions, with manual fallback. This is a target, not an achieved result.

AdvantageScope's documented automatic synchronization is for match video with score overlays. Practice clips therefore need our alignment data and initially a documented manual lock step. Generate match-length or incident-length clips because AdvantageScope caches frames. Do not promise an unsupported deep link or API that sets its video offset. Research and verify any automation separately.

The hub can preview aligned video at a selected event and provide the original log/clip plus alignment instructions. Do not crop/rewrite a raw WPILOG merely to align it; a valid derived clip/export is a separate artifact if a later reader supports that operation.

## Preservation and failure behavior

- Late note inside retained footage: create/pin the incident clip.
- Late note outside retention: save the note and display footage unavailable.
- Camera missing/stopped: show a capture gap; do not infer no incident occurred.
- Robot network outage: camera and local notebook continue independently.
- Clock unsynchronized: record now, mark mapping uncertain, reconcile later.
- Multiple cameras: separate clock mappings; never reuse one camera's offset for another.
- Session ends: finalize recordings, verify files, then allow retention policy to remove unpinned expendable footage only after configured safeguards.

Avoid retaining video indefinitely by default. Decide a retention policy with the team, including who can view recordings and whether audio is needed. No cloud upload is implied by recording.
