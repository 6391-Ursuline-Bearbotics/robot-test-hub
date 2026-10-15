# Software and commissioning audit — October 15, 2026

This audit inspected production code and targeted test coverage at published 9fc1235. It did not run hardware, establish physical health or prove the full goal complete. The prior frozen local suite ran 932 tests: 911 passed, 21 optional skips; exact-head hosted CI is recorded separately in VALIDATION_CURRENT.md.

| Requirement | Existing software evidence | Completion boundary |
| --- | --- | --- |
| R01 idle-only collection | collector.py, source_status.py; transfer/status tests exercise fresh disabled permission and full idle delay | Real network cancellation tail and robot/DS load unmeasured |
| R02 recovery | Collector fsync/checkpoint ordering, quarantine and restart/rename recovery; transfer/live/process tests | Actual USB/filesystem/reconnect and power-loss behavior unqualified |
| R03 queue/ETA | transfer.py, queue_api.py and processing projections distinguish open/blocked/digest/verification work and historical paused estimates | Measured link profiles and sustainable idle budget needed |
| R04 run separation | runs.py and run/pipeline tests preserve cross-file runs, phases, boot/gap boundaries | Season-scale serving and genuine collected-log qualification remain |
| R05 time lookup | timebase.py integer mappings/DST/uncertainty tests and run search | Actual clocks unqualified; selected-run notes truncate, fixed by RUN_NOTES increment |
| R06 notes | notebook.py, browser IndexedDB/outbox, boot-pinned explicit markers and independent tests preserve original intended times/revisions | Selected-run candidate pagination incomplete at audit; phone pairing is deferred; physical delivery still unqualified |
| R07 video context | Independent recorder, revisioned manual alignment, pinned context and generated native media tests | Camera measurement, shared seeking and automatic cues unfinished; LL4 archives lack playback/alignment integration |
| R08 comparable analysis | swerve.py, immutable approved baselines, maintenance-aware assignments, weighted statistics and generated-session regressions | REAL acquisition freshness and fault thresholds unqualified; additional subsystem analyzers missing |
| R09 exact context | Robot identities/config snapshots, recorded build/config hashes and pinned review context | Human-declared/missing context remains unverified; references do not archive build binaries/source automatically |
| R10 improvement/regression loop | ReviewStore links findings, changes and later jobs; immutable reference bundles preserve exact evidence; executable analyzer cases added after this audit | Robot-code replay and compatible later physical repair validation remain incomplete; analyzer-case success is a separate software check |
| R11 failures visible | Worker, transfer/import/retry, recording gap, backup/export state and UI tests | Log-first trial has no identified prerequisite visibility-code blocker; actual operator comprehension/load remain untested |
| R12 offline/private | Local runtime, private saved profiles, native wrappers, isolated exercise and portable summary assets | School policy/installation and actual Drive/home synchronization require qualification |

## Concrete remaining software work

1. **Resolved locally after this audit:** complete run-associated note navigation now has exact snapshot/revision pins and bounded read-only pages. See [RUN_NOTES.md](RUN_NOTES.md) for implementation and executed evidence; physical clock qualification remains separate.
2. **Partly implemented after this audit:** [executable analyzer cases](REGRESSION_CASES.md) preserve exact reviewed incident inputs, prove their relation to original logs and re-run allowlisted hub analyzers against explicit expectations. Existing regression-reference-1 bundles remain references. Robot-code replay and compatible later physical repair validation still need implementation, including change/timing/context rules; analyzer-case success does not close R10.
3. **Implemented locally after this audit:** reproducible committed-source assembly, verification/extraction and disposable cross-release upgrade/rollback through verified restore. See [RELEASE_ASSEMBLY.md](RELEASE_ASSEMBLY.md). School installation and real archive/OS lifecycle qualification remain open; a new OS service is not a log-first prerequisite.
4. Extend the analyzer set in explicit source-qualified increments: steering settling/oscillation/discontinuities; matched-workload effort/thermal; electrical brownout/sag/reset clusters; autonomous timing/path errors; shooter spin-up/recovery/readiness; intake/indexer jam/recovery; vision rejection/age/corrections. The automatic registry currently contains recording quality, swerve tracking and Phoenix status observations.
5. Address archive/index serving scale without unbounded current-catalog decoding or repeated whole-history work. Use measurements and maintained indexed metadata; do not infer season readiness from small fixtures.
6. Complete optional video software: generic continuous-video/media backup, LL4 decoded playback derivatives and explicit clock/run/note integration, shared investigation seeking and measured cue paths. Rewind grouped originals are backed up; generic recorder/preserved media are not.

Historical importer profiles, arbitrary high-frequency event querying, authenticated phone pairing and approved retention remain separate tasks. Automatic source deletion stays off. Do not introduce distributed collectors, cloud telemetry databases or automatic tuning merely to fill this list.

## Actual lab dependencies

T12 needs confirmed SystemCore endpoints, USB path/filesystem, read access and host identity, then the documented idle-transfer/interruption/load trial. T18 needs school installation/sign-in/sleep/logoff and a supervised practice workflow. T19 needs an independent storage failure domain and restore trial. Drive needs a school-to-home round trip with actual upload confirmation. Physical healthy baselines, clocks and camera alignment require measured independent evidence. These gaps prevent goal completion; they do not prevent the software increments above.

The [log-first commissioning checklist](LOG_FLOW_COMMISSIONING.md) remains the operational entry point. Camera work remains optional for that trial. Public checkboxes remain blank; save measured results and real telemetry privately.
