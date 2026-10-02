# Ingestion, health analysis, and improvement loop

## Import pipeline

Input is a verified immutable recording plus format profile and raw metadata. Prefer a version-compatible WPILib reader; if Python bindings do not support the exact 2027 alpha format/structs, use a small pinned Java extractor with the installed Alpha 7 libraries. Keep language bindings behind one extractor contract. Do not silently feed nanoseconds into a 2026 microsecond parser.

Extract source field path, type/schema, units, timestamps, values, validity/freshness, and source artifact/record provenance. Decode geometry/struct arrays through recorded schemas. Preserve high-rate odometry arrays with their sample timestamps; do not treat a whole array as a single 20 ms measurement. Handle record start/finish and field-ID reuse according to the actual format.

Separate original inputs, RealOutputs, and ReplayOutputs. Select a canonical alias map per robot schema/version; do not heuristically combine similarly named fields. Missing samples, default-filled inputs, disconnected devices, gaps, and invalid timestamps remain distinguishable from true zeros. Reject or explicitly mark unsupported field types.

Importers produce immutable derived datasets keyed by source hash, importer version, schema profile, and mapping revision. Run intervals and summary metrics are indexed in SQLite. Signal bulk storage can use Parquet. Import jobs are idempotent and retryable; unknown schema does not block other supported recordings.

## Analyzer contract

An analyzer declares ID/version, supported schema profiles, required/optional signals and units, eligible operating windows, minimum coverage, grouping keys, baseline requirements, and parameters. Input includes immutable signal references, run/config context, and approved baseline version. Output is metrics, coverage, findings, and unavailable reasons.

Every finding includes interval, severity, observed behavior, supporting metrics/graphs, comparison cohort, baseline version, sample count/eligible seconds, and suggested next check. Keep observations separate from hypotheses. Confidence describes evidence quality, not a fabricated probability of mechanical failure. Never report `healthy` when an analyzer had insufficient data.

Outcome values: `evaluated_no_finding`, `finding`, `insufficient_data`, `unsupported`, `failed`. Overall report shows evaluated coverage and unavailable checks; it is not an unqualified green score.

## First analyzer set

| Analyzer | Windows / signals | Metrics / findings |
| --- | --- | --- |
| Data quality | Entire run, timestamp/connection/freshness | Gaps, invalid clock, missing keys, stale sensors, logger faults |
| Swerve drive tracking | Enabled valid data; optimized command and measured velocity | Time-weighted RMSE, p95 absolute error, sustained lag; normalize by requested motion |
| Swerve steering | Meaningful steering demand/speed, wrapped commanded/measured angles | Wrapped error, settling time, oscillation, encoder discontinuity |
| Swerve effort/thermal | Matched speed/acceleration/voltage/workload windows | Stator/supply effort, temperature rise, per-module historical deviation |
| Electrical | Valid voltage/current plus battery/load context | Brownout events, sag under comparable load, disconnect/reset clusters |
| Autonomous | Phase/path ID, command/estimate and timing | Timeouts, phase duration, path tracking error and endpoint error |
| Shooter | Spin-up, steady-ready, feed windows | Spin-up time, sustained speed error, recovery after feed, readiness inhibition |
| Intake/indexer | Goal/command/current/velocity and recovery events | Jam rate per operating time, recovery duration/success, repeated retries |
| Vision | Observations, rejection reasons/uncertainties | Accepted/rejected fraction by cause, observation age, correction size |

Begin with data quality and swerve tracking; other analyzers are independent later tasks. Hardware faults with explicit status flags can be reported before a statistical baseline exists. Baseline-dependent comparisons remain unavailable until a meaningful cohort exists.

## Swerve details

Use commands after optimization/cosine scaling/desaturation actually sent to IO. Confirm sign, wheel radius, units, and physical module location. Steering error is wrapped to the shortest angular difference. Near zero drive speed, steering-angle comparison can be uninformative; exclude or classify separately. Align signal time domains and declared device latency before calling delay a control problem.

Compare each module against its own approved history, peers during similar motion, and commanded response. Rotation legitimately demands different module velocities; raw whole-session current ranking is not a health detector. Separate steady translation, acceleration, rotation, impacts/pushing, and idle. Baseline grouping includes hardware/configuration, test/surface, battery/load, and temperature where available.

Use time-weighted metrics for irregular samples. Require minimum eligible duration and independent runs before reporting trend regressions. Start with absolute engineering limits and robust median/MAD or percentile comparisons. Specify thresholds as analyzer configuration with units, provenance, and revision. Do not present arbitrary initial thresholds as validated mechanical limits.

Kinematic residuals can compare a held-out module with motion estimated from the other modules and gyro. They are indicators, not independent ground truth: slipping wheels and shared estimator errors can affect multiple channels. A pose estimate incorporating the same encoders cannot prove those encoders are accurate. Independent vision/video can corroborate where its own quality is adequate.

Do not divide current by velocity near zero. Supply and stator currents have different interpretations; retain both types explicitly. Temperature depends on prior workload and starting temperature. A high-current observation may reflect pushing, a brake, tread changes, or a mechanical fault; link the video/driver note and propose a repeatable test.

## Baselines and longitudinal learning

Approved healthy baselines are immutable versioned cohorts. A reviewer selects known-good runs after a standard test and inspection. Store cohort IDs, exclusion reasons, sample counts, metric distributions, and context limits. Never automatically absorb all recent runs into the baseline; that normalizes gradual degradation.

Track physical component IDs through swaps, independently from front-left/front-right labels. After a module swap, compare by component and by position; after wheel replacement/config changes, start or explicitly bridge a new cohort. Battery comparisons similarly require battery identity and comparable load. Missing identity reduces the strength of the conclusion.

New runs can extend an exploratory trend, but changing the approved baseline is a reviewed action. Measure false alarms and missed known issues. Deduplicate related anomalies into one incident instead of issuing dozens of alerts every loop.

## Example report structure

1. Coverage: duration, enabled time, valid clocks, required-signal availability.
2. Configuration: build/config/battery/component changes relative to baseline.
3. Findings ranked by significance, each with evidence interval and action.
4. Metric comparisons with eligible duration/cohort size.
5. Notes and aligned clips.
6. Unavailable checks and why.

Example wording (invented): `Front-left tracking error increased during three comparable straight-drive windows. Voltage was within the comparison range. Check tread and rolling resistance, then repeat test D1.` Never write `bearing failed` without confirming evidence.

## Human feedback and regression

Review disposition: confirmed hardware, confirmed software, expected behavior, false alarm, insufficient evidence, unresolved. Link a repair/commit/config change and a validation run. Store the reviewer and rationale. Use those labels to improve detector windows and thresholds, not to silently rewrite old findings.

Preserve selected real incidents privately as regression fixtures. Replay can test readiness logic, state transitions, and estimation decisions using logged inputs. It cannot predict changed physical response to new actuator commands because the recorded inputs remain fixed. Use simulation for control-response hypotheses and a supervised physical test to validate the final change. Exact build and effective configuration are necessary for reproducibility.

An AI report assistant may summarize numeric findings, retrieve similar incidents, and draft a proposed test or patch. Require evidence links for factual claims; distinguish observation from inference. No autonomous deployment, motor tuning, or hardware repair action belongs in this pipeline. A human decides whether to apply a change.
