"""Incremental DuckDB run statistics and portable summary-only reports."""
from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path
import tempfile
import time

from .analysis import identity
from .analysis_plan import resolve_plan,review_snapshot
from .backup import _digest,_json,_path
from .columnar import SUMMARY_VERSION,VERSION,Converter,ColumnarError,connection,publish_file,sql_path,verify_artifacts
from .importer import Importer
from .runs import RunCatalog
from .swerve import _config,MODULE_POSITIONS
from .wpilog import PROFILE
from .portable_summary import render_report

MAX_RUNS=10000
# Presentation changes must not invalidate unchanged per-run numeric queries.
STATISTICS_VERSION="summary-1"
MAX_HTML_RUNS=20
FILES=("summary.json","summary.csv","report.html","manifest.json")


def _fetch(duck,sql,params=None):
    cursor=duck.execute(sql,params or [])
    names=[c[0] for c in cursor.description]
    return [dict(zip(names,row)) for row in cursor.fetchall()]


def _window_query(paths,start,end,config):
    # All paths come from verified immutable catalog artifacts, never UI SQL.
    paths="["+",".join(sql_path(path) for path in paths)+"]"
    gap,age=config["max_gap_ns"],config["max_sample_age_ns"]
    fields=("enabled","mapping_valid","connected","drive_connected","turn_connected",
            "command_speed_mps","measured_speed_mps","command_angle_rad","measured_angle_rad",
            "command_cycle_ns","measured_cycle_ns","drive_current_amps","turn_current_amps",
            "drive_current_cycle_ns","turn_current_cycle_ns")
    leads=",".join("LEAD("+field+") OVER w AS next_"+field for field in fields)
    def recent(name,prefix=""):
        return (prefix+name+" IS NOT NULL AND "+prefix+"timestamp_ns >= "+prefix+name+
                " AND "+prefix+"timestamp_ns-"+prefix+name+" <= "+str(age))
    # Window stamps are explicit so previous and next receipt ages stay distinct.
    age_previous=" AND ".join(recent(name) for name in ("command_cycle_ns","measured_cycle_ns"))
    age_next=" AND ".join(recent(name,"next_") for name in ("command_cycle_ns","measured_cycle_ns"))
    finite_previous=" AND ".join(field+" IS NOT NULL" for field in fields[5:9])
    finite_next=" AND ".join("next_"+field+" IS NOT NULL" for field in fields[5:9])
    demands=" AND ABS(command_speed_mps)>="+str(config["min_demand_mps"])+" AND ABS(next_command_speed_mps)>="+str(config["min_demand_mps"])
    return """WITH ordered AS (
      SELECT *,LEAD(timestamp_ns) OVER w AS next_timestamp_ns,"""+leads+"""
      FROM read_parquet("""+paths+""")
      WHERE timestamp_ns BETWEEN """+str(start)+" AND "+str(end)+"""
      WINDOW w AS (PARTITION BY module_index ORDER BY timestamp_ns,import_job_id,cycle_record_index)
    ), intervals AS (
      SELECT *, (next_timestamp_ns-timestamp_ns)/1e9 AS dt,
             next_timestamp_ns>timestamp_ns AND next_timestamp_ns-timestamp_ns<="""+str(gap)+""" AS gap_ok,
             measured_speed_mps-command_speed_mps AS drive_error,
             ATAN2(SIN(measured_angle_rad-command_angle_rad),COS(measured_angle_rad-command_angle_rad)) AS steering_error
      FROM ordered
    )
    SELECT *,
      enabled IS TRUE AND next_enabled IS TRUE AND gap_ok AND mapping_valid IS TRUE
        AND next_mapping_valid IS TRUE AND connected IS TRUE AND next_connected IS TRUE
        AND """+finite_previous+" AND "+finite_next+" AND "+age_previous+" AND "+age_next+demands+""" AS tracking_eligible,
      enabled IS TRUE AND next_enabled IS TRUE AND gap_ok
        AND drive_connected IS TRUE AND next_drive_connected IS TRUE
        AND drive_current_amps IS NOT NULL AND next_drive_current_amps IS NOT NULL
        AND """+recent("drive_current_cycle_ns")+" AND "+recent("drive_current_cycle_ns","next_")+""" AS drive_current_eligible,
      enabled IS TRUE AND next_enabled IS TRUE AND gap_ok
        AND turn_connected IS TRUE AND next_turn_connected IS TRUE
        AND turn_current_amps IS NOT NULL AND next_turn_current_amps IS NOT NULL
        AND """+recent("turn_current_cycle_ns")+" AND "+recent("turn_current_cycle_ns","next_")+""" AS turn_current_eligible
    FROM intervals"""


TRACKING_SQL="""
WITH eligible AS (SELECT * FROM intervals WHERE tracking_eligible IS TRUE),
weights AS (
 SELECT *,
    SUM(dt) OVER (PARTITION BY module_index ORDER BY ABS(drive_error) ROWS UNBOUNDED PRECEDING) AS drive_cume,
    SUM(dt) OVER (PARTITION BY module_index ORDER BY ABS(steering_error) ROWS UNBOUNDED PRECEDING) AS angle_cume,
    SUM(dt) OVER (PARTITION BY module_index) AS total_dt
 FROM eligible
), aggregate AS (
 SELECT module_index,COUNT(*) AS interval_count,SUM(dt) AS eligible_seconds,
        SQRT(SUM(drive_error*drive_error*dt)/SUM(dt)) AS drive_rmse_mps,
        SQRT(SUM(steering_error*steering_error*dt)/SUM(dt)) AS steering_rmse_rad,
        MIN(ABS(drive_error)) FILTER(WHERE drive_cume>=.95*total_dt) AS drive_p95_absolute_error_mps,
        MIN(ABS(steering_error)) FILTER(WHERE angle_cume>=.95*total_dt) AS steering_p95_absolute_error_rad
 FROM weights GROUP BY module_index
), coverage AS (
 SELECT module_index,MAX(module_position) AS module_position,
        SUM(dt) FILTER(WHERE enabled IS TRUE AND next_enabled IS TRUE AND dt>0) AS expected_enabled_seconds,
        COUNT(*) FILTER(WHERE enabled IS TRUE AND connected IS NOT TRUE) AS connection_unknown_or_disconnected_cycles,
        COUNT(*) FILTER(WHERE enabled IS TRUE AND mapping_valid IS NOT TRUE) AS mapping_unavailable_cycles
 FROM intervals GROUP BY module_index
)
SELECT c.*,COALESCE(a.interval_count,0) AS interval_count,COALESCE(a.eligible_seconds,0) AS eligible_seconds,
       a.drive_rmse_mps,a.steering_rmse_rad,a.drive_p95_absolute_error_mps,a.steering_p95_absolute_error_rad
FROM coverage c LEFT JOIN aggregate a USING(module_index) ORDER BY module_index
"""


def _currents(duck,side):
    return _fetch(duck,"""SELECT module_index,MAX(module_position) AS module_position,
          COUNT(*) FILTER(WHERE """+side+"""_current_eligible IS TRUE) AS interval_count,
          COALESCE(SUM(dt) FILTER(WHERE """+side+"""_current_eligible IS TRUE),0) AS eligible_seconds,
          SUM("""+side+"""_current_amps*dt) FILTER(WHERE """+side+"""_current_eligible IS TRUE)
             / SUM(dt) FILTER(WHERE """+side+"""_current_eligible IS TRUE) AS mean_amps,
          MAX("""+side+"""_current_amps) FILTER(WHERE """+side+"""_current_eligible IS TRUE) AS peak_amps
        FROM intervals GROUP BY module_index ORDER BY module_index""")


class Summarizer:
    def __init__(self,root,db,converter):
        self.root,self.db,self.converter=Path(root).resolve(),db,converter
        self.verified_reports=set()

    def _run_statistics(self,run,jobs,policy):
        key=identity([STATISTICS_VERSION,{k:run[k] for k in ("run_id","start_monotonic_ns","end_monotonic_ns","segment_ids")},
                      [job["id"] for job in jobs],policy])
        cached=self.db.execute("SELECT document_json FROM summary_run_cache WHERE id=?",(key,)).fetchone()
        if cached:
            return json.loads(cached[0]),True
        frames=[_path(self.root,"analytics/parquet/"+job["id"]+"/frames.parquet") for job in jobs]
        records=[_path(self.root,"analytics/parquet/"+job["id"]+"/records.parquet") for job in jobs]
        start,end=int(run["start_monotonic_ns"]),int(run["end_monotonic_ns"])
        manifest_ranges=[]
        overlap=False
        for job in jobs:
            source=Importer(self.root,self.db).get_job(job["import_job_id"])
            # Converter has already verified the import and derivative once this lifetime.
            manifest=json.loads(_path(self.root,source["manifest_path"]).read_text(encoding="utf-8"))
            a,b=max(start,int(manifest["first_cycle_timestamp_ns"])),min(end,int(manifest["last_cycle_timestamp_ns"]))
            if a<=b:
                if any(max(a,c)<=min(b,d) for c,d in manifest_ranges):
                    overlap=True
                manifest_ranges.append((a,b))
        with connection(self.root,threads=self.converter.threads,memory_mb=self.converter.memory_mb,
                        stopping=self.converter.stopping) as duck:
            duck.execute("CREATE TEMP VIEW intervals AS "+_window_query(frames,start,end,policy))
            tracking=_fetch(duck,TRACKING_SQL)
            currents={side:_currents(duck,side) for side in ("drive","turn")}
            sources="["+",".join(sql_path(path) for path in records)+"]"
            quality=_fetch(duck,"""SELECT kind,COUNT(*) AS row_count,
                 COUNT(*) FILTER(WHERE validity IS NOT NULL AND validity<>'valid') AS invalid_or_unsupported_count
                 FROM read_parquet("""+sources+""")
                 WHERE COALESCE(cycle_timestamp_ns,timestamp_ns,sample_timestamp_ns,record_timestamp_ns) BETWEEN ? AND ?
                 GROUP BY kind ORDER BY kind""",[start,end])
            stamps=_fetch(duck,"""SELECT COUNT(*) AS cycle_count,
                 COUNT(*) FILTER(WHERE next_timestamp_ns<=timestamp_ns) AS nonadvancing_or_overlapping_cycles,
                 COUNT(*) FILTER(WHERE next_timestamp_ns-timestamp_ns>?) AS long_gap_count,
                 MAX(next_timestamp_ns-timestamp_ns) AS largest_gap_ns,
                 COUNT(*) FILTER(WHERE enabled IS TRUE AND (build_hash IS NULL OR config_hash IS NULL)) AS missing_build_config_cycles
                 FROM intervals WHERE module_index=0""",[policy["max_gap_ns"]])[0]
            identities=_fetch(duck,"SELECT DISTINCT build_hash,config_hash FROM intervals WHERE enabled IS TRUE")
            artifacts=_fetch(duck,"SELECT DISTINCT artifact_hash FROM intervals WHERE enabled IS TRUE")
            sources="["+ ",".join(sql_path(path) for path in frames)+"]"
            stamps["source_nonadvancing_cycles"]=duck.execute("""SELECT COUNT(*) FROM (
                SELECT timestamp_ns,LAG(timestamp_ns) OVER(PARTITION BY import_job_id ORDER BY cycle_record_index) AS previous_ns
                FROM read_parquet("""+sources+""") WHERE module_index=0) WHERE timestamp_ns<=previous_ns""").fetchone()[0]
        for module in tracking:
            expected=module["expected_enabled_seconds"] or 0
            module["coverage_fraction"]=module["eligible_seconds"]/expected if expected else None
            module["qualified_for_hardware_health"]=False
            module["physical_acquisition_time_qualified"]=False
            module["metric_basis"]="time_weighted_recorded_values_with_receipt_age_limits"
        if overlap or stamps["nonadvancing_or_overlapping_cycles"] or stamps["source_nonadvancing_cycles"]:
            for module in tracking:
                for name in ("drive_rmse_mps","steering_rmse_rad","drive_p95_absolute_error_mps","steering_p95_absolute_error_rad"):
                    module[name]=None
                module["unavailable_reason"]="overlapping_or_nonadvancing_source_cycles"
            for side in currents:
                for module in currents[side]:
                    module["mean_amps"]=module["peak_amps"]=None
                    module["unavailable_reason"]="overlapping_or_nonadvancing_source_cycles"
        result={"cache_key":key,"quality":{"record_counts":quality,**stamps,
                     "overlapping_source_intervals":overlap,"physical_sensor_freshness_qualified":False},
                "tracking":tracking,"currents":currents,"recorded_build_config":identities,"recorded_artifact_hashes":artifacts}
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO summary_run_cache VALUES(?,?)",(key,_json(result).decode()))
        return result,False

    def generate(self):
        document=RunCatalog(self.db).current()
        review=review_snapshot(self.db)
        runs=document["runs"]
        if len(runs)>MAX_RUNS:
            raise ColumnarError("Summary run limit reached; no partial success")
        successful=self.db.execute("SELECT * FROM columnar_jobs WHERE version=? AND state='succeeded' ORDER BY id",(VERSION,)).fetchall()
        by_import={job["import_job_id"]:dict(job) for job in successful}
        job_ids=sorted({segment for run in runs for segment in run["segment_ids"]} |
                       {item[0] for item in document.get("inputs",[]) if item[0] in by_import})
        if any(segment not in by_import for segment in job_ids):
            raise ColumnarError("Waiting for all run datasets to convert")
        import_jobs=[Importer(self.root,self.db).get_job(segment) for segment in job_ids]
        if any(self.converter.key(job)!=by_import[job["id"]]["id"] for job in import_jobs):
            raise ColumnarError("Current conversion version unavailable")
        inputs={"version":SUMMARY_VERSION,"run_catalog_revision":document["revision"],"review_revision":review["revision"],
                "columnar_job_ids":[by_import[j]["id"] for j in job_ids]}
        report_id=identity(inputs)
        previous=self.db.execute("SELECT * FROM summary_reports WHERE id=?",(report_id,)).fetchone()
        if previous and previous["state"]=="succeeded":
            if report_id not in self.verified_reports:
                verify_artifacts(self.root,self.db,"summary_artifacts","report_id",report_id,self.converter.stopping)
                self.verified_reports.add(report_id)
            return dict(previous),0,len(runs)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO summary_reports(id,created_utc_ns,state,input_json) VALUES(?,?,'running',?)",
                            (report_id,time.time_ns(),_json(inputs).decode()))
        recordings=[]
        for job_id in job_ids:
            job=by_import[job_id]
            manifest=json.loads(_path(self.root,job["manifest_path"]).read_text(encoding="utf-8"))
            recordings.append({key:manifest[key] for key in ("id","source_sha256","import_job_id","source_type",
                "profile","record_count","frame_count","record_quality","unsupported_types",
                "first_cycle_timestamp_ns","last_cycle_timestamp_ns")})
        outputs=[]
        queried,cached=0,0
        for run in runs:
            if self.converter.stopping():
                raise ColumnarError("Analysis interrupted")
            selected,plan=resolve_plan(run,review)
            policy=_config(plan["swerve_configuration"] if plan else {})
            # Descriptive statistics use only the existing timing/demand policy.
            policy={k:policy[k] for k in ("max_gap_ns","max_sample_age_ns","min_demand_mps")}
            jobs=[by_import[segment] for segment in run["segment_ids"]]
            stats,reused=self._run_statistics(run,jobs,policy)
            cached+=int(reused)
            queried+=int(not reused)
            manifests=[json.loads(_path(self.root,job["manifest_path"]).read_text(encoding="utf-8")) for job in jobs]
            raw_types={m["source_type"] for m in manifests}
            source_type=("synthetic" if "SYNTHETIC" in raw_types else
                         "simulation" if run["runtime_mode"]=="SIM" else
                         "real" if run["runtime_mode"]=="REAL" else "historical")
            reasons=list(selected["analysis_plan_unavailable"])
            identities=stats["recorded_build_config"]
            if not plan:
                reasons.append("comparable_test_context_unavailable")
            elif len(identities)!=1 or identities[0]!= {"build_hash":plan["build_hash"],"config_hash":plan["config_hash"]} or stats["quality"]["missing_build_config_cycles"]:
                reasons.append("recorded_build_configuration_missing_changed_or_different_from_plan")
            if stats["quality"]["overlapping_source_intervals"] or stats["quality"]["nonadvancing_or_overlapping_cycles"] or stats["quality"]["source_nonadvancing_cycles"]:
                reasons.append("source_overlap")
            if any(m["coverage_fraction"] is None or m["coverage_fraction"]<.95 for m in stats["tracking"]):
                reasons.append("at_least_95_percent_recorded_tracking_coverage_required")
            context={k:selected.get(k) for k in ("build_hash","config_hash","test_id","surface","battery_id","wheel_radius_m","component_ids","assignment_references","maintenance_references")}
            context["recorded_artifact_hashes"]=stats["recorded_artifact_hashes"]
            context["maintenance_references"]=[event for event in context.get("maintenance_references") or []
                if int(event["effective_utc_ns"])<=int(selected.get("start_utc_ns","0"))]
            context.update(robot_id=run["robot_id"],runtime_mode=run["runtime_mode"],source_type=source_type,
                           profile=PROFILE,policy=policy,analysis_plan_id=plan.get("id") if plan else None)
            cohort=None if reasons else identity(context)
            outputs.append({"run":run,"source_type":source_type,"context":context,"cohort_id":cohort,
                            "comparison_unavailable":sorted(set(reasons)),"source_sha256":sorted({m["source_sha256"] for m in manifests}),
                            "statistics":stats})
        comparisons=aggregate_cohorts(outputs)
        created=self.db.execute("SELECT created_utc_ns FROM summary_reports WHERE id=?",(report_id,)).fetchone()[0]
        summary={"schema_version":1,"id":report_id,"created_utc_ns":str(created),"inputs":inputs,
                 "source_scope":"all_current_catalog_runs","recordings":recordings,"runs":outputs,"cohorts":comparisons,
                 "physical_acquisition_time_qualified":False,"hardware_thresholds_qualified":False,
                 "html_run_limit":MAX_HTML_RUNS,"html_pagination":True,"full_summary_run_count":len(outputs)}
        staging=_path(self.root,"analytics/staging")
        staging.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging) as temp_name:
            temp=Path(temp_name)
            (temp/"summary.json").write_bytes(_json(summary))
            (temp/"summary.csv").write_bytes(summary_csv(summary))
            (temp/"report.html").write_bytes(render_report(summary).encode("utf-8"))
            items=[]
            for name in FILES[:-1]:
                source=temp/name
                if source.stat().st_size>64*1024*1024:
                    raise ColumnarError("Summary artifact limit reached")
                relative="analytics/reports/"+report_id+"/"+name
                sha,size=publish_file(source,_path(self.root,relative))
                items.append({"path":relative,"sha256":sha,"size_bytes":size})
            manifest={"schema_version":1,"id":report_id,"version":SUMMARY_VERSION,
                      "created_utc_ns":str(created),"inputs":inputs,"artifacts":items,"run_count":len(runs)}
            (temp/"manifest.json").write_bytes(_json(manifest))
            relative="analytics/reports/"+report_id+"/manifest.json"
            sha,size=publish_file(temp/"manifest.json",_path(self.root,relative))
            items.append({"path":relative,"sha256":sha,"size_bytes":size})
            with self.db:
                for item in items:
                    self.db.execute("INSERT OR IGNORE INTO summary_artifacts VALUES(?,?,?,?)",
                                    (report_id,item["path"],item["sha256"],item["size_bytes"]))
                self.db.execute("UPDATE summary_reports SET state='succeeded',manifest_path=?,manifest_sha256=? WHERE id=?",
                                (relative,sha,report_id))
        self.verified_reports.add(report_id)
        return dict(self.db.execute("SELECT * FROM summary_reports WHERE id=?",(report_id,)).fetchone()),queried,cached


def aggregate_cohorts(outputs):
    """Descriptive per-position comparisons of distinct, already-qualified runs.

    Missing/ambiguous module entries and invalid metrics contribute no value;
    neither duplicated output rows nor shuffled modules can change identity.
    """
    cohorts={}
    for output in outputs:
        key=output.get('cohort_id')
        run_id=output.get('run',{}).get('run_id')
        if type(key) is str and key and type(run_id) is str and run_id:
            cohorts.setdefault(key,{}).setdefault(run_id,[]).append(output)
    comparisons=[]
    for key,runs in sorted(cohorts.items()):
        first=next(iter(runs.values()))[0]
        for position in MODULE_POSITIONS:
            values=[]
            for candidates in runs.values():
                metrics=[]
                for candidate in candidates:
                    matches=[module for module in candidate.get('statistics',{}).get('tracking',[])
                             if module.get('module_position')==position]
                    if len(matches)!=1:break
                    value=matches[0].get('drive_rmse_mps')
                    try:
                        if type(value) not in (int,float) or not math.isfinite(value):break
                        metrics.append(float(value))
                    except (OverflowError,ValueError):break
                else:
                    if len(set(metrics))==1:values.append(metrics[0])
            scale=max((abs(value) for value in values),default=0) or 1
            mean=scale*(math.fsum(value/scale for value in values)/len(values)) if values else None
            comparisons.append(dict(cohort_id=key,test_id=first.get('context',{}).get('test_id'),module_position=position,
                independent_run_count=len(runs),runs_with_recorded_metric=len(values),
                minimum_recorded_rmse_mps=min(values) if values else None,
                maximum_recorded_rmse_mps=max(values) if values else None,
                mean_recorded_run_rmse_mps=mean,
                health_verdict='not_evaluated',baseline_approved=False))
    return comparisons


def summary_csv(summary):
    buffer=io.StringIO(newline="")
    writer=csv.writer(buffer)
    writer.writerow(("run_id","robot_id","source_type","cohort_id","module_position","metric","value","unit",
                     "interval_count","eligible_seconds","physical_acquisition_qualified"))
    for output in summary["runs"]:
        run=output["run"]
        for name in ("cycle_count","long_gap_count","source_nonadvancing_cycles","nonadvancing_or_overlapping_cycles"):
            writer.writerow(tuple(_csv_cell(v) for v in (run["run_id"],run["robot_id"],output["source_type"],
                output["cohort_id"],"",name,output["statistics"]["quality"][name],"count","","",False)))
        for module in output["statistics"]["tracking"]:
            for name,unit in (("drive_rmse_mps","meters per second"),("steering_rmse_rad","radians"),
                              ("drive_p95_absolute_error_mps","meters per second"),("steering_p95_absolute_error_rad","radians")):
                writer.writerow(tuple(_csv_cell(v) for v in (run["run_id"],run["robot_id"],output["source_type"],output["cohort_id"],
                                  module["module_position"],name,module[name],unit,module["interval_count"],module["eligible_seconds"],False)))
        for side in ("drive","turn"):
            for module in output["statistics"]["currents"][side]:
                for name in ("mean_amps","peak_amps"):
                    writer.writerow(tuple(_csv_cell(v) for v in (run["run_id"],run["robot_id"],output["source_type"],output["cohort_id"],
                                      module["module_position"],side+"_"+name,module[name],"amperes",
                                      module["interval_count"],module["eligible_seconds"],False)))
    return buffer.getvalue().encode("utf-8")


def _csv_cell(value):
    if isinstance(value,str) and value.lstrip().startswith(("=","+","-","@")):
        return "'"+value
    return value
