# T09 passive robot identity and status

Implemented in sibling `2027-6391-Rebuilt-V3`, under that repository's Alpha 7 instructions. This milestone adds passive instrumentation only. Commands v3 scheduling, IO selection, hardware configuration and autonomous behavior are retained.

## Identity and lifecycle

`frc.robot.util.TestHubStatus` defaults to the configured practice identity `6391-practice`. Override it with JVM property `-Dfrc.robotId=6391-competition` when commissioning a distinct physical robot; accepted IDs use 1–100 letters, digits, underscores or hyphens. Team number alone is not physical identity.

A UUID boot ID is created for each constructed robot execution. A run UUID begins at an observed enabled transition and remains unchanged across autonomous-to-teleoperated phase changes without disable. Disable or unknown status ends it. An initial enabled observation after startup/unknown gets a new ID with `RunStartComplete=false`, preserving the missing initial boundary instead of claiming an observed disabled-to-enabled transition. `ModeGeneration` advances for enabled state, operating mode or transfer-permission changes, including a selected operating mode change while disabled. `Sequence` advances every normal robot loop, including disabled loops (nominal 20 ms, not a measured hardware guarantee).

The state is sampled after the Commands v3 scheduler on the same robot thread. Live transfer permission requires DS attachment, a known RobotMode, disabled state and no emergency stop. Replay emits unknown enabled state and never grants permission. A downstream adapter still must enforce advancing sequence, receipt freshness, boot identity and generation. This instrumentation is not a file server.

## Atomic live envelope

The canonical NetworkTables status topic is `/Telemetry/TestHub/Status` (string containing schema version 1 JSON). AdvantageKit also records `TestHub/Status`, which is `/RealOutputs/TestHub/Status` in ordinary REAL/SIM outputs. The envelope includes `robot_id`, `boot_id`, nullable `run_id`, numeric `sequence` and `mode_generation`, nullable boolean `enabled`, `mode`, `operating_mode`, decimal-string `robot_monotonic_ns`, boolean `transfer_allowed`, nullable `active_segment_id`, `runtime_mode`, `logger_queue_fault`, `usb_write_health`, `configuration_sha256`, and `configuration_revision`.

`robot_monotonic_ns` comes from Alpha 7 `RobotController.getMonotonicTime()` and uses nanoseconds in the execution's monotonic clock domain. It is not UTC and is not comparable across boots. This deliberately avoids the overridable `getTime()` used by replay. The JSON representation preserves integers above JavaScript's exact integer range. `active_segment_id` remains null because segment rotation/manifest publication is a separate milestone.

The pinned Logger actually emits static metadata under `/RealMetadata/` (and `/ReplayMetadata/` during replay), verified by genuine T10 Robot recording. Additional typed AdvantageKit outputs under `/RealOutputs/TestHub/` are:

| Field | Type and meaning |
| --- | --- |
| RobotId, BootId | string identities, also static `/RealMetadata/RobotId` and `/RealMetadata/BootId` |
| RunId, RunActive, RunStartComplete | string (empty when inactive), boolean, boolean observed-start completeness |
| StateKnown, Enabled | boolean validity, boolean value; `Enabled=false` is only a placeholder when `StateKnown=false` |
| Mode, OperatingMode, RuntimeMode | strings; disabled/unknown/replay or enabled robot mode, requested HAL robot mode, REAL/SIM/REPLAY |
| ModeGeneration, Sequence | int64 counters |
| RobotMonotonicNs, RobotMonotonicNsUnit | int64 nanoseconds, string `nanoseconds` |
| TransferAllowed | boolean additional permission gate |
| ConfigurationSHA256, ConfigurationRevision | string hash, int64 revision |
| ConfigurationSnapshot | canonical sorted-key JSON string, emitted initially and when effective configuration changes |
| LoggerQueueFault, UsbWriteHealth | boolean supported AdvantageKit queue fault, string `unavailable` |

Alpha 7 AdvantageKit's `recordOutput(long)` has no units overload. The adjacent `RobotMonotonicNsUnit` and exact documented field profile supply the unit; the importer must preserve this value as a decimal string. StateKnown must accompany Enabled, preventing its false placeholder from granting permission. Actual replay output paths are controlled by AdvantageKit and must not be mistaken for live REAL outputs.

## Build and effective configuration evidence

The wrapper's `generateHubBuildIdentity` writes a fresh generated resource outside source. Source SHA-256 hashes sorted relative paths, lengths and exact bytes of `src/main`, vendordeps, Gradle/settings/wrapper configuration and WPILib preferences. Fixed configuration SHA-256 covers main Java, deploy assets and vendor manifests. Two different dirty source snapshots therefore have different source identities even with the same Git commit and dirty flag. Generated build time and Git metadata refresh each resource build.

Metadata records fresh ProjectName, BuildDate, GitSHA, GitBranch, GitDirty, SourceSHA256, FixedConfigurationSHA256, LibraryVersions, SourceSnapshotReference, ArtifactHashReference, ArtifactSHA256, RobotId, BootId, RuntimeMode, FieldFrame and HubSchemaVersion. The actual loaded application JAR is hashed once during startup; a classes directory or unavailable code source reports ArtifactSHA256=`unavailable`. The built JAR's independent hash is also written to `build/reports/testhub/artifact-sha256.txt`; a JAR cannot embed its own final digest. The application JAR retains the existing `backup/src` and vendor/Gradle snapshot and additionally archives settings, wrapper properties and WPILib preferences. It does not archive credentials or the whole checkout.

`HubConfiguration` reads the existing cached `LoggedTunableNumber` getters plus the direct Alpha 7 autonomous delay tunable. It adds no CAN polling or new actuator request. The effective snapshot also includes selected autonomous, runtime/robot identity, tuning mode, source hash and fixed configuration hash. Static gains, limits, geometry, lookup tables, vendor definitions and path assets are bound by their exact archived source/deploy bytes. `/Telemetry/TestHub/ConfigurationSnapshot` exposes the same canonical JSON as the logger. Hash/revision change whenever this snapshot changes, including live tunable changes; previous snapshots remain in the log. Unavailable or nonfinite effective numeric values are retained as strings instead of invalid JSON numeric tokens.

## Verified sources and validation

Exact installed sources were inspected: WPILib `2027.0.0-alpha-7` RobotState, RobotController, Telemetry, Tunables and simulation/HAL APIs, and AdvantageKit `27.0.0-alpha-6` Logger, LoggedNetworkNumber, LoggedDriverStation and WPILOGWriter. `Logger.getReceiverQueueFault()` is supported. WPILOGWriter has no public USB write-health/free-space accessor; NT publication and an empty logger queue are not USB recording evidence, so USB health is explicitly unavailable. SIM retains its existing NT-only logger. No dependency or Commands v2 integration was added.

Focused pure tests cover separate boot/run identities, complete/incomplete starts, unknown/replay state, operating-mode and permission generations, canonical hashes, JSON escaping and nanosecond precision. The actual stepped Robot integration checks disabled heartbeat advancement, tunable-driven configuration revision and DS disconnection closing permission. For DS disconnection, the test refreshes Alpha 7 DS state without sending a new simulated packet; `notifyNewData()` would reattach the simulator's DS.

A temporary synthetic main-source file changed the generated source digest; deleting the file restored the exact prior digest. The temporary file was removed. Final command: JDK25 `gradlew.bat build --console=plain`, successful in 1 m 39 s on 2026-10-04 UTC. All 17 tests passed with zero failures/errors (13 existing regressions plus 4 T09 tests). The final application JAR SHA-256 was `831ac5b8d040eb231957432a00c10fa94e9a7d8bff2e46073e2909b637ca0be0`; loading HubIdentity from that JAR reproduced the independent sidecar digest. Required archived source/settings/wrapper/preferences entries were verified. Test-generated whitespace in the initially clean tracked NetworkTables backup file was restored. Build/test success is separate from hardware evidence: no robot deployment, physical robot run, USB recording test, transfer-load measurement, rotation test or simulator GUI session was performed for T09.