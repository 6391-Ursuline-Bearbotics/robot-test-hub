# Alpha 7 status tool

Read [STATUS_BRIDGE.md](../../docs/STATUS_BRIDGE.md) for the exact protocol, version/hash lock, Python wrapper, explicit endpoint requirements and loopback evidence.

`python tools/status_bridge/run.py build` compiles without connecting anywhere. The `bridge` action requires `--host` and `--port` and only subscribes to `/Telemetry/TestHub/Status`. `synthetic-publisher` is a loopback-only qualification harness. Neither action is started by the default hub application.
