# Offline sample-log exercise — implementation handoff

Status: design only, October 15, 2026. No selftest module, bundled runtime sample or TestHub.cmd has been implemented. The last verified code checkpoint is c376a5abf0c0a819988c3abd93ea3716a352e036; its 884-case local suite and all four CI jobs passed. This task extends T18 laptop preparation and does not replace physical commissioning.

## Intended use

After SetupHub, an operator should be able to exercise the installed local data flow before configuring a robot connection. Keep CheckHub's existing static readiness contract unchanged. Introduce a separate TestHub.cmd so opening a private launch profile, active archive, network endpoint or native status reader is unnecessary.

Planned commands, unavailable until implementation:

```powershell
.\scripts\windows\TestHub.cmd
.\scripts\windows\TestHub.cmd -Json
.\scripts\windows\TestHub.cmd -Analytics
```

The Python entry point is planned as python -m robot_test_hub.selftest with only --json and --analytics. The internal API is run_selftest(*, analytics=False) -> dict. No configuration, source address, profile, archive or destination arguments are accepted. Importing the module has no side effects.

## Actual work to exercise

1. Load a bundled exact copy of the public, genuinely generated alpha7-main.wpilog through importlib.resources. Verify its bytes against a small locked digest manifest before using it. Tests must independently compare the bundled bytes with the existing official fixture. Runtime must not import tests or regenerate evidence.
2. Create a fresh owned temporary workspace. Exercise the actual Collector with an in-memory synthetic source and deterministic clock. Verify enabled/stale gating, the full disabled idle interval, a partial committed transfer, enable interruption, collector close/reopen, resumed offset, and final exact digest. Bounded ticks must fail clearly if expected progress never occurs.
3. Run the actual Pipeline and Importer. Verify the independently known two runs and fixture timestamps, exact saved report digests and maintained report membership. A successful download alone cannot pass this stage.
4. Exercise a real time-mapped run search using known fixture expectations. Save a delayed synthetic Notebook observation, close/reopen its catalog connection, and verify its original incident time and revision remain unchanged.
5. Create, verify and restore an actual coherent Backup into a fresh owned child location. Reopen the restored archive and compare original hashes, note revisions, run references and report receipts. No user-selected backup or archive may be opened.
6. If --analytics was requested, require the pinned DuckDB dependency and exercise real Parquet conversion, a known query result and portable summary artifacts through existing APIs. Missing dependencies or mismatched pins fail this requested stage; they are not a pass. Without the flag, explicitly skip analytics and do not import DuckDB.

All evidence is invented and must remain labeled SYNTHETIC. Do not alter core import provenance rules to simplify the exercise. Verification of synthetic transfer receipts never establishes physical robot data or hardware health.

## Output and failure behavior

Return bounded schema-1 JSON with success, source_type=SYNTHETIC, hardware_qualified=false, network_contacted=false, active_archive_opened=false, overall_health=not_assessed, analytics_requested, cleanup={state:passed|failed}, and stages=[{id,state,code,guidance}]. Stage IDs are fixture, transfer_gating, transfer_resume, import_index, search, notebook, backup_restore and analytics. States are passed, failed and skipped. Use fixed codes and useful fixed guidance; omit paths, arbitrary exceptions and raw sample payloads.

Failure stops dependent stages and marks them skipped. Overall success requires every requested stage and cleanup to pass. CLI exit is zero only on success. Human-readable output must clearly identify the synthetic exercise and its physical limits.

No service/server, browser, camera, SSH/NT adapter, cloud publisher, scheduler task, package installation or robot/native tool may start. Optional analytics may use the existing isolated local Python worker if required by its actual API; own its cancellation, child processes and cleanup explicitly. No worker may survive return. Keep all resources inside the allocated workspace and close SQLite connections explicitly before cleanup. Retain and report cleanup failure instead of claiming successful deletion or releasing ownership prematurely.

## Windows integration

Add Test to SetupHub.ps1's action set and a TestHub.cmd wrapper. Test must require the existing per-install virtual environment, anchor the full source working directory, and invoke the selftest module directly. It must not resolve or read a profile, require LOCALAPPDATA, fall back to demo, install dependencies or start the hub. Forward -Json and -Analytics as native arguments and propagate exact exit codes. Reject profile inputs for Test and reject test-only flags on other actions. Preserve existing wrapper quoting, private path checks and startup behavior.

## Implementation boundaries and acceptance

Use separate Sol implementation assignments for the selftest/sample/package data and Windows wrapper, with independent review before delivery. Parent integration should verify the real CLI and Windows wrapper, plus a built wheel outside the checkout to prove the bundled resources are sufficient. Keep package building offline with installed build dependencies; never fetch missing packages as a hidden test step.

Acceptance includes actual end-to-end expected results, repeated runs, missing/tampered sample failure, injected import/backup failures, cleanup and ownership on every outcome, no network/profile/active-archive access, requested analytics dependency failure, optional analytics success, bounded/redacted JSON, CLI failure codes, wrapper argument forwarding and missing-environment behavior. Deny sockets and real adapter/service constructors in independent tests. Run the relevant suites and complete repository regression suite after production changes; verify CI on the exact published revision.

Update WINDOWS_SETUP.md, LOG_FLOW_COMMISSIONING.md, IMPLEMENTATION.md and VALIDATION_CURRENT.md only with executed evidence. A successful exercise proves this isolated local data path, not SSH permissions, USB durability, DS load, real clocks, camera alignment, Drive synchronization or school readiness.
