# Main-laptop DuckDB and Parquet analytics

Implemented October 8, 2026. One dedicated driver-station laptop performs detailed processing. Home computers open small standalone reports and original WPILOGs. No robot code changes are required for this extension.

## Enable on the dedicated laptop

Stop the hub before upgrading. Keep its existing local data directory; upgrading migrates the catalog from version 4 to 5 without replacing originals or review history. Install the pinned optional dependency in the hub's existing Python environment:

~~~powershell
python -m pip install -e ".[sftp,analytics]"
~~~

Add these fields to the existing hub configuration (retain its other settings):

~~~json
{
  "schema_version": 1,
  "data_dir": "C:/Team/RobotTestHub/data",
  "analytics_enabled": true,
  "analytics_threads": 1,
  "analytics_memory_mb": 512,
  "analytics_interval": 2,
  "analytics_pause_when_enabled": true,
  "analytics_share_summaries": true,
  "log_export_destination": "G:/Shared drives/Bearbotics/Robot Test Hub",
  "log_export_interval": 60
}
~~~

The example Drive destination must be replaced with an actual existing folder that this laptop can write. Do not put data_dir, SQLite, or DuckDB temporary files in Drive. Sharing remains disabled unless both a destination and analytics_share_summaries are explicitly configured. Existing startup commands and live-source settings remain valid.

Open **Practice summaries** in the hub. It shows converted/pending/retry counts, processing state and the latest completed report. Missing or incorrectly pinned DuckDB installation appears as a failed analytics worker; collection remains independent. Base installation without the analytics extra continues to work when analytics_enabled is false.

For a one-time local backfill, stop the hub and run:

~~~powershell
python -m robot_test_hub.analytics --data-dir "C:/Team/RobotTestHub/data"
~~~

This command owns the data directory, rebuilds its run index, converts existing qualified imports, and creates a report. It does not publish to Drive. An error or missing import produces a nonzero exit. Background operation automatically catches up with qualified successful imports, one conversion at a time.

## Read results at home

Google Drive receives reports/<report-id>/report.html, summary.csv, summary.json and manifest.json, plus reports/index.html. Open the index or newest report folder. Download the entire report folder and open report.html locally; no server, DuckDB installation, or internet connection is needed to view the downloaded report. The HTML links to its CSV, JSON and provenance manifest. Google Drive's web preview may require downloading HTML rather than opening it directly.

Small reports are published before bulk originals. Interrupted publication retries; a report conflict is preserved and does not stop original-log sharing. The manifest is copied last locally, but Drive can synchronize files in another order. A local copy is not confirmation of cloud upload. Check Drive for desktop's status and perform a school-to-home round trip.

Reports contain:
- Recording quality: whole-file counts including disabled-only recordings, plus run cycle counts, invalid/unsupported records, long gaps, overlapping sources and source-order regressions.
- Recorded swerve tracking: time-weighted drive/steering RMSE, weighted 95th-percentile absolute error, coverage and eligible duration for all four positions.
- Recorded drive/turn motor currents: time-weighted mean and peak over eligible interval starts, with eligible durations.
- Conservative repeated-test comparisons: per-position min/max/mean **run RMSE**, independent-run counts and comparison exclusions. This is descriptive comparison, not a pooled sample-level RMSE, approved baseline, or hardware-health verdict.

HTML shows the newest 200 runs. CSV/JSON retain every current run, up to the explicit 10,000-run report limit. Exceeding that or a 64 MiB report-file limit fails visibly rather than silently truncating the machine-readable results. Snapshot files are immutable; a previously completed report remains available while later work retries. Reports reflect the run/review catalog at their capture time, not a continuously live view.

## Comparability and honest missing data

Configure an analysis plan and four physical component assignments using Review & maintenance before repeated testing. A complete, known-boot, single anchored run must fit the plan and assignments. Comparisons require agreeing recorded SourceSHA256 and ConfigurationSHA256, robot identity, mode, source class, exact supported profile, test identity, surface, battery, wheel radius, assignments, preceding maintenance history, artifact hashes, and receipt-age/demand policy. They also require at least 95% eligible recorded tracking coverage. Distinct test plans or configurations remain separate. Use a consistent test_id for repeated exercises and the plan's time bounds to identify sessions.

The pinned Alpha 7 source mapping establishes recorded SI units. Struct schemas must match exactly; contradictory unit metadata, unsupported values, finished/reused entries, unknown connections and stale receipt timestamps cannot become zero errors or healthy evidence. Source-order regressions or overlapping recording intervals invalidate tracking/current summaries conservatively.

Physical sensor acquisition timing remains **unqualified**. Receipt/cycle age establishes only recorded-value eligibility. REAL, SIM, explicitly synthetic and historical/unknown modes are separated. The new statistics neither qualify hardware fault thresholds nor approve a healthy baseline. Existing health reports keep their original qualification rules. Inspect evidence and repeat a controlled test before diagnosing or changing the robot.

## Storage and implementation contract

Only successful imports qualified for wpilib-2027.0.0-alpha-7_akit-27.0.0-alpha-6 are automatically converted. Historical 2026 formats are not newly supported.

Original WPILOGs and their verified JSONL extraction remain authoritative. Under analytics/parquet/<conversion-id>/:
- records.parquet retains every extracted row as row_json plus typed scalar values, exact signed-integer nanoseconds, high-rate sample values/indices, availability, timestamp precision and source provenance. Arrays, nested values, unsupported bytes, lifecycle controls and their original references remain in row_json.
- frames.parquet normalizes four recorded swerve positions per cycle. Held observations retain their own cycle/record references. Positions are labels; physical identities come from reviewed assignments.
- manifest.json pins the original/extraction hashes, profile and mapper/extractor/DuckDB versions, counts, derivative hashes, and source class.

One DuckDB connection per operation uses controlled local paths, pinned duckdb==1.5.6, disabled automatic extension loading/installing, a configurable thread count (1–4), and memory setting (128–4096 MB). The default is one thread and 512 MB. This setting is a DuckDB budget, not a hard total process RSS limit; native algorithms may allocate outside it, and disk spill consumes local space. There is no arbitrary SQL API, remote Parquet fetch or shared writable DuckDB database.

The optional worker uses its own catalog connection and cached status; HTTP status requests never scan raw data or run SQL analysis. Conversion and derivative verification check cancellation between blocks; a watchdog interrupts a native query after fresh enabled status or shutdown. Local publication can finish an individual small artifact before the next cancellation check. This is not a real-time scheduler guarantee. Offline/unknown robot status permits local analysis, which never reads robot storage. Google's sync and the existing importer/indexer are separate and are not paused by this analytics worker.

Conversion jobs are durable and retry failed work after 60 seconds. Native results are cached by run boundaries, source conversions, policy and analyzer version before reading Parquet again. Review/context changes regenerate the report but reuse unaffected numeric results; new recordings query only uncached runs. Published artifacts are hash verified once per worker lifetime, again after restart, and on report download; there is no continuous bit-rot scan. Backups include registered Parquet and summary artifacts in coherent catalog generations. Nothing is automatically deleted.

First-import processing still includes WPILOG extraction, JSONL normalization, integrity reads, Parquet creation, and the existing full run-index rebuild. The legacy bounded health-report path remains in place. This release accelerates native queries and repeated run calculations; it does not claim that all historical indexing or all health analyzers have migrated to DuckDB. It adds local storage beside existing JSONL and creates immutable summary snapshots; monitor disk growth until retention is separately implemented.

## Verification and performance

Run the synthetic benchmark:

~~~powershell
python tools/analytics/benchmark.py --samples 500000 --threads 1 --memory-mb 512
~~~

On this Windows/Python 3.10.7 machine, DuckDB 1.5.6 processed 500,000 invented module samples (125,000 cycles) through the actual weighted-tracking window/aggregation SQL in **1.045, 1.200 and 1.120 seconds**. Parquet was 9,550,726 bytes. Synthetic native preparation took 0.431 seconds. Connections were recreated for each query; OS cache was not flushed. These numbers exclude WPILOG import/conversion and catalog/report work. They are not a school-laptop or subsecond guarantee.

Thirty focused regressions cover official-reader synthetic fixture round trips, nanoseconds above floating-point precision, controls/held state, weighting and wraparound, gaps, missing evidence, native interruption, durable retry, identity conflicts, cached unchanged runs and new-run queries, comparable contexts, source-order anomalies, service/API shutdown, opt-in summary publication and backup restore. Current full-suite and browser evidence is recorded in VALIDATION_CURRENT.md.

Lab qualification still needs real Alpha 7 logs, realistic recording sizes, DS latency/resource measurements while collection and Drive operate, school/home folder access, actual sync completion, and human-reviewed baseline/test plans. No robot deployment, hardware operation or cloud round trip was performed by this implementation.
