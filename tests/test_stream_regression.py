"""Public invented originals exercise streamed incident transport and fresh proof."""
import hashlib
import json
from pathlib import Path
import tempfile
import struct
import unittest
from unittest.mock import patch
from robot_test_hub import reports,stream_analysis,stream_regression as regression
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.review import ReviewStore
from robot_test_hub.selftest import _sample
from robot_test_hub.storage import open_catalog
from test_wpilog import log_bytes,start,wire_record

LIMITS={'min_free_bytes':0}
EXPECTATIONS={'schema_version':1,'expected_outcome':'finding','checks':[{'kind':'finding_count','finding_kind':'disconnected_observations','min':1,'max':1}]}


def make_fixture(base,policy='automatic-reports-11'):
    root=base/'archive';root.mkdir();db=open_catalog(root)
    try:
        source=base/'invented.wpilog';source.write_bytes(_sample());job=Importer(root,db).import_file(source,source_type='SYNTHETIC')
        if job['state']!='succeeded':raise AssertionError(job)
        document=rebuild_from_imports(root,db);runner=reports.StreamReportRunner(root,db,limits={'min_free_bytes':0,'chunk_bytes':4096})
        try:
            reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version=policy)
            selected=None
            for _ in range(10000):
                with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=reports.advance_streams(root,db,runner,max_rows=100,max_bytes=4096)
                if progress['state'] in ('failed','canceled'):raise AssertionError(progress)
                if progress.get('report'):
                    check=next(c for c in progress['report']['checks'] if c['analyzer_id']=='recording-quality')
                    if check['findings']:selected=check;break
            if selected is None:raise AssertionError('no stream finding')
        finally:runner.close()
        index=next(i for i,f in enumerate(selected['findings']) if f['kind']=='disconnected_observations')
        review=ReviewStore(db).review_finding(dict(id='stream-review',job_id=selected['job_id'],finding_index=index,reviewer='Invented reviewer',rationale='Public generated source only',disposition='unresolved'))
        return root,review,selected
    finally:db.close()


def make_late_fixture(base,prefix=250001):
    root=base/'archive';root.mkdir();db=open_catalog(root)
    source=base/'invented-late.wpilog'
    events=[start(2,'/RealOutputs/TestHub/RobotId','string'),wire_record(2,b'late-robot'),
        start(3,'/RealOutputs/TestHub/BootId','string'),wire_record(3,b'late-boot'),
        start(4,'/RealOutputs/TestHub/RuntimeMode','string'),wire_record(4,b'SIM'),
        start(5,'/DriverStation/Enabled','boolean'),wire_record(5,b'\0'),
        start(6,'/Telemetry/Irrelevant','boolean'),start(7,'/Drive/Module0/DriveConnected','boolean'),wire_record(7,b'\1')]
    with source.open('wb') as output:
        output.write(log_bytes(*events));row=wire_record(6,b'\1')
        for _ in range(prefix//1000):output.write(row*1000)
        output.write(row*(prefix%1000))
        for index,enabled in ((1,True),(2,True),(3,False)):
            stamp=1000000000+index*100000000
            output.write(wire_record(1,struct.pack('<q',stamp),stamp//1000));output.write(wire_record(5,bytes([enabled]),stamp//1000))
            if index==2:output.write(wire_record(7,b'\0',stamp//1000))
    try:
        imported=Importer(root,db).import_file(source,source_type='SYNTHETIC')
        if imported['state']!='succeeded':raise AssertionError(imported)
        document=rebuild_from_imports(root,db);runner=reports.StreamReportRunner(root,db,limits={'min_free_bytes':0})
        try:
            sid=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version='automatic-reports-11')[0]['stream_id']
            while True:
                progress=reports.advance_streams(root,db,runner,max_rows=1000,max_bytes=1024*1024)
                if progress['state'] in ('failed','canceled'):raise AssertionError(progress)
                if progress.get('report'):break
        finally:runner.close()
        check=next(c for c in progress['report']['checks'] if c['analyzer_id']=='recording-quality')
        review=ReviewStore(db).review_finding(dict(id='late-stream-review',job_id=check['job_id'],finding_index=next(i for i,f in enumerate(check['findings']) if f['kind']=='disconnected_observations'),reviewer='Invented reviewer',rationale='Complete late source proof',disposition='unresolved'))
        return root,review,check,progress['descriptor']
    finally:db.close()


class StreamRegressionTests(unittest.TestCase):
    def test_roundtrip_complete_proof_and_fresh_candidate(self):
        with tempfile.TemporaryDirectory(prefix='stream case test ') as folder:
            base=Path(folder).resolve();root,review,selected=make_fixture(base)
            case=base/'case'
            exported=regression.export_case(root,review_id=review['id'],review_revision=1,expectations=EXPECTATIONS,output=case,limits=LIMITS)
            self.assertTrue(exported['verified']);self.assertEqual(exported['format'],regression.FORMAT)
            verified=regression.verify_case(case,limits=LIMITS);self.assertEqual(verified['case_id'],exported['case_id'])
            result=regression.run_case(case,limits=LIMITS)
            self.assertEqual(result['state'],'passed',result)
            self.assertFalse(result['robot_replay_executed'])
            self.assertEqual(result['scope'],regression.SCOPE)
            self.assertEqual(result['current_job_id'],selected['job_id'])
            self.assertFalse(any(p.name.endswith('.sqlite3') for p in case.rglob('*')))
            self.assertEqual(sum(p.stat().st_size for p in case.rglob('*') if p.is_file()),result['bundle_bytes'])

    def test_policies8_through11_have_fixed_available_original_proof(self):
        for policy in ('automatic-reports-8','automatic-reports-9','automatic-reports-10','automatic-reports-11'):
            with self.subTest(policy=policy),tempfile.TemporaryDirectory(prefix='stream fixed proof ') as folder:
                base=Path(folder).resolve();root,review,check=make_fixture(base,policy)
                case=base/'case';result=regression.export_case(root,review_id=review['id'],review_revision=1,expectations=EXPECTATIONS,output=case,limits=LIMITS)
                self.assertTrue(result['verified'])
                self.assertEqual(json.loads((case/'descriptor.json').read_text())['context']['pipeline_version'],policy)

    def test_admission_failure_never_publishes_a_prefix_case(self):
        from robot_test_hub.regression import RegressionError
        with tempfile.TemporaryDirectory(prefix='stream quota proof ') as folder:
            base=Path(folder).resolve();root,review,check=make_fixture(base)
            for name,limits in (('bundle',{'min_free_bytes':0,'max_bundle_bytes':1000}),
                                ('temp',{'min_free_bytes':0,'max_temp_bytes':1000}),
                                ('record',{'min_free_bytes':0,'record_bytes':4})):
                target=base/name
                with self.subTest(limit=name),self.assertRaises(RegressionError) as caught:
                    regression.export_case(root,review_id=review['id'],review_revision=1,expectations=EXPECTATIONS,output=target,limits=limits)
                self.assertEqual(caught.exception.code,'regression_resource_limit')
                self.assertFalse((target/'manifest.json').exists())
            self.assertEqual(list(root.glob('*.wpilog')),[])

    def test_late_original_beyond250k_prefix_reaches_full_eof_and_fresh_candidate(self):
        with tempfile.TemporaryDirectory(prefix='stream late source proof ') as folder:
            base=Path(folder).resolve();root,review,check,descriptor=make_late_fixture(base)
            self.assertGreater(descriptor['scanned_rows'],250000)
            original=next((root/'raw').rglob('*.wpilog'));before=hashlib.sha256(original.read_bytes()).hexdigest()
            case=base/'case';result=regression.export_case(root,review_id=review['id'],review_revision=1,expectations=EXPECTATIONS,output=case,limits=LIMITS)
            self.assertTrue(result['verified']);stored=json.loads((case/'descriptor.json').read_text())
            self.assertEqual(stored,descriptor)
            self.assertGreater(stored['scanned_rows'],250000)
            result=regression.run_case(case,limits=LIMITS)
            self.assertEqual(result['state'],'passed',result)
            self.assertEqual(result['checks'][0]['actual'],1)
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()
