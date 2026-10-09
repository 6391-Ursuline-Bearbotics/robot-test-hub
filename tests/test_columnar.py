"""Synthetic analytics regressions; no hardware or cloud credentials."""
from contextlib import closing
import importlib.util
import copy
import http.client
import io
import time
import json
import math
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.analytics import AnalyticsWorker, report_file
from robot_test_hub.backup import Backup, restore, verify
from robot_test_hub.columnar import (Converter,ColumnarError,AnalysisInterrupted,
    FRAME_TYPES,normalize_frames,connection,sql_path)
from robot_test_hub.config import Config,ConfigError
from robot_test_hub.importer import Importer
from robot_test_hub.log_export import LogExporter
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog,SCHEMA_VERSION
from robot_test_hub.analysis_plan import review_snapshot
from robot_test_hub.summaries import Summarizer,_window_query,TRACKING_SQL,_fetch,_currents,render_report
from robot_test_hub.swerve import ROBOT_FIELDS
from test_auto_swerve import protocol_recording
from test_wpilog import FIXTURES,log_bytes,start,wire_record
from robot_test_hub.wpilog import records
from robot_test_hub.review import ReviewStore
from robot_test_hub.swerve import MODULE_POSITIONS
from robot_test_hub.service import HubService
from robot_test_hub.server import create_http_server
from test_foundation import SmallSource,wait_for

POLICY={"max_gap_ns":250000000,"max_sample_age_ns":100000000,"min_demand_mps":.1}
AVAILABLE=importlib.util.find_spec("duckdb") is not None


def frame_rows(stamps=(1000000000,1020000000,1080000000,1100000000),*,errors=(.5,1,2,0)):
    for cycle,stamp in enumerate(stamps):
        for module in range(4):
            row={name:None for name in FRAME_TYPES}
            row.update(import_job_id="invented",source_sha256="a"*64,timestamp_ns=stamp,
                cycle_record_index=cycle,module_index=module,module_position=("front-left","front-right","back-left","back-right")[module],
                runtime_mode="SIM",enabled=True,mapping_valid=True,connected=True,drive_connected=True,turn_connected=True,
                command_speed_mps=2.,measured_speed_mps=2+errors[cycle],
                command_angle_rad=math.pi-.05,measured_angle_rad=-math.pi+.05,
                command_cycle_ns=stamp,measured_cycle_ns=stamp,
                drive_current_amps=10+cycle,turn_current_amps=2+cycle,
                drive_current_cycle_ns=stamp,turn_current_cycle_ns=stamp,
                build_hash="a"*64,config_hash="b"*64)
            yield row


@unittest.skipUnless(AVAILABLE,"Install optional analytics extra")
class ColumnarTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"data";self.root.mkdir()
        self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.converter=Converter(self.root,self.db)

    def import_log(self,payload=None):
        source=Path(self.temp.name)/"input.wpilog"
        source.write_bytes(payload if payload is not None else protocol_recording())
        return Importer(self.root,self.db).import_file(source,source_type="SYNTHETIC")

    def converted(self,payload=None):
        job=self.import_log(payload)
        self.assertIn(job["state"],("succeeded","succeeded_with_unsupported"))
        return job,self.converter.convert(job["id"])

    def report(self):
        self.converted()
        rebuild_from_imports(self.root,self.db)
        return Summarizer(self.root,self.db,self.converter).generate()[0]

    def query(self,rows,policy=None):
        path=self.root/"query.jsonl";path.write_text("".join(json.dumps(row)+"\n" for row in rows),encoding="utf-8")
        parquet=self.root/"query.parquet"
        with connection(self.root) as duck:
            cols="{"+",".join(sql_path(k)+":"+sql_path(v) for k,v in FRAME_TYPES.items())+"}"
            duck.execute("COPY (SELECT * FROM read_json("+sql_path(path)+",format='newline_delimited',columns="+cols+")) TO "+sql_path(parquet)+" (FORMAT PARQUET)")
            duck.execute("CREATE TEMP VIEW intervals AS "+_window_query([parquet],0,2**63-1,policy or POLICY))
            return _fetch(duck,TRACKING_SQL),_currents(duck,"drive")

    def test_lossless_records_and_nanoseconds_above_float_precision(self):
        stamp=2**53+123
        payload=log_bytes(start(2,"/Synthetic/Int","int64"),wire_record(2,struct.pack("<q",stamp)),
            wire_record(1,struct.pack("<q",stamp),1000200),
            start(3,"/Synthetic/Array","double[]"),wire_record(3,struct.pack("<2d",.25,2.5),1000200))
        job,converted=self.converted(payload)
        original=list(Importer(self.root,self.db).iter_dataset(job["id"]))
        with connection(self.root) as duck:
            rows=duck.execute("SELECT row_json,timestamp_ns,value_int64 FROM read_parquet(?) ORDER BY row_ordinal",
                [str(self.root/"analytics/parquet"/converted["id"]/"records.parquet")]).fetchall()
        self.assertEqual([json.loads(row[0]) for row in rows],original)
        self.assertIn(stamp,[row[1] for row in rows]);self.assertIn(stamp,[row[2] for row in rows])

    def test_official_reader_fixture_full_rows_preserved(self):
        fixture=next(p for p in FIXTURES.glob("*.wpilog"))
        job,converted=self.converted(fixture.read_bytes())
        original=list(Importer(self.root,self.db).iter_dataset(job["id"]))
        with connection(self.root) as duck:
            rows=duck.execute("SELECT row_json FROM read_parquet(?) ORDER BY row_ordinal",
                [str(self.root/"analytics/parquet"/converted["id"]/"records.parquet")]).fetchall()
        self.assertEqual([json.loads(row[0]) for row in rows],original)

    def test_repeat_conversion_does_not_read_jsonl(self):
        job,converted=self.converted()
        with patch("robot_test_hub.columnar.normalize_frames",side_effect=AssertionError("No rescan")):
            self.assertEqual(self.converter.convert(job["id"])["id"],converted["id"])

    def test_corrupt_derivative_detected_after_restart(self):
        job,converted=self.converted()
        (self.root/"analytics/parquet"/converted["id"]/"frames.parquet").write_bytes(b"changed")
        with self.assertRaises(ColumnarError):
            Converter(self.root,self.db).convert(job["id"])

    def test_corrupt_import_is_rejected_with_durable_retry(self):
        job=self.import_log()
        (self.root/job["dataset_path"]).write_bytes(b"changed")
        with self.assertRaises(ColumnarError):self.converter.convert(job["id"])
        row=self.db.execute("SELECT * FROM columnar_jobs").fetchone()
        self.assertEqual(row["state"],"retry_wait");self.assertGreater(row["next_retry_utc_ns"],0)

    def test_retry_after_partial_publication_reuses_identical_bytes(self):
        from robot_test_hub import columnar
        job=self.import_log();original=columnar.publish_file;count=0
        def interrupted(source,target):
            nonlocal count
            count+=1
            if count==2:raise OSError("Invented publication failure")
            return original(source,target)
        with patch("robot_test_hub.columnar.publish_file",side_effect=interrupted):
            with self.assertRaises(OSError):self.converter.convert(job["id"])
        self.assertEqual(self.converter.convert(job["id"])["state"],"succeeded")

    def test_summary_failure_does_not_block_original_publication(self):
        self.report();destination=Path(self.temp.name)/"drive";destination.mkdir()
        exporter=LogExporter(self.root,destination,include_summaries=True)
        with patch.object(exporter,"_summaries",side_effect=OSError("Synthetic report failure")):
            exporter.tick()
        self.assertEqual(exporter.snapshot["state"],"retry_wait")
        self.assertTrue(list(destination.rglob("*.wpilog")))
        exporter.tick();self.assertEqual(exporter.snapshot["state"],"idle")

    def test_pause_and_retry(self):
        job=self.import_log()
        with self.assertRaises(AnalysisInterrupted):
            Converter(self.root,self.db,stopping=lambda:True).convert(job["id"])
        result=self.converter.convert(job["id"])
        self.assertEqual(result["state"],"succeeded")
        self.assertEqual(result["attempts"],2)

    def test_native_query_interrupted(self):
        stopped=threading.Event()
        with self.assertRaises(Exception):
            with connection(self.root,stopping=stopped.is_set) as duck:
                timer=threading.Timer(.08,stopped.set);timer.start()
                try:duck.execute("SELECT SUM(SIN(i::DOUBLE)) FROM range(10000000000) t(i)").fetchone()
                finally:timer.cancel()

    def test_irregular_weighted_rms_percentile_and_angle_wrap(self):
        tracking,currents=self.query(list(frame_rows()))
        expected=math.sqrt((.5**2*.02+1**2*.06+2**2*.02)/.1)
        for module in tracking:
            self.assertAlmostEqual(module["drive_rmse_mps"],expected,places=12)
            self.assertAlmostEqual(module["steering_rmse_rad"],.1,places=12)
            self.assertEqual(module["drive_p95_absolute_error_mps"],2)
            self.assertAlmostEqual(module["eligible_seconds"],.1,places=12)
        for module in currents:
            self.assertAlmostEqual(module["mean_amps"],11)
            self.assertEqual(module["peak_amps"],12)

    def test_unknown_connections_missing_current_and_stale_hold_not_zero(self):
        rows=list(frame_rows())
        for row in rows:
            row["connected"]=None;row["drive_connected"]=None;row["drive_current_amps"]=None
            row["measured_cycle_ns"]=0
        tracking,currents=self.query(rows)
        self.assertTrue(all(m["drive_rmse_mps"] is None for m in tracking))
        self.assertTrue(all(m["mean_amps"] is None for m in currents))

    def test_near_zero_demand_is_not_tracking_evidence(self):
        rows=list(frame_rows())
        for row in rows:row["command_speed_mps"]=.01
        tracking,currents=self.query(rows)
        self.assertTrue(all(m["drive_rmse_mps"] is None for m in tracking))
        self.assertTrue(all(m["mean_amps"] is not None for m in currents))

    def test_gap_not_bridged(self):
        tracking,_=self.query(list(frame_rows(stamps=(1000000000,1020000000,2020000000,2040000000))))
        self.assertTrue(all(abs(m["eligible_seconds"]-.04)<1e-12 for m in tracking))

    def test_disabled_only_recording_still_has_whole_file_quality(self):
        original=protocol_recording();enabled_entry=None;events=[]
        for record in records(io.BytesIO(original)):
            if record.entry==0 and record.payload[0]==0:
                size=int.from_bytes(record.payload[5:9],"little")
                if record.payload[9:9+size]==b"/DriverStation/Enabled":
                    enabled_entry=int.from_bytes(record.payload[1:5],"little")
            events.append(wire_record(record.entry,bytes([0]) if record.entry==enabled_entry else record.payload,record.timestamp_us))
        size=12+int.from_bytes(original[8:12],"little")
        self.converted(original[:size]+b"".join(events));rebuild_from_imports(self.root,self.db)
        report=Summarizer(self.root,self.db,self.converter).generate()[0]
        doc=json.loads(report_file(self.root,report["id"],"summary.json"))
        self.assertFalse(doc["runs"]);self.assertEqual(len(doc["recordings"]),1)
        self.assertGreater(doc["recordings"][0]["record_count"],0)

    def test_completed_report_cached_before_native_queries(self):
        report=self.report()
        summarizer=Summarizer(self.root,self.db,self.converter)
        with patch.object(summarizer,"_run_statistics",side_effect=AssertionError("No repeated query")):
            self.assertEqual(summarizer.generate()[0]["id"],report["id"])

    def test_run_cache_survives_report_revision_changes(self):
        self.report()
        review=review_snapshot(self.db);review["revision"]="changed-review"
        summarizer=Summarizer(self.root,self.db,self.converter)
        with patch("robot_test_hub.summaries.connection",side_effect=AssertionError("No repeated query")), patch("robot_test_hub.summaries.review_snapshot",return_value=review):
            _,queried,cached=summarizer.generate()
        self.assertEqual(queried,0);self.assertGreater(cached,0)

    def test_missing_physical_timing_and_context_no_health_or_comparison(self):
        report=self.report()
        summary=json.loads(report_file(self.root,report["id"],"summary.json"))
        self.assertFalse(summary["physical_acquisition_time_qualified"])
        self.assertFalse(summary["cohorts"])
        for run in summary["runs"]:
            self.assertEqual(run["source_type"],"synthetic")
            self.assertTrue(run["comparison_unavailable"])
            self.assertTrue(all(not m["qualified_for_hardware_health"] for m in run["statistics"]["tracking"]))

    def test_report_download_exact_allowlist_hash_and_html_escape(self):
        report=self.report();original=report_file(self.root,report["id"],"report.html")
        self.assertIn(b"recorded",original)
        with self.assertRaises(KeyError):report_file(self.root,report["id"],"../../catalog.sqlite3")
        summary=json.loads(report_file(self.root,report["id"],"summary.json"))
        summary["runs"][0]["run"]["robot_id"]="<script>alert(1)</script>"
        self.assertNotIn("<script>",render_report(summary));self.assertIn("&lt;script&gt;",render_report(summary))
        path=self.root/"analytics/reports"/report["id"]/"report.html";path.write_bytes(b"corrupt")
        with self.assertRaises(ColumnarError):report_file(self.root,report["id"],"report.html")

    def test_background_worker_and_pause(self):
        self.import_log();rebuild_from_imports(self.root,self.db)
        config=Config(data_dir=str(self.root),analytics_enabled=True)
        worker=AnalyticsWorker(self.root,self.db,config)
        self.assertEqual(worker.tick()["state"],"processing")
        result=worker.tick()
        self.assertEqual(result["state"],"idle",result)
        self.assertTrue(result["latest_report_id"])
        paused=AnalyticsWorker(self.root,self.db,config,stopping=lambda:True)
        self.assertEqual(paused.tick()["state"],"paused")

    def test_indexer_waits_for_new_import(self):
        self.import_log()
        worker=AnalyticsWorker(self.root,self.db,Config(data_dir=str(self.root),analytics_enabled=True))
        worker.tick()
        self.assertEqual(worker.tick()["state"],"waiting_for_index")

    def test_missing_dependency_actionable_and_no_silent_success(self):
        with patch("robot_test_hub.analytics.dependency",side_effect=ColumnarError("Install analytics")):
            with self.assertRaises(ColumnarError):
                AnalyticsWorker(self.root,self.db,Config(data_dir=str(self.root)))

    def test_summary_sharing_optin_only_small_artifacts(self):
        report=self.report();destination=Path(self.temp.name)/"drive";destination.mkdir()
        exporter=LogExporter(self.root,destination)
        exporter.tick();self.assertFalse((destination/"reports").exists())
        exporter=LogExporter(self.root,destination,include_summaries=True);exporter.tick()
        names={p.name for p in (destination/"reports"/report["id"]).iterdir()}
        self.assertEqual(names,{"report.html","summary.csv","summary.json","manifest.json"})
        self.assertFalse(list(destination.rglob("*.parquet")))
        self.assertFalse(list(destination.rglob("*.sqlite3")))
        self.assertFalse(exporter.snapshot["cloud_upload_confirmed"])
        before=(destination/"reports"/report["id"]/"report.html").stat().st_mtime_ns
        exporter.tick()
        self.assertEqual((destination/"reports"/report["id"]/"report.html").stat().st_mtime_ns,before)

    def test_summary_conflict_preserved_then_retry(self):
        report=self.report();destination=Path(self.temp.name)/"drive";destination.mkdir()
        exporter=LogExporter(self.root,destination,include_summaries=True);exporter.tick()
        path=destination/"reports"/report["id"]/"report.html";original=path.read_bytes();path.write_bytes(b"changed")
        exporter.tick()
        self.assertEqual(exporter.snapshot["state"],"retry_wait")
        self.assertEqual(path.read_bytes(),b"changed")
        path.write_bytes(original);exporter.tick();self.assertEqual(exporter.snapshot["state"],"idle")

    def test_backup_restore_includes_registered_derivatives(self):
        report=self.report()
        backup=Backup(self.root,Path(self.temp.name)/"backup")
        result=backup.create("analytics")
        self.assertEqual(result["state"],"complete")
        folder=Path(self.temp.name)/"backup/analytics"
        verify(folder)
        recovered=Path(self.temp.name)/"restored";restore(folder,recovered)
        self.assertEqual(report_file(recovered,report["id"],"summary.json"),report_file(self.root,report["id"],"summary.json"))
        self.assertEqual(len(list(recovered.rglob("*.parquet"))),2)

    def test_comparison_context_and_incremental_new_run(self):
        from test_auto_swerve import UTC
        # Update exact encoded state samples each cycle, rather than claiming held values are fresh.
        # Source-order fixture gets refreshed from its actual declarations, no assumed entry IDs.
        def fresh(boot):
            original=protocol_recording().replace(b"author-boot",boot.encode())
            active={};events=[]
            for record in records(io.BytesIO(original)):
                if record.entry==0 and record.payload[0]==0:
                    entry=int.from_bytes(record.payload[1:5],"little")
                    size=int.from_bytes(record.payload[5:9],"little")
                    active[record.payload[9:9+size].decode()]=entry
                if record.entry==1 and record.timestamp_us>1000000:
                    for name,velocity in ((ROBOT_FIELDS["commands"],2.),(ROBOT_FIELDS["measured"],1.5)):
                        events.append(wire_record(active[name],struct.pack("<8d",*([velocity,0.]*4)),record.timestamp_us))
                events.append(wire_record(record.entry,record.payload,record.timestamp_us))
            header_size=12+int.from_bytes(original[8:12],"little")
            return original[:header_size]+b"".join(events)
        store=ReviewStore(self.db)
        for i,position in enumerate(MODULE_POSITIONS):
            store.assign_component(dict(id="assignment-"+str(i),robot_id="author-robot",component_id="module-"+str(i),
                location=position,start_utc_ns=str(UTC-1000000),end_utc_ns=None,reviewer="Synthetic",rationale="Synthetic assignment"))
        plan=dict(id="plan",robot_id="author-robot",start_utc_ns=str(UTC-1000000),end_utc_ns=None,
            reviewer="Synthetic",rationale="Synthetic repeated test",build_hash="a"*64,config_hash="b"*64,
            test_id="repeat",surface="invented-flat",battery_id="invented",wheel_radius_m=.05,swerve_configuration={})
        store.record_analysis_plan(plan)
        self.converted(fresh("author-boot"));rebuild_from_imports(self.root,self.db)
        summary=Summarizer(self.root,self.db,self.converter)
        first,queried,cached=summary.generate()
        doc=json.loads(report_file(self.root,first["id"],"summary.json"))
        self.assertTrue(doc["cohorts"],doc["runs"][0]["comparison_unavailable"])
        self.converted(fresh("second-boot"));rebuild_from_imports(self.root,self.db)
        second,queried,cached=summary.generate()
        self.assertGreater(queried,0);self.assertGreater(cached,0)
        doc=json.loads(report_file(self.root,second["id"],"summary.json"))
        self.assertTrue(any(c["independent_run_count"]==2 for c in doc["cohorts"]))

    def test_native_source_order_regression_is_not_sorted_away(self):
        job,converted=self.converted()
        frames=self.root/"analytics/parquet"/converted["id"]/"frames.parquet"
        with connection(self.root) as duck:
            duck.execute("CREATE TABLE damaged AS SELECT * FROM read_parquet(?)",[str(frames)])
            duck.execute("UPDATE damaged SET timestamp_ns=999999999 WHERE cycle_record_index=(SELECT MAX(cycle_record_index) FROM damaged)")
            alternate=self.root/"damaged.parquet";duck.execute("COPY damaged TO "+sql_path(alternate)+" (FORMAT PARQUET)")
        # Isolated manufactured input to statistics, never re-register corrupt bytes as real evidence.
        frames.write_bytes(alternate.read_bytes())
        rebuild_from_imports(self.root,self.db)
        run=rebuild_from_imports(self.root,self.db)["runs"][0]
        stats,_=Summarizer(self.root,self.db,self.converter)._run_statistics(run,[converted],POLICY)
        self.assertGreater(stats["quality"]["source_nonadvancing_cycles"],0)
        self.assertTrue(all(m["drive_rmse_mps"] is None for m in stats["tracking"]))

    def test_service_api_and_shutdown_with_analytics_enabled(self):
        report=self.report()
        source=SmallSource()
        service=HubService(Config(data_dir=str(self.root),analytics_enabled=True,analytics_interval=.02),source)
        server=create_http_server(service,source,0);thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            service.start()
            wait_for(lambda:service.snapshot()["analytics"]["latest_report_id"] is not None)
            request=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
            try:
                for path in ("/summaries","/api/v1/summaries","/summaries/"+report["id"]+"/report.html"):
                    request.request("GET",path);response=request.getresponse()
                    self.assertEqual(response.status,200);self.assertTrue(response.read())
            finally:request.close()
            source.enabled=True
            wait_for(lambda:service.snapshot()["analytics"]["state"]=="paused")
            source.enabled=False
            wait_for(lambda:service.snapshot()["analytics"]["state"]=="idle")
        finally:
            server.shutdown();thread.join();server.server_close();self.assertTrue(service.close())

    def test_migration_from_version_four_preserves_settings(self):
        with self.db:
            self.db.execute("INSERT INTO settings VALUES('analytics-test','keep')")
            for table in ("columnar_artifacts","columnar_jobs","summary_artifacts","summary_reports","summary_run_cache"):
                self.db.execute("DROP TABLE "+table)
            self.db.execute("DELETE FROM schema_migrations WHERE version=5")
            self.db.execute("PRAGMA user_version=4")
        self.db.close()
        self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0],SCHEMA_VERSION)
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='analytics-test'").fetchone()[0],"keep")


class PortableColumnarTests(unittest.TestCase):
    def test_config_optin_and_bounds(self):
        self.assertFalse(Config().analytics_enabled)
        self.assertFalse(Config().analytics_share_summaries)
        for options in ({"analytics_threads":0},{"analytics_threads":5},{"analytics_memory_mb":127},
                        {"analytics_interval":0},{"analytics_enabled":"yes"}):
            with self.assertRaises(ConfigError):Config(**options)

    def test_control_finish_clears_measurement_and_units_rejected(self):
        def observation(field,value,kind="struct:SwerveModuleVelocity[]",unit=None):
            return {"kind":"observation","field":field,"type":kind,"unit":unit,"validity":"valid",
                    "value":value,"entry_id":2 if field==ROBOT_FIELDS["measured"] else 3,
                    "cycle_timestamp_ns":"1000000000","record_index":4}
        values=[{"velocity":2.,"angle":{"value":0.}}]*4
        rows=[observation("/.schema/struct:Rotation2d","double value","structschema"),
              observation("/.schema/struct:SwerveModuleVelocity","double velocity;Rotation2d angle","structschema"),
              observation(ROBOT_FIELDS["commands"],values),observation(ROBOT_FIELDS["measured"],values),
              {"kind":"cycle","timestamp_ns":"1000000000","timestamp_record_index":5,"aliases":{}},
              {"kind":"control","control":"finish","entry_id":2,"field":ROBOT_FIELDS["measured"]},
              {"kind":"cycle","timestamp_ns":"1020000000","timestamp_record_index":6,"aliases":{}}]
        frames=list(normalize_frames(rows,"test","a"*64))
        self.assertTrue(frames[0]["mapping_valid"]);self.assertFalse(frames[4]["mapping_valid"])
        rows[3]["unit"]="feet"
        self.assertFalse(list(normalize_frames(rows[:5],"test","a"*64))[0]["mapping_valid"])


if __name__=="__main__":unittest.main()
