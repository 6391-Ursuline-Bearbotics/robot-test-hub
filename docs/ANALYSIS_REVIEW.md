# Analysis eligibility corrections

Current integration uses `automatic-reports-4`, `swerve-tracking` 3 and mapping `6391-alpha7-final-swerve-2`. Revisioned practice plans, automatic cohort execution and bounded plots are documented in [AUTOMATIC_SWERVE.md](AUTOMATIC_SWERVE.md). The sections below retain historical review evidence; REAL acquisition freshness remains unavailable.

The independent local review reproduced four defects and corrected them in `analysis.py`, `swerve.py`, and `tests/test_analysis.py`. This is a Python/synthetic evidence qualification, not a measured robot health result.

| Reproduced defect | Corrected behavior |
| --- | --- |
| A valid imported false connection boolean or disconnected array sample could return no finding because only an optional `connected` attribute was read. | Exact pinned ModuleIO and gyro paths, the explicit fixture/importer `Connected` alias, and sample `connection_sources` are inspected. False records retain field/hash/index provenance and are deduplicated across samples. Missing connection/freshness evidence has explicit unavailable reasons. |
| A stalled wheel with a 1 m/s command and zero measured speed returned `evaluated_no_finding` without configured engineering limits or a usable baseline detector. | Qualified metrics remain available, but the job is `insufficient_data` with `tracking_finding_criteria_not_configured_or_available`. Such a metrics-only job cannot become an approved no-finding baseline. An explicit configured engineering criterion or loaded cohort with a threshold revision is required for a finding decision. |
| Multi-artifact mapping read `source_hash` although importer records use `source_sha256`. | Command, measurement, cycle and connection source hashes use the actual importer field, are retained separately, and must belong to the immutable evidence set. Explicit record hashes are respected even for a singleton evidence set. |
| Finished entries and entry-ID replacement left observations in the adapter's held state. | Start/finish controls remove held values by field and entry ID. Later cycles cannot evaluate those values until a new physical observation restores them. |

Versions are `analysis-framework-2`, `data-quality` 2, `swerve-tracking` 2, and robot mapping `6391-alpha7-final-swerve-2`, preserving old immutable jobs rather than reusing earlier results under changed eligibility rules. Automatic report row selection must retain the declared connection observations and sample status/provenance; its separate pipeline version must advance when changing that selection.

`python -m unittest discover -s tests -p test_analysis.py -v`: **32 tests passed**. Regressions include the genuine Alpha 7 WPILOG false connection, sample-only availability, duplicated source references, missing status checks, a stalled wheel without finding criteria, rejected metrics-only baseline approval, command/measurement provenance across two artifacts, unlisted measurement hashes, and finished/reused command, measurement and connection entries. `test_review.py`: **11 tests passed**. Existing REAL acquisition freshness remains unknown and ineligible; ideal-cycle freshness remains an explicit simulation-only policy.

The initial full suite ran 189 tests and found an unrelated source-status test helper error (a `sequence` argument supplied twice), reported to its owner. After that fix, the concurrently growing full suite ran 200 tests; analysis, review and source-status checks passed, with one unrelated backup WAL-sidecar assertion failure reported to its owner. No automatic robot tuning, deployment, hardware repair, statistical confidence, or physical fault cause is claimed.

The Sol integration agent's final full suite subsequently passed 221 tests with one explicit optional native status-bridge skip; separate native status qualification passed. The earlier source-status, backup and report integration regressions were resolved.

## October 7 independent review

A fresh Sol review added cohort exclusion for repair/configuration boundaries between selected runs, mixed analyzer versions/configuration and overlapping UTC intervals. Comparison now loads and pins maintenance/component assignment history separately from immutable approval evidence, refusing later runs that cross a repair or changed physical assignment even when supplied context strings remain unchanged. Missing history or qualified intervals yields an explicit unavailable comparison.

Automatic report merging retains per-source nonadvancing cycle diagnostics before sorting/deduplicating timestamps, so a stalled cycle cannot disappear as a successful merge. Swerve tracking and automatic report versions advance to 3; prior jobs/reports remain immutable. Focused qualification: 32 analysis, 16 review and seven report tests passed. Browser qualification using a public synthetic recording showed a recording-quality disconnect finding, explicit unavailable swerve readiness, saved a synthetic component assignment and an unresolved finding review. No real fault, repair or healthy baseline was asserted.

Remaining work includes acquisition freshness on REAL hardware, automatic swerve/cohort analysis and plots, revision-history navigation and service worker recovery beyond restart.
