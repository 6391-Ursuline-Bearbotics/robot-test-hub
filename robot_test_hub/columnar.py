"""Versioned immutable Parquet derivatives of qualified imports; no robot or cloud access."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from .analysis import identity
from .backup import _digest, _json, _path, _sync_directory
from .importer import Importer
from .runs import _enabled
from .swerve import ROBOT_FIELDS, MODULE_POSITIONS
from .wpilog import PROFILE

DUCKDB_VERSION = "1.5.6"
VERSION = "columnar-1"
SUMMARY_VERSION = "summary-1"


class ColumnarError(ValueError):
    pass


class AnalysisInterrupted(ColumnarError):
    pass


def install_schema(db):
    db.execute("""CREATE TABLE columnar_jobs (
        id TEXT PRIMARY KEY, import_job_id TEXT NOT NULL REFERENCES import_jobs(id),
        version TEXT NOT NULL, input_sha256 TEXT NOT NULL, state TEXT NOT NULL,
        created_utc_ns INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_utc_ns INTEGER NOT NULL DEFAULT 0, error_code TEXT,
        manifest_path TEXT, manifest_sha256 TEXT,
        UNIQUE(import_job_id,version,input_sha256))""")
    db.execute("""CREATE TABLE columnar_artifacts (
        job_id TEXT NOT NULL REFERENCES columnar_jobs(id), path TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        PRIMARY KEY(job_id,path))""")
    db.execute("""CREATE TABLE summary_reports (
        id TEXT PRIMARY KEY, created_utc_ns INTEGER NOT NULL,
        state TEXT NOT NULL, manifest_path TEXT, manifest_sha256 TEXT,
        input_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE summary_artifacts (
        report_id TEXT NOT NULL REFERENCES summary_reports(id),
        path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        PRIMARY KEY(report_id,path))""")
    db.execute("""CREATE TABLE summary_run_cache (
        id TEXT PRIMARY KEY, document_json TEXT NOT NULL)""")


def dependency():
    try:
        import duckdb
    except ImportError:
        raise ColumnarError("Install the pinned analytics extra") from None
    if duckdb.__version__ != DUCKDB_VERSION:
        raise ColumnarError("The analytics worker requires the pinned DuckDB version")
    return duckdb


def sql_path(path):
    return "'" + str(path).replace("\\", "/").replace("'", "''") + "'"


@contextmanager
def connection(root, *, threads=1, memory_mb=512, stopping=lambda: False):
    duckdb = dependency()
    temp = _path(Path(root).resolve(), "analytics/duckdb-temp")
    temp.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect(config={"threads": threads, "memory_limit": str(memory_mb)+"MB",
                              "temp_directory": str(temp), "autoinstall_known_extensions": False,
                              "autoload_known_extensions": False})
    done = threading.Event()
    def watch():
        while not done.wait(.05):
            if stopping():
                db.interrupt()
                return
    watcher = threading.Thread(target=watch, name="hub-analytics-interrupt")
    watcher.start()
    try:
        if stopping():
            raise AnalysisInterrupted("Analytics paused or stopping")
        yield db
        if stopping():
            raise AnalysisInterrupted("Analytics paused or stopping")
    finally:
        done.set()
        watcher.join()
        db.close()


def publish_file(source, target):
    """Never overwrite published derivatives, even on an interrupted retry."""
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = _digest(source)
    if target.exists():
        if _digest(target) != expected:
            raise ColumnarError("Existing analytics derivative differs; preserved")
    else:
        with source.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.link(source, target)
        _sync_directory(target.parent)
    return expected


def _checked_digest(path,stopping):
    digest,size=hashlib.sha256(),0
    with path.open("rb") as stream:
        while True:
            if stopping():raise AnalysisInterrupted("Integrity verification paused")
            block=stream.read(1024*1024)
            if not block:break
            size+=len(block);digest.update(block)
    return digest.hexdigest(),size


def verify_artifacts(root,db,table,key,value,stopping=lambda:False):
    if (table,key) not in (("columnar_artifacts","job_id"),("summary_artifacts","report_id")):
        raise ColumnarError("Unsupported artifact contract")
    rows=db.execute("SELECT * FROM "+table+" WHERE "+key+"=? ORDER BY path",(value,)).fetchall()
    if not rows:
        raise ColumnarError("Analytics artifacts missing")
    for row in rows:
        if _checked_digest(_path(root,row["path"]),stopping) != (row["sha256"],row["size_bytes"]):
            raise ColumnarError("Analytics artifact integrity mismatch")
    return rows


EXPECTED_SCHEMAS = {
    "/.schema/struct:SwerveModuleVelocity": "doublevelocity;Rotation2dangle",
    "/.schema/struct:Rotation2d": "doublevalue",
}
FRAME_TYPES = {
    "import_job_id":"VARCHAR","source_sha256":"VARCHAR","timestamp_ns":"BIGINT",
    "cycle_record_index":"BIGINT","module_index":"INTEGER","module_position":"VARCHAR",
    "runtime_mode":"VARCHAR","enabled":"BOOLEAN","mapping_valid":"BOOLEAN",
    "command_speed_mps":"DOUBLE","measured_speed_mps":"DOUBLE",
    "command_angle_rad":"DOUBLE","measured_angle_rad":"DOUBLE",
    "command_cycle_ns":"BIGINT","measured_cycle_ns":"BIGINT",
    "command_record_index":"BIGINT","measured_record_index":"BIGINT",
    "connected":"BOOLEAN","drive_connected":"BOOLEAN","turn_connected":"BOOLEAN",
    "drive_current_amps":"DOUBLE","turn_current_amps":"DOUBLE",
    "drive_current_cycle_ns":"BIGINT","turn_current_cycle_ns":"BIGINT",
    "drive_current_record_index":"BIGINT","turn_current_record_index":"BIGINT",
    "build_hash":"VARCHAR","config_hash":"VARCHAR","artifact_hash":"VARCHAR",
}


def _finite(value):
    return value if type(value) in (int,float) and math.isfinite(value) else None


def _valid(row):
    return row is not None and row.get("validity")=="valid"


def _stamp(row):
    value=row.get("cycle_timestamp_ns") if row else None
    return int(value) if value is not None else None


def normalize_frames(rows, import_job_id, sha):
    """One source-order pass; held values keep original stamps and record indices."""
    state={}
    schemas={}
    for row in rows:
        if row.get("kind")=="control" and row.get("control") in ("start","finish"):
            for name,held in list(state.items()):
                if name==row.get("field") or (row.get("entry_id") is not None and held.get("entry_id")==row["entry_id"]):
                    state.pop(name,None)
            continue
        if row.get("kind")=="observation":
            name=row.get("field")
            if name in EXPECTED_SCHEMAS and row.get("type")=="structschema":
                schemas[name]="".join(str(row.get("value")).split()).rstrip(";")
            if name in ROBOT_FIELDS.values() or any(name==f"/Drive/Module{i}/"+suffix
                    for i in range(4) for suffix in ("DriveConnected","TurnConnected","TurnEncoderConnected","DriveCurrentAmps","TurnCurrentAmps")):
                state[name]=row
            continue
        if row.get("kind")!="cycle":
            continue
        aliases=row.get("aliases",{})
        def alias(name):
            ref=aliases.get(name,{})
            return ref.get("value") if ref.get("validity")=="valid" else None
        enabled=_enabled(row)
        if type(enabled) is not bool:
            enabled=None
        command,measured=(state.get(ROBOT_FIELDS[name]) for name in ("commands","measured"))
        mapping_valid=(schemas==EXPECTED_SCHEMAS and _valid(command) and _valid(measured)
                       and command.get("type")==measured.get("type")=="struct:SwerveModuleVelocity[]"
                       and isinstance(command.get("value"),list) and isinstance(measured.get("value"),list)
                       and len(command["value"])==len(measured["value"])==4
                       and command.get("unit") is None and measured.get("unit") is None)
        for i,position in enumerate(MODULE_POSITIONS):
            def connected(suffix):
                ref=state.get(f"/Drive/Module{i}/"+suffix)
                return ref.get("value") if _valid(ref) and type(ref.get("value")) is bool else None
            drive,turn,encoder=(connected(suffix) for suffix in ("DriveConnected","TurnConnected","TurnEncoderConnected"))
            frame={key:None for key in FRAME_TYPES}
            frame.update(import_job_id=import_job_id,source_sha256=sha,timestamp_ns=int(row["timestamp_ns"]),
                         cycle_record_index=row["timestamp_record_index"],module_index=i,module_position=position,
                         runtime_mode=alias("runtime_mode"),enabled=enabled,mapping_valid=mapping_valid,
                         connected=(drive and turn and encoder) if None not in (drive,turn,encoder) else None,
                         drive_connected=drive,turn_connected=turn,
                         build_hash=alias("source_sha256"),config_hash=alias("configuration_sha256"),artifact_hash=alias("artifact_sha256"))
            if mapping_valid:
                target,actual=command["value"][i],measured["value"][i]
                if isinstance(target,dict) and isinstance(actual,dict):
                    frame.update(command_speed_mps=_finite(target.get("velocity")),
                                 measured_speed_mps=_finite(actual.get("velocity")),
                                 command_angle_rad=_finite(target.get("angle",{}).get("value")) if isinstance(target.get("angle"),dict) else None,
                                 measured_angle_rad=_finite(actual.get("angle",{}).get("value")) if isinstance(actual.get("angle"),dict) else None,
                                 command_cycle_ns=_stamp(command),measured_cycle_ns=_stamp(measured),
                                 command_record_index=command.get("record_index"),measured_record_index=measured.get("record_index"))
            for side in ("drive","turn"):
                ref=state.get(f"/Drive/Module{i}/"+side.capitalize()+"CurrentAmps")
                # Exact pinned ModuleIO source mapping; contradictory metadata is rejected.
                if _valid(ref) and ref.get("type")=="double" and ref.get("unit") in (None,"amps","amperes"):
                    frame[side+"_current_amps"]=_finite(ref.get("value"))
                    frame[side+"_current_cycle_ns"]=_stamp(ref)
                    frame[side+"_current_record_index"]=ref.get("record_index")
            yield frame


RECORD_SQL = """
SELECT row_number() OVER ()-1 AS row_ordinal,
       json::VARCHAR AS row_json,
       json_extract_string(json,'$.kind') AS kind,
       json_extract_string(json,'$.source_sha256') AS source_sha256,
       json_extract_string(json,'$.field') AS field,
       json_extract_string(json,'$.type') AS recorded_type,
       json_extract_string(json,'$.unit') AS unit,
       json_extract_string(json,'$.validity') AS validity,
       TRY_CAST(json_extract_string(json,'$.record_index') AS BIGINT) AS record_index,
       TRY_CAST(json_extract_string(json,'$.entry_id') AS BIGINT) AS entry_id,
       TRY_CAST(json_extract_string(json,'$.entry_generation') AS BIGINT) AS entry_generation,
       TRY_CAST(json_extract_string(json,'$.record_timestamp_ns') AS BIGINT) AS record_timestamp_ns,
       TRY_CAST(json_extract_string(json,'$.cycle_timestamp_ns') AS BIGINT) AS cycle_timestamp_ns,
       TRY_CAST(json_extract_string(json,'$.timestamp_ns') AS BIGINT) AS timestamp_ns,
       TRY_CAST(json_extract_string(json,'$.sample_timestamp_ns') AS BIGINT) AS sample_timestamp_ns,
       TRY_CAST(json_extract_string(json,'$.sample_index') AS INTEGER) AS sample_index,
       json_extract_string(json,'$.sensor_availability') AS sensor_availability,
       json_extract_string(json,'$.timestamp_precision') AS timestamp_precision,
       CASE WHEN json_extract_string(json,'$.kind')='sample' AND json_extract_string(json,'$.validity')='valid'
            THEN TRY_CAST(json_extract_string(json,'$.value') AS DOUBLE) END AS sample_value_double,
       CASE WHEN json_extract_string(json,'$.type') IN ('double','float')
                  AND json_extract_string(json,'$.validity')='valid'
            THEN TRY_CAST(json_extract_string(json,'$.value') AS DOUBLE) END AS value_double,
       CASE WHEN json_extract_string(json,'$.type')='int64'
                  AND json_extract_string(json,'$.validity')='valid'
            THEN TRY_CAST(json_extract_string(json,'$.value') AS BIGINT) END AS value_int64,
       CASE WHEN json_extract_string(json,'$.type')='boolean'
                  AND json_extract_string(json,'$.validity')='valid'
            THEN TRY_CAST(json_extract_string(json,'$.value') AS BOOLEAN) END AS value_boolean
FROM read_json_objects(?,format='newline_delimited',maximum_object_size=16777216)
"""


class Converter:
    def __init__(self,root,db,*,threads=1,memory_mb=512,stopping=lambda:False):
        self.root,self.db=Path(root).resolve(),db
        self.threads,self.memory_mb,self.stopping=threads,memory_mb,stopping
        self.verified=set()

    def key(self,job):
        return identity([VERSION,DUCKDB_VERSION,job["id"],job["dataset_sha256"],job["manifest_sha256"]])

    def convert(self,import_job):
        importer=Importer(self.root,self.db)
        job=importer.get_job(import_job)
        if job["state"] not in ("succeeded","succeeded_with_unsupported") or job["profile"]!=PROFILE:
            raise ColumnarError("Qualified successful import required")
        key=self.key(job)
        existing=self.db.execute("SELECT * FROM columnar_jobs WHERE id=?",(key,)).fetchone()
        if existing and existing["state"]=="succeeded":
            if key not in self.verified:
                verify_artifacts(self.root,self.db,"columnar_artifacts","job_id",key,self.stopping)
                self.verified.add(key)
            return dict(existing)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO columnar_jobs(id,import_job_id,version,input_sha256,state,created_utc_ns) VALUES(?,?,?,?,'pending',?)",
                            (key,job["id"],VERSION,job["dataset_sha256"],time.time_ns()))
            self.db.execute("UPDATE columnar_jobs SET state='running',attempts=attempts+1,error_code=NULL WHERE id=?",(key,))
        try:
            raw=self.db.execute("SELECT * FROM import_artifacts WHERE sha256=?",(job["artifact_sha256"],)).fetchone()
            if raw is None or _checked_digest(_path(self.root,raw["relative_path"]),self.stopping)!=(raw["sha256"],raw["size_bytes"]):
                raise ColumnarError("Original integrity mismatch")
            for name in ("dataset","manifest"):
                if _checked_digest(_path(self.root,job[name+"_path"]),self.stopping)[0]!=job[name+"_sha256"]:
                    raise ColumnarError("Import integrity mismatch")
            manifest=json.loads(_path(self.root,job["manifest_path"]).read_text(encoding="utf-8"))
            folder=_path(self.root,"analytics/parquet/"+key)
            staging=_path(self.root,"analytics/staging")
            staging.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(dir=staging) as temporary:
                temp=Path(temporary)
                frames=temp/"frames.jsonl"
                with importer._path(job["dataset_path"]).open(encoding="utf-8") as inp,frames.open("wb") as out:
                    count=0
                    def rows():
                        for index,line in enumerate(inp):
                            if index%1024==0 and self.stopping():
                                raise AnalysisInterrupted("Conversion paused")
                            yield json.loads(line)
                    for frame in normalize_frames(rows(),job["id"],job["artifact_sha256"]):
                        out.write(_json(frame))
                        count+=1
                    out.flush()
                    os.fsync(out.fileno())
                with connection(self.root,threads=self.threads,memory_mb=self.memory_mb,stopping=self.stopping) as duck:
                    records=temp/"records.parquet"
                    duck.execute("COPY ("+RECORD_SQL+") TO "+sql_path(records)+" (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 65536)",
                                 [str(importer._path(job["dataset_path"]))])
                    frame_parquet=temp/"frames.parquet"
                    columns="{"+",".join(sql_path(name)+":"+sql_path(kind) for name,kind in FRAME_TYPES.items())+"}"
                    if count:
                        source="SELECT * FROM read_json("+sql_path(frames)+",format='newline_delimited',columns="+columns+")"
                    else:
                        source="SELECT "+",".join("CAST(NULL AS "+kind+") AS "+name for name,kind in FRAME_TYPES.items())+" WHERE FALSE"
                    duck.execute("COPY ("+source+") TO "+sql_path(frame_parquet)+" (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 65536)")
                    quality=duck.execute("""SELECT kind,COUNT(*),
                        COUNT(*) FILTER(WHERE validity IS NOT NULL AND validity<>'valid')
                        FROM read_parquet(?) GROUP BY kind ORDER BY kind""",[str(records)]).fetchall()
                    record_quality=[dict(kind=row[0],row_count=row[1],invalid_or_unsupported_count=row[2]) for row in quality]
                    records_count=sum(row[1] for row in quality)
                    if records_count!=sum(manifest["counts"].values()):
                        raise ColumnarError("Parquet extraction row count differs")
                created=self.db.execute("SELECT created_utc_ns FROM columnar_jobs WHERE id=?",(key,)).fetchone()[0]
                items=[]
                for path in (records,frame_parquet):
                    relative="analytics/parquet/"+key+"/"+path.name
                    sha,size=publish_file(path,_path(self.root,relative))
                    items.append({"path":relative,"sha256":sha,"size_bytes":size})
                document={"schema_version":1,"id":key,"version":VERSION,"duckdb_version":DUCKDB_VERSION,
                          "created_utc_ns":str(created),"import_job_id":job["id"],"source_sha256":job["artifact_sha256"],
                          "dataset_sha256":job["dataset_sha256"],"extractor_version":job["extractor_version"],
                          "profile":job["profile"],"mapping_revision":job["mapping_revision"],"source_type":manifest["source_type"],
                          "record_count":records_count,"frame_count":count,"timestamp_unit":"nanoseconds",
                          "record_quality":record_quality,"unsupported_types":manifest["unsupported_types"],
                          "first_cycle_timestamp_ns":manifest["first_cycle_timestamp_ns"],
                          "last_cycle_timestamp_ns":manifest["last_cycle_timestamp_ns"],
                          "physical_acquisition_time_qualified":False,"artifacts":items}
                local=temp/"manifest.json"
                local.write_bytes(_json(document))
                relative="analytics/parquet/"+key+"/manifest.json"
                sha,size=publish_file(local,_path(self.root,relative))
                items.append({"path":relative,"sha256":sha,"size_bytes":size})
                with self.db:
                    for item in items:
                        self.db.execute("INSERT OR IGNORE INTO columnar_artifacts VALUES(?,?,?,?)",
                                        (key,item["path"],item["sha256"],item["size_bytes"]))
                    self.db.execute("UPDATE columnar_jobs SET state='succeeded',manifest_path=?,manifest_sha256=?,next_retry_utc_ns=0,error_code=NULL WHERE id=?",
                                    (relative,sha,key))
            self.verified.add(key)
            return dict(self.db.execute("SELECT * FROM columnar_jobs WHERE id=?",(key,)).fetchone())
        except Exception as exc:
            with self.db:
                self.db.execute("UPDATE columnar_jobs SET state='retry_wait',error_code=?,next_retry_utc_ns=? WHERE id=?",
                                (type(exc).__name__,time.time_ns()+60_000_000_000,key))
            raise
