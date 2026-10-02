# Data and integration contracts

This is the target v1 contract. The prototype's Python dataclasses and `/api/status` are deliberately smaller and must be migrated, not assumed to implement this entire contract.

## Identity and time rules

- `robot_id`: configured identity of the physical robot, independent of team number.
- `boot_id`: UUID generated once per robot-program execution (not merely OS boot).
- `segment_id`: UUID generated before a physical recording starts; immutable content once closed.
- `run_id`: UUID on disabled-to-enabled transition; ends on disable, reboot, or unknown state. Auto-to-teleop without disable remains one run with separate phase intervals.
- `session_id`: practice/event grouping spanning boots and runs; may be assigned later.
- All durable IDs are opaque strings. The source namespace plus segment ID is unique. Original filenames are labels, never identity.
- Monotonic nanoseconds and UTC nanoseconds are signed integers in storage and **decimal strings in JSON**, avoiding JavaScript's integer precision loss. Durations exposed for display may be floating-point seconds.
- UTC may be null; preserve raw robot monotonic time even without valid wall clock. Every alignment references a boot and mapping revision. Do not compare monotonic timestamps across boots.
- Units are explicit per signal. Canonical derived units use SI, while originals retain their own unit and field path. Preserve coordinate-frame IDs, including the existing 2026 REBUILT blue-origin convention.

## Robot status envelope

```json
{
  "schema_version": 1,
  "robot_id": "6391-practice",
  "boot_id": "EXAMPLE-BOOT-ID",
  "sequence": 418,
  "mode_generation": 9,
  "enabled": false,
  "mode": "disabled",
  "robot_monotonic_ns": "123456789000",
  "transfer_allowed": true,
  "active_segment_id": "EXAMPLE-OPEN-SEGMENT"
}
```

The adapter attaches its own local monotonic **receipt** timestamp. Receipt of a cached message must not refresh liveness: sequence must advance. `mode_generation` increments on every mode/permission transition; this detects enable-then-disable between collector observations. Boot change invalidates earlier permissions. `transfer_allowed` is an additional gate; it cannot override enabled or stale state. Missing fields, invalid version, unknown mode, or unknown freshness mean no permission. A heartbeat without the robot's live status cannot grant permission.

Proposed heartbeat period: 100 ms; stale limit: 500 ms after commissioning. Neither is a measured hardware guarantee. The prototype freshness limit is one second.

## Closed-segment manifest

```json
{
  "schema_version": 1,
  "robot_id": "6391-practice",
  "manifest_revision": 24,
  "segments": [{
    "segment_id": "EXAMPLE-SEGMENT-ID",
    "boot_id": "EXAMPLE-BOOT-ID",
    "relative_path": "EXAMPLE-SEGMENT-ID.wpilog",
    "original_name": "akit_practice.wpilog",
    "state": "closed",
    "size_bytes": 1048576,
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "start_monotonic_ns": "1000000000",
    "end_monotonic_ns": "60000000000",
    "format": "wpilog",
    "format_profile": "wpilib-2027-alpha7_akit-27-alpha6"
  }]
}
```

Example values are placeholders, not valid fixture evidence. Only `closed` segments with final length and trusted identity become eligible. Publish the manifest atomically after closing/flushing a file. If hashing is pending, report `closed_pending_digest` and do not claim verification eligibility. An open segment is reported separately with an observed size and observation time; it is not part of the fixed backlog.

Paths must remain under configured robot log roots after normalization; reject traversal/absolute paths/symlink escapes. A path change may preserve identity. A size/hash change under the same closed segment ID is a source-contract violation. Record and quarantine it; do not silently restart as if it were the same artifact. Pagination must identify a single manifest revision, or explicitly return an incomplete discovery result.

Legacy recordings without a manifest require an explicit import adapter. Do not infer closure solely from stable size. They can be manually imported as immutable local files, or transferred as labeled provisional snapshots under a separately tested protocol.

## Source adapter interface

`get_status()` returns an independently refreshed cached status plus receipt age, never a blocking bulk operation. `discover_closed(cursor)` is bounded and cancelable. `read(segment_id, offset, max_length, permission_generation, cancellation)` returns bytes or a typed error. `cancel()` stops outstanding work and disconnects if the transport cannot cancel a pending read. `capabilities()` advertises range reads, sender enforcement, immutable manifest, and digest support.

Do not expose delete in the first adapter. No prefetch beyond the configured outstanding-byte limit. Network timeouts must be explicit; stale status must remain observable during stalled reads. Validate offset/length against closed size at both ends where supported. Separate read failures from malformed data and authentication failures; only transient failures retry automatically.

## Persistent entities (target migrations)

| Entity | Required data / uniqueness |
| --- | --- |
| Source | ID, type, robot identity, endpoint reference, capabilities, configuration revision; secrets are external references |
| Robot / component | Robot ID; component IDs, location assignments with effective intervals; battery ID |
| Boot | Boot ID, robot ID, artifact/build hash, configuration snapshot hash, library versions, raw metadata |
| Session | ID, label, purpose, venue/surface, local timezone, UTC interval, operator assignments |
| Run | ID, boot/session IDs, monotonic interval, mapped UTC interval and mapping revision, mode phases, completeness |
| Segment | Source/segment unique key, boot ID, path aliases, bytes, source digest, format profile, interval, lifecycle |
| Transfer | Segment FK, durable offset, partial path, attempts, next retry, priority, last error, verified digest |
| Artifact | Hash, local location, size, type, verification time, format validity, backup replicas |
| Run-segment | Run ID, segment ID, overlapping monotonic interval; supports many-to-many |
| Clock anchor / mapping | Clock domains, boot/camera IDs, raw anchors, validity, uncertainty, fit interval, revision |
| Annotation revision | Event ID/revision, author/source, submitted UTC, intended interval, original relative-time input, body/tags, mapping confidence, linked run |
| Marker delivery | Event ID, destination boot, outbox state, received/ack times, attempts; unique event/destination |
| Video segment | Camera/session, artifact, presentation-time range, recording UTC estimate, gaps/frame stats, alignment revision |
| Config / maintenance | Immutable snapshot hash or event ID, effective interval, component/battery, changes, author |
| Import / analysis job | Idempotency key, source hashes, code/schema/analyzer/config versions, state, coverage, error |
| Metric | Run/window, analyzer version, name/unit, value or unavailable reason, sample count, eligible duration |
| Finding / review | Detector/version, interval, severity, evidence refs, baseline version, explanation; reviewed disposition and validation links |

Enforce foreign keys and unique idempotency keys. Store null for unknown timestamps/measurements. Derived tables must be reproducible from immutable artifacts plus versioned config. Use append-only revisions for human observations and maintenance corrections.

## Notebook event envelope

```json
{
  "schema_version": 1,
  "event_id": "EXAMPLE-EVENT-ID",
  "revision": 1,
  "submitted_utc_ns": "1790898720000000000",
  "client_monotonic_ns": "50000000000",
  "when": {"kind": "seconds_ago", "seconds": 30},
  "event_utc_start_ns": "1790898690000000000",
  "event_utc_end_ns": "1790898690000000000",
  "uncertainty_ms": 1000,
  "text": "Steering felt wrong",
  "tags": ["drive"],
  "source": "practice-notebook"
}
```

Compute relative time at the button action, never receipt on the robot. Preserve the raw user input and clock quality even after mapping. Acknowledgments refer to `event_id` and receipt time, not a claim that the incident occurred at receipt. Same ID/revision and same body is an idempotent retry; same ID/revision with different content is a conflict. Late notes never rewrite the raw log.

## Hub HTTP API (target, `/api/v1`)

| Endpoint | Purpose |
| --- | --- |
| `GET /status` | Collector/recorder/indexer health, source freshness, queue snapshot, ETA basis |
| `GET /transfers?cursor=...` | Paged queue, durable progress, retries, reasons |
| `POST /collector/pause`, `/resume` | Persisted operator preference; resume never bypasses gates |
| `POST /transfers/{id}/retry`, `/priority` | Explicit retry or prioritization, audited |
| `GET /runs?from=...&to=...&robot=...` | Interval-overlap search, UTC and timezone-aware UI conversion |
| `GET /runs/{id}` | Evidence links, phase timeline, config, data coverage |
| `POST /annotations`, `GET /annotations` | Idempotent notebook write and interval query |
| `POST /annotations/{id}/revisions` | Revision with expected previous revision |
| `GET /findings`, `POST /findings/{id}/reviews` | Investigation and human outcome |
| `GET /video/{id}/alignment`, `POST /video/{id}/alignment` | Revisioned sync anchors, confidence, manual corrections |
| `POST /sessions`, `/maintenance-events` | Small structured context forms |

Use bounded pagination, validation, stable error codes, and explicit API/schema versions. Bind to loopback by default. LAN notebook support is a separate authenticated configuration with CSRF/origin protection and a paired client; never silently bind the demo to all interfaces. Browser input and imported strings are untrusted display text.
