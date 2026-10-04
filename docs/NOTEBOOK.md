# Hub notebook · T07 implementation

The local loopback hub serves a separate notebook at `/notebook`. It stores historical observations in the same local SQLite catalog as the collector. Notes do not depend on a robot connection. There is no robot marker delivery, run association, camera clip request, or offline device outbox. Every response labels its storage state `saved_in_hub` and delivery state `historical_hub_only`.

## Operator flow

Press **Mark event**, **10 seconds ago**, or **30 seconds ago** with no typing required. Optional text, observer, subsystem, and time uncertainty are captured at the button action. Add details later with **Edit details**; saving appends a revision and preserves the original event time. Revision history retains earlier text and time metadata.

Historical exact times require an ISO date/time with an explicit UTC offset or `Z`. An interval can span the two occurrences of a daylight-saving overlap by using their distinct offsets. Invalid calendar dates and reversed intervals are rejected. **Mark with unknown event time** saves null event endpoints, unknown clock quality, and null uncertainty; these notes remain searchable separately.

The browser clock is unverified. Its estimated uncertainty is supplied by the operator, not a measured synchronization guarantee. The page gives each browser session its own clock-domain ID and records `performance.now()` as client monotonic nanoseconds. UTC and monotonic ns are decimal strings over HTTP and signed integers in storage. Original relative-time input and client action/submission time are retained independently of hub receipt time. Network delay never changes the intended event interval.

If a request is not confirmed saved, the page retains the exact serialized request in the current tab for retry. Closing/reloading loses unconfirmed requests; this is explicitly disclosed. Durable offline browser storage belongs to T08. A retry after a lost successful response returns the already saved event/revision and its original receipt time.

## API

All routes use the existing loopback Host/Origin checks; mutations require JSON and `X-Hub-Request: 1`. Annotation bodies are limited to 16 KiB; text to 4 KiB of UTF-8; tags to 20 labels of 64 UTF-8 bytes each. Arbitrary text is rendered with DOM `textContent`, including revision history.

| Route | Behavior |
| --- | --- |
| `POST /api/v1/annotations` | Create revision 1; `201` for insertion, `200` for an identical retry |
| `POST /api/v1/annotations/{id}/revisions` | Append an annotation envelope with `expected_previous_revision`; stale/conflicting content returns `409` |
| `GET /api/v1/annotations` | Latest revision of each event, ordered by event ID; `limit` 1–100, default 50; `cursor` continues after an event ID |
| `GET /api/v1/annotations?from=...&to=...` | Inclusive overlap of intended UTC intervals, using decimal ns strings; optional `include_unknown=true/false` |
| `GET /api/v1/annotations/{id}` | Latest annotation plus ascending revision history; `limit` 1–100, `after_revision` for subsequent pages |

Interval filtering uses intended endpoints and reports uncertainty separately. It does not claim that unverified browser clocks match robot or video time. Unknown-time notes are included by default and can be excluded. Pagination is bounded and ordered by event identity; it is not a frozen snapshot while other users edit notes.

Create body example (values are synthetic):

```json
{
  "schema_version": 1,
  "event_id": "synthetic-event",
  "revision": 1,
  "submitted_utc_ns": "1791071520000000000",
  "client_monotonic_ns": "50000000000",
  "event_utc_start_ns": "1791071490000000000",
  "event_utc_end_ns": "1791071490000000000",
  "when": {"kind": "seconds_ago", "seconds": 30},
  "clock_domain": "browser-session-example",
  "clock_quality": "unverified_client",
  "uncertainty_ms": 1000,
  "text": "Steering felt wrong",
  "tags": ["drive"],
  "author": "example-observer",
  "device_id": "example-device",
  "source": "practice-notebook",
  "run_id": null
}
```

A revision POST wraps the full annotation as `{"annotation": {...}, "expected_previous_revision": 1}` and sets its `revision` to 2. Identical retries are accepted even if a later revision already exists. A reused event/revision with different normalized content is a conflict. The original client action timestamp stays in the annotation; each revision receives a separate immutable `hub_received_utc_ns`. Time corrections can be submitted as a new exact-interval revision; earlier time metadata remains in history. The page's details editor preserves existing time metadata.

`author` and `device_id` default to `unspecified` if omitted. Current clock qualities are `unverified_client`, `user_estimate`, and `unknown`. They are declarations, not authenticated clock certification. `run_id` must remain null until run linking is implemented. Unknown/unsupported fields are rejected, including fabricated robot acknowledgments. Stable errors include `invalid_annotation`, `invalid_query`, `revision_conflict`, `annotation_not_found`, and `notebook_storage_failed`.

## Persistence and validation

`notebook.install_schema(db)` creates `annotation_revisions` and its interval index inside the caller's migration transaction. It does not change `user_version` or commit the caller's connection. Notebook startup also installs the tables idempotently in a short transaction. Each request opens and closes its own SQLite connection, with full synchronous writes. The composite event/revision primary key and an immediate write transaction serialize competing edits; the HTTP save acknowledgment is emitted after commit. No raw logs are modified.

Local validation: `python -m unittest discover -s tests -p test_notebook.py -v` on Windows/Python 3.10.7: **13 tests passed**. Covers delayed action vs receipt, unchanged duplicate receipt, conflicting retries/edits, old-revision retries, bounded history/search, unknown time and blank markers, restarts, concurrent creation, transactional schema rollback, exact offset/calendar validation, UTF-8 limits, unsafe text preservation, loopback mutation protections, request-size bounds, and shutdown rejection. Browser behavior must be checked separately; no hardware claims are made.
