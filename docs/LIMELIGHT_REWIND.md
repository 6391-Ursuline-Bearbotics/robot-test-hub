# Limelight 4 Rewind collection

The optional LL4 collector saves recent camera footage at an observed run end and downloads recording groups directly to the dedicated laptop over HTTP. SystemCore supplies only the existing status heartbeat; video never passes through its storage or application.

Implementation and independent tests cover synthetic local HTTP and hub integration. Real LL4 firmware, recording sizes, camera load, network traffic and timing require the bench trial below.

## Enable on the laptop

Keep the existing live NT/SFTP configuration. Use a private file such as data/ll4-rewind.json:

~~~json
{
  "schema_version": 1,
  "enabled": true,
  "camera_id": "ll4-front",
  "origin": "http://CONFIRMED-LL4-HOST:5807",
  "robot_id": "6391-practice",
  "source_type": "REAL",
  "duration_seconds": 180,
  "timeout_seconds": 5,
  "download_deadline_seconds": 900,
  "poll_interval_seconds": 5,
  "max_recording_bytes": 536870912,
  "max_recordings_per_poll": 1,
  "revalidate_seconds": 3600
}
~~~

Replace the hostname and robot identity with confirmed values. The configured robot identity must match the live source configuration; REAL Rewind refuses synthetic demo status. The first profile accepts an explicit HTTP hostname/IPv4 origin on port 5807, with no URL credentials, path, query, proxy or redirect. Nothing contacts a camera without the explicit configuration.

Add the following option to the existing live launch command:

~~~text
--rewind-config data/ll4-rewind.json
~~~

Open Practice video and find Limelight 4 Rewind. It displays capture outcomes, archived original counts and recent sizes. A separate continuous practice recorder can remain disabled. No new dependencies or robot code changes are needed for this adapter.

## Capture and transfer behavior

The laptop must observe fresh enabled status followed by fresh disabled status in the same robot boot. It queues a save request tied to that status generation and camera configuration. After this request reaches the worker it is persisted; abrupt process loss before the observer's short in-memory queue drains can lose the event. Starting the hub after the robot is already disabled cannot reconstruct a missed run-end transition.

The small save request does not wait for the full bulk-transfer idle delay. It first checks that the camera is not already flushing. Pending requests wait while busy or temporarily unreachable; a later robot boot/generation or camera-configuration change prevents silently retargeting the old event.

An accepted POST means the camera accepted the request, not that footage is safely archived. An explicit negative response is rejected. A timeout, disconnect or crash after dispatch is unknown and is not automatically retried, because the camera might already have saved the buffer. Capture requests do not identify an exact resulting file in this firmware API. Their relationship to a recording remains contextual until timing is established.

Bulk discovery/download requires fresh disabled transfer permission, the complete configured idle delay, no operator pause, and the same permission generation throughout. Enable, stale status, boot/mode change, pause/resume or shutdown revokes the operation. Fresh recovery starts another complete idle interval. Status polling remains independent.

WPILOG source discovery/reads and LL4 requests share one mutual-exclusion lock. No camera request overlaps an in-flight robot-log request, including a stalled source call retaining its lock. The log collector waits on camera occupancy without consuming source retry attempts. One recording group per poll is the default; this provides opportunities for log transfers but is not a measured priority/fairness guarantee. An enabled edge never waits for either collector.

Limelight Rewind is a rolling buffer, not permanent continuous video. Requested duration is a maximum look-back; available history, interruptions and a delayed request can shorten actual coverage. A save can evict older recordings from the camera's allocated storage pool. The hub issues no remote delete requests, but this camera-managed eviction is an inherent save side effect. See [Limelight Rewind behavior](https://docs.limelightvision.io/docs/docs-limelight/software-change-log).

## Original files and qualification

The supported [REST profile](https://docs.limelightvision.io/docs/docs-limelight/apis/rest-http-api) uses /results, /recording-flush, /recording-list and /recording/<filename>. Each completed local group contains an AVI, its JSONL frame manifest, an optional compressed boot log, and metadata.json under rewind/groups/<content-id> in the local archive.

Files are length checked when HTTP supplies a length, hashed locally, and bounded. The manifest must contain complete JSON objects on complete lines. The whole group is published atomically before catalog registration; recovery rehashes published groups before recovering missing registrations. Interrupted downloads stay unpublished and restart from byte zero. There is no unqualified HTTP Range/resume assumption.

The initial schema interprets recording-list size as the sum of AVI, manifest and optional boot-log bytes. The public description does not explicitly resolve boot-log inclusion or all possible response wrappers, so firmware with different semantics fails visibly pending profile qualification. A short real-camera sample must confirm this before relying on automation.

Camera filenames and sizes are weak discovery observations. A persistent cache avoids downloading gigabytes every five seconds. Default content revalidation is hourly; same-name/same-size replacement can go undetected until then. An observed camera application-clock rollback invalidates cached observations, but an unobserved reboot is not reliably identifiable from this protocol. Distinct observed content is preserved under different immutable content IDs. Origin changes create a separate discovery scope.

Local byte preservation does not establish an atomic camera snapshot, decoded AVI validity, video/frame-manifest correspondence, exposure time, camera boot identity or alignment with WPILOGs. The [frame timestamp specification](https://docs.limelightvision.io/docs/docs-limelight/apis/json-results-specification) distinguishes application, wall and NT server times; raw values are preserved. No timestamp is silently converted into qualified robot time, and download time is not capture time.

The adapter does not yet create standard continuous-video segments, playback derivatives, automatic note/run alignment or AdvantageScope exports. Existing manual video tools remain a separate path. Original groups are included in configured coherent archive backups; video is not copied by the log/summary Drive exporter.

## Limits and troubleshooting

Defaults are a 180-second requested look-back, 512 MiB maximum group, five-second connect/response-gap limit, and 900-second download deadline. Tune size and idle windows from measured footage and throughput. A long download still cancels when permission changes; without qualified remote resume a short idle window may repeatedly restart it. Reserve a sufficiently long end-of-practice idle interval.

The HTTP reader checks cancellation during headers/body waits at approximately 100 ms intervals. Socket/time/resource limits are tested locally, not a hardware cancellation-tail guarantee. OS DNS resolution and native connection setup are not hard real-time operations; a confirmed static IPv4 avoids reliance on name resolution. On slow cleanup the hub retains archive ownership until its worker exits.

A failed camera operation retries on a later poll. No partial group is promoted to a recording. Unknown save outcomes remain visible instead of triggering more POSTs. Recovery/hash work uses local files and can run while robot transfer is paused. Keep sufficient local SSD space; no retention/deletion policy is enabled.

Private camera settings and raw footage do not belong in Git. The loopback status API /api/v1/rewind exposes counts, fixed errors and local identities, not camera addresses, raw targeting results or boot logs.

## First physical trial

1. Confirm the LL4 OS version, reachable direct HTTP origin and actual endpoint schemas. Use the team's normal powered hardware procedure.
2. Create one short recording and inspect recording-list. Check size semantics, filenames, optional boot log and flushing state. Record observations privately.
3. Verify one complete group reaches the laptop and opens in the intended viewer; compare decoded frame count with manifest rows using qualified local media tools.
4. Run the hub through enabled and disabled transitions. Confirm one prompt save request per observed run end and downloads only after the idle delay.
5. Enable during a large download, lose status while HTTP remains reachable, and interrupt/recover the network. Measure actual traffic/cancellation tail and verify preserved originals. Separate loopback tests from physical measurements.
6. Compare vision FPS/latency, camera disk/CPU load, DS responsiveness and robot-loop behavior with collection disabled and enabled. Measure the camera flush cost as well as download cost.
7. Measure retained duration, usable clock mappings, reboot behavior, full camera-pool eviction behavior and a realistic end-of-practice backlog before claiming coverage. Destructive/power-loss trials require their own scope.

This optional trial does not block the [log-first commissioning checklist](LOG_FLOW_COMMISSIONING.md).
