"""Independent automatic-swerve tests use generated portable WPILOG protocol data.
Native AdvantageKit writer qualification belongs to the parent; no hardware proof.
"""
import copy
import http.client
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest

from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.review import ReviewStore
from robot_test_hub.swerve import MODULE_POSITIONS
from robot_test_hub.storage import open_catalog
from test_wpilog import log_bytes, start, wire_record

U=1800000000000000000
CONFIG='c'*64


def recording(runtime='SIM',duplicate=False,distinct=False,finish_measurement=False,late_regression=False):
    events=[];entries={};next_id=2
    def declare(field,kind,payload,metadata='{"source":"AdvantageKit"}'):
        nonlocal next_id
        entries[field]=next_id;next_id+=1
        events.extend((start(entries[field],field,kind,metadata),wire_record(entries[field],payload)))
    declare('/.schema/struct:Rotation2d','structschema',b'double value')
    declare('/.schema/struct:SwerveModuleVelocity','structschema',b'double velocity;Rotation2d angle')
    for field,value in (('/Metadata/RobotId','review-robot'),('/Metadata/BootId','review-boot'),
        ('/RealOutputs/TestHub/RobotId','review-robot'),('/RealOutputs/TestHub/BootId','review-boot'),
        ('/RealOutputs/TestHub/RuntimeMode',runtime),('/RealOutputs/TestHub/Mode','TELEOPERATED'),
        ('/RealOutputs/TestHub/ConfigurationSHA256',CONFIG)):
        declare(field,'string',value.encode())
    declare('/RealOutputs/TestHub/StateKnown','boolean',b'\x01')
    for field in ('/DriverStation/Enabled','/RealOutputs/TestHub/Enabled'):
        declare(field,'boolean',b'\x00')
    declare('/SystemStats/EpochTimeValid','boolean',b'\x01')
    declare('/SystemStats/EpochTime','double',struct.pack('<d',U//1000),'{"unit":"microseconds"}')
    for index in range(4):
        for field in ('DriveConnected','TurnConnected','TurnEncoderConnected'):
            declare(f'/Drive/Module{index}/'+field,'boolean',b'\x01')
    declare('/RealOutputs/SwerveStates/SetpointsOptimized','struct:SwerveModuleVelocity[]',struct.pack('<8d',*([2.,0.]*4)))
    # Measurements intentionally remain held throughout; receipt provenance must
    # survive the automatic adapter rather than becoming new physical samples.
    declare('/RealOutputs/SwerveStates/Measured','struct:SwerveModuleVelocity[]',struct.pack('<8d',*([1.5,0.]*4)))
    if distinct:declare('/Review/DistinctArtifact','string',b'additional unrelated source evidence')
    for index in range(1,21):
        ns=1000000000+index*100000000
        if duplicate and index==10:ns-=100000000
        if finish_measurement and index==15:
            events.append(wire_record(0,b'\x01'+struct.pack('<I',entries['/RealOutputs/SwerveStates/Measured']),ns//1000))
        events.append(wire_record(1,struct.pack('<q',ns),ns//1000))
        for field in ('/DriverStation/Enabled','/RealOutputs/TestHub/Enabled'):
            events.append(wire_record(entries[field],bytes([index<20]),ns//1000))
        events.append(wire_record(entries['/SystemStats/EpochTime'],struct.pack('<d',(U+ns-1000000000)//1000),ns//1000))
        events.append(wire_record(entries['/RealOutputs/SwerveStates/SetpointsOptimized'],struct.pack('<8d',*([2.,0.]*4)),ns//1000))
    if late_regression:events.append(wire_record(1,struct.pack('<q',2000000000),2000000))
    return log_bytes(*events)


class AutoSwerveReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.store=ReviewStore(self.db);self.pipeline=Pipeline(self.root,self.db)
        self.plan=dict(id='review-plan',robot_id='review-robot',start_utc_ns=str(U-1),end_utc_ns=None,
            reviewer='Synthetic reviewer',rationale='Independent explicit portable protocol case',
            build_hash='b'*64,config_hash=CONFIG,test_id='repeatable-tracking',surface='synthetic-flat',
            battery_id='review-battery',wheel_radius_m=.05,
            swerve_configuration={'drive_error_limit_mps':.25,'threshold_revision':'review-limits-1'},
            approved_baseline_id=None,ideal_simulation_cycle_policy=True)

    def import_recording(self,runtime='SIM',source_type='MANUAL_LOCAL',duplicate=False,distinct=False,finish_measurement=False):
        source=self.root/('protocol-'+str(len(list(self.root.glob('protocol-*.wpilog'))))+'.wpilog')
        body=recording(runtime,duplicate,distinct,finish_measurement);source.write_bytes(body)
        job=Importer(self.root,self.db).import_file(source,source_type=source_type)
        self.assertEqual(job['state'],'succeeded',job)
        return job,body

    def assignments(self,omit=None):
        for index,position in enumerate(MODULE_POSITIONS):
            if position==omit:continue
            self.store.assign_component(dict(id='assignment-'+str(index),robot_id='review-robot',
                component_id='physical-module-'+str(index),location=position,start_utc_ns=str(U-1),
                end_utc_ns=None,reviewer='Synthetic reviewer',rationale='Explicit module location'))

    def http(self):
        source=DemoSource();service=HubService(Config(data_dir=str(self.root)),source)
        self.addCleanup(service.close)
        server=create_http_server(service,source,0)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        self.addCleanup(lambda:(server.shutdown(),thread.join(2),server.server_close()))
        def request(method,path,payload=None,origin=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
            headers={'Content-Type':'application/json','X-Hub-Request':'1'}
            if origin:headers['Origin']=origin
            try:
                connection.request(method,path,json.dumps(payload) if payload is not None else None,headers)
                response=connection.getresponse()
                return response.status,json.loads(response.read())
            finally:connection.close()
        return request

    @staticmethod
    def swerve(report):
        return next(check for check in report['checks'] if check['analyzer_id']=='swerve-tracking')

    def save_plan(self,request,plan=None):
        status,body=request('POST','/api/v1/review/analysis-plans',plan or self.plan)
        self.assertEqual(status,200,body)
        return body

    def test_actual_import_metrics_held_provenance_and_review_only_regeneration(self):
        job,body=self.import_recording();self.assignments();request=self.http();self.save_plan(request)
        self.pipeline.tick();first=self.reports();self.assertEqual(len(first),1)
        report=first[0];check=self.swerve(report)
        self.assertEqual(report['source_type'],'simulation');self.assertEqual(check['outcome'],'finding')
        metrics=[m for m in check['metrics'] if m['name']=='drive_rmse_mps']
        self.assertEqual({m['module_position'] for m in metrics},set(MODULE_POSITIONS))
        self.assertEqual(len(metrics),4)
        for metric in metrics:self.assertAlmostEqual(metric['value'],.5)
        trace=[t for module in check['coverage']['modules'] for t in module['evidence_trace']]
        self.assertTrue(trace)
        self.assertTrue(all(t['source_reference']['command_source_hash']==job['artifact_sha256'] for t in trace))
        self.assertTrue(all(t['source_reference']['measured_updated_in_cycle'] is False for t in trace))
        self.assertEqual(len({t['source_reference']['measured_record_index'] for t in trace}),1)
        self.assertGreater(len({t['source_reference']['command_record_index'] for t in trace}),1)
        prior=self.db.execute('SELECT report_id,result_json FROM analysis_reports').fetchall()
        revised=copy.deepcopy(self.plan);revised.update(revision=2,expected_revision=1)
        revised['swerve_configuration']['drive_error_limit_mps']=.75
        self.save_plan(request,revised);self.pipeline.tick()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM import_jobs').fetchone()[0],1)
        all_reports=self.reports();self.assertEqual(len(all_reports),2)
        current=next(r for r in all_reports if r['report_id']!=report['report_id'])
        self.assertEqual(self.swerve(current)['outcome'],'evaluated_no_finding')
        for old in prior:
            stored=self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(old['report_id'],)).fetchone()[0]
            self.assertEqual(stored,old['result_json'])
        self.pipeline.tick();Pipeline(self.root,self.db).tick();self.assertEqual(len(self.reports()),2)
        raw=self.root/'raw'/job['artifact_sha256'][:2]/(job['artifact_sha256']+'.wpilog')
        self.assertEqual(raw.read_bytes(),body)

    def test_real_unknown_acquisition_is_insufficient(self):
        self.import_recording(runtime='REAL');self.assignments();request=self.http()
        plan=copy.deepcopy(self.plan);plan['ideal_simulation_cycle_policy']=False
        self.save_plan(request,plan);self.pipeline.tick();check=self.swerve(self.reports()[0])
        self.assertEqual(check['outcome'],'insufficient_data')
        self.assertEqual(check['coverage'].get('evaluated_modules',0),0)
        self.assertFalse(check['findings'])

    def test_synthetic_sim_label_cannot_promote_ideal_freshness(self):
        self.import_recording(source_type='SYNTHETIC');self.assignments();request=self.http();self.save_plan(request)
        self.pipeline.tick();report=self.reports()[0];check=self.swerve(report)
        self.assertEqual(report['source_type'],'synthetic')
        self.assertIn(check['outcome'],('insufficient_data','unsupported'))
        self.assertFalse(check['findings'])

    def test_missing_fourth_assignment_and_logged_config_mismatch_are_not_health(self):
        self.import_recording();self.assignments(omit=MODULE_POSITIONS[-1]);request=self.http();self.save_plan(request)
        self.pipeline.tick();check=self.swerve(self.reports()[0])
        self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])
        index=3;self.store.assign_component(dict(id='assignment-3',robot_id='review-robot',
            component_id='physical-module-3',location=MODULE_POSITIONS[3],start_utc_ns=str(U-1),end_utc_ns=None,
            reviewer='Synthetic reviewer',rationale='Complete fourth explicit assignment'))
        plan=copy.deepcopy(self.plan);plan.update(revision=2,expected_revision=1,config_hash='d'*64)
        self.save_plan(request,plan);self.pipeline.tick()
        newest=self.swerve(self.reports()[-1]);self.assertEqual(newest['outcome'],'insufficient_data')
        self.assertFalse(newest['findings'])

    def test_duplicate_cycles_rejected_before_automatic_analysis(self):
        source=self.root/'duplicate.wpilog';source.write_bytes(recording(duplicate=True))
        job=Importer(self.root,self.db).import_file(source,source_type='MANUAL_LOCAL')
        self.assertEqual(job['state'],'invalid')
        self.assertEqual(job['error_code'],'invalid_format')
        self.assertIsNone(job['dataset_path'])
        self.assignments();self.store.record_analysis_plan(self.plan);self.pipeline.tick()
        self.assertFalse(self.reports())

    def test_cross_artifact_overlapping_cycles_are_not_deduplicated_into_health(self):
        first,_=self.import_recording();second,_=self.import_recording(distinct=True)
        self.assertNotEqual(first['artifact_sha256'],second['artifact_sha256'])
        self.assignments();request=self.http();self.save_plan(request);self.pipeline.tick()
        reports=self.reports();self.assertTrue(reports)
        for report in reports:
            check=self.swerve(report)
            self.assertEqual(check['outcome'],'insufficient_data')
            self.assertFalse(check['findings'])
            self.assertIn('overlapping_source_cycle_intervals',check['unavailable'])

    def test_finished_measurement_state_cannot_qualify_good_prefix(self):
        self.import_recording(finish_measurement=True);self.assignments()
        request=self.http();self.save_plan(request);self.pipeline.tick()
        check=self.swerve(self.reports()[0])
        self.assertEqual(check['outcome'],'insufficient_data')
        self.assertFalse(check['findings'])
        self.assertIn('whole_run_swerve_state_coverage_required',check['unavailable'])

    def test_late_cycle_regression_cannot_hide_after_closed_run(self):
        source=self.root/'after-run-regression.wpilog'
        source.write_bytes(recording(late_regression=True))
        job=Importer(self.root,self.db).import_file(source,source_type='MANUAL_LOCAL')
        self.assertEqual(job['state'],'invalid')
        self.assertEqual(job['error_code'],'invalid_format')
        self.assertIsNone(job['dataset_path'])
        self.assignments();self.store.record_analysis_plan(self.plan);self.pipeline.tick()
        self.assertFalse(self.reports())

    def test_battery_boundary_review_only_rebuild_preserves_old_report(self):
        self.import_recording();self.assignments();request=self.http();self.save_plan(request)
        self.pipeline.tick();before=self.reports()[0]
        self.assertEqual(self.swerve(before)['outcome'],'finding')
        self.store.record_maintenance(dict(id='battery-boundary',robot_id='review-robot',
            kind='battery_change',effective_utc_ns=str(U+1000000000),
            reviewer='Synthetic reviewer',rationale='Different battery interrupts compatible interval'))
        self.pipeline.tick();reports=self.reports();self.assertEqual(len(reports),2)
        current=next(r for r in reports if r['report_id']!=before['report_id'])
        check=self.swerve(current)
        self.assertEqual(check['outcome'],'insufficient_data')
        self.assertIn('maintenance_splits_run',check['unavailable'])
        self.assertFalse(check['findings'])
        self.assertEqual(next(r for r in reports if r['report_id']==before['report_id']),before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM import_jobs').fetchone()[0],1)

    def reports(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT result_json FROM analysis_reports ORDER BY created_utc_ns')]


if __name__=='__main__':unittest.main()
