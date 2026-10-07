# Live collection setup and qualification

October 7 independent review adds actual forced-disconnect qualification: recognized SSH/EOF transport failures become redacted retryable errors, preserving the durable offset and keeping collection alive through reconnect. Authentication, identity and permission failures keep their distinct handling. A loopback regression forces an unexpected disconnect during a read, reconnects and verifies the byte-exact archive; unrelated programming errors are not silently converted to transport retries.

Implemented locally on October 6, 2026. The hub composes the authoritative Alpha 7 NetworkTables status reader with read-only SFTP and the durable transfer/import workers. Actual SSH/SFTP and Alpha 7 NT loopback tests pass. **Physical SystemCore operation remains unqualified; no robot was connected to or deployed.**

## Recording and transfer behavior

The robot records continuously. Its experimental receiver closes a recording after five seconds continuously permitted disabled following a run, opening a successor without restarting AdvantageKit. Idle recordings rotate after five minutes or 256 MiB when permitted disabled. Run IDs and file IDs are independent: importing reconstructs runs from logged state rather than filenames.

Closed files wait for disabled-only source hashing. The hub downloads digest-ready immutable files from the receiver's paged manifest. Open bytes and bytes awaiting a digest remain separate from downloadable backlog. Orphan, changed, failed or incomplete recordings produce attention rather than a false caught-up state.

The default live gate requires ten seconds continuously fresh and disabled; status freshness is at most 500 ms. The NT reader requires advancing publications and rejects cached, replay, wrong-robot and disconnected status. Each operation checks permission and boot/mode generation before and after receiving data. A watcher closes the SSH channel when permission expires. Enabling never waits for the collector. Bytes already in flight may still arrive; physical cancellation tail and server load require measurement.

One SSH connection is reused while eligible. There is one outstanding operation, no prefetch, at most 256 KiB per collector chunk and 32 KiB per SFTP read. Durable offsets survive disconnect and hub restart; canceled chunks are discarded before commit. Authentication/identity failures require attention; transient transport failures use persisted retries. Local hash verification and indexing run independently.

The transfer page shows remaining files/bytes, progress, speed, ETA, pause reason, status freshness, open recording observations and pending digests. ETA uses measured throughput; unknown or blocked backlog cannot claim a finish time. Live sources hide and reject simulated enable controls. `/api/v1/status` also exposes redacted `connection` details and `hardware_qualified: false`.

The local NT reader is supervised. Child exit, invalid framing, probe timeout and launch failures revoke permission and retry with exponential backoff from one to thirty seconds. The previous reader must be reaped before a replacement starts. Restart clears its clock and protocol state; replacement status must advance and the full disabled idle delay must elapse again. A local revocation epoch prevents an old permission token from becoming valid when the robot's boot/mode generation has not changed. The connection view shows reader recovery, retry countdown and restart count. A reachable but stalled robot heartbeat still pauses transfer; restarting the local reader does not manufacture robot progress.

## Prepare the robot source

Implementation is in sibling repository `2027-6391-Rebuilt-V3`. REAL retains its ordinary WPILOGWriter by default. For a later authorized bench deployment, configure these JVM properties with the confirmed mounted writable directory:

```text
-Dfrc.robotId=6391-practice
-Dfrc.testHubRecordingDir=/CONFIRMED-USB-MOUNT/testhub
```

This explicitly selects the experimental rotating receiver as the REAL file writer, avoiding a second bulk writer. SIM accepts a local absolute directory; REPLAY ignores this option. The receiver snapshot supplies `active_segment_id` in atomic status. Robot compilation and all 32 automated tests passed with the Alpha 7 JDK25; this includes stepped simulation and official replay. Hardware qualification remains false. Ordinary AdvantageKit logs alone do not provide the immutable-close manifest. Do not infer close from disabled state or stable size. See [T10 encoder and limits](T10_IMPLEMENTATION.md).

## Configure the computer

The initial launcher requires this source checkout on Windows x86-64, Python 3.10+ and the pinned Java 25/Alpha 7 installation. A wheel alone does not contain the Java tool. Install the transport dependency:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[sftp]"
```

Save source settings under ignored `data/`, for example `data/systemcore-source.json`. Replace every placeholder with confirmed values:

```json
{
  "schema_version": 1,
  "host": "CONFIGURED-SYSTEMCORE-HOST",
  "port": 22,
  "username": "CONFIGURED-READ-ACCOUNT",
  "known_hosts": "known_hosts",
  "private_key": "collector_key",
  "log_root": "/CONFIRMED-USB-MOUNT/testhub",
  "robot_id": "6391-practice",
  "timeout": 1,
  "max_read": 262144,
  "freshness": 0.5
}
```

Credential paths resolve relative to this file. Pin the host key using a fingerprint verified through a trusted setup channel. Unknown/changed keys are rejected. No automatic trust, SSH agent, credential discovery, remote writes/deletes or shell commands are used. The initial launcher reads a local private-key file without interactive password prompts. Prefer an account restricted to the recording directory where supported. Protect the key; do not commit it, real recordings or source configuration to this public repository.

NT host and port are explicit and may differ from SSH. Substitute the confirmed endpoint and port. Use the repository's Python environment so the pinned transport version is selected:

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.server --source-config data/systemcore-source.json --nt-host CONFIGURED-NT-HOST --nt-port CONFIRMED-NT-PORT --data-dir data/practice --wpilib-install C:\Users\Public\wpilib\2027_alpha7
```

Open `http://127.0.0.1:6391`. Explicit `--idle-delay` or hub-config `idle_delay` overrides the ten-second live default. Use a separate practice archive from the demo. Credentials stay outside diagnostic/config snapshots. Ctrl+C stops the reader/workers preserving committed checkpoints. Reachable SSH alone never grants permission.

The live launcher verifies the locked native dependencies and compiles the Alpha 7 status reader before opening the archive or starting any endpoint connection. Missing dependencies, hash mismatches, wrong Java version and compiler failures stop startup with fixed guidance. After preparation, the reader still waits until archive ownership and loopback HTTP binding succeed before starting. Its prepared command/environment are reused for supervised restarts.

## Check setup before connecting

Run the offline readiness check first, with the same source/NT/toolchain settings you will use for the server:

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.live_check --source-config data/systemcore-source.json --nt-host CONFIGURED-NT-HOST --nt-port CONFIRMED-NT-PORT --wpilib-install C:\Users\Public\wpilib\2027_alpha7
```

The check validates source settings, the pinned Paramiko version, the exact SSH host/port entry in your known-hosts file, and local private-key readability. It applies the live idle/freshness/chunk rules, verifies locked Alpha 7 dependencies and compiles the status reader locally. It opens no NT/SSH connection, performs no DNS lookup, and opens no hub archive. It may create/update the ignored local status-tool build directory. Key contents, endpoint values, paths and provider exceptions are omitted from its report; compiler output is captured rather than mixed into JSON. Each check uses one immutable source-settings snapshot. Rerun it after editing settings; a previous READY report does not qualify a later configuration.

For nonstandard SSH ports, known-hosts needs an entry named `[host]:port`; an ordinary `host` entry is insufficient. The check only establishes that a matching pin is configured. You must independently verify the fingerprint through a trusted setup channel, and later SSH connection must match that pin. A readable key does not prove that the server authorizes it.

Use `--config data/hub-config.json` and/or `--idle-delay` to check overrides. Add `--json` for a redacted machine-readable report. Exit code 0 means local prerequisites passed; 2 means attention is needed. `hardware_qualified` remains false in both cases. READY does not establish endpoint reachability, server access, receiver manifests, live status or physical performance; complete the bench worksheet after the team's authorized robot setup.

## Validation and bench qualification

`python -m unittest discover -s tests -v` ran 236 tests: 234 passed, two optional native tests skipped. Eleven SFTP tests cover actual temporary SSH/SFTP, pinned keys, resume after restart, enable during a blocked read, no enabled/stale reads, connection reuse, authentication failures, manifest integrity, path/symlink confinement, open/pending bytes and orphan/malformed attention.

This opt-in command uses only ephemeral loopback endpoints, actual locked Alpha 7 NT publisher/subscriber, SSH/SFTP, hub workers and HTTP together. All four tests passed on Windows/Java 25. It verifies enabled pause, disabled collection, byte-exact archive, import, live/demo separation and stalled-publication pause:

```powershell
$env:ROBOT_HUB_STATUS_INTEGRATION = '1'
python -m unittest discover -s tests -p test_live_transfer.py -v
Remove-Item Env:ROBOT_HUB_STATUS_INTEGRATION
```

Next inputs: confirmed SystemCore SSH endpoint/account, local key and verified host key, receiver log root, authoritative NT endpoint. Then perform [T12](IMPLEMENTATION.md#t12--physical-transfer-qualification) using [VALIDATION](VALIDATION.md): baseline versus collection loop/DS/USB performance, enable/disconnect/power interruption, resume/original hash equality, cancellation tail, throughput and idle budget. Source-server disabled enforcement is not implemented; current gating is client-side. Defaults remain provisional. Camera capture, phone pairing, robot marker delivery and source deletion are separate tasks.

Use the [bench worksheet](LIVE_TRANSFER_BENCH.md) to record the actual setup and measurements. This worksheet supplies the remaining hardware qualification; local tests cannot supply its results.

The [session recorder](TRANSFER_SESSION.md) saves redacted queue, status, rate/ETA and reader-recovery observations from the running local hub. It provides a timestamped report for the worksheet without changing robot or collector behavior. Polling estimates do not measure physical network traffic or cancellation tails.
