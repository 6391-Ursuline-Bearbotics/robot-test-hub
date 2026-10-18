# Indexed run search and selection

Catalog schema 9 maintains a lookup for run search and selected-run reads. GET no longer decodes or scans the complete current catalog document to find a page or one run. Original catalog revisions, their document bytes, run identities and original run order remain authoritative and unchanged. This increment changes lookup and publication, not run derivation, clock mapping or physical clock qualification.

## Stored contract

- `run_lookup_state` records the source generation, historical discovery keyset, completion fence and fixed discovery error.
- `run_lookup_jobs` pins a revision to its source row identity and generation, exact source-document SHA-256, byte/run counts, next ordinal and pending/running/partial/ready/failed state.
- `run_lookup_runs` stores each selected run's original ordinal, identity, robot, exact selected-object JSON, SHA-256 and interval count.
- `run_lookup_intervals` stores each mapped UTC piece's expanded integer bounds. Indexes support revision/generation, robot, run identity, original ordinal and time filtering.

Source insert/update/delete triggers discard stale derived rows and queue a new generation. Replacement or deletion/reinsertion under the same revision identity cannot reuse the previous ready lookup. Derived-row mutation guards invalidate ready data. Reads validate the expected tables, indexes and trigger definitions, source-row identity, ready progress and indexed first/last ordinal witnesses. Missing or modified structures are unavailable; reads do not repair them.

These protections detect supported source changes and the tested forms of inconsistent storage. They do not make arbitrary manual SQLite edits an editing API. Preserve an affected archive rather than modifying original evidence to force a ready state.

## Migration and publication

Opening an older supported catalog installs schema 9 transactionally. Installation creates the lookup contract and discovery state without reading or decoding retained `run_catalog_revisions.document` values. Migration failure rolls back; reopening can retry. Older catalog bodies and revision identities are preserved.

Normal run rebuilding publishes the already-derived document, current revision pointer and complete ready lookup in one transaction. Savepoints preserve an outer caller-owned transaction: its rollback removes both source and lookup publication. An existing immutable revision must match the proposed canonical document; a conflicting body is rejected. Readers see a coherent prior or committed new revision, including under WAL concurrency.

New publication indexes the run objects it already holds. It does not impose the legacy 64 MiB whole-document admission limit on GET. A tested new catalog above 64 MiB remains searchable and selectable without reading its document column. This does not remove the per-run, page or query budgets below.

## Historical backfill

The worker tick requires a clean connection with no caller-owned transaction and rejects one before executing SQL. SQLite interruption can abort an entire write transaction despite a savepoint; a worker must not risk unrelated caller edits. Atomic normal publication retains its separate savepoint contract. The owner-held pipeline performs lookup work on every tick, even when imports and review revisions have not changed. Default discovery admits at most 50 historical revision identities. One eligible revision then advances by at most 50 runs and 4 MiB of selected-object bytes; the current revision takes priority over other history. Discovery and per-revision progress are persistent. Historical revisions are retained for pinned investigations, not discarded when current changes.

Legacy normalization admits at most 64 MiB for one stored catalog document. It validates and hashes its exact bytes, rejects malformed or duplicate JSON members and duplicate run identities, and uses SQLite's original selected-object serialization for each stored run. A failed revision cannot supply a successful partial page. A selected job that exhausts its SQL work budget is rolled back and marked failed only if its captured source row and generation still match. Discovery, job work and failure recording are separately bounded; an oversized historical job cannot stop new import/index processing. Schema and other database failures are not converted into a resource failure. Failed jobs remain visible in lookup coverage; this increment adds no operator retry control for terminal failures.

Backfill still reads and parses historical document bodies in the worker, and SQLite may scan those bodies while extracting a batch. The batch and VM limits are resource admission boundaries, not a hard elapsed-time deadline. Oversized or otherwise unsupported legacy data remains preserved and unavailable. Run derivation itself still collects imported cycle aliases in memory; an incremental per-boot derivation pipeline is separate work.

## Read-only API behavior

The existing run list and detail endpoints use SQLite URI read-only mode, `query_only`, explicit connection closure and consistent read snapshots. They install no schema, perform no discovery or repair, contact no source and generate no artifacts. Search reads scalar lookup fields and only the admitted page's selected-run payloads. Selection reads one indexed payload and verifies its stored digest. Neither path falls back to scanning a catalog document.

Search preserves original catalog order and ordinary offset/limit pagination. UTC endpoints are exact signed 64-bit integer nanoseconds. A run matches when any one UTC piece, expanded by its own nonnegative uncertainty, overlaps the query inclusively. Expanded bounds clamp to the supported integer range without floating-point conversion. Overlapping pieces do not duplicate a run; gaps between pieces are not bridged. Unknown-clock runs have no usable indexed UTC pieces and remain available in all-runs search; a time-filtered search includes them only with `include_unknown=true`. A missing, null or malformed clock mapping preserves the exact raw run object and indexes no interval subset, even when an earlier piece was valid. More than 256 declared pieces exceeds the resource contract and makes that revision unavailable instead of becoming an unknown-clock result. Robot filtering still applies.

A complete ready revision supplies exact totals, including a genuine zero. Pending, partial, failed, malformed, missing-contract or budget-exhausted lookup work does not supply an empty-success claim. The lookup API returns unknown totals and no partial items; HTTP list/detail routes return a fixed 503 error. A missing run in a valid ready revision remains 404. One ready revision can be used while unrelated historical revisions are still pending.

| Boundary | Default |
| --- | --- |
| Search page size | 50; accepted range 1–200 |
| Selected run payload | 1 MiB |
| UTC pieces per run | 256 |
| Aggregate selected stored run payloads per search page | 4 MiB, checked before decoding the page |
| Legacy source document admitted for backfill | 64 MiB |
| Discovery / backfill run batch | 50 identities / 50 runs, with a 4 MiB run-byte budget |
| SQLite query/worker work | Approximately five million VM steps |

These limits describe admitted stored payloads and SQL work, not a promised network response latency or a measured school-laptop load.

## Note snapshots and backups

[Run note candidates](RUN_NOTES.md) continue to pin the exact run-catalog revision, selected-object SHA-256 and annotation watermark. Historical backfill preserves the same raw SQLite selected-object bytes used by the previous reader, including its serialization, so lookup does not substitute a newly canonicalized object hash. A newer current revision cannot retarget an existing cursor. Existing schema/VACUUM, witness and boundary checks remain; an unavailable historical lookup cannot fall back to current run evidence. A schema migration can invalidate a cursor's schema fence, requiring explicit refresh.

Selected-run lookup shares the caller's snapshot. Inside note queries it preserves the outer SQLite progress handler, so selecting an indexed run cannot disable the subsequent candidate-query budget. Unknown run clocks still yield unavailable note association, not a zero-note assertion. Note candidate overlap continues to use intended UTC intervals without applying run-clock uncertainty; search's uncertainty expansion does not change that separate meaning.

A coherent backup includes the lookup generation, discovery state, partial progress, ready rows and failures. Restore preserves the snapshot and resumes background work on normal opening/processing. Verification checks the schema-9 lookup contract without rebuilding it. A schema-8 backup remains schema 8 through verification and restore and migrates only when opened by the newer hub; rollback to older software requires the matching pre-upgrade backup rather than changing a version number.

## Executed evidence and remaining qualification

Focused author, integration and independent tests cover exact integers beyond JavaScript's safe range and at signed limits, disjoint/uncertain intervals, null/malformed unknown clocks without partial interval subsets, the 256-piece admission limit, stable order, duplicate/malformed input, source replacement, derived/job metadata corruption, schema failures, partial backfill, caller rollback, WAL snapshots and coherent backup/restore during backfill. Actual list/detail GET tests deny reads of the original catalog document and database writes. Tests also preserve old note revisions, watermarks and selected-object hashes after current changes, and verify that nested selection retains the note query's work budget.

Run the focused suite from the source checkout:

```powershell
python -m unittest discover -s tests -p 'test_run_lookup*.py' -v
```

`tools/bench_run_lookup.py` provides repeatable 10,000/50,000 catalog-only synthetic publication and lookup cases, plus optional dense generated WPILOG profiling. Catalog cases clone an imported public synthetic run template; they measure the publication/lookup path, not derivation of 50,000 raw recordings. Consult [current validation](VALIDATION_CURRENT.md) for recorded executions and complete-suite evidence. No physical robot, private archive, school laptop or field-network load is qualified by these tests.

## Runs awaiting their first report

A known indexed run with no saved report returns a valid empty immutable report-history envelope. Its run details and saved-note pages remain usable while analysis has not produced a report. Unknown runs return 404 and an unavailable current index returns 503. Saved report histories with actual retained reports remain independently reachable without current catalog lookup.
