# Manual footage investigation

This browser workflow connects an explicitly selected **robot time interval** to verified recording frames. It can include an exact saved notebook revision as context. The separate [saved-context workflow](VIDEO_CONTEXT.md) also converts note UTC through imported robot clock anchors or uses a saved run's robot interval. Neither workflow plays footage, preserves a clip, or exports to AdvantageScope. The original footage and robot logs remain unchanged.

## Operator workflow

Open **Practice video → Align and investigate** on the local hub. Recording must have produced closed, verified segments; recording can be disabled while inspecting an existing archive.

1. Choose footage from one camera/session/capture and the corresponding robot identity and boot. A camera restart or robot reboot needs a separate alignment.
2. Find two visible events in the footage and their corresponding robot timestamps in the log. Select the actual indexed video frames. Frame numbers and presentation timestamps come from the verified recording manifest, rather than nominal frame rate.
3. Enter the timing uncertainty and describe the evidence for each anchor. Include cue actuation, exposure, sampling and identification limits. Zero is an explicit assumption, not a measured physical accuracy result. The declared model uncertainty accounts for error beyond the anchors under the assumed continuous affine clock model.
4. Save an alignment revision. Its valid robot interval must stay within the anchor interval. Later changes append a revision with the preceding revision's exact hash; old revisions remain available.
5. Enter an incident's robot time interval. Optionally select a saved driver note. The note's event/submission timestamps and uncertainty remain separate context; selecting it does not establish that the manually entered robot interval is the note's true time.
6. Inspect the selected frame indexes, presentation timestamps, uncertainty and coverage gaps. `mapped` means available frames cover the declared mathematical uncertainty envelope. It does not mean the physical camera has been calibrated or synchronization accuracy measured.

No launch-time UTC offset or guessed constant frame rate fills a missing mapping. Results distinguish selected frames, partial coverage, a recording gap, a time outside calibration and a mismatched clock domain. Missing or changed originals must not produce a successful selection.

## Evidence and service boundary

The service wraps the existing [manual alignment core](VIDEO_ALIGNMENT.md). It resolves footage through the recording worker's known archive identities, validates manifests and original bytes on explicit inspection, and keeps the status snapshot independent of that work. Frames and alignments are paged; requests and selection size are bounded. Camera credentials and recording paths are omitted from responses. Explicit media inspection may hash large originals; season-scale request latency remains unqualified.

Nanoseconds cross the API as canonical decimal strings. Browsers retain them as strings or exact integers. Alignment revisions are immutable, hash-linked files under the private data directory; retries with identical content are idempotent. Inspection pins the selected revision and digest. Notes remain immutable notebook revisions and are never rewritten by footage inspection.

| Local API | Purpose |
| --- | --- |
| `GET /api/v1/video/frames?segment_id=…&offset=0&limit=50` | Verified frame metadata; pages contain at most 100 frames. |
| `GET /api/v1/video/alignments?limit=20&cursor=…` | Paged saved revision identities and hashes. |
| `GET /api/v1/video/alignments/{id}?revision=…&sha256=…` | Reload an exact revision against current original evidence. |
| `POST /api/v1/video/alignments` | Append typed manual calibration, with exact predecessor hash. |
| `POST /api/v1/video/map` | Inspect explicit robot endpoints against a pinned revision, optionally with an exact saved note revision. |

POSTs retain the hub's same-origin, `X-Hub-Request: 1` and JSON requirements. Bodies are capped at 32 KiB, with at most 100 segments, 20 windows and 100 total anchors. Aggregate manifest input is capped at 64 MiB; persisted revision documents and serialized responses are capped at 1 MiB. A preflight rejects more than 10,000 candidate frames before constructing selections. These limits are conservative admission bounds, not season-scale throughput qualification. The browser edits one two-cue window; richer API mappings remain inspectable without silently replacing their calibration through that form.

Response documents omit path fields. The receipt hash identifies the **stored canonical revision**, not the projected browser document, and the server verifies it when reloading. Missing originals return `recording_unavailable`; changed bytes or canonical manifests reject inspection rather than produce successful footage. Publication is serialized under the service's shutdown guard. Shutdown retains archive ownership if publication remains stalled; a request that finishes hashing after shutdown cannot append a revision.

## Local verification — October 7, 2026

Two Sol authors implemented the backend and browser; a separate Sol review accepted both after fixes to request headers, response bounds, stale selection handling and shutdown publication ownership. Thirteen focused investigation tests and five independent actual HTTP tests pass. They cover paging, exact timestamps above JavaScript's safe integer range, frame gaps, wrong boots, pinned revision reload/retry/conflicts, exact historical note context, changed original/manifest evidence, redacted publication failure/retry, request protections and ownership during blocked publication.

The final full suite on Windows with pinned Python 3.10.7, native Alpha 7 status tests and pinned FFmpeg/FFprobe enabled ran **380 tests in 62.536 seconds: 378 passed, two Windows symbolic-link privilege skips**. Existing generated recorder, service, bounded-stream and decoded-cue qualifications passed. No real robot or camera was contacted.

An isolated browser archive used actual encoded 10 Hz black/white cue footage with source frames 2 and 6 dropped. Manual robot-clock cues at 99 ms and 891 ms selected recorded frames 1 and 7, with 10 ms declared video uncertainty. The browser saved revision 1, reloaded it after restarting the isolated hub, and selected frame 3 (400 ms PTS, 100 ms duration) for robot time 445.5 ms. Robot time 247.5 ms produced a gap. The 148.5–841.5 ms interval selected frames 1–6 and exposed both 200–300 ms and 600–700 ms PTS gaps. Saved note revision 1 displayed its separate 500 ms user-estimated uncertainty. Appending revision 2 retained the original bytes and revision 1 hash and linked its predecessor. Loss of the isolated hub disabled frame selection and required revision reload. The user's existing hub was not restarted.

Browser accessibility and displayed text verified these behaviors. Both screenshot methods returned a blank background, so visual appearance remains unverified. Generated clocks/cues and supplied uncertainty are fixture evidence; physical camera alignment, including the <100 ms target, remains unqualified.

Published service/browser commit `4c2975f` passed all four Windows/Linux, Python 3.10/3.13 checks ([CI run 37639883666](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37639883666)). Optional native and generated-media qualifications are the separate local Windows evidence above.

## Remaining work

Saved UTC-note/run candidate association is now available through the [saved-context workflow](VIDEO_CONTEXT.md), including clock inversion, uncertainty, ambiguity and gaps. Timeline-linked media preview, clip preservation, AdvantageScope instructions/export, camera-specific timing measurement and video backup remain separate work. The [camera setup worksheet](CAMERA_SETUP.md) captures the physical commissioning details still needed.
