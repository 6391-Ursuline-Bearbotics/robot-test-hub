# Recorded SystemCore electrical and fault observations

Implemented locally with focused author, integration and independent review coverage, October 18, 2026. The automatic system-status-observation check uses source-qualified AdvantageKit inputs to expose recorded fault flags, counter changes and descriptive voltage statistics. Physical fault semantics, acquisition freshness and battery-health thresholds remain unqualified.

## Source profile

The inspected artifact is org.littletonrobotics.akit:akit-java:27.0.0-alpha-6:sources, SHA-256 d822d7625a9553c311526134721ceabac79035a5f296c3e88f6bac7bda3cc1ea. Parent and implementation independently inspected the installed source JAR. Reinspect the corresponding source when changing AdvantageKit/SystemCore versions.

| Source file | SHA-256 | Verified behavior |
| --- | --- | --- |
| org/littletonrobotics/junction/Logger.java | 7b294da27548c910e542f8a3685f3ca0494bdc21c464c6a9c3a6acc5d7fb161a | Calls LoggedSystemStats.saveToLog on the SystemStats input subtable when not replaying |
| org/littletonrobotics/junction/LoggedSystemStats.java | 5527ab2217f86a53aff35c1e351b8dc1deb6a19891d52d3633a32618ba93e73c | Writes BatteryVoltage with volts metadata and the named fault flags/counters |
| org/littletonrobotics/conduit/ConduitApi.java | 8042bd2ac2539db0a1626a5b9d4cf156a64be76db9b1e92123f7d3052de2eab1 | Returns schema fields directly; the inspected getters add no current/latched/cumulative/reset guarantee |

The supported WPILOG schema is the existing pinned Alpha 7 importer profile. Fields are exact input paths:

| Field | WPILOG type | Unit |
| --- | --- | --- |
| /SystemStats/BatteryVoltage | double | volts |
| /SystemStats/Faults/<name> | boolean | absent |
| /SystemStats/FaultCounts/<name> | int64 | absent |

Names are Brownout, CANBusDown, CANBusUnavail, Display, IMU, IO, RSL and USB. Unit, type, category, validity, source hash, record index, entry generation and timestamps must agree. Similar-looking outputs and historical roboRIO fields are not aliases for this profile. A field labeled Brownout does not by itself qualify a battery diagnosis; the counter label does not prove an exact number of physical events.

## Operator result and metric definitions

Automatic pipeline 6 introduced the fourth check; pipeline 7 retains it in the saved run report. Exact true recorded fault flags produce explainable warnings with source references. Voltage statistics summarize qualified recorded logger states. Counter increases/decreases describe observed changes with qualified in-run ordering; a decrease is not automatically called a hardware reset. The existing run-report view displays metrics, unavailable reasons and pinned coverage/provenance.

Unchanged AdvantageKit values may be delta encoded. The analyzer replays held logger state across cycles, respects entry finish/reuse and source boundaries, and marks held observations. A logger state is not a fresh physical measurement. Before-run state can explain a held flag, but a pre-run counter difference cannot be counted as an in-run physical event.

Positive fault evidence remains visible when other fields are missing. Missing, invalid, partial or ambiguous data cannot produce a complete no-finding result. Resource/order/gap problems remain explicit. A complete no-finding result means the supported recorded checks found no warning; overall robot health remains not_assessed.

The profile contains exactly 17 fields: one voltage, eight booleans and eight counters. Counter values accept nonnegative int64 integers or canonical unsigned decimal strings (`0` or no leading zeros, below 2^63); booleans and floats are rejected. Findings store previous/current values and signed changes as decimal strings so values above JavaScript's safe-integer range retain exact differences. Changes require two distinct in-run observations from the same source and entry generation in advancing record/time order. A pre-run value can supply held flag/voltage context but cannot supply a counter-change baseline. Gaps above 250 ms, source changes and lifecycle breaks clear comparison state.

`recorded_battery_voltage_min` and `recorded_battery_voltage_max` use valid voltage logger states at eligible in-run cycles, including held states. `recorded_battery_voltage_time_weighted_mean` weights the left state over adjacent cycles separated by more than zero and at most 250 ms, within the same source and compatible voltage entry generation. No final extrapolation is added. `eligible_seconds` is the summed interval duration; an absent eligible interval yields null mean, not zero. The 250 ms bound qualifies logger continuity only. Source/lifecycle/order/invalid-value gaps prevent a complete no-finding result; positive evidence remains reportable. Complete coverage requires at least two cycles, both run endpoints and every field at every expected cycle, with no structural unavailable reason. Physical qualification limitations remain listed even for `evaluated_no_finding`.

Warnings deduplicate a held true flag by original record identity rather than counting each cycle as a new fault. Finding groups retain at most 128 source-reference entries and explicitly flag truncation. Selection/analyzer resource limits are 50,000 rows; overflow is unavailable rather than a partial pass.

Voltage is descriptive. Matched-load sag detection, battery comparisons, physical acquisition age, fault-latching semantics and causal reset/brownout diagnosis require independent commissioning and additional context. No threshold is inferred from the field name and no motor setting or robot command is changed.

## Versioned analysis and preserved cases

Pipeline `automatic-reports-6` introduced this check; current pipeline `automatic-reports-7` retains it, with selector `source_order_required_schemas_lifecycle_held_state_phoenix_sdk_system_status_observations_and_run_cycles`. It retains the new typed observations and adds system-status-observation. Existing saved reports/jobs remain immutable.

Explicit automatic-reports-5 and automatic-reports-6 selectors remain supported as an explicit policy with its exact old field selection, source order and job/report/input identities. [Private analyzer cases](REGRESSION_CASES.md) dispatch reconstruction by the pinned pipeline/selector pair. They never substitute the new rows into an old case. Running an older recording-quality version against the current version still needs --allow-analyzer-version-change; the original-to-input proof stays pinned to the older selector.

This versioning is necessary even for an older log without the new fields: report declarations and provenance have changed. Historical missing fields become unavailable system checks, without fabricating zero voltage or false fault flags. Unknown selectors remain unsupported.

## Lab preparation and remaining work

No robot code or deployment is part of this change. The pinned logger already emits these fields when the corresponding inputs are available. First confirm their actual paths, types, values and availability on collected lab recordings. Record the deployed image/library/build identities; source inspection is not firmware or sensor qualification.

Log battery identity and use repeatable load/test context before comparing voltage between sessions. Verify any optional power-distribution telemetry against the installed SystemCore toolchain and actual hardware before adding it; this check does not assume a roboRIO-style default power-distribution device.

Keep original logs and inspect warning intervals in AdvantageScope and the notebook. Establish source/counter behavior under the team's approved bench procedure before interpreting changes as physical events. Never induce dangerous faults to validate a detector. Use [the log-first commissioning checklist](LOG_FLOW_COMMISSIONING.md); camera work remains optional.

Focused validation: `python -m unittest discover -s tests -p "test_system_status*.py" -v` ran 26 tests and passed on October 18, 2026. This includes typed binary WPILOG import through the actual pipeline, literal pinned-source inspection, held/pre-run state, lifecycle/entry reuse, gaps/order/resource hazards, wrong types/units, exact large int64 differences, fourth-check reports, historical selector identity preservation, old-case proof/version opt-in and rejection of coherently resealed invented rows. This focused run is not a full-suite pass; integrated evidence is maintained separately in [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md). The broader [analysis design](ANALYSIS.md) still includes physical steering qualification, effort/thermal, matched-load electrical, autonomous, shooter, intake/indexer and vision analyzers as separate work.
