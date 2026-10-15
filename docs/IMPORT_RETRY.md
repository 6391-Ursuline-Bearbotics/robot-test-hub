# Explicit local import retry

This increment adds a durable operator action for a failed local import. Transfer Retry still retries robot-log collection. Import Retry uses an already archived original and never contacts the robot. Implementation and executed evidence are recorded in [current validation](VALIDATION_CURRENT.md); physical commissioning remains separate.

## Operator contract

Select a recording in Recording processing, inspect Import attempts, and choose Retry local import on the exact eligible attempt. The UI submits a small saved request; accepted/queued is not imported or searchable. Refreshing processing reads progress without starting another retry. Indexer availability is separate from stored evidence.

Only current supported profile/extractor/mapping jobs in failed state with import_io_or_integrity or extractor_failure are eligible initially. The same bytes under the same profile cannot repair invalid framing, unsupported profiles or checksum mismatches. Resource-limit failures require investigation of the configured limit instead of a misleading identical retry. Successful, active and obsolete-version jobs are not reset by this action. Private paths and arbitrary exception strings are not exposed.

A network error after POST is an unknown result, not proof of rejection. An explicit retry of that submission reuses its request ID and original pins. There is no automatic POST replay. Navigation, offline status or late responses cannot retarget the action to another recording or newer job attempt. Stale pins require a read-only refresh and a new explicit decision.

## Request and persistence

The loopback POST route is /api/v1/imports/<job-id>/retry. It retains the existing Host, Origin, X-Hub-Request and JSON guards, with strict bounded JSON and fixed redacted errors. Payload pins request_id, job_id, source_sha256, extractor_version, profile, mapping_revision, expected_attempts and expected_updated_utc_ns. Nanoseconds remain exact decimal strings. The route identity must agree with the payload.

Enqueue uses only SQLite metadata. It does not read/hash/copy originals, extract datasets, install schemas or call a source transport. The service must be accepting work and its indexer available. A SQLite transaction compares the exact job generation; it never looks up a newer attempt after a conflict. Repeating a request ID with different pins fails. Distinct request IDs for the same pinned attempt deduplicate to one durable request.

Catalog schema 8 adds import_retry_requests. Its audit preserves exact target identity, attempt/time pins, request state, fixed error and resulting job outcome. Requests survive restart and backup/restore. Migration installs metadata only, preserving every existing job and report. Current schema and coherent backup validation reject incomplete or altered retry structures rather than repairing them on reads.

## Worker and original preservation

The indexing worker processes at most one queued retry per tick independently of whether its prior import/review signature changed. It claims an exact failed attempt transactionally and records ownership of the resulting running attempt. Original identity, supported profile and attempt ownership remain separate checks. An unrelated job change supersedes the request instead of silently retrying the latest state.

Retry uses only the catalog's canonical content-addressed raw original, confined to the archive. It verifies stored SHA-256 and size before extracting. It does not resnapshot an arbitrary path, alter original bytes, upgrade provenance, remove a checksum claim or create a differently hashed job from corrupt bytes. Derivative publication remains immutable; existing conflicting artifacts are preserved and cause a visible failure.

Request outcome and job outcome are distinct. Successful processing completes the request; an unsuccessful import or request failure remains failed with its recorded result when available. A queue/worker failure cannot claim success. Restart reconciles an owned running attempt without incrementing it blindly; successful job and request completion are published consistently. Per-request failures are isolated so one missing or malformed original cannot block later valid requests. Existing ordinary import and explicit CLI behavior remain supported.

## Acceptance and limitations

Acceptance covers exact-generation conflict, duplicate/lost-response submissions, malformed JSON and local-origin guards, unchanged GET catalog data, no file/source work during HTTP enqueue, manual and collected originals, immutable hash/size/path failures, request/worker crash stages, restart without duplicate attempts, queue isolation, successful import-to-run/report flow, coherent pending-request backup/restore, and browser navigation/offline/stopping behavior.

This is an explicit retry of existing data, not an import-profile editor, an automatic endless retry policy, a repair of corrupt originals, a source download, or a hardware-health pass. A stopped/failed indexer still requires its documented service recovery. Unknown or unsupported producing-library versions require a qualified importer profile. School storage, robot load and real-network behavior are not qualified by local fixtures.
