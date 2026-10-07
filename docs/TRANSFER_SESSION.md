# Save a live transfer session

The session recorder saves the running hub's queue, transfer estimates and status observations for the first SystemCore bench test and later practice troubleshooting. It reads only the local hub status API. It does not connect directly to a robot, change collection permission, pause collection, enable the robot or fetch recording bytes.

Start the hub using the [live setup](LIVE_TRANSFER.md), then run this in a second terminal from the repository:

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.transfer_session --output data/transfer-session-001.jsonl --duration 600 --interval 1 --json
```

Choose a new filename for every session. The parent directory must already exist. Existing files are never overwritten, including after an interrupted session. Keep reports under ignored `data/` or another private location. On Linux a newly created report uses owner-only permissions; on Windows use a folder with appropriate account permissions.

The default port is 6391; use `--port` for a different local hub. The destination is always `127.0.0.1`, with proxies and redirects disabled. Duration defaults to 60 seconds and accepts 1 through 86400 seconds. Interval defaults to 1 second and accepts 0.1 through 60 seconds. Requests use socket timeouts and a shrinking body-read deadline, with a 1 MiB response limit. Slow HTTP headers or local storage can extend actual elapsed time; this is an observation recorder, not a precise timing instrument. At most 100000 samples are requested per session.

## What is saved

Each `record_type: sample` JSONL row includes integer UTC and monotonic elapsed nanoseconds and a whitelist of status fields:

- Reported source type and collector state; enabled, transfer permission, freshness and heartbeat age.
- Known transferable, blocked, open and digest-pending bytes; file counts and discovery completeness.
- Reported active and historical throughput, active ETA, snapshot age and verification time.
- Outstanding byte limits and safe status-reader retry/restart observations.

Unknown fields, identifiers, endpoints, paths, free-form messages, credential references and response bodies are omitted. Invalid or unavailable responses produce a fixed error code and a missing snapshot. Missing scalar fields remain null and are listed explicitly. An unfamiliar reader error becomes `other_status_error`; its original text is not saved. Synthetic demo sessions remain visibly synthetic.

The final `record_type: summary` row records sample counts, missing/partial snapshots, observed queue changes, reported rate range, source transitions, reader counter changes and clock reversals. Its time categories are estimates that hold the last polling observation until the next observation. The initial request time is unavailable. Active time requires reported fresh disabled permission, no operator pause and a downloading state. Stale or missing status is unavailable. Brief transitions between samples may be missed.

Samples are flushed as they are written; the final summary and preceding records are flushed and fsynced on successful completion. Ctrl+C retains saved rows and attempts an `interrupted` summary. A failure during sampling preserves the file and attempts a `failed` summary. Abrupt termination or full storage can leave an incomplete final line or no summary; retain the file as incomplete evidence. A `complete` summary means the polling session completed, even if every snapshot was unavailable. It does not mean collection succeeded. A final disk-sync failure can leave that row present while the command exits with an error; check the exit status before treating the save as durable. Exit codes are 0 for completed capture, 2 for configuration/output failure and 130 for Ctrl+C.

## Interpret the report

Queue reductions are not downloaded-byte measurements: discovery, missing files and new recordings can change the queue. The current status API supplies neither cumulative durable download bytes nor boot identity, so those measurements are explicitly unavailable. Reader restart counters cannot establish robot reboot counts. Reported rate ranges are samples of the hub's throughput estimator, not independently measured network goodput percentiles.

The report cannot measure physical traffic after enable, cancellation latency, robot CPU/USB load, driver-station impact or camera timing. `hardware_qualified`, `physical_network_measurement` and `cancellation_tail_measured` remain false. Use it alongside the [bench worksheet](LIVE_TRANSFER_BENCH.md), physical traffic measurements and robot logs; it never automatically qualifies the setup.

Qualification uses deterministic clocks, temporary loopback HTTP servers and an actual temporary HubService status endpoint. No real robot endpoint is involved.
