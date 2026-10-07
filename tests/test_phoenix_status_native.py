"""Locked native AK writer -> portable importer -> automatic SDK status reports.

Invented observations only. No CAN objects, robot startup or camera access.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.reports import list_for_run
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE


@unittest.skipUnless(os.environ.get('ROBOT_HUB_STATUS_INTEGRATION') == '1',
                     'Explicit pinned native qualification opt-in required')
class PhoenixStatusNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools.fixtures.run import prepare
        cls.generated = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.generated.cleanup)
        cls.fixture_root = Path(cls.generated.name)
        cls.command, cls.environment = prepare(Path(r'C:\Users\Public\wpilib\2027_alpha7'),
                                               Path.home() / '.gradle/caches')
        subprocess.run(cls.command + ['GeneratePhoenixObservations', str(cls.fixture_root)],
                       env=cls.environment, check=True, capture_output=True, text=True, timeout=30)

    def setUp(self):
        self.archive = tempfile.TemporaryDirectory()
        self.addCleanup(self.archive.cleanup)
        self.root = Path(self.archive.name)
        self.db = open_catalog(self.root)
        self.addCleanup(self.db.close)

    def import_case(self, name, source='SYNTHETIC'):
        path = self.fixture_root / (name + '.wpilog')
        job = Importer(self.root, self.db).import_file(path, profile=PROFILE, source_type=source)
        self.assertEqual(job['state'], 'succeeded')
        self.assertEqual(job['artifact_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        Pipeline(self.root, self.db).tick()
        run = RunCatalog(self.db).current()['runs'][0]
        self.assertEqual(run['completeness'], 'complete')
        report = list_for_run(self.db, run['run_id'])[0]
        check = next(c for c in report['checks'] if c['analyzer_id'] == 'phoenix-status-observation')
        return job, run, report, check

    def official(self, name):
        return json.loads(subprocess.run(self.command + ['OfficialReader',
            str(self.fixture_root / (name + '.wpilog'))], env=self.environment,
            check=True, capture_output=True, text=True, timeout=30).stdout)

    def test_native_warning_precedes_connection_debounce_and_preserves_held_evidence(self):
        official = self.official('phoenix-warning')
        records = official['records']
        prefix = '/Drive/Module0/'
        status = [(i, r) for i, r in enumerate(records)
                  if r.get('name') == prefix + 'PhoenixDriveVelocityStatusOk' and 'value' in r]
        self.assertEqual([r['value'] for _, r in status], [True, False, True])
        self.assertEqual([r['timestamp_ns'] for _, r in status], [1000000000, 1200000000, 1500000000])
        connected = [r['value'] for r in records if r.get('name') == prefix + 'DriveConnected' and 'value' in r]
        self.assertEqual(connected, [True])
        self.assertEqual(len(official['advantagekit_replay_cycles']), 8)
        job, run, report, check = self.import_case('phoenix-warning')
        self.assertEqual(report['source_type'], 'synthetic')
        self.assertEqual(report['overall_health'], 'not_assessed')
        self.assertEqual(check['outcome'], 'finding')
        self.assertEqual(len(check['findings']), 2)
        signal = next(f for f in check['findings'] if f['kind'] == 'recorded_phoenix_signal_status_not_ok')
        self.assertEqual(signal['module_index'], 0)
        self.assertEqual(signal['observed_false_status_records'], 1)
        self.assertFalse(signal['physical_health_qualified'])
        reference = signal['source_references'][0]
        raw_index, raw = status[1]
        self.assertEqual(reference['status_ok']['record_index'], raw_index)
        self.assertEqual(reference['status_ok']['source_sha256'], job['artifact_sha256'])
        self.assertEqual(reference['status_ok']['field'], raw['name'])
        self.assertEqual(int(reference['status_ok']['record_timestamp_ns']), raw['timestamp_ns'])
        self.assertEqual(int(reference['status_ok']['current_cycle_timestamp_ns']), 1200000000)
        code_ref = reference['status_code']
        self.assertEqual(records[code_ref['record_index']]['type'], 'int64')
        self.assertEqual(records[code_ref['record_index']]['value'], -1)
        coverage = check['coverage']
        self.assertFalse(coverage['physical_acquisition_time_qualified'])
        self.assertFalse(coverage['native_timestamp_availability_qualified'])
        observed = coverage['module_observations'][0]
        self.assertIsNone(observed['physical_component_id'])
        self.assertEqual(observed['status_cycles'], coverage['expected_cycles'])
        self.assertEqual(observed['timing_cycles'], coverage['expected_cycles'])
        latest = observed['latest_observation']
        self.assertEqual(int(latest['robot_end_ns']) - int(latest['robot_start_ns']), 2000)
        self.assertAlmostEqual(latest['vendor_end_seconds'] - latest['vendor_start_seconds'], .001)
        drive = latest['signals']['DriveVelocity']
        self.assertEqual(drive['recorded_values']['RawValue'], 2.5)
        self.assertEqual(drive['raw_value_unit'], 'rotations per second')
        self.assertTrue(drive['references']['RawValue']['before_run'])
        self.assertFalse(drive['references']['RawValue']['updated_in_cycle'])
        raw_value_ref = drive['references']['RawValue']
        self.assertEqual(records[raw_value_ref['record_index']]['value'], 2.5)
        self.assertEqual(latest['signals']['TurnPosition']['recorded_values']['RawValue'], .125)
        receipt = observed['signal_observations']['DriveVelocity']
        self.assertEqual(receipt['receipt_comparisons']['held'], 1)
        self.assertEqual(receipt['raw_value_comparisons']['held'], observed['distinct_sdk_observations'])
        self.assertEqual(receipt['best_timestamp_sources'], {'1': observed['distinct_sdk_observations']})
        self.assertIn('physical_acquisition_time_not_qualified', check['unavailable'])
        self.assertEqual(next(c for c in report['checks'] if c['analyzer_id'] == 'swerve-tracking')['outcome'],
                         'insufficient_data')
        before = {row['report_id']: row['result_json'] for row in self.db.execute('SELECT report_id,result_json FROM analysis_reports')}
        self.db.close()
        self.db = open_catalog(self.root)
        self.addCleanup(self.db.close)
        Pipeline(self.root, self.db).tick()
        after = {row['report_id']: row['result_json'] for row in self.db.execute('SELECT report_id,result_json FROM analysis_reports')}
        self.assertEqual(before, after)
        self.assertEqual(Importer(self.root, self.db).get_job(job['id'])['artifact_sha256'], job['artifact_sha256'])

    def test_native_all_ok_and_repeated_values_do_not_claim_physical_freshness(self):
        official = self.official('phoenix-clear')
        self.assertTrue(all(r['value'] is True for r in official['records']
                            if r.get('name', '').endswith('StatusOk') and 'value' in r))
        _, _, report, check = self.import_case('phoenix-clear', 'MANUAL_LOCAL')
        self.assertEqual(report['source_type'], 'real')
        self.assertEqual(check['outcome'], 'evaluated_no_finding')
        self.assertFalse(check['findings'])
        self.assertEqual(report['overall_health'], 'not_assessed')
        self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])
        self.assertIn('sdk_age_fault_threshold_not_configured', check['unavailable'])
        for module in check['coverage']['module_observations']:
            comparisons = module['signal_observations']['DriveVelocity']['receipt_comparisons']
            self.assertGreater(comparisons['advanced'], 0)
            self.assertEqual(comparisons['held'], 1)

    def test_native_simulation_defaults_remain_unavailable(self):
        job, _, report, check = self.import_case('phoenix-sim-unavailable', 'MANUAL_LOCAL')
        self.assertEqual(report['source_type'], 'simulation')
        self.assertEqual(check['outcome'], 'insufficient_data')
        self.assertFalse(check['findings'])
        self.assertEqual(report['overall_health'], 'not_assessed')
        self.assertTrue(all(m['profile_cycles'] == 0 and m['latest_observation'] is None
                            for m in check['coverage']['module_observations']))
        rows = list(Importer(self.root, self.db).iter_dataset(job['id']))
        raw = next(r for r in rows if r.get('field') == '/Drive/Module0/PhoenixDriveVelocityRawValue'
                   and r.get('kind') == 'observation')
        self.assertEqual(raw['validity'], 'nonfinite')
        self.assertIsNone(raw['value'])
        self.assertTrue(raw['raw_hex'])
