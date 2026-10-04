"""Bounded local JSON diagnostics. Exception text/tracebacks are never exported."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import re
import threading

from . import __version__


def redact(value):
    if isinstance(value, dict):
        return {key: "[redacted]" if re.search(r"password|secret|token|credential|authorization|api.?key", str(key), re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(\w+://)[^/\s@]+@", r"\1[redacted]@", value)
        value = re.sub(r"(?i)(password|secret|token|api.?key)\s*[=:]\s*[^\s,;&]+", r"\1=[redacted]", value)
        value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", value)
    return value


class Diagnostics:
    def __init__(self, root: Path):
        folder = root / "diagnostics"
        folder.mkdir(exist_ok=True)
        self.handler = RotatingFileHandler(folder / "service.jsonl", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        self.lock = threading.Lock()
        self.events = []

    def record(self, code: str, message: str, *, exception: BaseException | None = None):
        event = {"schema_version": 1, "utc": datetime.now(timezone.utc).isoformat(),
                 "code": code, "message": message}
        if exception is not None:
            # Arbitrary exception text may contain unlabelled secrets.
            event["exception_type"] = type(exception).__name__
        event = redact(event)
        with self.lock:
            self.events.append(event)
            self.events = self.events[-100:]
            self.handler.emit(logging.LogRecord("hub", logging.INFO, "", 0, json.dumps(event), (), None))

    def snapshot(self):
        with self.lock:
            return {"schema_version": 1, "service_version": __version__, "python_version": platform.python_version(),
                    "events": list(self.events)}

    def close(self):
        self.handler.close()
