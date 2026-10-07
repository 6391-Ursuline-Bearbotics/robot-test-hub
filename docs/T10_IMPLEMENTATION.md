# T10 experimental segmented recording

Published robot branch: `codex/test-hub-recording`, commit `aeef012`; [draft robot integration review](https://github.com/6391-Ursuline-Bearbotics/2027-6391-Rebuilt-V3/pull/1). This branch contains T09/T10 and the live recording selection; it is not merged or deployed.

October 7 independent review: normal aftermath/time/size rotation now requires both the queued logged disabled envelope and current RobotState idle permission. Loss of either resets the disabled delay, preventing backlog from rotating files after live enable. Schema/gap decoding boundaries remain separate. Twelve focused receiver tests pass, including queued-disabled versus live enabled/unknown and permission recovery. The final Alpha 7 JDK25 wrapper build passes all 32 tests, zero failures/errors. Automated stepped simulation/replay passed; GUI/hardware/deployment were not exercised.

Implemented in `2027-6391-Rebuilt-V3` as `frc.robot.hub.RotatingLogReceiver` with an independent bounded `SegmentWriter`. REAL retains WPILOGWriter by default; an explicit absolute `-Dfrc.testHubRecordingDir=<directory>` selects the experimental receiver as its sole file writer. SIM retains NT-only logging unless this property is supplied; REPLAY ignores it. No dependencies, Commands v2 commands, actuator requests or physical robot deployment were added. See [live setup](LIVE_TRANSFER.md).

## Threading, state and boundaries

The constructor performs no filesystem operation. AdvantageKit's pinned ReceiverThread calls start, putTable and end on `AdvantageKit_LogReceiver`. Its ordered logged table supplies RealMetadata RobotId/BootId and RealOutputs/TestHub StateKnown/Enabled/TransferAllowed/Sequence/RobotMonotonicNs. Recording proceeds during enabled, disabled and unknown states; only fresh explicitly disabled permission advances the disabled rotation timer. Known-state validity, sequence and monotonic timestamps are checked before policy rotation. Missing identity or nonadvancing clocks/sequence fail visibly rather than claiming a fresh recording.

Default policy: close after five seconds continuously permitted disabled following an enabled observation; idle recordings also rotate after five minutes or 256 MiB, after the same disabled delay. Size/age thresholds defer while enabled or unknown. A changed entry type or existing schema definition creates an explicit `/TestHubSegment/SchemaBoundary` decoding context, including while enabled, so a previous type cannot silently hide the new value in AdvantageKit replay. This is a schema boundary rather than size/age policy rotation. Every normal cycle belongs to exactly one file: the successor starts with the next ordered table, with no duplicate boundary timestamp. Initial snapshots contain all retained state, metadata and nested struct/protobuf schemas. Definitions precede values. `/TestHubSegment/InitialSnapshot` and `/TestHubSegment/BootstrapKeys` explicitly distinguish copied held values from new events; a consumer must not interpret a held initial marker/string as a fresh event. Repeated event text remains meaningful through its independently advancing event sequence. Missing cycle sequence creates an explicit successor `/TestHubSegment/GapBefore`; write failure leaves the prior artifact failed/incomplete and marks recovery as a gap.

All artifact/catalog/manifest writes and flush/sync operations run on the receiver thread. Scheduler integration only reads a volatile health snapshot. `/RealOutputs/TestHubRecording/WriteState`, LastError, ActiveSegmentId, PendingDigests and IntegrationQualified expose receiver health; IntegrationQualified is false. The T09 atomic status includes the receiver snapshot's active_segment_id (null before a file opens or when no receiver is configured); the snapshot can lag a receiver-thread boundary and is not immutable close evidence. No Logger stop/start cycle is used for rotation.

## Stable files and close evidence

Each recording has a fresh UUID and exclusive `<UUID>.wpilog` file creation. Artifact files are never replaced, renamed or deleted by this receiver. Close flushes and synchronizes the artifact before atomically publishing `<UUID>.segment.properties` close evidence. Pending files use state closed_pending_digest and sha256=null; only completed idle hashing produces state closed and a digest. Open bytes are observations, not fixed backlog. A stream error/close error becomes failed_incomplete with a reason. Clean program shutdown leaves the final segment pending until a later permitted hashing opportunity; it never fabricates a digest.

Restart reads bounded close records, preserves their boot identities, resumes pending digest work, and checks recorded lengths. Missing/changed paths remain source_missing/identity_conflict. UUID WPILOGs with no durable close record are retained as orphan_incomplete and are never declared closed based on stable size or a readable prefix. No crash recovery truncation is performed. Explicit orphan recovery remains future work.

The low-priority digest worker reads only closed immutable artifacts in 64 KiB chunks. It requires a fresh advancing logged disabled envelope, a 500 ms local lease and a separate current RobotState disabled/DS/known-mode/not-estopped gate. It checks permission before each chunk and after the final read, discards a canceled partial digest, and checks length/mtime before publishing a result. There are at most 32 queued/active jobs. The receiver thread publishes results into the catalog; the worker never writes manifests. Enabling, unknown state, stalled table delivery or shutdown stop new hashing. Shutdown/interruption leaves pending close evidence retryable; only observed size/mtime/regular-file mismatch claims identity conflict. Other digest I/O errors remain pending with a one-second retry backoff and recorded failure reason. Cancellation cannot promise instantaneous interruption of a filesystem read already in progress; tail/load have not been measured on hardware.

## Bounded manifest contract

The recording root contains atomic `manifest.json` with:

```json
{
  "schema_version": 1,
  "robot_id": "6391-practice",
  "boot_id": "UUID",
  "manifest_revision": "DECIMAL-INTEGER",
  "segments_complete": true,
  "known_segment_count": 4,
  "segment_count_lower_bound": 4,
  "page_size": 128,
  "max_pages": 64,
  "closed_ready_count": 2,
  "pending_digest_count": 2,
  "pages": [{"relative_path": "manifest-page-SHA256.json", "sha256": "SHA256", "size_bytes": 1234}],
  "observed_monotonic_ns": "DECIMAL-INTEGER",
  "open_segment": null,
  "write_state": "stopped",
  "last_error": ""
}
```

Each immutable content-addressed page is `{schema_version:1,entries:[...]}`, at most 128 entries and 256 KiB. A root revision pins its exact page descriptors; pages do not contain a root revision, allowing unchanged pages to be reused. Old pages remain byte-identical and are never deleted. Descriptor sha256/size_bytes must be verified by consumers. The index has configurable maxPages 1–512, default 64 (8192 indexed records); excess records remain on disk and segments_complete=false exposes a lower bound. Root size is bounded by that page limit, below the bridge's 1 MiB root limit. The receiver publishes catalog/state changes immediately and open observations at most once per second, not every 20 ms. Revision is a decimal JSON string and survives restart.

Page entries contain segment_id, robot_id, boot_id, relative_path, original_name, state, sha256, created_at, format, format_profile, size_bytes, start_monotonic_ns, end_monotonic_ns, first_sequence, last_sequence, cycles, gap_before and failure. Profile is exactly `wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6`. Start/end bounds are observed inclusive T09 robot monotonic nanosecond values, encoded as decimal strings and meaningful only within boot_id. They are not binary WPILOG header timestamps. created_at is ISO UTC only when the accompanying SystemStats/EpochTimeValid is true, using the pinned DOUBLE microsecond EpochTime payload after explicit unit/finite/signed-nanosecond-range checks; otherwise null. Filename/filesystem time is never promoted to UTC.

States: closed, closed_pending_digest, failed_incomplete, orphan_incomplete, identity_conflict, source_missing. Open state appears only in separate open_segment with an observed size and root observed_monotonic_ns. Only closed with valid final length and digest is transfer eligible. Truncated indexes must not be called caught up. This receiver does not implement SFTP or sender enforcement, and does not grant robot permission from server liveness.

Atomic replacement requires filesystem support for ATOMIC_MOVE. Artifact and temporary metadata files are synchronized before publication. Parent-directory fsync is not available through this Windows test path; sudden power loss can leave an orphan or lose the latest publication, which is why absence of close evidence never implies completeness. Durability on actual SystemCore USB storage requires commissioning.

## Exact Alpha 7 findings and encoder

Installed `datalog-java` and `datalog-cpp` sources for 2027.0.0-alpha-7 and AdvantageKit 27.0.0-alpha-6 were inspected. The native `jni/DataLogJNI.cpp` copyWriteBuffer implementation ignores its supplied start offset when copying, repeating the beginning of the buffer after 16 KiB. A genuine actual-Robot table reproduced corrupted start control records using the official DataLogWriter OutputStream path. That path also swallows checked IOExceptions. The experimental receiver therefore uses a bounded WPILOG 1.0 encoder with the exact AdvantageKit extra header, type strings, units metadata, control records and primitive/array payload layouts, validated against the installed official DataLogReader/WPILOGReader and the portable importer. Record payloads are capped at 16 MiB before allocating oversized output buffers. Native RandomAccessFile writes avoid interruptible FileChannel closure while AdvantageKit interrupts/drains its receiver on clean shutdown.

The official DataLogIterator requires 16 remaining bytes in hasNext, which can skip the final short data record. Closing emits a timestamp-unit metadata control trailer so every genuine last-cycle value reaches replay; this adds no fake cycle or event. Binary header timestamps are explicitly converted from input nanoseconds to microseconds; `/Timestamp` and RobotMonotonicNs payloads remain exact int64 nanoseconds. Protobuf/struct schemas and all primitive/array forms are preserved.

Actual Logger metadata paths are `/RealMetadata/*` in REAL/SIM and `/ReplayMetadata/*` in replay. This corrects the earlier static `/Metadata/*` assumption; T09 documentation and hub importer aliases were updated. Dynamic `/RealOutputs/TestHub/*` paths remain unchanged.

## Validation and remaining qualification

Eleven focused receiver tests cover independently decodable successors; every unique ordered cycle and repeated event text; complete metadata/held initial state; nested structs, struct arrays, protobuf schemas, all primitive/array forms and long precision; metadata changes, unit removal and explicit type/schema boundaries; typed-double UTC and invalid clock rejection; interruption-versus-identity failure classification; enabled size deferral and unknown-state timer reset; mid-digest cancellation and resume; immutable page history/final hashes/restart; bounded index with explicit incomplete discovery; write failure/recovery and missing-cycle gaps; retained crash orphans. An additional integration test opts the actual stepped SIM Robot into recording and replays all 20 disabled cycles including genuine nested schemas and fresh metadata. Validation recordings are retained under robot `build/reports/testhub/segments` and `build/reports/testhub/robot-recordings`; the official reader memory-maps files without a close API on Windows.

Portable importer additionally decoded a genuine Robot recording without format errors (1387 records in the inspected run). Final JDK25 `gradlew.bat build --console=plain` succeeded in 2 m 8 s on 2026-10-04 UTC: all 29 tests passed with zero failures/errors (13 original, 4 T09, 12 T10). Independent Sol review corrected DOUBLE microsecond UTC handling and interruption classification before this build. The initially clean tracked NetworkTables backup was restored after its test-generated default autonomous-delay preference/format rewrite. `git diff --check` passed. Existing full simulation/Commands v3 regressions continue to run with default NT-only SIM behavior.

October 6 update: with the installed Alpha 7 JDK25, `gradlew.bat build --console=plain` succeeds; **all 31 tests pass**, zero failures/errors. Two new tests verify default/REPLAY selection and explicit absolute REAL/SIM paths. The actual stepped Robot/replay test verifies active-segment identity in atomic status after the receiver opens its first file. It waits for the background receiver's first manifest rather than assuming fast stepped simulation implies completed file IO. The scheduler reads one immutable health snapshot per cycle. The test-generated tracked NetworkTables backup was restored to its prior bytes. Compilation and automated simulation tests passed; no real-time GUI run or deployment was performed in this update.

Still unqualified: segmented/reassembled behavior-output equivalence for full actual autonomous executions, real-time GUI performance, receiver queue and scheduler timing under rotation/digest load, power-loss durability, actual USB health/free space, filesystem read cancellation tail, SystemCore access/load, marker delivery, and physical robot operation. The October 6 live work adds explicit REAL opt-in and atomic active-segment snapshots, with default logging unchanged. Local NT/SFTP source integration now passes; see [live evidence](LIVE_TRANSFER.md). No hardware deployment/run occurred.
