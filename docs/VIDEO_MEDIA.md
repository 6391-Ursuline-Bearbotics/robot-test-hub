# Preserving and watching incident footage

The local service/browser workflow is implemented and independently reviewed. Generated encoded media, actual HTTP downloads/ranges, restart recovery and browser playback have passed. This is software qualification; physical camera timing and actual AdvantageScope application use remain unqualified.

## Workflow

Load the exact saved alignment and inspect a saved driver note, saved run or explicit robot interval. Check the displayed clock uncertainty, missing coverage and possible clock candidates before requesting preservation. Preservation keeps the selected evidence and its calibration/context revisions; it does not assign a note to a run or establish physical synchronization.

The explicit preservation action creates a durable request identity. Retry that same request after an interrupted response; its pinned selection cannot silently become a newer calibration, note or catalog revision. Generation runs in a separate local worker so collection status and the notebook remain usable. No browser polling should start another render.

Clips remain separate across ambiguous clock candidates, original recording segments and coverage gaps. Watch or download the resulting MP4s individually. An unavailable interval stays unavailable even when neighboring footage exists. Original camera recordings and robot logs remain unchanged. A derived clip is an additional artifact with its own hash and timing evidence.

## Local tools

Media generation needs explicit, version-pinned local FFmpeg and FFprobe paths. These are configured independently of camera capture, so the team can inspect an archived session with every camera disconnected. The service does not start a camera to render a historical incident. Private tool configuration stays outside Git; public status reports availability and safe failure codes rather than paths or diagnostic text.

Pass `--video-media-config data/media-tools.json` to the normal hub start command. Omit it to disable new generation; verified completed artifacts can still recover and be downloaded with generation disabled. The private JSON has these fields (replace paths, version and both SHA-256 placeholders with the locally qualified installation):

```json
{
  "schema_version": 1,
  "ffmpeg": "C:/tools/ffmpeg/bin/ffmpeg.exe",
  "ffprobe": "C:/tools/ffmpeg/bin/ffprobe.exe",
  "version": "YOUR-PINNED-VERSION",
  "ffmpeg_sha256": "REPLACE-WITH-64-HEX-DIGEST",
  "ffprobe_sha256": "REPLACE-WITH-64-HEX-DIGEST",
  "operation_timeout": 600,
  "maximum_output_bytes": 1073741824,
  "maximum_total_bytes": 21474836480,
  "minimum_free_bytes": 268435456,
  "maximum_jobs": 100
}
```

The example placeholders deliberately fail validation. Tool paths must be absolute local paths; version and binary hashes are checked. Defaults allow 600 seconds per native operation, 1 GiB per output, 20 GiB in the media derivative archive, a 256 MiB free-space reserve and 100 retained requests. Completed/failed requests count toward that request bound; there is no automatic deletion. Full configured output capacity plus sidecar/receipt overhead is reserved before rendering, which can reject a small clip conservatively near capacity. File growth is checked during rendering; cancellation/reaping can permit transient overshoot, so these are application limits rather than an OS-enforced byte-exact quota.

## Local HTTP contract

`POST /api/v1/video/preservations` accepts a durable `request_id` and a `selection`. An explicit interval selection has `kind: interval`, exact `alignment_id`, `revision`, `sha256`, `robot_id`, `boot_id`, `start_robot_ns`, `end_robot_ns`, `candidate_index: 0`, and optional context `event_id`/`note_revision` (use null when absent). A saved-context selection has `kind: context`, exact alignment identity/revision/hash, `catalog_revision`, `context` (`kind: note`, `event_id`, `note_revision`, or `kind: run`, `run_id`) and the explicitly selected `candidate_index`. The browser constructs this request from the inspected result; it stores and reads back the exact request before POST. Same-ID conflicts are rejected rather than reinterpreted.

`GET /api/v1/video/media-tools` reports cached tool availability. `GET /api/v1/video/preservations` pages receipts, and `GET /api/v1/video/preservations/{request_id}` retrieves one receipt. States are `queued`, `verifying`, `rendering`, `ready`, `failed`, `interrupted` or `unavailable`. Polling does not queue another render. A failed/interrupted request stays as evidence; a deliberate new attempt needs a new request ID. An uncertain HTTP response is retried with the original ID and selection.

Ready items carry opaque identities, MP4 and sidecar hashes, byte sizes, frame counts and selected candidate/segment/group identities. `GET`/`HEAD /api/v1/video/media/{item_id}` serves the MP4; `/sidecar` serves its JSON evidence. MP4 reads support one bounded byte range, including suffix ranges, for browser playback/seek. Files are verified on the exact opened descriptor at response start. This detects prior mutation; external in-place modification during streaming is outside that verification guarantee. The service never modifies its published artifacts.

## Evidence and failure behavior

Preservation pins the exact alignment, imported catalog and note/run or explicit interval. The server resolves originals and computes selected actual frames; requests do not supply filesystem paths, media URLs or authoritative frame offsets. Derived publication requires verification of original hashes, selected frame count and actual presentation timestamps. Timestamp gaps, source provenance, all declared uncertainties and candidate identities remain in the sidecar.

Unfinished outputs are retained as unpublished evidence rather than promoted on restart. Completed requests verify their published artifacts on recovery/download; changed originals or derivatives are conflicts. Rendering has explicit queue, resource and time bounds, and shutdown waits for or cancels/reaps its native child before releasing archive ownership. Cached receipt polling does not hash large files under the service's status lock.

Media endpoints resolve opaque artifact identities, support bounded single-range reads for browser seeking, and expose no private archive path. Downloads are local, explicit operator actions; they do not upload footage or notes elsewhere.

The H.264 MP4 review copies omit audio. Each actual exposure-contiguous group becomes its own clip with normalized PTS; source segment boundaries, missing exposures and candidate ambiguity remain separate. FFprobe verifies frame count, relative PTS and frame durations before publication. Sidecars retain the complete pinned evaluation, original recording/manifest hashes, exact source/derived frame correspondence, tool pins and uncertainty. The `original_logs` section contains catalog/import references when available. Those log bytes are **not reverified for this export and have no download endpoint yet**; a catalog reference is not a downloadable log/video pair.

## AdvantageScope manual alignment

AdvantageScope supports a local video file and manual alignment of its selected frame with the corresponding time in the robot log. Its documented automatic alignment depends on match score overlays; it is not a practice-camera synchronization API. See the [official video workflow](https://docs.advantagescope.org/tab-reference/video/).

The export sidecar supplies the original recording identity/hash, selected source frames and timestamps, derived frame timestamps, pinned mapping and an explicit robot-time cue where the mapping supports one. A cue is labelled `operator_anchor` or `estimated_affine_frame_cue`; the latter is an inverse-model estimate within the calibrated window, with its conservative robot-time uncertainty displayed separately. A null reference supplies no offset. Use the exact original robot log for the selected boot, opened on its own without a merge offset. In AdvantageScope, load the downloaded incident clip, inspect the stated derived-time cue in its cached video, select the corresponding robot-log time, and lock the video timeline. Preserve the sidecar with the clip so another investigator can reproduce the manual alignment.

Source inspection at upstream commit `87abed36429a37a08ea9c04acfa2cfccfd12eba2` establishes a necessary distinction: the [WPILOG worker](https://github.com/Mechanical-Advantage/AdvantageScope/blob/87abed36429a37a08ea9c04acfa2cfccfd12eba2/src/hub/dataSources/wpilog/wpilogWorker.ts) converts binary header microseconds to log seconds; the [historical loader](https://github.com/Mechanical-Advantage/AdvantageScope/blob/87abed36429a37a08ea9c04acfa2cfccfd12eba2/src/hub/dataSources/HistoricalDataSource.ts) may apply a merge offset. The [video controller](https://github.com/Mechanical-Advantage/AdvantageScope/blob/87abed36429a37a08ea9c04acfa2cfccfd12eba2/src/hub/controllers/VideoController.ts) computes lock/playback time from a one-based cached frame number and a reported frame rate. Its cache frame number must not be confused with this hub's zero-based original frame index or actual variable-rate PTS. This source evidence does not qualify the installed application or establish a supported external seek API.

A single manual lock establishes one offset; it does not remove fitted drift or uncertainty. Do not claim every frame in a long clip remains aligned under a nonunit clock scale. Separate short incidents and check additional cues where needed. An ambiguous clock candidate or unknown mapping cannot supply a confirmed event assignment. AdvantageScope's frame cache is intended for match-length videos, so avoid loading an entire practice session as one video.

## Qualification

Two Sol authors implemented the service and browser; an independent Sol reviewer accepted the final work. Thirteen author tests and six independent review tests cover immutable/idempotent requests, gaps, range/HEAD reads, same-size artifact tampering, stopped service, malformed native responses, sidecar allocation limits, uncertainty and cancellation ownership. Review corrected inverse cue uncertainty to use the maximum prediction error at the calibrated window endpoints and corrected storage accounting/reservations.

Two opt-in actual native tests on the locally pinned FFmpeg/FFprobe build verify generated black/white cue footage. One starts source PTS at 5 seconds and drops source frames 2 and 6, then independently decodes the derived cue pixels and checks exact PTS/durations and original hashes. The integrated test preserves eight frames as three clips (2/3/3), downloads them through actual HTTP, checks SHA/HEAD/range/sidecars, restarts with camera and generation disabled and recovers the same artifacts. These tests contact no real robot or camera.

The final full suite on Windows, pinned Python 3.10.7, with native Alpha 7 status and generated-media qualifications enabled ran **422 tests in 73.693 seconds: 420 passed, two symbolic-link privilege skips**. Optional native tests require `ROBOT_HUB_STATUS_INTEGRATION=1`, `ROBOT_HUB_FFMPEG`, `ROBOT_HUB_FFPROBE` and `ROBOT_HUB_FFMPEG_VERSION` set to the qualified local installation.

An isolated browser using generated footage preserved the saved note's candidate as three separate clips. The first clip actually played to its 0.2-second end (`readyState=4`, no playback error). The second showed the estimated cue at robot 0.297 seconds, derived PTS zero and 9,900,001 ns cue uncertainty. Rendered screenshots confirmed the clip controls and cue display. A preliminary browser check also saved an MP4 through the browser, verified its downloaded hash, preserved an explicit point interval and recovered exact requests after reload. Stopping the final isolated host removed playable receipts and disabled actions while retaining the exact browser retry record. All isolated workers joined and archive ownership was released; the user's running hub was untouched.

## Remaining qualification

Actual AdvantageScope application use, camera exposure/clock accuracy, multi-hour operation, disk/resource load, original-log downloads, video backup and retention remain separate work. Log/video-linked seeking in a single hub timeline and automatic visible-cue detection are not implemented. The [camera setup worksheet](CAMERA_SETUP.md) records the pending model and connection details. No robot or camera operation is authorized by this document.
