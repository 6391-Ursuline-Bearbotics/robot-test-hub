# Recording processing status

On Transfers, Recording processing separates local evidence stages from the closed-file download queue. A queue that is caught up describes collection; it does not say every recording has usable decoded data, searchable runs, reports or a cloud copy.

## Operator workflow

The panel lists original content identities, with separate columns for original verification, import, current run indexing and saved reports. The total and Import needs attention view cover the catalog, including recordings outside the current 20-entry page. Original transfers, import attempts, searchable runs and saved reports each have their own bounded reference pages for a selected recording.

Choose a recording, then choose a reference section. Inspect versions and hashes under details when needed. Run/report links refer to catalog evidence rather than starting an analysis or changing the robot. Browser Back/Forward and a selected recording URL retain the identity. Refresh explicitly to reload selected evidence. First page starts at the current membership; Previous remembers visited pages locally. Newly added originals can sort before an existing cursor and appear after refresh. This is live SHA keyset membership, not an immutable practice-session snapshot.

The indexer status is separate and prominent. If indexing stops or fails, previously saved catalog evidence remains inspectable; it does not mean new arrivals are being processed. If the hub is unavailable or stopping, the panel clears processing claims and disables stale page controls. Losing the robot connection does not invalidate already saved local evidence.

## Read each stage separately

- **Original:** a known expected hash alone is not a verified download. Verification comes from the matching completed transfer/digest receipt or a separately registered imported artifact, and is recorded catalog knowledge rather than a new file rehash on every page load. Multiple transfers of the same bytes share a content identity while retaining their own receipts. A failed expected-checksum claim stays unverified even if the observed snapshot has its own content hash.
- **Import:** states belong to exact profile, extractor and mapping versions. Unsupported profiles, malformed recordings, failed jobs and partially supported fields remain visible. Opaque demo transfer bytes are not WPILOGs and are not a parser failure or a robot health result.
- **Searchable runs:** successful import and inclusion in the current run catalog are different facts. An imported disabled-only recording can have no enabled runs. A catalog can still reference an older successful job while a different current import version is pending or unavailable.
- **Reports:** a saved report is evidence of an analysis result, including unavailable/unsupported checks. Its presence does not imply a healthy robot. Links identify retained reports associated with this original; their source hashes and versions remain authoritative.

Report references use a [maintained catalog lookup](REPORT_LOOKUP.md). While legacy discovery or report normalization remains pending or failed, the full count is unknown. Known exact references remain navigable as a partial result. Unavailable with no known references differs from no saved reports. The background indexer performs maintenance; refreshing the page never starts it.

The display does not claim remote USB durability, physical sample freshness, independent backup or Google Drive upload. Those have separate status and commissioning procedures.

## Handling attention

For unsupported data, preserve the original and confirm the library/profile supported by the importer. For malformed logs, investigate the recording and logging configuration; downloading identical bytes again does not repair their format. For import I/O failures, inspect diagnostics/storage before retrying. Existing transfer Retry actions retry collection and do not retry terminal import jobs.

For a supported current import that failed with a local I/O/integrity or extractor error, inspect Import attempts and choose **Retry local import**. The saved request is tied to that exact job attempt. Queued and running receipts remain separate from import success; Refresh reads the latest receipt and outcome. A lost response can be checked by explicitly retrying the same saved request. See the [retry contract and recovery limits](IMPORT_RETRY.md).

Invalid, unsupported, checksum-mismatched, resource-limited, active, successful and obsolete-version jobs do not offer this action. Preserve the original and investigate their specific cause. For advanced profile/manual workflows, stop the hub through its normal graceful procedure before using the existing importer CLI. There is no automatic terminal retry loop. Processing GETs perform no writes, native analysis or source requests; the explicit POST queues metadata and the indexer performs local work.

## Resource and API boundaries

The loopback processing API reads the local catalog through explicitly closed URI read-only SQLite connections with query_only enabled. It does not invoke schema constructors or read raw/derived/video files. Query limits, cursors, JSON projection and reference pages are bounded. Private filenames, filesystem paths, arbitrary exception text and source endpoints are omitted; safe stage/error codes and validated identities are used instead.

The resource profile limits the current run document to 64 MiB and core request work to approximately fifty million SQLite VM steps. Report bodies are not read or hashed by processing GETs; maintained metadata has a separate work budget. Background report normalization has individual byte/work limits, with no aggregate report-body cap. Budgets are not hard wall-clock deadlines or measured DS load guarantees. Responses are capped at 1 MiB and page sizes at 50 (the browser uses 20).

The list route is /api/v1/processing with view=all or view=attention, limit and cursor. A selected `/api/v1/processing/<sha256>` returns its summary and reference-section inventory. Its optional section=transfers, imports, runs or reports selects a bounded page. Original SHA-256 and reference identities are validated. Paging represents local catalog knowledge, not live hardware acquisition.

Implementation evidence, exact test counts and remaining limits are recorded in [current validation](VALIDATION_CURRENT.md). Physical log-flow commissioning remains in the [school checklist](LOG_FLOW_COMMISSIONING.md).
