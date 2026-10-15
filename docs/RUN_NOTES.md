# Run-associated saved note candidates

Implemented locally October 15, 2026, extending R05/R06 and T06/T07. Before this increment, run detail took only the first 50 notes per clock interval and the browser silently truncated candidates at 100. The notebook stores the remaining notes; this work makes them reachable from the relevant run.

## Candidate meaning

A candidate's intended event UTC interval overlaps at least one of the selected run's mapped UTC pieces. Gaps between pieces are not bridged. One event appears once even when it overlaps multiple pieces. Note/clock uncertainty stays visible and separate from this intended-interval query; association is contextual and never a robot receipt or proof of causality. Unknown note times cannot be assigned to a run. A run without usable UTC mapping has unavailable candidate coverage, not a claim that no notes occurred.

## API and snapshot contract

GET /api/v1/runs/<run-id>/annotations accepts limit (default 20, maximum 100) and an optional bounded opaque cursor. It returns schema 1, exact run identity, immutable run_catalog_revision, decimal annotation_watermark, availability, exact saved annotations, total/returned/limit/next_cursor and candidate_basis. Unknown fields, malformed cursors and changed run/limit pins fail explicitly. Missing/unavailable snapshots cannot silently fall back to current evidence.

The first request pins an immutable catalog revision and the current maximum append-only annotation row ID. Select the latest revision of each event at or before that watermark, then apply the mapped intervals. Keyset pages sort by event ID and preserve that selection across later note edits/additions. Refresh deliberately starts a new snapshot. Full counts and page rows share one read snapshot. Saved event/submission/hub-receipt times and revision numbers remain unchanged.

Run detail retains a bounded first annotation_candidates page for compatibility and adds explicit annotations_display metadata. It no longer loops over only the first Notebook.list page of each interval. Read endpoints use SQLite URI read-only mode and query_only, explicit connection closure and no schema constructors, source requests, analysis or artifact generation. Query work is budgeted; exhaustion returns unavailable rather than a successful partial count or page. Limits are a 64 MiB catalog document, 1 MiB selected run, 256 clock pieces, 16 KiB note payload, 2 MiB response and five million SQLite VM steps. These are resource boundaries, not a real-time latency guarantee. A separate bounded selected-run lookup validates the run before candidate lookup. Candidate-only 503 failures leave valid run/report evidence available with explicit unavailable note metadata and unknown counts; corrupt or missing selected-run evidence still fails. The direct annotations endpoint retains its 503. No catalog migration is required.

## Browser behavior

The selected run has independently bounded note pages, visible counts/snapshot scope and controls for previous/next, first-page and refresh. Exact saved revisions and uncertainty remain visible. Candidate request sequencing prevents late responses after run changes, refresh or connection loss from repopulating a different investigation. Paging replaces the old list instead of growing the DOM. Failure/unavailable states clear unsupported claims while preserving unrelated valid run/report evidence when appropriate. Data uses text-safe DOM APIs and constructed local URLs.

## Acceptance

Verify more than 100 notes across multiple clock pieces, deduplicated membership, exact saved revisions/hub times, no gap bridging/unknown-time association, watermark stability through later edits and new events, full bounded traversal and deliberate refresh. Adversarial tests reject wrong-run/limit/cursor/snapshot pins and enforce read-only behavior, no catalog creation and no source/schema calls. Test work-budget exhaustion without partial success. Execute actual UI logic for counts, pagination, snapshot changes, late responses, offline behavior and hostile note text. Parent browser checks and full-suite/published-CI evidence remain separate from physical clock/delivery qualification.

## Executed local evidence

Ten author, seven independent and ten actual-JavaScript UI tests pass. Coverage includes more than 100 saved notes, duplicate-free overlap across clock pieces, latest-revision selection before interval matching, exact intended/hub timestamps, additions/edits after the fence, retained historical run revisions, strict cursor pins, unavailable clocks, byte/query budgets, no catalog creation, forbidden Notebook constructors/schema/source calls, and actual loopback error responses. An injected candidate-query budget failure returns valid run/report detail while the note endpoint remains 503; corrupt selected-run evidence does not get this fallback. The UI tests cover bounded replacement, cached previous/newest pages, explicit refresh, malformed/short/self-loop pages, stale requests after run/search changes, independent report history, hostile text and offline behavior.

Parent browser qualification used an actual isolated hub, one public synthetic WPILOG and 120 invented notes. Six pages reached all notes with exactly 20 cards per page, including notes beyond the old 50/100 cutoffs. A hostile markup label appeared literally. After an existing note was edited to revision 2 and a later event added, Returning to the cached first page still showed the original 120-note snapshot/revision 1; explicit Refresh showed 121 notes and revision 2. Switching to another run showed zero candidates without retaining the previous run's notes. After graceful host shutdown, Refresh cleared note cards and displayed unavailable guidance while keeping the saved historical report view. Service cleanup returned true; no test host or archive owner remained.

No real robot, camera, private archive or cloud was used. Complete-suite and exact published CI evidence are recorded in VALIDATION_CURRENT.md. This establishes saved candidate navigation, not physical clock accuracy, robot receipt or repair causality.

## Operational limits

Snapshots assume the supported append-only Notebook editing API. Cursors detect schema/VACUUM changes, removed witness/boundary rows and changed run documents; they do not make arbitrary direct database rewrites a supported workflow. Do not modify SQLite rows manually to edit notes. Snapshot pages may continue against a retained immutable run revision after newer imports arrive. First note page means the first event-ID page of that saved snapshot, not chronological newest; Refresh is the action that includes later events or revisions. No pinned-revision notebook deep link is invented: each candidate displays its exact saved receipt inline, and the notebook remains a separate view.
