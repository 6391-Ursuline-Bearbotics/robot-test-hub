# Executable offline analyzer regression cases

The original tuple format was implemented locally October 15, 2026. A separate full-source streamed format was added October 22; qualification is recorded in [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md). This increment adds executable checks for the hub's analysis code. It does not execute Java robot code, replay changed actuator responses, prove a physical cause or establish that a repair worked. Robot-behavior replay/simulation and a later independent physical retest remain required parts of R10.

## Intended workflow

1. Save a human finding review in Evidence review. Inspect its exact retained revision in Saved review history. Identify the review ID and revision to preserve; later revisions must not replace it silently.
2. Write explicit expected behavior in a small private JSON file. Choose engineering expectations deliberately; copying today's output automatically would merely preserve today's bug. Expectation success means the declared software checks matched, never that the robot is healthy.
3. Export that exact incident to a private local case directory outside Git and outside the working archive. The case contains the original WPILOGs, fixed selected input rows, saved analyzer result/configuration/context, exact review revision, expectations and checksums. Originals can contain a full recording, including activity outside the incident interval; treat the entire case as private telemetry.
4. Run the case against an installed candidate hub version. It verifies all preserved bytes, executes the allowlisted analyzer on fixed inputs using fresh work state, and compares the declared outcome and checks. Tuple cases use an in-memory job store; streamed cases use an owned temporary archive as described below. Source evidence and historical reports remain unchanged. SQLite may create or use its normal -wal/-shm reader-coordination sidecars even with mode=ro and query_only; this is not a zero-filesystem-write guarantee. No journal is deleted or ignored to avoid that behavior.
5. Keep the bounded result with the proposed analysis change. A deliberate analyzer-version change needs an explicit option and is recorded with current code hashes. Use real independent runs for physical repair validation; case execution alone does not validate a different physical response.

A case includes the incident only. Maintenance links without saved revisions remain unpinned; linked validation jobs are marked not included. An embedded approved-baseline summary stays a frozen input, without claiming that its whole cohort was recomputed.

The existing regression-reference-1 review bundle remains a reference export with contains_raw_data=false. It is not converted into an executable case or relabeled as a successful repair. These local cases are a separate artifact with explicit operator-selected expectations.

## Expected behavior file

```json
{
  "schema_version": 1,
  "expected_outcome": "finding",
  "checks": [
    {"kind": "finding_count", "finding_kind": "recording_gap", "min": 1, "max": 1},
    {"kind": "metric", "selector": {"name": "gap_count", "unit": "count"}, "min": 1, "max": 1}
  ]
}
```

This is an illustrative invented case, not a recommended hardware threshold. There must be 1–64 checks. Bounds are inclusive; provide min, max or both. Metric selectors require an exact name and unit and may additionally select module_id, module_position, module_index or signal. The selector must match exactly one metric with a finite numeric value. Missing, null, nonfinite and ambiguous values fail. Finding counts select an exact finding kind and use nonnegative integer bounds. Unknown fields, duplicate JSON keys, malformed ranges and unsupported expectations are rejected rather than ignored.

An expected unavailable or insufficient-data analyzer outcome can be an intentional software-behavior test. It must be explicitly declared and cannot establish health or data adequacy. A package/compatibility/integrity failure is unavailable execution and cannot satisfy an expectation.

## Tuple-format reproducibility and limits

Capture reads a consistent read-only catalog snapshot and pins a finding_review by ID and revision. It reconstructs the original automatic-report input selection using the saved context, plan and baseline. It must reproduce the complete input provenance and rows_digest; current review history, current baseline selection and current run-catalog revisions cannot substitute for the saved evidence. Unsupported old selectors/importer/mapping versions fail explicitly. Supported exact pipeline/selector pairs are `automatic-reports-5` with `source_order_required_schemas_lifecycle_held_state_phoenix_sdk_observations_and_run_cycles`, and `automatic-reports-6` or current `automatic-reports-7` with `source_order_required_schemas_lifecycle_held_state_phoenix_sdk_system_status_observations_and_run_cycles`. Reconstruction uses the saved pair and never inserts new SystemStats rows into a version-5 case. Unknown or mismatched pairs fail explicitly. Version-5/6 recording-quality cases require `--allow-analyzer-version-change` to evaluate with current version 7; original-to-input proof still reconstructs their fixed historical rows. This path covers the current automatic-report analyzers, not arbitrary user-supplied Python, command strings or robot replay programs.

The export verifies original and derived source identities before publication. Case verification rehashes all originals and structured artifacts, then reimports the original logs with the supported pinned extractor into a fresh temporary archive and reconstructs the exact selected rows/context. Dataset and manifest receipts must match too. This tests the original-to-input relationship rather than accepting a coordinated set of replacement hashes. Only after that proof can the candidate analyzer run. Publication uses an exclusive directory and manifest-last completion point, with conflicts/partials preserved. Checksums are integrity receipts rather than signatures. Keep case sources and software trusted; no automatic Drive publication, Git upload, installation, endpoint access, deployment or physical action occurs.

Bounds include at most 16 originals, 2 GiB per original and 8 GiB total originals, 50,000 selected rows, 64 MiB normalized input and 64 MiB saved job. Oversized or incomplete evidence fails explicitly rather than running a truncated case. Selected normalized inputs are fixed. Verification reruns the supported pinned decoder/selector to prove them; candidate evaluation does not treat a changed ingestion algorithm as interchangeable. Unsupported decoder/selector versions fail explicitly. The proof requires temporary SSD space for another original/derived copy; a large case can be expensive to verify. Preserved originals support later importer/robot-replay case extensions.

## Full-source streamed cases

Use explicit export format `offline-stream-analyzer-regression-1` for saved streamed pipeline-8/9/10/11 checks. The existing `offline-analyzer-regression-1` tuple export remains the default and keeps its original 5/6/7 selection and proof semantics. The streamed format currently accepts the pinned Alpha 7 profile/mapping only; historical 2026 importer support does not imply streamed-case support. Verify/run dispatch by the manifest format; there is no silent conversion or raised tuple row cap.

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.regression export `
  --format offline-stream-analyzer-regression-1 `
  --data-dir "C:\RobotHubPrivate\archive" `
  --review-id "review-example" --review-revision 1 `
  --expectations "C:\RobotHubPrivate\expectations.json" `
  --output "C:\RobotHubCases\streamed-incident-example"
.\.venv\Scripts\python.exe -m robot_test_hub.regression verify `
  --case "C:\RobotHubCases\streamed-incident-example"
.\.venv\Scripts\python.exe -m robot_test_hub.regression run `
  --case "C:\RobotHubCases\streamed-incident-example" --json
```

Capture pins the exact review revision, full saved check, descriptor, original/import receipts, run/context and fixed stream policies. The private bundle contains originals, complete selected chunk bytes, a paged JSONL chunk inventory and final stream manifest. It contains no executable case code, catalog database, trusted checkpoint or copied percentile work. Publication uses an exclusive new directory and writes `manifest.json` last; failed captures preserve partial output for inspection. The source read-only SQLite snapshot closes before bulk copying, so a long export does not hold a source WAL reader throughout file transfer.

Verification reimports every included original with its pinned decoder and source classification in a fresh owned temporary archive, reconstructs the entire fixed stream through EOF, recomputes reducer/quantile work, and compares exact chunk bytes/order/ranges, final manifest, descriptor and saved check. The saved job ID alone is insufficient because it does not hash saved result bytes. Only originals selected by this incident are exported. Global catalog/clock revisions, human plans/context, review authority and import origins remain retained declarations: the scope is `source_to_analysis_reproducibility_under_retained_run_context`. Internal hashes prove reproducibility under those declarations, not authenticated historical origin or independent correctness of human context. A coherently reauthored entire case is not a signed historical record.

Run first proves that historical result, then reimports/reduces again in a separate fresh workspace using an installed allowlisted candidate. Candidate output never comes from the saved result or proof checkpoints. Default candidate names are `<fixed-pipeline>:<analyzer-id>` for converted checks under that policy. `--candidate` selects a trusted installed registry entry, never imports package-supplied Python. An analyzer-version change additionally requires `--allow-analyzer-version-change` and the same declared input contract. That option does not authorize changed selectors, source origins, mapping, build admission or pipeline reinterpretation. An old policy whose proof implementation is unavailable must fail explicitly. Results record stored and candidate input identities, observed evaluator-module hashes, loaded method fingerprints and Python version. Expectations use the same strict finite/unique-selector/inclusive-bound rules above. Unavailable packaging or proof cannot satisfy an expected analyzer outcome.

Default cooperative admission bounds:

| Resource | Limit |
| --- | --- |
| Selected originals | 128, 2 GiB each, 8 GiB total |
| Published bundle | 16 GiB including metadata/inventories/chunks |
| Temporary proof/candidate work | 16 GiB including retained bundle bytes and work copies/derivatives/catalog; 2 GiB free-space reserve |
| Structured metadata / inventory line / inventory query | 4 MiB / 8 KiB / 128 rows |
| Selected chunks | 65,536; at most 1 MiB per chunk |
| Raw record / entry declarations | 1 MiB / 4 MiB and 65,536 entry IDs |
| Decoder schema text / held payloads / decoded held-row envelope | 4 MiB / 8 MiB / 16 MiB |
| Decoded row and guarded import batch | 1 MiB each |
| Retained import metadata | 1 MiB and 4,096 fields |
| Source snapshot SQL | 5,000,000 progress steps |

These are admission limits, not truncation rules or an OS-enforced disk quota. Planned file growth is admitted before guarded writes with conservative transaction/WAL headroom; final SQL report publication reserves additional growth and is checked afterward; source framing is preflighted before decoding. A source within the raw-size limit may still exceed derivative/work bounds and fail explicitly. The ordinary importer retains its previous byte serialization and batching when no regression resource guard is supplied. Python state and inventories stay bounded; total proof time remains proportional to the full source. Each verify/run is potentially expensive and requires private local SSD space. There is no camera/robot/network contact or cloud publication.

Temporary archive ownership remains held until runner and database handles actually close. A failed close returns `regression_cleanup_pending` and retains the in-process workspace/owner for `stream_regression_proof.cleanup_retained_work()` to retry; it does not claim completed cleanup or start a detached worker. A standalone CLI process exiting cannot retain an OS lease after exit; inspect any leftover private temporary work before manual recovery. Original cases and source archives are preserved.

This format extends the analyzer regression loop to dense full-source incidents. It does not execute Java robot behavior, predict changed actuator responses, qualify sensor freshness or establish a successful physical repair.

## Remaining robot and physical validation

A robot-replay adapter still needs pinned robot source/build assets and effective configuration, an approved installed toolchain, REPLAY IO with no hardware/NT side effects, fixed clocks and declared command/output expectations. It must record exactly which candidate robot build ran. Recorded inputs cannot predict the physical response to changed motor commands.

A later physical validation chain still needs explicit analyzer/test/context compatibility, component and maintenance boundaries, and known nonoverlapping intervals after the recorded change. A linked later job alone is not proof of a successful repair. These requirements remain open; analyzer-case success does not close them.

## Commands

From a complete installed hub source tree, with an existing private archive and a new private case folder:

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.regression export `
  --data-dir "C:\RobotHubPrivate\archive" `
  --review-id "review-example" --review-revision 1 `
  --expectations "C:\RobotHubPrivate\expectations.json" `
  --output "C:\RobotHubCases\incident-example"
.\.venv\Scripts\python.exe -m robot_test_hub.regression verify `
  --case "C:\RobotHubCases\incident-example"
.\.venv\Scripts\python.exe -m robot_test_hub.regression run `
  --case "C:\RobotHubCases\incident-example" --json
```

Replace the sample review ID and paths. Create the private parent folder first; the output case directory itself must not exist. Export rejects archive/source-tree ancestry and Git worktrees. It preserves existing outputs rather than merging or overwriting them. Keep partially written groups for inspection; a manifest alone does not bypass complete verification.

Run uses the package installed in the interpreter you invoke. To test a changed analyzer version deliberately, add --allow-analyzer-version-change. The result records stored/current analyzer versions, Python runtime, observed module-file hashes and the loaded evaluator's code-object fingerprint. File hashes describe observed source files; the loaded-code fingerprint describes the actual callable and can vary with Python/build metadata. These are provenance receipts, not signatures.

The result is structured JSON. Exit zero means verification or the declared test passed; failed expectations, unavailable execution and operational errors return nonzero. Known failures use fixed codes/guidance without printing private paths, SQL or raw exception messages. The analyzer allowlist is recording-quality, swerve-tracking, phoenix-status-observation, system-status-observation and recorded-steering-observation. Unknown analyzers are unavailable and never execute package-supplied code.

## Qualification

Implementation and independent review exercise actual public synthetic WPILOG decoding, exact saved review revisions and frozen plan/context, pre-refactor report/job/input identity oracles, coordinated rehashed row/job/review forgeries, byte corruption, links, resource bounds, fresh evaluator execution, genuine analyzer-version changes, strict expectations and explicit unavailable behavior. Source-preservation checks cover closed WAL-mode catalogs as well as current catalog/context edits. Parent actual CLI qualification exported revision 1 with revision 2 already present, verified and ran a passing and intentionally failing case, observed the expected exit codes and identical repeat results, and proved closed-source evidence bytes unchanged while recording normal SQLite sidecars. The final full suite and exact-head CI are recorded in current validation. This does not qualify real robot replay, physical health or repair effectiveness.
 The [software audit](SOFTWARE_READINESS.md), [review history](REVIEW_HISTORY.md), [analysis design](ANALYSIS.md) and [log-first lab checklist](LOG_FLOW_COMMISSIONING.md) preserve the broader goal and physical boundaries.
