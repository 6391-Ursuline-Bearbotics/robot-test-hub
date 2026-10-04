# Validation and release evidence

## Test layers

1. Deterministic unit tests for gating, checkpoint ordering, ETA, parsing, time mapping, idempotency, and analyzers.
2. Process tests with local fake servers, interrupted reads, app restart/kill, filesystem faults, and browser controls.
3. Genuine synthetic WPILOG fixtures generated with pinned libraries; compare extractor output to independently known values.
4. Bench testing on the actual SystemCore/USB/DS network, initially with no robot motion required.
5. Supervised physical tests for module comparisons and video timing. Never create dangerous faults merely to validate a detector; use recorded/synthetic faults or approved low-risk setups.

The foundation suite covers the collector model, migration, process ownership, worker lifecycle, and independent status/API access. A green Python suite is not proof of SFTP compatibility, WPILOG validity, real cancellation latency, or a correct hardware diagnosis.

## Foundation validation — October 3, 2026

Local Windows checks used Python 3.10.7. The original demonstration catalog migrated from schema 0 to 1 with its three complete artifacts intact: 37,748,736 bytes, each independently checked against its stored length and SHA-256. Browser checks on the restarted service confirmed durable pause, resume, enabled-state blocking, stale-heartbeat blocking, and return to caught-up after the idle delay. The ETA displays `Paused` while collection is blocked rather than falsely claiming completion. These are synthetic files and controls; no robot was contacted.

Independent review identified a startup recovery transaction that could block saving operator pause while a later archive was being hashed. Recovery now commits short per-file catalog updates outside file I/O, with a regression covering pause during the second startup hash.

## Acceptance matrix

| ID | Scenario | Pass condition |
| --- | --- | --- |
| V01 | Enabled from startup | No listing, read, source hashing, or other bulk work starts |
| V02 | Disable shorter than idle delay | No bulk work; each invalid interval resets countdown |
| V03 | Enable during stalled read | Independent status consumer revokes permission; no next request; measured bounded tail |
| V04 | Enable-disable between polls | Generation change resets idle window and invalidates in-flight block |
| V05 | Stale/future/replayed heartbeat, robot reboot | No transfer; require advancing valid status in correct boot domain |
| V06 | Network loss at offsets including zero/last byte | Resume from durable checkpoint; final hash equals original |
| V07 | Kill process before/after file fsync and DB commit | Correct recovery; extra bytes truncated, missing bytes never assumed |
| V08 | Crash after archive rename | Reconcile final artifact and catalog without duplicate transfer |
| V09 | Rename, replaced path, changed closed hash/size | Stable ID resumes rename; identity conflict quarantined |
| V10 | Hash mismatch/truncated source | No verified original created; visible bounded retry behavior |
| V11 | Disk full, partial corruption, second collector | No lost checkpoint; actionable error; single owner enforced |
| V12 | Vary throughput, pause minutes, add new files | ETA uses active time; labels historical/unknown/incomplete; blocked backlog not hidden |
| V13 | Multiple runs per file, run spanning files, rapid toggles | Correct intervals and many-to-many links; no dropped boundary events |
| V14 | 2026/2027 units, invalid epoch, UTC jump, DST | Correct mapping or explicit uncertainty; never 1000x time error |
| V15 | Offline note, delayed delivery, duplicate retry, reboot | One event, original event time preserved, honest ack state |
| V16 | Known visual cues, dropped frames, camera restart | Measured alignment residuals; gaps and drift represented |
| V17 | Known-good and injected swerve cases | Expected metric values, restricted windows, explainable findings |
| V18 | Missing/stale sensor, changed battery/module/config | Insufficient/incompatible status instead of false pass/comparison |
| V19 | Dirty build, tunable change, module replacement | Exact effective version/history recoverable |
| V20 | Replay regression plus physical retest | Software decisions reproducible; physical claims separately validated |
| V21 | Worker crash, recorder stopped, USB write failure | UI shows failure/coverage gap; no false all-clear |
| V22 | No internet, restart, backup restore | Core workflow works offline and restored catalog/artifacts match hashes |

## Required fixtures

- Timestamp profile fixtures for known 2026 logs and the current Alpha 7 log format, including nested structs and odometry arrays.
- Valid/invalid epoch sequences, one clock jump, two boots with overlapping monotonic values, DST overlap/gap examples.
- Mode transitions including autonomous-to-teleop without disable and abrupt program termination.
- Stable closed manifests, paged manifests, renames, identity conflicts, source disappearance, and large sparse files.
- Swerve constant speed, acceleration, rotation, wrapped steering angles, legitimate optimization flips, known lag, missing samples, and common voltage sag.
- Video with generated visual cues and known PTS anchors; one discontinuity and variable frame cadence.

Synthetic fixtures may be public. Real recordings remain private unless deliberately reviewed for publication. Large artifacts should be generated or supplied through a private fixture path rather than checked into Git.

## Physical transfer benchmark

Record hardware/OS/firmware/library versions, USB model/filesystem, link type, DS configuration, block/prefetch/rate limits, source status rate, and test duration. Capture baseline loop/DS/CAN/logger metrics without transfers, then idle transfers and interrupted transfers at several loads. Record actual source traffic around permission revocation, not only UI state change time.

Publish a commissioning result with p50/p95/worst goodput and cancellation tail, max outstanding bytes, error counts, queue depth, loop overruns, source/local CPU/storage load, and observed impact. Set production limits from measurements. If status transport stalls behind bulk traffic, improve isolation/rate limiting before field use.

## Repeatable physical tests

Define short standard drivetrain tests (straight forward/reverse, strafe, controlled rotation, steering changes) with supervised execution and consistent surface/battery/load context. Log test ID and revision. Never auto-start motion on discovery. Use these runs to establish approved healthy cohorts, then repeat after maintenance.

Shooter tests need a safe existing team procedure and independently observed outcome; a feed command is not a successful shot. Vision tests need independent references, not just the vision-corrected estimator comparing to itself. Preserve simulation-vs-real distinctions.

## Definition of done for a task

Relevant automated tests pass; documentation and migration/rollback behavior match the code; new settings have defaults and validation; error/unavailable states are visible; no private data is committed; implemented/planned status updated. Report local tests, CI, browser checks, simulation, and physical checks separately. No task is hardware-verified without an attached measured result.
