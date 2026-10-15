"""Opt-in LL4 run-end capture and idle-only transfer worker; no robot control."""
from __future__ import annotations

from collections import deque
from contextlib import closing
import copy
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .backup import _json
from .rewind import RewindConfig, RewindHttpTransport, RewindArchive, RewindError, _json as strict_json, install_schema as install_archive
from .storage import open_catalog


def install_schema(db):
    install_archive(db)
    db.execute("""CREATE TABLE IF NOT EXISTS rewind_capture_requests (
        id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, robot_boot_id TEXT NOT NULL,
        generation INTEGER NOT NULL, observed_utc_ns TEXT NOT NULL,
        state TEXT NOT NULL, requested_seconds INTEGER NOT NULL, error_code TEXT)""")


def load_rewind_config(path):
    """Explicit private endpoint settings, bounded input, fixed error text."""
    if path is None:
        return None
    try:
        with Path(path).open("rb") as stream:
            payload=stream.read(16385)
        if len(payload)>16384:
            raise ValueError()
        values=strict_json(payload)
        if not isinstance(values,dict) or type(values.get("schema_version")) is not int or values["schema_version"]!=1 or values.get("enabled") is not True:
            raise ValueError()
        values.pop("schema_version");values.pop("enabled")
        return RewindConfig.from_mapping(values)
    except (OSError,ValueError,TypeError,KeyError,OverflowError,RecursionError,RewindError):
        raise ValueError("Invalid private Rewind configuration; check explicit endpoint, identity and bounds") from None


class RewindWorker:
    def __init__(self,service,config=None,*,transport_factory=RewindHttpTransport,clock=time.monotonic):
        if config is not None and not isinstance(config,RewindConfig):
            raise ValueError("Explicit validated Rewind configuration required")
        if config is not None and config.source_type=="REAL":
            source=service.source
            if (getattr(source,"source_type",None)!="systemcore_sftp_unqualified" or
                    getattr(getattr(source,"config",None),"robot_id",None)!=config.robot_id):
                raise ValueError("REAL Rewind requires matching authoritative live robot status")
        self.service,self.config,self.clock=service,config,clock
        self.lock=threading.Lock()
        self.events=deque()
        self.latest=None
        self.idle_since=None
        self.idle_generation=None
        self.last_state=None
        self.revocation=0
        self.idle_preference_generation=service.preference_generation
        self.camera_scope=hashlib.sha256(_json([config.camera_id,config.robot_id,config.origin,config.source_type])).hexdigest() if config else None
        self.transport_factory=transport_factory
        self.transport=None
        self.db=None
        self.archive=None
        self.retry_at=0.
        self.next_poll=0.
        self.health={"schema_version":1,"enabled":config is not None,"state":"starting" if config else "disabled",
            "source_type":config.source_type if config else None,"camera_id":config.camera_id if config else None,
            "error_code":None,"archived_recordings":0,"pending_captures":0,"capture_unknown":0,
            "last_capture_state":None,"last_capture_id":None,"recent_recordings":[],
            "physical_camera_qualified":False,"clock_alignment_qualified":False,
            "remote_identity_qualified":False,"source_deletion_enabled":False,"video_drive_sharing_enabled":False}
        if config is not None:
            with service.settings_db:
                install_schema(service.settings_db)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.health)

    def emit(self,**values):
        with self.lock:
            self.health.update(values)
            snapshot=copy.deepcopy(self.health)
        self.service._health("rewind",snapshot["state"],snapshot["error_code"])

    def _fresh(self,status,now):
        return (status is not None and type(status.enabled) is bool and
                type(status.observed_at) in (int,float) and math.isfinite(status.observed_at) and
                0<=now-status.observed_at<=self.service.config.freshness and
                bool(status.boot_id) and status.boot_id!="unknown")

    def observe(self,status):
        if self.config is None:
            return
        now=self.clock()
        with self.lock:
            previous=self.latest
            fresh=self._fresh(status,now)
            permitted=fresh and status.enabled is False and status.transfer_allowed is True and not self.service.paused.is_set()
            generation=(status.boot_id,status.generation)
            if not permitted or not self._fresh(previous,now) or generation!=self.idle_generation or self.idle_preference_generation!=self.service.preference_generation or (self.idle_since is not None and now<self.idle_since):
                self.revocation+=1
                self.idle_since=None
            self.idle_preference_generation=self.service.preference_generation
            if permitted:
                if self.idle_since is None:
                    self.idle_since=now
                self.idle_generation=generation
            if (permitted and self._fresh(previous,now) and previous.enabled is True and
                    previous.boot_id==status.boot_id and generation!=(previous.boot_id,previous.generation)):
                key=hashlib.sha256(_json([self.camera_scope,*generation])).hexdigest()
                if len(self.events)<128:
                    self.events.append(dict(id=key,boot_id=status.boot_id,generation=status.generation,
                        observed_utc_ns=str(time.time_ns())))
                else:
                    self.health["error_code"]="capture_event_queue_full"
            self.latest=status

    def _permission(self,*,idle=False,token=None):
        now=self.clock()
        with self.lock:
            status=self.latest
            if self.idle_preference_generation!=self.service.preference_generation:
                self.idle_since=None;self.revocation+=1
                self.idle_preference_generation=self.service.preference_generation
            okay=(not self.service.stop.is_set() and not self.service.paused.is_set() and
                  self._fresh(status,now) and status.enabled is False and status.transfer_allowed is True)
            if not self._fresh(status,now) and self.idle_since is not None:
                self.idle_since=None;self.revocation+=1
            if token is not None:
                okay=okay and token==(status.boot_id,status.generation,self.revocation,self.service.preference_generation)
            if idle:
                okay=okay and self.idle_since is not None and now-self.idle_since>=self.service.config.idle_delay
            return okay

    def _token(self):
        with self.lock:
            status=self.latest
            return (status.boot_id,status.generation,self.revocation,self.service.preference_generation)

    def _cancelled(self,token,idle):
        return not self._permission(idle=idle,token=token)

    def _open(self):
        self.db=open_catalog(self.service.root)
        with self.db:
            # A timed-out/crashed POST must not be blindly repeated: it may already have saved/evicted.
            self.db.execute("UPDATE rewind_capture_requests SET state='unknown',error_code='capture_outcome_unknown' WHERE camera_id=? AND state='issuing'",(self.config.camera_id,))
        self.transport=self.transport_factory(self.config)
        self.archive=RewindArchive(self.service.root,self.db,self.config,transport=self.transport)
        self.archive.recover(cancel=self.service.stop.is_set)

    def _events_to_db(self):
        with self.lock:
            pending=list(self.events);self.events.clear()
        with self.db:
            for event in pending:
                self.db.execute("""INSERT OR IGNORE INTO rewind_capture_requests
                    VALUES(?,?,?,?,?,'pending',?,NULL)""",(event["id"],self.config.camera_id,event["boot_id"],
                    event["generation"],event["observed_utc_ns"],self.config.duration_seconds))

    def _capture(self):
        row=self.db.execute("""SELECT * FROM rewind_capture_requests WHERE camera_id=? AND state='pending'
            ORDER BY observed_utc_ns LIMIT 1""",(self.config.camera_id,)).fetchone()
        if row is None:
            return False
        if self.clock()<self.retry_at:
            return True
        if not self._permission():
            return False
        token=self._token()
        expected=hashlib.sha256(_json([self.camera_scope,row["robot_boot_id"],row["generation"]])).hexdigest()
        if row["id"]!=expected or (row["robot_boot_id"],row["generation"])!=token[:2]:
            with self.db:
                self.db.execute("UPDATE rewind_capture_requests SET state='missed',error_code='run_end_permission_lost' WHERE id=?",(row["id"],))
            return True
        if not self.service.bulk_lock.acquire(blocking=False):
            self.emit(state="waiting_for_logs")
            return True
        self.service.camera_network.set()
        try:
            if self._cancelled(token,False):
                return True
            try:
                camera_state=self.transport.state(cancel=lambda:self._cancelled(token,False))
                if self.transport.is_flushing(camera_state):
                    self.emit(state="waiting_for_camera")
                    return True
            except Exception as exc:
                self.retry_at=self.clock()+max(1.,self.config.poll_interval_seconds)
                self.emit(state="retry_wait",error_code=type(exc).__name__)
                return True
            if self._cancelled(token,False):
                return True
            with self.db:
                self.db.execute("UPDATE rewind_capture_requests SET state='issuing' WHERE id=?",(row["id"],))
            self.emit(state="saving_rewind")
            try:
                result=self.transport.flush(self.config.duration_seconds,cancel=lambda:self._cancelled(token,False))
                accepted=isinstance(result,dict) and result.get("success") is True
                state,error=("accepted",None) if accepted else ("rejected","camera_rejected_capture")
            except Exception:
                state,error="unknown","capture_outcome_unknown"
            with self.db:
                self.db.execute("UPDATE rewind_capture_requests SET state=?,error_code=? WHERE id=?",(state,error,row["id"]))
            self.emit(last_capture_state=state,last_capture_id=row["id"],error_code=error)
        finally:
            self.service.camera_network.clear()
            self.service.bulk_lock.release()
        return True

    def _archive_snapshot(self):
        rows=[json.loads(row[0]) for row in self.db.execute("SELECT metadata_json FROM rewind_recordings WHERE camera_id=? ORDER BY created_ns DESC LIMIT 20",(self.config.camera_id,))]
        self.emit(archived_recordings=self.db.execute("SELECT COUNT(*) FROM rewind_recordings WHERE camera_id=?",(self.config.camera_id,)).fetchone()[0],
            recent_recordings=[{"recording_id":row.get("recording_id"),"camera_id":self.config.camera_id,
                "source_type":self.config.source_type,"size_bytes":sum(item["size_bytes"] for item in row["files"].values()),
                "created_utc_ns":str(row["created_ns"]),"clock_alignment_qualified":False} for row in rows],
            pending_captures=self.db.execute("SELECT COUNT(*) FROM rewind_capture_requests WHERE camera_id=? AND state='pending'",(self.config.camera_id,)).fetchone()[0],
            capture_unknown=self.db.execute("SELECT COUNT(*) FROM rewind_capture_requests WHERE camera_id=? AND state='unknown'",(self.config.camera_id,)).fetchone()[0])

    def tick(self):
        self._events_to_db()
        if self._capture():
            self._archive_snapshot()
            return
        if not self._permission(idle=True):
            self.emit(state="paused",error_code=None)
            self._archive_snapshot()
            return
        if self.clock()<self.retry_at:
            self.emit(state="retry_wait")
            return
        if self.clock()<self.next_poll:
            return
        # Logs get first opportunity; the shared lock enforces actual serialization.
        if not self.service.bulk_lock.acquire(blocking=False):
            self.emit(state="waiting_for_logs")
            return
        token=self._token()
        self.service.camera_network.set()
        self.next_poll=self.clock()+self.config.poll_interval_seconds
        try:
            self.emit(state="downloading",error_code=None)
            self.archive.collect(gate=lambda:self._permission(idle=True,token=token),
                                 cancel=lambda:self._cancelled(token,True))
            self._archive_snapshot()
            self.emit(state="idle")
        except Exception as exc:
            if self._cancelled(token,True):
                self.emit(state="paused",error_code=None)
            else:
                self.retry_at=self.clock()+max(5.,self.config.poll_interval_seconds)
                self.emit(state="retry_wait",error_code=type(exc).__name__)
        finally:
            self.service.camera_network.clear()
            self.service.bulk_lock.release()

    def run(self,stop):
        if self.config is None:
            return
        try:
            self._open()
            self._archive_snapshot()
            while not stop.is_set():
                self.tick()
                stop.wait(min(.25,self.config.poll_interval_seconds))
        except Exception as exc:
            self.emit(state="failed",error_code=type(exc).__name__)
            self.service.diagnostics.record("rewind_worker_failed","Rewind stopped; local originals preserved",exception=exc)
        finally:
            if self.transport is not None:
                self.transport.abort()
            if self.db is not None:
                self.db.close()
            if self.snapshot()["state"]!="failed":
                self.emit(state="stopped")
