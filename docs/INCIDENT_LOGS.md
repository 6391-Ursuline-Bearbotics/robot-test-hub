# Download original robot logs for an incident

The local incident view can retrieve the original WPILOG files referenced by a preserved clip. The user explicitly approved this loopback-only download feature. It does not upload logs or footage elsewhere, connect to the robot, start a camera, rewrite logs or assign a driver note to a run.

## Operator workflow

Preserve an inspected interval or saved-context candidate using [incident media](VIDEO_MEDIA.md). On a ready clip, choose **Find original robot logs**. This action loads references on demand; polling does not hash or download original robot logs.

For a saved run, the list identifies that exact run's import-job references in its pinned catalog. For a saved note, it identifies conditional references from the same robot boot; those are candidates and can contain multiple runs. An explicit interval currently has no automatic log references. Missing references stay unavailable.

Inspect the pinned SHA-256, size and source type. A **download candidate** means its catalog identity is eligible for verification; the original bytes have not already been checked by this listing. Click the local download link to verify and retrieve the whole immutable original. A file can span other runs and include data outside the incident. It is not cropped or renamed into a fabricated run-only log.

Keep the downloaded MP4 and timing sidecar with the chosen WPILOG. Open that original alone in AdvantageScope, avoiding merged-log timestamp offsets, and use the [manual cue instructions](VIDEO_MEDIA.md#advantagescope-manual-alignment). The hub does not automatically open or synchronize AdvantageScope.

## Identity and compatibility

References come only from the clip's hash-verified immutable sidecar, never from a newer catalog or a caller-supplied path. The current import row must match its pinned hash, size and source, canonical raw path, qualified Alpha 7 profile/extractor/mapping and successful valid import. Checksum-mismatched, unsupported, unknown-source or invalid references are unavailable. A valid file with an incomplete run remains a whole original; incomplete run coverage does not become a health pass.

New sidecars have `original_logs.projection_version: 2` and preserve `MANUAL_LOCAL`, `SYNTHETIC` and `VERIFIED_TRANSFER` sources. Version-absent historical sidecars reconstruct their original projection on recovery, retaining the exact original sidecar bytes and digest. Historical manual imports projected as `other` remain unknown and unavailable for download. A deliberately new preservation request can record the actual manual source label; the old incident is not silently upgraded. Malformed or unknown projection versions fail explicitly.

The sidecar's `download_available: false` and `original_bytes_reverified_for_export: false` describe its immutable preservation-time snapshot. Current download eligibility is a separate API result. Neither is retroactively rewritten after a successful download.

## Local HTTP contract

`GET /api/v1/video/media/{item_id}/logs` returns schema version 1, `item_id`, the pinned reference `basis`, `qualification: pinned_catalog_references` and at most 100 projected references. Each has `import_job_id`, `sha256`, `size_bytes`, `source_type`, `format_valid`, `state` (`download_candidate` or `unavailable`), `error_code` and `original_bytes_reverified_for_export: false`. No archive filename, private path or source diagnostic is returned.

`GET`/`HEAD /api/v1/video/media/{item_id}/logs/{import_job_id}` verifies and serves that referenced original, supporting one normal, open-ended or suffix byte range. Response type is `application/octet-stream` with an attachment filename based only on the original SHA-256 and `.wpilog`. Reads are at most 64 KiB. Unknown/unreferenced identities, query fields, malformed/repeated ranges, stopped service, identity conflicts and unavailable imports fail explicitly. After headers start, interruption closes the response rather than appending an error document to log bytes.

The resolver admits two concurrent original-log operations and rejects further requests with a safe busy error. It accepts originals up to 16 GiB, checks a 60-second verification deadline and a 300-second stream deadline between filesystem operations, and checks service cancellation during hashing/streaming. An OS filesystem call can itself block; these deadlines are not a guarantee that the OS can be interrupted. Verification and streaming do not hold collector/status locks.

Raw bytes are hashed on the exact opened regular descriptor, with size/hash, before/after/current path identity and catalog rechecks. Canonical paths are `raw/{sha_prefix}/{sha}.wpilog`; symlinks, Windows junction/reparse hazards, path substitution and changed bytes are rejected. Streaming rechecks descriptor/path metadata. This does not prove protection against an external in-place write that evades metadata changes during streaming; service-owned originals remain immutable.

The service owns each descriptor operation through hashing, streaming and close retries. Shutdown refuses new operations, joins these lifetime guards and retains archive ownership while cleanup is pending. Another hub cannot take the same archive while an original descriptor remains outstanding. Source deletion remains disabled.

## Qualification

Two Sol authors implemented backend/browser work and an independent Sol reviewer accepted it. Twelve author resolver/HTTP tests, nine independent tests and an additional media compatibility regression cover pins, source/format eligibility, versioned manual provenance, unchanged legacy recovery, same-size tampering, path conflicts, actual Range/HEAD, stop behavior, deadline/truncation and descriptor cleanup ownership.

Two opt-in native integration tests use the actual importer and public synthetic Alpha 7 WPILOG with generated encoded footage. They verify both `SYNTHETIC` and `MANUAL_LOCAL` import workflows: saved-run preservation, exact whole-log HTTP download, SHA/HEAD/range, independent decoding of all seven recorded cycles, newer boot/catalog isolation, unchanged sidecars and recovery with camera/media generation disabled. No real robot or camera is contacted.

Browser qualification inspects the explicit lookup action, exact saved-run references and conditional saved-note references, safe local link, displayed source/hash/size and offline clearing. The source is the public 4530-byte synthetic fixture, never private telemetry. Native/HTTP tests verify actual downloaded bytes; the browser check does not claim a completed browser file save or actual AdvantageScope application use.

The final native-enabled Windows suite with pinned Python 3.10.7 ran **446 tests in 84.766 seconds: 444 passed, two symbolic-link privilege skips**. Physical source transfer, camera timing, season-scale load and installed AdvantageScope use remain separate qualification work. See [VALIDATION_CURRENT.md](VALIDATION_CURRENT.md) for the broader checkpoint.
