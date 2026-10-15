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

If report reference lookup exceeds its catalog resource budget or cannot safely interpret report metadata, the report stage says unavailable with an unknown count. Other valid stages remain readable. This is different from no saved reports. A source-to-report reverse index is still required for efficient season-scale lookup; the bounded first implementation does not claim that scale qualification is complete.

The display does not claim remote USB durability, physical sample freshness, independent backup or Google Drive upload. Those have separate status and commissioning procedures.

## Handling attention

For unsupported data, preserve the original and confirm the library/profile supported by the importer. For malformed logs, investigate the recording and logging configuration; downloading identical bytes again does not repair their format. For import I/O failures, inspect diagnostics/storage before retrying. Existing transfer Retry actions retry collection and do not retry terminal import jobs.

A maintainer can stop the hub through its normal graceful stop procedure and use the existing explicit importer retry workflow with the correct profile and original file. Automatic retry policy and an operator import-retry action are separate work; this status view performs no writes, deletes, native analysis or source requests.

## Resource and API boundaries

The loopback processing API reads the local catalog through explicitly closed URI read-only SQLite connections with query_only enabled. It does not invoke schema constructors or read raw/derived/video files. Query limits, cursors, JSON projection and reference pages are bounded. Private filenames, filesystem paths, arbitrary exception text and source endpoints are omitted; safe stage/error codes and validated identities are used instead.

The first resource profile limits the current run document to 64 MiB, aggregate report JSON to 32 MiB, report metadata inspection to approximately five million SQLite VM steps, and core request work to approximately fifty million steps. JSON byte/work budgets are not a hard wall-clock deadline or a measured DS load guarantee. Responses are capped at 1 MiB, page sizes at 50 (the browser uses 20), and exact selected report hashes are computed from bounded chunks of stored bytes. No report content is rehashed by a default summary request.

The list route is /api/v1/processing with view=all or view=attention, limit and cursor. A selected `/api/v1/processing/<sha256>` returns its summary and reference-section inventory. Its optional section=transfers, imports, runs or reports selects a bounded page. Original SHA-256 and reference identities are validated. Paging represents local catalog knowledge, not live hardware acquisition.

Implementation evidence, exact test counts and remaining limits are recorded in [current validation](VALIDATION_CURRENT.md). Physical log-flow commissioning remains in the [school checklist](LOG_FLOW_COMMISSIONING.md).
