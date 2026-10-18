""""Collect now" (P0.2): pull finished logs over SFTP after practice, no robot-side rotation.

The operator presses Collect now while the robot is disabled. The hub lists the
robot's log folder, downloads every .wpilog except the newest one (which the robot
program may still be writing), and imports them like any folder. The newest file
is included only when the operator confirms the robot program is stopped. A file
whose size changes during download is treated as still being written and is not
imported. Files already pulled (same name and size) are skipped. Downloads go to a
private staging folder through ".part" names; originals on the robot are never
changed or deleted.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time

NAME = re.compile(r'[A-Za-z0-9._-]{1,200}\.wpilog')
CHUNK = 256 * 1024


class PullError(RuntimeError):
    pass


def install_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS pulled_files (
        remote_name TEXT NOT NULL, size_bytes INTEGER NOT NULL, mtime INTEGER NOT NULL, sha256 TEXT NOT NULL,
        pulled_utc_ns INTEGER NOT NULL, PRIMARY KEY (remote_name, size_bytes))""")


def listing(sftp, root):
    """Flat regular .wpilog files in the remote folder: [(name, size, mtime)] oldest first."""
    items = []
    for entry in sftp.listdir_attr(root):
        name = entry.filename
        if not NAME.fullmatch(name) or not stat.S_ISREG(entry.st_mode or 0):
            continue
        items.append((name, int(entry.st_size), int(entry.st_mtime or 0)))
    return sorted(items, key=lambda item: (item[2], item[0]))


def plan(items, *, include_newest, already):
    """(to download, held back newest name or None, skipped already pulled)."""
    held = None
    if items and not include_newest:
        held = items[-1][0]
        items = items[:-1]
    todo = [i for i in items if (i[0], i[1]) not in already]
    skipped = [i[0] for i in items if (i[0], i[1]) in already]
    return todo, held, skipped


def _download(sftp, root, name, size, staging, progress):
    remote = str(PurePosixPath(root) / name)
    part = staging / (name + '.part')
    digest = hashlib.sha256()
    with sftp.open(remote, 'rb') as source, open(part, 'wb') as target:
        copied = 0
        while True:
            block = source.read(CHUNK)
            if not block:
                break
            target.write(block)
            digest.update(block)
            copied += len(block)
            if progress:
                progress(name, copied, size)
        target.flush()
        os.fsync(target.fileno())
    after = sftp.stat(remote)
    if copied != size or int(after.st_size) != size:
        part.unlink(missing_ok=True)
        return None, 'still_being_written'
    final = staging / name
    if final.exists():
        final.unlink()
    part.replace(final)
    return (final, digest.hexdigest()), None


def connect(config):
    """Open (client, sftp) with the pinned host key from the source configuration."""
    import paramiko
    client = paramiko.SSHClient()
    client.load_host_keys(config.known_hosts)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(config.host, port=config.port, username=config.username, key_filename=config.private_key,
                   timeout=max(config.timeout, 2.0), banner_timeout=5, auth_timeout=5,
                   allow_agent=False, look_for_keys=False)
    return client, client.open_sftp()


def collect(root, db, config, *, include_newest=False, profile=None, connect_fn=connect, progress=None):
    """Pull and import. Returns a summary dict; raises PullError when the robot is unreachable."""
    from .import_folder import import_paths, summarize
    from .wpilog import PROFILE
    root = Path(root)
    staging = root / 'pull-staging'
    staging.mkdir(parents=True, exist_ok=True)
    with db:
        install_schema(db)
    already = {(r[0], r[1]) for r in db.execute('SELECT remote_name, size_bytes FROM pulled_files')}
    try:
        client, sftp = connect_fn(config)
    except Exception as exc:
        raise PullError('Could not connect to the robot over SFTP; check that it is on and on this network') from exc
    downloaded, problems = [], []
    try:
        with closing(sftp):
            items = listing(sftp, config.log_root)
            todo, held, skipped = plan(items, include_newest=include_newest, already=already)
            for name, size, mtime in todo:
                result, problem = _download(sftp, config.log_root, name, size, staging, progress)
                if problem:
                    problems.append({'file': name, 'reason': problem})
                    continue
                downloaded.append((name, size, mtime, result[0], result[1]))
    finally:
        try:
            client.close()
        except Exception:
            pass
    results = import_paths(root, db, [d[3] for d in downloaded], profile=profile or PROFILE, source_type='MANUAL_LOCAL')
    now = time.time_ns()
    with db:
        for (name, size, mtime, path, sha), result in zip(downloaded, results):
            if result['state'] in ('succeeded', 'succeeded_with_unsupported', 'unsupported'):
                # Unsupported logs are archived unchanged too; they are not downloaded again.
                db.execute('INSERT OR REPLACE INTO pulled_files VALUES (?,?,?,?,?)', (name, size, mtime, sha, now))
                path.unlink(missing_ok=True)
    return {'listed': len(items), 'downloaded': len(downloaded), 'already_pulled': len(skipped),
            'held_back_newest': held, 'problems': problems, 'imports': summarize(results),
            'files': [{'file': r['file'].rsplit(os.sep, 1)[-1], 'state': r['state']} for r in results]}
