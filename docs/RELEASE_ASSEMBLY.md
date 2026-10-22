# Reproducible source release and upgrade rehearsal

Implemented locally, October 15, 2026. This is the T18 packaging component; school installation, real operating-system lifecycle and physical commissioning remain separate. See [current validation](VALIDATION_CURRENT.md) for executed evidence.

## What to send to school

A release group contains source.zip and manifest.json in `robot-test-hub-<full-commit>`. The ZIP contains the complete committed source tree, Windows scripts, public sample and tools/status_bridge. It excludes Git metadata, environments, known private/generated paths and media except exact digest-locked public synthetic fixtures. This policy is not a general secret scanner: review source before committing it to the public repository.

Verify the group before forwarding both files through a trusted channel. The recipient extracts the complete source.zip into a new local folder and follows [WINDOWS_SETUP.md](WINDOWS_SETUP.md), then runs TestHub.cmd and TestHub.cmd -Analytics. Initial setup still needs installed Python, approved package access/cache and, for live NT, the separately installed WPILib tools. A wheel alone omits live source tooling.

The manifest records SHA-256 for the ZIP and each file, Git commit/tree identities, sizes, modes and format version. It detects changed bytes against that receipt. It is not a signature or proof of trusted authorship; someone replacing both files can replace the receipt too. Use a trusted source/channel. The release verifier validates structure without executing the released code; rehearsal explicitly executes the operator-selected trusted releases.

## Maintainer commands

From an existing hub installation with its dependencies available:

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.release build `
  --repo "C:\Source\robot-test-hub" --commit HEAD `
  --output "C:\HubReleases"
.\.venv\Scripts\python.exe -m robot_test_hub.release verify `
  --release "C:\HubReleases\robot-test-hub-<full-commit>"
.\.venv\Scripts\python.exe -m robot_test_hub.release extract `
  --release "C:\HubReleases\robot-test-hub-<full-commit>" `
  --destination "C:\SchoolApps\robot-test-hub-<full-commit>"
```

Replace placeholders with real paths and commit identities. The output parent must already exist. Build needs local Git objects and never fetches missing objects. Verification and extraction need no Git. Commands print bounded JSON and return nonzero on failure. Extraction requires a nonexistent destination; it never merges into an installed tree.

Build reads committed blobs rather than the working tree, so dirty edits remain untouched and uncommitted changes are absent. Ordering and ZIP metadata are deterministic. Rebuilding an identical verified group returns existing; an empty, partial or conflicting existing group is preserved and rejected. Source/output ancestry and recognized archive destinations are rejected. Keep release output separate from all private archives.

Publication reserves the final directory exclusively and publishes the ZIP before manifest.json. The manifest is the completion point; an interrupted group fails verification and remains for inspection. This is not a directory-wide atomic rename or a power-loss durability guarantee. Failed staging cleanup is reported. Failed extraction removes only its own unchanged file identities and empty owned directories; conflicts are retained rather than overwritten.

Verification checks exact manifest shape, hashes, sizes, inventory, CRCs, ZIP metadata and missing/extra/duplicate entries before extraction. It rejects links, traversal, absolute paths, Windows reserved/ambiguous names and case/file-directory collisions. Limits: 5,000 files, 8 MiB per file, 64 MiB total source, 96 MiB ZIP and 2 MiB manifest. Unsupported content fails rather than being silently omitted. These bounds cover this source project, not arbitrary repositories.

## Offline upgrade and rollback exercise

Build two explicit local commits into separate verified release groups, then run:

```powershell
.\.venv\Scripts\python.exe -m robot_test_hub.release_rehearsal `
  --older "C:\HubReleases\robot-test-hub-<older-full-commit>" `
  --newer "C:\HubReleases\robot-test-hub-<newer-full-commit>" `
  --python "C:\Source\robot-test-hub\.venv\Scripts\python.exe" `
  --analytics --json
```

This accepts no active archive, live profile or endpoint. It verifies/extracts both releases into a fresh temporary workspace, then uses the explicit installed interpreter with -I. Imports are checked against the selected extracted source. No pip/Java/native installation or interpreter fallback occurs. Analytics is optional; when requested, its pinned installed dependency must actually work.

The older code imports the controller's bundled public synthetic WPILOG, generates two reports and saves invented note/review records, then closes the archive and creates a verified pre-upgrade backup. The newer code opens/migrates that disposable archive and checks exact original, note, review, job and report receipts before adding new revisions and an explicitly unsupported job. Rollback restores the pre-upgrade backup into a fresh directory before the older code reopens it. The newer isolated sample exercise runs afterward when available. Older releases without that exercise expose unavailable status; requested analytics cannot be silently skipped.

Reports distinguish a version change from an actual schema migration and include exact commits, before/after/restored schema, stage outcomes and cleanup. A failed stage blocks dependent stages; incomplete cleanup cannot return success. Child deadlines/output are bounded and owned processes are reaped. After closing parent streams, the controller removes only its two disposable output files and confirms inherited handle release before dropping workspace ownership. Windows sharing violations receive one shared five-second cleanup allowance; other errors fail immediately, and incomplete cleanup retains ownership. Parsed receipts are read before output removal; originals and other workspace files are not part of this cleanup. Network/child-process guards cover the known exercise path; this is not an operating-system sandbox for untrusted Python source.

The supported qualification pair is c1b3261e3d48ad5cb029cca939ebeaf88cc4f4d0 (schema 7) to f9bc2fca3b082b5f997af75709444ef6bbf509c1 (schema 8). An additional actual packaged rehearsal qualified 2a71e1ed7158280363a2ad43c249df50d3094f39 (schema 8) to bba6f6256cd29796e4e11dc246f6649e8d1da8e1 (schema 9), with analytics and all six stages passing, including restore to schema 8 and successful cleanup.  A subsequent packaged rehearsal qualified bba6f6256cd29796e4e11dc246f6649e8d1da8e1 (schema 9) to 296241467f7c7b26e3d413a8faa3ab2c7aff9293 (schema 10): all six stages passed with analytics, verified restore/reopening at schema 9 and successful cleanup. An additional packaged rehearsal qualified 72f02b2a6d48f2530458d1f2cadb148af8a9a7f2 (schema 10) to 546fea78286c26e155457813542527d6b3f69408 (schema 11): all six stages passed with analytics, exact evidence receipts, verified restore/reopening at schema 10 and successful cleanup. This is disposable synthetic software qualification, not school commissioning. Historical integration tests explicitly skip when those local Git objects are absent; child-boundary/adversarial cases still run. Arbitrary historical releases may have incompatible APIs and fail honestly. School sign-in/sleep/logoff, live upgrade downtime, real backup media and physical robot load still require the [commissioning checklist](LOG_FLOW_COMMISSIONING.md). Stop the real hub and take a verified backup before any actual upgrade; changing source folders alone does not undo a migration.

An additional packaged rehearsal qualified 19fce2c8b6ec30b964da98a22863af9164fe01e0 (schema 11) to e6cbf4212429e616679e413768ba70b42f4e69aa (schema 12). All six stages passed with analytics, exact original/note/review/report receipts, verified restore and old-release reopening at schema 11, and successful cleanup. The verified new package contains 384 files; tree df268d2c6cbb2febd60f8da0e1ecd83d921b240a and source ZIP SHA-256 e1b733cad986c739d454d8e632b39ad7d427729e3d3b2ac1f94041b072b63603. Extracted new code additionally migrated an actual old-release canceled stream with a committed chunk, preserved generation-1 evidence, completed fresh generation 2, and verified both schema-12 restore and schema-11 pre-upgrade rollback. The frozen old fixture stayed exact; disposable copies were removed. This is synthetic software qualification, not school commissioning or a real backup failure-domain test.
