# First SystemCore transfer session

The implemented collector has been qualified with real NT/SSH libraries on loopback. This worksheet establishes what remains to measure on the robot. Robot deployment and operation use the team's separately authorized procedure; the hub never enables the robot or waits for collection before allowing enable.

## Record the setup

Save results privately alongside the test archive, using a new session ID. Record robot/hub commit, runtime mode, WPILib/AdvantageKit versions, SystemCore OS, USB device and filesystem, SSH account restrictions, NT and SSH endpoints, link type, idle delay, freshness, chunk limit and source timeout. Record test start/end UTC and robot boot ID. Keep keys and passwords outside the worksheet. Begin with the experimental receiver explicitly selected and its log directory confirmed; an ordinary growing WPILOG is not an immutable closed segment.

## Qualify in this order

| Step | Observe | Required result |
| --- | --- | --- |
| No collector baseline | Loop overruns, logger queue/drops, USB/CPU load, DS link health, CAN faults | Reference measurements under the team's repeatable test procedure |
| Hub startup while enabled | Authoritative status, SSH source requests, transfer queue | No new source discovery or recording reads |
| Disable briefly, then enable | Disabled countdown and source requests | Idle countdown resets; no bulk work before the full delay |
| Sustained disabled interval | Closed segments, digest readiness, queue and disk usage | Only closed digest-ready files download; open/pending bytes remain visible |
| Enable during transfer | Actual source/network traffic, observed permission edge, last source read, durable offset | No new request after observed revocation; in-flight cancellation tail measured; discarded block does not advance the checkpoint |
| Status loss with SSH reachable | Freeze/disconnect authoritative publication through the test setup | Collection pauses even though the file connection is reachable |
| Status reader failure | Stop only the hub's identified reader child, leaving the hub up | Visible retry, no transfer while unknown, advancing replacement status plus full idle delay before resume |
| Link interruption | Interrupt the collector link, then restore it | Saved offset survives; retry resumes automatically; original/archive hashes agree |
| Hub restart during partial file | Record committed offset, stop/restart hub | Offset is recovered; no duplicate original or silently accepted partial file |
| Separate controlled robot reboot | Reboot through the team's normal procedure | Old immutable recordings remain recoverable; new boot requires new status proof; incomplete/orphan recording is visible |
| Sustained backlog | Known closed bytes, measured active transfer rate, queue ETA | ETA follows durable active throughput and excludes enabled pauses, open bytes and local verification |
| Final integrity | Source identity/digest, downloaded size/hash, import state | Byte-exact verified original; format/import verification remains separately visible |

Exercise robot enable changes through the team's existing operator procedure. Do not disable logging or deliberately fill the robot's USB drive to test faults. Storage-full, corrupted-source and process-crash ordering cases already have software fault-injection tests; physical power-loss tests need their own approved scope.

## Save measurements and decisions

For each row record pass/fail/unmeasured, observed timestamps, a concise incident note and links to private evidence. Capture goodput p50/p95/worst, maximum outstanding bytes, source traffic after observed revocation in bytes/milliseconds, robot loop/DS/logger impact relative to baseline, local verification time and error/retry counts. Network capture must measure actual traffic; a UI screenshot alone cannot prove cancellation latency. Document whether enable and status loss were physically observed or software-injected.

Run the [local session recorder](TRANSFER_SESSION.md) alongside the hub to preserve queue, rate/ETA, freshness and reader-recovery observations. Keep its JSONL report with this worksheet. Its polling categories and reported rate range supplement the physical measurements; they do not supply network goodput percentiles, robot boot counts, cumulative downloaded bytes or cancellation tails.

Initial settings are 10 s idle delay, 500 ms status freshness, one 256 KiB collector chunk and one 32 KiB SFTP read. The provisional cancellation target is under 250 ms on the selected setup; report a failure instead of changing the definition afterward. Adjust settings from measured results and rerun affected cases.

Check archive capacity as `idle_fraction × idle_goodput > average_log_generation_rate`, with rates in matching units. If not, use a faster link or reserve an end-of-practice idle window. Record the operational limit; the queue display cannot solve insufficient capacity.

Keep `hardware_qualified: false` until measurements support a reviewed qualification report. Source deletion remains unavailable even after this test.
