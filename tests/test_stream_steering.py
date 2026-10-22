"""Policy11 integration and bounded exact work; invented recordings stay SYNTHETIC."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import struct
import unittest
from unittest.mock import patch
from robot_test_hub import reports, stream_analysis, stream_steering as steering, stream_tracking_store as work
from robot_test_hub.stream_motion import MotionReducer, PIPELINE
from robot_test_hub.storage import open_catalog
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.review import ReviewStore
import test_stream_tracking as tracking_tests
from test_auto_swerve import protocol_recording,UTC
from test_wpilog import log_bytes,start,wire_record
from test_stream_steering_review import context,sample

def dense_original():
    events=[];fields={}
    def field(name,kind,value,metadata='{"source":"AdvantageKit"}'):
        entry=len(fields)+2;fields[name]=entry
        events.extend([start(entry,name,kind,metadata),wire_record(entry,value)])
    field('/.schema/struct:Rotation2d','structschema',b'double value')
    field('/.schema/struct:SwerveModuleVelocity','structschema',b'double velocity;Rotation2d angle')
    for name,value in [('/RealOutputs/TestHub/RobotId','author-robot'),('/RealOutputs/TestHub/BootId','author-boot'),
        ('/RealOutputs/TestHub/RuntimeMode','SIM'),('/RealOutputs/TestHub/Mode','TELEOPERATED'),
        ('/RealOutputs/TestHub/ConfigurationSHA256','b'*64),('/RealMetadata/SourceSHA256','a'*64)]:field(name,'string',value.encode())
    field('/DriverStation/Enabled','boolean',b'\0')
    field('/SystemStats/EpochTimeValid','boolean',b'\1')
    field('/SystemStats/EpochTime','double',struct.pack('<d',UTC/1000),'{"unit":"microseconds"}')
    for module in range(4):
        for name in ('DriveConnected','TurnConnected','TurnEncoderConnected'):field(f'/Drive/Module{module}/'+name,'boolean',b'\1')
        field(f'/Drive/Module{module}/DriveVelocityRadPerSec','double',struct.pack('<d',40.))
        field(f'/Drive/Module{module}/TurnPosition','struct:Rotation2d',struct.pack('<d',0.))
    command='/RealOutputs/SwerveStates/SetpointsOptimized'
    field(command,'struct:SwerveModuleVelocity[]',struct.pack('<8d',*([2.,0.]*4)))
    for i in range(1,432):
        stamp=1000000000+i*100000000;target=.3 if i%2 or i>400 else 0.;actual=(.3 if i%2 else -.3) if i<=400 else 0.
        events.append(wire_record(1,struct.pack('<q',stamp),stamp//1000))
        events.append(wire_record(fields['/DriverStation/Enabled'],bytes([i<431]),stamp//1000))
        events.append(wire_record(fields['/SystemStats/EpochTime'],struct.pack('<d',(UTC+stamp-1000000000)/1000),stamp//1000))
        events.append(wire_record(fields[command],b'' if i==200 else struct.pack('<8d',*([2.,target]*4)),stamp//1000))
        for module in range(4):events.append(wire_record(fields[f'/Drive/Module{module}/TurnPosition'],struct.pack('<d',actual),stamp//1000))
    return log_bytes(*events)


class SteeringStoreTests(unittest.TestCase):
    def test_twelve_fixed_exact_targets_restart_and_rehashed_forgery(self):
        with tempfile.TemporaryDirectory() as temp:
            db=open_catalog(Path(temp))
            try:
                state=work.initial(work.MOTION_VERSION)
                for i,(value,ns) in enumerate(((1.,19),(9.,1))):
                    with db:
                        for module in range(4):
                            for name in work.MOTION_METRICS:work.contribute(db,'new',1,state,i,dict(stream_source_ordinal=0,stream_line_ordinal=i),module,name,value,ns)
                for _ in range(100):
                    with db:final=work.finalize_step(db,'new',1,state,1,1.,lambda:0.)
                    if final['complete']:break
                self.assertEqual(final['metric_index'],12);self.assertEqual(len(final['quantiles']),12)
                self.assertTrue(all(v==1. for v in final['quantiles'].values()))
                valid=work.validation_initial(work.MOTION_VERSION)
                for _ in range(200):
                    if work.validate_step(db,'new',1,state,valid,1,1.,lambda:0.):break
                else:self.fail('validation failed to finish')
                final['quantiles']['3:recorded_steering']=99.;raw=json.dumps(final,sort_keys=True,separators=(',',':'))
                db.execute('UPDATE analysis_tracking_finalization SET state_json=?,state_sha256=?',(raw,hashlib.sha256(raw.encode()).hexdigest()))
                valid=work.validation_initial(work.MOTION_VERSION)
                with self.assertRaisesRegex(ValueError,'semantic_mismatch'):
                    for _ in range(200):work.validate_step(db,'new',1,state,valid,1,1.,lambda:0.)
            finally:db.close()

    def test_missing_snapshot_boundary_preserves_denominator_and_censors(self):
        state=steering.initial_steering(context(.5));pairs=[]
        for seconds in (0.,.1):
            for module in range(4):steering.consume_sample(state,sample(module,seconds),lambda *v:pairs.append(v),lambda *v:True)
        steering.boundary(state,'missing_cycle')
        for module in range(4):steering.consume_sample(state,sample(module,.5),lambda *v:pairs.append(v),lambda *v:True)
        steering.flush(state)
        for module in state['modules']:
            self.assertEqual(module['expected_ns'],'500000000');self.assertEqual(module['eligible_ns'],'100000000')
            self.assertEqual(module['recorded_interval_count'],1)

class SteeringOriginalTests(unittest.TestCase):
    fixture=tracking_tests.TrackingOriginalTests.fixture
    runner=tracking_tests.TrackingOriginalTests.runner
    drain=tracking_tests.TrackingOriginalTests.drain
    def test_policy11_all_checks_and_strict_pin_preserve_synthetic(self):
        root,db,document=self.fixture();runner=self.runner(root,db)
        reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version=PIPELINE)
        complete=self.drain(root,db,runner,max_rows=7,max_bytes=4096)
        self.assertEqual(complete['report']['source_type'],'synthetic')
        check=next(c for c in complete['report']['checks'] if c['analyzer_id']=='recorded-steering-observation')
        self.assertEqual(check['analyzer_version'],steering.VERSION)
        self.assertNotIn('stream_evaluator_not_converted',check['unavailable'])
        self.assertEqual(check['provenance']['context']['stream_motion_build_policy'],'recorded_build_and_configuration_required_1')
        self.assertEqual(work._load(db,complete['stream_id'],1,json.loads(db.execute('SELECT state_json FROM analysis_stream_checkpoints WHERE stream_id=?',(complete['stream_id'],)).fetchone()[0])['tracking']['work'])['metric_index'],12)

    def qualified_fixture(self,**options):
        if options.get('dense'):
            temp=tempfile.TemporaryDirectory(prefix='steering dense invented ');self.addCleanup(temp.cleanup)
            root=Path(temp.name).resolve();db=open_catalog(root);self.addCleanup(db.close)
            source=root/'invented-dense.wpilog';source.write_bytes(dense_original())
            imported=Importer(root,db).import_file(source,source_type='SYNTHETIC');self.assertEqual(imported['state'],'succeeded')
            document=rebuild_from_imports(root,db)
        else:root,db,document=self.fixture()
        store=ReviewStore(db)
        plan=dict(id='stream-plan',robot_id='author-robot',start_utc_ns=str(UTC-1000000),end_utc_ns=None,
            reviewer='Invented author case',rationale='Generated protocol only',build_hash='a'*64,config_hash='b'*64,
            test_id='repeatable-case',surface='invented-flat',battery_id='invented-battery',wheel_radius_m=.05,
            swerve_configuration={'drive_error_limit_mps':.75,'threshold_revision':'invented'},
            steering_configuration={'recorded_jump_limit_rad':.5,'threshold_revision':'invented'},ideal_simulation_cycle_policy=True)
        if options.get('dense'):
            plan.update(stream_tracking_measurement_source='module_io_if_array_absent',steering_configuration=dict(
                settling_limit_seconds=.2,recorded_jump_limit_rad=.5,threshold_revision='invented-dense'))
        store.record_analysis_plan(plan)
        from robot_test_hub.swerve import MODULE_POSITIONS
        for i,position in enumerate(MODULE_POSITIONS):store.assign_component(dict(id='assignment-'+str(i),robot_id='author-robot',
            component_id='module-'+str(i),location=position,start_utc_ns=str(UTC-1000000),end_utc_ns=None,reviewer='Author',rationale='Invented assignment'))
        return root,db,document

    def test_original_positive_descriptive_and_restart_backup(self):
        from robot_test_hub.backup import Backup,restore,verify
        root,db,document=self.qualified_fixture();runner=self.runner(root,db)
        sid=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version=PIPELINE)[0]['stream_id']
        for _ in range(10000):
            with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=runner.step(sid,max_rows=3,max_bytes=4096)
            self.assertNotEqual(progress['state'],'failed',progress)
            if progress['phase']=='reducer_finalize':break
        else:self.fail('no finalize phase')
        with patch.object(stream_analysis.time,'monotonic',return_value=0):runner.step(sid,max_rows=1,max_bytes=4096)
        with tempfile.TemporaryDirectory() as external:
            destination=Path(external)/'backup';Backup(root,destination).create('new11');verify(destination/'new11')
            recovered=Path(external)/'restored';restore(destination/'new11',recovered)
            expected=self.drain(root,db,runner,max_rows=3,max_bytes=4096)
            otherdb=open_catalog(recovered);other=self.runner(recovered,otherdb)
            try:actual=self.drain(recovered,otherdb,other,max_rows=5,max_bytes=4096)
            finally:other.close();otherdb.close()
            self.assertEqual(expected['report'],actual['report'])
        check=next(c for c in expected['report']['checks'] if c['analyzer_id']=='recorded-steering-observation')
        self.assertEqual(check['outcome'],'evaluated_no_finding',check['unavailable'])
        self.assertEqual(check['coverage']['evaluated_modules'],4)
        self.assertTrue(all(m['physical_sample_count'] is None for m in check['coverage']['modules']))

    def test_dense_original_scalar_refs_retention_and_late_settling_finding(self):
        from robot_test_hub.report_api import detail as report_api
        root,db,document=self.qualified_fixture(dense=True);runner=self.runner(root,db)
        sid=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version=PIPELINE)[0]['stream_id']
        result=self.drain(root,db,runner,max_rows=500,max_bytes=65536)
        check=next(c for c in result['report']['checks'] if c['analyzer_id']=='recorded-steering-observation')
        self.assertEqual(check['outcome'],'finding',check['unavailable'])
        self.assertIn('motion_state_missing_while_enabled',check['unavailable'])
        self.assertTrue(any(m['value'] is not None for m in check['metrics']))
        self.assertIn('recorded_steering_settling_limit',{f['kind'] for f in check['findings']})
        raw=db.execute('SELECT state_json FROM analysis_stream_checkpoints WHERE stream_id=?',(sid,)).fetchone()[0]
        self.assertLessEqual(len(raw.encode()),1024*1024)
        state=json.loads(raw);self.assertLessEqual(state['steering']['reference_budget']['count'],128)
        for module in check['coverage']['modules']:
            self.assertGreater(module['event_count_total'],128)
            self.assertGreater(module['recorded_interval_count'],400)
            self.assertGreater(module['event_count_omitted'],0)
            self.assertEqual(module['evidence_trace_omitted'],module['evidence_trace_total']-len(module['evidence_trace']))
            self.assertTrue(module['evidence_trace_truncated'])
        projected=report_api(db,result['report']['run_id'],result['report']['report_id'])
        self.assertLess(len(json.dumps(projected)),4*1024*1024)


if __name__=='__main__':unittest.main()
