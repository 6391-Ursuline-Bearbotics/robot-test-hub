"""Foreground service with independent status, transfer, and API access."""
from __future__ import annotations

import copy
import math
from pathlib import Path
import queue
import threading
import time

from .collector import Collector, RobotStatus, Source
from .config import Config
from .diagnostics import Diagnostics
from .storage import DataRootOwner, open_catalog


class CachedSource:
    def __init__(self, source: Source):
        self.source = source
        self.lock = threading.Lock()
        self.latest = RobotStatus(None, 0, "unknown", -1)

    def status(self):
        with self.lock:
            return self.latest

    def update(self, status):
        with self.lock:
            self.latest = status

    def list_closed_files(self):
        return self.source.list_closed_files()

    def read(self, *args):
        return self.source.read(*args)


class HubService:
    def __init__(self, config: Config, source: Source, *, collector_factory=Collector):
        self.config, self.source = config, source
        self.root = Path(config.data_dir).resolve()
        self.owner = DataRootOwner(self.root)
        try:
            self.settings_db = open_catalog(self.root)
            self.diagnostics = Diagnostics(self.root)
        except BaseException:
            if hasattr(self, "settings_db"):
                self.settings_db.close()
            self.owner.close()
            raise
        self.settings_lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.preference_lock = threading.Lock()
        self.preference_generation = 0
        self.paused = threading.Event()
        row = self.settings_db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
        if row is not None and row[0] == "1":
            self.paused.set()
        self.stop = threading.Event()
        self.cached_source = CachedSource(source)
        self.commands = queue.Queue()
        self.collector_factory = collector_factory
        self.cache = {"state": "starting", "reason": "Opening catalog and recovering checkpoints", "error": None,
                      "files": [], "pending_files": 0, "completed_files": 0, "remaining_bytes": 0,
                      "bytes_per_second": None, "eta_seconds": None, "active_id": None, "discovery_complete": False}
        self.health = {"collector": {"state": "starting"}, "status": {"state": "starting"}}
        self.started = False
        self.closed = False
        self.threads = []
        self.diagnostics.record("service_initialized", "Demo service initialized; no robot connection")

    def _health(self, worker, state, code=None):
        with self.cache_lock:
            self.health[worker] = {"state": state, "error_code": code}

    def _preference(self):
        with self.preference_lock:
            return self.paused.is_set(), self.preference_generation

    def _publish(self, snapshot):
        # Never export arbitrary transport exceptions or legacy error text.
        if snapshot["error"]:
            snapshot["error"] = "Collection interrupted; see diagnostics and check source/storage"
        for row in snapshot["files"]:
            if row.get("error"):
                row["error"] = "File needs retry or investigation"
        snapshot["snapshot_monotonic"] = time.monotonic()
        with self.cache_lock:
            self.cache = snapshot

    def start(self):
        if self.started or self.closed:
            raise RuntimeError("Service cannot be started twice or after close")
        self.started = True
        self.threads = [threading.Thread(target=self._status_worker, name="hub-status"),
                        threading.Thread(target=self._collector_worker, name="hub-collector")]
        for thread in self.threads:
            thread.start()

    def _status_worker(self):
        self._health("status", "running")
        try:
            while not self.stop.is_set():
                self.cached_source.update(self.source.status())
                self.stop.wait(self.config.status_interval)
        except Exception as exc:
            self.cached_source.update(RobotStatus(None, 0, "unknown", -1))
            self._health("status", "failed", "status_worker_failed")
            self.diagnostics.record("status_worker_failed", "Status worker failed; collection blocked. Restart the hub after checking the source adapter", exception=exc)
        finally:
            with self.cache_lock:
                if self.health["status"]["state"] != "failed":
                    self.health["status"] = {"state": "stopped", "error_code": None}

    def _collector_worker(self):
        collector = None
        try:
            collector = self.collector_factory(self.root, self.cached_source, idle_delay=self.config.idle_delay,
                freshness=self.config.freshness, chunk_size=self.config.chunk_size, owner=self.owner,
                paused=self._preference, stopping=self.stop.is_set, publish=self._publish)
            self._health("collector", "running")
            self._publish(collector.snapshot())
            last_error = None
            while not self.stop.is_set():
                while not self.commands.empty():
                    if self.commands.get_nowait() == "retry":
                        collector.retry_errors()
                collector.tick()
                snapshot = collector.snapshot()
                if snapshot["error"] and snapshot["error"] != last_error:
                    self.diagnostics.record("collection_interrupted", "Collection interrupted; check source connectivity, manifest identity, and local storage")
                last_error = snapshot["error"]
                self._publish(snapshot)
                self.stop.wait(self.config.tick_interval)
        except Exception as exc:
            self._health("collector", "failed", "collector_worker_failed")
            self.diagnostics.record("collector_worker_failed", "Collector worker failed; progress saved. Restart the hub after checking local storage/source adapter", exception=exc)
        finally:
            if collector is not None:
                try:
                    collector.close()
                except Exception as exc:
                    self._health("collector", "failed", "collector_close_failed")
                    self.diagnostics.record("collector_close_failed", "Catalog close failed; preserve the data directory and restart the hub", exception=exc)
            with self.cache_lock:
                if self.health["collector"]["state"] != "failed":
                    self.health["collector"] = {"state": "stopped", "error_code": None}

    def set_paused(self, value: bool):
        if type(value) is not bool:
            raise ValueError("Expected a boolean")
        with self.settings_lock:
            if self.closed or self.stop.is_set():
                raise ValueError("Service is stopping")
            with self.settings_db:
                self.settings_db.execute("INSERT OR REPLACE INTO settings VALUES ('paused',?)", ("1" if value else "0",))
            # A successful response means the preference is committed locally.
            with self.preference_lock:
                if self.paused.is_set() != value:
                    self.paused.set() if value else self.paused.clear()
                    self.preference_generation += 1
        self.diagnostics.record("operator_pause" if value else "operator_resume", "Operator collection preference saved")

    def retry_errors(self):
        if self.stop.is_set():
            raise ValueError("Service is stopping")
        self.commands.put("retry")

    def snapshot(self):
        with self.cache_lock:
            result = copy.deepcopy(self.cache)
            health = copy.deepcopy(self.health)
        status = self.cached_source.status()
        age = time.monotonic() - status.observed_at
        snapshot_age = time.monotonic() - result.get("snapshot_monotonic", time.monotonic())
        fresh = math.isfinite(age) and 0 <= age <= self.config.freshness and status.enabled is not None
        result.update({"schema_version": 1, "source_type": "synthetic_demo", "workers": health,
                       "paused_by_operator": self.paused.is_set(), "snapshot_age_seconds": max(0, snapshot_age), "source_status": {
                           "enabled": status.enabled, "fresh": fresh, "age_seconds": age if math.isfinite(age) and age >= 0 else None}})
        if health["collector"]["state"] == "failed":
            result.update(state="attention", reason="Collector worker failed; restart the hub", error=health["collector"]["error_code"])
        elif health["status"]["state"] == "failed":
            result.update(state="attention", reason="Status worker failed; collection blocked", error=health["status"]["error_code"])
        elif self.stop.is_set():
            result.update(state="stopping", reason="Stopping workers; preserving checkpoints")
        elif self.paused.is_set():
            result.update(state="paused", reason="Paused by operator; preference saved")
        elif not fresh or status.enabled:
            result.update(state="paused", reason="Robot enabled" if fresh and status.enabled else "Robot status unknown or stale")
        if result["state"] in ("paused", "attention", "stopping", "discovering", "verifying", "starting"):
            result.update(bytes_per_second=None, eta_seconds=None)
        return result

    def close(self) -> bool:
        if self.closed:
            return True
        self.stop.set()
        cancel = getattr(self.source, "cancel", None)
        if cancel is not None:
            try:
                cancel()
            except Exception as exc:
                self.diagnostics.record("source_cancel_failed", "Source cancellation failed; waiting for outstanding I/O", exception=exc)
        deadline = time.monotonic() + self.config.shutdown_timeout
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            self.diagnostics.record("shutdown_timeout", "Worker still has outstanding I/O; ownership retained. Adapter must provide bounded I/O/cancellation")
            return False
        with self.settings_lock:
            self.settings_db.close()
            self.closed = True
        self.diagnostics.record("service_stopped", "Workers stopped; committed checkpoints preserved")
        self.diagnostics.close()
        self.owner.close()
        return True
