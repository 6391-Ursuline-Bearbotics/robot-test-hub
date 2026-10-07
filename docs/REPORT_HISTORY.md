# Report history and complete trace navigation

Automatic reports retain immutable results when a plan, cohort or mapping changes. Investigation must keep the selected result fixed while new results arrive. This increment adds navigation to retained report versions and their complete stored module traces; it does not add physical sample freshness or change analyzer eligibility.

## Operator workflow

Inspect a run, then select a retained report from its history. The selected report has its own source, configuration, plan/cohort revisions and outcome. An older result describes the evidence and policy at that revision; it is not a claim about current robot health.

Each module initially displays at most 500 eligible intervals. Trace navigation requests another bounded window from the same report and module. Plots use only that window and retain gaps; full-run metrics remain separate. Source-reference tables retain exact timestamps, signed errors and original record indexes. No trace pages are concatenated into an unbounded browser collection.

Connection loss clears current evidence and invalidates pending requests. Selecting another run or report prevents responses from the earlier selection from changing the new view. A later automatic result never silently replaces the selected historical report.

## Read-only API

| Route | Contract |
| --- | --- |
| `GET /api/v1/runs/{run_id}/reports?limit=20&cursor=…` | Metadata-only retained versions, at most 50 per page, with a stable snapshot, next/previous cursors and explicit counts |
| `GET /api/v1/runs/{run_id}/reports/{report_id}` | Selected immutable result projected to at most 500 trace intervals per module, plus a hash of the complete stored result bytes |
| `GET /api/v1/runs/{run_id}/reports/{report_id}/traces?analyzer_id=swerve-tracking&module_id=…&offset=0&limit=500` | One module's eligible intervals, at most 500, exact total/offset/returned/next-offset and pinned result hash |

History cursors retain their run scope and insertion high-water boundary. New reports, including those with equal creation timestamps, are excluded from an already selected snapshot. A fresh history request starts a new snapshot. Trace requests bind the run, report, analyzer and physical module identity; a report from another run cannot supply evidence for the selection.

The result hash identifies the exact canonical JSON retained in the local archive. It is distinct from original WPILOG hashes and does not establish hardware accuracy. SQL extracts bounded metadata and trace pages before Python decodes them. Hashing reads bounded chunks of the stored result. This bounds application memory, while SQLite query/hash work still depends on result size. Original recordings and complete stored results are never rewritten by these reads.

Supported stored results are at most 64 MiB; hash chunks are 64 KiB. Detail metadata is limited to 64 KiB at the top level and 1 MiB per check, with at most 16 checks. A check supports at most 16 modules and 64 findings, with at most 50,000 intervals per module. Trace/finding elements are limited to 16 KiB before application decoding. Selected-report/history/trace responses are at most 4 MiB; the compatible initial run-detail route can contain five separately bounded report projections, up to 20 MiB plus run/note metadata. Unsupported or oversized results are unavailable explicitly. These limits do not establish season-scale query performance.

## Qualification and remaining work

Qualification must exercise more than five report versions and more than 500 intervals, mid-page result insertion, cross-scope requests, malformed cursors, old-result repeatability, shutdown/offline clearing and stale browser responses. An invented long recording generated with the locked native AdvantageKit writer has 648 eligible intervals; front-left error changes from 0.02 to 0.8 m/s after the initial page. The independent official reader supplies the source oracle. Generated evidence cannot qualify real robot health.

Seven author tests, four independent HTTP tests and the three native swerve integrations pass. The complete native-enabled suite ran 477 tests in 118.062 seconds: 475 passed, two Windows symbolic-link privilege skips. Actual browser qualification reaches an older result across history pages and its late interval window, verifies source records and clears evidence after hub loss. Focused JavaScript checks cover stale selection races and bounded navigation. Public synthetic fixtures still pass the locked official-reader oracle. No robot or camera is contacted.

Human-review record history, a shared telemetry/video seek timeline, REAL acquisition freshness, physical thresholds and season-scale storage/load measurements remain separate work. Current test and browser evidence belongs in [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md).
