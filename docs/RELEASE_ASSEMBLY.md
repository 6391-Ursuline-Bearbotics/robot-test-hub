# Reproducible full-source release and upgrade rehearsal

Status: design handoff only, October 15, 2026. No release assembler or cross-release rehearsal command exists yet. This is the next locally testable T18 packaging task from the software-readiness audit; the existing Windows setup and sign-in workflow remain the operator entry point.

## Intended operator result

Produce one versioned source ZIP with a verified inventory and a clear school setup entry point. It contains the complete committed source, Windows scripts, bundled sample and tools/status_bridge; it contains no Git metadata, virtual environment, private settings, real logs/video or credentials. The school extracts the complete release and follows WINDOWS_SETUP.md. Initial dependency installation still needs an approved index/cache and installed Python/WPILib. Do not silently download or redistribute a toolchain.

## Assembly contract

Build from an explicit locally resolved Git commit, defaulting to the current committed HEAD. Read committed blobs, never the working tree; preserve unrelated uncommitted work. Inventory every included relative path, size and SHA-256 plus the commit/tree identity and release format/tool version. Use deterministic ordering/ZIP metadata, omit volatile wall-clock data, and prove two assemblies of the same commit produce the same bytes. Include only repository source assets; preserve executable flags in the inventory where relevant. Validate Windows portability, case collisions, absolute/traversal paths and links before writing. Reject unsupported entries rather than following filesystem links. A sidecar digest/manifest is a local integrity receipt, not a cryptographic signature or proof of trusted authorship.

Publish the ZIP and manifest atomically to an explicit local output directory outside the source/archive; preserve existing conflicting outputs. No automatic GitHub release, cloud upload, endpoint connection, OS task registration, setup installation or source deletion is authorized as an assembly side effect.

## Offline verification and rehearsal

A verifier checks the complete archive inventory, duplicate/extra/missing entries, exact hashes/sizes and resource bounds before extracting into a newly owned temporary workspace. Reject traversal, linked entries and ambiguous Windows names. Run an extracted release's existing isolated sample exercise with an explicitly supplied installed Python environment; optional analytics must be requested and its pinned dependency present. No hidden pip/Java/native dependency installation or alternate interpreter fallback.

The upgrade/rollback rehearsal takes two explicit source releases and only disposable synthetic evidence. Exercise the older code against an owned synthetic archive, close it, create and verify the pre-upgrade backup, then exercise the newer code against that archive. Rollback restores the verified pre-upgrade snapshot into a fresh directory before reopening it with the older code; never assume older code can open a migrated database. Compare original bytes, note/review revisions, job/report references and source identities across the authorized boundaries. Record exact versions and distinguish source compatibility from database migration coverage. Include at least one actual supported schema transition in qualification; a same-schema version change does not prove migration rollback.

## Acceptance and boundaries

Use separate Sol implementation and independent adversarial review. Verify deterministic bytes, exact committed-state selection with a dirty checkout, complete live-source tooling, safe Windows extraction, tampering/resource failures and an extracted-release selftest outside the repository. A genuine cross-release rehearsal must prove both upgrade and clean-directory restore rollback, not only mock command arguments. Run relevant/full suites and check exact published CI. Preserve all real user archives/configuration and run no robot, camera, live transport or OS lifecycle actions.

This work improves release repeatability. School policy, actual sign-in/logoff/sleep behavior, real upgrade downtime, hardware load and independent backup durability still need commissioning. A Windows service or new cloud API is not required for the selected dedicated-user laptop workflow.
