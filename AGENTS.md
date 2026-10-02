# Robot Test Hub implementation instructions

## Purpose and source of truth

This repository implements the Team 6391 testing hub. Read `docs/README.md`, `docs/DECISIONS.md`, and the specific task in `docs/IMPLEMENTATION.md` before changing code. The design describes the target system; README and the implementation gap table describe what exists. Do not describe planned features as implemented.

Use small tasks with explicit acceptance criteria. Take the next dependency-ready task when asked to continue generally. Update its status, validation evidence, and any changed decisions before finishing. Ask only about genuinely blocking product/hardware choices; use documented defaults for ordinary implementation choices. No model or parallel-agent requirement is imposed by these instructions.

## Invariants

- No bulk source reads without fresh, explicitly disabled status and an elapsed idle delay. Unknown/stale/disconnected means pause. Do not gate robot enable on collector completion.
- Preserve raw files and metadata. Identity is not a filename. A closed segment is immutable. Never append bytes after an identity mismatch.
- Flush durable file bytes before committing the resume offset. Keep partial/verified/indexed/backed-up states distinct.
- Source deletion is disabled until the separate retention milestone is implemented and explicitly configured.
- Persist notes locally before attempting delivery to the robot. Event time and submission time are distinct.
- Preserve timestamp units, coordinate frames, schema versions, source provenance, and uncertainty. Missing or invalid data is not zero and not a health pass.
- Keep code and data separate. Do not commit real logs, video, private observations, or secrets to this public repository.
- Never add a fake successful real adapter. Demo, simulated, historical, and real sources must be visibly distinguishable.

## Implementation

Python 3.10 is the initial compatibility floor. The current prototype uses the standard library and a small static browser UI. Add dependencies only for a concrete task, pin/test the chosen versions, and document installation. Do not require Node or cloud access just to run the local collector.

Robot-side changes belong in the robot repository and require its own AGENTS.md instructions. The current robot uses WPILib 2027 Alpha 7, Java 25, Commands v3, and AdvantageKit 27.0.0-alpha-6. Preserve scheduler threading and REAL/SIM/REPLAY IO boundaries. This repo does not authorize deployment or autonomous robot motion.

Do not restart AdvantageKit to rotate files. Implement rotation only under the receiver contract and tests in `docs/ROBOT_INTEGRATION.md`. Verify alpha APIs against exact installed sources rather than stable documentation snippets.

## Validation and delivery

Run `python -m unittest discover -s tests -v` after collector changes. Add behavior tests for failure recovery, timing, unit conversion, idempotency, and source gating. Use a monotonic fake clock rather than long sleeps. UI changes need a browser check; hardware claims need measurements on hardware. Report these separately.

Inspect git status and preserve other work. Avoid unrelated refactors. Never publish reports, send team notifications, or open issues automatically as a side effect of analysis; those integrations require a configured workflow. Human review remains required before code deployment, tuning application, or physical repairs.
