# Robot integration and instrumentation

## Verified baseline

Inspected repository: `6391-Ursuline-Bearbotics/2027-6391-Rebuilt-V3`, local sibling `../2027-6391-Rebuilt-V3`. This design does not modify it. Its own AGENTS.md governs implementation there.

- `build.gradle`: WPILib/GradleRIO 2027.0.0-alpha-7, application plugin, Java 25, SystemCore target.
- `vendordeps/AdvantageKit.json`: 27.0.0-alpha-6.
- `Robot.java`: constructs one default WPILOGWriter in REAL; NT publisher in REAL/SIM; WPILOGReader and new output writer in REPLAY. SIM currently does not write a WPILOG by default.
- Inspected installed Alpha 6 AdvantageKit source JAR: default robot path `/U/logs`; writer opens in `start()`, writes throughout `putTable()`, closes in `end()`. No rotation on disable. Filename can change after valid date/match metadata arrives. A new process normally creates a fresh file.
- `Logger.getTimestamp()` and WPILOG writer timestamp metadata use nanoseconds in this version. `LoggedSystemStats` stores `EpochTime` in microseconds and `EpochTimeValid` separately. Do not apply 2026 timestamp assumptions.
- `LoggedPowerDistribution.getInstance()` returns the existing instance; logging occurs only if it is non-null. No explicit initialization found in the project. Verify supported actual device, CAN bus and ID before configuring.
- `BuildConstants.java` contains an April 2026 identity; current Gradle file has no regeneration task. Robot logs consume those constants.
- `ModuleIO`: position, speed, voltage, stator current, temperature, connection indicators, and high-rate odometry arrays. Drive publishes optimized setpoints as well as requested/measured states.
- `ShooterHoodIOServo.updateInputs()` places commanded angle into `positionDeg`; it is not sensor feedback.

Recheck these facts before implementation because both the project and alpha dependencies can change. Keep a compatibility table by exact versions rather than inferring compatibility from a filename or year.

## Minimal instrumentation milestone

1. Generate fresh build identity during the build. Include Git commit, dirty status, artifact hash, and an archived source/config snapshot reference. A dirty flag alone cannot reconstruct deployed uncommitted source. Exclude secrets from source snapshots; store them privately as deployment evidence.
2. Generate `robot_id`, `boot_id`, and `run_id`; publish/log mode transitions with monotonic timestamps. A run is one contiguous enabled interval; record auto/teleop/utility phases independently. Reboots close an incomplete run with unknown exact ending if necessary.
3. Log the effective configuration at boot and any tunable changes: controller gains, limits, gearing, wheel radii, sensor offsets, camera transforms, lookup tables, path asset hashes, vendor versions, and field frame.
4. Publish a compact advancing status heartbeat with boot, mode generation, enabled/mode, source timestamp, active segment ID, and permission. Read enabled state from Alpha 7 RobotState APIs verified against the installed source. A separate file server cannot fabricate robot status from its own liveness.
5. Add optional SIM file logging so importer and rotation tests can use genuine generated WPILOGs.
6. Expose logger queue/fault and USB storage/write status. Logging in memory or NT publication is not proof USB recording is healthy.

Keep all command/scheduler operations on the normal thread. Other threads may receive bytes and enqueue immutable messages; consume them in logged IO on the robot thread. Existing autonomous timing, CAN settings, runtime modes, and control behavior remain unchanged by these instrumentation tasks.

## Rotation contract (custom work, not a WPILOGWriter setting)

Implement a rotating `LogDataReceiver` compatible with the pinned version. Do not call `Logger.end()`/`start()` during a practice to switch files. One receiver instance owns the file writers on the receiver-processing thread; no scheduler thread opens/closes/hashes large files.

Suggested initial policy: after five seconds continuously disabled following a run, close the current segment at an ordered table boundary. Start a successor so disabled activity continues to be recorded. If enabled before that boundary, defer rotation. Long idle sessions rotate by a configurable age/size cap. A size cap during a long enabled run should be deferred initially; show the growing file. Add enabled-time rotation only after specific performance testing.

Derive trigger decisions from the ordered logged state accompanying the tables. Maintain latest known values and all required schemas, including metadata and struct/protobuf schemas. A fresh writer must receive a complete initial state and decodable schema set, not just the changed keys from the previous file. The existing writer's `lastTable` resets on start, which is useful, but do not assume that alone covers separately supplied schemas or metadata. Prove it with real nested struct/array fixtures and replay.

Segments use explicit stable-ID paths to avoid relying on auto-renaming. Publish closed state only after successful flush/close; publish final digest when ready. The manifest update is atomic and revisioned. Do not mislabel an open or hashing-pending segment as complete.

Maintain continuous timestamp coverage with an explicit boundary convention: each normal loop belongs to exactly one segment; an initial snapshot in a successor is marked as a bootstrap snapshot when duplicates are necessary. Importer deduplication uses source identity/timestamp/type rules, never arbitrary equal-value removal. Preserve events that repeat legitimately.

On write failure, surface the failure and stop claiming coverage. If a valid recovery path exists, start a new segment and record the gap. Sudden power loss may leave an orphan `.wpilog`; a recovery worker can determine the last valid record boundary, retain the original, and register a distinct recovered artifact marked incomplete. Recovery must not invent missing terminal events or rewrite the original silently.

Acceptance: multiple enable/disable cycles, metadata/schema changes, restart/crash, reader validation, and replay of every segment. Compare original continuous behavior outputs against segmented/reassembled replay. Test marker events at boundaries. Measure logger queues and loop time before calling rotation hardware-ready.

## Instrumentation priorities

| Area | Already available / verify | Add or clarify |
| --- | --- | --- |
| Swerve | States, optimized commands, stator current, volts, temp, odometry | Supply current, controller resets/faults, signal age, module physical ID/location mapping |
| Power | System battery voltage and brownout faults | Supported power-distribution initialization, battery ID, wiring channel labels; verify real readings |
| Shooter | Goal, command RPM/angle, velocity, readiness, control mode | Readiness component/rejection reasons, feed gates, attempt/result events, measured hood sensor only if installed |
| Intake/indexer | Goals, states, currents, jam indicators | Jam/recovery transitions and outcomes, rehome phase, piece sensor observations where present |
| Autonomous | Selected routine, trajectory error, some timeout outputs | Phase entry/exit/cancel, wait reason, deadline/timeouts, marker lifetime, effective delay |
| Vision | Pose observations, timestamps, ambiguity, tag count/distance, accepted/rejected pose collections | Per-observation rejection reason, applied uncertainty, measurement age, correction magnitude |
| System | CAN/network/CPU/memory/IMU stats in pinned AK source | USB-specific free space/write failures, queue fault reporting, telemetry coverage health |
| Configuration | Some tunables logged | Effective snapshot hash and revisions even outside tuning mode |

Do not add high-rate CAN polling indiscriminately. Measure bus/load impact. Motion signals follow control/odometry needs; faults and temperatures usually need less bandwidth. Event transitions are preferable to repeating long explanatory strings every cycle. Missing hardware stays explicitly unavailable.

## Driver markers

Notebook writes succeed locally first. A bounded robot mailbox accepts versioned marker envelopes, enqueues them for logged IO, and returns receipt acknowledgments by ID. Deduplicate IDs within a boot; hub outbox handles reconnect/reboot delivery rules. A historical note must not be inserted into a new boot as though it happened there. Allow an optional contextual receipt record carrying the original boot/event time, or retain it hub-only.

No marker affects actuator commands. Rate-limit and bound text/batch sizes. Consume marker events during disable too. Use a latched event sequence or array so a short marker cannot disappear between logger samples. Any dashboard input that influences behavior must be logged as input for deterministic replay.

## Hardware commissioning prerequisites

Confirm SystemCore OS/image, actual USB mount and free-space API, access account and SFTP host key, status-channel source, DS version, radio topology, supported power hardware, available LED cue, and existing camera access. Store deployment credentials outside Git. Use the installed Alpha 7 JDK/wrapper build and focused robot tests. Report build, simulated startup, real logging, transfer load, and deployment separately.
