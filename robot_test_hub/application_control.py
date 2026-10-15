"""Private loopback application lifecycle; never terminate an OS process.

The runtime is a credential, not a PID kill file. Schema 2 crash recovery uses
an OS advisory lease held through worker cleanup. Older or ambiguous records
remain manual; inability to contact a port never proves its owner exited.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import stat
from pathlib import Path
import secrets
import tempfile


class ControlError(ValueError):
    pass


class ApplicationControl:
    def __init__(self, path, shutdown):
        self.path = Path(path).absolute()
        self.shutdown = shutdown
        self.token = secrets.token_hex(32)
        self.instance_id = secrets.token_hex(16)
        self.payload = None
        self.lease_path = self.path.with_name(self.path.name + ".lease")
        self._lease_fd = None

    @property
    def lease_held(self):
        return self._lease_fd is not None

    @staticmethod
    def _regular(info):
        return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and not getattr(info, 'st_file_attributes', 0) &
                getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))

    def _verify_lease(self):
        if self._lease_fd is None:
            raise ControlError("Private application lease is not held.")
        actual = self.lease_path.lstat()
        opened = os.fstat(self._lease_fd)
        if (not self._regular(actual) or not self._regular(opened)
                or (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ControlError("Private lease file changed; preserve it for local inspection.")

    def _acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = None
        try:
            if os.path.lexists(self.lease_path) and not self._regular(self.lease_path.lstat()):
                raise ControlError("Private lease must be a regular unlinked local file.")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOINHERIT', 0)
            fd = os.open(self.lease_path, flags, 0o600)
            if not self._regular(os.fstat(fd)):
                raise ControlError("Private lease must be a regular unlinked local file.")
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lease_fd, fd = fd, None
            self._verify_lease()
            # Windows byte-range locking permits locking beyond EOF. Never
            # initialize before locking, which could alter another owner's file.
            if os.fstat(self._lease_fd).st_size == 0:
                os.write(self._lease_fd, b'\0')
                os.fsync(self._lease_fd)
        except (OSError, ControlError):
            if fd is not None:
                os.close(fd)
            self.close()
            raise ControlError("Private application lease unavailable; another hub may still own it. Check local file permissions or preserved records.") from None

    @staticmethod
    def _recognized(raw):
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError()
                value[key] = item
            return value
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
        return (isinstance(value, dict)
                and value.keys() == {'schema_version', 'port', 'pid', 'token', 'instance_id'}
                and type(value['schema_version']) is int and value['schema_version'] == 2
                and type(value['port']) is int and 1 <= value['port'] <= 65535
                and type(value['pid']) is int and 0 < value['pid'] < 2**32
                and isinstance(value['token'], str) and re.fullmatch('[a-f0-9]{64}', value['token']) is not None
                and isinstance(value['instance_id'], str) and re.fullmatch('[a-f0-9]{32}', value['instance_id']) is not None)

    def check_available(self):
        """Acquire ownership before any source/archive opens; reclaim only schema 2."""
        if self.lease_held:
            self._verify_lease()
            return
        self._acquire()
        try:
            if not os.path.lexists(self.path):
                return
            info = self.path.lstat()
            if not self._regular(info) or info.st_size > 65536:
                raise ValueError()
            with self.path.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                raw = stream.read(65537)
            if (not self._regular(opened) or len(raw) > 65536
                    or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
                    or not self._recognized(raw)):
                raise ValueError()
            self._verify_lease()
            current = self.path.lstat()
            with self.path.open('rb') as stream:
                current_raw = stream.read(65537)
            if (not self._regular(current)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                    or current_raw != raw):
                raise ValueError()
            self.path.unlink()
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            self.close()
            raise ControlError("Private runtime record is legacy, invalid or changed; preserve it for local inspection before restarting.") from None

    def close(self):
        """Release only after all workers close. Never delete the stable lease file."""
        fd, self._lease_fd = self._lease_fd, None
        if fd is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def publish(self, port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ControlError("Private runtime requires a valid loopback port.")
        if self.payload is not None:
            raise ControlError("Private runtime is already published by this instance.")
        self.check_available()
        self._verify_lease()
        value = dict(schema_version=2, port=port, pid=os.getpid(),
                     token=self.token, instance_id=self.instance_id)
        payload = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".hub-runtime-", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Exclusive, fully written publication; never replace an existing
            # owner. Supported on local NTFS and the tested POSIX filesystems.
            os.link(temporary, self.path)
            self.payload = payload
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self):
        if self.payload is None or not self.lease_held:
            return False
        try:
            self._verify_lease()
            if not self._regular(self.path.lstat()):
                return False
            with self.path.open('rb') as stream:
                current = stream.read(65537)
            if current != self.payload:
                return False
            self.path.unlink()
            self.payload = None
            return True
        except (OSError, ControlError):
            return False

    def request(self, method, path, headers):
        """Return a bounded response; request headers are never echoed."""
        if method not in ("GET", "POST") or path != (
                "/api/v1/application" if method == "GET" else "/api/v1/application/stop"):
            return 404, {"error_code": "application_route_not_found"}
        auth = headers.get_all("Authorization", [])
        # Compare bytes, avoiding compare_digest's non-ASCII string exception.
        if len(auth) != 1 or not hmac.compare_digest(
                auth[0].encode("utf-8"), ("Bearer " + self.token).encode("ascii")):
            return 403, {"error_code": "application_control_denied"}
        lengths = headers.get_all("Content-Length", [])
        if (headers.get_all("Transfer-Encoding") or lengths not in ([], ["0"])
                or headers.get_all("Origin") or len(headers.get_all("Host", [])) != 1):
            return 400, {"error_code": "invalid_application_request"}
        if method == "POST":
            if lengths != ["0"] or headers.get_all("X-Hub-Request", []) != ["1"]:
                return 400, {"error_code": "invalid_application_request"}
            self.shutdown.set()
        return (202 if method == "POST" else 200), dict(
            schema_version=1, instance_id=self.instance_id, pid=os.getpid(),
            state="stopping" if self.shutdown.is_set() else "running")
