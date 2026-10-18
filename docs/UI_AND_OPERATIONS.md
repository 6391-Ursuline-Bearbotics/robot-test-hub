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

**Measured findings (P2.2/P2.3, `run_metrics.py`).** After indexing, the pipeline streams each dataset once (one per worker tick) and stores per-run, per-component measurements in `run_metrics` under `run-metrics-1`; a new version adds rows rather than replacing them. The home page adds three kinds of sentence:

- *Matching parts:* swerve modules, Left/Right and Top/Bottom pairs. A part is flagged at 2× (1.5× for wheel speed error) the median of all matching parts plus an absolute floor. Average current is flagged in either direction, because a dead motor draws almost nothing. Supply current is stored but not compared, since it mirrors stator current.
- *Robot-wide rules of thumb* (stated in the sentence): battery below 7 V, 3 s or more below 9 V, any brownout, 25+ loops over 25 ms, CAN bus 95%+ busy. A rule that fires in 60% or more of a day's runs (and at least 3) appears once in a "Seen in most runs this day" box instead of on each card.
- *Unusual compared with recent runs:* the last 20 runs with the same robot, modes and a length within ½–2×. A value is flagged only in the worse direction, beyond 3.5 scaled MADs and the metric's absolute floor, and outside the quoted 25th–75th percentile range. At least 5 earlier runs are required, and runs under 10 s are not trended. A run less than half the usual length for its mode is flagged as possibly disabled early.

### Implemented: matches, crew and practice time (P1b 1–4, 8)

- **Matches** (`matches.py`): an auto run whose match clock starts at 10 s or more, paired with a teleop run in the same boot that starts within 15 s of when auto was *scheduled* to end and whose clock starts at 60 s or more. With FMS attached, the label uses the logged type, number and event (for example "Qualification 6 · MOST1"); otherwise it reads "Practice match". The home page groups the two runs under one header. All seven real 2026 matches in the shakedown were detected and labeled correctly, including one whose auto was disabled after 4 s. The match context comes from `run-metrics-2` and needs the DS match fields, which the 2026 profile maps; Alpha 7 aliases do not include them yet.
- **Roster** (`crew.py`, Transfers (admin) page): first names or nicknames, kept only in the local catalog (not in Git, the Drive log copies or summaries). Students can be hidden but not deleted, so old credit keeps its name.
- **Crew**: "Who's on the controls?" on the home page saves a driver/operator/coach selection that starts when saved and lasts until the next one. Each run card shows its crew. **Fix crew** under Details saves a correction as a new revision; the original selection and earlier corrections are kept.
- **Practice time** (`/practice-time`): enabled time, match time, runs, matches and last practiced per student and role, for this week or the season. Runs with nobody selected count as **Unassigned**.

### Implemented: season actions, match scores and leaderboards (P1b 5–7, 9–13)

- **Season file** (`robot_test_hub/seasons/<year>.json`, committed, no private data): actions with a signal and a detect mode (`counter`, `rising`, `enters` a value, or `threshold` with re-arm), timed tasks (from the first *from* action after the previous *to* until the next *to*, kept only within `min_seconds`–`max_seconds`), and an optional score estimate. Results are stored per run under the file's content hash (`season_results`), so editing a definition adds new results and keeps old ones.
- **2026 definition**: the 2026 robot had no event counters, so it uses logged goal states: *Intake* = `/RealOutputs/Intake/Goal` enters `INTAKE`, *Shoot* = `/RealOutputs/Indexer/Goal` enters `FEED` (one volley, not a game-piece count), *Cycle* = intake to volley within 2–30 s, and the score estimate is volleys. Checked against the raw log of one real qualification match: 1 auto volley and 6 teleop volleys, matching a count of the logged FEED transitions. This was not compared with video.
- **Match records** (`leaderboard.py`): crew, auto and teleop counts, cycles, average and best cycle, and the score estimate. The home page match header shows the score line and a **Don't count this match** button, which asks for a reason, is stored as a revision and can be undone.
- **Practice board** (`/board`, for a TV): the latest match result (with "New crew best!" or the crew's best so far), then boards by driver + operator crew: best match, most cycles, fastest average cycle (3+ cycles), most improved this week, and most consistent over the last 5 matches. It refreshes every 10 s. `?names=0` shows "Crew 1, 2…" instead of names and `?window=season` switches the window. Matches marked not counted are left out.

Not yet done: the robot-side `PracticeEvents.record(...)` counter helper (robot repository, under its own AGENTS.md) and the 2027 season file.

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
