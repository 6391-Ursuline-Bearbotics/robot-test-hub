"""Private loopback application lifecycle; never terminate an OS process.

The runtime is a credential, not a PID kill file. A stale record is retained for
operator inspection; inability to contact a port never proves its owner exited.
"""
from __future__ import annotations

import hmac
import json
import os
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

    def check_available(self):
        # lexists also rejects a dangling symlink. The private parent must be
        # controlled by the local account, not a shared/cloud-synced directory.
        if os.path.lexists(self.path):
            raise ControlError("Private runtime record already exists; stop the existing hub or inspect a stale record before retrying.")

    def publish(self, port):
        self.check_available()
        value = dict(schema_version=1, port=port, pid=os.getpid(),
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
        if self.payload is None:
            return False
        try:
            if self.path.is_symlink() or self.path.read_bytes() != self.payload:
                return False
            self.path.unlink()
            self.payload = None
            return True
        except OSError:
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
