# Reviewed local checkpoint — October 7, 2026

Latest increment: [incident preservation and playback](VIDEO_MEDIA.md) passed independent Sol review and a full native-enabled Windows run of **422 tests in 73.693 seconds: 420 passed, two symbolic-link privilege skips**. Thirteen author tests, six independent review tests and two actual native-media tests cover immutable requests, gaps, uncertainty, bounded rendering, download/range/HEAD, artifact mutation and recovery with camera/tools disabled. An isolated browser preserved a saved-note candidate into three clips, played the first to its 0.2-second end without error, displayed an estimated 0.297-second robot cue with its separate 9,900,001 ns uncertainty, and cleared playable claims when the host stopped. Rendered screenshots and a preliminary actual browser download were verified. Workers shut down and archive ownership was released; the user's hub was untouched.

Previous [saved note/run association](VIDEO_CONTEXT.md) commit `3a785a1` passed all four Windows/Linux, Python 3.10/3.13 checks ([run 37643150733](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37643150733)). Note timing, imported clock allowance and video uncertainty remain separate. Physical camera synchronization, actual AdvantageScope application use, original-log downloads, a shared telemetry/video seek timeline and video backup remain incomplete. Generated footage never establishes hardware performance.

Published investigation commit `4c2975f` also passed all four Windows/Linux, Python 3.10/3.13 checks ([run 37639883666](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37639883666)).

A test-only follow-up controls UTC and monotonic clocks in the persisted retry deadline regression. An earlier documentation-only commit's Windows/Python 3.13 run had allowed the real one-second deadline to elapse during fixture restart, making a second read legitimate. The repaired test asserts the exact durable UTC deadline, blocks reads before it across restart, and verifies a successful read after it. All 40 focused transfer tests pass; independent review ran the repaired case and accepted it. Production retry policy and backoff are unchanged.

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

The fixture follow-up `fed0157` passed all four Windows/Linux, Python 3.10/3.13 CI jobs ([run 37631208668](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37631208668)).

## Live transfer session reports - October 7

Sol implementation and independent Sol review added `robot_test_hub.transfer_session`, reading only the running hub's local versioned status endpoint. Exclusive private JSONL output contains whitelisted samples and a persisted completion summary. No robot/source connections, controls, identifiers, paths, credentials or arbitrary provider text are included. Queue changes and reported rates remain observations rather than independently measured traffic; boot identity and cumulative durable download bytes are explicitly unavailable.

Sixteen focused tests pass, including an actual temporary HubService HTTP endpoint, direct loopback/proxy/redirect constraints, oversized/duplicate/nonfinite responses, missing/partial/stale observations, source and reader-counter changes, clock reversals, interrupted timing summaries and output failure preservation. Independent review fixed stale interrupted elapsed time and connection cleanup when response close fails. A final-sync regression verifies fixed error guidance and exit 2 while preserving evidence and refusing overwrite. A separate actual CLI subprocess against an isolated temporary hub saved two valid synthetic samples and a matching complete summary; that server shut down cleanly. The user's running hub was untouched.

The final Windows/Python 3.10.7 full suite ran 355 tests in 56.100 seconds: 353 passed and two Windows symbolic-link privilege cases skipped. Native Alpha 7 NT and all opt-in generated-media checks were enabled. These results include the recorder fixture follow-up. No real robot or camera connection, deployment or operation occurred. Physical transfer commissioning still requires confirmed SystemCore access and the bench worksheet's actual USB/DS/load/traffic/cancellation measurements.

CI for `a247d69` exposed a Python 3.13 HTTP response-lifetime difference on Ubuntu: reading the final declared body byte can close the retained socket before the next loop iteration sets its timeout. The reader now recognizes completed response bodies before touching that socket. Deterministic regressions preserve valid JSON, retain malformed-JSON classification, and reject premature closure, including a valid JSON prefix followed by an empty read with declared bytes still missing. Actual loopback tests remain enabled. Eighteen focused session tests pass. With the final production fix, the full local native-enabled suite ran 356 tests in 56.132 seconds: 354 passed and two Windows symbolic-link cases skipped; the additional empty-read regression was then added and passed in the focused suite without further production edits. The final code commit `b2435f6` passed all four Windows/Linux, Python 3.10/3.13 CI jobs ([run 37633077265](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37633077265)).

## SFTP cancellation cleanup ownership - October 7

Sol implementation and independent Sol review resolved source operations returning while cancellation helpers were still alive. Connection and read helpers now finish before their operation releases its lock or outstanding slot. Cancellation is scoped to the helper's client identity, so a delayed old helper cannot detach a successor. Review also found failed closes losing their detached client reference; cleanup now retains that client and retries close at 50 ms intervals until successful, with pending cleanup tracked across global cancellation. Source operation retry/error handling is unchanged.

Twelve focused source tests pass, including blocked connect/read closes, preserved outstanding-byte ownership, delayed old-client cancellation, and both recognized and unexpected close failures. A separate Sol-authored actual HubService and temporary HTTP test holds synthetic cleanup beyond the former join timeout, preserves a 512-byte checkpoint and exact partial bytes, rejects a second archive owner, prevents additional clients/reads, and verifies responsive stopping status with no active rate/ETA. Releasing cleanup allows all workers to exit and ownership to be acquired again. Independent review corrected the test's source timeout to 50 ms so its 500 ms hold actually exceeds the former 150 ms join. Both authors' files were frozen before final validation.

The full Windows/Python 3.10.7 suite ran 362 tests in 59.951 seconds: 360 passed and two Windows symbolic-link privilege cases skipped. Native Alpha 7 NT, actual loopback SSH/SFTP, and generated-media qualifiers were enabled. No real robot or camera was contacted. These checks establish local lifecycle/checkpoint behavior; SystemCore load, physical cancellation tails and hardware commissioning remain unqualified. Persistently blocked or failing native cleanup can keep shutdown pending while ownership stays held.

CI for `6d47cce` passed both Linux jobs and Windows/Python 3.10. Windows/Python 3.13 exposed an unclosed fixture-only SQLite reader during temporary-directory removal: the connection's transaction context does not close its handle. The checkpoint helper now explicitly closes that connection on success or error. Its focused service test passes in 1.228 seconds with unchanged assertions; production files are unchanged by this follow-up.

The final cleanup qualification commit `9a830d7` passed all four Windows/Linux, Python 3.10/3.13 CI jobs ([run 37635892252](https://github.com/6391-Ursuline-Bearbotics/robot-test-hub/actions/runs/37635892252)).
