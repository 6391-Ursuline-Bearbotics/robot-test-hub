"""Actual typed WPILOG plan/pipeline/private-case steering integration."""
import copy
import json
from pathlib import Path
import struct
import tempfile
import shutil
import subprocess
import unittest
from robot_test_hub import reports, regression, regression_evidence, report_api
from robot_test_hub.analysis_plan import validate_plan
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.review import ReviewStore
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
from robot_test_hub.swerve import MODULE_POSITIONS
from test_wpilog import log_bytes, start, wire_record

UTC=1800000000000000000

def recording(*,finish=False,samples=31):
    events=[];entries={}
    def field(name,kind,payload,metadata='{}'):
        entry=len(entries)+2;entries[name]=entry
        events.extend((start(entry,name,kind,metadata),wire_record(entry,payload)))
    field('/.schema/struct:Rotation2d','structschema',b'double value')
    field('/.schema/struct:SwerveModuleVelocity','structschema',b'double velocity;Rotation2d angle')
    for name,value in [('/RealOutputs/TestHub/RobotId','steering-robot'),('/RealOutputs/TestHub/BootId','steering-boot'),
        ('/RealOutputs/TestHub/RuntimeMode','REAL'),('/RealOutputs/TestHub/Mode','TELEOPERATED'),
        ('/RealOutputs/TestHub/ConfigurationSHA256','b'*64),('/RealMetadata/SourceSHA256','a'*64)]:field(name,'string',value.encode())
    field('/RealOutputs/TestHub/StateKnown','boolean',b'\1')
    field('/DriverStation/Enabled','boolean',b'\0')
    field('/RealOutputs/TestHub/Enabled','boolean',b'\0')
    field('/SystemStats/EpochTimeValid','boolean',b'\1')
    field('/SystemStats/EpochTime','double',struct.pack('<d',UTC/1000),'{"unit":"microseconds"}')
    for i in range(4):
        for connection in ('DriveConnected','TurnConnected','TurnEncoderConnected'):field(f'/Drive/Module{i}/'+connection,'boolean',b'\1')
    command='/RealOutputs/SwerveStates/SetpointsOptimized';measured='/RealOutputs/SwerveStates/Measured'
    field(command,'struct:SwerveModuleVelocity[]',struct.pack('<8d',*([2.,0.]*4)))
    field(measured,'struct:SwerveModuleVelocity[]',struct.pack('<8d',*([2.,0.]*4)))
    for i in range(1,samples+1):
        ns=1000000000+i*100000000
        events.append(wire_record(1,struct.pack('<q',ns),ns//1000))
        events.append(wire_record(entries['/DriverStation/Enabled'],bytes([i<samples]),ns//1000))
        events.append(wire_record(entries['/RealOutputs/TestHub/Enabled'],bytes([i<samples]),ns//1000))
        events.append(wire_record(entries['/SystemStats/EpochTime'],struct.pack('<d',(UTC+ns-1000000000)/1000),ns//1000))
        events.append(wire_record(entries[command],struct.pack('<8d',*([2.,.6 if i>=10 else 0.]*4)),ns//1000))
        if not finish or i<=20:events.append(wire_record(entries[measured],struct.pack('<8d',*([2.,.6 if i>=16 else 0.]*4)),ns//1000))
        if finish and i==20:events.append(wire_record(0,b'\1'+struct.pack('<I',entries[measured]),ns//1000))
    return log_bytes(*events)

class SteeringIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.base=Path(temp.name)
        self.root=self.base/'archive';self.root.mkdir();self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.store=ReviewStore(self.db)
        self.plan=dict(id='steering-plan',robot_id='steering-robot',start_utc_ns=str(UTC-1000000),end_utc_ns=None,
            reviewer='Invented integration',rationale='Exact recorded steering test',build_hash='a'*64,config_hash='b'*64,
            test_id='steering-step',surface='invented-flat',battery_id='invented-battery',wheel_radius_m=.05,
            swerve_configuration={},steering_configuration={'settling_limit_seconds':.15,'threshold_revision':'invented-1'})
    def execute(self,*,plan=True,omit=None,finish=False,samples=31):
        source=self.base/'source.wpilog';source.write_bytes(recording(finish=finish,samples=samples))
        self.assertEqual(Importer(self.root,self.db).import_file(source,source_type='MANUAL_LOCAL')['state'],'succeeded')
        if plan:self.store.record_analysis_plan(self.plan)
        for i,position in enumerate(MODULE_POSITIONS):
            if position==omit:continue
            self.store.assign_component(dict(id='assignment-'+str(i),robot_id='steering-robot',component_id='module-'+str(i),location=position,
                start_utc_ns=str(UTC-1000000),end_utc_ns=None,reviewer='Invented',rationale='Explicit assignment'))
        Pipeline(self.root,self.db).tick()
        return json.loads(self.db.execute('SELECT result_json FROM analysis_reports ORDER BY created_utc_ns DESC').fetchone()[0])
    def steering(self,report):return next(c for c in report['checks'] if c['analyzer_id']=='recorded-steering-observation')
    def test_real_typed_step_pipeline_review_export_proof_and_execution(self):
        report=self.execute();check=self.steering(report)
        self.assertEqual(len(report['checks']),5);self.assertEqual(report['overall_health'],'not_assessed')
        self.assertEqual(check['outcome'],'finding',check)
        self.assertEqual(check['provenance']['source_type'],'real')
        self.assertFalse(check['coverage']['physical_acquisition_freshness_qualified'])
        self.assertFalse(check['coverage']['ideal_simulation_cycle_policy_applied'])
        self.assertTrue(check['metrics']);self.assertEqual(check['configuration'],validate_plan(self.plan)['steering_configuration'])
        review=self.store.review_finding(dict(id='steering-incident',job_id=check['job_id'],finding_index=0,disposition='unresolved',reviewer='Invented',rationale='Recorded only'))
        folder=self.base/'case';kind=check['findings'][0]['kind']
        regression.export_case(self.root,review_id=review['id'],review_revision=1,expectations={'schema_version':1,'expected_outcome':'finding','checks':[{'kind':'finding_count','finding_kind':kind,'min':1}]},output=folder)
        self.assertTrue(regression.verify_case(folder)['verified']);result=regression.run_case(folder)
        self.assertEqual(result['state'],'passed',result);self.assertIn('steering',result['evaluator_module_sha256']);self.assertFalse(result['hardware_qualified'])
        captured=regression_evidence.capture(self.root,review_id=review['id'],review_revision=1)
        self.assertEqual(captured['job']['configuration'],check['configuration'])
    def test_missing_plan_assignment_or_endpoint_stays_unavailable(self):
        for kwargs in ({'plan':False},{'omit':MODULE_POSITIONS[-1]},{'finish':True}):
            with self.subTest(kwargs=kwargs):
                fixture=SteeringIntegrationTests();fixture.setUp()
                try:
                    check=fixture.steering(fixture.execute(**kwargs));self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])
                finally:fixture.doCleanups()
    def test_policy_revision_regenerates_without_mutating_saved_report(self):
        report=self.execute();old=self.steering(report)
        saved=self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report['report_id'],)).fetchone()[0]
        self.store.record_analysis_plan({**self.plan,'revision':2,'expected_revision':1,
            'steering_configuration':{'settling_limit_seconds':1.,'threshold_revision':'invented-2'}})
        Pipeline(self.root,self.db).tick()
        newest=json.loads(self.db.execute('SELECT result_json FROM analysis_reports ORDER BY created_utc_ns DESC').fetchone()[0])
        current=self.steering(newest);self.assertNotEqual(current['job_id'],old['job_id'])
        self.assertEqual(current['configuration']['threshold_revision'],'invented-2')
        self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report['report_id'],)).fetchone()[0],saved)

    def test_optional_plan_absence_and_normalized_policy_roundtrip(self):
        old={k:v for k,v in self.plan.items() if k!='steering_configuration'}
        normalized=validate_plan(old);self.assertNotIn('steering_configuration',normalized)
        self.assertEqual(validate_plan(normalized),normalized)
        configured=validate_plan(self.plan);self.assertEqual(validate_plan(configured),configured)
        for value in (None,False,{'unknown':1},{'max_gap_ns':True},{'minimum_coverage':float('inf')},{'settling_limit_seconds':.1}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_plan({**old,'steering_configuration':value})
    def test_explicit_legacy_five_and_six_factories_preserve_reports(self):
        self.execute();document=rebuild_from_imports(self.root,self.db)
        saved={}
        for version,count in [('automatic-reports-5',3),('automatic-reports-6',4)]:
            historical=reports.generate(self.root,self.db,document,pipeline_version=version)
            self.assertTrue(all(len(r['checks'])==count for r in historical))
            self.assertTrue(all(c['analyzer_id']!='recorded-steering-observation' for r in historical for c in r['checks']))
            for r in historical:saved[r['report_id']]=self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(r['report_id'],)).fetchone()[0]
        current=reports.generate(self.root,self.db,document);self.assertTrue(all(len(r['checks'])==5 for r in current))
        for key,value in saved.items():self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(key,)).fetchone()[0],value)
    @unittest.skipUnless(shutil.which('node'),'Node is optional for isolated browser-logic checks')
    def test_ui_policy_roundtrip_and_strict_json_errors(self):
        script=r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const elements=new Map();global.el=id=>{if(!elements.has(id))elements.set(id,{_value:'',get value(){return this._value},set value(v){this._value=String(v)},checked:false});return elements.get(id)};
global.selectedPlan=null;global.planConfiguration={};global.planNeedsReload=false;global.catalog={baseline:[]};global.baselineContext=()=>{};global.reviewControls=()=>{};
function part(start,end){return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)))}
vm.runInThisContext(part('function utcNS(',"el('plan-load').onclick").replace(part('function baselineContext(', 'function fillPlan('),''));
const plan={id:'case',revision:1,robot_id:'robot',start_utc_ns:'1800000000000000000',end_utc_ns:null,build_hash:'a'.repeat(64),config_hash:'b'.repeat(64),test_id:'test',surface:'flat',battery_id:'battery',wheel_radius_m:.05,swerve_configuration:{minimum_coverage:.8,minimum_eligible_seconds:1},reviewer:'Tester',rationale:'Invented'};
fillPlan(plan);let payload=planPayload();assert.equal(Object.hasOwn(payload,'steering_configuration'),false);
const cfg={max_gap_ns:250000000,settling_limit_seconds:.2,threshold_revision:'test',target_stability_rad:.02};
fillPlan({...plan,steering_configuration:cfg});payload=planPayload();assert.deepEqual(payload.steering_configuration,cfg);assert.equal(payload.revision,2);assert.equal(payload.expected_revision,1);
for(const text of ['{"a":1,"a":2}','{"a":1,"\\u0061":2}','{"x":{"a":1,"a":2}}','{"x":1e400}','{"x":NaN}','[]','null','false','{broken'])assert.throws(()=>steeringPolicy(text));
assert.equal(steeringPolicy('  '),undefined);assert.deepEqual(steeringPolicy('{"max_gap_ns":250000000}'),{max_gap_ns:250000000});
console.log('Steering policy UI logic passed');
"""
        result=subprocess.run([shutil.which('node'),'-e',script,str(Path(__file__).resolve().parents[1]/'robot_test_hub/static/review.html')],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_fixed_five_and_six_cases_prove_originals_before_explicit_current_evaluation(self):
        source=self.base/'incident.wpilog';source.write_bytes((Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog').read_bytes())
        self.assertEqual(Importer(self.root,self.db).import_file(source,source_type='SYNTHETIC')['state'],'succeeded')
        document=rebuild_from_imports(self.root,self.db)
        for version in ('automatic-reports-5','automatic-reports-6'):
            with self.subTest(version=version):
                old=reports.generate(self.root,self.db,document,pipeline_version=version)
                check=next(c for r in old for c in r['checks'] if c['analyzer_id']=='recording-quality' and c['findings'])
                review=self.store.review_finding(dict(id='incident-'+version,job_id=check['job_id'],finding_index=0,disposition='unresolved',reviewer='Invented',rationale='Pinned old input'))
                folder=self.base/version
                regression.export_case(self.root,review_id=review['id'],review_revision=1,expectations={'schema_version':1,'expected_outcome':check['outcome'],'checks':[{'kind':'finding_count','finding_kind':check['findings'][0]['kind'],'min':1}]},output=folder)
                before=(folder/'evidence.json').read_bytes();self.assertTrue(regression.verify_case(folder)['verified'])
                self.assertEqual(regression.run_case(folder)['code'],'regression_analyzer_version_changed')
                result=regression.run_case(folder,allow_analyzer_version_change=True);self.assertEqual(result['state'],'passed');self.assertEqual(result['current_analyzer_version'],'automatic-reports-7')
                self.assertEqual((folder/'evidence.json').read_bytes(),before);self.assertEqual(json.loads(before)['context']['pipeline_version'],version)

    def test_bounded_projection_keeps_observed_total_and_only_pages_retained_trace(self):
        report=self.execute(samples=180);check=self.steering(report);module=check['coverage']['modules'][0]
        trace=module['evidence_trace'];self.assertEqual(len(trace),128);self.assertGreater(module['evidence_trace_total'],len(trace));self.assertTrue(module['evidence_trace_truncated'])
        view=report_api.detail(self.db,report['run_id'],report['report_id'])['report']
        shown=self.steering(view)['coverage']['modules'][0];self.assertEqual(shown['evidence_trace_total'],module['evidence_trace_total']);self.assertTrue(shown['evidence_trace_truncated']);self.assertEqual(shown['evidence_trace_available'],len(trace))
        page=report_api.traces(self.db,report['run_id'],report['report_id'],{'analyzer_id':'recorded-steering-observation','module_id':module['module_id']})
        self.assertEqual(page['total'],len(trace));self.assertIsNone(page['next_offset'])

if __name__=='__main__':unittest.main()
