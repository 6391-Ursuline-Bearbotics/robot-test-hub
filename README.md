# Robot Test Hub · Team 6391

Design and implementation workspace for turning physical robot testing into searchable evidence: automatic log collection, run history, driver observations, synchronized practice video, and repeatable health reports.

**Current status: design plus a tested transfer prototype with the T01 service foundation. No real robot connection, WPILOG importer, log rotation, video integration, or health analysis is implemented.** Synthetic demo files are not valid WPILOGs.

## Start here

- [Full design and document map](docs/README.md)
- [Implementation handoff](docs/IMPLEMENTATION.md): small tasks with dependencies, acceptance tests, and suggested model prompts
- [Decisions and unresolved hardware questions](docs/DECISIONS.md)
- [Contributor/agent instructions](AGENTS.md)

## Run the local demo

Python 3.10+; the prototype has no runtime dependencies. From this repository:

```powershell
python -m robot_test_hub.server
```

Open **http://127.0.0.1:6391**. The demo uses a three-second idle delay and about 36 MiB of deterministic synthetic data. Enable the simulated robot, disconnect it, freeze its heartbeat, or vary its speed to exercise pause/resume. Use Ctrl+C to stop. Restart with the same data directory to recover saved progress.

```powershell
python -m robot_test_hub.server --port 6392 --data-dir data/another-demo --idle-delay 10
python -m unittest discover -s tests -v
```

Use a new `--data-dir` for a fresh demonstration. An OS-held ownership lock rejects a second writer before it opens the catalog or touches transfers. The lock is released on clean shutdown or process exit; a leftover `.owner.lock` file is normal and must not be deleted while a hub is running. Use a local filesystem; network-share locking has not been qualified.

## Configuration and diagnostics

CLI options remain supported. Optional JSON configuration must specify `schema_version: 1`; CLI options override corresponding file values. Paths resolve relative to the invocation directory. Save private configuration under ignored `data/`, for example `data/hub-config.json`:

```json
{
  "schema_version": 1,
  "data_dir": "data/demo",
  "port": 6391,
  "idle_delay": 3,
  "freshness": 1,
  "chunk_size": 262144,
  "tick_interval": 0.01,
  "status_interval": 0.05,
  "shutdown_timeout": 5
}
```

```powershell
python -m robot_test_hub.server --config data/hub-config.json
```

Invalid or unknown settings fail with field-specific guidance before service data is modified. Existing prototype catalogs migrate transactionally to schema 1, retaining offsets, filenames, transfer states, and operator pause. Catalogs from a newer schema are rejected; preserve the data directory and use its matching newer hub version.

Durations must be finite numbers: idle delay is 0–86400 seconds; freshness is positive and at most 3600 seconds; tick/status intervals are positive and at most 60 seconds; shutdown timeout is positive and at most 300 seconds. Port is 1–65535 and read chunks are 1 byte–16 MiB. These validation limits protect worker waits; they are not hardware commissioning recommendations.

Status polling and HTTP snapshots run independently of transfer reads, discovery, recovery, and local verification. A successful pause response means the preference is committed to SQLite. Retry returns `202` with a pending state and is applied by the collector worker. `/api/status` exposes worker health, source freshness, and snapshot age; `/api/diagnostics` exports structured redacted diagnostic JSON. Local JSON logs rotate under `<data-dir>/diagnostics/` (1 MiB each, three backups). Arbitrary exception text and tracebacks are omitted because they can contain credentials.

Ctrl+C stops the foreground host; SIGTERM and Windows Ctrl+Break are handled too. Shutdown cancels the demo read, waits for workers, closes the catalog, then releases ownership. If a future adapter ignores cancellation or has unbounded I/O, a shutdown timeout is reported and ownership remains held while its worker lives; this foundation cannot forcibly interrupt arbitrary Python I/O. Adapter deadlines/cancellation and source-load qualification remain T02/T11/T12 work. Production autostart installation remains T18.

## What exists

- Persistent SQLite transfer catalog and operator pause setting.
- Versioned validated configuration, explicit schema migration, and per-data-root process ownership.
- Independent status/transfer workers, responsive cached HTTP snapshots, durable pause, and visible worker failures.
- Structured redacted diagnostics with bounded local rotation and clean foreground shutdown.
- Disabled/fresh-status gate, idle delay, boot/transition generation tracking.
- Bounded reads, checkpoint resume, SHA-256 verification, corrupt-file quarantine.
- Recovery from missing partials, uncommitted tails, and a crash after final rename.
- Loopback demo UI with queue size/count, progress, transfer rate, and idle-time ETA.
- 37 local automated tests, including the original collector cases, migration/configuration checks, stalled-I/O HTTP checks, and subprocess ownership/shutdown. CI configuration covers Windows/Linux and Python 3.10/3.13; the T01 suite was run locally on Windows with Python 3.10.7.

The demo intentionally has no credentials or robot address. `.logdata` artifacts are opaque synthetic payloads, not recordings for AdvantageScope. Test success demonstrates the collector model, not actual SystemCore transfer latency or physical robot operation.

## Prototype limitations

Read the [implementation gap table](docs/IMPLEMENTATION.md#prototype-gap-table) before treating any prototype behavior as the production design. Examples: the ETA resets when paused; discovery is not persisted as a complete snapshot; queue priority is newest-first without aging; manifest checksums are supplied by the fake source; local verification runs inline in the transfer worker; there is no source retry backoff, production service packaging, or real WPILOG validation. Status/API responsiveness is tested with synthetic blocked operations, not a real network transport.

## Data handling

This is a **public source-code repository**. Keep real logs, video, notes, database files, credentials, robot access configuration, and generated reports in ignored local data directories or the team's private archive. Git stores code, design, and explicitly reviewed synthetic fixtures. Original robot recordings are never edited by the analysis pipeline.

## First next task

Implement **T02** in [IMPLEMENTATION.md](docs/IMPLEMENTATION.md): the production queue, bounded cancelable I/O, discovery snapshots, retry/fairness, verification jobs, and honest ETA. Hardware work can wait until the source adapter contract and the physical acceptance checklist are ready.
