"""Fail-closed, bounded transfer queue; immutable evidence is never source-deleted."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import random
import re
import sqlite3
import time
from typing import Callable, Protocol
from .storage import DataRootOwner, open_catalog
from .transfer import (BoundedIO, Cancellation, IdentityError, ManifestPage, SourceMissing,
                       Throughput, TransferCancelled, TransferError)
from .verification import Verifier
from .config import Config


@dataclass(frozen=True)
class RobotStatus:
    enabled: bool | None
    observed_at: float
    boot_id: str
    generation: int
    transfer_allowed: bool = True
    sequence: int | None = None
    link_profile: str = "default"


@dataclass(frozen=True)
class LogFile:
    id: str
    name: str
    size: int
    sha256: str
    created_at: float
    relative_path: str | None = None
    boot_id: str | None = None
    format: str = "opaque"
    format_profile: str | None = None

    def validate(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.id):
            raise ValueError("Unsafe or missing file identity")
        if type(self.size) is not int or self.size < 0 or not isinstance(self.sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise ValueError("Invalid closed-file size or checksum")
        if type(self.created_at) not in (int, float) or not math.isfinite(self.created_at):
            raise ValueError("Invalid file creation time")
        if self.relative_path is not None:
            path = self.relative_path
            if (not isinstance(path, str) or not path or '\\' in path or '\x00' in path
                    or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive
                    or '..' in PurePosixPath(path).parts):
                raise ValueError("Unsafe source path; must remain under the configured log root")


class Source(Protocol):
    def status(self) -> RobotStatus: ...
    def list_closed_files(self) -> list[LogFile]: ...
    def read(self, file_id: str, offset: int, length: int) -> bytes: ...


class Collector:
    def __init__(self, root: Path, source: Source, *, idle_delay=10, freshness=1,
                 chunk_size=256 * 1024, clock: Callable[[], float] = time.monotonic,
                 owner=None, paused=None, stopping=lambda: False, publish=None,
                 independent_verification=False, io_timeout=5, discovery_page_size=100,
                 retry_initial=1, retry_max=30, retry_limit=8, eta_window=15,
                 eta_minimum=1, historical_max_age=300, jitter=None):
        if (type(idle_delay) not in (int, float) or not math.isfinite(idle_delay) or idle_delay < 0
                or type(freshness) not in (int, float) or not math.isfinite(freshness) or freshness <= 0
                or type(chunk_size) is not int or chunk_size <= 0):
            raise ValueError("Invalid collector limits")
        Config(idle_delay=idle_delay, freshness=freshness, chunk_size=chunk_size,
               io_timeout=io_timeout, discovery_page_size=discovery_page_size,
               retry_initial=retry_initial, retry_max=retry_max, retry_limit=retry_limit,
               eta_window=eta_window, eta_minimum=eta_minimum, historical_max_age=historical_max_age)
        self._owner = DataRootOwner(root) if owner is None else None
        self.root, self.source, self.clock = root, source, clock
        self._paused_provider, self._stopping, self._publish = paused, stopping, publish
        self.idle_delay, self.freshness, self.chunk_size = idle_delay, freshness, chunk_size
        self.independent_verification, self.io_timeout = independent_verification, io_timeout
        self.discovery_page_size = discovery_page_size
        self.retry_initial, self.retry_max, self.retry_limit = retry_initial, retry_max, retry_limit
        self.jitter = jitter or (lambda: random.uniform(0.8, 1.2))
        self.throughput = Throughput(eta_window, eta_minimum, historical_max_age)
        self.io = BoundedIO()
        self.verifier = Verifier(self._digest)
        self._wall_origin = time.time() - self.clock()
        self.hashers = {}
        try:
            self._initialize()
        except BaseException:
            if hasattr(self, 'db'):
                self.db.close()
            if self._owner is not None:
                self._owner.close()
            raise

    def _initialize(self):
        self.db = open_catalog(self.root)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO transfer_meta(file_id) SELECT id FROM files')
        self.partial, self.archive = self.root / 'partial', self.root / 'archive'
        self.partial.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)
        row = self.db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
        self.paused = row is not None and row[0] == '1'
        self.state, self.reason, self.error = 'waiting', 'Waiting for fresh robot status', None
        self.disabled_since = self.status_token = self.last_discovery = self.pause_generation = None
        self.active_id = self.cursor = self.discovery_id = None
        self.seen, self.cursors = set(), set()
        self.recent_streak = 0
        self.discovery_complete = False
        self.discovery_retry = 0
        self.discovery_attempts = 0
        latest = self.db.execute('SELECT * FROM manifest_snapshots ORDER BY snapshot_id DESC LIMIT 1').fetchone()
        self.manifest = dict(latest) if latest else None
        successful = self.db.execute('SELECT observed_utc_ns FROM manifest_snapshots WHERE complete=1 ORDER BY snapshot_id DESC LIMIT 1').fetchone()
        self.last_successful_discovery_utc_ns = successful[0] if successful else None
        self.verification_seconds = 0
        self._recover()

    def _publish_progress(self):
        if self._publish is not None:
            self._publish(self.snapshot())

    def _digest(self, path):
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
        return h.hexdigest()

    def _event(self, code, identity=None, detail=None):
        with self.db:
            self.db.execute('INSERT INTO transfer_events(file_id,code,monotonic_ns,detail) VALUES (?,?,?,?)',
                            (identity, code, str(int(self.clock() * 1e9)), detail))

    def _quarantine(self, path, identity):
        if not path.exists():
            return
        destination = self.partial / (identity + '.invalid')
        if destination.exists():
            destination = self.partial / (identity + '.' + str(time.time_ns()) + '.invalid')
        path.replace(destination)

    def _recover(self):
        # Never hold a catalog write transaction while hashing or touching files.
        for row in self.db.execute('SELECT * FROM files').fetchall():
            final, part = self.archive / (row['id'] + '.logdata'), self.partial / (row['id'] + '.part')
            meta = self.db.execute('SELECT * FROM transfer_meta WHERE file_id=?', (row['id'],)).fetchone()
            if meta['error_code'] == 'identity_conflict':
                continue  # Restart never acknowledges a broken immutable identity.
            if final.exists():
                if final.stat().st_size == row['size'] and self._digest(final) == row['sha256']:
                    with self.db:
                        self.db.execute("UPDATE files SET offset=size,state='complete',error=NULL WHERE id=?", (row['id'],))
                        self.db.execute("INSERT OR REPLACE INTO verification_jobs(file_id,state,digest,verified_utc_ns) VALUES (?,'complete',?,?)", (row['id'], row['sha256'], str(time.time_ns())))
                    continue
                self._quarantine(final, row['id'])
                self._event('archive_integrity_failed', row['id'], 'Archived bytes changed; preserved in quarantine')
            reset = row['state'] == 'complete'
            offset = 0 if reset else row['offset']
            actual = part.stat().st_size if part.exists() else 0
            if actual < offset:
                offset, reset = 0, True
            if part.exists():
                with part.open('r+b') as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
            h = hashlib.sha256()
            if offset:
                with part.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(block)
                if (meta['checkpoint_digest'] and (meta['checkpoint_offset'] != offset or meta['checkpoint_digest'] != h.hexdigest())):
                    self._quarantine(part, row['id'])
                    offset, reset, h = 0, True, hashlib.sha256()
                    self._event('partial_integrity_failed', row['id'], 'Checkpoint digest changed; quarantined and restarted')
            self.hashers[row['id']] = h
            with self.db:
                if reset:
                    self.db.execute("UPDATE files SET offset=0,state='queued',error=NULL WHERE id=?", (row['id'],))
                elif row['state'] == 'verifying' or (offset == row['size'] and row['state'] == 'queued'):
                    self.db.execute("UPDATE files SET state='verifying' WHERE id=?", (row['id'],))
                    self.db.execute("INSERT OR REPLACE INTO verification_jobs(file_id,state) VALUES (?,'queued')", (row['id'],))
                self.db.execute('UPDATE transfer_meta SET checkpoint_offset=?,checkpoint_digest=? WHERE file_id=?', (offset, h.hexdigest(), row['id']))

    def set_paused(self, paused):
        self.paused = paused
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('paused',?)", ('1' if paused else '0',))
        self.disabled_since = None
        self.throughput.reset()
        self.io.cancel()
        self.state, self.reason = ('paused', 'Paused by operator') if paused else ('waiting', 'Waiting for idle window')

    def retry_errors(self, identity=None):
        rows = self.db.execute("SELECT id,offset,size FROM files WHERE state='error'" + (' AND id=?' if identity else ''), (identity,) if identity else ()).fetchall()
        with self.db:
            for row in rows:
                meta = self.db.execute('SELECT error_code FROM transfer_meta WHERE file_id=?', (row['id'],)).fetchone()
                if meta['error_code'] == 'identity_conflict':
                    continue  # Cannot acknowledge a broken immutable identity as a safe retry.
                local_retry = meta['error_code'] == 'local_verification' and row['offset'] == row['size']
                self.db.execute("UPDATE files SET state=?,error=NULL WHERE id=?", ('verifying' if local_retry else 'queued', row['id']))
                if local_retry:
                    self.db.execute("INSERT OR REPLACE INTO verification_jobs(file_id,state) VALUES (?,'queued')", (row['id'],))
                self.db.execute('UPDATE transfer_meta SET attempts=0,next_retry=0,error_code=NULL WHERE file_id=?', (row['id'],))
        self.discovery_retry = 0
        self.last_discovery = None
        self._event('explicit_retry', identity)

    def set_priority(self, identity, priority, urgent=False):
        if type(priority) is not int or not 0 <= priority <= 100 or type(urgent) is not bool:
            raise ValueError('Invalid priority')
        with self.db:
            result = self.db.execute('UPDATE transfer_meta SET priority=?,urgent=? WHERE file_id=?', (priority, int(urgent), identity))
            if not result.rowcount:
                raise ValueError('Unknown transfer')
        self._event('priority_changed', identity, f'priority={priority}; urgent={urgent}')

    def _gate(self):
        if self._paused_provider is not None:
            self.paused, generation = self._paused_provider()
            if generation != self.pause_generation:
                self.disabled_since = None
                self.throughput.reset()
                self.last_discovery = None
                self.discovery_complete = False
                self.discovery_id = self.cursor = None
                self.io.cancel()
            self.pause_generation = generation
        status = self.source.status()
        now = self.clock()
        token = (status.boot_id, status.generation)
        if token != self.status_token:
            self.disabled_since = None
            self.throughput.reset()
            self.last_discovery = None
            self.discovery_complete = False
            self.discovery_id = self.cursor = None
            self.io.cancel()
        self.throughput.change_profile(status.link_profile)
        self.status_token = token
        try:
            age = now - status.observed_at
            valid = (type(status.enabled) is bool and type(status.transfer_allowed) is bool
                     and type(status.observed_at) in (int, float)
                     and isinstance(status.boot_id, str) and bool(status.boot_id)
                     and type(status.generation) is int and status.generation >= 0
                     and math.isfinite(age) and 0 <= age <= self.freshness)
        except (TypeError, OverflowError):
            valid = False
        if self._stopping():
            self.state, self.reason = 'paused', 'Service stopping; progress saved'
        elif self.paused:
            self.state, self.reason = 'paused', 'Paused by operator'
        elif not valid:
            self.state, self.reason = 'paused', 'Robot status unknown or stale'
        elif status.enabled:
            self.state, self.reason = 'paused', 'Robot enabled'
        elif not status.transfer_allowed:
            self.state, self.reason = 'paused', 'Source transfer permission denied'
        else:
            if self.disabled_since is None:
                self.disabled_since = now
            remaining = self.idle_delay - (now - self.disabled_since)
            if remaining <= 0:
                return True
            self.state, self.reason = 'waiting', f'Idle window begins in {math.ceil(remaining)} s'
            return False
        self.disabled_since = None
        self.throughput.reset()
        self.io.cancel()
        return False

    def _source_call(self, function, length=0):
        generation = (self.status_token, self.pause_generation)
        token = Cancellation(generation, time.monotonic() + self.io_timeout)
        bind = getattr(self.source, 'bind_cancellation', None)
        if bind is not None:
            bind(token)
        def permitted():
            return self._gate() and generation == (self.status_token, self.pause_generation)
        result = self.io.call(lambda: function(token), token, self.io_timeout, length=length, permitted=permitted)
        if not permitted():
            token.cancel()
        token.check()
        return result

    def _discover(self):
        self.state, self.reason = 'discovering', 'Discovering closed files; queue snapshot pending'
        self.last_discovery, self.discovery_complete = None, False
        self._publish_progress()
        modern = getattr(self.source, 'discover_closed', None)
        if modern:
            page = self._source_call(lambda token: modern(self.cursor, self.discovery_page_size, token))
        else:
            files = self._source_call(lambda token: self.source.list_closed_files())
            revision = hashlib.sha256(json.dumps([asdict(f) for f in files], sort_keys=True).encode()).hexdigest()
            page = ManifestPage(revision, tuple(files))
        if (not isinstance(page, ManifestPage) or not isinstance(page.revision, str) or not page.revision
                or type(page.complete) is not bool or type(page.open_bytes) is not int or page.open_bytes < 0
                or type(page.pending_digest_bytes) is not int or page.pending_digest_bytes < 0
                or page.complete != (page.next_cursor is None)
                or (modern and len(page.files) > self.discovery_page_size)):
            raise ValueError('Invalid or unbounded manifest page')
        if self.discovery_id is None:
            with self.db:
                result = self.db.execute('INSERT INTO manifest_snapshots(revision,observed_utc_ns,observed_monotonic_ns) VALUES (?,?,?)', (page.revision, str(time.time_ns()), str(int(self.clock() * 1e9))))
            self.discovery_id = result.lastrowid
            self.manifest = {'revision': page.revision, 'open_bytes': 0, 'pending_digest_bytes': 0}
            self.seen, self.cursors = set(), set()
        if page.revision != self.manifest['revision']:
            raise ValueError('Manifest revision changed during pagination; discovery incomplete')
        if page.next_cursor in self.cursors:
            raise ValueError('Manifest pagination cursor repeated')
        page_seen = set()
        for item in page.files:
            if not isinstance(item, LogFile):
                raise ValueError('Malformed manifest segment')
            item.validate()
            if item.id in self.seen or item.id in page_seen:
                raise ValueError('Duplicate file identity in manifest')
            page_seen.add(item.id)
            old = self.db.execute('SELECT * FROM files WHERE id=?', (item.id,)).fetchone()
            if old and (old['size'] != item.size or old['sha256'] != item.sha256):
                self._quarantine(self.partial / (item.id + '.part'), item.id)
                with self.db:
                    self.db.execute("UPDATE files SET state='error',error='Closed file changed identity' WHERE id=?", (item.id,))
                    self.db.execute("UPDATE transfer_meta SET error_code='identity_conflict' WHERE file_id=?", (item.id,))
                    self.db.execute('INSERT OR REPLACE INTO manifest_items VALUES (?,?,?)', (self.discovery_id, item.id, json.dumps(asdict(item))))
                self._event('identity_conflict', item.id)
                raise IdentityError(f'Closed file changed identity: {item.id}')
        with self.db:
            for item in page.files:
                self.db.execute('''INSERT INTO files(id,name,size,sha256,created_at) VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name''', (item.id, item.name, item.size, item.sha256, item.created_at))
                self.db.execute('INSERT OR IGNORE INTO transfer_meta(file_id) VALUES (?)', (item.id,))
                self.db.execute("UPDATE files SET state='queued',error=NULL WHERE id=? AND state='missing'", (item.id,))
                self.db.execute("UPDATE transfer_meta SET available=1,error_code=CASE WHEN error_code='source_missing' THEN NULL ELSE error_code END WHERE file_id=?", (item.id,))
                self.db.execute('INSERT INTO manifest_items VALUES (?,?,?)', (self.discovery_id, item.id, json.dumps(asdict(item))))
            observed_utc_ns, observed_monotonic_ns = str(time.time_ns()), str(int(self.clock() * 1e9))
            self.db.execute('UPDATE manifest_snapshots SET complete=?,open_bytes=?,pending_digest_bytes=?,observed_utc_ns=?,observed_monotonic_ns=? WHERE snapshot_id=?', (int(page.complete), page.open_bytes, page.pending_digest_bytes, observed_utc_ns, observed_monotonic_ns, self.discovery_id))
            if page.complete:
                self.db.execute('''UPDATE transfer_meta SET available=0,error_code=CASE WHEN error_code='identity_conflict' THEN error_code ELSE 'source_missing' END WHERE file_id IN
                    (SELECT id FROM files WHERE state NOT IN ('complete','verifying')) AND file_id NOT IN
                    (SELECT file_id FROM manifest_items WHERE snapshot_id=?)''', (self.discovery_id,))
                self.db.execute("UPDATE files SET state='missing' WHERE id IN (SELECT file_id FROM transfer_meta WHERE available=0) AND state='queued'")
        self.seen.update(item.id for item in page.files)
        self.manifest.update(open_bytes=page.open_bytes, pending_digest_bytes=page.pending_digest_bytes, observed_utc_ns=observed_utc_ns, observed_monotonic_ns=observed_monotonic_ns)
        self.cursor = page.next_cursor
        if page.next_cursor is not None:
            self.cursors.add(page.next_cursor)
        if page.complete:
            self.last_discovery, self.discovery_complete = self.clock(), True
            self.last_successful_discovery_utc_ns = observed_utc_ns
            self.discovery_id, self.cursor = None, None
            self.discovery_attempts = 0

    def _select(self):
        rows = self.db.execute('''SELECT f.*,m.priority,m.urgent FROM files f JOIN transfer_meta m ON f.id=m.file_id
            WHERE f.state='queued' AND m.available=1 AND m.next_retry<=? ORDER BY m.urgent DESC,m.priority DESC,f.created_at DESC,f.id''', (self._wall_origin + self.clock(),)).fetchall()
        if not rows:
            self.active_id = None
            return None
        urgent = next((r for r in rows if r['urgent']), None)
        current = next((r for r in rows if r['id'] == self.active_id), None)
        if current is not None and (urgent is None or current['urgent']):
            return current
        if urgent is not None:
            row, reason = urgent, 'explicit urgent priority'
        elif self.recent_streak >= 2:
            row, reason = min(rows, key=lambda r: (r['created_at'], r['id'])), 'oldest eligible after two recent files'
            self.recent_streak = 0
        else:
            row, reason = rows[0], 'incident priority' if rows[0]['priority'] else 'most recent completed recording'
            self.recent_streak += 1
        self.active_id = row['id']
        with self.db:
            self.db.execute('UPDATE transfer_meta SET selection_reason=?,last_selected=? WHERE file_id=?', (reason, self._wall_origin + self.clock(), row['id']))
        self._event('file_selected', row['id'], reason)
        return row

    def _queue_verification(self, row):
        part = self.partial / (row['id'] + '.part')
        part.touch(exist_ok=True)
        with self.db:
            self.db.execute("UPDATE files SET state='verifying' WHERE id=?", (row['id'],))
            self.db.execute("INSERT OR REPLACE INTO verification_jobs(file_id,state) VALUES (?,'queued')", (row['id'],))
        self.active_id = None
        self.state, self.reason = 'verifying', 'Verifying downloaded file locally'
        self._publish_progress()
        if not self.independent_verification:
            start = self.clock()
            self._apply_verification_result((row['id'], self._digest(part), None, max(0, self.clock() - start)))

    def _verification_path(self, identity):
        final = self.archive / (identity + '.logdata')
        return final if final.exists() else self.partial / (identity + '.part')

    def _apply_verification_result(self, result):
        identity = result[0]
        try:
            self._finish_verification(*result)
        except (OSError, sqlite3.Error) as exc:
            # A local finalize fault preserves full durable bytes, never queues
            # another robot download. Explicit retry re-verifies/reconciles locally.
            with self.db:
                self.db.execute("UPDATE files SET state='error',error='Local verification finalization failed' WHERE id=?", (identity,))
                self.db.execute("UPDATE transfer_meta SET error_code='local_verification' WHERE file_id=?", (identity,))
                self.db.execute("UPDATE verification_jobs SET state='error',error_code='local_verification' WHERE file_id=?", (identity,))
            self.state, self.reason, self.error = 'attention', 'Local verification finalization failed; check storage and explicitly retry', str(exc)
            self._event('local_verification_failed', identity)

    def _finish_verification(self, identity, digest, error, duration):
        row = self.db.execute('SELECT * FROM files WHERE id=?', (identity,)).fetchone()
        part = self._verification_path(identity)
        self.verification_seconds += duration
        if row['state'] != 'verifying':
            return  # A later identity conflict takes precedence over a local job.
        if error or digest != row['sha256'] or not part.exists() or part.stat().st_size != row['size']:
            self._quarantine(part, identity)
            with self.db:
                self.db.execute("UPDATE files SET offset=0,state='error',error='Checksum mismatch or verification read failure' WHERE id=?", (identity,))
                self.db.execute("UPDATE transfer_meta SET error_code='integrity',checkpoint_offset=0,checkpoint_digest=NULL WHERE file_id=?", (identity,))
                self.db.execute("UPDATE verification_jobs SET state='error',error_code='integrity',elapsed_seconds=? WHERE file_id=?", (duration, identity))
            self.hashers.pop(identity, None)
            self.state, self.reason = 'attention', 'Checksum mismatch; original remains on source'
            self._event('verification_failed', identity)
            return
        final = self.archive / (identity + '.logdata')
        if part != final:
            part.replace(final)
        with self.db:
            self.db.execute("UPDATE files SET offset=size,state='complete',error=NULL WHERE id=?", (identity,))
            self.db.execute("UPDATE transfer_meta SET attempts=0,next_retry=0,error_code=NULL WHERE file_id=?", (identity,))
            self.db.execute("UPDATE verification_jobs SET state='complete',digest=?,elapsed_seconds=?,verified_utc_ns=? WHERE file_id=?", (digest, duration, str(time.time_ns()), identity))
        self.hashers.pop(identity, None)
        self._event('archive_verified', identity)

    def _verification_tick(self):
        if not self.independent_verification:
            row = self.db.execute("SELECT j.file_id FROM verification_jobs j JOIN files f ON f.id=j.file_id WHERE j.state='queued' AND f.state='verifying' ORDER BY j.rowid LIMIT 1").fetchone()
            if row:
                part = self._verification_path(row['file_id'])
                part.touch(exist_ok=True)
                start = self.clock()
                self._apply_verification_result((row['file_id'], self._digest(part), None, max(0, self.clock() - start)))
            return
        result = self.verifier.poll()
        if result:
            self._apply_verification_result(result)
        if self.verifier.thread is None:
            row = self.db.execute("SELECT j.file_id FROM verification_jobs j JOIN files f ON f.id=j.file_id WHERE j.state='queued' AND f.state='verifying' ORDER BY j.rowid LIMIT 1").fetchone()
            if row:
                with self.db:
                    self.db.execute("UPDATE verification_jobs SET state='running' WHERE file_id=?", (row['file_id'],))
                self.verifier.submit(row['file_id'], self._verification_path(row['file_id']))

    def _failure(self, exc, identity=None):
        code = 'malformed_manifest' if isinstance(exc, ValueError) else getattr(exc, 'code', 'transient')
        transient = getattr(exc, 'retryable', True) and not isinstance(exc, ValueError)
        if getattr(exc, 'errno', None) == 28 or getattr(exc, 'winerror', None) == 112:
            code, transient = 'disk_full', False
        if identity:
            meta = self.db.execute('SELECT attempts FROM transfer_meta WHERE file_id=?', (identity,)).fetchone()
            attempts = meta['attempts'] + 1
            retry = transient and attempts < self.retry_limit
            delay = min(self.retry_max, self.retry_initial * 2 ** min(attempts - 1, 30) * self.jitter())
            with self.db:
                self.db.execute('UPDATE transfer_meta SET attempts=?,next_retry=?,error_code=?,available=CASE WHEN ? THEN 0 ELSE available END WHERE file_id=?', (attempts, self._wall_origin + self.clock() + delay, code, isinstance(exc, SourceMissing), identity))
                self.db.execute('UPDATE files SET state=?,error=? WHERE id=?', ('queued' if retry else 'error', str(exc), identity))
            self.active_id = None
            self.state, self.reason = ('waiting', 'File retry backoff; other files may continue') if retry else ('attention', 'File requires explicit retry or investigation')
            if code == 'disk_full':
                self.reason = 'Local storage full; free space and explicitly retry'
        else:
            self.discovery_attempts += 1
            self.discovery_retry = self.clock() + min(self.retry_max, self.retry_initial * 2 ** min(self.discovery_attempts - 1, 30) * self.jitter()) if transient else math.inf
            self.state, self.reason = ('paused', 'Discovery interrupted; will retry') if transient else ('attention', 'Source manifest or authentication requires attention')
            if self.discovery_id:
                with self.db:
                    self.db.execute('UPDATE manifest_snapshots SET error_code=? WHERE snapshot_id=?', (code, self.discovery_id))
            self.discovery_id = self.cursor = None
        self.error = str(exc)
        self._event(code, identity)

    def tick(self):
        identity = None
        try:
            self._verification_tick()  # Local work remains eligible while robot is enabled.
            if not self._gate():
                return
            self.error = None
            if self.io.busy:
                self.state, self.reason = 'attention', 'Adapter still has canceled I/O outstanding; no new source requests'
                return
            if self.last_discovery is None or self.clock() - self.last_discovery >= 5:
                if self.clock() >= self.discovery_retry:
                    self._discover()
                elif not self.discovery_complete:
                    self.state, self.reason = 'attention', 'Manifest unavailable; queue is last known'
                if not self._gate():
                    return
                if self.cursor is not None:
                    return
            row = self._select()
            if row is None:
                snapshot = self.snapshot()
                if snapshot['verification_pending_files']:
                    self.state, self.reason = 'verifying', 'Verifying downloaded files locally'
                elif snapshot['blocked_files'] or snapshot['pending_digest_bytes'] or not self.discovery_complete:
                    self.state, self.reason = 'attention', 'Unavailable files or incomplete discovery require attention'
                elif snapshot['transferable_files']:
                    self.state, self.reason = 'waiting', 'Waiting for file retry backoff'
                else:
                    self.state, self.reason = 'caught_up', 'All discovered closed files archived'
                return
            identity = row['id']
            self.state, self.reason = 'downloading', 'Collecting during idle time'
            self._publish_progress()
            part, offset = self.partial / (identity + '.part'), row['offset']
            if offset < row['size']:
                length = min(self.chunk_size, row['size'] - offset)
                start = self.clock()
                # Signature negotiation is explicit: legacy demo reads are client-only.
                read = self.source.read
                modern = 'cancellation' in inspect.signature(read).parameters
                block = self._source_call(lambda token: read(identity, offset, length, permission_generation=token.generation, cancellation=token) if modern else read(identity, offset, length), length)
                if not isinstance(block, bytes) or not block or len(block) > length:
                    raise TransferError('Source returned an empty or oversized block')
                h = self.hashers.get(identity, hashlib.sha256())
                if identity not in self.hashers and offset:
                    raise OSError('Partial hash checkpoint unavailable; restart to recover')
                candidate = h.copy()
                candidate.update(block)
                with part.open('r+b' if part.exists() else 'w+b') as stream:
                    if stream.seek(0, 2) < offset:
                        raise OSError('Partial file is shorter than its checkpoint; restart to recover')
                    stream.seek(offset)
                    stream.write(block)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                offset += len(block)
                with self.db:
                    self.db.execute('UPDATE files SET offset=?,error=NULL WHERE id=?', (offset, identity))
                    self.db.execute('UPDATE transfer_meta SET checkpoint_offset=?,checkpoint_digest=?,attempts=0,next_retry=0,error_code=NULL WHERE file_id=?', (offset, candidate.hexdigest(), identity))
                self.hashers[identity] = candidate
                self.throughput.add(self.clock() - start, len(block), self.clock())
            if offset == row['size']:
                self._queue_verification(row)
                self._verification_tick()
        except TransferCancelled:
            self._gate()
            self._event('permission_revoked', identity)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._failure(exc, identity)

    def snapshot(self):
        last_archive = self.db.execute("SELECT MAX(CAST(verified_utc_ns AS INTEGER)) FROM verification_jobs WHERE state='complete'").fetchone()[0]
        rows = [dict(r) for r in self.db.execute('''SELECT f.*,m.attempts,m.next_retry,m.error_code,m.priority,m.urgent,m.available,m.format_status,m.selection_reason
            FROM files f JOIN transfer_meta m ON f.id=m.file_id ORDER BY f.created_at DESC,f.id''')]
        pending = [r for r in rows if r['state'] != 'complete']
        blocked = [r for r in pending if r['state'] == 'error' or not r['available']]
        transferable = [r for r in pending if r not in blocked and r['state'] != 'verifying']
        remaining = sum(r['size'] - r['offset'] for r in transferable)
        pending_digest_bytes = self.manifest.get('pending_digest_bytes', 0) if self.manifest else 0
        blocked_bytes = sum(r['size'] - r['offset'] for r in blocked) + pending_digest_bytes
        active = self.state == 'downloading'
        eta = self.throughput.estimate(remaining, self.clock(), active=active, complete=self.discovery_complete, blocked=bool(blocked) or pending_digest_bytes > 0)
        return {'state': self.state, 'reason': self.reason, 'error': self.error, 'files': rows,
                'last_archive_utc_ns':str(last_archive) if last_archive is not None else None,
                'paused_by_operator': self.paused, 'active_id': self.active_id,
                'remaining_bytes': remaining + blocked_bytes, 'transferable_bytes': remaining,
                'blocked_bytes': blocked_bytes, 'blocked_files': len(blocked),
                'transferable_files': len(transferable), 'pending_files': len(pending),
                'completed_files': len(rows) - len(pending),
                'verification_pending_files': sum(r['state'] == 'verifying' for r in rows),
                'verification_seconds': self.verification_seconds,
                'discovery_complete': self.discovery_complete,
                'queue_is_last_known': self.last_discovery is None,
                'manifest_revision': self.manifest['revision'] if self.manifest else None,
                'manifest_observed_utc_ns': self.manifest.get('observed_utc_ns') if self.manifest else None,
                'last_successful_discovery_utc_ns': self.last_successful_discovery_utc_ns,
                'open_bytes': self.manifest.get('open_bytes', 0) if self.manifest else 0,
                'pending_digest_bytes': self.manifest.get('pending_digest_bytes', 0) if self.manifest else 0,
                'outstanding_bytes': self.io.outstanding_bytes,
                'max_outstanding_bytes': self.io.maximum_bytes, 'outstanding_byte_limit': self.chunk_size,
                'source_cancellation_guarantee': 'client bounded; transport cancellation tail unqualified',
                **eta}

    def close(self):
        self.io.cancel()
        # Ownership cannot be released while an adapter/local worker still uses root.
        if self.io.thread is not None:
            self.io.thread.join()
        if self.verifier.thread is not None:
            self.verifier.thread.join()
            result = self.verifier.poll()
            if result:
                self._apply_verification_result(result)
        self.db.close()
        if self._owner is not None:
            self._owner.close()
