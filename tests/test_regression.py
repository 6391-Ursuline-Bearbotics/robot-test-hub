"""Executable regression cases from real public synthetic imports and saved reviews."""
from dataclasses import replace
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import regression
from robot_test_hub.analysis import identity
from robot_test_hub.importer import Importer
from robot_test_hub.notebook import Notebook
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.review import ReviewStore
from robot_test_hub.selftest import _sample
from robot_test_hub.storage import DataRootOwner,open_catalog

EXPECTATIONS = {'schema_version':1,'expected_outcome':'finding','checks':[
    {'kind':'metric','selector':{'name':'gap_count','unit':'count'},'min':0,'max':0},
    {'kind':'finding_count','finding_kind':'disconnected_observations','min':1,'max':1}]}


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='rth-regression-tests-')
        cls.base=Path(cls.temporary.name).resolve()
        cls.root=cls.base/'archive'
        sample=cls.base/'sample.wpilog';sample.write_bytes(_sample())
        owner=DataRootOwner(cls.root)
        db=None
        try:
            db=open_catalog(cls.root)
            imported=Importer(cls.root,db).import_file(sample,source_type='SYNTHETIC')
            if imported['state']!='succeeded':raise AssertionError(imported)
            Pipeline(cls.root,db).tick()
            jobs=[json.loads(row[0]) for row in db.execute("SELECT result_json FROM analyzer_jobs WHERE analyzer_id='recording-quality' ORDER BY job_id")]
            cls.job=next(job for job in jobs if any(finding.get('kind')=='disconnected_observations' for finding in job['findings']))
            cls.review=ReviewStore(db).review_finding(dict(id='synthetic-regression-review',job_id=cls.job['job_id'],finding_index=next(i for i,f in enumerate(cls.job['findings']) if f['kind']=='disconnected_observations'),reviewer='synthetic-reviewer',rationale='Invented saved regression incident',disposition='unresolved'))
        except BaseException:
            cls.temporary.cleanup()
            raise
        finally:
            if db is not None:db.close()
            owner.close()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temporary_case=tempfile.TemporaryDirectory(prefix='rth-private-case-')
        self.addCleanup(self.temporary_case.cleanup)
        self.folder=Path(self.temporary_case.name).resolve()/'case'

    def export(self, expectations=None):
        return regression.export_case(self.root,review_id=self.review['id'],review_revision=1,expectations=expectations or EXPECTATIONS,output=self.folder)

    def rewrite(self, mutate):
        case=json.loads((self.folder/'case.json').read_text(encoding='utf-8'))
        job=json.loads((self.folder/'saved-job.json').read_text(encoding='utf-8'))
        evidence=json.loads((self.folder/'evidence.json').read_text(encoding='utf-8'))
        mutate(case,evidence,job)
        for name,value in [('case.json',case),('evidence.json',evidence),('saved-job.json',job)]:
            (self.folder/name).write_bytes(regression._json_bytes(value))
        manifest=json.loads((self.folder/'manifest.json').read_text(encoding='utf-8'))
        for item in manifest['inventory']:
            data=(self.folder/item['path']).read_bytes();item.update(size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        (self.folder/'manifest.json').write_bytes(regression._json_bytes(manifest))

    def test_real_export_repeat_run_and_retained_archive_artifacts_unchanged(self):
        def snapshot():return {p.relative_to(self.root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file() and p.name not in ('catalog.sqlite3-wal','catalog.sqlite3-shm')}
        before=snapshot()
        receipt=self.export();self.assertTrue(receipt['verified'])
        self.assertEqual(receipt['source_type'],'synthetic')
        self.assertEqual(receipt['reference_scope'],'incident_evidence_only')
        first=regression.run_case(self.folder);second=regression.run_case(self.folder)
        self.assertEqual(first,second)
        self.assertEqual(first['state'],'passed',first)
        self.assertFalse(first['robot_replay_executed']);self.assertEqual(first['overall_health'],'not_assessed')
        self.assertEqual(set(first['evaluator_module_sha256']),{'analysis','reports','swerve','phoenix_status','system_status','steering','analysis_plan','regression'})
        self.assertEqual(len(first['loaded_evaluator_code_sha256']),64)
        self.assertEqual(snapshot(),before)

    def test_wrong_explicit_threshold_fails_and_cli_returns_nonzero(self):
        expectations=json.loads(json.dumps(EXPECTATIONS));expectations['checks'][0]['min']=10;expectations['checks'][0]['max']=20
        self.export(expectations)
        report=regression.run_case(self.folder);self.assertEqual(report['state'],'failed')
        with redirect_stdout(io.StringIO()) as output:
            code=regression.main(['run','--case',str(self.folder),'--json'])
        self.assertEqual(code,1);self.assertEqual(json.loads(output.getvalue())['state'],'failed')

    def test_tampered_original_or_structured_metadata_never_runs(self):
        self.export()
        original=next((self.folder/'originals').iterdir());original.write_bytes(original.read_bytes()+b'changed')
        with self.assertRaises(regression.RegressionError):regression.run_case(self.folder)
        shutil.rmtree(self.folder);self.export()
        with (self.folder/'evidence.json').open('ab') as stream:stream.write(b' ')
        with self.assertRaises(regression.RegressionError):regression.verify_case(self.folder)

    def test_refreshed_manifest_cannot_hide_provenance_or_review_tampering(self):
        self.export()
        self.rewrite(lambda case,evidence,job:evidence['rows'][0].update(kind='changed'))
        with self.assertRaises(regression.RegressionError):regression.verify_case(self.folder)
        shutil.rmtree(self.folder);self.export()
        def coherent_tamper(case,evidence,job):
            evidence['rows'][0]['invented_regression_value']=42
            job['provenance']['rows_digest']=identity(evidence['rows'])
            job['job_id']=identity([case['framework_version'],job['declaration'],job['provenance'],job['configuration']])
            case['job_id']=job['job_id'];case['review']['job_id']=job['job_id']
            case['review']['finding_identity']=identity([job['job_id'],case['review']['finding_index'],job['findings'][case['review']['finding_index']]])
        self.rewrite(coherent_tamper)
        with self.assertRaises(regression.RegressionError) as failed:
            regression.verify_case(self.folder)
        self.assertEqual(failed.exception.code,'regression_evidence_integrity')
        shutil.rmtree(self.folder);self.export()
        self.rewrite(lambda case,evidence,job:case['review'].update(finding_identity='0'*64))
        with self.assertRaises(regression.RegressionError):regression.verify_case(self.folder)

    def test_unknown_analyzer_is_unavailable_and_version_change_requires_option(self):
        self.export()
        def mutate(case,evidence,job):
            case['analyzer_id']='unknown-analyzer';job['analyzer_id']=case['analyzer_id'];job['declaration']['analyzer_id']=case['analyzer_id']
            job['job_id']=identity([case['framework_version'],job['declaration'],job['provenance'],job['configuration']]);case['job_id']=job['job_id'];case['review']['job_id']=job['job_id'];case['review']['finding_identity']=identity([job['job_id'],case['review']['finding_index'],job['findings'][case['review']['finding_index']]])
        self.rewrite(mutate)
        self.assertEqual(regression.run_case(self.folder)['state'],'unavailable')
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(regression.main(['run','--case',str(self.folder),'--json']),1)
        self.assertEqual(json.loads(output.getvalue())['state'],'unavailable')
        shutil.rmtree(self.folder);self.export()
        from robot_test_hub.reports import analyzers
        current=analyzers()
        selected=next(analyzer for analyzer in current if analyzer.declaration.analyzer_id==self.job['analyzer_id'])
        selected.declaration=replace(selected.declaration,version='deliberate-test-version')
        with patch('robot_test_hub.reports.analyzers',return_value=current):
            self.assertEqual(regression.run_case(self.folder)['code'],'regression_analyzer_version_changed')
            self.assertEqual(regression.run_case(self.folder,allow_analyzer_version_change=True)['state'],'passed')

    def test_actual_current_evaluator_executes_without_saved_cache(self):
        self.export()
        from robot_test_hub.reports import analyzers
        current=analyzers();selected=next(analyzer for analyzer in current if analyzer.declaration.analyzer_id==self.job['analyzer_id'])
        original=selected.evaluate;calls=[]
        def changed(evidence,configuration):
            calls.append(True);result=original(evidence,configuration)
            next(metric for metric in result['metrics'] if metric['name']=='gap_count')['value']=42
            return result
        selected.evaluate=changed
        baseline=regression.run_case(self.folder)
        with patch('robot_test_hub.reports.analyzers',return_value=current):report=regression.run_case(self.folder)
        self.assertEqual(len(calls),1);self.assertEqual(report['state'],'failed')
        self.assertNotEqual(report['loaded_evaluator_code_sha256'],baseline['loaded_evaluator_code_sha256'])

    def test_missing_null_nonfinite_or_ambiguous_metrics_fail(self):
        self.export()
        from robot_test_hub.reports import analyzers
        for mode in ('missing','null','ambiguous','nonfinite'):
            current=analyzers();selected=next(analyzer for analyzer in current if analyzer.declaration.analyzer_id==self.job['analyzer_id']);original=selected.evaluate
            def changed(evidence,configuration,mode=mode,original=original):
                result=original(evidence,configuration);value=next(metric for metric in result['metrics'] if metric['name']=='gap_count')
                if mode=='missing':result['metrics'].remove(value)
                elif mode=='null':value['value']=None
                elif mode=='nonfinite':value['value']=float('nan')
                else:result['metrics'].append(dict(value))
                return result
            selected.evaluate=changed
            with self.subTest(mode=mode),patch('robot_test_hub.reports.analyzers',return_value=current):
                self.assertEqual(regression.run_case(self.folder)['state'],'failed')

    def test_strict_expectations_partial_groups_extra_files_and_git_outputs(self):
        for invalid in ({'schema_version':1,'expected_outcome':'finding','checks':[]}, {'schema_version':1,'expected_outcome':'finding','checks':[{'kind':'metric','selector':{'name':'gap_count','unit':'count'},'min':True}]}, {'schema_version':1,'expected_outcome':'finding','checks':[{'kind':'finding_count','finding_kind':'x','min':2,'max':1}]}):
            with self.subTest(invalid=invalid),self.assertRaises(regression.RegressionError):self.export(invalid)
        self.assertFalse(self.folder.exists())
        self.export();(self.folder/'extra.txt').write_bytes(b'not in inventory')
        with self.assertRaises(regression.RegressionError):regression.verify_case(self.folder)
        (self.folder/'extra.txt').unlink();(self.folder/'manifest.json').unlink()
        with self.assertRaises((regression.RegressionError,OSError)):regression.verify_case(self.folder)
        git_parent=Path(self.temporary_case.name)/'private-but-git';git_parent.mkdir();(git_parent/'.git').write_text('gitdir: elsewhere',encoding='utf-8')
        with self.assertRaises(regression.RegressionError):
            regression.export_case(self.root,review_id=self.review['id'],review_revision=1,expectations=EXPECTATIONS,output=git_parent/'case')

    def test_cli_reports_only_recognized_fixed_failure_codes(self):
        with patch.object(regression,'verify_case',side_effect=regression.RegressionError('regression_integrity')),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(regression.main(['verify','--case',str(self.folder)]),1)
        self.assertEqual(json.loads(output.getvalue())['code'],'regression_integrity')
        with patch.object(regression,'verify_case',side_effect=ValueError('PRIVATE_PATH SECRET')),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(regression.main(['verify','--case',str(self.folder)]),1)
        self.assertEqual(json.loads(output.getvalue())['code'],'regression_operation_failed')
        self.assertNotIn('PRIVATE_PATH',output.getvalue())
        self.assertNotIn('SECRET',output.getvalue())

    def test_duplicate_nonfinite_json_and_hardlinks_are_rejected(self):
        for data in (b'{"schema_version":1,"schema_version":1}',b'{"value":NaN}',b'{"value":1e999}'):
            with self.assertRaises(regression.RegressionError):regression._parse(data)
        self.export()
        import os
        linked=Path(self.temporary_case.name)/'linked-case-json'
        os.link(self.folder/'case.json',linked)
        with self.assertRaises(regression.RegressionError):regression.verify_case(self.folder)


if __name__=='__main__':
    unittest.main()
