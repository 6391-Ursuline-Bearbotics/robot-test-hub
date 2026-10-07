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
| Persistence | Transactional migrations, checkpoints, ownership, durable jobs; opt-in verified backup/restore | Independent failure-domain and external-media qualification; T19 |
| Source | Opt-in NT/SFTP transport, connection reuse and immutable manifest/page checks; loopback integration passes | Confirmed SystemCore access and physical qualification; T12 |
| Gate | Fresh disabled permission, boot/generation, persisted pause, bounded cancellation | Real network cancellation/load measurements; T11–T12 |
| Queue / ETA | Paged discovery, fairness/priority, backoff, independent verification, historical paused estimates | Real link profiles and commissioning defaults |
| Import | Qualified Alpha 7 reader, immutable raw/derived archive, automatic supported-log indexing | Other version profiles and season-scale qualification |
| UI | Transfer queue, diagnostics, run search, offline notebook, recording-quality reports, review/maintenance forms | Phone pairing, robot markers, review history and video integration |
| Robot | T09 identity/status and T10 experimental rotation, explicit SIM/REAL recording selection | USB/load qualification, marker IO and bench run; T08–T12 |
| Analysis | Automatic recording-quality reports, deterministic swerve metrics, explicit cohort approval and maintenance-aware comparison | Automatic swerve/cohort scheduling, plots, REAL freshness; T14–T15 |
| Video | Design and acceptance contracts; recorder core in development | Recorder integration, native/camera qualification and alignment; T16–T17 |
| Packaging | Foreground CLI, pinned timezone dependency, Windows/Linux CI | Autostart, upgrade/rollback and restore qualification; T18–T19 |

Status polling, transfers, local verification and indexing use independent workers. Tests and browser checks establish local behavior; real transport, sensor freshness, recording load, camera alignment and independent backup require their own evidence. No hardware deployment or operation has been performed.

## T00 — Transfer reference prototype

Status: **implemented, local unit tests passed**. Includes fake source, collector, loopback UI, and 16 tests. It establishes observable state/failure behavior. It does not claim any later milestone is complete.

## T01 — Configuration, schema migrations, and worker ownership

Status: **implemented; local Windows acceptance tests passed** (October 3, 2026). Depends: T00. Read ARCHITECTURE, CONTRACTS, UI_AND_OPERATIONS.

Work: introduce validated versioned configuration, data-root ownership lock, explicit database migrations from the current prototype schema, structured/redacted diagnostics, and clean worker lifecycle. Separate collector tick/status/API access so long I/O cannot starve UI/status handling. Keep a working demo entry point.

Acceptance: existing data migrates without losing offsets/pause flag; fresh install works; second process fails clearly without modifying files; unexpected worker failure is visible; shutdown preserves checkpoints; invalid config has actionable errors. Add subprocess tests for ownership and shutdown. No new robot/library dependencies needed.

Evidence: `python -m unittest discover -s tests -v` on Windows, Python 3.10.7: **37 tests passed** (18 collector and 19 foundation tests). Covers exact original-schema partial/complete/error rows and pause preservation, transactional migration rollback/retry, future schema rejection, fresh installation, configuration type/range/NaN/Infinity rejection, root-lock cleanup, second subprocess rejection without file changes, actual foreground Ctrl+Break shutdown/restart with offsets/pause retained, independent status/HTTP pause during blocked reads/discovery/hashing, pause/resume transitions during reads, operation publication after caught-up, worker failures and redaction, rotation bounds, and ownership retention on shutdown timeout. No robot/library dependency added. Existing demo controls and checkpoint durability ordering remain intact.

Limits: synthetic local sources only; Linux CI configuration exists but was not run locally for this handoff. Network-share locks, production transport cancellation, independent verification jobs, service installation, and hardware operation remain unqualified. Diagnostics exports omit arbitrary exception text rather than claiming it can be perfectly redacted. A noncancelable future adapter can retain ownership and delay process exit after a reported shutdown timeout.

## T02 — Production transfer engine and ETA

Status: **implemented; local synthetic transport acceptance tests passed** (October 3, 2026). Depends: T01. Read TRANSFER and source contracts.

Implemented: schema-2 migration adds durable manifest revisions/pages/raw segment metadata, retry/priority/checkpoint metadata, verification jobs, and monotonic transfer events while preserving original offsets, pause settings, and file columns. Production transport calls receive cancellation/generation/deadline context, one bounded page or chunk is outstanding, permission is checked before dispatch and after return, and canceled/stalled calls retain the single slot. Status and operator pause revoke tokens independently. Service verification uses one independent local worker, can overlap later downloads, and continues during enable. Verified digest and format-not-checked remain explicit; import/index/backup state belongs to separate jobs. No source-delete API exists.

Selection continues a segment across chunks, supports audited incident/urgent priorities and checkpoint preemption, and selects the oldest eligible item after two recent selections. Per-file transient faults have persisted bounded jittered backoff and a finite attempt budget; authentication, integrity, immutable identity, and disk-full faults require attention. Missing segments remain visible with their partial bytes. Startup truncates uncommitted tails, validates stored checkpoint digests, quarantines altered bytes, recovers final-checkpoint verification, and reconciles archive rename without duplicate files. Legacy checkpoints establish a first local digest on migration; the mandatory final source digest still applies.

ETA uses a configurable active-time rolling window (service defaults: 15 s window, 2 s minimum), separates transferable/blocked/open/hash-pending bytes and local verification, labels incomplete discovery as a lower bound, and prevents a finite completion estimate for blocked backlog. Paused historical estimates carry observation age/interface profile and expire after the configured age or interface change. API snapshots expose configured/current/maximum outstanding bytes and persist the last successful discovery time.

Evidence: Windows Python 3.10.7, `python -m unittest discover -s tests -p test_transfer.py -v`: **40 tests passed**; `python -m unittest discover -s tests -p test_foundation.py -v`: **19 tests passed**. Together with existing collector cases these exercise V01-V12 in deterministic synthetic sources: enable/stale/future/replayed status and permission generations, blocked/canceled/timeout transport, deadline retry accounting, fsync-before-checkpoint and disk-full recovery, rename/replacement conflicts surviving restart, partial corruption, persisted retries, fairness/preemption, bounded checksum retry, paged/revision-changing manifests, missing/hash-pending backlog, varying active-time rates, paused/expired/interface estimates, queued service controls, and independent verification while enabled. Integrated `python -m unittest discover -s tests -q` passed **134 tests** across the concurrently implemented transfer, notebook, importer, and time/run modules. Source/request cancellation remains a synthetic test result.

Limits: the legacy demo list API is atomic compatibility behavior; production adapters must implement bounded `discover_closed(cursor, limit, cancellation)` pages and cancellation-aware range reads. Client wait deadlines do not terminate a noncompliant transport; no new request is made while its slot remains outstanding, shutdown retains ownership, and real cancellation tails remain T11/T12 qualification. Source path normalization is checked locally; adapter-side root/symlink enforcement, sender permission checks, status-envelope transport parsing, source digest generation, and actual robot performance remain T09-T12. Digest verification does not assert WPILOG format validity; T05 handles format/import validation separately. Startup integrity hashing remains outside write transactions on the collector startup worker; independent status/API pause remain responsive, but transfer startup awaits local recovery. No robot connection, deployment, or physical operation was performed.

## T03 — Queue UI and target API

Status: **implemented; local API and browser verification passed**. Paged queue, per-file actions, source distinction, backlog/ETA/verification status and diagnostics are integrated. Live connection/recovery status is integrated; physical qualification remains T12.

Work: implement `/api/v1/status`, paged transfers, pause/resume/retry/priority actions; distinguish connection/status errors and local verification. Display count/bytes/open bytes, ETA basis, transfer speed, last successful archive, and source type. Keep demo controls exclusively in demo mode.

Acceptance: browser tests for enable/pause/reconnect, stale data, unknown throughput, errors, and incomplete discovery; keyboard accessibility and small-screen layout; HTML injection test using malicious filename; resume never bypasses permission. No real enable control exists.

## T04 — Genuine WPILOG fixture generator

Status: **implemented for the pinned Alpha 7 profile; official-reader and portable checks passed** (October 3, 2026). Depends: T01. Read ROBOT_INTEGRATION and VALIDATION.

Evidence: four tiny public synthetic logs generated with actual AdvantageKit 27.0.0-alpha-6 and WPILib 2027.0.0-alpha-7 on JDK 25. Separate-process regeneration matches every log and manifest byte. Matching official WPILib reader and AK replay checks pass with the documented short-tail iterator limitation. Nine portable tests are included in standard CI discovery. See [fixture tool and exact units](../tools/fixtures/README.md). AdvantageScope GUI opening and older profiles remain unverified; no robot project was modified or run.

Work: create a reproducible generator using pinned Alpha 7/AK source-compatible libraries, optionally in a small isolated Java fixture project. Produce tiny logs with known fields, schemas, nested arrays, metadata, valid/invalid epoch, mode transitions, and gaps. Document generation and expected values. Add a separate older-version fixture/profile for cross-year time tests when exact dependencies or reviewed sample data are available.

Acceptance: opens in the matching official reader/AdvantageScope; independent expected timestamps and units documented; no real private data; does not alter/deploy the robot project. Unsupported older profile remains explicit until tested.

## T05 — Versioned log importer

Status: **implemented as a manual local importer; Alpha 7 profile qualified** (October 3, 2026). Depends: T04. Read ANALYSIS and CONTRACTS.

Evidence: strict portable extraction agrees with the pinned official reader on all 362 physical records in the four genuine synthetic recordings. Twelve extractor and twelve importer tests pass, covering complete short tails, corrupt/truncated data, timestamp units, entry reuse, nested structs/arrays, explicit unsupported fields, disconnected/missing samples, checksum-vs-format separation, immutable archive conflicts, idempotency, and interrupted publication. See [importer contract and commands](IMPORTER.md). Automatic service ingestion and run/UI integration are now implemented; older profiles and real recordings remain unqualified.

Work: choose/pin compatible reader (Python if verified, otherwise small Java extractor), normalize signals with provenance/units, validate format separately from checksum, decode struct arrays and sample timestamps, persist import status/idempotency. Reject unknown profiles visibly. Retain originals byte-for-byte.

Acceptance: V14 unit/type fixtures; known extracted values agree with generator; corrupted/truncated recordings remain marked; rerun creates no duplicate artifacts; missing/disconnected values are not zeros; RealOutputs and ReplayOutputs remain distinct.

## T06 — Run catalog, time mapping, and search

Status: **implemented locally; scale qualification remains open**. Integer time mapping and immutable catalog revisions handle boots, mode phases, file boundaries, unknown clocks and gaps. Browser checked three genuine synthetic runs, DST overlap choices and spring-forward rejection. Verified transfers automatically import/index on an independent worker. See [RUNS](RUNS.md).

Work: sessions/boots/runs/segments and interval mapping, logged and legacy run derivation, valid epoch anchors with piecewise mappings, timezone-aware search, run detail/coverage view. Create revisions when mapping improves.

Acceptance: V13–V14; one run across files and multiple runs per file; auto-to-teleop phase handling; reboot/gap incompleteness; search around 6:52 returns overlapping candidates; DST ambiguity handled; invalid epoch never yields false exact time. Unknown-time runs stay accessible.

## T07 — Hub notebook and idempotent annotations

Status: **implemented; backend and browser checks passed**. Thirteen backend tests cover durable revisions/idempotency/time validation and injection-safe rendering. Run details show overlapping note candidates without claiming an exact robot match. See [NOTEBOOK](NOTEBOOK.md).

Work: immediate marker, seconds-ago/exact interval entry, durable server store, revisions, idempotent event IDs, local timestamps and clock-quality fields, tags, and hub-only historical notes. Future robot delivery uses a separate outbox; do not fabricate acknowledgments.

Acceptance: delayed/repeated requests preserve original event time and create one revision; conflicting retries rejected; note edits retained; unknown run/clock allowed; text safely displayed; action usable without typing. UI says saved in hub, not logged on robot.

## T08 — Offline notebook and optional robot marker bridge

Status: **offline loopback notebook implemented; phone pairing and robot bridge outstanding**. Real browser saved a seconds-ago note while hub was stopped, reloaded offline and automatically synchronized with unchanged incident time after restart. Two JS harness/syntax tests supplement backend coverage. See [offline verification](OFFLINE_NOTEBOOK.md).

Work: browser durable outbox, authenticated LAN pairing, stateful acknowledgment UI; bounded logged robot marker IO with event dedupe and boot-aware delivery. Preserve receipt vs intended event time; historical disconnected notes stay useful.

Acceptance: V15 under browser/hub/robot disconnect and reboot; press 30-seconds-ago then delay delivery 60 s and prove intended time unchanged; no actuator behavior; replay preserves marker inputs; no anonymous LAN control by default. Robot tests/build required for robot changes, no deployment without scope.

## T09 — Robot build/config identity and authoritative status

Status: **implemented in sibling robot project; build and 17 tests passed**. Dirty-source identity, artifact hash, live effective tunable revisions and advancing status are verified. USB write health remains explicitly unavailable. No hardware deployment. See [T09 evidence](T09_IMPLEMENTATION.md).

Work: generated build identity/source snapshot reference, robot/boot/run IDs, config snapshots, mode generation and fresh heartbeat, logger/USB health, optional SIM recording. Keep Alpha 7 API and Commands v3 behavior intact.

Acceptance: different dirty builds distinguishable; tunable change logged; heartbeat advances even in disable while robot program healthy; disconnected/stalled robot cannot be represented as fresh disabled; all robot regression tests/build pass. Document selected NT/status topic contract and source timestamps.

## T10 — Rotating receiver and closed manifest

Status: **experimental receiver implemented in the robot repository; local receiver/replay tests pass, physical qualification outstanding**. See [T10 evidence](T10_IMPLEMENTATION.md) and [live setup](LIVE_TRANSFER.md). Depends: T09 and T04. Read ROBOT_INTEGRATION, TRANSFER, CONTRACTS.

Work: receiver-thread rotation at table boundaries after disabled aftermath window; stable names, full bootstrap/schema/metadata, closed/hashing-ready manifest, orphan recovery policy. Preserve disabled recording and never restart the logger for rotation.

Acceptance: every segment independently decodes; segmented/reassembled replay agrees with continuous reference; no silent missing/duplicated events at boundaries; final hashes/lengths reliable; interruption/write failure visible; source hashing does not block scheduler. Initial testing in SIM only, then T12 bench.

## T11 — SystemCore transport adapter

Status: **implemented locally; actual SSH/SFTP and Alpha 7 NT loopback integration passes** (October 7, 2026). Explicit live CLI composition, pinned host/key authentication, bounded cancellation, durable resume, automatic import and supervised status-reader recovery are implemented. Independent review covers cleanup and permission generation; 15 focused native status checks pass. Killed-reader/in-flight transfer qualification preserves checkpoints and resumes only after fresh advancing status plus idle delay. Current published modules: 255 tests, 254 passed and one Windows symbolic-link skip with native NT enabled. Confirmed bench access and physical measurements remain T12. See [setup/evidence](LIVE_TRANSFER.md). Depends: T02, T09, T10. Read source/permission contracts.

Work: verify SFTP/account/host key/log root and status transport, implement bounded range reads and cancellation, pin credentials externally, advertise capabilities, handle renames/missing files/reconnect. If sender enforcement is unavailable, expose bounded client-only guarantee accurately. No root credential guess, unrestricted path browsing, or delete API.

Acceptance: protocol integration tests against fake/temporary SFTP service; no speculative enable actions; statuses expire despite reachable file server; cancellation measured with instrumented transport; host mismatch is a visible failure. Real access test only with a configured bench endpoint.

## T12 — Physical transfer qualification

Status: **bench worksheet prepared; requires confirmed access and separately authorized robot setup**. See [worksheet](LIVE_TRANSFER_BENCH.md). Depends: T11. Read VALIDATION and TRANSFER.

Work: run the specified load/interruption matrix, collect source/network/DS/loop/USB metrics, choose defaults from measurements, and write a dated commissioning report in the private evidence archive with a public redacted summary if appropriate.

Acceptance: originals match hashes; interruption/restart cases pass; loop/DS performance compared to baseline; actual cancellation-tail/outstanding-byte evidence; throughput/idle budget quantified; unresolved limits documented. Do not label hardware qualified merely because mock tests pass.

## T13 — Analyzer framework and coverage report

Status: **implemented; local deterministic analyzer tests passed** (October 3, 2026). Depends: T05, T06. Read ANALYSIS.

Implemented: `analysis.py` declares analyzer identity/version, supported profile, required/optional signals and units/categories, coverage/windows/grouping/baseline requirements. Immutable evidence references include SHA-256, source type, importer/mapping versions, run/config context and row-content digest. SQLite jobs/reports are idempotent and pinned to declarations/configuration/content; all five outcome states remain explicit. Required missing/invalid inputs yield insufficient data, mismatched profiles/units yield unsupported, and analyzer exceptions are isolated with safe error types. Configured minimum coverage prevents a no-finding outcome below its declaration. Reports list evaluated/unavailable checks and keep overall health not assessed.

Data-quality analyzer checks cycle coverage/gaps/nonadvancing timestamps, invalid/stale/disconnected observations and invalid/unknown epoch coverage. Findings retain immutable hashes, record indices and relevant intervals. Missing physical freshness is not inferred from unchanged imported values. Analyzer code computes numerical metrics; no model-generated values are accepted as evidence.

Evidence: Windows Python 3.10.7, `python -m unittest discover -s tests -p test_analysis.py -v`: **24 tests passed** across framework, swerve core and robot mapping; integrated suite: **160 tests passed**. Tests cover all five outcomes, failure isolation/redaction, exact repeated results/jobs/reports, version/config/content invalidation, required keys/units/category separation, coverage thresholds, input mutation isolation, unknown-clock/gap provenance, and explicit real/simulation distinctions.

Limits: automatic analyzer worker scheduling and report UI integration are separate service work. Initial data quality uses declared imported validity and optional explicit freshness/connection flags; it cannot reconstruct unlogged physical sampling freshness or logger hardware failures. Approved baseline cohorts are not fabricated or automatically selected.

## T14 — Swerve tracking and comparable-run report

Status: **partially implemented; deterministic tracking core and explicit robot mapping tested** (October 3, 2026). Depends: T13. Read ANALYSIS and module telemetry audit.

Implemented: `swerve.py` consumes the explicit `swerve-final-io-si-1` sample contract with final IO optimized/cosine-scaled/desaturated commands, physical module identity/position, connected/fresh/valid flags, sample acquisition times, SI units and source references. It computes time-weighted velocity/steering RMSE, weighted p95, normalized requested-motion error, sustained error intervals, and per-module evidence traces suitable for plotting. Excludes disabled/unknown, disconnected, stale, unaligned, invalid, gap, near-zero-demand and optionally low-voltage intervals, with counted reasons and minimum duration/coverage. Findings require an explicit threshold revision and describe observed tracking rather than a mechanical cause. Current/temperature rankings are absent. Approved baseline comparisons remain explicitly unavailable even when a version label exists without loaded cohort evidence.

Source-reviewed adapter `6391-alpha7-final-swerve-1` maps exact `/RealOutputs/SwerveStates/SetpointsOptimized`, `/RealOutputs/SwerveStates/Measured`, and generated module connection field paths. Inspection of current `Drive.java`, `Module.java`, generated `ModuleIOInputsAutoLogged`, and installed Alpha7 `SwerveModuleVelocityStruct`/`Rotation2dStruct` confirms FL/FR/BL/BR order, post-desaturation/optimization/cosine commands, velocity m/s and angles radians. Adapter requires matching recorded struct schemas and separately supplied physical component assignments; it preserves original/held record references.

Evidence: the **24 analyzer tests** include independently known irregular-sample RMSE/p95/normalization, valid rotation with unequal module velocities, legitimate optimized reversal and wrapped angles, sustained synthetic lag, stale/disconnected/near-zero/gap/voltage exclusions, no current diagnosis, schema/source/time validation, and real held-record mapping versus explicitly ideal simulation policy. Full suite: **160 tests passed**. No hardware measurement or robot code modification was part of this task.

Outstanding: current hardware IO logs connection booleans but no acquisition timestamp/freshness for measured velocity. An AdvantageKit logging-cycle/held value is not a physical measurement timestamp. The adapter therefore marks REAL freshness unknown and the tracking analyzer returns insufficient data; it cannot assert real drivetrain health from the existing fields. An explicitly enabled ideal-cycle policy only accepts source type simulation and remains labeled simulation. Real timestamp/latency qualification, approved comparable-run baseline loading/robust comparisons, cohort exclusions and report plots remain required before T14 is complete. Effort/thermal analysis remains deferred pending signals/context and approved cohorts. T15 owns human baseline approval and component/config history.

## T15 — Baseline approval, maintenance, and review loop

Status: **implemented core and local review UI; acceptance remains partial**. Component assignment revisions, maintenance records, immutable human-approved cohorts, robust comparison, finding disposition and regression reference bundles are present. October 7 independent review adds repair/config boundaries between cohort/observed runs, assignment continuity, overlapping-run exclusion, analyzer-policy matching and pinned comparison history. Focused analysis/review/report suite: 55 passed. Automatic swerve/cohort execution, plots, accessible revision history and physical freshness remain open. See [review evidence](ANALYSIS_REVIEW.md). Depends: T07, T13, T14. Read ANALYSIS and UI_AND_OPERATIONS.

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

Status: **implemented locally; physical backup qualification outstanding**. Opt-in scheduled coherent SQLite/archive capture, verified immutable replication, capture-age UI and clean-directory restore are present. Independent review adds exact generation inventory checks and link rejection for staging/temporary outputs. Synthetic focused suite: 20 passed, one Windows symlink-privilege skip. No independent storage failure domain or power-loss durability is claimed. See [setup/evidence](BACKUP.md). Depends: T01. Read ARCHITECTURE and UI_AND_OPERATIONS.

Work: configurable second destination, verified artifact replication, SQLite-safe snapshots, revision/config backup, offline retry queue, backup lag UI, and clean-directory restore command. Keep external destinations opt-in.

Acceptance: V22 restore reproduces hashes and relationships; interrupted backup retries; no credentials in diagnostics; corruption detected; second copy is a genuinely separate failure domain. A copy beside the original is not backup qualification.

## T20 — Retention and source cleanup

Status: **deferred / off by default**. Depends: T19, T12. Read UI_AND_OPERATIONS.

Work: explicit retention policies, pinning, audit and dry-run previews; robot-source deletion only for old immutable segments with verified local and independent backup copies. Video and derived retention use separate policies.

Acceptance: never deletes active/unverified/pinned/only-copy evidence; interrupted deletion reconciles safely; identity mismatch prevents deletion; policy changes logged; dry-run explains each candidate. No automatic enablement on upgrade.

## Completing a handoff

Update the task status and README/gap table. Record exact commands and environment for tests; link relevant commissioning evidence where applicable. Keep user-facing outcome concise. If blocked on hardware or data, finish the independently testable portion, state the specific missing input, and leave the task partially complete rather than fabricating a successful adapter.
