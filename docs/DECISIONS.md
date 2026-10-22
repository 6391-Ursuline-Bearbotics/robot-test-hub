# Decisions, assumptions, and open questions

## Accepted design defaults

| Decision | Reason / consequence |
| --- | --- |
| Private executable analyzer cases separate from reference bundles | Exact review/input/configuration pins and genuine original-log reimport proof precede fresh allowlisted execution. Explicit expectations can fail; a passed analyzer case is neither robot-code replay nor physical repair validation. |
| Deterministic full-source release and synthetic rehearsal | Read committed local Git blobs; verify exact inventory before fresh extraction. Exclusive manifest-last publication preserves conflicting/partial outputs. Checksums are integrity receipts, not signatures. Upgrade/rollback executes trusted extracted versions only against disposable synthetic evidence. |
| Portable full-snapshot summary explorer | Saved HTML embeds existing summary evidence and fixed CSP-hashed code, with exact UTC candidates and bounded pages. Filters preserve cohort boundaries; presentation upgrades reuse numeric caches and retain old report bytes. |
| Separate offline installation exercise | TestHub uses a bundled digest-locked synthetic sample and fresh temporary archive. CheckHub retains static live-readiness checks; neither replaces physical commissioning. |
| Public repo `6391-Ursuline-Bearbotics/robot-test-hub` | User explicitly selected name and public visibility. Data remains private/local. |
| Commission log collection and home access before cameras | User confirmed October 9. Camera, PoE and camera network configuration are deferred; use [log flow checklist](LOG_FLOW_COMMISSIONING.md). |
| One dedicated DS laptop collects; Google Drive shares completed originals | User confirmed October 8. Active data stays local; home inspection needs one-way publication. |
| Full-source Windows install with saved private profiles | Setup creates a per-install environment; daily wrappers select the source root and never infer a live endpoint or fall back to demo. Authenticated local Stop requests worker cleanup without killing processes. |
| Opt-in current-user Windows sign-in startup | XML preview is the default; installation requires an explicit command and offline readiness. No immediate start, stored password, elevation or failure restart. Busy scheduled instances must finish graceful StopHub cleanup before replacement/removal. |
| Stable OS lease for launcher crash recovery | Only a recognized schema-2 runtime is reclaimed under the exclusive lease. Legacy/ambiguous records stay manual; no PID killing or port-liveness ownership guesses. Pending shutdown retains the service and both leases through process exit. |
| Local-first Python service and browser UI | Fits existing Python analysis work; runs without cloud or internet. |
| Files plus SQLite; opt-in Parquet and DuckDB derivatives | Main laptop performs detailed analysis; original evidence remains authoritative and writable databases stay local. |
| Separate download, import, searchable-run and report status | A caught-up queue does not imply usable data. Bounded catalog-only projections expose attention without source requests; report lookup can be explicitly unavailable under its resource budget. See [processing status](PROCESSING_STATUS.md). |
| Explicit exact-attempt local import retries | Retry eligible current I/O/extractor failures from retained originals, with durable deduplicated receipts and worker recovery; transfer retry stays separate. See [import retry](IMPORT_RETRY.md). |
| Run identity independent of files | Existing recordings span enable/disable cycles; future rotation may split runs. |
| Idle-only bounded resumable transfers | User priority: no bulk collection while operating; save work at interruptions. |
| Explicit closed-segment manifest as preferred target | Stable size/identity avoids unreliable filename/growth heuristics. |
| Custom receiver rotation, separately validated | Pinned AdvantageKit does not rotate on disable. Avoid restarting logger. |
| Original evidence immutable | Notes, mappings, analyzer changes are revisions/derivatives. |
| Human-approved healthy baselines | Prevent gradual faults from becoming normal automatically. |
| Explicit revisioned UTC practice plans and selected cohorts | Assign comparable test/configuration context; edits regenerate reports without changing original evidence. |
| Separate signal, run-catalog and clock revisions | New imports do not invalidate an unchanged signal mapping; results retain each revision independently. |
| Read-only review history with exact revision pins | Stable append snapshots reach older records; embedded bundle evidence stays preserved and unpinned maintenance never resolves silently to latest. See [review history](REVIEW_HISTORY.md). |
| Source-verified streaming quality/status fallback without replacing small-run analysis | Automatic pipeline-7 overflow now queues separate stream11 work; explicit policies 5–10 and their saved results remain unchanged. Pipeline 8 keeps its original quality-only checkpoints and pipeline 9 keeps recorded-status mapping. Pipeline 10 adds separately versioned tracking, exact persistent weighted quantiles and explicit scalar-input selection. Full-byte EOF verification and bounded reducer finalization precede publication; pipeline 11 adds bounded recorded steering dynamics and strict recorded build/configuration admission, with no physical-freshness qualification. See [STREAMING_RUNTIME.md](STREAMING_RUNTIME.md). |
| Maintained schema-9 run/time lookup | New catalog publications index atomically; writer-only resumable backfill preserves exact legacy run bytes. Read-only search/selection uses bounded indexed rows, with unavailable rather than zero for incomplete indexes. Integer uncertainty and pinned note revisions remain exact. See [RUN_LOOKUP.md](RUN_LOOKUP.md). |
| Maintained schema-7 report provenance lookup | Atomic writer metadata and resumable legacy backfill; unknown totals stay unknown while valid known references remain reachable. See [lookup contract](REPORT_LOOKUP.md). |
| Immutable report selection with bounded trace pages | Older results remain investigable while new results arrive; complete traces are reachable without accumulating them in the browser. |
| Provisional tracking thresholds; REAL freshness required | Automatic reports cannot establish physical health from logging-cycle timestamps or invented sample freshness. |
| Recorded Phoenix status warnings separate from physical freshness | SDK errors can precede connection debounce; receipt/transmit times and SDK validity are observations until hardware timing is independently qualified. See [source audit and bench worksheet](SWERVE_STATUS_TIMING.md). |
| Recorded steering dynamics separate from physical freshness | Stable-step settling and censored windows, hysteresis crossings and recorded angle changes use exact logged references; optional criteria are revisioned. Current pipeline 7 preserves historical 5/6 evidence, and REAL descriptive results never invent acquisition freshness. See [STEERING_DYNAMICS.md](STEERING_DYNAMICS.md). |
| Source-qualified SystemCore observations with versioned selectors | Exact Alpha 7 AdvantageKit input paths, held logger states and exact int64 changes support recorded warnings and descriptive voltage only. Current pipeline 6 adds a fourth check; fixed pipeline-5 case inputs remain pinned and evaluator-version changes require explicit opt-in. See [SYSTEM_STATUS.md](SYSTEM_STATUS.md). |
| Snapshot-pinned run-note candidates | Bounded pages deduplicate events across mapped clock pieces and retain exact saved revisions until explicit refresh. Unknown run clocks mean unavailable association, never a claim of no notes. |
| Notebook saves independently of robot | Late/offline observations are still valuable. |
| Explicit saved-revision marker delivery with boot/hash pins | Separate contextual receipt; preserve original incident time, never automatically retarget after reboot or claim USB durability. Default off and loopback UI only. |
| Continuous practice video with clip extraction | Late reports and pre-event context survive enable-trigger delays. |
| Direct LL4 Rewind as a separate opt-in source | Laptop issues save/download HTTP requests directly; SC supplies only existing status. Preserve raw groups and weak-identity/timing limitations. See [LL4 setup](LIMELIGHT_REWIND.md). |
| Available practice camera: Panasonic AW-HE40SWP | User supplied the exact model. Trial documented RTSP capture first; firmware/settings, connection and measured timing remain commissioning work. |
| Explicit original-log downloads from incident references on loopback | User approved archived WPILOG delivery to the browser on this computer. Pins and original bytes are verified; no external uploads or broader network exposure. |
| No automatic robot deletion initially | Backup and retention must be proven separately. |
| Human reviews proposed fixes | AI may summarize evidence; no automatic deployment/tuning. |

These are implementation defaults, not claims that all features exist. Change a decision by documenting evidence and consequences here. Do not block routine tasks waiting for answers to hardware questions they do not depend on.

## Questions needed before real commissioning

| Question | Needed for | Work that can proceed now |
| --- | --- | --- |
| Dedicated DS laptop confirmed; local SSD and Drive folder paths? | School configuration | Opt-in export, synthetic validation and setup guide |
| Actual SystemCore image, SFTP account/host key, USB mount/filesystem? | Real adapter and rotation deployment | Adapter interface, failure injection |
| Which authoritative Alpha 7 status transport is accessible? | Fresh permission / generation mapping | Contract and synthetic heartbeat tests |
| Typical log growth and idle connection throughput? | Rate/chunk defaults, retention sizing | Configurable ETA and benchmarking harness |
| LL4 OS version, direct HTTP origin and recording-list size semantics? | Direct Rewind archive commissioning | Implemented bounded collector and synthetic protocol/failure tests |
| Camera already available? USB/RTSP/other? | Recorder adapter and exposure/latency validation | Video manifest/alignment algorithms with generated footage |
| Supported power device/bus/ID and component labels? | Electrical telemetry and physical baselines | Explicit missing-data behavior |
| What backup destination and retention period? | Automatic replication/cleanup | Local archive, hashes, backup interface |
| Notebook devices/auth requirements on practice LAN? | LAN access and offline client | Loopback UI, idempotent note model |
| Access to representative 2026 and Alpha 7 recordings? | Parser compatibility and baseline exploration | Genuine synthetic fixtures using pinned tools |

Avoid collecting passwords in chat or committing them. Confirm access locally through credential references and pinned host identity.

## Source references and limitations

Local source inspection is authoritative for the project's pinned alpha versions. Checked files: robot `Robot.java`, `build.gradle`, `BuildConstants.java`, `ModuleIO.java`, `ModuleIOTalonFX.java`, `ShooterHoodIOServo.java`; installed AdvantageKit source classes `WPILOGWriter`, `Logger`, `LoggedSystemStats`, `LoggedPowerDistribution`. Findings are recorded in ROBOT_INTEGRATION. Revalidate after upgrades.

- [SystemCore AdvantageScope integration](https://github.com/wpilibsuite/SystemcoreTesting/blob/main/AdvantageScope.md): alpha integration documents SFTP log download; status layouts/features evolve.
- [AdvantageScope video](https://docs.advantagescope.org/tab-reference/video/): local files, score-overlay-based automatic match synchronization, manual alignment, frame-cache limitations.
- [OBS WebSocket protocol](https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md): recording/replay-buffer controls; use installed-version compatibility checks.
- [AdvantageKit traditional replay](https://docs.advantagekit.org/getting-started/traditional-replay/): modified outputs do not alter recorded inputs.
- [AdvantageKit nondeterministic inputs](https://docs.advantagekit.org/getting-started/common-issues/non-deterministic-data-sources/): dashboard and other external inputs must be captured for accurate replay.

Stable web documentation can describe older units and APIs. In particular, do not copy default power-distribution initialization or timestamp assumptions from stable documentation into this Alpha 7 project without checking the pinned library source.

## Deferred complexity

Distributed collectors, a cloud telemetry database, automatic tuning, learned fault classifiers, live video computer vision for scoring, automatic AdvantageScope seeking, and full event-network deployment are not prerequisites for the first release. Preserve extension points; do not implement them speculatively in early tasks.
