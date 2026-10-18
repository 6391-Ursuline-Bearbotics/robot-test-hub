"""List (and optionally delete) leftover scratch workspaces under the repository's data/ folder.

Only names matching known manual-check patterns are touched; native tool caches that
tests use (data/native-*) and anything else are kept. Dry run by default:

    python tools/clean_workspace.py            # show what would be removed
    python tools/clean_workspace.py --delete   # remove those folders/files
"""
import argparse
import fnmatch
from pathlib import Path
import shutil

PATTERNS = ('browser-*', '*-review', 'review-demo', 'live-ui-check', 'marker-browser-check*', 'video-ui-review',
            'video-ui-stop-*', '*-full-suite.txt', '*-ci*.txt', '*-browser-info.json', '*-ui-checks-result.txt')
KEEP = ('native-*', 'demo')


def size(path):
    return path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob('*') if p.is_file())


def candidates(data):
    for path in sorted(Path(data).iterdir()):
        if any(fnmatch.fnmatch(path.name, k) for k in KEEP):
            continue
        if any(fnmatch.fnmatch(path.name, p) for p in PATTERNS) and not path.is_symlink():
            yield path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parent.parent / 'data')
    parser.add_argument('--delete', action='store_true')
    args = parser.parse_args(argv)
    if not args.data.is_dir():
        print('No data folder; nothing to clean.')
        return 0
    total = 0
    for path in candidates(args.data):
        bytes_ = size(path)
        total += bytes_
        print(f"{'deleting' if args.delete else 'would delete'} {path.name} ({bytes_ / 1e6:.1f} MB)")
        if args.delete:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    print(f"{'Freed' if args.delete else 'Would free'} {total / 1e6:.0f} MB. Kept native tool caches and other files.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
