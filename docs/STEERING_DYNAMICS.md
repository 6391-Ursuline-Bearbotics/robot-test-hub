# Recorded swerve steering dynamics

Implemented with local author, integration and independent review coverage, October 18, 2026. This extends the full [analysis design](ANALYSIS.md); it does not qualify physical robot health. Current implementation evidence will be recorded in [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md).

## What the operator gets

The fifth automatic check, recorded-steering-observation, evaluates source-pinned steering logger states for each explicitly assigned physical module. It reports wrapped steering error, response to stable command steps, repeated signed-error crossings, abrupt recorded-angle changes and sustained error against optional engineering criteria. Findings identify intervals and original references for investigation in AdvantageScope; they do not prescribe a repair or motor tuning.

Use Review & maintenance to assign all four modules and save a matching analysis plan, as described in [AUTOMATIC_SWERVE.md](AUTOMATIC_SWERVE.md). The optional Recorded steering policy JSON field supplies this check's independent policy. Leaving it blank preserves older plan records exactly; empty policy has structural defaults and no warning thresholds. Loaded policies survive edits and saves as a new plan revision. Invalid or duplicate keys and nonfinite numbers must fail rather than silently changing criteria.

Supported REAL recordings can supply descriptive recorded-state results even when physical acquisition freshness is unknown. This does not change the existing fresh swerve-tracking eligibility policy. Overall robot health remains not_assessed, this analyzer is ineligible for healthy baseline approval, and no automatic tuning, command, deployment or source deletion occurs.

## Recorded evidence and windows

The source mapping remains 6391-alpha7-final-swerve-2: exact Alpha 7 SwerveModuleVelocity and Rotation2d schemas, final optimized/cosine-scaled/desaturated IO commands, measured arrays, and all three recorded connection flags per module. The selected plan, software/configuration context, component history and complete run must agree. No component identity is invented from its position.

Original command/measured/connection records and logger-cycle references are checked against the immutable input. Held values retain their original record identities. A logger interval is not a new physical sample. Unknown physical sampling counts and acquisition times remain unavailable. Source changes, lifecycle/schema boundaries, missing mapped cycles, nonadvancing time, disconnection, invalid values, near-zero demand and oversized gaps must interrupt eligible windows; event dwell cannot bridge them.

Error is the wrapped measured-minus-command angle. Duration-weighted RMSE and p95 use left-held logger states over adjacent eligible cycles, without interpolation. Angular wrap at minus/plus pi is not a jump. A near-pi change accompanied by signed wheel-velocity reversal is handled as an equivalent optimized representation, without diagnosing an encoder or control fault from that representation change.

A step is a command-angle change at least command_step_rad. The target must remain within target_stability_rad, and entering settle_band_rad must persist for settle_dwell_seconds. Short, interrupted, target-changed and unfinished observations are explicitly censored; their settling time is unavailable, never zero. Repeated crossings count changes between error signs outside the settle band during sufficiently long stable-target windows. Recorded angle-change rates divide wrapped adjacent logged changes by logger interval duration; they are not qualified physical angular velocities.

## Policy

Structural defaults define the reproducible observation, not acceptable hardware performance:

| Setting | Default / unit |
| --- | --- |
| max_gap_ns | 250000000 nanoseconds |
| min_demand_mps | 0.1 meters per second |
| minimum_eligible_seconds | 1 second |
| minimum_coverage | 0.8 fraction |
| command_step_rad | 0.15 radians |
| target_stability_rad | 0.02 radians |
| settle_band_rad | 0.05 radians |
| settle_dwell_seconds | 0.1 seconds |
| minimum_stable_seconds | 0.5 seconds |
| sustained_error_seconds | 0.5 seconds |

Optional finding criteria are settling_limit_seconds, oscillation_crossing_limit, recorded_jump_limit_rad and sustained_error_limit_rad. All default to null. Configuring any criterion requires a nonempty threshold_revision. Select criteria from supervised repeatable tests and recorded evidence; no default physical fault threshold is supplied. Censored observations must remain distinguishable from completed settling, even when they support a warning that the configured observation window was exceeded.

Per-module metric names are recorded_steering_rmse_rad, recorded_steering_p95_absolute_error_rad, recorded_settling_max_seconds, recorded_oscillation_crossings, recorded_angle_jump_max_rad, recorded_angle_rate_max_rad_per_second, and longest_recorded_sustained_error_seconds. Metrics with inadequate coverage or no relevant event are unavailable. A complete no-finding result evaluates configured recorded-state criteria only, never certifies health. No configured criteria, missing required steps or stable windows, and partial/invalid coverage cannot supply an unqualified no-finding result.

## Bounds, revisions and commissioning

The historical tuple check admits at most 50,000 retained input rows and stores at most 128 events across all categories plus 128 event supporting references per module, with full-run aggregate and criterion counts. Traces retain 128 intervals per module; finding references share a 128-reference budget for the complete check. Input and supporting evidence are bounded. Event/trace displays must label their retained prefixes and total observations; later events still contribute to full-run metrics and findings. A capped display cannot hide a late warning or imply that omitted trace intervals can be paged when they were never retained. Complete raw evidence stays in the original WPILOG and private executable incident case.

Current automatic pipeline 7 adds the fifth check while preserving explicit pipeline 5 and 6 factories, saved report bytes and exact original-to-input case reconstruction. Versions 6 and 7 share the existing field-selection policy; the pipeline/selector pair and analyzer/configuration identities remain pinned. Old plans without steering_configuration keep that field absent. Analyzer version changes in saved cases require explicit opt-in and never replace their preserved rows.

No additional robot instrumentation or deployment is part of this increment. First collect the existing supported signals, verify actual units/reference semantics and clocks, inspect warning intervals manually, and repeat the same supervised test. Physical steering dynamics, acquisition latency, control-loop performance and unique mechanical causes require independent measurements. Follow [log-first commissioning](LOG_FLOW_COMMISSIONING.md); camera work is optional.

Initial local verification: 34 focused cases passed, followed by the frozen Windows/Python 3.10.7 suite: 1,083 tests in 314.276 seconds, 1,062 passed and 21 optional skips. A later 880-cycle resource test exposed an API metadata-size failure. Shared event/reference budgets and truthful projection counts were then added; their final integrated evidence is recorded in VALIDATION_CURRENT.md. Actual browser policy saves/reload and pinned five-check report inspection passed using invented data; exact pipeline-6 report bytes also matched the previous verified source package. See VALIDATION_CURRENT.md for boundaries and later CI results.

After the budget/projection fixes, a second complete suite ran 1,085 tests in 318.488 seconds: 1,064 passed and 21 optional skips. The last interval-total metadata aliases and new regression were verified by the final 37 focused cases; exact-head hosted CI remains a separate check.

## Full-source steering path

Pipeline 11 adds a separately versioned streamed implementation after pipeline-7 resource overflow. It preserves the fixed tuple and stream policies 5–10; its distinct result/work/numerical/context identities are not substitutions for an old executable case. See [streaming contracts](STREAMING_RUNTIME.md) and [current qualification](VALIDATION_CURRENT.md).

The new evaluator persists bounded step/dwell/hysteresis/censor state and exact integer-duration weighted percentiles. The final endpoint may begin an observation with no elapsed time; this is censored, never a completed zero-time response. Both steering endpoints must be enabled; its expected-duration denominator is distinct from tracking. REAL results describe recorded states only. Strict matching recorded build/configuration evidence is required even when a human plan names the expected build.

Ordinary invalid/missing supported motion payloads interrupt windows and count against declared coverage. Valid intervals can still produce metrics/findings above the coverage minimum; missing data prevents a clean no-finding claim. Source/order/identity/schema/authority failures are hard unavailability. The exact classifier is pinned in the saved context. Four modules share 128 retained reference copies and a 384 KiB reference-byte allowance across displayed traces, pending windows, events and findings, with exact full-run/omitted totals. These display limits never truncate calculations. Run history groups measurements, error plots and completed/censored event examples per module; retained examples cannot be paged as a complete trace. Streamed private case proof/export remains a separate unfinished milestone.
