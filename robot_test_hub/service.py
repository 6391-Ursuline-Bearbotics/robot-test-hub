"""Foreground service with independent status, transfer, and API access."""
from __future__ import annotations

import copy
import math
import inspect
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
        self.cancellation = None
        self.last_sequence = None
        self.sequence_boot = None

    def status(self):
        with self.lock:
            return self.latest

    def update(self, status):
        valid = (isinstance(status, RobotStatus) and type(status.enabled) in (bool, type(None))
                 and type(status.observed_at) in (int, float) and math.isfinite(status.observed_at)
                 and isinstance(status.boot_id, str) and bool(status.boot_id)
                 and type(status.generation) is int and status.generation >= 0
                 and type(status.transfer_allowed) is bool
                 and isinstance(status.link_profile, str) and bool(status.link_profile))
        if not valid:
            status = RobotStatus(None, 0, "unknown", -1)
        with self.lock:
            if status.sequence is not None:
                valid_sequence = type(status.sequence) is int and status.sequence >= 0
                if status.boot_id == self.sequence_boot and self.last_sequence is not None:
                    valid_sequence = valid_sequence and status.sequence > self.last_sequence
                if not valid_sequence:
                    # A changed or malformed replay makes permission ambiguous.
                    previous = self.latest
                    if (type(status.sequence) is not int or status.sequence < 0
                            or (status.enabled, status.generation, status.transfer_allowed) !=
                            (previous.enabled, previous.generation, previous.transfer_allowed)):
                        self.latest = RobotStatus(None, previous.observed_at, previous.boot_id, previous.generation)
                        if self.cancellation is not None:
                            self.cancellation.cancel()
                    return
                self.last_sequence, self.sequence_boot = status.sequence, status.boot_id
            previous = self.latest
            self.latest = status
            if self.cancellation is not None and (status.enabled is not False or not status.transfer_allowed
                    or (status.boot_id, status.generation) != (previous.boot_id, previous.generation)):
                self.cancellation.cancel()

    def bind_cancellation(self, cancellation):
        with self.lock:
            self.cancellation = cancellation
            latest = self.latest
            if (latest.enabled is not False or not latest.transfer_allowed or
                    (latest.boot_id, latest.generation) != cancellation.generation[0]):
                cancellation.cancel()

    def __getattr__(self, name):
        if name == 'discover_closed':
            return getattr(self.source, name)
        raise AttributeError(name)

    def list_closed_files(self):
        return self.source.list_closed_files()

    def read(self, file_id, offset, length, *, permission_generation=None, cancellation=None):
        read = self.source.read
        if 'cancellation' in inspect.signature(read).parameters:
            return read(file_id, offset, length, permission_generation=permission_generation, cancellation=cancellation)
        return read(file_id, offset, length)


class HubService:
    def __init__(self, config: Config, source: Source, *, collector_factory=Collector,
                 video_config=None, recorder_factory=None, video_media_config=None, media_adapter_factory=None,
                 marker_config=None, marker_status_factory=None, marker_transport_factory=None):
        from .marker_service import MarkerWorker, MarkerConfig
        if marker_config is not None and not isinstance(marker_config,MarkerConfig):
            raise ValueError("Explicit validated marker configuration required")
        from .recorder import FFmpegConfig, Recorder
        from .recorder_service import RecorderWorker
        from .video_media import MediaToolsConfig, MediaWorker, NativeMediaAdapter
        if video_media_config is not None and not isinstance(video_media_config,MediaToolsConfig):
            raise ValueError('Explicit validated local media tools configuration required')
        if video_config is not None and not isinstance(video_config,FFmpegConfig):
            raise ValueError('Explicit validated private video configuration required')
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
        self.backup_cache = {"schema_version": 1, "enabled": config.backup_destination is not None, "state": "starting" if config.backup_destination else "disabled", "failure_domain_qualified": False, "last_capture_utc_ns": None, "last_completed_utc_ns": None, "error_code": None}
        self.log_export_cache = {"schema_version": 1, "enabled": config.log_export_destination is not None,
                                 "state": "starting" if config.log_export_destination else "disabled",
                                 "cloud_upload_confirmed": False, "copied_files": 0, "pending_files": 0,
                                 "pending_bytes": 0, "skipped_files": 0, "error_code": None}
        self.health = {"collector": {"state": "starting"}, "status": {"state": "starting"}, 'indexer':{'state':'starting'}}
        self.analytics_cache={"schema_version":1,"enabled":config.analytics_enabled,"state":"starting" if config.analytics_enabled else "disabled","error_code":None,"latest_report_id":None,"converted_files":0,"pending_files":0,"retry_files":0}
        self.health["analytics"]={"state":self.analytics_cache["state"]}
        self.health["log_export"] = {"state": self.log_export_cache["state"]}
        self.health["backup"] = {"state": "starting" if config.backup_destination else "disabled"}
        self.video=RecorderWorker(self.root,video_config,recorder_factory=recorder_factory or Recorder,
                                  publish=self._video_publish)
        self.health['video']={'state':'starting' if video_config else 'disabled'}
        self.started = False
        self.closed = False
        self.media=MediaWorker(self,video_media_config,adapter_factory=media_adapter_factory or NativeMediaAdapter)
        marker_options={}
        if marker_status_factory is not None:marker_options['status_factory']=marker_status_factory
        if marker_transport_factory is not None:marker_options['transport_factory']=marker_transport_factory
        try:
            self.markers=MarkerWorker(self,marker_config,**marker_options)
        except BaseException:
            # Marker schema/startup failure precedes worker creation. Release archive ownership
            # so a repaired startup can open the same root rather than waiting for process exit.
            self.settings_db.close()
            self.diagnostics.close()
            self.owner.close()
            raise
        self.threads = []
        self.io_lifetimes = []
        self.diagnostics.record("service_initialized", "Local source service initialized; no deployment or source deletion")
        self.diagnostics.record("transfer_limits", f"One request outstanding; maximum {config.chunk_size} bytes; client deadline {config.io_timeout} s; source tail requires transport qualification")

    def _health(self, worker, state, code=None):
        with self.cache_lock:
            self.health[worker] = {"state": state, "error_code": code}

    def _video_publish(self,snapshot):
        self._health('video',snapshot['state'],snapshot['error_code'])

    def _video_worker(self):
        self.video.run(self.stop)

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
                        threading.Thread(target=self._collector_worker, name="hub-collector"),
                        threading.Thread(target=self._pipeline_worker,name='hub-indexer'),
                        threading.Thread(target=self._video_worker,name='hub-video'),
                        threading.Thread(target=self.media.run,args=(self.stop,),name='hub-media'),
                        threading.Thread(target=self.markers.run,args=(self.stop,),name='hub-markers')]
        if self.config.backup_destination is not None:
            self.threads.append(threading.Thread(target=self._backup_worker, name="hub-backup"))
        if self.config.log_export_destination is not None:
            self.threads.append(threading.Thread(target=self._log_export_worker, name="hub-log-export"))
        if self.config.analytics_enabled:
            self.threads.append(threading.Thread(target=self._analytics_worker,name="hub-analytics"))
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

    def _pipeline_worker(self):
        from .pipeline import Pipeline
        db=None
        self._health('indexer','running')
        try:
            db=open_catalog(self.root)
            pipeline=Pipeline(self.root,db)
            while not self.stop.is_set():
                pipeline.tick()
                self.stop.wait(0.5)
        except Exception as exc:
            self._health('indexer','failed','indexer_failed')
            self.diagnostics.record('indexer_failed','Local import/indexing stopped; originals preserved. Check catalog/storage and restart',exception=exc)
        finally:
            if db is not None:
                db.close()
            with self.cache_lock:
                if self.health['indexer']['state']!='failed':
                    self.health['indexer']={'state':'stopped','error_code':None}

    def _analytics_publish(self,snapshot):
        with self.cache_lock:
            self.analytics_cache=snapshot
            self.health["analytics"]={"state":snapshot["state"],"error_code":snapshot["error_code"]}

    def _analytics_paused(self):
        status=self.cached_source.status()
        age=time.monotonic()-status.observed_at
        return self.stop.is_set() or (self.config.analytics_pause_when_enabled and status.enabled is True
            and 0<=age<=self.config.freshness)

    def _analytics_worker(self):
        from .analytics import AnalyticsWorker
        db=None
        try:
            db=open_catalog(self.root)
            worker=AnalyticsWorker(self.root,db,self.config,stopping=self._analytics_paused,publish=self._analytics_publish)
            while not self.stop.is_set():
                worker.tick()
                self.stop.wait(self.config.analytics_interval)
        except Exception as exc:
            self.diagnostics.record("analytics_worker_failed","Analytics stopped; check the pinned analytics installation and storage",exception=exc)
            with self.cache_lock:
                self.analytics_cache.update(state="failed",error_code=type(exc).__name__)
                self.health["analytics"]={"state":"failed","error_code":type(exc).__name__}
        finally:
            if db is not None:db.close()

    def _log_export_publish(self, snapshot):
        with self.cache_lock:
            self.log_export_cache = snapshot
            self.health["log_export"] = {"state": snapshot["state"], "error_code": snapshot["error_code"]}

    def _log_export_worker(self):
        from .log_export import LogExporter
        try:
            exporter = LogExporter(self.root, self.config.log_export_destination,
                                   stopping=self.stop.is_set, publish=self._log_export_publish,
                                   include_summaries=self.config.analytics_share_summaries)
            while not self.stop.is_set():
                try:
                    exporter.tick()
                except (OSError, ValueError, RuntimeError) as exc:
                    self.diagnostics.record("log_export_retry", "Log sharing interrupted; local originals preserved. Check Drive folder", exception=exc)
                self.stop.wait(self.config.log_export_interval)
        except Exception as exc:
            self._health("log_export", "failed", "log_export_worker_failed")
            self.diagnostics.record("log_export_worker_failed", "Log sharing stopped; local originals preserved", exception=exc)
            with self.cache_lock:
                self.log_export_cache.update(state="failed", error_code="log_export_worker_failed")

    def _backup_publish(self, snapshot):
        with self.cache_lock:
            self.backup_cache = snapshot

    def _backup_worker(self):
        from .backup import BackupScheduler
        self._health("backup", "running")
        try:
            scheduler = BackupScheduler(self.root, self.config.backup_destination,
                interval=self.config.backup_interval, retry=self.config.backup_retry,
                stopping=self.stop.is_set, publish=self._backup_publish, configuration=self.config)
            while not self.stop.is_set():
                scheduler.tick()
                self.stop.wait(0.25)
        except Exception as exc:
            self._health("backup", "failed", "backup_worker_failed")
            self.diagnostics.record("backup_worker_failed", "Backup worker stopped; local originals preserved. Check configured destination and restart", exception=exc)
            with self.cache_lock:
                self.backup_cache.update(state="failed", error_code="backup_worker_failed")
        finally:
            with self.cache_lock:
                if self.health["backup"]["state"] != "failed":
                    self.health["backup"] = {"state": "stopped", "error_code": None}

    def _collector_worker(self):
        collector = None
        try:
            collector = self.collector_factory(self.root, self.cached_source, idle_delay=self.config.idle_delay,
                freshness=self.config.freshness, chunk_size=self.config.chunk_size, owner=self.owner,
                paused=self._preference, stopping=self.stop.is_set, publish=self._publish,
                independent_verification=True, io_timeout=self.config.io_timeout,
                discovery_page_size=self.config.discovery_page_size,
                retry_initial=self.config.retry_initial, retry_max=self.config.retry_max,
                retry_limit=self.config.retry_limit, eta_window=self.config.eta_window,
                eta_minimum=self.config.eta_minimum, historical_max_age=self.config.historical_max_age)
            self._health("collector", "running")
            self._publish(collector.snapshot())
            last_error = None
            while not self.stop.is_set():
                while not self.commands.empty():
                    command = self.commands.get_nowait()
                    if command[0] == 'retry':
                        collector.retry_errors(command[1])
                    elif command[0] == 'priority':
                        collector.set_priority(command[1], command[2], command[3])
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
                    if value and self.cached_source.cancellation is not None:
                        self.cached_source.cancellation.cancel()
        self.diagnostics.record("operator_pause" if value else "operator_resume", "Operator collection preference saved")

    def _validate_transfer(self, identity):
        if not isinstance(identity, str) or not identity:
            raise ValueError("Expected a transfer identity")
        with self.cache_lock:
            if not any(row['id'] == identity for row in self.cache['files']):
                raise ValueError("Unknown transfer identity")

    def retry_errors(self, identity=None):
        if self.stop.is_set():
            raise ValueError("Service is stopping")
        if identity is not None:
            self._validate_transfer(identity)
        self.commands.put(('retry', identity))

    def set_priority(self, identity, priority, urgent=False):
        if self.stop.is_set():
            raise ValueError("Service is stopping")
        self._validate_transfer(identity)
        if type(priority) is not int or not 0 <= priority <= 100 or type(urgent) is not bool:
            raise ValueError("Invalid priority")
        self.commands.put(('priority', identity, priority, urgent))

    def snapshot(self):
        with self.cache_lock:
            result = copy.deepcopy(self.cache)
            health = copy.deepcopy(self.health)
            backup = copy.deepcopy(self.backup_cache)
            log_export = copy.deepcopy(self.log_export_cache)
            analytics = copy.deepcopy(self.analytics_cache)
        status = self.cached_source.status()
        age = time.monotonic() - status.observed_at
        snapshot_age = time.monotonic() - result.get("snapshot_monotonic", time.monotonic())
        fresh = math.isfinite(age) and 0 <= age <= self.config.freshness and type(status.enabled) is bool and type(status.transfer_allowed) is bool
        capture = backup.get("last_capture_utc_ns")
        age_ns = time.time_ns() - int(capture) if capture is not None else None
        backup["capture_age_seconds"] = age_ns / 1e9 if age_ns is not None and age_ns >= 0 else None
        result.update({"analytics":analytics,"log_export": log_export, "backup": backup, "schema_version": 1, "source_type": getattr(self.source,'source_type','unconfigured'), "workers": health,
                       "paused_by_operator": self.paused.is_set(), "snapshot_age_seconds": max(0, snapshot_age), "source_status": {
                           "enabled": status.enabled, "transfer_allowed": status.transfer_allowed, "fresh": fresh, "age_seconds": age if math.isfinite(age) and age >= 0 else None}})
        if health["collector"]["state"] == "failed":
            result.update(state="attention", reason="Collector worker failed; restart the hub", error=health["collector"]["error_code"])
        elif health["status"]["state"] == "failed":
            result.update(state="attention", reason="Status worker failed; collection blocked", error=health["status"]["error_code"])
        elif self.stop.is_set():
            result.update(state="stopping", reason="Stopping workers; preserving checkpoints")
        elif self.paused.is_set():
            result.update(state="paused", reason="Paused by operator; preference saved")
        elif not fresh or status.enabled or not status.transfer_allowed:
            result.update(state="paused", reason="Robot enabled" if fresh and status.enabled else ("Source transfer permission denied" if fresh and not status.transfer_allowed else "Robot status unknown or stale"))
        if result["state"] in ("paused", "attention", "stopping", "discovering", "verifying", "starting"):
            result.update(bytes_per_second=None, eta_seconds=None)
        result['video']=self.video.snapshot()
        result['markers']=self.markers.snapshot()
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
        if not self.settings_lock.acquire(timeout=max(0,deadline-time.monotonic())):
            self.diagnostics.record('shutdown_timeout','Local I/O registration is outstanding; ownership retained')
            return False
        try:workers=list(self.threads)+list(self.io_lifetimes)
        finally:self.settings_lock.release()
        for thread in workers:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in workers):
            self.diagnostics.record("shutdown_timeout", "Worker still has outstanding I/O; ownership retained. Adapter must provide bounded I/O/cancellation")
            return False
        if not self.settings_lock.acquire(timeout=max(0,deadline-time.monotonic())):
            self.diagnostics.record("shutdown_timeout", "Local publication is still outstanding; ownership retained")
            return False
        try:
            if self.closed:
                return True
            self.settings_db.close()
            self.closed = True
        finally:
            self.settings_lock.release()
        self.diagnostics.record("service_stopped", "Workers stopped; committed checkpoints preserved")
        self.diagnostics.close()
        self.owner.close()
        return True
