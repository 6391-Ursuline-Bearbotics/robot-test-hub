# Offline sample-log installation exercise

Implemented October 15, 2026. Run this after SetupHub and before connecting the robot. It exercises the installed local data flow using a bundled public synthetic WPILOG and a fresh temporary workspace. CheckHub remains the separate static live-readiness check.

## Run

From the complete source installation:

```powershell
.\scripts\windows\TestHub.cmd
.\scripts\windows\TestHub.cmd -Json
.\scripts\windows\TestHub.cmd -Analytics
```

The Python equivalent is `.venv\Scripts\python.exe -m robot_test_hub.selftest --json`; add `--analytics` to require analytics. The existing virtual environment is required. TestHub never installs packages, resolves a private launch profile or starts the hub. It works from another current directory and without LOCALAPPDATA or Java. Test accepts only Json and Analytics; profile/interpreter overrides are rejected.

## What passes mean

The exercise invokes the actual Collector, Pipeline, Importer, RunCatalog, Notebook and Backup APIs. It checks:

1. The bundled 4,530-byte sample and locked manifest match the independently known digest and seven exact cycle timestamps.
2. Enabled, stale and insufficient-idle status block source discovery/reads. A partial transfer survives close/reopen and resumes its committed offset after a complete idle interval, preserving exact original bytes.
3. Import creates the expected two runs and exact stored report identities/hashes. A known UTC query finds its expected run.
4. A delayed note survives catalog reopen with its original incident time and exact revision.
5. A real verified backup restores into another fresh owned location. Both original-byte copies, run references, exact report receipts and note revisions match.
6. With Analytics requested, pinned DuckDB converts the original into Parquet, a real query returns seven expected cycles, and two run summaries have verified portable artifact hashes. Missing/wrong dependencies fail this requested stage. Without the flag analytics is explicitly skipped without importing DuckDB.

All top-level exercise evidence is SYNTHETIC with hardware health not assessed. Existing transfer/import provenance rules remain unchanged; verified invented bytes do not qualify physical telemetry.

## Results and cleanup

Human output identifies each stage and its fixed guidance. Json returns bounded schema-1 output with success, synthetic provenance, physical limits, cleanup state and stage receipts. It contains no local paths, raw sample payloads or arbitrary exception messages. The CLI returns zero only when every requested stage and cleanup pass; failures skip dependent stages.

No service, browser, real transport, camera, cloud publisher, native reader or scheduler task starts. There are no source/profile/archive/destination parameters. Every catalog and artifact belongs to the newly allocated workspace, including the restored copy. Connections close before deletion. A close failure reports failed cleanup and retains the corresponding resources and owner until process exit instead of claiming successful cleanup. A deletion failure also fails the exercise and preserves the remaining workspace.

## Executed qualification

Twelve author and five independent tests cover expected results, requested analytics success/failure, fixture/timestamp corruption, exact restored originals/reports/notes, repeated runs, injected import/backup failures, and ownership retention for failed primary/restored catalog closes. Independent tests deny socket creation, service/native/transport constructors and child processes, verify isolated SQLite locations, and check that no worker thread survives return.

Seven Windows wrapper cases use actual CMD and PowerShell with intercepted Python startup to check argument/exit propagation, hostile path quoting, missing LOCALAPPDATA, no profile access and no install fallback. Fourteen existing setup cases remain passing. Parent qualification also executed the real wrapper in human, Json and Json+Analytics modes from an unrelated directory with no usable Java/live profile; all passed.

A wheel built offline with already-installed build tools was extracted outside the checkout. Isolated Python imported the extracted package and passed core and analytics modes using the installed dependencies. Both runtime sample resources were present; tests/tools were absent. This qualifies selftest packaging only: live installation still requires the full checkout for tools/status_bridge.

See [current validation](VALIDATION_CURRENT.md) for the complete-suite checkpoint. No real robot, camera, Drive destination or active archive was contacted. SSH access, USB durability, DS load, real clocks, camera alignment, school sign-in behavior and physical readiness require the [commissioning checklist](LOG_FLOW_COMMISSIONING.md).
