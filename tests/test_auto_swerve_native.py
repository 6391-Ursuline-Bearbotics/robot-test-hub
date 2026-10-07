"""Opt-in actual pinned AdvantageKit writer -> importer -> automatic report checks.

All values are invented; native library execution never starts a robot or camera.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.reports import list_for_run
from robot_test_hub.review import ReviewStore
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import open_catalog
from robot_test_hub.swerve import MODULE_POSITIONS
from robot_test_hub.wpilog import PROFILE

EPOCH = 1791003600000000000


def native_plan():
    return dict(id='native-swerve-plan', revision=1, robot_id='native-swerve-robot',
        start_utc_ns=str(EPOCH), end_utc_ns=None, reviewer='Synthetic qualification',
        rationale='Invented observations only; thresholds are not physical qualifications',
        build_hash='a'*64, config_hash='b'*64, test_id='native-d1',
        surface='invented-flat', battery_id='invented-battery', wheel_radius_m=.0508,
        swerve_configuration={'drive_error_limit_mps':.5,
                              'threshold_revision':'native-test-limits-1'},
        approved_baseline_id=None, ideal_simulation_cycle_policy=True)


def native_assignments(store):
    for index, position in enumerate(MODULE_POSITIONS):
        store.assign_component(dict(id='native-assignment-'+str(index),
            component_id='native-component-'+str(index), robot_id='native-swerve-robot',
            location=position, start_utc_ns=str(EPOCH), end_utc_ns=None,
            reviewer='Synthetic qualification', rationale='Invented physical identities'))


def swerve_check(db, run):
    reports=list_for_run(db, run['run_id'])
    assert reports, 'Automatic report missing'
    return next(check for check in reports[0]['checks']
                if check['analyzer_id']=='swerve-tracking')


@unittest.skipUnless(os.environ.get('ROBOT_HUB_STATUS_INTEGRATION')=='1',
                     'Explicit pinned native qualification opt-in required')
class AutomaticSwerveNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools.fixtures.run import prepare
        cls.generated=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.generated.cleanup)
        cls.fixture_root=Path(cls.generated.name)
        cls.command,cls.environment=prepare(Path(r'C:\Users\Public\wpilib\2027_alpha7'),
                                           Path.home()/'.gradle/caches')
        subprocess.run(cls.command+['GenerateSwerveSessions',str(cls.fixture_root)],
            env=cls.environment, check=True, capture_output=True, text=True, timeout=30)

    def setUp(self):
        self.archive=tempfile.TemporaryDirectory()
        self.addCleanup(self.archive.cleanup)
        self.root=Path(self.archive.name)
        self.db=open_catalog(self.root)
        self.addCleanup(self.db.close)
        self.importer=Importer(self.root,self.db)
        self.pipeline=Pipeline(self.root,self.db)
        self.store=ReviewStore(self.db)

    def import_case(self,name,source='MANUAL_LOCAL'):
        file=self.fixture_root/(name+'.wpilog')
        job=self.importer.import_file(file,profile=PROFILE,source_type=source)
        self.assertEqual(job['state'],'succeeded')
        self.assertEqual(job['artifact_sha256'],hashlib.sha256(file.read_bytes()).hexdigest())
        self.pipeline.tick()
        return job

    def runs(self):
        return sorted(RunCatalog(self.db).current()['runs'],
                      key=lambda run:int(run['utc_intervals'][0]['utc_start_ns']))

    def test_actual_writer_manual_import_baseline_new_boot_and_restart(self):
        official=subprocess.run(self.command+['OfficialReader',
            str(self.fixture_root/'swerve-sim.wpilog')], env=self.environment,
            check=True,capture_output=True,text=True,timeout=30)
        document=json.loads(official.stdout)
        self.assertEqual(len(document['advantagekit_replay_cycles']),88)
        arrays=[row['value'] for row in document['records']
                if row.get('name')=='/RealOutputs/SwerveStates/Measured'
                and row.get('type')=='struct:SwerveModuleVelocity[]' and 'value' in row]
        self.assertEqual([round(array[0]['velocity_mps'],2) for array in arrays],
                         [0,1,0,.98,0,.96,0,.65,0])
        first=self.import_case('swerve-sim')
        native_assignments(self.store)
        self.store.record_analysis_plan(native_plan())
        # Same pipeline instance must react without another import or restart.
        self.assertEqual(self.pipeline.tick()['state'],'indexed')
        runs=self.runs()
        self.assertEqual(len(runs),4)
        self.assertTrue(all(run['completeness']=='complete' for run in runs))
        original=[swerve_check(self.db,run) for run in runs]
        for run, check, error in zip(runs,original,[0,.02,.04,.35]):
            self.assertEqual(check['outcome'],'evaluated_no_finding')
            self.assertEqual(check['provenance']['source_type'],'simulation')
            self.assertEqual(check['provenance']['source_hashes'],[first['artifact_sha256']])
            self.assertEqual(check['coverage']['evaluated_modules'],4)
            metric=next(item for item in check['metrics'] if item['name']=='drive_rmse_mps'
                        and item['module_position']=='front-left')
            self.assertAlmostEqual(metric['value'],error,places=10)
            self.assertGreaterEqual(metric['eligible_seconds'],1.8)
            self.assertTrue(all(module['evidence_trace'] for module in check['coverage']['modules']))
        baseline=self.store.approve_baseline(dict(id='native-baseline', label='Invented D1 cohort',
            job_ids=[check['job_id'] for check in original[:3]], exclusions={},
            reviewer='Synthetic qualification', rationale='Explicit invented known-good selection'))
        cohort_bytes=json.dumps(baseline,sort_keys=True)
        self.store.record_analysis_plan({**native_plan(), 'revision':2,'expected_revision':1,
                                        'approved_baseline_id':'native-baseline'})
        self.assertEqual(self.pipeline.tick()['state'],'indexed')
        observed=swerve_check(self.db,runs[3])
        self.assertEqual(observed['outcome'],'finding')
        finding=next(item for item in observed['findings']
                     if item['kind']=='tracking_regression_against_approved_cohort')
        self.assertEqual(finding['module_position'],'front-left')
        self.assertEqual(finding['baseline_version'],'native-baseline')
        self.assertEqual(finding['physical_cause'],'unconfirmed')
        comparison=finding['metric']
        self.assertAlmostEqual(comparison['baseline_median'],.02)
        self.assertAlmostEqual(comparison['baseline_mad'],.02)
        self.assertAlmostEqual(comparison['engineering_upper_limit'],.14)
        later=self.import_case('swerve-sim-later')
        latest=self.runs()[-1]
        later_check=swerve_check(self.db,latest)
        self.assertEqual(later_check['outcome'],'finding')
        self.assertEqual(later_check['provenance']['source_hashes'],[later['artifact_sha256']])
        self.assertEqual(later_check['provenance']['context']['mapping_revision'],
                         observed['provenance']['context']['mapping_revision'])
        self.assertEqual(json.dumps(self.store.records('baseline')[0],sort_keys=True),cohort_bytes)
        prior={row['report_id']:row['result_json'] for row in self.db.execute(
            'SELECT report_id,result_json FROM analysis_reports')}
        Pipeline(self.root,self.db).tick()
        after={row['report_id']:row['result_json'] for row in self.db.execute(
            'SELECT report_id,result_json FROM analysis_reports')}
        self.assertEqual(prior,after)
        persisted=self.importer.get_job(first['id'])
        self.assertEqual(persisted['artifact_sha256'],first['artifact_sha256'])
        self.store.record_maintenance(dict(id='native-repair-between-sessions',
            robot_id='native-swerve-robot',component_id='native-component-0',
            kind='physical_repair',effective_utc_ns=str(EPOCH+20_000_000_000),
            reviewer='Synthetic qualification',rationale='Invented repair-history boundary only'))
        self.assertEqual(self.pipeline.tick()['state'],'indexed')
        after_repair=swerve_check(self.db,latest)
        self.assertEqual(after_repair['outcome'],'insufficient_data')
        self.assertIn('baseline_maintenance_configuration_boundary',after_repair['unavailable'])
        self.assertFalse(after_repair['coverage']['baseline_comparisons'])
        self.assertEqual(json.dumps(self.store.records('baseline')[0],sort_keys=True),cohort_bytes)

    def test_long_actual_writer_history_and_late_fault_trace_pages(self):
        from robot_test_hub.run_api import get
        official=subprocess.run(self.command+['OfficialReader',
            str(self.fixture_root/'swerve-sim-long.wpilog')],env=self.environment,
            check=True,capture_output=True,text=True,timeout=30)
        document=json.loads(official.stdout)
        self.assertEqual(len(document['advantagekit_replay_cycles']),651)
        measured=[row['value'][0]['velocity_mps'] for row in document['records']
                  if row.get('name')=='/RealOutputs/SwerveStates/Measured'
                  and row.get('type')=='struct:SwerveModuleVelocity[]' and 'value' in row]
        self.assertEqual([round(value,2) for value in measured],[0,.98,.2,0])
        job=self.import_case('swerve-sim-long')
        native_assignments(self.store)
        self.store.record_analysis_plan(native_plan())
        self.pipeline.tick()
        for revision in range(2,8):
            self.store.record_analysis_plan({**native_plan(),'revision':revision,
                'expected_revision':revision-1,'rationale':'Invented history revision '+str(revision)})
            self.assertEqual(self.pipeline.tick()['state'],'indexed')
        run=self.runs()[0];run_id=run['run_id']
        report= list_for_run(self.db,run_id)[0]
        check=next(item for item in report['checks'] if item['analyzer_id']=='swerve-tracking')
        self.assertEqual(check['outcome'],'finding')
        metric=next(item for item in check['metrics'] if item['name']=='drive_rmse_mps'
                    and item['module_position']=='front-left')
        self.assertAlmostEqual(metric['value'],math.sqrt((549*.02**2+99*.8**2)/648),places=10)
        prefix='/api/v1/runs/'+run_id+'/reports'
        first=get(self.root,prefix,{'limit':'3'},None)
        self.assertEqual(first['total'],8)
        ids=[item['report_id'] for item in first['items']]
        snapshot=first['snapshot_id'];cursor=first['next_cursor']
        original_bytes=self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',
                                      (report['report_id'],)).fetchone()[0]
        self.store.record_analysis_plan({**native_plan(),'revision':8,'expected_revision':7,
                                         'rationale':'New result while older history is being paged'})
        self.pipeline.tick()
        while cursor is not None:
            page=get(self.root,prefix,{'limit':'3','cursor':cursor},None)
            self.assertEqual(page['snapshot_id'],snapshot)
            self.assertEqual(page['total'],8)
            ids.extend(item['report_id'] for item in page['items'])
            cursor=page['next_cursor']
        self.assertEqual(len(ids),8);self.assertEqual(len(set(ids)),8)
        self.assertEqual(get(self.root,prefix,{'limit':'3'},None)['total'],9)
        pinned=prefix+'/'+report['report_id']
        detail=get(self.root,pinned,{},None)
        expected_hash=hashlib.sha256(original_bytes.encode()).hexdigest()
        self.assertEqual(detail['result_sha256'],expected_hash)
        query={'analyzer_id':'swerve-tracking','module_id':'native-component-0','limit':'500'}
        start=get(self.root,pinned+'/traces',{**query,'offset':'0'},None)
        tail=get(self.root,pinned+'/traces',{**query,'offset':'500'},None)
        self.assertEqual((start['total'],start['returned'],start['next_offset']),(648,500,500))
        self.assertEqual((tail['total'],tail['returned'],tail['next_offset']),(648,148,None))
        self.assertTrue(all(abs(item['drive_error_mps']+.02)<1e-10 for item in start['evidence_trace']))
        errors=[item['drive_error_mps'] for item in tail['evidence_trace']]
        self.assertTrue(all(abs(value+.02)<1e-10 for value in errors[:49]))
        self.assertTrue(all(abs(value+.8)<1e-10 for value in errors[49:]))
        self.assertEqual(start['result_sha256'],expected_hash)
        self.assertEqual(tail['result_sha256'],expected_hash)
        for item in tail['evidence_trace']:
            self.assertEqual(item['source_reference']['source_hash'],job['artifact_sha256'])
        Pipeline(self.root,self.db).tick()
        self.assertEqual(get(self.root,pinned+'/traces',{**query,'offset':'500'},None),tail)
        self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',
                                        (report['report_id'],)).fetchone()[0],original_bytes)
        self.assertEqual(hashlib.sha256((self.fixture_root/'swerve-sim-long.wpilog').read_bytes()).hexdigest(),
                         job['artifact_sha256'])

    def test_actual_real_mode_and_synthetic_source_cannot_gain_ideal_freshness(self):
        native_assignments(self.store)
        self.store.record_analysis_plan(native_plan())
        self.import_case('swerve-real-mode')
        check=swerve_check(self.db,self.runs()[0])
        self.assertEqual(check['outcome'],'insufficient_data')
        self.assertFalse(check['findings'])
        self.assertFalse(any(metric['value'] is not None for metric in check['metrics']))
        # Even a logged SIM label on a SYNTHETIC import cannot opt into freshness.
        self.import_case('swerve-sim',source='SYNTHETIC')
        for run in self.runs()[:4]:
            check=swerve_check(self.db,run)
            self.assertEqual(check['outcome'],'insufficient_data')
            self.assertFalse(check['findings'])
            self.assertFalse(any(metric['value'] is not None for metric in check['metrics']))


if __name__=='__main__':
    unittest.main()
