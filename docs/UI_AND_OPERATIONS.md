# User interface and operations

## Product priorities

Drivers need a glanceable answer to whether collection/recording is working and how much idle time remains. Investigators need evidence by time/run and comparisons. Keep credentials, schema IDs, and internal paths in setup/diagnostics rather than normal practice flows. An operator should not need to understand SFTP or a database to use the system.

## Screen 1: Practice overview

```text
6391 Practice · [session selector]       Robot: disabled · status age 0.1 s
Logging: healthy   Video: recording   Clock: aligned ±80 ms

COLLECTING                         [Pause collection]
About 3 min idle transfer time remaining
3.2 GB / 7 closed files            17 MB/s recent rate
Currently recording: 180 MB        Last archive: 6:52:14 PM

[MARK EVENT] [10 seconds ago] [30 seconds ago] [Add note / exact time]

Recent run 24 · 6:49–6:52 PM · Teleop · Battery B07
Archive complete · analysis pending · video available

Download queue                    [Prioritize selected] [Retry failed]
Run 24  620/900 MB  Downloading
Run 23  0/480 MB    Queued
...
```

On enable, display `Paused — robot enabled; progress saved`. Retain the last estimate as historical with its basis. Source status stale is a different label from robot enabled. Incomplete discovery, unknown speed, unavailable files, open files, and blocked disk/auth errors all need distinct presentations. See TRANSFER for ETA rules.

Keep the event action reachable by keyboard and touch. The user can leave text blank initially. Show where a note is saved/acknowledged. Collection pause does not disable logging, notes, or recording. Do not add a dashboard control that enables the robot.

### Implemented: "Today's practice" home page (October 18, 2026)

`/` is now the student-facing home page; the transfer dashboard above moved to `/admin`. The page shows:

- a one-line strip in plain words ("Logs: caught up", "Logs: on hold: robot enabled", import progress);
- **Mark event**, **10 s ago** and **30 s ago** buttons. These hand the click time to the notebook (`/notebook#mark=<seconds>&at=<epoch ms>`), so notes keep using the notebook's device-first durable save path. A handoff older than 2 minutes is refused rather than saved with a wrong time;
- one card per run for the selected local day (default: most recent day with runs; arrows step between practice days). Each card shows start time, length, modes, battery low point when recorded, note count, a text chip (*Look at this* / *Nothing flagged* / *Couldn't check*) and one plain sentence for the most notable finding. **Details** lists every finding sentence, the limits wording, and links to `/runs#run=<id>`.

Chip rules (`robot_test_hub/practice.py`): any finding or sibling outlier → *Look at this*; otherwise at least one check evaluated with no finding → *Nothing flagged*, with the count of checks that could not run; otherwise *Couldn't check*. Missing data never becomes *Nothing flagged*.

**Sibling comparison (first part of P2).** For per-module metrics (steering settling time, steering error, oscillation crossings, drive speed error, longest drive error), a module is flagged when its value is at least 2× the median of the other modules and above an absolute floor per metric. It is labeled a descriptive comparison within the run, not a health result, and needs no approved baseline.

Runs without a wall-clock time are counted below the cards and remain available in Run history.

## Screen 2: Run/time search

Filters: robot, session, local date/time interval, test type, build/config, component, battery, finding status. Show the resolved absolute date and timezone for natural-language or relative dates. Return overlapping intervals, including uncertainty, instead of matching only run start time.

Run card: local start/end, mode/phase, build summary, battery, source completeness, video coverage, notes, highest-priority findings. Unknown wall-clock data belongs in a clearly separated list rather than disappearing. Show both run ID and friendly run number; numbers alone are not globally unique.

## Screen 3: Investigation

One timeline links robot state, annotations, findings, and video. Selecting an event centers a configurable context window. Graphs retain units, source names, freshness indicators, and comparison cohort. Include a coverage strip for recording gaps and uncertain alignment.

Provide `Open log location`, `Export investigation bundle`, `Open clip`, and AdvantageScope setup instructions. Verify actual platform launch integration before promising a one-click seek/open. A bundle contains source hashes, note/mapping revisions, report configuration, and references or selected artifacts; it is private unless explicitly exported for sharing.

Allow reviewer disposition and a link to a maintenance event, commit, or follow-up test. Comparing before/after requires compatible cohorts; explain mismatches instead of silently treating all changes as regressions.

## Screen 4: Hardware and trends

Component identity and location history, battery history, approved baselines, metric trends, maintenance events. Trend points show eligible duration and number of runs. Missing data is a gap, not zero. Separate whole-robot/common-mode degradation from a single-module outlier.

## Screen 5: Setup and diagnostics

Robot/profile, allowed log roots, status channel, transport, credential reference, host fingerprint, idle/read limits, archive location, timezone, recorder, retention/backup. Provide read-only connection checks without enabling the robot or initiating large downloads unexpectedly.

Diagnostics shows worker liveness, last successful discovery, source status age, logger health, source/local space, download retry reason, blocked bytes, import failures, analysis availability, recorder gaps, backup lag, and service version. Offer a redacted diagnostic export. Credentials never appear in it.

## Behavior and accessibility

- Text labels accompany color; status is not color-only.
- Keyboard focus visible; controls have descriptive labels; graph alternatives expose numeric evidence.
- Update queue/ETA about once per second; do not announce every byte update through screen readers.
- Escape imported filenames, notes, and errors; no raw HTML from log data.
- Connection loss freezes last-known values with an explicit stale label.
- Demo/source identity is always visible; fake data cannot be mistaken for a robot session.
- A local HTTP UI is sufficient first. Authenticated LAN notebook access is a separate milestone.

## Storage and backup policy

Default: no robot-side deletion and no automatic removal of raw recordings. Monitor source and local free space early enough to act. Measure daily data rates before choosing thresholds and retention periods.

Production cleanup requires explicit policy, source identity, local verified artifact, verified second copy in another failure domain, and a minimum age. Never delete active segments, unverified files, pinned incidents, or the sole remaining copy. Deletion is separately audited and retried idempotently; it must not be a hidden side effect of normal downloads.

Video retention can be shorter than raw telemetry, with pinned run/incident clips exempt. Derivatives can be regenerated; retain source hashes and software versions. Backup catalogs, annotations, mappings, configurations, and review history along with files. Practice a restore into a clean directory before relying on backups.

## Operating checklist

Before practice: correct robot/profile, logging healthy, recorder active if expected, clock status plausible, battery identity, available storage. These are visible status checks, not repeated approval dialogs.

During practice: drive normally, mark observations, let collection pause/resume. Operator can pause collection manually when troubleshooting.

After practice: keep the robot powered and disabled while the displayed queue drains; verify `closed files caught up` and handle the final open segment through normal session close/rotation. Then confirm indexing/report and backup status separately. The hub must not imply the robot can be powered down with uncollected bytes simply because analysis completed on earlier files.

Recovery: source unplugged -> reconnect; disk full -> free/move space through explicit maintenance; worker failure -> restart with same data root; integrity error -> inspect/retry; unknown clock -> retain evidence and add alignment anchors later. Every case should have a concise actionable message.
