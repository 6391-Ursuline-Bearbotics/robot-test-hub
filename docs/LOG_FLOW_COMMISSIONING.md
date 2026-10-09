# Log flow commissioning checklist

Start with one dedicated driver-station laptop collecting robot logs, producing summaries, and sharing originals and reports through Google Drive. Camera recording, PoE equipment, camera IP addressing, and video alignment are deferred.

## What the school needs to provide

- [ ] Identify the dedicated Windows x86-64 DS laptop and an operator for the first bench session.
- [ ] Confirm local SSD free space for originals, extracted data, Parquet, and temporary processing. Record available space before and after the trial; the school's Drive allocation does not increase laptop capacity.
- [ ] Provide access to the existing working robot network. Confirm the robot's SSH/SFTP host and port and its authoritative NetworkTables host and port. A new camera switch or camera subnet is not a prerequisite for this trial.
- [ ] Confirm the SystemCore image/version, mounted writable USB log directory, and recording filesystem.
- [ ] Arrange a collector account/key authorized to read that recording directory, and independently verify the SSH host-key fingerprint. Configure credentials locally; do not send private keys or passwords in chat.
- [ ] Provide a team-controlled Google Drive folder writable from the laptop and readable by the home account. Install/sign in to Drive for desktop locally and confirm the actual Windows folder path.
- [ ] Keep the team's normal robot-operation and deployment procedure available for the bench session.

The first configuration details to collect are the laptop/archive location, robot endpoints, USB mount, authorized read access, verified host identity, and Drive destination. Camera details can wait.

## Prepare the laptop

- [ ] Clone or update robot-test-hub. Use a full checkout: the live status reader needs the Java tool in tools/status_bridge.
- [ ] Install Python 3.10+ and the pinned WPILib 2027 Alpha 7 installation with its Java 25 runtime.
- [ ] Create the hub's Python virtual environment and install transport and analytics dependencies from the repository root:

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[sftp,analytics]"
~~~

- [ ] Set a private local working archive outside Google Drive. Keep source settings and credentials out of Git.
- [ ] Create the source configuration with confirmed endpoints, robot identity, known-hosts file, collector key, and log root using [Live transfer setup](LIVE_TRANSFER.md#configure-the-computer).
- [ ] Create the hub configuration using [Analytics setup](COLUMNAR_ANALYTICS.md#enable-on-the-dedicated-laptop). Enable analytics and, when the Drive folder is ready, original-log export and summary sharing. Start with one analysis thread and the 512 MB DuckDB setting.
- [ ] Run the [offline readiness check](LIVE_TRANSFER.md#check-setup-before-connecting) with the same source, NT, hub-config, and toolchain arguments intended for live operation. Resolve each failed prerequisite before connecting.
- [ ] Confirm Windows date/time and time zone are correct. Check that run history reports known versus unavailable wall-clock mappings honestly.
- [ ] Save a repeatable launch command or shortcut with the confirmed settings. Autostart/service installation remains a later packaging task.

Do not supply video-config, video-media-config, or marker-config for the first trial. Camera recording and robot note delivery are separate opt-ins; local notebook notes can still be used.

## Prepare automatic collection on the robot

- [ ] Through the team's authorized deployment process, select the reviewed SystemCore/Alpha 7 robot build containing TestHub status and the experimental rotating receiver. The 2026 roboRIO project is a reference, not the deployment target for this flow.
- [ ] Confirm the deployed build and recording-directory configuration. Follow [Prepare the robot source](LIVE_TRANSFER.md#prepare-the-robot-source): set the robot identity and frc.testHubRecordingDir to the actual writable USB path.
- [ ] Confirm live status identifies the intended robot/boot and advances; confirm the receiver publishes its closed-file manifest.
- [ ] Confirm logging begins, a normal run followed by sustained permitted disable closes a segment, and a successor recording opens.

Ordinary AdvantageKit logs do not supply this closed-file manifest. Disabled state or a file that appears to stop growing is not sufficient proof that a file is immutable. Existing ordinary WPILOGs can test the [manual import path](IMPORTER.md) before robot deployment, but that does not qualify automatic collection. The current analysis profile supports the pinned Alpha 7/AdvantageKit build; historical 2026 logs are not newly supported.

## First lab session

Use the [detailed transfer bench worksheet](LIVE_TRANSFER_BENCH.md) for measurements and pass/fail evidence. Start with Drive synchronization paused so robot transfer is measured separately.

- [ ] Record robot/hub versions, network connection, USB device/filesystem, settings, local free space, and session time in the private test archive.
- [ ] Establish a no-collector baseline for robot loop overruns, logging drops/queues, DS link health, and CPU/USB load.
- [ ] Start the live hub using [Live transfer setup](LIVE_TRANSFER.md#configure-the-computer), retaining the source/NT/Java arguments and adding the hub config. Open http://127.0.0.1:6391. Verify it identifies the live reader rather than the synthetic demo.
- [ ] Start the [transfer session recorder](TRANSFER_SESSION.md) in a second terminal to save queue/status observations.
- [ ] Run one short exercise through the normal operator procedure. Disable and allow the recording to close and obtain its digest.
- [ ] Confirm the hub waits for the full disabled idle interval, then downloads only closed, digest-ready segments. Initial live defaults are 10 seconds idle delay and 500 ms status freshness.
- [ ] During an eligible transfer, enable through the normal operator procedure. Confirm collection pauses, operation remains responsive, and the last committed offset is preserved. Measure actual traffic after permission revocation using the bench worksheet; the UI alone cannot establish the cancellation tail.
- [ ] Disable again and confirm automatic resume after the complete idle delay.
- [ ] Interrupt and restore the network connection. Then stop/restart the hub during a partial download. Confirm recovery, exact final size/hash, and no duplicate original.
- [ ] Confirm collection pauses when authoritative status is stale or disconnected even if SSH remains reachable.
- [ ] Compare robot/DS/logger load with the baseline. Record transfer speed, backlog, idle budget, and any observed problems. Save offsets before enabling/disconnecting; queue changes alone are not downloaded-byte measurements.

Use separately scoped trials for controlled robot reboot and reader-process failure as listed in the worksheet. Do not add abrupt power-loss testing to this initial session.

## Confirm the local evidence

- [ ] Confirm original integrity verification and import status separately. Open a downloaded WPILOG in AdvantageScope.
- [ ] Find the exercise in Run history by date/time and robot identity. If time mapping is unavailable, record the gap rather than assuming a time.
- [ ] Save a local notebook note and verify its incident time can be associated with the intended run.
- [ ] Open Practice summaries. Confirm conversion completes and recording-quality, tracking, and current results appear; missing evidence must remain unavailable.
- [ ] For repeated-test comparisons, record an analysis plan and four physical component assignments in Review & maintenance. Do not treat a report as a healthy baseline or mechanical diagnosis.
- [ ] Measure local disk growth and processing time with representative logs. Allow an end-of-practice disabled window if collection cannot keep up.

## Complete the school to home round trip

- [ ] Resume Drive synchronization after the transfer trial. Confirm the hub publishes an original plus its JSON identity sidecar, logs.csv, and the optional report folder.
- [ ] Check Drive for desktop reports synchronization complete. The hub's copied status only confirms local folder publication.
- [ ] At home, download one WPILOG and its matching JSON sidecar. Verify size/hash using [Home workflow](GOOGLE_DRIVE.md#home-workflow), then open it in AdvantageScope.
- [ ] Download a complete report folder and open report.html locally. Confirm its CSV/JSON links work. Home viewing does not require DuckDB or another analysis worker.
- [ ] Keep the active database and Parquet on the school laptop. Do not point a second running hub at its working archive or sync a writable database.
- [ ] If Drive and driving will run concurrently, repeat the load trial with synchronization enabled. Until that passes, pause Drive during driving and resume it after practice.

## Ready for routine practice

The log flow is ready for a limited practice pilot when automatic collection and interruption recovery pass, originals verify and open, time search and summaries are understandable, measured DS/robot load is acceptable, and the school-to-home round trip succeeds. Record unresolved limits and the end-of-practice catch-up procedure.

Before relying on the hub as the only store of notebook/review history, configure and test [coherent backup and clean-directory restore](BACKUP.md). Shared originals and portable reports do not include the complete working catalog or notebook history. Keep original evidence; automatic source deletion remains off.

Save completed checklists and real test results privately. Leave public checkboxes blank; this document is the procedure, not a completed hardware qualification. Camera commissioning can be scheduled separately after this flow works.
