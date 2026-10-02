"""Synthetic source. Bytes are NOT WPILOG data and no robot is contacted."""
from __future__ import annotations

import hashlib
import threading
import time
from .collector import LogFile, RobotStatus


class DemoSource:
    def __init__(self):
        self.lock = threading.Lock()
        self.enabled = False
        self.connected = True
        self.stale = False
        self.generation = 0
        self.speed_mib = 4.0
        self.last_status = time.monotonic()
        self.files = {}
        self.manifest = []
        for index, mib in enumerate((8, 12, 16), 1):
            data = bytes([index]) * (mib * 1024 * 1024)
            identity = f"demo-v1-{index}"
            self.files[identity] = data
            self.manifest.append(LogFile(identity, f"Synthetic run {index}.demo", len(data), hashlib.sha256(data).hexdigest(), float(index)))

    def configure(self, action: str, value=None) -> None:
        with self.lock:
            if action in ("enabled", "connected", "stale"):
                if type(value) is not bool:
                    raise ValueError("Expected a boolean")
                if getattr(self, action) != value:
                    setattr(self, action, value)
                    self.generation += 1
            elif action == "speed_mib":
                speed = float(value)
                if not 0.25 <= speed <= 32:
                    raise ValueError("Speed must be between 0.25 and 32 MiB/s")
                self.speed_mib = speed
            else:
                raise ValueError("Unknown demo control")

    def snapshot(self) -> dict:
        with self.lock:
            return {"enabled": self.enabled, "connected": self.connected,
                    "stale": self.stale, "speed_mib": self.speed_mib}

    def status(self) -> RobotStatus:
        with self.lock:
            if self.connected and not self.stale:
                self.last_status = time.monotonic()
            return RobotStatus(self.enabled if self.connected else None,
                               self.last_status, "demo-boot-v1", self.generation)

    def list_closed_files(self) -> list[LogFile]:
        with self.lock:
            if not self.connected:
                raise OSError("Demo connection lost")
            return list(self.manifest)

    def read(self, file_id: str, offset: int, length: int) -> bytes:
        with self.lock:
            generation = self.generation
            speed = self.speed_mib * 1024 * 1024
        # This source enforces cancellation itself, including during a slow read.
        deadline = time.monotonic() + length / speed
        while time.monotonic() < deadline:
            with self.lock:
                if self.enabled or not self.connected or self.stale or self.generation != generation:
                    raise OSError("Demo read interrupted by robot state")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        with self.lock:
            if self.enabled or not self.connected or self.stale or self.generation != generation:
                raise OSError("Demo read interrupted by robot state")
            return self.files[file_id][offset:offset + length]
