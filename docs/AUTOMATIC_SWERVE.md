# Automatic swerve reports for a practice session

Imported recordings now run the existing swerve tracking and approved-cohort analyzers automatically under an explicit analysis plan. Sol implementation, independent backend review, genuine generated-recording integration and a local browser check qualify this software path. No real robot or camera is operated by this workflow.

## Operator workflow

In **Review & maintenance**, identify each physical module at `front-left`, `front-right`, `back-left` and `back-right`. Assignment intervals must cover the whole run. Close the old assignment with a new revision when a module moves; a location label alone is not a physical component identity.

Save an analysis plan for the robot and the UTC interval of a comparable practice session. Record the software/configuration identities, named test, surface, battery, wheel radius, reviewer and rationale. Plans can cover future imports; corrections create a new revision. End a plan when its test context changes. Conflicting plans do not choose a winner silently.

Enter explicit tracking criteria and a threshold revision when a finding decision is wanted. These are engineering criteria with units, not measured mechanical fault limits. Without finding criteria or a usable selected cohort, tracking metrics cannot become a no-finding baseline. Ideal-cycle freshness is an explicit simulation-only option, off by default; it cannot qualify physical measurements.

As recordings import, the local indexing worker evaluates each supported run. Changes to plans, assignments, maintenance or selected cohorts also cause reevaluation without importing another log. Raw originals, prior analyzer jobs, reports and approved baseline versions remain immutable.

Inspect the run's checks, module coverage, error plots, supporting references and unavailable reasons. Tracking error describes the difference from the final command actually sent to IO. A finding proposes another check; it does not identify a unique physical cause. Overall robot health remains unassessed.

After a standard test and inspection, explicitly select at least three independent qualified no-finding jobs to approve a baseline. Select that baseline in a plan to compare later runs. New runs never join the approved cohort automatically. Battery/configuration/test/source and component-history incompatibilities prevent comparison rather than being averaged away.

## Plan contract

The local review API accepts `POST /api/v1/review/analysis-plans`; its snapshot includes latest `analysis_plan` records. The existing loopback/origin, 16 KiB request-size and shutdown rules apply. Duplicate/nonfinite JSON is rejected, and shutdown is checked again under the service's settings lock before a write. Matching plans require a complete run with one anchored UTC interval, expanded by its declared clock uncertainty.

| Field | Meaning |
| --- | --- |
| `id`, `revision`, `expected_revision` | Opaque plan identity and optimistic revision controls |
| `robot_id`, `start_utc_ns`, `end_utc_ns` | Robot and effective UTC interval; timestamps are decimal nanosecond strings; null end means ongoing |
| `reviewer`, `rationale` | Human context and reason for selecting this policy |
| `build_hash`, `config_hash` | Explicit software/configuration identities; lowercase 64-character SHA-256 values |
| `test_id`, `surface`, `battery_id` | Comparable test and environment context |
| `wheel_radius_m` | Positive finite declared radius in meters |
| `swerve_configuration` | Existing analyzer parameters with exact units and explicit threshold revision |
| `approved_baseline_id` | Explicit approved cohort identity, or null; never an automatically chosen recent cohort |
| `ideal_simulation_cycle_policy` | Explicit ideal simulation assumption, default false |

Recorded configuration must agree with the selected context throughout the run. A recorded build hash must also agree; a missing build hash remains visibly human-declared and unverified. Human-declared information remains distinguishable from values actually observed in a log. Unknown fields, invalid values, ambiguous scope and incomplete coverage fail explicitly.

## Evidence and limits

The source-reviewed mapping uses recorded `SwerveModuleVelocity` and `Rotation2d` schemas, the final optimized/cosine-scaled/desaturated commands, measured states and all three connection fields per module. Schema and entry lifecycle controls and necessary pre-run held observations are part of the input evidence. An unchanged AdvantageKit value retains its original record reference.

The signal-mapping revision is distinct from the run catalog and clock-mapping revision. A new import changes the catalog, not the meaning of an unchanged qualified signal schema. Plan, assignment, history, cohort and source/dataset revisions are pinned so a later edit produces another result instead of reusing a stale report.

Actual hardware acquisition freshness is currently unavailable in the robot's recorded swerve arrays. Logging-cycle timestamps cannot establish when hardware measured those values. The additional [Phoenix SDK status/timing diagnostics](SWERVE_STATUS_TIMING.md) preserve receipt/transmit observations and warn on recorded SDK errors; these do not qualify acquisition freshness. REAL recordings therefore remain ineligible for fresh tracking metrics until the robot integration records and qualifies acquisition times. Synthetic imports cannot gain ideal freshness simply by carrying a `SIM` label. A supported explicitly recorded SIM mode with the simulation policy is qualified only under that assumption; it is not physical evidence.

Automatic input selection admits at most 50,000 retained rows and scans at most five times that limit per run. Unsupported source types, source overlap, missing endpoints, changed runtime/configuration and other mapping hazards prevent a partial tracking pass. Every module must reach the exact run endpoints and qualify its eligible windows; an explicitly selected cohort must yield all eight drive/steering comparisons.

The initial run API projection displays the latest five report versions with truthful `reports_display` counts and the first 500 eligible intervals per module. [Report history and trace navigation](REPORT_HISTORY.md) provides explicit older-result selection and further bounded interval windows from the same immutable result. Review snapshots omit trace bodies and bound their latest records before loading them. Browser plots compute duration-weighted RMS columns only from the selected displayed intervals, label omissions and retain gaps; signed errors and original source indexes remain available. Full-run findings can refer beyond an initial projection, so later intervals must be requested explicitly. Human-review record history, voltage/effort/thermal comparisons, impacts/slip diagnosis and physical threshold calibration remain separate work.

## Qualification

Six author regressions and nine independent importer/HTTP/pipeline regressions pass. Two native integration tests use the locked installed Alpha 7/AdvantageKit writer and a separate official reader. Their invented four-run session has front-left velocity errors of 0, 0.02, 0.04 and 0.35 m/s with the other modules at zero error. The first three constitute an explicitly selected test cohort, not an automatically learned or physically healthy baseline. The fourth exceeds its provisional 0.14 m/s cohort comparison limit. A separate boot/import retains comparison eligibility; a declared repair between sessions prevents reuse of the old cohort. Restart preserves exact prior report bytes. A recording labeled REAL and a SYNTHETIC import carrying a SIM label both refuse to invent acquisition freshness.

An isolated browser checks actual imported native-generated evidence: four modules, 19 eligible intervals each, eight cohort comparisons, explicit simulation assumptions, source references, revisioned context and offline clearing. Editing a loaded plan preserves normalized parameters and its selected cohort and causes a new worker report without a new import. Focused JavaScript checks cover exact nine-digit UTC conversion, normalized revision policy, stale responses, unequal-duration RMS, gaps and projection bounds.

Generated recordings stay outside the public source tree; only generator/test code is published. Existing public fixtures still pass the locked official-reader oracle after adding swerve-array decoding. The native-enabled Windows/Python 3.10.7 suite ran **465 tests in 90.730 seconds: 463 passed, two symbolic-link privilege skips**. Final browser evidence and published checks belong in [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md).
