# Implementation handoff

## How to use this plan with Sol or another implementation model

Give the model this repository as its working directory. Ask it to read AGENTS.md, this document, and the task's linked design documents. Assign one task or a small dependency-ready group. Require its acceptance tests and status update. Do not ask it to implement the entire design in one pass.

Suggested prompt:

> Implement T01 from docs/IMPLEMENTATION.md. Read AGENTS.md and the referenced contracts first. Preserve other work. Use the documented defaults; ask only about a decision that actually blocks this task. Add the acceptance tests, run the relevant suite, and update the task status and implementation gap table. Report what is verified and what remains simulated. Do not connect to or deploy to a robot.

For hardware integration, replace the last sentence with the specific authorized bench scope. Do not assume that permission to edit software authorizes deployment or motion. The next model should use the version-pinned robot sources where required.

## Milestones and dependency order

```text
T00 prototype (done)
  T01 foundation (done, local tests) -> T02 production queue -> T03 queue UI
  T01 -> T04 genuine log fixtures -> T05 importer -> T06 run/time catalog
  T01 -> T07 notebook -> T08 offline/robot markers
  T01 -> T09 robot metadata/status -> T10 rotation manifest
  T02 + T09 + T10 -> T11 real source adapter -> T12 bench qualification
  T05 + T06 -> T13 analyzer framework -> T14 swerve report
  T07 + T13 -> T15 review/baselines/maintenance
  T06 + T07 -> T16 recording -> T17 video alignment
  T12 + T15 + T17 -> T18 practice pilot
  T01 -> T19 backup/restore -> T20 retention (later)
```

Useful release A: T01–T07 and T09–T12 (collection, time search, hub notebook). Offline phone support needs T08. Release B: T13–T15 (health reports). Release C: T16–T18 (video and integrated practice). T19 should precede depending on the archive as the only evidence store; T20 is optional and remains off by default.

## Prototype gap table

| Area | Implemented reference | Still required / task |
| --- | --- | --- |
| Persistence | SQLite offsets, fsync ordering, restart recovery, v0-to-v1 migration, process ownership | Jobs, independent verification; T02 |
| Source | Deterministic synthetic source only | Status + immutable manifest + real transport; T09–T12 |
| Gate | Disabled, freshness, boot/generation, durable operator pause/generation, independent cached status worker | Production adapter cancellation/deadlines, negotiated limits; T02/T11 |
| Queue | Newest-first, identity/rename checks | Fairness, complete discovery snapshots, retry policy, missing source states; T02 |
| ETA | Active read/write samples; resets on pause | Historical paused estimate, uncertainty, link profiles, blocked backlog; T02/T03 |
| Checksums | Fake-source digest, local whole-file verification | Source digest generation, async verification, format checks; T02/T05/T10 |
| UI | Loopback demo controls and queue, responsive cached snapshots, worker health, redacted diagnostics endpoint | Target API, real/demo adapter separation, diagnostics UI/search/notebook; T03/T06/T07 |
| Files | Opaque `.logdata` synthetic payloads | Genuine WPILOG validation and raw archive layout; T04/T05 |
| Robot | None | Metadata, marker IO, status, receiver rotation; T08–T10 |
| Analysis/video/backup | None | T13–T20 |
| Packaging | `python -m` foreground CLI, validated JSON configuration, shutdown signals, pyproject, CI workflow | Dependency locking when dependencies are added, service install/autostart, supported restore procedure; T18/T19 |

The transfer worker owns collector operations and publishes cached snapshots before discovery/read/verification. Independent source-status polling and HTTP access stay responsive during synthetic blocked I/O, including durable operator pause. Local verification and startup recovery still run in the transfer worker; independent jobs and production transport deadlines/cancellation remain T02/T11. The OS ownership lock is qualified only on local Windows storage. Tests exercise synthetic source/service behavior, not a real transport.

## T00 — Transfer reference prototype

Status: **implemented, local unit tests passed**. Includes fake source, collector, loopback UI, and 16 tests. It establishes observable state/failure behavior. It does not claim any later milestone is complete.

## T01 — Configuration, schema migrations, and worker ownership

Status: **implemented; local Windows acceptance tests passed** (October 3, 2026). Depends: T00. Read ARCHITECTURE, CONTRACTS, UI_AND_OPERATIONS.

Work: introduce validated versioned configuration, data-root ownership lock, explicit database migrations from the current prototype schema, structured/redacted diagnostics, and clean worker lifecycle. Separate collector tick/status/API access so long I/O cannot starve UI/status handling. Keep a working demo entry point.

Acceptance: existing data migrates without losing offsets/pause flag; fresh install works; second process fails clearly without modifying files; unexpected worker failure is visible; shutdown preserves checkpoints; invalid config has actionable errors. Add subprocess tests for ownership and shutdown. No new robot/library dependencies needed.

Evidence: `python -m unittest discover -s tests -v` on Windows, Python 3.10.7: **37 tests passed** (18 collector and 19 foundation tests). Covers exact original-schema partial/complete/error rows and pause preservation, transactional migration rollback/retry, future schema rejection, fresh installation, configuration type/range/NaN/Infinity rejection, root-lock cleanup, second subprocess rejection without file changes, actual foreground Ctrl+Break shutdown/restart with offsets/pause retained, independent status/HTTP pause during blocked reads/discovery/hashing, pause/resume transitions during reads, operation publication after caught-up, worker failures and redaction, rotation bounds, and ownership retention on shutdown timeout. No robot/library dependency added. Existing demo controls and checkpoint durability ordering remain intact.

Limits: synthetic local sources only; Linux CI configuration exists but was not run locally for this handoff. Network-share locks, production transport cancellation, independent verification jobs, service installation, and hardware operation remain unqualified. Diagnostics exports omit arbitrary exception text rather than claiming it can be perfectly redacted. A noncancelable future adapter can retain ownership and delay process exit after a reported shutdown timeout.

## T02 — Production transfer engine and ETA

Status: **planned**. Depends: T01. Read TRANSFER and source contracts.

Work: cancellation token/status generation, bounded independent I/O, persisted manifest snapshots, current/open/blocked backlog, per-file retry/backoff, explicit local verification jobs, fair priority/aging, startup partial-integrity checks, and time-window throughput estimates that retain a labeled paused estimate. Preserve no-delete behavior.

Acceptance: V01–V12 against a fake transport with controllable stalls and faults; source status updates remain responsive during a blocked read; no duplicate archived artifacts; incomplete discovery/blocked bytes never report a false finish time. Test clock-driven ETA changes and retry starvation. Record outstanding-byte bounds in diagnostics.

## T03 — Queue UI and target API

Status: **planned**. Depends: T02. Read UI_AND_OPERATIONS and CONTRACTS.

Work: implement `/api/v1/status`, paged transfers, pause/resume/retry/priority actions; distinguish connection/status errors and local verification. Display count/bytes/open bytes, ETA basis, transfer speed, last successful archive, and source type. Keep demo controls exclusively in demo mode.

Acceptance: browser tests for enable/pause/reconnect, stale data, unknown throughput, errors, and incomplete discovery; keyboard accessibility and small-screen layout; HTML injection test using malicious filename; resume never bypasses permission. No real enable control exists.

## T04 — Genuine WPILOG fixture generator

Status: **implemented for the pinned Alpha 7 profile; official-reader and portable checks passed** (October 3, 2026). Depends: T01. Read ROBOT_INTEGRATION and VALIDATION.

Evidence: four tiny public synthetic logs generated with actual AdvantageKit 27.0.0-alpha-6 and WPILib 2027.0.0-alpha-7 on JDK 25. Separate-process regeneration matches every log and manifest byte. Matching official WPILib reader and AK replay checks pass with the documented short-tail iterator limitation. Nine portable tests are included in standard CI discovery. See [fixture tool and exact units](../tools/fixtures/README.md). AdvantageScope GUI opening and older profiles remain unverified; no robot project was modified or run.

Work: create a reproducible generator using pinned Alpha 7/AK source-compatible libraries, optionally in a small isolated Java fixture project. Produce tiny logs with known fields, schemas, nested arrays, metadata, valid/invalid epoch, mode transitions, and gaps. Document generation and expected values. Add a separate older-version fixture/profile for cross-year time tests when exact dependencies or reviewed sample data are available.

Acceptance: opens in the matching official reader/AdvantageScope; independent expected timestamps and units documented; no real private data; does not alter/deploy the robot project. Unsupported older profile remains explicit until tested.

## T05 — Versioned log importer

Status: **planned**. Depends: T04. Read ANALYSIS and CONTRACTS.

Work: choose/pin compatible reader (Python if verified, otherwise small Java extractor), normalize signals with provenance/units, validate format separately from checksum, decode struct arrays and sample timestamps, persist import status/idempotency. Reject unknown profiles visibly. Retain originals byte-for-byte.

Acceptance: V14 unit/type fixtures; known extracted values agree with generator; corrupted/truncated recordings remain marked; rerun creates no duplicate artifacts; missing/disconnected values are not zeros; RealOutputs and ReplayOutputs remain distinct.

## T06 — Run catalog, time mapping, and search

Status: **planned**. Depends: T05. Read CONTEXT_AND_VIDEO and CONTRACTS.

Work: sessions/boots/runs/segments and interval mapping, logged and legacy run derivation, valid epoch anchors with piecewise mappings, timezone-aware search, run detail/coverage view. Create revisions when mapping improves.

Acceptance: V13–V14; one run across files and multiple runs per file; auto-to-teleop phase handling; reboot/gap incompleteness; search around 6:52 returns overlapping candidates; DST ambiguity handled; invalid epoch never yields false exact time. Unknown-time runs stay accessible.

## T07 — Hub notebook and idempotent annotations

Status: **planned**. Depends: T01; integrate run linking after T06. Read CONTEXT_AND_VIDEO and CONTRACTS.

Work: immediate marker, seconds-ago/exact interval entry, durable server store, revisions, idempotent event IDs, local timestamps and clock-quality fields, tags, and hub-only historical notes. Future robot delivery uses a separate outbox; do not fabricate acknowledgments.

Acceptance: delayed/repeated requests preserve original event time and create one revision; conflicting retries rejected; note edits retained; unknown run/clock allowed; text safely displayed; action usable without typing. UI says saved in hub, not logged on robot.

## T08 — Offline notebook and optional robot marker bridge

Status: **planned**. Depends: T07, T09; separate offline-only changes if convenient. Read CONTEXT_AND_VIDEO and ROBOT_INTEGRATION.

Work: browser durable outbox, authenticated LAN pairing, stateful acknowledgment UI; bounded logged robot marker IO with event dedupe and boot-aware delivery. Preserve receipt vs intended event time; historical disconnected notes stay useful.

Acceptance: V15 under browser/hub/robot disconnect and reboot; press 30-seconds-ago then delay delivery 60 s and prove intended time unchanged; no actuator behavior; replay preserves marker inputs; no anonymous LAN control by default. Robot tests/build required for robot changes, no deployment without scope.

## T09 — Robot build/config identity and authoritative status

Status: **planned; robot repo task**. Depends: T01. Read ROBOT_INTEGRATION and robot AGENTS.md.

Work: generated build identity/source snapshot reference, robot/boot/run IDs, config snapshots, mode generation and fresh heartbeat, logger/USB health, optional SIM recording. Keep Alpha 7 API and Commands v3 behavior intact.

Acceptance: different dirty builds distinguishable; tunable change logged; heartbeat advances even in disable while robot program healthy; disconnected/stalled robot cannot be represented as fresh disabled; all robot regression tests/build pass. Document selected NT/status topic contract and source timestamps.

## T10 — Rotating receiver and closed manifest

Status: **planned; robot repo task**. Depends: T09 and T04. Read ROBOT_INTEGRATION, TRANSFER, CONTRACTS.

Work: receiver-thread rotation at table boundaries after disabled aftermath window; stable names, full bootstrap/schema/metadata, closed/hashing-ready manifest, orphan recovery policy. Preserve disabled recording and never restart the logger for rotation.

Acceptance: every segment independently decodes; segmented/reassembled replay agrees with continuous reference; no silent missing/duplicated events at boundaries; final hashes/lengths reliable; interruption/write failure visible; source hashing does not block scheduler. Initial testing in SIM only, then T12 bench.

## T11 — SystemCore transport adapter

Status: **planned; hardware facts required for commissioning**. Depends: T02, T09, T10. Read source/permission contracts.

Work: verify SFTP/account/host key/log root and status transport, implement bounded range reads and cancellation, pin credentials externally, advertise capabilities, handle renames/missing files/reconnect. If sender enforcement is unavailable, expose bounded client-only guarantee accurately. No root credential guess, unrestricted path browsing, or delete API.

Acceptance: protocol integration tests against fake/temporary SFTP service; no speculative enable actions; statuses expire despite reachable file server; cancellation measured with instrumented transport; host mismatch is a visible failure. Real access test only with a configured bench endpoint.

## T12 — Physical transfer qualification

Status: **planned; requires bench access**. Depends: T11. Read VALIDATION and TRANSFER.

Work: run the specified load/interruption matrix, collect source/network/DS/loop/USB metrics, choose defaults from measurements, and write a dated commissioning report in the private evidence archive with a public redacted summary if appropriate.

Acceptance: originals match hashes; interruption/restart cases pass; loop/DS performance compared to baseline; actual cancellation-tail/outstanding-byte evidence; throughput/idle budget quantified; unresolved limits documented. Do not label hardware qualified merely because mock tests pass.

## T13 — Analyzer framework and coverage report

Status: **planned**. Depends: T05, T06. Read ANALYSIS.

Work: versioned analyzer declaration/result contracts, idempotent job execution, required signal checks, window eligibility, unavailable reasons, provenance and report generation. Implement data-quality analyzer first.

Acceptance: each of five outcome states exercised; missing keys produce insufficient data; failing analyzer does not block others; reruns pinned to exact versions; report shows coverage and links evidence. No model-generated numerical metrics.

## T14 — Swerve tracking and comparable-run report

Status: **planned**. Depends: T13. Read ANALYSIS and module telemetry audit.

Work: optimized command matching, wrapped steering error, time-weighted tracking metrics, motion windows, robust cohort comparison, event persistence and evidence plots. Start with explicit test IDs and narrow cohorts; effort/thermal analysis follows when signals/context are verified.

Acceptance: V17–V18 fixtures include valid rotation/optimization, lag, stale inputs, battery sag, near-zero speed; no false diagnosis from peer current alone; numerical outputs checked independently; baseline-incompatible data excluded visibly.

## T15 — Baseline approval, maintenance, and review loop

Status: **planned**. Depends: T07, T13, T14. Read ANALYSIS and UI_AND_OPERATIONS.

Work: component/battery identity, effective location assignments, maintenance/config events, immutable approved cohorts, finding disposition, before/after validation links, and a reusable incident regression bundle. Keep baseline updates explicit.

Acceptance: module swap tracked by physical ID; maintenance splits cohorts correctly; healthy baseline cannot silently drift; findings retain old version; review/repair/test chain navigable; no automatic code/config deployment.

## T16 — Practice recording adapter

Status: **planned; camera selection needed only for live setup**. Depends: T06, T07. Read CONTEXT_AND_VIDEO.

Work: recorder interface and first OBS/FFmpeg implementation with pinned-version checks, continuous bounded segments, health statistics, clip preservation around notes/runs, raw/derived artifact links. Mock adapter for CI.

Acceptance: independently generated footage clips correctly by actual PTS; network loss does not stop capture; stopped camera/space errors visible; late notes outside retention say unavailable; original recording preserved; no camera stream through robot network required.

## T17 — Video alignment and investigation view

Status: **planned**. Depends: T16 and T09 for logged cue. Read CONTEXT_AND_VIDEO.

Work: manual sync anchors first, then logged visible-cue detection, drift/discontinuity mappings, measured uncertainty, timeline-linked preview, AdvantageScope export/instructions. Do not depend on undocumented AS automation.

Acceptance: V16 generated PTS fixtures and measured camera test; drift/gaps not hidden; mapping edits revisioned; manual fallback works; selected note retrieves correct clip interval with uncertainty displayed.

## T18 — Integrated practice pilot and release packaging

Status: **planned**. Depends: T12, T15, T17; T19 before relying on archive durability. Read all operational/validation docs.

Work: run a full practice workflow, observe driver burden/false alarms, tune defaults, document startup/end-of-practice procedure, package a supported autostart service with explicit stop/upgrade/rollback, and produce a release checklist.

Acceptance: collector recovers without manual file selection, time search/notes/video/report agree, coverage failures obvious, no driver workflow obstruction, resource limits measured, rollback/restore tested. Do not require every advanced analyzer for this pilot.

## T19 — Backup and restore

Status: **planned**. Depends: T01. Read ARCHITECTURE and UI_AND_OPERATIONS.

Work: configurable second destination, verified artifact replication, SQLite-safe snapshots, revision/config backup, offline retry queue, backup lag UI, and clean-directory restore command. Keep external destinations opt-in.

Acceptance: V22 restore reproduces hashes and relationships; interrupted backup retries; no credentials in diagnostics; corruption detected; second copy is a genuinely separate failure domain. A copy beside the original is not backup qualification.

## T20 — Retention and source cleanup

Status: **deferred / off by default**. Depends: T19, T12. Read UI_AND_OPERATIONS.

Work: explicit retention policies, pinning, audit and dry-run previews; robot-source deletion only for old immutable segments with verified local and independent backup copies. Video and derived retention use separate policies.

Acceptance: never deletes active/unverified/pinned/only-copy evidence; interrupted deletion reconciles safely; identity mismatch prevents deletion; policy changes logged; dry-run explains each candidate. No automatic enablement on upgrade.

## Completing a handoff

Update the task status and README/gap table. Record exact commands and environment for tests; link relevant commissioning evidence where applicable. Keep user-facing outcome concise. If blocked on hardware or data, finish the independently testable portion, state the specific missing input, and leave the task partially complete rather than fabricating a successful adapter.
