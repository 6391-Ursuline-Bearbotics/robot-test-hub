# Robot Test Hub · Team 6391

Design and implementation workspace for turning physical robot testing into searchable evidence: automatic log collection, run history, driver observations, synchronized practice video, and repeatable health reports.

**Current status: design plus a tested transfer prototype. No real robot connection, WPILOG importer, log rotation, video integration, or health analysis is implemented.** Synthetic demo files are not valid WPILOGs.

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

Use a new `--data-dir` for a fresh demonstration. Do not run two instances against the same directory. The prototype does not yet enforce a single-writer process lock.

## What exists

- Persistent SQLite transfer catalog and operator pause setting.
- Disabled/fresh-status gate, idle delay, boot/transition generation tracking.
- Bounded reads, checkpoint resume, SHA-256 verification, corrupt-file quarantine.
- Recovery from missing partials, uncommitted tails, and a crash after final rename.
- Loopback demo UI with queue size/count, progress, transfer rate, and idle-time ETA.
- 16 deterministic collector tests; CI configuration for Windows/Linux and Python 3.10/3.13.

The demo intentionally has no credentials or robot address. `.logdata` artifacts are opaque synthetic payloads, not recordings for AdvantageScope. Test success demonstrates the collector model, not actual SystemCore transfer latency or physical robot operation.

## Prototype limitations

Read the [implementation gap table](docs/IMPLEMENTATION.md#prototype-gap-table) before treating any prototype behavior as the production design. Examples: the ETA resets when paused; discovery is not persisted as a complete snapshot; queue priority is newest-first without aging; manifest checksums are supplied by the fake source; local verification runs inline; there is no source retry backoff, process lock, production service packaging, or real WPILOG validation.

## Data handling

This is a **public source-code repository**. Keep real logs, video, notes, database files, credentials, robot access configuration, and generated reports in ignored local data directories or the team's private archive. Git stores code, design, and explicitly reviewed synthetic fixtures. Original robot recordings are never edited by the analysis pipeline.

## First next task

Implement **T01** in [IMPLEMENTATION.md](docs/IMPLEMENTATION.md): configuration, schema migrations, single-process ownership, and a reliable worker lifecycle. Hardware work can wait until the source adapter contract and the physical acceptance checklist are ready.
