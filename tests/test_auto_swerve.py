"""Author regressions for explicit policy and complete automatic analysis inputs."""
import copy
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.analysis_plan import validate_plan
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.reports import generate,public_check,public_for_run
from robot_test_hub.review import ReviewStore
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import open_catalog
from robot_test_hub.swerve import MODULE_POSITIONS
from test_wpilog import log_bytes,start,wire_record

UTC=1800000000000000000


def protocol_recording(*,build='a'*64,finish_measured=False):
    """Invented exact SI structs; independently fixed .5 m/s wheel error."""
    events=[];fields={}
    def field(name,kind,value,metadata='{"source":"AdvantageKit"}'):
        entry=len(fields)+2;fields[name]=entry
        events.extend([start(entry,name,kind,metadata),wire_record(entry,value)])
    field('/.schema/struct:Rotation2d','structschema',b'double value')
    field('/.schema/struct:SwerveModuleVelocity','structschema',b'double velocity;Rotation2d angle')
    for name,value in [('/RealOutputs/TestHub/RobotId','author-robot'),('/RealOutputs/TestHub/BootId','author-boot'),
        ('/RealOutputs/TestHub/RuntimeMode','SIM'),('/RealOutputs/TestHub/Mode','TELEOPERATED'),
        ('/RealOutputs/TestHub/ConfigurationSHA256','b'*64),('/RealMetadata/SourceSHA256',build)]:
        field(name,'string',value.encode())
    field('/DriverStation/Enabled','boolean',b'\0')
    field('/SystemStats/EpochTimeValid','boolean',b'\1')
    field('/SystemStats/EpochTime','double',struct.pack('<d',UTC/1000),'{"unit":"microseconds"}')
    for i in range(4):
        for connection in ('DriveConnected','TurnConnected','TurnEncoderConnected'):
            field(f'/Drive/Module{i}/'+connection,'boolean',b'\1')
    field('/RealOutputs/SwerveStates/SetpointsOptimized','struct:SwerveModuleVelocity[]',struct.pack('<8d',*([2.,0.]*4)))
    field('/RealOutputs/SwerveStates/Measured','struct:SwerveModuleVelocity[]',struct.pack('<8d',*([1.5,0.]*4)))
    for i in range(1,22):
        ns=1000000000+i*100000000
        events.append(wire_record(1,struct.pack('<q',ns),ns//1000))
        events.append(wire_record(fields['/DriverStation/Enabled'],bytes([i<21]),ns//1000))
        events.append(wire_record(fields['/SystemStats/EpochTime'],struct.pack('<d',(UTC+ns-1000000000)/1000),ns//1000))
        if finish_measured and i==16:
            events.append(wire_record(0,b'\1'+struct.pack('<I',fields['/RealOutputs/SwerveStates/Measured']),ns//1000))
    return log_bytes(*events)


class AutomaticSwerveTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.store=ReviewStore(self.db)
        self.plan=dict(id='author-plan',robot_id='author-robot',start_utc_ns=str(UTC-1000000),end_utc_ns=None,
            reviewer='Invented author case',rationale='Explicit generated protocol policy',build_hash='a'*64,
            config_hash='b'*64,test_id='repeatable-case',surface='invented-flat',battery_id='invented-battery',
            wheel_radius_m=.05,swerve_configuration={'drive_error_limit_mps':.75,'threshold_revision':'author-limits'},
            ideal_simulation_cycle_policy=True)
        for i,position in enumerate(MODULE_POSITIONS):
            self.store.assign_component(dict(id='assignment-'+str(i),robot_id='author-robot',component_id='module-'+str(i),
                location=position,start_utc_ns=str(UTC-1000000),end_utc_ns=None,reviewer='Author',rationale='Invented physical assignment'))

    def execute(self,**options):
        source=self.root/'generated.wpilog';source.write_bytes(protocol_recording(**options))
        job=Importer(self.root,self.db).import_file(source,source_type='MANUAL_LOCAL')
        self.assertEqual(job['state'],'succeeded',job)
        self.store.record_analysis_plan(self.plan);Pipeline(self.root,self.db).tick()
        return json.loads(self.db.execute('SELECT result_json FROM analysis_reports ORDER BY created_utc_ns DESC').fetchone()[0])

    @staticmethod
    def check(report):return next(c for c in report['checks'] if c['analyzer_id']=='swerve-tracking')

    def test_known_build_and_si_metrics_are_pinned(self):
        check=self.check(self.execute());self.assertEqual(check['outcome'],'evaluated_no_finding')
        values=[m['value'] for m in check['metrics'] if m['name']=='drive_rmse_mps']
        self.assertEqual(values,[.5]*4)
        context=check['provenance']['context']
        self.assertEqual(context['recorded_build_sha256'],['a'*64])
        self.assertEqual(context['build_context_basis'],'recorded_alias_and_human_plan')
        self.assertEqual(context['mapping_revision'],'alpha7-aliases-2')
        self.assertNotEqual(context['mapping_revision'],context['run_catalog_revision'])

    def test_recorded_build_mismatch_cannot_be_human_overridden(self):
        check=self.check(self.execute(build='c'*64))
        self.assertEqual(check['outcome'],'insufficient_data')
        self.assertIn('recorded_build_hash_mismatch_or_changed',check['unavailable'])

    def test_finish_control_removes_held_tail_instead_of_healthy_prefix(self):
        check=self.check(self.execute(finish_measured=True))
        self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])
        self.assertIn('whole_run_swerve_state_coverage_required',check['unavailable'])

    def test_later_regressing_source_cycle_is_not_hidden_by_postrun_boundary(self):
        self.execute();original=Importer.iter_dataset
        def damaged(importer,job):
            last=None
            for row in original(importer,job):
                if row['kind']=='cycle':last=row
                yield row
            yield {**last,'timestamp_ns':'4000000000'}
            yield {**last,'timestamp_ns':'3200000000'}
        with patch.object(Importer,'iter_dataset',damaged):
            report=generate(self.root,self.db,RunCatalog(self.db).current())[0]
        check=self.check(report);self.assertEqual(check['outcome'],'insufficient_data')
        self.assertIn('source_cycle_order_unqualified',check['unavailable'])

    def test_plan_strict_revision_overlap_and_finite_limits(self):
        first=self.store.record_analysis_plan(self.plan)
        self.assertEqual(self.store.record_analysis_plan(self.plan),first)
        with self.assertRaises(ValueError):self.store.record_analysis_plan({**self.plan,'id':'overlap'})
        for changed in ({'wheel_radius_m':10**10000},{'unexpected':True},{'start_utc_ns':str(UTC)+'.0'},
                        {'ideal_simulation_cycle_policy':1},{'swerve_configuration':{'max_gap_ns':10**10000}}):
            with self.subTest(fields=list(changed)),self.assertRaises(ValueError):validate_plan({**self.plan,**changed})
        second=self.store.record_analysis_plan({**self.plan,'revision':2,'expected_revision':1,'rationale':'Explicit corrected context'})
        self.assertEqual(second['revision'],2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM review_records WHERE kind='analysis_plan'").fetchone()[0],2)

    def test_display_bounds_preserve_full_immutable_evidence_and_source_references(self):
        report=self.execute();check=self.check(report)
        module=check['coverage']['modules'][0];original=copy.deepcopy(check)
        module['evidence_trace']=module['evidence_trace']*100
        view=public_check(check)
        self.assertEqual(len(view['coverage']['modules'][0]['evidence_trace']),500)
        self.assertTrue(view['coverage']['modules'][0]['evidence_trace_truncated'])
        self.assertEqual(view['coverage']['modules'][0]['evidence_trace_total'],len(module['evidence_trace']))
        self.assertEqual(view['coverage']['modules'][0]['evidence_trace'][0]['source_reference'],module['evidence_trace'][0]['source_reference'])
        review=self.store.snapshot();self.assertTrue(all(not m['evidence_trace'] for j in review['analysis_jobs'] for m in j['coverage'].get('modules',[])))
        self.assertEqual(self.check(json.loads(self.db.execute('SELECT result_json FROM analysis_reports').fetchone()[0])),original)
        for i in range(7):
            value={**report,'report_id':'projection-'+str(i)}
            self.db.execute('INSERT INTO analysis_reports VALUES (?,?,?,?)',(value['report_id'],'{}',json.dumps(value),str(i)))
        views,metadata=public_for_run(self.db,report['run_id'])
        self.assertEqual(len(views),5);self.assertEqual(metadata['total'],8);self.assertTrue(metadata['truncated'])


if __name__=='__main__':unittest.main()
