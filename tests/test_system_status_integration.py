"""Real importer/pipeline and pinned-selector integration for status reports."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import reports, regression, regression_evidence
from robot_test_hub.analysis import identity
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.review import ReviewStore
from robot_test_hub.runs import RunCatalog, rebuild_from_imports
from robot_test_hub.storage import open_catalog
from test_auto_swerve import protocol_recording
from test_wpilog import start, wire_record

PUBLIC = Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
FLAGS = ('Brownout','CANBusDown','CANBusUnavail','Display','IMU','IO','RSL','USB')


def with_status(raw, *, brownout=True, volts=11.8):
    header_size=12+int.from_bytes(raw[8:12],'little')
    events=[];entry=10000
    values=[('/SystemStats/BatteryVoltage','double',struct.pack('<d',volts),'{"unit":"volts"}')]
    for flag in FLAGS:
        values.extend([(f'/SystemStats/Faults/{flag}','boolean',bytes([brownout and flag=='Brownout']),'{}'),
                       (f'/SystemStats/FaultCounts/{flag}','int64',struct.pack('<q',3 if brownout and flag=='Brownout' else 0),'{}')])
    for path,kind,payload,metadata in values:
        events.extend([start(entry,path,kind,metadata),wire_record(entry,payload)]);entry+=1
    return raw[:header_size]+b''.join(events)+raw[header_size:]


class SystemStatusIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='system status integration ')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.root=self.base/'archive';self.root.mkdir()
        self.db=open_catalog(self.root);self.addCleanup(self.db.close)

    def import_bytes(self, raw):
        source=self.base/'invented.wpilog';source.write_bytes(raw)
        imported=Importer(self.root,self.db).import_file(source,source_type='SYNTHETIC')
        self.assertEqual(imported['state'],'succeeded',imported)
        return imported

    def test_actual_typed_recording_pipeline_adds_fourth_check_with_recorded_voltage_and_fault(self):
        raw=with_status(protocol_recording());self.import_bytes(raw)
        result=Pipeline(self.root,self.db).tick();self.assertEqual(result['runs'],1)
        document=json.loads(self.db.execute('SELECT result_json FROM analysis_reports').fetchone()[0])
        self.assertEqual(len(document['checks']),5)
        self.assertEqual([c['analyzer_id'] for c in document['checks']],
            ['recording-quality','swerve-tracking','phoenix-status-observation','system-status-observation','recorded-steering-observation'])
        check=next(c for c in document['checks'] if c['analyzer_id']=='system-status-observation')
        self.assertEqual(check['outcome'],'finding',check)
        self.assertEqual(check['provenance']['context']['pipeline_version'],'automatic-reports-7')
        self.assertEqual(check['provenance']['context']['row_selection'],reports.ROW_SELECTION)
        self.assertTrue(any(metric['name']=='recorded_battery_voltage_min' and metric['unit']=='volts' and metric['value']==11.8 for metric in check['metrics']))
        self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])
        self.assertFalse(check['coverage']['counter_semantics_qualified'])
        self.assertTrue(check['findings'])
        self.assertEqual(document['overall_health'],'not_assessed')
        self.assertEqual(document['source_hashes'],[hashlib.sha256(raw).hexdigest()])

    def test_system_status_review_is_an_executable_allowlisted_case(self):
        self.import_bytes(with_status(protocol_recording()));Pipeline(self.root,self.db).tick()
        report=json.loads(self.db.execute('SELECT result_json FROM analysis_reports').fetchone()[0])
        check=next(c for c in report['checks'] if c['analyzer_id']=='system-status-observation')
        review=ReviewStore(self.db).review_finding({'id':'system-incident','job_id':check['job_id'],'finding_index':0,
            'disposition':'unresolved','reviewer':'Invented fixture','rationale':'Recorded status only'})
        captured=regression_evidence.capture(self.root,review_id=review['id'],review_revision=1)
        self.assertEqual(captured['job']['analyzer_id'],'system-status-observation')
        self.assertEqual(captured['evidence']['context']['row_selection'],reports.ROW_SELECTION)
        self.assertTrue(any(row.get('field')=='/SystemStats/BatteryVoltage' and row.get('before_run') for row in captured['evidence']['rows']))
        folder=self.base/'system-case'
        regression.export_case(self.root,review_id=review['id'],review_revision=1,
            expectations={'schema_version':1,'expected_outcome':'finding',
                'checks':[{'kind':'finding_count','finding_kind':'recorded_system_fault_true','min':1}]},output=folder)
        current=regression.run_case(folder)
        self.assertEqual(current['state'],'passed');self.assertFalse(current['analyzer_version_changed'])
        self.assertIn('system_status',current['evaluator_module_sha256'])
        self.assertFalse(current['hardware_qualified'])

    def test_legacy_missing_status_is_unavailable_not_a_health_pass(self):
        self.import_bytes(protocol_recording());Pipeline(self.root,self.db).tick()
        for row in self.db.execute('SELECT result_json FROM analysis_reports'):
            document=json.loads(row[0]);check=next(c for c in document['checks'] if c['analyzer_id']=='system-status-observation')
            self.assertEqual(check['analyzer_id'],'system-status-observation')
            self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])
            self.assertEqual(document['overall_health'],'not_assessed')

    def test_exact_v5_selector_retains_report_and_filters_new_pre_run_fields(self):
        imported=self.import_bytes(with_status(PUBLIC.read_bytes()))
        run_document=rebuild_from_imports(self.root,self.db)
        old=reports.generate(self.root,self.db,run_document,pipeline_version=reports.LEGACY_VERSION)
        before={report['report_id']:self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report['report_id'],)).fetchone()[0] for report in old}
        self.assertTrue(all(len(report['checks'])==3 for report in old))
        old_job=next(c for report in old for c in report['checks'] if c['findings'])
        review=ReviewStore(self.db).review_finding({'id':'old-incident','job_id':old_job['job_id'],'finding_index':0,
            'disposition':'unresolved','reviewer':'Invented fixture','rationale':'Exact historical selector'})
        captured=regression_evidence.capture(self.root,review_id=review['id'],review_revision=1)
        self.assertFalse(any(row.get('kind')=='observation' and
            (row.get('field','').startswith('/SystemStats/Fault') or row.get('field')=='/SystemStats/BatteryVoltage')
            for row in captured['evidence']['rows']))
        self.assertTrue(regression_evidence.verify_originals(captured['originals'],captured['imports'],captured['evidence'],captured['job'])['verified'])
        Pipeline(self.root,self.db).tick()
        for report_id,value in before.items():self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report_id,)).fetchone()[0],value)
        newest=reports.generate(self.root,self.db,RunCatalog(self.db).current())
        self.assertTrue(all(len(report['checks'])==5 for report in newest))
        self.assertTrue(set(before).isdisjoint(report['report_id'] for report in newest))
        current=regression_evidence.capture(self.root,review_id=review['id'],review_revision=1)
        self.assertEqual(current,captured)

    def test_old_case_proof_pins_v5_and_current_execution_requires_version_opt_in(self):
        self.import_bytes(with_status(PUBLIC.read_bytes()))
        document=rebuild_from_imports(self.root,self.db)
        old=reports.generate(self.root,self.db,document,pipeline_version=reports.LEGACY_VERSION)
        check=next(c for report in old for c in report['checks'] if c['analyzer_id']=='recording-quality' and c['findings'])
        review=ReviewStore(self.db).review_finding({'id':'old-case','job_id':check['job_id'],'finding_index':0,
            'disposition':'unresolved','reviewer':'Invented fixture','rationale':'Version upgrade case'})
        folder=self.base/'case'
        expectation={'schema_version':1,'expected_outcome':check['outcome'],
            'checks':[{'kind':'finding_count','finding_kind':check['findings'][0]['kind'],'min':1}]}
        regression.export_case(self.root,review_id=review['id'],review_revision=1,expectations=expectation,output=folder)
        self.assertTrue(regression.verify_case(folder)['verified'])
        blocked=regression.run_case(folder)
        self.assertEqual(blocked['state'],'unavailable');self.assertEqual(blocked['code'],'regression_analyzer_version_changed')
        allowed=regression.run_case(folder,allow_analyzer_version_change=True)
        self.assertEqual(allowed['state'],'passed');self.assertTrue(allowed['analyzer_version_changed'])
        self.assertEqual(allowed['stored_analyzer_version'],'automatic-reports-5')
        self.assertEqual(allowed['current_analyzer_version'],'automatic-reports-7')
        self.assertIn('system_status',allowed['evaluator_module_sha256'])
        evidence=json.loads((folder/'evidence.json').read_text())
        self.assertEqual(evidence['context']['pipeline_version'],'automatic-reports-5')
        self.assertEqual(evidence['context']['row_selection'],reports.ROW_SELECTION_V5)

    def test_selector_pairs_cannot_silently_retarget(self):
        for version,selection in ((reports.LEGACY_VERSION,reports.ROW_SELECTION),
                                  (reports.VERSION,reports.ROW_SELECTION_V5),('unknown',reports.ROW_SELECTION),
                                  (None,None)):
            with self.subTest(version=version),self.assertRaises(ValueError):reports.selector_policy(version,selection)


if __name__=='__main__':unittest.main()
