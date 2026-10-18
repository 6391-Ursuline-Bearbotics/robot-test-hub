"""Independent historical-evidence and offline analyzer-regression checks."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.storage import open_catalog
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.review import ReviewStore
from test_wpilog import wire_record

FIXTURE=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'


class RegressionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='regression review ')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.root=self.base/'archive';self.root.mkdir()
        self.db=open_catalog(self.root);self.addCleanup(self.db.close)

    def imported(self,*,multiple=False,pipeline_version=None):
        Importer(self.root,self.db).import_file(FIXTURE,source_type='SYNTHETIC')
        if multiple:
            second=self.base/'second.wpilog'
            second.write_bytes(FIXTURE.read_bytes()+wire_record(0,b'\x01'+struct.pack('<I',1),timestamp_us=10000000))
            self.assertEqual(Importer(self.root,self.db).import_file(second,source_type='SYNTHETIC')['state'],'succeeded')
        if pipeline_version is None:
            Pipeline(self.root,self.db).tick()
        else:
            from robot_test_hub import reports
            from robot_test_hub.runs import rebuild_from_imports
            document=rebuild_from_imports(self.root,self.db)
            reports.generate(self.root,self.db,document,pipeline_version=pipeline_version)
        return [json.loads(row[0]) for row in self.db.execute('SELECT result_json FROM analysis_reports ORDER BY report_id')]

    def reviewed(self,*,pipeline_version=None):
        reports=self.imported(pipeline_version=pipeline_version)
        job=next(check for report in reports for check in report['checks'] if check['findings'])
        review=ReviewStore(self.db).review_finding({'id':'review-oracle','job_id':job['job_id'],'finding_index':0,
            'disposition':'unresolved','reviewer':'independent fixture','rationale':'Invented public-fixture review'})
        return review,job

    def exported(self,expectations=None,*,pipeline_version=None):
        from robot_test_hub import regression
        review,job=self.reviewed(pipeline_version=pipeline_version)
        expectations=expectations or {'schema_version':1,'expected_outcome':'finding',
            'checks':[{'kind':'finding_count','finding_kind':'disconnected_observations','min':1,'max':1}]}
        folder=self.base/'case'
        regression.export_case(self.root,review_id=review['id'],review_revision=1,expectations=expectations,output=folder)
        return folder,job

    def reseal(self,folder):
        manifest=json.loads((folder/'manifest.json').read_bytes())
        for item in manifest['inventory']:
            raw=(folder/item['path']).read_bytes();item.update(size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        (folder/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n',encoding='utf-8')

    def test_multi_source_selector_preserves_pre_refactor_job_report_and_row_identities(self):
        # Captured before extracting automatic-reports-5's selector. Two immutable
        # sources overlap identical cycles; the appended valid control changes
        # source identity without replacing any logged sample.
        reports=self.imported(multiple=True,pipeline_version='automatic-reports-5')
        expected={
            '90f0ce767b49f59d5c92e9a868f739a4b8dade342f8bd98120af8ddb6c1bf252':(
                '48f20267336e435dbea3d51399c4f519e5dacb229722e37de74bece77e1aa0ec',
                ['e4cb0734c76bb7da19d800f94687dbc042214567fd54862a88b7a4745622ed22','eed7361efee578e87f70863244f48af10612a6d7143bc7476559d0a78d2c3a04','3de26de43d38952b2815f1e456cdcb3ade2c867c5b9e16ade137adff40c190d2']),
            '926f4bd5e8a0e12cf0c368897ca62cff981244b236fed4379473992b4203e9aa':(
                '5226e6ac42c68bc2ffb562bb2d4edcd39b3987bd337b2901af674d5e0617bf98',
                ['e3629c8493abf7377bf419d2612c26ea43114e2da24ddba530a2304eaa7281e9','9a96cf07b85ead64ab588e3b3c7720f9360c9b34428f495a83308d6e935117c1','11ff4c4c2f54f41b24951a0cba52775204a03e3d994d299ad0be1ba4c8044a7a'])}
        self.assertEqual({report['report_id'] for report in reports},set(expected))
        for report in reports:
            digest,jobs=expected[report['report_id']]
            self.assertEqual(len(report['source_hashes']),2)
            self.assertEqual([check['job_id'] for check in report['checks']],jobs)
            self.assertEqual({check['provenance']['rows_digest'] for check in report['checks']},{digest})


    def test_capture_exact_old_review_ignores_current_catalog_plans_and_schema_writers(self):
        from robot_test_hub import regression_evidence
        from robot_test_hub.runs import RunCatalog
        review,job=self.reviewed()
        ReviewStore(self.db).review_finding({**review,'revision':2,'expected_revision':1,'disposition':'expected_behavior','rationale':'Later correction must not replace revision one'})
        with self.db:
            self.db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',('f'*64,json.dumps({'revision':'f'*64,'runs':[],'mappings':[]})))
            self.db.execute('UPDATE run_catalog_state SET revision=? WHERE id=1',('f'*64,))
        before=(self.root/'catalog.sqlite3').read_bytes()
        with patch.object(Importer,'__init__',side_effect=AssertionError('no importer constructor during readonly capture')),patch.object(ReviewStore,'__init__',side_effect=AssertionError('no review constructor')),patch.object(RunCatalog,'current',side_effect=AssertionError('no latest catalog')),patch.object(socket,'socket',side_effect=AssertionError('no network')),patch.object(subprocess,'Popen',side_effect=AssertionError('no processes')):
            captured=regression_evidence.capture(self.root,review_id=review['id'],review_revision=1)
        self.assertEqual(captured['review'],review)
        self.assertEqual(captured['job'],job)
        self.assertEqual(captured['evidence']['context'],job['provenance']['context'])
        self.assertEqual(captured['evidence']['source_hashes'],tuple(job['provenance']['source_hashes']))
        self.assertEqual((self.root/'catalog.sqlite3').read_bytes(),before)
        with self.assertRaises(regression_evidence.EvidenceError):regression_evidence.capture(self.root,review_id=review['id'],review_revision=3)

    def test_capture_rejects_raw_hardlinks_and_modified_bytes_without_repair(self):
        from robot_test_hub import regression_evidence
        review,_=self.reviewed()
        raw=next((self.root/'raw').rglob('*.wpilog'));alias=self.base/'outside-alias.wpilog'
        try:alias.hardlink_to(raw)
        except OSError as exc:self.skipTest('Filesystem cannot create local hardlinks: '+type(exc).__name__)
        try:
            with self.assertRaises(regression_evidence.EvidenceError):regression_evidence.capture(self.root,review_id=review['id'],review_revision=1)
        finally:alias.unlink()
        original=raw.read_bytes();changed=original[:-1]+bytes([original[-1]^1]);raw.write_bytes(changed)
        with self.assertRaises(regression_evidence.EvidenceError):regression_evidence.capture(self.root,review_id=review['id'],review_revision=1)
        self.assertEqual(raw.read_bytes(),changed)

    def test_current_evaluator_runs_fresh_and_code_fingerprint_tracks_actual_behavior(self):
        from robot_test_hub import regression,reports
        from robot_test_hub.analysis import Analyzer
        folder,saved=self.exported();before=(self.root/'catalog.sqlite3').read_bytes()
        normal=regression.run_case(folder);self.assertEqual(normal['state'],'passed')
        original=next(a for a in reports.analyzers() if a.declaration.analyzer_id==saved['analyzer_id']);called=[]
        def changed(evidence,configuration):
            called.append(True)
            value=original.evaluate(evidence,configuration)
            value.update(outcome='evaluated_no_finding',findings=[])
            return value
        candidate=Analyzer(original.declaration,changed)
        with patch.object(reports,'analyzers',return_value=[candidate]),patch.object(socket,'socket',side_effect=AssertionError('no network')),patch.object(subprocess,'Popen',side_effect=AssertionError('no child/native execution')):
            result=regression.run_case(folder)
        self.assertEqual(called,[True]);self.assertEqual(result['state'],'failed')
        self.assertEqual(result['current_outcome'],'evaluated_no_finding')
        self.assertNotEqual(normal['loaded_evaluator_code_sha256'],result['loaded_evaluator_code_sha256'])
        self.assertEqual((self.root/'catalog.sqlite3').read_bytes(),before)

    def test_changed_analyzer_version_requires_explicit_opt_in_without_breaking_proof(self):
        from robot_test_hub import regression,reports
        from robot_test_hub.analysis import Analyzer
        folder,saved=self.exported()
        original=next(a for a in reports.analyzers() if a.declaration.analyzer_id==saved['analyzer_id']);called=[]
        def evaluated(evidence,configuration):
            called.append(True);return original.evaluate(evidence,configuration)
        candidate=Analyzer(replace(original.declaration,version='independent-next-version'),evaluated)
        with patch.object(reports,'analyzers',return_value=[candidate]):
            refused=regression.run_case(folder)
            self.assertEqual(refused['state'],'unavailable');self.assertEqual(called,[])
            accepted=regression.run_case(folder,allow_analyzer_version_change=True)
        self.assertEqual(accepted['state'],'passed');self.assertEqual(called,[True])
        self.assertTrue(accepted['analyzer_version_changed'])
        self.assertNotEqual(accepted['current_job_id'],saved['job_id'])

    def test_fully_resealed_forged_rows_cannot_claim_unchanged_original_proof(self):
        from robot_test_hub import regression
        from robot_test_hub.analysis import identity
        folder,_=self.exported()
        case=json.loads((folder/'case.json').read_bytes());evidence=json.loads((folder/'evidence.json').read_bytes());job=json.loads((folder/'saved-job.json').read_bytes())
        row=next(row for row in evidence['rows'] if row.get('kind')=='observation' and row.get('field')=='/Drive/Module0/Connected' and row.get('value') is False)
        row['value']=True
        job['provenance']['rows_digest']=identity(evidence['rows'])
        job['job_id']=identity([case['framework_version'],job['declaration'],job['provenance'],job['configuration']])
        case['job_id']=job['job_id'];case['review']['job_id']=job['job_id']
        case['review']['finding_identity']=identity([job['job_id'],case['review']['finding_index'],job['findings'][case['review']['finding_index']]])
        for name,value in (('case.json',case),('evidence.json',evidence),('saved-job.json',job)):
            (folder/name).write_text(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n',encoding='utf-8')
        self.reseal(folder)
        with self.assertRaises(regression.RegressionError):regression.verify_case(folder)

    def test_real_recording_quality_version_bump_preserves_old_selector_proof(self):
        from robot_test_hub import regression,reports
        folder,saved=self.exported(pipeline_version='automatic-reports-5')
        with patch.object(reports,'VERSION','automatic-reports-7'):
            refused=regression.run_case(folder)
            self.assertEqual(refused['state'],'unavailable')
            self.assertEqual(refused['code'],'regression_analyzer_version_changed')
            accepted=regression.run_case(folder,allow_analyzer_version_change=True)
        self.assertEqual(accepted['state'],'passed')
        self.assertEqual(accepted['stored_analyzer_version'],'automatic-reports-5')
        self.assertEqual(accepted['current_analyzer_version'],'automatic-reports-7')
        self.assertNotEqual(accepted['current_job_id'],saved['job_id'])

    def test_missing_null_ambiguous_or_boolean_metrics_fail_without_health_claims(self):
        from robot_test_hub import regression,reports
        from robot_test_hub.analysis import Analyzer
        expectations={'schema_version':1,'expected_outcome':'finding','checks':[{'kind':'metric','selector':{'name':'gap_count','unit':'count'},'min':0,'max':0}]}
        folder,saved=self.exported(expectations)
        original=next(a for a in reports.analyzers() if a.declaration.analyzer_id==saved['analyzer_id'])
        for values in ([],[None],[0,0],[True]):
            with self.subTest(values=values):
                def evaluated(evidence,configuration):
                    result=original.evaluate(evidence,configuration)
                    result['metrics']=[{'name':'gap_count','unit':'count','value':value} for value in values]
                    return result
                with patch.object(reports,'analyzers',return_value=[Analyzer(original.declaration,evaluated)]):
                    result=regression.run_case(folder)
                self.assertEqual(result['state'],'failed');self.assertIsNone(result['checks'][0]['actual'])
                self.assertEqual(result['overall_health'],'not_assessed');self.assertFalse(result['hardware_qualified'])
        case=json.loads((folder/'case.json').read_bytes())
        case['expectations']={'schema_version':1,'expected_outcome':'insufficient_data','checks':[{'kind':'finding_count','finding_kind':'disconnected_observations','min':0,'max':0}]}
        (folder/'case.json').write_text(json.dumps(case),encoding='utf-8');self.reseal(folder)
        def unavailable(evidence,configuration):return {'outcome':'insufficient_data','metrics':[],'findings':[],'coverage':{},'unavailable':['fixture_missing_evidence']}
        with patch.object(reports,'analyzers',return_value=[Analyzer(original.declaration,unavailable)]):result=regression.run_case(folder)
        self.assertEqual(result['state'],'passed');self.assertEqual(result['current_outcome'],'insufficient_data')
        self.assertEqual(result['overall_health'],'not_assessed');self.assertFalse(result['hardware_qualified']);self.assertFalse(result['robot_replay_executed'])

    def test_portable_metadata_hardlink_and_extraneous_files_are_not_verified(self):
        from robot_test_hub import regression
        folder,_=self.exported();alias=self.base/'linked-metadata'
        try:alias.hardlink_to(folder/'evidence.json')
        except OSError as exc:self.skipTest('Filesystem cannot create local hardlinks: '+type(exc).__name__)
        try:
            with self.assertRaises(regression.RegressionError):regression.verify_case(folder)
        finally:alias.unlink()
        (folder/'extras').mkdir();(folder/'extras/private.bin').write_bytes(b'not part of the selected evidence')
        with self.assertRaises(regression.RegressionError):regression.verify_case(folder)

if __name__=='__main__':unittest.main()
