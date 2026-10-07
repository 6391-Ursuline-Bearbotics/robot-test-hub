# Reviewed local checkpoint — October 7, 2026

This checkpoint publishes completed collection, evidence-analysis/review and backup work. It is not physical commissioning or completion of the full design.

Three independent Sol reviews covered live source transport/status, analysis/cohort/report correctness, and backup/restore. A further Sol review covered robot recording/status/build identity. They fixed and added regressions for unexpected SSH disconnect/reconnect, backup staging inventory/link hazards, repair/configuration/assignment cohort boundaries, overlapping runs/policy mismatch, preserved nonadvancing source cycles, and queued-disabled versus current-enabled rotation.

On this Windows computer with the repository's pinned Python 3.10 environment, installed Alpha 7 and Java 25:

```powershell
$env:ROBOT_HUB_STATUS_INTEGRATION = '1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

246 tests ran: **245 passed, one skipped** because this Windows account cannot create symbolic links. Both native Alpha 7 NT tests ran and passed; actual SSH/SFTP tests use temporary loopback servers and generated keys. Published commit 53cc1b4 passed all four CI jobs on Windows/Linux with Python 3.10/3.13 (run 37619797350). Optional native qualifications remain local Windows results.

The sibling robot's Alpha 7 wrapper build passes **all 32 tests**, zero failures/errors. This includes stepped robot simulation, coroutine regressions, receiver policy/interruption tests, actual recording and independent official-reader replay. The test-generated tracked NetworkTables backup was restored to its pre-test bytes. No deployment, physical motion or real-time simulator GUI run was performed.

Browser qualification used an isolated loopback demo archive populated only with the public synthetic Alpha 7 fixture. The run view displayed a disconnected-observation finding and explicit unavailable swerve readiness. The review page saved a synthetic component assignment and an unresolved finding disposition. Backup disabled/configured views were checked in prior focused qualification. These are functional UI checks; screenshot capture timed out in the desktop browser during this update.

Remaining gaps: real SystemCore endpoints/credentials and USB/DS/load/cancellation measurements; REAL acquisition freshness; automatic swerve/baseline report execution and plots; review-history navigation; phone pairing and robot marker delivery; camera/native recorder/PTS alignment; season-scale archives; independent backup-device and power-loss qualification; autostart/upgrade/rollback. Source deletion remains disabled. See [implementation status](IMPLEMENTATION.md), [live setup](LIVE_TRANSFER.md), [analysis review](ANALYSIS_REVIEW.md), and [backup](BACKUP.md).

The separate T16 recorder-core increment is undergoing review and is not part of this test count or publication checkpoint.

## Live recovery increment — October 7

The live reader now restarts automatically with bounded backoff. Independent review fixed abandoned shutdown cleanup and brief invalid-heartbeat permission reuse. All 15 focused status-reader tests pass with native Alpha 7 loopback enabled. The killed-reader/delayed-SFTP test verifies cancellation preserves its 512-byte checkpoint, a retained replacement heartbeat cannot resume transfer, the full idle delay is required after valid progress, and the final archive/import matches the public fixture.

The current working-tree full suite ran **290 tests: 287 passed, three skipped**, with native NT enabled. This includes 35 tests in separate unpublished recorder/alignment work; it is not the published live increment's test count. Its skips are two Windows symbolic-link privilege cases and the separately opt-in native FFmpeg test. The published modules account for **255 tests, 254 passed and one Windows symbolic-link skip** in that run. Source reader, SSH/SFTP and bridge-failure native checks all ran.

The connection view was checked in an isolated synthetic browser fixture: failed-reader retry/count, stale heartbeat, fresh recovery and idle countdown were visible, with the existing queue and ETA intact. A screenshot was captured during this check. No robot endpoint or camera was used. The [bench worksheet](LIVE_TRANSFER_BENCH.md) records the outstanding hardware measurements.

Published live recovery commit `84bd193` passed all four Windows/Linux, Python 3.10/3.13 CI jobs ([run 37621523898](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37621523898)). The follow-up browser check also verifies that hub loss clears cached heartbeat freshness and active transfer speed.

## Offline live setup check — October 7

The new `robot_test_hub.live_check` command verifies local prerequisites before a live server launch. Nine focused tests passed, including exact host/port pin lookup, bad key/version/configuration cases, redacted reports and CLI overrides. Tests prohibit DNS, socket connection, status-reader launch, SFTP connection and archive opening. A separate check exercised the actual installed locked Alpha 7/Java 25 toolchain with generated local keys under those same prohibitions; all readiness stages passed.

The full working-tree suite ran **322 tests: 320 passed, two Windows symbolic-link privilege skips**, with native NT and generated-media qualifications enabled. Both integrated live NT/SFTP tests and reader-recovery tests passed. This total includes separate unpublished recorder-service work preserved in the working tree; it is not a test count for the live-only publication. No real robot or camera was contacted. The check does not prove endpoint reachability, server authorization, receiver health or physical performance.

## Live launcher preparation — October 7

Sol implementation and a separate Sol review resolved native preparation happening after archive creation. Live configuration now prepares the pinned native reader synchronously, then passes its command/environment to the unstarted supervised bridge. Recognized native failures receive fixed source guidance, including missing Java executables, import/hash failures and compiler timeouts. No archive or endpoint is started on those failures. Independent review also found and resolved mixed source-settings snapshots, duplicate preparation and uncaptured compiler diagnostics in the readiness report.

Focused evidence: eleven readiness tests, three actual server CLI startup tests, six live configuration tests and one portable compiler-output test pass. An actual installed Alpha 7/Java 25 preparation check passed with generated local source settings and network/reader-start/archive operations forbidden. The final full working-tree suite ran **330 tests: 328 passed, two Windows symbolic-link privilege skips**, with native NT and generated-media qualifications enabled. This includes the separate unpublished recorder-service increment. No real robot deployment, operation or connection was performed.

The preceding readiness publication `c7fab30` passed all four Windows/Linux, Python 3.10/3.13 CI jobs ([run 37625214303](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37625214303)). Physical transfer performance and receiver/USB commissioning remain governed by the bench worksheet.

## Recording worker integration — October 7

The previously separate recorder-service increment is now integrated for opt-in use. Sol implementation and independent Sol review covered private configuration, independent worker lifetime, archive recovery, redacted cached health/pagination, capture finalization receipts and retained ownership during native cleanup. Review fixed auxiliary-child cleanup abandonment, frozen cached frame ages, signed64 uncertainty and path-like metadata exposure. Twenty-one core and fourteen service tests pass; the actual generated-media service qualification recovered seven verified originals and preserved an unfinished fragment. The final 330-test run above exercised these same production files.

Browser qualification used only an isolated synthetic source and generated FFmpeg footage. It verified Recording with advancing frames, 20-row Next/First pagination, loss of current capture/frame claims after hub shutdown, and recovery of all 338 locally generated segments with recording disabled and no camera configured. The isolated hosts shut down gracefully; no user hub or robot was restarted. A screenshot of the generated-footage capture view is stored locally outside the public repository.

Video still needs note/run alignment and clip/media/export integration, footage backup, physical timing/device qualification and native-parent-crash cleanup qualification. Capture is disabled by default. The full goal remains incomplete. Live-startup commit `8dd32cb` passed CI ([run 37626728625](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37626728625)).

## Bounded recording outputs - October 7

Independent review found no blocker in the native capture streaming increment. Progress and stderr drain continuously into a bounded latest sanitized snapshot and rolling private diagnostic tail. Progress bounds apply per complete block and retained snapshot, rather than cumulative lifetime output; malformed/oversized records and storage failures remain visible. Advancing frames use monotonic receive-time freshness. Generic protocol-adapter file-growth checks are unchanged, and child/thread/pipe cleanup continues to retain ownership.

All 28 focused core tests passed using the pinned virtual environment. The separate actual generated-media test `test_recorder_stream_native.py` passed in 10.397 seconds against the previously recorded FFmpeg/FFprobe 9.0.2 build, profile `lavfi-10hz-512-byte-progress-retention-v1`: 10 closed segments, 84 observed frames, 1669 progress bytes drained, 36 retained bytes under the 512-byte progress cap, zero stderr bytes and zero live readers after stop. Original hashes, independently probed PTS and final tail checks passed. Local helper tests separately exercise rolling stderr retention and its failures. No physical camera or robot was contacted.

The full Windows suite ran 338 tests in 56.977 seconds: 336 passed and two Windows symbolic-link privilege cases skipped. Native Alpha 7 NT and generated-media qualifications were enabled. Multi-hour practice, physical camera/storage throughput, timing and DS contention remain unqualified. Recovery still hashes historical recordings before capture startup, potentially delaying coverage, and growing segment metadata/history needs season-scale qualification. Note/run alignment, media/export integration, video backup and native-parent-crash cleanup remain open.

CI for `8c16c9e` passed both Linux jobs and Windows/Python 3.10, but Windows/Python 3.13 exposed a short real-child fixture deadline and unsafe temporary-directory cleanup after that assertion failed. The fixture now allows a bounded 20-second wait and retries recorder shutdown before removing its directory. A regression proves cleanup after an intentional assertion failure and temporary shutdown errors. All 29 focused recorder tests pass locally; production capture behavior and flood/retention assertions are unchanged.
