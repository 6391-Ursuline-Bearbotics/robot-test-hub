# Read-only Alpha 7 status bridge (T11 portion)

`robot_test_hub.status_bridge.StatusBridge` reads the robot's atomic string topic `/Telemetry/TestHub/Status` through the exact installed WPILib 2027.0.0-alpha-7 libraries. It requires an explicit NetworkTables host, port, expected robot identity and REAL/SIM runtime profile. It does not infer an address from team number, start a DS client, publish robot inputs, control actuators, or serve as a bulk transfer source. The application does not enable this bridge by default. The explicit live launcher composes it with SFTP; see [live setup](LIVE_TRANSFER.md). Configured credentials/host key, bench endpoint and physical commissioning remain required.

The Java executable uses `org.wpilib.networktables.NetworkTableInstance.startClient(String)` and `setServer(host, port)`; this Alpha 7 source does not expose the historical `startClient4` method. `StringSubscriber.readQueue()` provides actual timestamped updates, whereas `get()`/`getAtomic()` can return a retained value. Its `TimestampedString.timestamp` and `serverTime` fields are **nanoseconds** in the client/server time domains. The subscriber requests a 20 ms update period, one queued value, duplicate delivery and remote-only updates. It creates no publisher. An asynchronous connection listener remembers disconnects even if connectivity returns between polling iterations.

Every connection starts with unavailable robot permission. `StatusInbox` requires two advancing robot sequence/monotonic publications; its disconnect reset discards proof from the earlier connection. Initial retained values, duplicate robot payloads, reachability and cached polling cannot refresh the authoritative observation time. Nonadvancing NT timestamps, malformed/oversized status, a wrong robot/runtime profile, process death or connection loss revoke permission. A stalled publisher leaves its old observation time unchanged, so the collector's configured freshness check expires permission even while the NT server remains reachable.

To avoid mistaking delayed pipe output for a fresh publication, the child answers bounded clock probes using `WPIUtilJNI.now()` in the NT local clock. Python records each probe's send/receive times and uses the **send time** to calculate a conservative offset. Since probe execution follows its send, converted publication times are no later than the actual publication. The bridge supplies that converted time to `StatusInbox`, preserving the age of buffered publications. Probe responses over 500 ms are rejected; two seconds without a qualified probe disconnects status and terminates the child. This clock mapping establishes local age, not robot UTC synchronization or measured robot-network latency.

Java stdout contains only versioned JSON events (`ready`, `clock_probe`, `connected`, `disconnected`, `publication`, `rejected`). Publication payloads are limited to 16 KiB UTF-8; the Python line reader limits framing to 128 KiB to include JSON escaping. Native subscription queue depth is one, clock probe queues hold at most two requests, and the process emits at most one status per 20 ms polling iteration. Oversized/invalid bridge framing terminates the process. NT native reception itself is governed by the installed library; this is not an authenticated or hardened hostile-network protocol.

## Build and explicit use

The optional tool uses the installed Java 25 runtime at `C:\Users\Public\wpilib\2027_alpha7` and performs no dependency downloads. `tools/status_bridge/dependencies.lock.json` pins binary/source JARs plus Windows x86-64 `ntcore`, `wpiutil`, `datalog` and `wpinet` native ZIP hashes. Actual startup exposed `ntcore.dll`'s `wpinet.dll` dependency; it is included explicitly. A missing dependency, changed hash, unsupported OS/native profile or wrong Java version fails rather than substituting another library. Build output is ignored and reproducible from these pinned dependencies.

Automatic preparation currently requires this source checkout's `tools/status_bridge` files; the optional Java tool is not packaged into a hub wheel. An explicitly supplied prepared Java command/environment can be used by another launcher. Startup failure always leaves status unavailable.

```powershell
python tools/status_bridge/run.py build
```

An explicitly configured adapter can start the wrapper and use its status inbox without mutating robot state:

```python
from robot_test_hub.status_bridge import StatusBridge

# Substitute a configured endpoint and expected robot identity. This opens NT.
with StatusBridge("configured-host", 5810, "configured-robot-id") as bridge:
    status = bridge.status()
    # The collector must still check status.observed_at, enabled, generation,
    # boot identity and transfer_allowed according to its existing idle gate.
```

The standalone Java event stream is available through `python tools/status_bridge/run.py bridge --host CONFIGURED_HOST --port CONFIGURED_PORT`. Only the Python wrapper converts clock probes and validates the full authoritative status contract; raw process stdout is not robot transfer permission. `synthetic-publisher --port PORT` is a qualification harness that binds **127.0.0.1 only**; it is not a production robot publisher or source adapter.

## Validation evidence

On October 3, 2026, Windows x86-64, Python 3.10.7 and the installed Java 25/locked Alpha 7 libraries:

```powershell
python -m unittest discover -s tests -p test_status_bridge.py -v
$env:ROBOT_HUB_STATUS_INTEGRATION = '1'
python -m unittest discover -s tests -p test_status_bridge.py -v
```

The portable command passes seven protocol/process tests and explicitly skips the native loopback test. With the explicit environment opt-in, **all eight tests pass**, including a real NT loopback server/publisher and a separate Java subscriber process. Qualification creates its own ephemeral loopback port; it never connects to a robot endpoint. It verifies a retained disabled value remains unknown until robot progress, correctly converted publication age, duplicate payloads not refreshing time, a reachable stalled publisher retaining its old age, server disconnect/reconnect requiring new proof, and subscriber process death revoking permission.

Deterministic tests additionally inject delayed stdout by 60 seconds, a slower than 500 ms probe, future/nonadvancing publication timestamps, a wrong robot identity, invalid native profile/configuration, process exit and oversized pipe output. These tests establish software eligibility behavior, not SystemCore network performance, credentials, SFTP cancellation, real DS/robot timing, USB logging or hardware transfer qualification. No robot code, build, deployment or hardware connection occurred for this bridge work.

The final standard suite reported by the Sol backup/integration agent passed **221 tests, with one explicit optional native-bridge skip**. The separate native opt-in command above ran that skipped qualification and passed all eight status-bridge tests. The prior report-test failure was corrected: unknown mapping revisions remain unsupported, simulated qualified upgrades are tested explicitly, and qualified imports with equal update/creation times are ordered by catalog insertion rather than arbitrary hashes. Six focused report tests passed; old immutable import evidence remains preserved.
