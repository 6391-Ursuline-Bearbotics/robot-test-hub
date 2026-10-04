# Run catalog and clock search

The local pipeline imports verified recordings with an explicit supported profile and indexes their enabled intervals. Opaque transfer-demo files stay unindexed. Unknown or invalid format jobs remain visible in the import catalog; their checksum-verified originals are preserved. The importer and indexer run independently of collection and robot status. A worker failure appears in service diagnostics; it does not silently become a successful report.

Run identity is independent of the physical log file. The catalog merges agreeing overlaps from multiple segments in one robot/boot, keeps auto-to-teleop as phases in one uninterrupted enable, and separates each disabled-to-enabled interval. Missing identity stays explicitly unknown and cannot merge across unrelated artifacts. Gaps, conflicting overlap, unknown state, and an enabled end-of-recording produce incomplete coverage. T09's explicit unknown status overrides older driver-station placeholders. All source import IDs, boot IDs, source types, runtime modes, and raw nanosecond intervals remain attached.

Clock anchors use the imported nanosecond AK cycle payload, the explicitly fresh epoch-microsecond update, and its validity flag. A replay-held epoch value is not a new anchor. Invalid samples, more than 250 ms between anchors, and clock discontinuities split coverage. Mapping uses centered integer/rational interpolation only between anchors; no unmeasured extrapolation is supplied. The default 1 ms anchor uncertainty and 5 ms discontinuity tolerance are initial analysis policy, not hardware synchronization measurements. A run can have several disjoint UTC intervals or no wall-clock mapping. Search tests each interval separately, expanded by its stated uncertainty, rather than filling a clock jump with fictional coverage.

`/runs` offers a local calendar date/time, explicit IANA timezone, and a neighborhood (default ±2 minutes). A repeated DST time returns both UTC offsets for the user to choose. A nonexistent spring-forward time fails visibly. UTC and robot nanoseconds remain decimal strings across HTTP. Pinned `tzdata==2026.1` supplies the timezone database on Windows. “All runs” includes unknown wall-clock runs. Run details expose phase/coverage/provenance and notes whose intended UTC intervals overlap as **candidates**, without claiming exact incident correspondence or robot acknowledgment.

Catalog revisions are immutable and keyed by input identities/content plus derivation policy. Repeating an identical build does not add another revision. Improvements to mapping or added segments create a new revision and preserve previous evidence. The current index rebuild reads the successful cycle datasets and replaces the current revision atomically. Large season archives need a subsequent incremental per-boot index and resource qualification; the current implementation has only been exercised with small public fixtures. Signal datasets remain streaming JSONL, but the run derivation currently collects cycle aliases in memory. This is not a measured season-scale performance claim.

The server indexes existing successful imports on startup and new verified WPILOG transfers as they arrive. Manual import is deliberately an offline operation protected by the data-root ownership lock. Stop the hub first:

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.importer tests/fixtures/synthetic/alpha7-main.wpilog --data-dir data/demo --profile wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6 --synthetic
.\.venv\Scripts\python.exe -m robot_test_hub.server --data-dir data/demo
```

Only invented fixture data may use `--synthetic`. To explicitly rebuild without starting the service, use `python -m robot_test_hub.runs --data-dir data/demo` after successful import. It acquires the same single-owner lock.

Validation covers segments spanning runs and runs spanning segments, auto-to-teleop phases, rapid enable intervals, gaps/reboots/unknown states, immutable mapping revisions, held/invalid epoch samples, overlap-order determinism, DST gaps/overlaps, exact sub-microsecond payload precision, and genuine fixture transfer through automatic import/indexing. Physical clock quality and older log profiles remain unqualified.
