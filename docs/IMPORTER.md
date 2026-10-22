# Versioned WPILOG import (T05)

The manual local importer supports the pinned `wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6` profile with mapping revision `alpha7-aliases-2`, plus the explicit historical `wpilib-2026_akit-26` profile described below. The standard-library Python extractor was compared with the locked installed official Alpha7 `DataLogReader.forEach()` for all **362 physical records** in the four T04 recordings, including their complete 14/15-byte tails. It avoids the installed iterator's short-tail omission and rejects incomplete framing. Selection of a profile is explicit: WPILOG's wire version and AdvantageKit extra header cannot establish the producing library versions. A conflicting recorded fixture profile, incompatible timestamp metadata, unknown profile, or unknown mapping revision remains an unsupported job. Historical 2026 logs require their explicit profile; 2025 logs remain unsupported.

## Import a folder of logs

Use this after practice, at events, or any day the automatic collector misbehaves. It needs no robot connection, receiver, manifest, SFTP or NT status. Every `.wpilog` under the folder (searched recursively, empty files skipped) is archived unchanged, imported, and passed through the normal run index and automatic checks. Identity is the SHA-256 of the bytes, so running it again over the same folder, or over renamed copies, imports nothing new.

- **While the hub is running:** on the Transfers page, type the folder path in **Import logs from a folder** and choose **Import logs**. The indexer thread runs the import; progress and any files that could not be imported appear under the button. Only full local paths are accepted, and the hub's own data folder is refused.
- **While the hub is stopped:** double-click `scripts\windows\ImportLogs.cmd` and pick the folder, or run `ImportLogs.cmd E:\`. It uses the same private launch profile as `StartHub.cmd` to find the data folder.
- **Developer CLI:** `python -m robot_test_hub.import_folder <folder> --data-dir <data> [--profile <profile>] [--synthetic] [--no-process] [--json]`. The exit code is nonzero if any file was not imported.

The log profile defaults to the pinned Alpha 7 profile and is never inferred from file contents. Logs from another library version are recorded as `unsupported` and kept in the archive for a later importer revision; they are not guessed at.

### Collect now (pull after practice, P0.2)

A manual snapshot alternative to the rotating receiver. It requires an explicitly configured **live** launch profile with authoritative NT status and SFTP for the same robot. Follow [live profile setup](WINDOWS_SETUP.md#save-one-private-live-profile), including the explicit NT host/port and pinned WPILib installation; do not infer them from the SFTP address. Add `"pull_source_config": "source.json"` to the hub configuration, pointing at the same SFTP source file that profile uses (matching robot ID, host, port, user, pinned `known_hosts`, private key and `log_root`). The NT bridge host must match that explicitly configured robot host. Offline/demo profiles cannot authorize a real download. The setup wizard saves SFTP settings only; it does not create the live status profile.

Choose **Collect now** after practice while disabled. The request waits for fresh advancing live status, explicit disabled/transfer permission, and the continuous idle delay. Enable, disconnected/stale/unknown status, reboot or status-generation change interrupts the capture. Operator Pause and hub shutdown also revoke permission. A new capture requires a new request after permission loss; partial snapshots are kept. The automatic closed-segment collector may separately report a missing manifest; this manual flow needs live status but does not require that manifest.

- It lists the flat `*.wpilog` files in `log_root` (oldest first, by modification time, then by name).
- It skips the newest file by default. Older modification time does **not** prove a file is closed. The optional newest checkbox requests another manual snapshot and never overrides live permission. Stopping the robot program usually stops its status too: use USB/local folder import when fresh status is unavailable.
- Each attempt captures a finite observed-size snapshot in its own `data/pull-staging/sessions/` folder, with durable receipts and preserved partials. Size or metadata changes cause attention; unchanged metadata still does not establish a closed, immutable source. A snapshot may omit later logging. Captures are labeled manual snapshots, not verified closed segments.
- Complete captures use the folder importer and archive by content hash. Every explicit capture re-reads eligible files, so equal-name/equal-size replacements cannot be silently skipped. Staging snapshots/receipts remain local and are outside the current archive-backup inventory; imported originals use the normal backup/export flow. Partial mutable snapshots are never appended or resumed across attempts. The advanced immutable closed-segment flow retains its separate durable-resume behavior. Nothing on the robot is changed or deleted.

The **setup wizard** at `/admin/setup` (P3.2) prepares SFTP only. It suggests the team address 10.63.91.2, reads the robot's SSH host key without logging in and shows its SHA-256 fingerprint, and pins it only after the mentor confirms it. It takes a login name and a private-key file path (no password is stored), runs a read-only test that lists the log folder, and saves `robot_known_hosts` and `robot_source.json` next to `hub.json`, setting `pull_source_config` and keeping `hub.json.previous`. Saving needs a hub started from a launch profile (StartHub.cmd). Configure the matching live profile and authoritative NT status before restarting for collection; until then use **Import logs from a folder**. Unknown host keys are always rejected. Tested over real loopback SSH/SFTP with synthetic logs only; it has not been run against a SystemCore or roboRIO.

### Historical 2026 logs (`wpilib-2026_akit-26`)

Last season's roboRIO logs (WPILib 2026, AdvantageKit 26) import with `--profile wpilib-2026_akit-26` (or `ImportLogs.cmd -LogProfile wpilib-2026_akit-26`). The profile was built against real September 2026 match logs, and its rules come from those files:

- `/Timestamp` is int64 **microseconds** with no unit metadata and equals each record's header time; any disagreement is a format error.
- The robot mode is not logged as an enum. `robot_mode` is a *derived* alias: AUTONOMOUS if `/DriverStation/Autonomous`, TEST if `/DriverStation/Test`, otherwise TELEOPERATED. Each derived alias names its source fields and rule.
- The wall clock is `/SystemStats/EpochTimeMicros`. `epoch_valid` is derived as "after 2020-01-01", because the roboRIO starts with a wrong clock until the Driver Station sets it. The epoch value is read during the cycle, not at the cycle timestamp. On real logs its offset moved 1–10 ms per cycle and occasionally ~100–150 ms, so every anchor states ±200 ms uncertainty instead of the Alpha 7 ±1 ms.
- There is no boot ID. Each file is its own boot (AdvantageKit starts a new file per boot), so runs from one file are grouped together.
- A final record cut off by power loss is accepted for this profile only: the bytes before it import normally and a `quality` row records the cut-off offset and length. The Alpha 7 profile still rejects incomplete framing.

The existing Alpha 7 analyzers report `unsupported` for 2026 runs. Real-length runs instead get the [per-run measurements](UI_AND_OPERATIONS.md#implemented-todays-practice-home-page-october-18-2026) (`run_metrics.py`), which feed the home page. 2025 logs (AdvantageKit 25) remain unsupported.

Single-file manual import:

From the repository root:

```powershell
python -m robot_test_hub.importer tests/fixtures/synthetic/alpha7-main.wpilog --data-dir data/import-demo --profile wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6 --synthetic
python -m unittest discover -s tests -p test_wpilog.py -v
python -m unittest discover -s tests -p test_importer.py -v
python tools/importer/qualify.py
```

The first three commands require only Python 3.10 or newer. Qualification reuses T04's locked installed Windows JDK25/library/native dependencies; it downloads nothing. `--expected-sha256` checks an external digest independently from format validity. `--retry` explicitly retries failed/unsupported jobs; a previous `running` job resumes automatically. CLI output is the persistent job state; non-success returns a nonzero exit code. The manual CLI owns the normal data-root lock, so stop the hub before importing into its data directory. Raw files remain private under the ignored data root. `--synthetic` is an explicit provenance assertion for invented data, not a way to sanitize real recordings.

The running hub also supports [explicit dashboard retries](IMPORT_RETRY.md) for eligible current local I/O/integrity or extractor failures. These use the retained original, preserve provenance, and run on the indexer without stopping collection. Invalid/unsupported/checksum/resource-limit outcomes still require investigation rather than an identical dashboard retry.

T09's long `/RealOutputs/TestHub/RobotMonotonicNs` and equivalent ReplayOutputs field use an exact profile nanosecond mapping because pinned Logger has no long-with-unit overload. The adjacent `RobotMonotonicNsUnit`, when recorded, must equal `nanoseconds`; conflicting types/units remain unsupported. These payloads also stay decimal strings even at small timestamps.

## Storage and idempotency

`Importer(root, db)` uses the caller's owned catalog connection. `install_schema(db)` creates the `import_artifacts`, `import_jobs`, and `import_requests` tables within the caller's transaction without committing or changing `user_version`; the main catalog migration can call it. The standalone CLI calls it transactionally after `open_catalog`. Automatic ingestion-worker, HTTP, and run-catalog integration belongs to the parent implementation and is not provided by these two modules alone.

The raw original is copied to `raw/<hash-prefix>/<sha256>.wpilog`, flushed and fsynced before catalog publication. Format validation never rewrites it. Filename aliases and expected checksum claims are recorded separately in `import_requests`; a filename is not identity. The immutable dataset is `derived/<job-id>/signals.jsonl` with a companion manifest. A job key includes source hash, extractor version, exact profile, and mapping revision. Reimporting identical renamed bytes creates no duplicate artifact/dataset/job. Publication uses a new-file hard link and never overwrites an existing original or derivative; existing bytes must match. A crash after derivative publication can retry with the same bytes. An interrupted job stays distinguishable from success, and a retry increments attempts. Byte integrity is checked when reading a dataset or returning a previously successful import. Changed archive bytes raise an integrity conflict and remain untouched.

Manual sources use `MANUAL_LOCAL`; public invented fixtures use `SYNTHETIC`. Worker callers may supply `VERIFIED_TRANSFER` only with the transfer's expected SHA-256. A mismatch remains `checksum_mismatch`, retains its bytes and claim, and never starts format decoding. Failed transfer provenance is labeled `TRANSFER_CHECKSUM_MISMATCH`; a subsequent explicit retry with the correct expected digest can establish `VERIFIED_TRANSFER`, retaining both claims. Unknown profiles, invalid framing, resource-limit failures, I/O/integrity failures, and partial field support have distinct persistent states/errors. A matching checksum does not prove format validity or terminal-event coverage. File fsync plus SQLite FULL durability is tested locally; power-loss durability of Windows directory metadata and network filesystems is not qualified.

## Dataset contract

Every row contains the source SHA-256, extractor version, profile, and mapping revision. Physical rows also retain their zero-based record index, source byte offset, entry ID/generation, original field path/type, raw metadata, and header timestamp in decimal-string nanoseconds. Start, finish, metadata update, and entry-ID replacement controls remain visible. Each observation represents an actual recorded update; unchanged replay-held values are not emitted as fresh observations.

| Row kind | Meaning |
| --- | --- |
| `control` | Original start/finish/metadata record and effective entry definition |
| `observation` | Recorded value, encoding validity, raw unit, separate cycle timestamp, category, optional canonical value/unit |
| `cycle` | Raw AK timestamp payload and explicit alias references with original record index and `updated_in_cycle` |
| `nested_array` | AK `/length` plus child-array reconstruction; missing elements are null and each child keeps its own freshness/provenance |
| `sample` | One drive-odometry array element paired with its recorded seconds timestamp and value/timestamp source references |
| `quality` | Missing/invalid/mismatched odometry arrays or invalid sample times; no invented sample |

`/Timestamp`, every other `int64`/`int64[]` payload explicitly labeled `nanoseconds`, and cycle/header nanoseconds are decimal strings. Nanosecond observations declare `value_representation` as `decimal_string_int64` or `decimal_string_int64_array`; no float conversion loses precision. The binary header is converted from microseconds exactly once, while the payload retains its extra nanosecond precision. Epoch doubles remain original microseconds with the separately logged validity flag; T06 performs UTC validation/mapping. Odometry timestamps remain original float seconds and include a rounded integer-nanosecond representation labeled `rounded_from_float_seconds`. Explicit array-to-timestamp paths also accompany decoded drive/turn arrays, so consumers can preserve each high-rate sample rather than treating the array as one loop measurement. Drive sample rows carry recorded connection references and `sensor_availability`; `validity=valid` denotes valid finite encoding, not a hardware health pass.

Recorded nested struct schemas decode little-endian primitive fields, nested structs, fixed arrays, character buffers, and struct arrays. Pose output remains nested according to its schema, for example `translation.x`, `translation.y`, and `rotation.value`. Schema text is preserved as observations. Bitfields, enum declarations, protobuf, raw/vendor types, and missing schemas remain explicitly unsupported with original payload hex; they produce `succeeded_with_unsupported` rather than successful-looking zero values. Nonfinite values become null with `nonfinite` validity and retain raw payload hex. Scalar and array lengths, UTF-8, control strings, timestamp range/ordering, and all file framing are strict. The per-record limit is 64 MiB; exceeding it is a resource failure, not proof of corrupt format.

`RealOutputs`, `ReplayOutputs`, input, metadata, and schema categories stay distinct. Unit metadata updates apply only from their control record onward. Canonical mappings are limited to explicitly supported units (meters, centimeters, meters/second, radians, seconds); raw values/units remain alongside them. Coordinate frames are assigned only to enumerated pose paths under the declared robot profile, with `coordinate_frame_source=explicit_profile_mapping`. Unmapped units/frames remain unknown.

The manifest publishes the exact alias map. Real DS mode is `/DriverStation/RobotMode` (pinned AK enum strings `AUTONOMOUS`, `TELEOPERATED`, `UTILITY`, `UNKNOWN`), DS enabled is `/DriverStation/Enabled`, and synthetic `/Fixture/Mode` is separate. T09 aliases enumerate typed `/RealOutputs/TestHub` fields individually, including dynamic `run_id` at `/RealOutputs/TestHub/RunId`, `state_known`, `run_active`, `status_enabled`, `status_mode`, `status_monotonic_ns`, and boot/robot IDs. Static boot/robot IDs remain separate `/Metadata` aliases so consumers can detect disagreement. T09's `StateKnown=false` invalidates its placeholder Enabled=false; it must never establish an authoritative disabled interval. Missing fields and empty inactive run IDs stay explicit. No identity is inferred from filenames. Cycle alias state can retain unchanged values, with freshness explicit; metadata last-values are merely an index and all revisions remain in physical observations. Bootstrap status is unknown (`null` / `not_recorded`), never fabricated.

`list_jobs()` and `get_job(id)` read persistent job status. `read_manifest(id)` and `iter_dataset(id, kind=None)` validate successful immutable artifact hashes and expose the manifest/rows; an unsuccessful job cannot provide a dataset. These are internal Python APIs. Public JSON transports must serialize catalog nanosecond integers as decimal strings, as the CLI does.

Local evidence: extractor and importer tests, official-reader comparison, checksum-versus-format separation, immutable byte preservation, restart/idempotency, entry reuse, missing/disconnected samples, schema decoding, explicit unsupported fields, and retry after dataset publication were exercised on Windows/Python 3.10. No real recording, robot, simulation, deployment, hardware diagnosis, cross-year support, or AdvantageScope GUI qualification is claimed.
