# Share completed logs with home through Google Drive

October 8, 2026: one dedicated driver-station laptop owns collection. Collector handoff, shared writable catalogs and multi-computer note merging are outside this workflow.

## What is implemented

The opt-in exporter copies completed collector originals and manually imported originals from the local catalog to an explicitly configured, existing sync folder. It never connects to the robot, copies the live SQLite database, discovers Google accounts, uploads to GitHub, or deletes local/robot originals. Robot download permission and its queue remain unchanged.

Objects use their full SHA-256: logs/<first-two-hash-characters>/<sha256>.wpilog, with a matching .json sidecar. Each original is copied once, even if referenced by several transfers/imports. The logs.csv index maps original filenames to shared paths and publication times. Publication time is **when the hub shared the file**, not the event/recording time; actual run times remain inside the WPILOG. Spreadsheet formula characters in original filenames are escaped in CSV; JSON preserves the original name.

Copying checks size and SHA-256 and reads back the destination. A WPILOG magic/header-length check excludes opaque demo bytes; this is not full parser compatibility qualification. Metadata is published after successful local verification. Google may synchronize files in a different order, so a sidecar appearing at home alone is not proof of completion. Local receipts avoid re-reading gigabytes on every poll. Later corruption of a same-sized shared file is detected by the explicit home verification command, rather than continuously re-hashing every published log.

Missing sync folders are never created automatically: a missing Drive mount must not silently redirect copies onto the wrong filesystem. Failures retain originals and retry on the next interval or restart. Previously completed files are reused. An interrupted unpublished file keeps its .writing artifact and restarts that file's copy from the beginning; published files are not recopied. Conflicting existing originals/metadata are preserved and produce visible errors. Source checkpoint resume remains separate and unchanged.

Cached status appears in /api/v1/log-export and /api/v1/status.log_export. The Transfers page shows copied/pending counts and bytes, skipped non-WPILOG files, retries and errors. Paths/exception details are not exposed. **Copied to Drive folder does not mean uploaded to Google.** The cloud_upload_confirmed field remains false. Google Drive for desktop owns authentication, upload queues, retry behavior and bandwidth controls.

## School setup — once, on the dedicated laptop

1. Install Google Drive for desktop and sign in locally with an authorized school account. Create a team-controlled folder such as Robot Test Hub/Original Logs and grant the home account access.
2. Find that folder's actual path in Windows Explorer. Shared Drive layouts and drive letters differ; the example below is not a configured location. The folder must exist, be writable and be outside local hub data. Linked/junction destinations are rejected.
3. Keep the working archive on the laptop SSD, outside Drive. Add these fields to the existing private data/hub-config.json, preserving other live settings:

~~~json
{
  "schema_version": 1,
  "data_dir": "C:/Team/RobotTestHub/data",
  "log_export_destination": "G:/Shared drives/Robotics/Robot Test Hub/Original Logs",
  "log_export_interval": 60
}
~~~

4. Start the existing live hub command with --config data/hub-config.json. Retain the explicit source/status/Java arguments; sharing does not configure robot access. The exporter checks every 60 seconds by default. A null destination disables it.
5. Collect one short test log. Confirm the Transfers page shows a copied log, confirm Drive for desktop reports sync complete, then download and verify the file and JSON sidecar with the home account.

No Google passwords, tokens or private logs belong in this public repository. Accounts and permissions are configured in Google's own software.

For deliberate one-time publication without running the service:

~~~powershell
python -m robot_test_hub.log_export publish --data-dir C:/Team/RobotTestHub/data --destination "G:/Shared drives/Robotics/Robot Test Hub/Original Logs"
~~~

Only one publisher per destination is supported. A destination ownership lock rejects competing local writers. Source reads are committed read-only SQLite queries and may run alongside collection.

## Home workflow

1. Open the shared folder in Drive, or install Drive for desktop at home.
2. Use logs.csv to find an original filename and its WPILOG path. Download both the .wpilog and matching .json into the same folder. Ignore incomplete .writing files.
3. If the hub is installed at home, verify the downloaded original:

~~~powershell
python -m robot_test_hub.log_export verify "C:/Team/DownloadedLogs/FULL_SHA256.wpilog"
~~~

4. Open the WPILOG normally in AdvantageScope. The hub is not required merely to open a log. For hub analysis, use the existing manual importer with a separate home archive; see [IMPORTER.md](IMPORTER.md).

This workflow shares originals, identity sidecars and the filename index. Optional [main-laptop analytics](COLUMNAR_ANALYTICS.md) also publishes small HTML/CSV/JSON reports when analytics_share_summaries is explicitly enabled. It does not synchronize notebook revisions, video, connection settings, Parquet or the live searchable catalog. For occasional complete hub recovery use [verified backup generations](BACKUP.md); never point active data into Drive. Home imports regenerate supported run/analysis information but cannot reconstruct hub-only notes or approvals.

## Practice and storage

Export copies only local completed originals, independently of robot transfer, and can continue after the robot is off. It does **not** automatically pause Google's background network traffic when the robot enables. Initially pause Drive synchronization during driving if it shares a constrained connection, then resume after practice. Measure disk/CPU/network load before concurrent sync. The collector-pause button controls robot downloads, not Google's sync client.

The school's stated 100 TB allocation helps central capacity; laptop space still limits offline caching. Shared Drives are streamed in Drive for desktop. Make recent practices or selected recordings available offline rather than pinning the entire archive. See [Google's guidance](https://support.google.com/drive/answer/13401938?hl=en).

No automatic cleanup is implemented. A local receipt, Drive-mounted file or successful home verification is not a retention authorization. Future cloud verification/retention must establish an independent durable copy before deletion.

## Qualification

Genuine synthetic Alpha 7 recordings qualify exact-byte copy, size/hash verification, transfer/import deduplication, restart receipts, interrupted copy, metadata publication failure, unavailable destination retry, conflict preservation, home corruption detection, opaque-byte exclusion, unsafe catalog paths, opt-in validation, worker/API behavior and spreadsheet-safe indexing.

Real Drive mounts, school/home account permissions, offline behavior, upload latency/order, actual AdvantageScope opening and DS resource contention remain unqualified. Complete a school-to-home round trip before relying on this as a second copy.

Validation: 14 focused tests passed; full configured-environment suite ran 537 tests (517 passed, 20 optional native/Windows skips). Browser verified the synthetic copied-log panel and explicit unconfirmed-cloud status. See [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md).
