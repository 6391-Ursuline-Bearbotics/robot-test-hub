# Saved notes and runs as footage candidates

This workflow finds footage for a saved driver note or run without retyping its robot timestamps. It uses an explicitly selected saved video alignment and an immutable run-catalog revision. The [manual calibration workflow](VIDEO_INVESTIGATION.md) still supplies observed camera/robot cue anchors.

## Operator workflow

Load a saved alignment, then choose **Find footage for saved note** or **Find footage for saved run**. The page displays the catalog revision and exact note revision or run identity being inspected.

A run already has a robot-relative interval in its imported catalog. Its robot and boot must match the selected alignment, and an unknown boot is unavailable. Incomplete runs retain their completeness flags; the footage result does not turn incomplete robot evidence into complete evidence.

A driver note records an intended UTC interval and an estimated time uncertainty. The service expands that interval, finds possible robot times through imported epoch anchors for the selected robot boot, and passes those robot ranges to the camera alignment. Unknown note time, clock quality or uncertainty cannot become a zero-error match. Selecting a particular alignment constrains the robot/boot domain; the result is a candidate association within that domain, not proof of which robot or run the observer meant. Notes remain historical/hub-only observations, without robot acknowledgment or an automatically saved run assignment.

Inspect every candidate. A clock step or recording gap is never bridged with an invented offset. Multiple matching clock pieces are shown as ambiguous; no preferred candidate is silently selected. Note timing, imported clock allowance and video-alignment uncertainty remain distinct. Missing footage and changed original evidence cannot produce a successful inspection.

## Clock and uncertainty model

Catalog clock pieces are the existing importer's pre-split, boot-scoped anchor intervals. Association validates their stored identities, revision, order, timestamp units and bounds. It does not rerun a different clock policy that might merge the pieces. Epoch timestamps and robot timestamps remain integer nanoseconds; inverse interpolation uses exact rational arithmetic and rounds selection bounds outward.

For a piece with multiple anchors, the conservative UTC allowance is the largest stored anchor uncertainty plus one nanosecond for interpolation rounding. Expand the note's intended interval by its estimated uncertainty, then expand possible clock matches by that UTC allowance. Invert the expanded endpoints only inside the piece's nominal anchor coverage and clamp robot endpoints to its supported interval. There is no extrapolation before the first or after the last anchor.

Guaranteed clock coverage under this declared model is more conservative: the piece's UTC interval contracts by the clock allowance at both ends. Requested note time outside that contracted interval is reported as uncovered even when some candidate frames can be selected. A single anchor supplies only its robot point; it supplies no assumed clock scale or surrounding interval. A point outside guaranteed coverage cannot pass merely because a zero-length interval has no measured duration.

The resulting robot interval already contains the note and imported clock allowances. The video core then propagates its own cue/model uncertainty through the camera mapping. The browser shows all three sources of uncertainty and the selected actual frame PTS; it does not add them again as one unexplained error number.

These are conditional piecewise clock and manual camera models. Imported anchor uncertainties, including the import policy's default allowance, are not measurements of the physical clock or camera. A browser note's clock may itself be unverified. Association does not establish physical synchronization accuracy, even if mathematical residuals are small.

## API and evidence pins

`POST /api/v1/video/associate` accepts an alignment identity, revision and exact digest, a `catalog_revision`, and one context:

```json
{"kind":"note","event_id":"saved-event","note_revision":1}
```

or:

```json
{"kind":"run","run_id":"catalog-run-identity"}
```

The service reads that exact catalog snapshot and exact note revision. A later catalog rebuild or note edit does not replace the request's evidence. Associations are read-only inspections; they do not rewrite notes, catalog snapshots, original footage or robot logs. The existing loopback request protections, strict JSON, stopping checks, response limits and original-evidence verification apply. Oversized or malformed evidence fails explicitly rather than truncating the time model silently.

## Qualification

Two Sol authors implemented the backend and browser, and a separate Sol reviewer accepted the final correction. Fifteen author tests and six independent math/actual-HTTP tests pass. They cover exact inverse rounding at large nanosecond values, nonunit scale, conservative coverage edges, single anchors, repeated UTC pieces, unknown clocks/boots, incomplete runs, immutable historical catalog/note pins, changed original evidence, request bounds and nested public metadata projection. Extra stored phase/UTC metadata is excluded from public context without changing the stored catalog.

The full suite on Windows with pinned Python 3.10.7, native Alpha 7 status integration and pinned FFmpeg/FFprobe enabled ran **401 tests in 63.532 seconds: 399 passed, two symbolic-link privilege skips**. Generated recorder and decoded-cue checks passed. No real robot or camera was contacted.

An isolated browser host reused the actual generated 10 Hz black/white recording with source frames 2 and 6 dropped. Its generated catalog used `SIM`/`SYNTHETIC` cycles, one matching boot, a different boot and an explicitly unknown boot. A saved zero-estimate note selected actual frame 3 at PTS 400 ms, displaying its 1,000,001 ns imported clock allowance separately from 10 ms video uncertainty. A 500 ms note estimate selected a wider partial range and showed both imported-clock edges and dropped-frame gaps. The matching run used its saved 300–600 ms robot interval; wrong/unknown boots and an unknown-clock note returned unavailable without stale frame results. Context selectors and candidate result were also inspected in rendered screenshots. These are generated clock and footage checks, not camera timing measurements.

## Remaining work

Selected candidates now have explicit [clip preservation, playback, MP4 downloads and timing sidecars](VIDEO_MEDIA.md), with [original-log downloads](INCIDENT_LOGS.md) scoped to their pinned references. Timeline-linked telemetry/video seeking and actual AdvantageScope application qualification remain incomplete. Camera exposure/cue timing, robot epoch accuracy, client clock error, season-scale archives and physical practice behavior need measurements. The [camera setup worksheet](CAMERA_SETUP.md) records the user-confirmed AW-HE40SWP and pending connection/settings details. No hardware operation or deployment is implied by this software workflow.
