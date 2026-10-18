# Full-source recording quality and recorded status

Streaming stages S1 and S2 have local implementations, October 18, 2026. Executed qualification is recorded separately in [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md). This path evaluates recording quality and source-reviewed Phoenix/SystemCore recorded status through complete verified sources. Tracking, steering and streamed incident proof remain unfinished stages S3–S4 in [STREAMING_ANALYSIS.md](STREAMING_ANALYSIS.md). Overall robot health remains not assessed.

## Normal operation

Normal small recordings keep the existing five-check automatic pipeline 7. When automatic selection reaches its existing row/scan resource limit, that immutable unavailable report is retained and a separate pipeline-9 job is queued. The indexing worker advances it in bounded turns even when no new import or plan revision arrives. Explicit historical pipeline 5/6/7 generation retains its old selector, inputs, output bytes and lack of stream scheduling. Explicit pipeline-8 and pipeline-9 scheduling use a separate input contract and never construct an empty tuple Evidence. Pipeline 8 retains its original quality-only interpretation, saved report bytes and reducer checkpoints.

Pipeline 9 retains the five-check order: quality, tracking, Phoenix status, SystemCore status and steering. Quality and the two status checks consume the full verified input; tracking and steering explicitly remain unavailable until converted. Pipeline 8 keeps its original quality result and four unavailable checks. Ordinary pipeline-7 reports remain usable, and a streamed result does not replace their saved bytes. A recorded warning is not a physical diagnosis.

Only completed byte verification, reducer completion and final evidence publication allow a saved report. Pending scans, canceled work, resource failures and missing inputs are never successful prefix analyses. Failures have fixed operator explanations; arbitrary exception text, private context and source paths are excluded from the progress API.

## Evidence and recovery

Catalog schema 10 installs metadata only. Its jobs, sources, chunks, checkpoints, persistent uniqueness and reservation tables are created without opening source artifacts. `StreamRunner` validates the supported format profile/extractor/importer/mapping tuple, source sizes and hashes, run/configuration/plan/component context and the structural chunking policy. Original import provenance takes priority over payload runtime labels: synthetic recordings stay synthetic.

Raw WPILOG, manifest and complete dataset bytes are verified in bounded phases. The new admission path does not call the legacy synchronous `Importer.read_manifest`, `iter_dataset` or `_check_dataset` helpers. Those helpers still exist on the older importer/catalog/pipeline-7 paths; this increment does not make the entire application startup or ingestion path incremental.

The selected evidence uses `stream-evidence-1`, framework `stream-analysis-1` and selection policy `full_source_scoped_rows_1`. Deterministic normalized JSONL chunks carry explicit source/line references and before-run, in-run, after-run or unscoped meaning. A versioned SHA-256 chain binds chunk bytes and range metadata. Full inventory generation is paged on disk rather than accumulated in a growing Python list. Chunk policy is part of input identity; runtime work-turn sizes, elapsed-time yields and storage allowances are not.

Chunk bytes are durably published before their metadata, reducer state and uniqueness contributions commit together. Checkpoints have exact hashes and chunk ordinals. On a fresh process or restored archive, raw/manifest verification restarts, the complete dataset byte prefix is rehashed from zero, and committed chunks are reconstructed and checked before new reducer contributions are accepted. A matching selected chain alone cannot validate changed unselected bytes. The same live byte hash continues through EOF; there is no serialized hash object or pickle checkpoint. Copied files may have new physical identities; their portable content pins stay exact and their new live identities are checked during reading.

Cancellation and resume are currently internal worker APIs. Canceled work retains its evidence and resumes with verification; no report is published from a canceled prefix. Terminal failures remain terminal for that staging identity. Refreshing the progress view does not retry analysis. An explicit operator retry/new-request workflow is still needed. Source changes require inspection rather than silently retargeting old evidence.

Normal turns retain no OS file handles. A failed close that leaves a handle open retains its strong owner; service cleanup remains pending and keeps the archive lease until actual closure. Connection/transaction ownership is explicit: writer operations reject caller-owned transactions before installing SQL callbacks or executing SQL. SQL budget exhaustion is isolated to the selected generation after rollback; unrelated database/schema failures remain visible.

## Recording-quality scope

Every admitted source is traversed through EOF. Quality counters, duration and findings are attributed only to the run's declared interval. Exact cycle payload time determines observation scope when the wire timestamp has lower precision. Source boundaries reset adjacent timing. A source that leaves the run cannot reenter it by regressing its clock and manufacture new in-run coverage.

Recording quality counts cycles/gaps, invalid or explicitly stale observations, recorded disconnected sources and clock coverage. Persistent uniqueness prevents a false connection observation reused by many samples from becoming many independent failures. Counts and decisions use all admitted data; retained supporting references share a 128-item budget, with exact omission totals. Full-source ordering/overlap/after-run hazards remain separate from in-run event attribution. Physical acquisition freshness and USB health stay unqualified.

## Recorded status scope and policy dispatch

Pipeline 9 pins `recorded-status@stream-recorded-status-1`, mapper `stream-cycle-mapper-1`, and distinct `stream-phoenix-status-1` / `stream-system-status-1` analyzer declarations. The composite checkpoint retains the unchanged recording-quality reducer alongside the two status accumulators. The worker dispatches by both saved pipeline version and exact reducer identity. A pending pipeline-8 job still resumes with `recording-quality@stream-recording-quality-1`; incompatible policy/reducer pairs are refused before advancing. No schema migration or rewriting of old reports/checkpoints is needed.

The mapper retains only required live fields and their original source, entry generation, record index and timestamp. Pre-run records can establish held state, while only in-run cycles contribute status counts and intervals. Source transitions, lifecycle controls, incompatible schemas, invalid observations and discontinuities prevent affected state from bridging incompatible evidence. After-run flags and counter updates are not new in-run warnings. Full-source ordering hazards remain separate qualification evidence.

Phoenix evaluates the source-reviewed diagnostics profile with exact recorded false-status decisions, deduplicated observation identities and bounded supporting references. SDK receipt/transmit timestamps, validity and ages remain recorded observations; physical acquisition time and native timestamp availability remain unqualified. SystemCore evaluates all 17 source-reviewed logger fields, exact true flags and canonical integer counter changes. Held states are counted separately from distinct source records. Counter increases/decreases do not establish physical event counts, resets or current/latched/cumulative fault semantics.

Voltage minimum/maximum and a stable time-weighted left-held mean are descriptive only. Adjacent compatible logger endpoints must advance by at most 250 ms; gaps and lifecycle/source boundaries do not become invented weighted coverage. No voltage fault threshold or physical freshness is introduced. Persistent uniqueness keys and bounded checkpoint state prevent full-run identities or example lists from accumulating in Python memory. Both converted checks retain explicit coverage limitations and bounded source-reference omission totals.

## Resource policy and saved configuration

| Boundary | Default |
| --- | --- |
| Input work per turn | At most 4,096 records or 4 MiB, with a cooperative 25 ms elapsed check |
| Raw line / normalized selected row / deterministic chunk | 1 MiB each |
| Original import manifest | 4 MiB |
| Pinned sources per run | 128 |
| Stored supporting references per check | 128 shared across findings |
| SQL work allowance | Approximately five million VM steps |
| Conservative catalog-plus-stream disk allowance | 8 GiB |
| Free-space reserve | 2 GiB |

The elapsed check is cooperative. A single file operation, fsync, atomic chunk/reducer transaction or metadata operation can exceed it; it is not a real-time deadline. Current service processing also waits 0.5 seconds between worker ticks. Active processing benchmarks below exclude those waits and therefore are not service completion times or driver-station load guarantees.

The disk allowance includes the entire catalog SQLite page allocation plus reserved stream files. It intentionally overcounts unrelated catalog data instead of pretending that shared SQLite indexes have zero cost. Reservations are durable before writes and retain charges for unfinished or orphaned owned work. Uniqueness/index growth is checked before reducer checkpoint commit. Existing large catalogs may require a larger allowance even before their first stream. No automatic evidence deletion or retention policy is added.

The saved private hub configuration accepts `stream_max_spool_bytes` (default 8589934592; 1 MiB–1 TiB) and `stream_min_free_bytes` (default 2147483648; 0–1 TiB), as strict integers. Despite the historical field name, the first value is the conservative catalog-plus-stream allowance described above. Edit the existing configuration and restart the hub to change admission limits; keep its archive/source profile unchanged. Raising an allowance does not qualify disk capacity, performance, freshness or a failed job for automatic retry.

## Investigation and backup

Selected runs show a separate Full-log analysis progress section with source counts, exact byte/row counters, replayed bytes and saved-report availability. Read-only `/api/v1/runs/<id>/stream-progress` queries bounded SQLite metadata only; it never constructs a runner, scans artifacts, installs schema or repairs work. Live pages default to five jobs (maximum 20) and may shift as new jobs arrive. They are explicitly not immutable history snapshots. Missing/corrupt/budget-exhausted progress returns unavailable with unknown totals. Exact counters/timestamps use decimal strings, including values beyond JavaScript's safe integer range. Failed refresh clears live progress claims while preserving separately saved report and note evidence.

Coherent backups preserve source originals/datasets/manifests, exact committed chunks, ownership markers, final inventories and all resumable metadata/uniqueness/reservation rows. Restoring partially completed work into a fresh root and connection re-verifies it before continuing. Actual generated-data integration reproduced the identical final selection chain and full saved report after that restore.

## Executed qualification and limits

Separate Sol author, integration and independent tests exercise actual imported WPILOGs, late findings beyond both old limits, exact time scope, source origin, batch invariance, fresh-connection recovery, complete byte hashes, conflicts, cancellation, SQL/disk/line limits, contribution rollback and cleanup ownership. Browser qualification and measured timings/memory are recorded in [current validation](VALIDATION_CURRENT.md).

Run the complete focused group with `python -m unittest discover -s tests -p "test_stream*.py" -v`. `tools/bench_stream_analysis.py` is a synthetic qualification helper for the full source checkout: it imports the public test generator, builds isolated recordings and measures streaming in fresh `-I` child processes. Importer/run-catalog cost is measured separately, and fixed-size latency histograms avoid an expanding per-turn sample list. It does not contact a robot, camera or network endpoint. `tools/bench_stream_status.py` similarly measures pipeline 9 with all four reviewed Phoenix profiles and the 17 SystemCore fields; final two-size evidence and concurrent-load limitations are recorded in current validation.

Remaining work includes streaming tracking/steering and exact percentiles, shared-source/multiple-run processing efficiency, service throughput and DS load qualification, explicit operator retry controls, complete streamed incident proof/export and portable evidence integration. Current tuple incident cases cannot be silently converted to stream8/9 input, even with evaluator-version opt-in; streamed case reconstruction/export remains a separate format and milestone. Physical commissioning, diagnosis and repair validation remain separate requirements.
