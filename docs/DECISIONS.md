# Decisions, assumptions, and open questions

## Accepted design defaults

| Decision | Reason / consequence |
| --- | --- |
| Public repo `6391-Ursuline-Bearbotics/robot-test-hub` | User explicitly selected name and public visibility. Data remains private/local. |
| One dedicated DS laptop collects; Google Drive shares completed originals | User confirmed October 8. Active data stays local; home inspection needs one-way publication. |
| Local-first Python service and browser UI | Fits existing Python analysis work; runs without cloud or internet. |
| Files plus SQLite; optional derived Parquet later | Simple portable operation and reproducible original evidence. |
| Run identity independent of files | Existing recordings span enable/disable cycles; future rotation may split runs. |
| Idle-only bounded resumable transfers | User priority: no bulk collection while operating; save work at interruptions. |
| Explicit closed-segment manifest as preferred target | Stable size/identity avoids unreliable filename/growth heuristics. |
| Custom receiver rotation, separately validated | Pinned AdvantageKit does not rotate on disable. Avoid restarting logger. |
| Original evidence immutable | Notes, mappings, analyzer changes are revisions/derivatives. |
| Human-approved healthy baselines | Prevent gradual faults from becoming normal automatically. |
| Explicit revisioned UTC practice plans and selected cohorts | Assign comparable test/configuration context; edits regenerate reports without changing original evidence. |
| Separate signal, run-catalog and clock revisions | New imports do not invalidate an unchanged signal mapping; results retain each revision independently. |
| Immutable report selection with bounded trace pages | Older results remain investigable while new results arrive; complete traces are reachable without accumulating them in the browser. |
| Provisional tracking thresholds; REAL freshness required | Automatic reports cannot establish physical health from logging-cycle timestamps or invented sample freshness. |
| Recorded Phoenix status warnings separate from physical freshness | SDK errors can precede connection debounce; receipt/transmit times and SDK validity are observations until hardware timing is independently qualified. See [source audit and bench worksheet](SWERVE_STATUS_TIMING.md). |
| Notebook saves independently of robot | Late/offline observations are still valuable. |
| Explicit saved-revision marker delivery with boot/hash pins | Separate contextual receipt; preserve original incident time, never automatically retarget after reboot or claim USB durability. Default off and loopback UI only. |
| Continuous practice video with clip extraction | Late reports and pre-event context survive enable-trigger delays. |
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
