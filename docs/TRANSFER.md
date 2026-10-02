# Fault-tolerant idle transfer

## Rules

The driver never waits for collection. Bulk transfer must yield to operation. Logging continues while collection is paused. No new bulk request starts unless the status is fresh, explicitly disabled, permission is allowed, and the idle delay has elapsed. A stationary enabled robot does not qualify as idle.

Laptop-only SFTP cancellation cannot promise zero bytes after the physical enable edge. Bytes already requested/buffered can arrive. Bound outstanding work, cancel promptly, and measure the tail. A sender-side permission check is an optional stronger guarantee; it still cannot retract packets already on the network. State these limits in release notes.

## State machine

| State | Enter when | Exit / effect |
| --- | --- | --- |
| Disconnected | No authoritative status or source connection | Keep checkpoints; reconnect with backoff |
| Paused: enabled | Enabled is observed | Cancel bulk work; await disable |
| Paused: unknown | Status stale, malformed, missing, or boot ambiguous | Cancel; require new advancing heartbeat |
| Paused: operator | User pause flag set | Persist across restart; only explicit resume clears |
| Waiting for idle | Fresh disable begins | Countdown resets on any invalid condition |
| Discovering | Idle gate passes | Enumerate bounded manifest pages; still cancelable |
| Downloading | Eligible segment chosen | Read bounded chunks and checkpoint |
| Verifying | Final bytes locally durable | Local digest and format checks; no robot reads |
| Caught up | Complete discovery and all eligible files verified | Watch for newly closed segments; distinguish open bytes |
| Attention | Nontransient auth, identity, disk, or integrity error | Show action; do not spin retries |

Suggested commissioning defaults: idle delay 10 s, heartbeat stale after 500 ms, one 256 KiB request outstanding, one file at a time. Reduce chunk size if measured cancellation tails are excessive. Prototype uses 1 s freshness and a 3 s UI demo delay. These numbers are configuration, not constants spread through adapters.

Status handling runs independently of I/O. Cancellation sets a token immediately; adapters poll it or close the read channel on a bounded timeout. Recheck the boot/mode generation before committing a returned block. Keep state transition telemetry with monotonic times so tests can prove when permission was revoked and when source traffic ceased.

## File selection and fairness

Prefer an explicitly marked incident, then the most recent completed run. Continue the selected segment across chunks to avoid arbitrary switching. At file boundaries, use an aging policy: after two recent/priority files, choose one oldest eligible file unless an explicit urgent override exists. Preserve work on interrupted files. Urgent prioritization may preempt at a checkpoint. Record why an item moved in the queue.

Queue discovery must be paged and bounded. Show whether the count is complete or a lower bound. Persist manifest revision, last successful discovery, known closed backlog, and observed open-segment size separately. On source reconnect the old queue remains visible as last-known until refreshed.

## Checkpoint protocol

For an immutable segment with final size N and digest H:

1. Verify identity and manifest metadata before first/resumed read. Confirm the path still resolves under the allowed root.
2. Open/create a partial file; recover to the durable offset stored in SQLite.
3. Obtain one chunk at the expected offset under the current permission token.
4. Recheck permission/generation. If revoked, discard returned data and do not advance the checkpoint.
5. Write bytes at the offset, flush and fsync the file, then transactionally commit the new offset.
6. At N bytes, close/flush and compute local SHA-256 in a separate local verification job.
7. If digest matches, atomically rename into the raw archive on the same filesystem, then commit the verified state. Preserve raw source metadata. Format validation is a separate job.
8. Never delete the robot copy merely because transfer succeeded.

Checkpointing every small chunk is simple but may limit throughput. A later implementation may durably commit batches; on restart it discards bytes beyond the last committed offset. Any batching must bound lost work and preserve file-before-database ordering.

Prototype recovery truncates an uncommitted tail and resets a missing/short partial to zero. Production should also record local checkpoint digests or perform conservative revalidation if local partial data was externally altered. Final digest remains mandatory.

## Failure handling

| Failure | Required behavior |
| --- | --- |
| Enable during read | Cancel, save only prior checkpoint, no additional chunk requests |
| Enable then disable between polls | Mode generation changed; reset idle delay even if current state is disabled |
| Stale heartbeat | Pause even if file server is reachable |
| Reboot | Invalidate permission/clock domain; retain old immutable segment queue |
| File rename | Resume by stable ID after manifest refresh |
| Closed size/hash changes | Flag contract violation; quarantine conflicting evidence |
| Source file missing | Retain catalog entry as missing; do not claim archive completion |
| Transient network failure | Backoff with jitter (e.g. 1, 2, 4…30 s); preserve partial |
| Authentication/host-key failure | Attention required; never downgrade trust automatically |
| Disk full | Pause and retain checkpoint; clear explanation and retry after space restored |
| Crash before checkpoint | Truncate extra local bytes to committed offset |
| Crash after rename, before DB commit | Recognize verified final bytes and reconcile transaction |
| Hash mismatch | Quarantine; one explicit/finitely configured retry; avoid endless redownload |
| Active file still growing | Exclude from immutable queue; display separately |
| Abrupt robot power loss | Recover valid prefix via explicit orphan-recovery workflow; mark incomplete, never silently promote |

Source hashing and manifest generation can themselves be expensive. Prefer an incremental hash maintained in the writer/close pipeline if supported without loop blocking; otherwise hash closed files in a cancelable idle worker. Measure storage contention. Do not compute a fresh whole-file source hash at every resume.

## ETA contract

Let B be the sum of undispatched bytes for known eligible files, using their durable offsets. Effective throughput is durable bytes divided by active download wall time, including transfer/checkpoint overhead. Exclude deliberate idle waits, enabled pauses, and unrelated analysis. Verification time is tracked separately.

Use a time-based rolling window (initial target 10–20 s), not the last packet speed. Update visible estimates at approximately 1 Hz. Display `B / rate` only after sufficient samples (initially 2 s). Range estimates may use recent sustained-rate percentiles; do not label them statistical confidence intervals without calibration. Use rounded units, not subsecond precision for multi-minute backlogs.

- Active: `About 3 min of idle transfer time · 3.2 GB / 7 files · 17 MB/s`.
- Paused: retain the estimate as `About 3 min at last observed rate; paused while enabled`.
- Changed interface/reconnected after a long interval: invalidate current estimate; optionally show historical rate separately.
- Unknown throughput: `Estimating…`, with visible bytes/count.
- Incomplete discovery: `At least 3 min; discovering remaining files`.
- Failed/unavailable files: distinguish transferable backlog and blocked backlog; never offer a finite finish estimate for unresolved bytes.
- No eligible bytes but open segment exists: `Closed files caught up · recording 180 MB`.

Use explicit units. UI can default to decimal MB/GB to match the user's planning language; prototype uses MiB/GiB. Do not mix their denominators. One GB per minute implies 16.67 MB/s; three GB then takes three minutes of actual transfer availability. Future recordings increase the queue and are excluded from the estimate until known.

## Performance experiment

Measure log generation rate, transfer goodput, CPU, USB throughput, logger queue depth, loop overruns, DS link health, status age, and cancellation tail. Repeat tethered and on practice Wi-Fi, at several rate/chunk limits. The steady-state archive can catch up only if idle fraction × idle goodput exceeds average log generation rate. If it cannot, improve the link/storage or plan a longer final idle window; an ETA display cannot fix insufficient capacity.

Initial target for bench acceptance: no new request after observed permission revocation; bounded in-flight data; measured tail preferably under 250 ms on the selected setup. Treat failure to reach that target as a commissioning finding, not permission to claim it passed. Driver enable is never blocked by the collector.
