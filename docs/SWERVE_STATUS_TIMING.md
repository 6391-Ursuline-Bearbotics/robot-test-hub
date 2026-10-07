# Recorded Phoenix status and timing diagnostics

## Purpose and limits

The robot records the SDK status of the drive-velocity and turn-position values it actually uses. This lets the hub warn about a recorded controller communication/status error even while the existing 0.5-second falling connection debounce still reports connected. The warning identifies the recorded module location and signal; it does not establish a mechanical cause or authorize a repair.

The producer is reviewed robot commit `2991ce6` on the [existing integration draft](https://github.com/6391-Ursuline-Bearbotics/2027-6391-Rebuilt-V3/pull/1), with the Alpha 7/JDK 25 build and all 42 tests passing. The hub pipeline runs a separate `phoenix-status-observation` version `1` check under `automatic-reports-5`; prior report bytes remain immutable. Local native-enabled hub qualification ran 497 tests (495 passed, two Windows privilege skips), including twelve author, five independent and three actual native-writer cases. No hardware was deployed or operated.

Timestamp and repeated-value observations are diagnostic evidence. They do **not** qualify REAL measurement freshness, physical sensor acquisition time, or absolute synchronization with the robot clock. Overall health remains `not_assessed`. Older recordings and default simulation IO have unavailable Phoenix diagnostics; their missing fields cannot produce a status pass. Swerve tracking keeps its existing REAL freshness exclusion and explicit ideal-simulation policy.

## Source audit and ownership

The robot manifest `vendordeps/Phoenix6-26.70.0-alpha-2.json` pins `com.ctre.phoenix6:wpiapi-java:26.70.0-alpha-2`. Two independent audits used the installed source JAR (SHA-256 `82dca54a28510bb810f17b4a834bcfbac4618189b2b2756eac53485302acbd0c`) and compiled API, alongside WPILib `2027.0.0-alpha-7` and AdvantageKit `27.0.0-alpha-6`.

Relevant source contracts:

* `Timestamp.java`: System time describes host receipt; CANivore time describes adapter receipt; Device time describes transmission by a device and requires the appropriate hardware/license. These are SDK seconds, not a demonstrated physical acquisition timestamp. `getLatency()` subtracts the timestamp from `Utils.getCurrentTimeSeconds()`.
* `Utils.java`: the status clock used above differs from the separately documented monotonic clock and can be overridden in simulation. No proven offset/origin relation to `RobotController.getTime()` is supplied by this inspection.
* `BaseStatusSignal.java`: `hasUpdated()` compares System receipt timestamps and consumes a mutable previous-timestamp latch. The robot diagnostics do not call it. Aggregate successful refresh status does not establish a new frame. The pinned SDK marks System/CANivore timestamp validity true during refresh; validity and best-source selection alone do not qualify actual hardware/native timestamp availability.
* `StatusSignal.java` and `AllTimestamps.java`: timestamp getters expose mutable owned objects. A clone provides independent timestamp state; `getDataCopy()` neither supplies all source/validity fields nor establishes an atomic native snapshot.
* WPILib `Timer.java`/`RobotController.java`: robot time is nanoseconds; Timer seconds divide that clock by `1e9`. The injectable time supplier can jump. NetworkTables/Alpha7 log cycle timestamp units must be preserved separately.

The main drive-velocity and turn-position Java signal objects are owned by the scheduler. High-rate odometry refreshes separate deep clones. After the three existing group refreshes, the diagnostics read each selected raw value once, clone its timestamp state immediately, and retain only primitives. The same copied raw values feed the original rotations-to-radians and `Rotation2d.fromRotations` conversions. There is no additional CAN refresh, frame-rate change, native frequency query or actuator behavior. This is a scheduler-owned Java-cache observation, **not a guarantee of atomic native sampling**. Added Java allocation, logging volume and scheduler cost still require hardware measurement.

## Logged contract

Each `/Drive/Module0` through `/Drive/Module3` input table carries flat AutoLog fields. Primitive defaults keep unavailable SDK evidence distinct from successful observations. Generated `ModuleIOInputsAutoLogged` remains build output.

| Fields (prefix `Phoenix`) | Meaning and units |
| --- | --- |
| `DiagnosticsPresent`, `DiagnosticsProfile`, `SnapshotMethod` | `true`, `phoenix6-status-observation-1`, `scheduler_owned_cached_read_after_existing_refresh` only for this hardware IO observation |
| `ObservationSequence` | Per-module scheduler observation counter; not a CAN frame counter |
| `RobotObservationStartNs`, `RobotObservationEndNs` | Robot-clock bookends in integer nanoseconds |
| `VendorObservationStartSeconds`, `VendorObservationEndSeconds` | Phoenix status-clock bookends in seconds; no conversion to robot time inferred |
| `ObservationClockValid`, `ObservationClockRegressed` | Recorded clock diagnostics; raw readings are retained |
| `PhysicalAcquisitionTimeQualified`, `NativeTimestampAvailabilityQualified` | Always false in this profile |
| `DriveGroupRefreshStatusCode/StatusOk`, `TurnGroupRefreshStatusCode/StatusOk` | Existing aggregate refresh status; separate from selected signal status and debounced connection flags |

Both signal prefixes `PhoenixDriveVelocity` and `PhoenixTurnPosition` append:

| Suffix | Meaning |
| --- | --- |
| `RawValue` | Drive rotations/second, turn rotations; exactly the copied control-input value |
| `StatusCode`, `StatusOk` | Cached numeric SDK code and SDK `isOK()` result |
| `BestTimestampSeconds`, `BestTimestampSource`, `BestTimestampValid` | SDK-selected time, source `0=System`, `1=CANivore`, `2=Device`, SDK validity |
| `SystemTimestampSeconds/Valid`, `CANivoreTimestampSeconds/Valid`, `DeviceTimestampSeconds/Valid` | Independent copied raw times/validities, all in SDK seconds |
| `ReceiptComparison` | System receipt comparison: `unknown`, `first`, `held`, `advanced`, `regressed`, `invalid` |
| `RawValueComparison` | Independent raw-value comparison: `unknown`, `first`, `held`, `changed`, `invalid` |
| `BestTimestampSourceChanged` | Recorded change in valid best-source selection |
| `AgeAtObservationStartSeconds`, `AgeAtObservationEndSeconds` | Raw vendor-bookend minus best timestamp; negative/nonfinite values remain evidence, not clamped freshness |
| `TimestampInFuture` | Best SDK timestamp is later than the ending vendor-clock bookend |

AdvantageKit encodes Java integer status/source fields as WPILOG `int64`; the clock fields ending `Seconds` are doubles. Repeated values with advancing receipt timestamps are normal possible observations. Held receipts have no automatic fault threshold. SDK status warnings retain original file hashes, original/held value record indices and the logging-cycle reference.

## Before-season bench worksheet

Deployment and robot operation need separate authorization. Once bench access is available:

1. Record firmware, vendor versions, CAN bus topology, CANivore/Pro licensing and frame periods. Save an unchanged baseline build/configuration, battery, surface, module identities and practice-test plan.
2. Measure scheduler/DS/CAN/USB load before and after diagnostics, disabled and under representative motion. Check log growth, receiver rotation, transfer coexistence and shutdown. Do not infer acceptable overhead from pure unit tests.
3. Compare robot/vendor clock pairs through startup, a long session and reconnect. Record offset/drift/discontinuities with evidence. Observation spans are not sensor/transport uncertainty bounds.
4. Observe stationary equal values with advancing receipts, moving values, controlled input loss and restoration. Confirm selected signal status, aggregate refresh status and delayed connection flag behavior. Use separately authorized safe bench procedures; the hub does not command motion or disconnection.
5. Establish which timestamp sources actually exist on this hardware. Measure sensor/filter/update/transport delays using an independent reference before selecting any physical freshness budget.
6. Save immutable original WPILOGs and a dated private qualification report. Publish a redacted summary only if requested. Until a reviewed profile and uncertainty model exist, REAL acquisition freshness remains unknown.

Generated native-writer fixtures contain invented observations only. They verify decoding, warning provenance, held records, units, unavailable simulation defaults and immutable report behavior; they are not measurements of the robot or camera.
