"""Opt-in Limelight4 Rewind HTTP and immutable local group archive.

Only /results, /recording-list, /recording-flush and /recording/<safe-name>
are reachable. No proxy, redirect, source deletion or Range/resume support.
Camera filenames/sizes have no strong version identity: each content revalidation reads
all bytes anew and deduplicates using locally observed complete-group hashes.
Discovery caching cannot detect same-name/same-size replacements until recheck.
A successful transfer establishes byte preservation, never camera timing,
physical identity, frame decoding, video/manifest alignment or USB durability.

Protocol: https://docs.limelightvision.io/docs/docs-limelight/apis/rest-http-api
and /apis/json-results-specification. Detailed size is interpreted as the group
combined size (video + manifest + optional bootlog); incompatible firmware
fails closed pending explicit schema qualification. DNS resolution follows the
OS resolver; socket connect/read and total response processing are bounded,
but OS DNS latency is not a hard real-time guarantee. abort() shuts down an
active socket; the caller supplies fresh-disabled/idle permission per chunk.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import threading
import time
from urllib.parse import urlsplit


class RewindError(RuntimeError):
    """Redacted operational error, safe for worker health."""


class RewindCancelled(RewindError):
    pass


def _integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RewindError('Invalid Rewind ' + name)
    return value


def _number(value, minimum, maximum, name):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise RewindError('Invalid Rewind ' + name)
    return value


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', value):
        raise RewindError('Invalid Rewind identity')
    return value


def _filename(value):
    # ASCII allowlist also excludes percent-encoded paths and Windows devices.
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,179}', value)
            or '..' in value or value.endswith('.') or value.split('.')[0].upper() in
            {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(10)),*(f'LPT{i}' for i in range(10))}):
        raise RewindError('Invalid Rewind recording filename')
    if not value.endswith(('.avi', '_manifest.jsonl', '_bootlog.txt.gz')):
        raise RewindError('Unsupported Rewind file type')
    return value


@dataclass(frozen=True)
class RewindConfig:
    camera_id: str
    origin: str
    robot_id: str
    source_type: str = 'REAL'
    duration_seconds: int = 180
    timeout_seconds: float = 5.0
    max_recording_bytes: int = 536870912
    poll_interval_seconds: float = 5.0
    max_json_bytes: int = 2097152
    max_recordings_per_poll: int = 1
    list_limit: int = 1000
    revalidate_seconds: float = 3600.0
    download_deadline_seconds: float = 900.0

    def __post_init__(self):
        _identifier(self.camera_id)
        _identifier(self.robot_id)
        if self.source_type not in ('REAL', 'SYNTHETIC'):
            raise RewindError('Rewind source_type must be REAL or SYNTHETIC')
        try:
            parsed = urlsplit(self.origin)
            host = parsed.hostname
            valid = (parsed.scheme == 'http' and parsed.port == 5807 and host and
                     parsed.path in ('', '/') and not parsed.query and not parsed.fragment and
                     not parsed.username and not parsed.password and
                     re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host))
            if valid and re.fullmatch(r'[0-9.]+', host):
                socket.inet_pton(socket.AF_INET, host)
            if not valid or any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-')
                                for part in host.split('.')):
                raise ValueError()
        except (TypeError, ValueError, OSError, AttributeError):
            raise RewindError('Rewind requires explicit HTTP hostname/IPv4 origin on port 5807') from None
        _integer(self.duration_seconds, 1, 500, 'duration')
        _number(self.timeout_seconds, .1, 30, 'timeout')
        _integer(self.max_recording_bytes, 1, 8 * 1024**3, 'recording byte bound')
        _number(self.poll_interval_seconds, .1, 3600, 'poll interval')
        _integer(self.max_json_bytes, 128, 16 * 1024**2, 'JSON byte bound')
        _integer(self.max_recordings_per_poll, 1, 100, 'poll recording bound')
        _integer(self.list_limit, 1, 10000, 'list bound')
        _number(self.revalidate_seconds, 1, 86400, 'content revalidation interval')
        _number(self.download_deadline_seconds, 1, 3600, 'download deadline')

    @classmethod
    def from_mapping(cls, value):
        if value is None:
            return None
        if not isinstance(value, dict) or value.keys() - cls.__dataclass_fields__.keys():
            raise RewindError('Invalid Rewind configuration fields')
        try:
            return cls(**value)
        except TypeError:
            raise RewindError('Required Rewind configuration fields missing') from None


def _check(cancel):
    if cancel():
        raise RewindCancelled('Rewind operation paused or cancelled')


def _json(raw):
    try:
        def constant(_):
            raise ValueError()
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        return json.loads(raw.decode('utf-8', errors='strict'), parse_constant=constant, object_pairs_hook=unique)
    except (UnicodeError, ValueError, RecursionError):
        raise RewindError('Invalid Rewind JSON') from None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')


class _InterruptibleReader(io.RawIOBase):
    """Own a duplicate socket so HTTPConnection's close semantics stay valid.

    Poll before recv rather than timing out a socket.makefile buffer (which
    becomes unusable after a timeout in CPython). This also bounds header waits
    on Windows where close/shutdown need not interrupt another thread's recv.
    """
    def __init__(self, sock, cancel, deadline, gap_timeout):
        self.gap_timeout = gap_timeout
        self.socket = sock.dup()
        self.cancel, self.deadline = cancel, deadline
        self.socket.settimeout(.1)

    def readable(self):
        return True

    def readinto(self, buffer):
        gap_deadline = time.monotonic() + self.gap_timeout
        while True:
            _check(self.cancel)
            remaining = min(self.deadline, gap_deadline) - time.monotonic()
            if remaining <= 0:
                raise RewindError('Rewind response deadline exceeded')
            ready, _, _ = select.select([self.socket], [], [], min(.1, remaining))
            if ready:
                try:
                    size = self.socket.recv_into(buffer)
                    _check(self.cancel)
                    if time.monotonic() > self.deadline:
                        raise RewindError('Rewind response deadline exceeded')
                    return size
                except socket.timeout:
                    continue

    def close(self):
        if not self.closed:
            self.socket.close()
        super().close()


class _ResponseSocket:
    def __init__(self, sock, cancel, deadline, gap_timeout):
        self.sock, self.cancel, self.deadline = sock, cancel, deadline
        self.gap_timeout = gap_timeout

    def makefile(self, mode):
        if mode != 'rb':
            raise RewindError('Unsupported Rewind socket mode')
        return io.BufferedReader(_InterruptibleReader(self.sock, self.cancel, self.deadline, self.gap_timeout))


class RewindHttpTransport:
    def __init__(self, config):
        if not isinstance(config, RewindConfig):
            raise RewindError('Explicit validated Rewind configuration required')
        self.config = config
        self._lock = threading.Lock()
        self._active = None
        self._abort_event = None

    def abort(self):
        with self._lock:
            connection = self._active
            if self._abort_event is not None:
                self._abort_event.set()
        if connection is not None:
            try:
                if connection.sock is not None:
                    connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def _request(self, method, path, cancel, limit, sink=None):
        _check(cancel)
        if not ((method == 'GET' and path in ('/results', '/recording-list')) or
                (method == 'GET' and path.startswith('/recording/') and _filename(path[11:])) or
                (method == 'POST' and re.fullmatch(r'/recording-flush\?duration=(?:[1-9][0-9]{0,2})', path))):
            raise RewindError('Rewind request outside allowlist')
        parsed = urlsplit(self.config.origin)
        connection = http.client.HTTPConnection(parsed.hostname, 5807, timeout=self.config.timeout_seconds)
        aborted = threading.Event()
        with self._lock:
            if self._active is not None:
                raise RewindError('Concurrent Rewind request rejected')
            self._active = connection
            self._abort_event = aborted
        started = time.monotonic()
        response_timeout = self.config.download_deadline_seconds if sink is not None else self.config.timeout_seconds
        original_cancel = cancel
        cancel = lambda: aborted.is_set() or original_cancel()
        connection.response_class = lambda sock, **kwargs: http.client.HTTPResponse(
            _ResponseSocket(sock, cancel, started + response_timeout, self.config.timeout_seconds), **kwargs)
        data = bytearray()
        count = 0
        digest = hashlib.sha256()
        response = None
        try:
            connection.request(method, path, body=b'' if method == 'POST' else None,
                               headers={'Accept-Encoding': 'identity', 'Connection': 'close'})
            _check(cancel)
            response = connection.getresponse()
            if response.status != 200:
                raise RewindError('Rewind HTTP response rejected')
            lengths = response.headers.get_all('Content-Length', [])
            if len(lengths) > 1:
                raise RewindError('Ambiguous Rewind response length')
            expected = None
            if lengths:
                if not re.fullmatch(r'[0-9]{1,20}', lengths[0]):
                    raise RewindError('Invalid Rewind response length')
                expected = int(lengths[0])
                if expected > limit:
                    raise RewindError('Rewind response exceeds byte bound')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise RewindError('Encoded Rewind response rejected')
            transfers = response.headers.get_all('Transfer-Encoding', [])
            if transfers and (lengths or len(transfers) != 1 or transfers[0].lower() != 'chunked'):
                raise RewindError('Ambiguous or unsupported Rewind transfer framing')
            while True:
                _check(cancel)
                if time.monotonic() - started > response_timeout:
                    raise RewindError('Rewind response deadline exceeded')
                # read1 returns on available data so permission is rechecked each chunk.
                chunk = response.read1(min(65536, limit - count + 1))
                if time.monotonic() - started > response_timeout:
                    raise RewindError('Rewind response deadline exceeded')
                if not chunk:
                    break
                _check(cancel)
                count += len(chunk)
                if count > limit:
                    raise RewindError('Rewind response exceeds byte bound')
                digest.update(chunk)
                if sink is None:
                    data.extend(chunk)
                else:
                    sink.write(chunk)
            if expected is not None and count != expected:
                raise RewindError('Truncated Rewind response')
            _check(cancel)
            return bytes(data) if sink is None else {'size_bytes': count, 'sha256': digest.hexdigest()}
        except (OSError, http.client.HTTPException, ValueError):
            _check(cancel)
            raise RewindError('Rewind HTTP transfer failed') from None
        finally:
            if response is not None:
                response.close()
            connection.close()
            with self._lock:
                self._active = None
                self._abort_event = None

    def state(self, cancel=lambda: False):
        value = _json(self._request('GET', '/results', cancel, self.config.max_json_bytes))
        if not isinstance(value, dict):
            raise RewindError('Invalid Rewind results object')
        body = value.get('Results', value)
        rewind = body.get('rewind') if isinstance(body, dict) else None
        if not isinstance(rewind, dict) or type(rewind.get('flushing')) not in (int, bool) or rewind['flushing'] not in (0, 1):
            raise RewindError('Rewind flushing status unavailable')
        _number(rewind.get('storedSeconds'), 0, 86400, 'storedSeconds')
        return value

    @staticmethod
    def is_flushing(state):
        return bool(state.get('Results', state)['rewind']['flushing'])

    def flush(self, duration_seconds=None, cancel=lambda: False):
        duration = self.config.duration_seconds if duration_seconds is None else duration_seconds
        _integer(duration, 1, 500, 'duration')
        value = _json(self._request('POST', '/recording-flush?duration=' + str(duration), cancel, self.config.max_json_bytes))
        if not isinstance(value, dict) or type(value.get('success')) is not bool:
            raise RewindError('Rewind flush response invalid')
        return value

    def list_recordings(self, cancel=lambda: False):
        value = _json(self._request('GET', '/recording-list', cancel, self.config.max_json_bytes))
        if not isinstance(value, list) or len(value) > self.config.list_limit:
            raise RewindError('Rewind recording list schema/bound invalid')
        entries = []
        seen = set()
        for entry in value:
            if not isinstance(entry, dict) or not {'video', 'manifest', 'bootlog', 'size'} <= entry.keys():
                raise RewindError('Invalid Rewind recording entry')
            video, manifest, bootlog = entry['video'], entry['manifest'], entry['bootlog']
            _filename(video)
            _filename(manifest)
            if not video.endswith('.avi') or manifest != video[:-4] + '_manifest.jsonl':
                raise RewindError('Rewind group filename mismatch')
            if bootlog is not None:
                _filename(bootlog)
                if bootlog != video[:-4] + '_bootlog.txt.gz':
                    raise RewindError('Rewind bootlog filename mismatch')
            _integer(entry['size'], 1, self.config.max_recording_bytes, 'listed size')
            if video in seen:
                raise RewindError('Duplicate Rewind recording name')
            seen.add(video)
            entries.append(entry)
        return entries

    def download(self, filename, destination, cancel=lambda: False, max_bytes=None):
        _filename(filename)
        limit = self.config.max_recording_bytes if max_bytes is None else max_bytes
        _integer(limit, 1, self.config.max_recording_bytes, 'download byte bound')
        # Exclusive create prevents accidental mutation of any published evidence.
        with Path(destination).open('xb') as stream:
            result = self._request('GET', '/recording/' + filename, cancel, limit, sink=stream)
            stream.flush()
            os.fsync(stream.fileno())
        return result


def install_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS rewind_recordings (
        recording_id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, robot_id TEXT NOT NULL,
        source_type TEXT NOT NULL, metadata_json TEXT NOT NULL,
        relative_path TEXT NOT NULL UNIQUE, created_ns INTEGER NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS rewind_observations (
        observation_key TEXT PRIMARY KEY, camera_scope TEXT NOT NULL,
        recording_id TEXT NOT NULL, checked_ns INTEGER NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS rewind_camera_context (
        camera_scope TEXT PRIMARY KEY, last_ts_us REAL)''')


def _sync_directory(path):
    # Windows does not support opening directories through os.open; file fsync +
    # same-volume atomic directory rename protects process-crash publication.
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _hash_file(path, cancel):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        while True:
            _check(cancel)
            chunk = stream.read(65536)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {'size_bytes': size, 'sha256': digest.hexdigest()}


class RewindArchive:
    """Single-worker archive. SQLite connection must belong to calling worker.

    Directory publication precedes SQLite registration. recover() verifies and
    registers orphan published groups after process interruption; staged partial
    attempts remain unavailable. No source-deletion method exists.
    """
    def __init__(self, root, connection, config, transport=None):
        if not isinstance(config, RewindConfig):
            raise RewindError('Explicit validated Rewind configuration required')
        self.root = Path(root).resolve()
        self.groups = self.root / 'rewind' / 'groups'
        self.staging = self.root / 'rewind' / 'partial'
        self.groups.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        if any(path.is_symlink() for path in (self.root / 'rewind', self.groups, self.staging)):
            raise RewindError('Rewind archive directory path invalid')
        self.connection, self.config = connection, config
        self.transport = transport or RewindHttpTransport(config)
        install_schema(connection)
        self._cursor = 0
        self._scope = hashlib.sha256(_canonical({'camera_id':config.camera_id,
            'robot_id':config.robot_id, 'source_type':config.source_type, 'origin':config.origin})).hexdigest()

    def _register(self, metadata, directory):
        encoded = _canonical(metadata).decode('utf-8')
        existing = self.connection.execute('SELECT metadata_json FROM rewind_recordings WHERE recording_id=?',
                                           (metadata['recording_id'],)).fetchone()
        if existing is not None:
            if existing[0] != encoded:
                raise RewindError('Rewind immutable registration conflict')
            return False
        with self.connection:
            self.connection.execute('INSERT INTO rewind_recordings VALUES(?,?,?,?,?,?,?)',
                (metadata['recording_id'], metadata['camera_id'], metadata['robot_id'], metadata['source_type'],
                 encoded, str(directory.relative_to(self.root)).replace('\\', '/'), metadata['created_ns']))
        return True

    def _manifest(self, path, cancel):
        count = 0
        # Each line corresponds to one metadata frame, not proof of AVI decode.
        with path.open('rb') as stream:
            while True:
                _check(cancel)
                line = stream.readline(self.config.max_json_bytes + 1)
                if not line:
                    break
                if len(line) > self.config.max_json_bytes or not line.endswith(b'\n'):
                    raise RewindError('Rewind manifest line bound/completeness invalid')
                if not isinstance(_json(line), dict):
                    raise RewindError('Rewind manifest frame must be an object')
                count += 1
                if count > 1000000:
                    raise RewindError('Rewind manifest frame bound exceeded')
        if not count:
            raise RewindError('Empty Rewind manifest')
        return count

    def _observation_key(self, entry):
        return hashlib.sha256(_canonical({'scope':self._scope, 'entry':entry})).hexdigest()

    def _observed(self, entry, recording_id):
        with self.connection:
            self.connection.execute('INSERT OR REPLACE INTO rewind_observations VALUES(?,?,?,?)',
                (self._observation_key(entry), self._scope, recording_id, time.time_ns()))

    def collect(self, gate, cancel=lambda: False):
        def stop():
            return cancel() or not gate()
        _check(stop)
        before = self.transport.state(stop)
        if self.transport.is_flushing(before):
            return []
        entries = self.transport.list_recordings(stop)
        body = before.get('Results', before)
        ts_us = body.get('ts_us')
        # An observed application clock rollback invalidates discovery shortcuts;
        # lack of rollback does not establish a stable boot identity.
        if type(ts_us) in (int, float) and math.isfinite(ts_us) and ts_us >= 0:
            previous = self.connection.execute('SELECT last_ts_us FROM rewind_camera_context WHERE camera_scope=?',
                                               (self._scope,)).fetchone()
            with self.connection:
                if previous and previous[0] > ts_us:
                    self.connection.execute('DELETE FROM rewind_observations WHERE camera_scope=?', (self._scope,))
                self.connection.execute('INSERT OR REPLACE INTO rewind_camera_context VALUES(?,?)', (self._scope, ts_us))
        now = time.time_ns()
        pending = []
        for entry in entries:
            observation = self.connection.execute('SELECT recording_id,checked_ns FROM rewind_observations WHERE observation_key=?',
                                                  (self._observation_key(entry),)).fetchone()
            if observation is None or now < observation[1] or now - observation[1] >= self.config.revalidate_seconds * 1e9:
                pending.append(entry)
        entries = pending
        if not entries:
            return []
        result = []
        # Rotate bounded work so a long list does not starve later recordings.
        start = self._cursor % len(entries)
        chosen = (entries[start:] + entries[:start])[:self.config.max_recordings_per_poll]
        self._cursor = (start + len(chosen)) % len(entries)
        for entry in chosen:
            _check(stop)
            key = self._observation_key(entry)
            stage = self.staging / key
            stage.mkdir(exist_ok=True)
            if stage.is_symlink():
                raise RewindError('Rewind partial path invalid')
            # Restart unsupported remote resume from zero; never touch closed groups.
            for old in stage.iterdir():
                if not old.is_file() or old.is_symlink():
                    raise RewindError('Unexpected Rewind partial artifact')
                old.unlink()
            files = {}
            remaining = self.config.max_recording_bytes
            for role in ('video', 'manifest', 'bootlog'):
                name = entry[role]
                if name is None:
                    continue
                if remaining < 1:
                    raise RewindError('Rewind group exceeds byte bound')
                info = self.transport.download(name, stage / name, stop, remaining)
                remaining -= info['size_bytes']
                files[role] = {'name':name, **info}
            if sum(item['size_bytes'] for item in files.values()) != entry['size']:
                raise RewindError('Rewind combined size mismatch')
            if files['video']['size_bytes'] == 0:
                raise RewindError('Empty Rewind video')
            frames = self._manifest(stage / entry['manifest'], stop)
            after = self.transport.state(stop)
            if self.transport.is_flushing(after):
                raise RewindError('Rewind source started flushing during transfer')
            current = self.transport.list_recordings(stop)
            if entry not in current:
                raise RewindError('Rewind source entry changed during transfer')
            identity = {'camera_id':self.config.camera_id, 'robot_id':self.config.robot_id,
                        'source_type':self.config.source_type, 'files':files}
            recording_id = hashlib.sha256(_canonical(identity)).hexdigest()
            final = self.groups / recording_id
            if final.exists():
                # Existing content is verified before treating observation as duplicate.
                existing = self._verify_directory(final, stop)
                self._register(existing, final)
                self._observed(entry, recording_id)
                for duplicate in stage.iterdir():
                    duplicate.unlink()
                stage.rmdir()
                continue
            metadata = {'schema_version':1, 'recording_id':recording_id, **identity,
                'created_ns':time.time_ns(), 'remote_entry':entry,
                'camera_state_before':before, 'camera_state_after':after,
                'manifest_frame_count':frames, 'decoded_video_frame_count':None,
                'remote_identity_strength':'content-observed-no-remote-version',
                'remote_content_identity_qualified':False,
                'discovery_cache_revalidation_seconds':self.config.revalidate_seconds,
                'discovery_cache_limit':'same-name/same-size replacement detectable only on content revalidation',
                'camera_boot_identity':None, 'boot_context_available':'bootlog' in files,
                'physical_camera_qualified':False, 'video_alignment_qualified':False,
                'timestamp_alignment_qualified':False, 'state':'verified-original-bytes'}
            with (stage / 'metadata.json').open('xb') as stream:
                stream.write(_canonical(metadata))
                stream.flush()
                os.fsync(stream.fileno())
            _check(stop)
            _sync_directory(stage)
            stage.rename(final)
            _sync_directory(self.groups)
            if self._register(metadata, final):
                result.append(metadata)
            self._observed(entry, recording_id)
        return result

    def _verify_directory(self, directory, cancel):
        path = directory / 'metadata.json'
        if directory.is_symlink() or path.is_symlink():
            raise RewindError('Rewind archive path invalid')
        with path.open('rb') as stream:
            raw = stream.read(self.config.max_json_bytes * 3 + 1)
        if len(raw) > self.config.max_json_bytes * 3:
            raise RewindError('Rewind archive metadata bound exceeded')
        metadata = _json(raw)
        try:
            identity = {key:metadata[key] for key in ('camera_id','robot_id','source_type','files')}
            if (metadata['recording_id'] != directory.name or
                    hashlib.sha256(_canonical(identity)).hexdigest() != directory.name or
                    set(metadata['files']) - {'video','manifest','bootlog'} or
                    not {'video','manifest'} <= set(metadata['files'])):
                raise RewindError('Rewind archive identity mismatch')
            _integer(metadata['created_ns'], 1, 2**63-1, 'archive time')
            total = 0
            for info in metadata['files'].values():
                name = _filename(info['name'])
                file = directory / name
                if file.is_symlink() or _hash_file(file, cancel) != {key:info[key] for key in ('size_bytes','sha256')}:
                    raise RewindError('Rewind archive file hash mismatch')
                total += info['size_bytes']
                if total > self.config.max_recording_bytes:
                    raise RewindError('Rewind archive exceeds byte bound')
            return metadata
        except (KeyError, TypeError, ValueError, OSError):
            raise RewindError('Rewind archive metadata invalid') from None

    def recover(self, cancel=lambda: False):
        count = 0
        for index, directory in enumerate(self.groups.iterdir()):
            _check(cancel)
            if index >= 10000:
                raise RewindError('Rewind recovery group bound exceeded')
            if not re.fullmatch(r'[a-f0-9]{64}', directory.name) or not directory.is_dir():
                raise RewindError('Unexpected Rewind published artifact')
            metadata = self._verify_directory(directory, cancel)
            count += self._register(metadata, directory)
        return count

    def list_records(self, limit=100):
        _integer(limit, 1, 1000, 'record list limit')
        return [_json(row[0].encode('utf-8')) for row in self.connection.execute(
            'SELECT metadata_json FROM rewind_recordings ORDER BY created_ns DESC, recording_id LIMIT ?', (limit,))]

    def get_record(self, recording_id):
        if not isinstance(recording_id, str) or not re.fullmatch(r'[a-f0-9]{64}', recording_id):
            raise RewindError('Invalid Rewind recording id')
        row = self.connection.execute('SELECT metadata_json FROM rewind_recordings WHERE recording_id=?',
                                      (recording_id,)).fetchone()
        return None if row is None else _json(row[0].encode('utf-8'))

    def file_path(self, recording_id, role):
        metadata = self.get_record(recording_id)
        if metadata is None or role not in ('video','manifest','bootlog') or role not in metadata['files']:
            raise RewindError('Rewind original unavailable')
        directory = self.groups / recording_id
        self._verify_directory(directory, lambda: False)
        return directory / _filename(metadata['files'][role]['name'])
