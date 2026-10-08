# Passive robot note markers

This is the implemented T08 marker contract (October 8, 2026). Local automated/native and browser checks pass. Notebook storage receipts remain separate from marker receipts. Phone pairing and physical robot qualification remain unfinished.

## Operator workflow

Save the observation first, including its original button/time input. A saved note remains useful with no robot connection. When marker delivery is configured, an operator can explicitly send a particular saved revision to the currently confirmed robot boot. The hub commits a durable delivery job before any publication. Notebook storage receipts and marker delivery receipts remain separate.

An accepted marker is a contextual receipt in the robot's logging inputs. It does not establish when the physical incident occurred, select a run, validate the client's clock, or prove USB durability. A note entered 30 seconds after an incident retains that original incident interval even if its delivery takes another minute. Later revisions have separate jobs; a retry never substitutes the latest revision.

The bridge is default off on both sides. It has no actuator, command, enable, deployment, tuning, or log-deletion capability. Passive small marker traffic can operate during enable and while the bulk collector is paused. Bulk transfer keeps its separate disabled-and-idle gate. Unknown/stale/disconnected status pauses delivery. Native NetworkTables remains a trusted practice-network integration, not an authenticated public endpoint.

## Exact version 1 wire contract

Both topics use a STRING value:

- Request: `/TestHub/Notebook/MarkerRequest`
- Acknowledgment: `/Telemetry/TestHub/MarkerAck`

The request has exactly these keys:

```json
{
  "schema_version": 1,
  "profile": "testhub-note-marker-1",
  "delivery_id": "delivery-uuid",
  "destination_robot_id": "6391-practice",
  "destination_boot_id": "boot-uuid",
  "event_id": "event-uuid",
  "revision": 1,
  "annotation_sha256": "64 lowercase hex characters",
  "payload_sha256": "64 lowercase hex characters",
  "payload_json": "exact projected note JSON"
}
```

IDs use `[A-Za-z0-9_-]{1,100}`. Revision is an integer from 1 through 1,000,000. The outer UTF-8 envelope is at most 16 KiB. Reject invalid Unicode, duplicate JSON keys, nonfinite numbers, excess nesting, unsupported fields, and wrong types. Too-large projections do not prevent saving the original hub note; the delivery is unavailable with an explicit reason. Never silently truncate text.

`annotation_sha256` pins the full canonical normalized saved annotation, including its author/device metadata, in the hub. The robot echoes this source pin; it cannot independently verify omitted metadata. `payload_sha256` hashes the exact UTF-8 `payload_json` bytes and is independently verified by the robot.

The payload projects only `schema_version`, `event_id`, `revision`, `submitted_utc_ns`, `client_monotonic_ns`, `event_utc_start_ns`, `event_utc_end_ns`, `when`, `uncertainty_ms`, `clock_domain`, `clock_quality`, `text`, `tags`, and `source`. Author, device identity, run assignment, storage receipts and delivery fields are omitted. Decimal nanosecond strings preserve signed 64-bit values exactly, including values beyond JavaScript's integer precision. Unknown times stay null. The original `when` structure, clock quality and uncertainty stay unchanged.

The ACK echoes all request pins except `payload_json`, followed by:

```json
{
  "state": "accepted_into_log_input",
  "duplicate": false,
  "receipt_robot_ns": "123456789012345",
  "reason": null,
  "ack_scope": "contextual_receipt_only",
  "usb_durability": "unavailable",
  "logger_queue_fault": false,
  "runtime_mode": "SIM"
}
```

Rejected requests use state `rejected`, a null receipt, and a fixed reason: `wrong_robot`, `wrong_boot`, `payload_conflict`, `capacity`, or `log_input_unavailable`. Malformed or unpinnable requests increment a health counter without echoing arbitrary input. ACK runtime is REAL or SIM; REPLAY never sends an ACK.

## Boot identity and retry rules

The marker producer shares the existing TestHubStatus robot ID and boot UUID. Its receipt uses Alpha 7 `RobotController.getMonotonicTime()` nanoseconds. It does not introduce another boot UUID or infer a UTC clock mapping.

The robot maintains a scheduler-owned, boot-local ledger of at most 1,024 admitted entries, with no eviction. The key is event/revision/destination robot/destination boot. Identical annotation and payload hashes return the first receipt, including when delivery ID changes. A changed hash for that key is a conflict. Capacity rejects new keys while continuing to serve existing duplicates. Admission failures do not consume capacity.

The hub preserves exact job bytes and pins across retries and restart. A confirmed new boot ends an old pending job as historical/unconfirmed; it never retargets it. Stale status does not prove a reboot. A job with a lost ACK can retry the exact STRING value because publishers/subscribers explicitly preserve duplicates. An ACK must match every pin, the configured runtime, and a persisted publication attempt. An ACK cannot renew status freshness.

## Logging and scheduler boundary

`MarkerIONetworkTables` polls on a dedicated worker. Native subscription storage is four messages; parsed request and outgoing ACK queues hold eight each. UTF-8, JSON and SHA validation run there. `pollStorage` bounds message count, not native/network allocation bytes: this does not provide hostile-frame memory isolation.

The normal scheduler admits at most one prevalidated request per robot cycle, including disabled cycles. It resets `AcceptedEnvelopes` every cycle and logs the exact original request string alongside the contextual receipt. All AdvantageKit calls occur on the normal robot thread. Generated `@AutoLog` serialization remains the source of truth; no generated files are handwritten.

An `Admission` wrapper confirms that the pinned AdvantageKit logger actually invoked live `toLog` before committing the ledger and queuing a success ACK. A stopped logger is a no-op; replay invokes `fromLog`; neither qualifies a new live receipt. This proves entry into the logging input table. Receiver queue faults are reported separately and USB durability remains unavailable.

REAL and SIM require explicit `-Dfrc.testHubMarkersEnabled=true`. Default off constructs empty IO. REPLAY always constructs empty IO even if that property is set, consumes recorded marker inputs, and creates no live subscriber, publisher, worker or acknowledgment. SIM ACKs and inputs remain labeled SIM.

## Hub configuration and API

The private marker config explicitly names `nt_host`, `nt_port`, `robot_id`, `runtime_mode` and optional installed Alpha 7 path. No robot endpoint is guessed. The CLI prepares the pinned native runtime before opening the archive. Logs, endpoints, configuration and original observations stay outside the public repository. The marker bridge currently conservatively requires known Driver Station enabled/disabled state as well as advancing status; an advancing heartbeat with unknown DS state still waits.

Example for a separately started local SIM endpoint, saved under the ignored `data/` directory:

```json
{
  "schema_version": 1,
  "enabled": true,
  "nt_host": "127.0.0.1",
  "nt_port": 5810,
  "robot_id": "6391-practice",
  "runtime_mode": "SIM",
  "freshness": 1,
  "ack_timeout": 2,
  "retry_initial": 1,
  "retry_max": 30,
  "maximum_attempts": 20,
  "maximum_pending": 128
}
```

Start the hub with its existing options plus `--marker-config data/marker-config.private.json`. This opts into a robot connection independently of the bulk source. Use a configured REAL endpoint only within an authorized bench test. The current native runtime profile requires the installed Windows Alpha 7 toolchain; the portable hub tests do not qualify native operation on other systems.

- `GET /api/v1/markers`: redacted bridge health and current destination readiness.
- `GET /api/v1/markers/annotations/{event_id}?revision=N`: exact committed source SHA for that saved revision.
- `POST /api/v1/markers/deliveries`: explicitly schedule `{delivery_id,event_id,note_revision,annotation_sha256,destination_robot_id,destination_boot_id}`.
- `GET /api/v1/markers/deliveries`: bounded pages of delivery receipts.
- `GET /api/v1/markers/deliveries/{id}`: one pinned delivery.
- `POST /api/v1/markers/deliveries/{id}/retry` with `{}`: retry the existing job without changing pins.

HTTP handlers persist/inspect jobs; they do not publish to the robot. Mutations retain the existing loopback, JSON, same-origin and request-header rules. The native transport has its own worker and cleanup lifetime. Service ownership must remain held until child processes and readers/writers have actually stopped, even after a bounded shutdown timeout.

## Qualification before use

Automated acceptance must cover original-time preservation, exact revision/hash matching, commit-before-send recovery, lost-ACK retry, changed boot, dedupe/conflict/capacity, UTF-8 and deep/duplicate-key JSON, forged ACKs, stalled heartbeat, enabled/paused collector operation, generated input replay, and cleanup ownership/redaction. Actual native loopback proves library/protocol behavior only. Physical SystemCore admission cost, NT load, USB receiver behavior and abrupt power loss still require commissioning measurements; do not claim hardware qualification from synthetic fixtures.

The robot's `MarkerLoopbackFixture` is a test-only standalone SIM producer. It constructs no robot devices or normal Robot container, binds NT to loopback, and writes genuine AdvantageKit WPILOGs. The optional hub test `tests/test_marker_native.py` requires three explicit environment variables: `ROBOT_HUB_MARKER_FIXTURE_CLASSPATH` (a file containing the sibling robot test runtime classpath), `ROBOT_HUB_MARKER_JAVA` (installed Java 25 executable), and `ROBOT_HUB_MARKER_NATIVE_DIR` (sibling `build/jni/release`). This verifies actual hub-to-producer admission during enabled status with a paused collector and checks the exact original request/receipt bytes in the closed WPILOG. It is skipped in portable CI; robot-native NT duplicate qualification runs in the sibling wrapper tests.
