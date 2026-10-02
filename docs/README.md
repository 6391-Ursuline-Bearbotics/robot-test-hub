# Full design · version 0.1

Design baseline: October 1, 2026, America/Chicago. Status: proposed system with a transfer prototype. The user's requested outcome is a system the team can set up before the season and use with minimal effort throughout physical robot testing.

## Intended experience

The robot records locally whenever its program runs. After it is disabled long enough, a collector automatically retrieves eligible data, saving progress when operation resumes. A notebook accepts immediate markers and delayed observations. The archive connects all evidence by robot/run identity and a validated clock mapping. Analyzers produce reproducible findings and comparisons with approved healthy baselines. An investigator can retrieve a run by local date/time, inspect its video and telemetry, record a diagnosis, and validate a repair or software change with another run.

## Document map

| Document | Read when implementing |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Processes, boundaries, storage, deployment, operating workflow |
| [CONTRACTS.md](CONTRACTS.md) | Source protocol, persistent entities, versioning, APIs, timestamps |
| [TRANSFER.md](TRANSFER.md) | Idle permission, checkpoint/recovery, prioritization, ETA, source load |
| [ROBOT_INTEGRATION.md](ROBOT_INTEGRATION.md) | Exact current logging behavior, rotation, instrumentation, alpha integration |
| [CONTEXT_AND_VIDEO.md](CONTEXT_AND_VIDEO.md) | Clock mapping, notes, run search, camera capture/alignment |
| [ANALYSIS.md](ANALYSIS.md) | Ingestion, health metrics, baselines, uncertainty, feedback/replay |
| [UI_AND_OPERATIONS.md](UI_AND_OPERATIONS.md) | Screens, operator actions, diagnostics, storage/backup, setup |
| [VALIDATION.md](VALIDATION.md) | Failure matrix, fixtures, bench trials, release evidence |
| [IMPLEMENTATION.md](IMPLEMENTATION.md) | Dependency-ordered tasks, acceptance criteria, model handoff |
| [DECISIONS.md](DECISIONS.md) | Accepted defaults, deferred decisions, hardware questions, source references |

## Requirements and traceability

| ID | Requirement | Main design / acceptance |
| --- | --- | --- |
| R01 | Collect automatically only during permitted idle time | TRANSFER; V01–V05 |
| R02 | Resume after enable, disconnect, or process crash without corrupting evidence | TRANSFER; V06–V11 |
| R03 | Show queue count/bytes and realistic idle-transfer ETA | UI_AND_OPERATIONS; V12 |
| R04 | Separate individual runs even across log boundaries | CONTRACTS; V13 |
| R05 | Retrieve evidence by real-world date/time | CONTEXT_AND_VIDEO; V14 |
| R06 | Save immediate and retrospective notes, including offline | CONTEXT_AND_VIDEO; V15 |
| R07 | Connect practice footage to robot events with measured alignment | CONTEXT_AND_VIDEO; V16 |
| R08 | Find repeatable subsystem outliers across comparable runs | ANALYSIS; V17–V18 |
| R09 | Tie evidence to exact software/configuration/hardware | ROBOT_INTEGRATION; V19 |
| R10 | Turn confirmed incidents into repeatable regression cases | ANALYSIS; V20 |
| R11 | Expose collection, logging, and analysis failures | UI_AND_OPERATIONS; V21 |
| R12 | Operate locally without internet; keep private data out of Git | ARCHITECTURE; V22 |

## Scope and limits

First useful release: durable idle-only collection, a time-indexed run catalog, a notebook, and an understandable transfer dashboard. The next releases add health reports and video. The full design covers both so their identifiers and clocks do not need incompatible redesign later.

The hub does not replace AdvantageKit, AdvantageScope, the driver station, the robot scheduler, or the existing simulation suite. It does not automatically modify motor settings, deploy code, enable the robot, diagnose a unique mechanical cause from ambiguous signals, or promise every second of telemetry survives abrupt power loss. It must report coverage gaps honestly.

No real robot, camera, archive server, or field network was exercised when creating this design. Numeric thresholds marked provisional are commissioning targets, not measured guarantees. The source adapter and receiver rotation require testing against the team's installed versions.
