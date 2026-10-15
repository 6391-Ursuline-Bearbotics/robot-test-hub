# Maintained report provenance lookup

The laptop maintains a SHA-to-report lookup introduced in catalog schema 7 and retained by later schemas. Processing pages read its scalar metadata instead of reparsing every saved report. There is no aggregate 32 MiB report-body limit in this path. Individual report normalization remains bounded; season-scale latency and physical DS load still require measurement.

## Persistent contract

- `report_lookup_state`: singleton generation counter, legacy discovery keyset and completion fence, fixed discovery error. The counter survives report deletion and reinsertion.
- `report_lookup_jobs`: report identity, generation, pending/running/ready/failed state, exact stored-body SHA-256, run identity and fixed error code.
- `report_source_refs`: unique report/source-SHA membership with the generation that established it. The source-first index supports reverse lookup.
- Insert/update/delete triggers invalidate old references and assign a new generation. They also protect direct SQL writes and replacement with foreign-key enforcement disabled. A ready row contributes only through a matching generation and an existing report.

Original analysis report bodies remain authoritative and unchanged. Lookup metadata is derived evidence, not a robot-health verdict or a second copy of the high-rate trace data. No file path, endpoint, raw diagnostic or report payload is exported by the processing projection.

## Migration and writers

Opening an older supported catalog installs the lookup through the transactional migration chain. Migration installs tables, indexes and invalidation triggers but does not parse legacy reports. Existing bodies remain available through their normal exact report routes. Startup validates the expected lookup definitions, including trigger bodies, rather than silently repairing missing or inert structures. Backup integrity validation applies the same rules.

New framework reports normalize the actual stored row in the report insertion transaction. INSERT OR IGNORE must never index a newly reconstructed body when another immutable body already owns that identity. Savepoints preserve caller-owned transactions: rollback removes both the report and its lookup changes. Standalone analysis databases without hub lookup infrastructure retain their original behavior.

## Resumable background work

Each pipeline tick discovers at most a bounded batch of legacy report identities (default 50) and normalizes one pending/running report, independently of whether imports or review revisions changed. Discovery progress and jobs are persistent. Process loss resumes discovery and unfinished jobs; terminal malformed jobs do not monopolize the worker.

Normalization uses one consistent SQLite snapshot. It checks the individual byte limit (64 MiB by default), hashes exact stored bytes in 64 KiB chunks, validates bounded run/report/source metadata and duplicate membership-defining keys in SQLite, and avoids decoding trace documents into Python. The default per-report SQL work limit is approximately five million VM steps. Supported identifiers and source hashes must be safe scalars. Missing run scope or invalid/unsupported metadata produces a fixed failed job rather than a false empty result or rejection of the original saved report.

After releasing a worker-owned read snapshot, publication compares the persistent generation inside a write transaction. Replacement or deletion during normalization invalidates that result. Matching references, digest and ready state publish atomically. These budgets bound work, not a hard wall-clock deadline or hardware load guarantee.

## Read-only processing and incomplete coverage

GET uses URI read-only SQLite, query_only and a bounded snapshot. It performs no schema installation, discovery, repair, report-body parsing, hashing, native analysis or source I/O. Selected report links use the previously computed exact stored-body digest.

Global coverage is complete only after discovery finishes and all existing reports have ready jobs. Pending/running, failed or undiscovered reports make a recording's full report count unknown because their source membership has not been established. Valid known references remain reachable and paginated during this time:

- Summary: `count:null`, `known_count:N`; partial when known references exist, otherwise unavailable.
- Reports page: `total:null`, `known_total:N`, partial availability and ordinary bounded items/cursors when known references exist.
- Complete coverage: exact count/total and available references. Only then can zero establish no indexed reports.

Transfer verification, imports and searchable runs remain separate. An interrupted optional lookup read can expose unavailable report references while preserving those stages. An invalid catalog/schema fails visibly. The UI explains incomplete lookup and retains exact known links rather than showing unknown as zero.

A failed job stays visible in coverage. There is no automatic terminal retry control in this increment; investigate its fixed error and preserve the original. Replacing the report row assigns a new generation, but changing original evidence to clear a warning is not a repair procedure.

## Backup and acceptance

Lookup state lives entirely in the catalog. A coherent backup includes generations, discovery progress, ready references and pending/failed jobs. Restore preserves them and resumes background processing; it does not reindex during validation or copy additional artifacts. Older supported backups remain restorable and migrate when normally opened.

Acceptance covers transactional migration/rollback, malformed definitions, pending/running restart, direct SQL replace/rename/delete with foreign keys disabled, stale-generation publication, exact stored-byte hashing, caller rollback, malformed-job isolation, pending and failed partial navigation, report archives exceeding the former aggregate limit, GET without body reads, and coherent backup/restore during backfill. Current executed evidence is recorded in [validation](VALIDATION_CURRENT.md).
