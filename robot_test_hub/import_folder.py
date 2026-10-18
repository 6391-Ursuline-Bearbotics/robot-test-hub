"""Import every WPILOG under a folder (USB drive, Drive-for-desktop folder, download).

This is the fallback path that needs no robot connection, receiver, manifest, SFTP
or NetworkTables status: originals are archived unchanged, imported under the
explicitly selected profile, and the normal run index and automatic checks run.
Identity is the file's SHA-256, so re-running over the same folder is idempotent.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .importer import ArchiveConflict, Importer, install_schema
from .pipeline import Pipeline
from .storage import DataRootOwner, open_catalog
from .wpilog import PROFILE

SUFFIX = ".wpilog"
MAX_PIPELINE_TICKS = 1000
OK_STATES = ("succeeded", "succeeded_with_unsupported")


def find_logs(folder: Path):
    """Sorted WPILOG paths under folder, skipping empty files and links."""
    folder = Path(folder)
    if folder.is_file():
        candidates = [folder]
    elif folder.is_dir():
        candidates = sorted(path for path in folder.rglob("*") if path.suffix.lower() == SUFFIX)
    else:
        raise FileNotFoundError(f"Folder not found: {folder}")
    return [path for path in candidates if path.is_file() and not path.is_symlink() and path.stat().st_size > 0]


def import_paths(root: Path, db, paths, *, profile=PROFILE, source_type="MANUAL_LOCAL", progress=None):
    """Import each path, continuing past failures. Returns one result per path."""
    importer = Importer(root, db)
    results = []
    for index, path in enumerate(paths):
        try:
            job = importer.import_file(path, profile=profile, source_type=source_type)
            result = {"file": str(path), "state": job["state"], "job_id": job["id"],
                      "error_code": job.get("error_code"), "error": job.get("error")}
        except (OSError, ValueError, ArchiveConflict) as error:
            result = {"file": str(path), "state": "failed", "job_id": None,
                      "error_code": type(error).__name__, "error": str(error)}
        results.append(result)
        if progress is not None:
            progress(index + 1, len(paths), result)
    return results


def process(root: Path, db, *, max_ticks=MAX_PIPELINE_TICKS):
    """Run the normal indexing/report pipeline until it reports idle."""
    pipeline = Pipeline(root, db)
    summary = {}
    try:
        for _ in range(max_ticks):
            result = pipeline.tick()
            if result.get("measured_runs"):
                summary["measured_runs"] = summary.get("measured_runs", 0) + result["measured_runs"]
            elif result["state"] == "idle":
                return dict(summary, state="idle")
            if result["state"] == "indexed":
                summary.update(runs=result["runs"], reports=result["reports"], revision=result["revision"])
        return dict(summary, state="incomplete")
    finally:
        pipeline.close()


def data_dir_from_launch_profile(path):
    """Resolve the hub's private data directory the same way StartHub.cmd does."""
    from .config import Config
    from .launcher import load_profile
    profile = load_profile(path)
    return Path(Config.load(profile["hub_config"], resolve_paths=True).data_dir)


def summarize(results):
    counts = {}
    for result in results:
        key = "imported" if result["state"] in OK_STATES else result["state"]
        counts[key] = counts.get(key, 0) + 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder (searched recursively) or a single .wpilog file")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--data-dir", type=Path)
    target.add_argument("--launch-profile", type=Path, help="Private launch.json used by StartHub.cmd")
    parser.add_argument("--profile", default=PROFILE,
                        help=f"Exact library/schema profile of these logs (default {PROFILE}); never inferred")
    parser.add_argument("--synthetic", action="store_true", help="Assert these are invented test logs (provenance label)")
    parser.add_argument("--no-process", action="store_true", help="Archive and import only; skip run indexing and checks")
    parser.add_argument("--json", action="store_true", help="Print one JSON document instead of progress lines")
    args = parser.parse_args(argv)
    paths = find_logs(args.folder)
    data_dir = args.data_dir or data_dir_from_launch_profile(args.launch_profile)

    def progress(done, total, result):
        if not args.json:
            note = "" if result["state"] in OK_STATES else f"  ({result['error_code']}: {result['error']})"
            print(f"[{done}/{total}] {result['state']:<26} {Path(result['file']).name}{note}", flush=True)

    owner = DataRootOwner(data_dir)
    try:
        db = open_catalog(owner.root)
        try:
            with db:
                install_schema(db)
            if not args.json:
                print(f"Found {len(paths)} log file(s) under {args.folder}", flush=True)
            results = import_paths(owner.root, db, paths, profile=args.profile,
                                   source_type="SYNTHETIC" if args.synthetic else "MANUAL_LOCAL", progress=progress)
            processed = None if args.no_process else process(owner.root, db)
        finally:
            db.close()
    finally:
        owner.close()
    counts = summarize(results)
    if args.json:
        print(json.dumps({"files": results, "counts": counts, "processing": processed}, sort_keys=True))
    else:
        print("Summary: " + ", ".join(f"{count} {state}" for state, count in sorted(counts.items())) if counts else "Summary: no logs found")
        if processed is not None:
            print(f"Indexed {processed.get('runs', 0)} run(s); {processed.get('reports', 0)} report(s) generated. "
                  "Start the hub and open it to see results.")
    return 0 if paths and all(r["state"] in OK_STATES for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
