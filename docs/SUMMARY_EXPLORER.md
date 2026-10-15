# Portable practice/event summary explorer

Implemented locally October 15, 2026, extending T22. This closes the portable report's newest-200-run limit without requiring analytics software or a hub server on home computers. Physical thresholds, camera alignment and school commissioning remain separate.

## Operator workflow

Open a downloaded complete report folder's report.html. Filter its immutable snapshot by robot, source class, runtime mode, test identity and UTC interval. Move through bounded pages to reach every saved run, including those older than 200. Inspect recorded tracking/current metrics, source identities and clock context. Original complete CSV, JSON and manifest remain adjacent links.

UTC selection uses inclusive overlap with any saved UTC candidate expanded by its recorded uncertainty. Inputs require explicit ISO UTC ending in Z, support up to nine fractional digits, and reject invalid calendar dates or reversed bounds. It is a candidate search, not proof that an ambiguous clock mapping is correct. Unknown UTC is explicitly controlled, interval errors do not silently broaden selection, and uncertainty/multiple candidates remain visible. Ordering compares exact nanosecond integers. Selectors use exact values; missing context is unavailable, not an empty matching identity.

## Comparisons and scope

Recompute descriptive comparison rows from selected independent runs only, within each existing non-null cohort identity. Existing conservative grouping continues to separate source classes, robot/build/configuration, reviewed test context, maintenance and component assignments. Never make a new cohort out of excluded runs. Match metrics by module position, keep missing/nonfinite metrics unavailable, and distinguish total independent runs from runs with a usable metric. Means/minima/maxima describe run RMSE values; they are not pooled sample statistics, approved baselines or health verdicts.

Whole-recording quality belongs to the entire captured snapshot and includes disabled recordings; filter controls must not imply it describes only selected run intervals. It remains separately labeled and bounded in the DOM. All saved run metrics/evidence must be reachable.

## Offline and immutable contract

The renderer embeds the complete existing summary document as escaped non-executable JSON and a fixed application script. It reads packaged static assets through importlib.resources. A computed script CSP hash authorizes only the fixed script; no unsafe-inline script, eval, external assets, fetch, server, local storage or DuckDB execution is needed. All data enters the DOM through text-safe APIs. A no-script message explains how to use CSV/JSON.

Run, cohort-group and whole-recording sections each show at most twenty cards per page. Exact-identity selector options are bounded by the existing 10,000-run snapshot limit. These page sizes bound evidence rendering; the existing 10,000-run and 64 MiB artifact limits remain visible failure boundaries. Presentation version changes produce a new immutable report identity while unchanged numeric statistics retain their existing cache identity. Old reports are not rewritten. The existing schema remains compatible with truthful pagination metadata. Snapshot download/Drive/backup contracts are unchanged.

## Acceptance evidence required

Use Sol implementation and independent review. Tests must execute actual filter/ordering/aggregation code, cover runs beyond 200, exact nanoseconds, invalid bounds, missing/ambiguous UTC, cohort separation and metric availability. Inject hostile identifiers/text and verify embedded JSON cannot escape its script block, fixed-script CSP matches its exact bytes, and no report payload becomes executable code. Verify actual generated reports and cached unchanged statistics, old report preservation, packaging assets and complete original CSV/JSON. Exercise the generated report in a browser with synthetic data, including navigation/filtering/offline behavior. Run the complete regression suite after implementation/review freeze and record exact published CI separately.

## Executed local qualification

Six author, ten UI/model and five independent tests pass. They exercise 10,000 rows, exact UTC/uncertainty boundaries, invalid calendar inputs, unknown/ambiguous clocks, all three paginated sections, hostile text, safe full JSON embedding, exact fixed-script CSP, module-position aggregation, missing/duplicate metrics and retained numeric caches. Thirty existing columnar regressions also passed before the complete-suite run. Real generated-fixture report upgrades preserve old bytes while avoiding repeated DuckDB run queries. Packaged-resource tests include zip-backed resources.

Parent browser qualification used a clearly invented 265-run stress snapshot based on genuine synthetic-fixture statistics. Page 12 reached rows older than the former 200-run boundary with exactly 20 cards. Selecting one robot produced 130 runs and a comparison over all 130, separate from the unchanged whole-recording count. A secondary UTC candidate selected one run; explicitly including unlocated context added five unknown-time runs. Invalid February 30 cleared selected run/cohort claims. Hostile markup appeared literally. The disposable host received only the initial report request; filtering still worked after host shutdown.

The managed browser rejected direct file-protocol navigation under its URL policy; no alternate browser or bypass was attempted. Browser evidence therefore covers the loaded self-contained HTTP document and operation after shutdown, not an executed file:// launch. Actual local file opening on the school/home browser remains in commissioning.

An actual wheel built offline with installed tools contained both explorer assets and runtime sample resources. Isolated Python imported that extracted wheel outside the checkout; core and requested-analytics selftests both passed. No live hardware, private archive, camera or cloud destination was used. Complete-suite and published CI results are recorded in [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md).

## Upgrade and use

With the normal analytics worker enabled, restart the updated hub through the existing graceful StopHub/StartHub procedure. Its next summary uses presentation version summary-2 and the unchanged numeric-cache version summary-1. Download the complete new report folder through the existing summary/Drive workflow, then open report.html locally in a normal browser. JavaScript is needed for interactive views; CSV/JSON remain accessible without it. Old immutable reports keep their original presentation. Treat the full report folder as private robot evidence.
