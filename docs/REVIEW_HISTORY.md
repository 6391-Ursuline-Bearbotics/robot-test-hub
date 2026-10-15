# Saved review history

Open **Evidence review** and use **Saved review history** to inspect retained component assignments, maintenance, approved baselines, finding reviews, regression reference bundles, and automatic analysis plans. This read-only section is independent of the editing forms below it. Inspecting a revision does not populate or submit a form.

## Daily investigation

1. Choose a record type. The identity list shows its latest revision as of the page snapshot, in reverse append order. Use Next page to reach older identities, including those outside the original 100-record dashboard summary.
2. Select an identity and use its revision list to inspect an exact saved version. Both lists show at most 20 entries at a time; moving pages replaces their contents.
3. Use Link to this exact revision to retain the identity and revision in the browser URL hash. Refresh preserves that exact selection. Browser Back and Forward restore previous selections.
4. For finding reviews and regression bundles, inspect Repair and validation chain. Expand the saved fields or follow a validated exact report link. The existing run report view supports [complete trace navigation](REPORT_HISTORY.md).

Previous page returns through up to 64 visited page cursors. A shared cursor link has no previous-page memory on a new browser; First page starts a new current snapshot. Refresh also starts a current snapshot. New appends and corrections do not reshuffle a cursor-pinned sequence. A database schema change or VACUUM invalidates its cursors and asks for refresh. Arbitrary database rewrites/restores are outside the supported append-only contract; refresh after maintenance.

## What the references mean

- **Pinned** identifies an exact retained review revision or immutable analysis job. Report links name a specific run and report; the hub does not select its newest report.
- **Embedded** displays the record content preserved inside a regression bundle. Later corrections do not replace that content. An embedded copy is distinct from a separately retained record with the same identity.
- **Unpinned** means the original record saved only an identity. In particular, older finding reviews contain a maintenance identity without its revision. The view does not substitute current maintenance and imply it was the historical repair.
- **Missing** identifies absent evidence. **Unavailable** identifies malformed, oversized or unsupported evidence that cannot safely be inspected by this view. Neither means a repair succeeded or hardware is healthy.

Review, maintenance and validation remain human decisions. Linking a later run does not establish a mechanical cause or repair effectiveness by itself. No command, tuning application or deployment is triggered here.

## Read-only API and limits

The three loopback GET routes are:

~~~text
/api/v1/review/history?kind=maintenance&limit=20
/api/v1/review/history?kind=maintenance&record_id=repair-example&limit=20
/api/v1/review/record?kind=maintenance&id=repair-example&revision=1
/api/v1/review/chain?kind=finding_review&id=finding-example&revision=1
~~~

History accepts one each of kind, optional record_id, limit (1-50) and cursor. Record/chain require exactly kind, id and revision; chain supports finding_review and regression_bundle. Repeated, unknown and malformed fields fail with a fixed 400 code. Missing exact revisions return 404, changed cursor snapshots 409, and unavailable storage/evidence or stopping service 503. Errors omit SQL, paths and private payload values.

Reads open SQLite in URI read-only mode with query_only enabled, without invoking schema installers. Snapshot cursors bind kind, optional identity, row high-water boundary and schema cookie. Lists project bounded scalar metadata in SQL before Python decoding; original duplicate keys and identity/type mismatches fail visibly. Exact review payloads are capped at 1 MiB and responses at 4 MiB. Job inspection accepts at most 16 modules and 64 findings, removes evidence traces and large finding arrays before Python decoding, and reports omission counts. At most 20 exact report references are displayed; excess is stated. Full trace inspection uses the retained report API separately.

The existing writable review APIs, approval requirements, stored records and schema are unchanged. The recent-record snapshot still serves the editing forms. No additional package is required to run the hub; Node is optional for the browser-behavior development tests.

## Qualification

Sol implementation and independent review cover all six record kinds, more than 100 identities, more than 100 revisions, tied timestamps, appends/corrections during paging, exact pins, embedded/unpinned/missing references, original duplicate JSON keys, strict resource/query limits, read-only database bytes, schema/VACUUM invalidation and redacted errors. Actual loopback HTTP tests exercise real review-store maintenance records, routing and shutdown. Node VM tests execute the actual browser script and cover stale-response races, back/forward, unavailable clearing, safe text rendering and validated exact links.

An isolated browser trial uses 105 invented maintenance identities and 25 revisions of one identity. It reaches revision 1, restores revisions 25 and 1 through Back/Forward, displays HTML-like reviewer text literally, distinguishes unpinned maintenance from preserved embedded revision 1, and shows missing jobs explicitly. It connects to no robot or camera. Physical repair validation, lab operations and season-scale load remain commissioning work; see [current validation evidence](VALIDATION_CURRENT.md).
