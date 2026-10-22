"""Permission-gated manual SFTP snapshots, never proof of closed segments.

Older/non-newest is only a selection heuristic. Each attempt has unique retained
bytes and append-only source-scoped receipts; the immutable collector is separate.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time
import uuid

NAME = re.compile(r'[A-Za-z0-9._-]{1,200}\.wpilog')
CHUNK = 256 * 1024
MAX_LISTED_ENTRIES = 4096

class PullError(RuntimeError):
    pass

class PermissionLost(PullError):
    pass

def install_schema(db):
    """Legacy schema retained for readers; new captures never write this table."""
    db.execute("""CREATE TABLE IF NOT EXISTS pulled_files (
        remote_name TEXT NOT NULL, size_bytes INTEGER NOT NULL, mtime INTEGER NOT NULL, sha256 TEXT NOT NULL,
        pulled_utc_ns INTEGER NOT NULL, PRIMARY KEY (remote_name, size_bytes))""")

def _check(permission):
    try:
        result = permission.check()
        if result is False:
            raise ValueError('permission denied')
    except Exception as exc:
        raise PermissionLost('Fresh disabled permission was revoked') from exc

def _metadata(entry):
    if not stat.S_ISREG(entry.st_mode or 0):
        raise PullError('source_not_regular')
    size = entry.st_size
    if type(size) is not int or size < 0:
        raise PullError('invalid_source_size')
    if type(entry.st_mtime) not in (int,float) or not math.isfinite(entry.st_mtime):
        raise PullError('invalid_source_mtime')
    return {'size_bytes': size, 'mtime': entry.st_mtime}

def listing(sftp, root, permission, call):
    _check(permission)
    if hasattr(sftp, 'listdir_iter'):
        entries = iter(sftp.listdir_iter(root, read_aheads=1))
    else:
        entries = iter(call(sftp.listdir_attr, root))
    _check(permission)
    items = []
    count = 0
    while True:
        sentinel = object()
        entry = call(lambda: next(entries, sentinel))
        if entry is sentinel:
            break
        count += 1
        if count > MAX_LISTED_ENTRIES:
            raise PullError('listing_entry_limit; listing is incomplete')
        if NAME.fullmatch(entry.filename) and stat.S_ISREG(entry.st_mode or 0):
            meta = _metadata(entry)
            items.append((entry.filename, meta['size_bytes'], meta['mtime']))
    return sorted(items, key=lambda item: (item[2], item[0]))

def plan(items, *, include_newest, already=None):
    """No filename/size identity guesses; every selected snapshot is recaptured."""
    held = items[-1][0] if items and not include_newest else None
    return (items[:-1] if held else items), held, []

class _Session:
    """Own cancellation watcher and all transport cleanup through actual close."""
    def __init__(self, permission, timeout):
        self.permission = permission
        self.timeout = timeout
        self.deadline = None
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.resources = []
        self.closed = set()
        self.error = None
        self.timed_out = False
        self.revoked = False
        self.thread = threading.Thread(target=self._watch, name='hub-pull-permission')
    def check(self):
        _check(self.permission)
        if self.timed_out:
            raise PullError('source_io_timeout')
    def register(self, resource):
        with self.lock:
            self.resources.append(resource)
        if self.revoked or self.timed_out:
            self.close()
    def close(self):
        with self.close_lock:
            with self.lock:
                resources = list(self.resources)
            for resource in resources:
                if id(resource) in self.closed:
                    continue
                try:
                    resource.close()
                    self.closed.add(id(resource))
                except Exception as exc:
                    self.error = exc
            return len(self.closed) == len(resources)
    def call(self, function, *args):
        _check(self.permission)
        if self.timed_out:
            raise PullError('source_io_timeout')
        self.deadline = time.monotonic() + self.timeout
        try:
            result = function(*args)
            _check(self.permission)
            if self.timed_out:
                raise PullError('source_io_timeout')
            return result
        except Exception:
            _check(self.permission)
            if self.timed_out:
                raise PullError('source_io_timeout') from None
            raise
        finally:
            self.deadline = None
    def _watch(self):
        while not self.done.wait(.02):
            try:
                _check(self.permission)
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    self.timed_out = True
                    if self.close():
                        return
            except PermissionLost:
                self.revoked = True
                if self.close():
                    return
    def __enter__(self):
        self.thread.start()
        return self
    def __exit__(self, *args):
        self.done.set()
        # A failed close retains this execution and its shared bulk slot.
        # No worker is abandoned and no cleanup success is reported early.
        retry = threading.Event()
        while not self.close():
            retry.wait(.02)
        self.thread.join()

def connect(config, *, register=None, permission=None):
    """Pinned-key connection with bounded socket, authentication and channel I/O."""
    import paramiko
    client = paramiko.SSHClient()
    if register:
        register(client)
    client.load_host_keys(config.known_hosts)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    if permission is not None:
        _check(permission)
    client.connect(config.host, port=config.port, username=config.username, key_filename=config.private_key,
                   timeout=config.timeout, banner_timeout=config.timeout, auth_timeout=config.timeout,
                   allow_agent=False, look_for_keys=False)
    if permission is not None:
        _check(permission)
    transport = client.get_transport()
    if transport is not None:
        transport.sock.settimeout(config.timeout)
    channel = transport.open_session(timeout=config.timeout)
    if register:
        register(channel)
    channel.settimeout(config.timeout)
    if permission is not None:
        _check(permission)
    channel.invoke_subsystem('sftp')
    sftp = paramiko.SFTPClient(channel)
    if register:
        register(sftp)
    sftp.get_channel().settimeout(config.timeout)
    return client, sftp

def _receipt(session, record):
    path = session / (uuid.uuid4().hex + '.json')
    with path.open('x', encoding='utf-8') as output:
        json.dump(dict(record, receipt_utc_ns=time.time_ns()), output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    return path.relative_to(session.parents[2]).as_posix()

def _download(sftp, config, name, size, mtime, session, permission, progress, record, owned):
    call = owned.call
    remote = str(PurePosixPath(config.log_root) / name)
    part = session / (uuid.uuid4().hex + '.part')
    record.update(file=name, planned_bytes=size, copied_bytes=0, partial_path=str(part), state='interrupted')
    digest = hashlib.sha256()
    source = None
    try:
        _check(permission)
        before = _metadata(call(sftp.stat, remote))
        _check(permission)
        record['before'] = before
        if before != {'size_bytes':size, 'mtime':mtime}:
            raise PullError('source_changed')
        if size == 0:
            raise PullError('empty_source')
        _check(permission)
        def open_source():
            handle = sftp.open(remote, 'rb')
            owned.register(handle)
            return handle
        source = call(open_source)
        _check(permission)
        if hasattr(source, 'settimeout'):
            source.settimeout(config.timeout)
        with part.open('xb') as target:
            try:
                while record['copied_bytes'] < size:
                    _check(permission)
                    length = min(CHUNK, config.max_read, size-record['copied_bytes'])
                    block = call(source.read, length)
                    _check(permission)  # Discard bytes returned after revocation.
                    if not block:
                        raise PullError('source_truncated')
                    if not isinstance(block, bytes) or len(block) > length:
                        raise PullError('invalid_read_length')
                    target.write(block)
                    digest.update(block)
                    record['copied_bytes'] += len(block)
                    if progress:
                        progress(name, record['copied_bytes'], size)
            finally:
                target.flush()
                os.fsync(target.fileno())
        _check(permission)
        after = _metadata(call(sftp.stat, remote))
        _check(permission)
        record['after'] = after
        if after != before:
            raise PullError('source_changed')
        _check(permission)
        final = part.with_suffix('.wpilog')
        # Hard-link publication is exclusive; no replacement of prior evidence.
        os.link(part, final)
        record['snapshot_path'] = str(final)
        _check(permission)
        part.unlink()
        record.update(state='captured', snapshot_path=str(final), sha256=digest.hexdigest())
        return final
    finally:
        if source is not None:
            try:
                source.close()
            except Exception:
                _check(permission)
                raise PullError('source_close_failed') from None

def collect(root, db, config, *, permission=None, bulk_slot=None, include_newest=False,
            profile=None, connect_fn=connect, progress=None):
    """Capture finite mutable snapshots with a live permission and shared bulk slot.

    Permission must expose check(), raising (or returning False) on any loss.
    bulk_slot() must acquire the service's shared network slot with cancellation.
    Interrupted partials are retained, never appended/resumed as immutable files.
    """
    from .import_folder import import_paths, summarize
    from .wpilog import PROFILE
    if permission is None or not callable(getattr(permission, 'check', None)):
        raise PullError('Fresh disabled permission is required')
    if not callable(bulk_slot):
        raise PullError('Shared bulk slot is required')
    _check(permission)
    session = Path(root) / 'pull-staging' / 'sessions' / uuid.uuid4().hex
    session.mkdir(parents=True)
    scope = dict(robot_id=config.robot_id, host=config.host, port=config.port, log_root=config.log_root)
    base = dict(schema_version=1, source=scope, source_type='MANUAL_LOCAL',
                capture_kind='mutable_manual_snapshot', closed_segment_verified=False,
                identity_strength='path_size_mtime_only; replacements_with_restored_metadata_not_detectable',
                session_id=session.name, include_newest=include_newest, mtime_unit='unix_seconds_source_reported')
    downloaded, problems, receipts, items, results = [], [], [], [], []
    held = None
    listing_complete = False
    interrupted = False
    try:
        with bulk_slot():
            _check(permission)
            with _Session(permission, config.timeout) as owned:
                _check(permission)
                if connect_fn is connect:
                    client, sftp = owned.call(lambda: connect_fn(config, register=owned.register, permission=owned))
                else:
                    def open_connection():
                        pair = connect_fn(config)
                        for resource in pair:
                            owned.register(resource)
                        return pair
                    client, sftp = owned.call(open_connection)
                _check(permission)
                items = listing(sftp, config.log_root, permission, owned.call)
                listing_complete = True
                todo, held, _ = plan(items, include_newest=include_newest)
                for name, size, mtime in todo:
                    record = dict(base)
                    try:
                        path = _download(sftp, config, name, size, mtime, session, permission, progress, record, owned)
                        downloaded.append((path,name))
                    except PermissionLost:
                        record.update(state='interrupted', reason='permission_revoked')
                        raise
                    except Exception as exc:
                        record.update(state='interrupted', reason=str(exc) if isinstance(exc,PullError) else 'source_io_failed')
                        problems.append({'file':name, 'reason':record['reason']})
                    finally:
                        receipts.append(_receipt(session,record))
    except PermissionLost:
        interrupted = True
        problems.append({'file':None, 'reason':'permission_revoked'})
    except Exception as exc:
        interrupted = True
        problems.append({'file':None, 'reason':str(exc) if isinstance(exc,PullError) else 'source_io_failed'})
    # Captured bytes are local evidence: later permission loss must not discard
    # earlier complete snapshots or prevent their local import.
    for path,name in downloaded:
        imported = import_paths(root, db, [path], profile=profile or PROFILE, source_type='MANUAL_LOCAL')[0]
        results.append(imported)
        receipts.append(_receipt(session,dict(base,state='imported',snapshot_path=str(path),import_result=imported)))
    import_attention = any(r['state'] not in ('succeeded','succeeded_with_unsupported') for r in results)
    state = 'interrupted' if interrupted else 'attention' if problems or not downloaded or import_attention else 'complete'
    receipts.append(_receipt(session,dict(base,state=state,listed=len(items) if listing_complete else None,listing_complete=listing_complete,captured=len(downloaded),problems=problems)))
    return {'state':state, 'listed':len(items) if listing_complete else None, 'listing_complete':listing_complete, 'downloaded':len(downloaded), 'already_pulled':0,
            'held_back_newest':held, 'problems':problems, 'imports':summarize(results), 'receipts':receipts,
            'capture_kind':'mutable_manual_snapshot', 'closed_segment_verified':False,
            'files':[{'file':name,'state':r['state']} for (_,name),r in zip(downloaded,results)]}
