"""Separate verified stream reports, fallback scheduling and coherent lifecycle."""
from contextlib import closing
import json
import copy
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub import reports,stream_analysis,storage
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog,DataRootOwner,OwnershipError,SCHEMA_VERSION
from robot_test_hub.stream_quality import RecordingQualityReducer
from robot_test_hub.backup import Backup,restore,verify

PUBLIC=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'

class StreamIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='stream integration ');self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve();self.root=self.base/'archive';self.root.mkdir()
        self.db=open_catalog(self.root);self.addCleanup(lambda:self.db.close())
        self.imported=Importer(self.root,self.db).import_file(PUBLIC,source_type='SYNTHETIC')
        self.assertEqual(self.imported['state'],'succeeded')
        full=rebuild_from_imports(self.root,self.db);self.document={**full,'runs':full['runs'][:1]}
    def runner(self,root=None,db=None,**limits):
        runner=stream_analysis.StreamRunner(root or self.root,db or self.db,reducer=RecordingQualityReducer(),limits={'min_free_bytes':0,**limits})
        self.addCleanup(runner.close);return runner
    def drain(self,runner,root=None,db=None,**budgets):
        for _ in range(20000):
            progress=reports.advance_streams(root or self.root,db or self.db,runner,**budgets)
            self.assertNotEqual(progress.get('state'),'failed',progress)
            if progress.get('report') is not None:return progress
        self.fail('Stream did not finish within bounded test turns')
    def test_explicit_eight_uses_no_tuple_admission_and_publishes_five_verified_checks(self):
        runner=self.runner()
        with patch.object(Importer,'read_manifest',side_effect=AssertionError('legacy read_manifest')),patch.object(Importer,'iter_dataset',side_effect=AssertionError('legacy iter_dataset')),patch.object(Importer,'_check_dataset',side_effect=AssertionError('legacy check_dataset')):
            scheduled=reports.generate(self.root,self.db,self.document,pipeline_version='automatic-reports-8')
            self.assertEqual(len(scheduled),1);self.assertIsNone(scheduled[0]['report_id'])
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
            complete=self.drain(runner)
        report=complete['report'];self.assertEqual(len(report['checks']),5)
        self.assertEqual(report['framework_version'],'stream-analysis-1');self.assertEqual(report['overall_health'],'not_assessed')
        self.assertEqual(report['source_type'],'synthetic')
        quality=report['checks'][0];self.assertTrue(quality['provenance']['full_source_verified'])
        self.assertEqual(quality['provenance']['input_format'],'stream-evidence-1')
        self.assertNotIn('rows_digest',quality['provenance'])
        for check in report['checks'][1:]:self.assertEqual((check['outcome'],check['unavailable']),('insufficient_data',['stream_evaluator_not_converted']))
    def test_automatic_fallback_preserves_old_report_and_explicit_seven_does_not_schedule(self):
        historical=reports.generate(self.root,self.db,self.document,max_rows=2,pipeline_version='automatic-reports-7')
        before={report['report_id']:self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report['report_id'],)).fetchone()[0] for report in historical}
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0],0)
        default=reports.generate(self.root,self.db,self.document,max_rows=2)
        self.assertEqual(default,historical);self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0],1)
        dispatch=reports.StreamReportRunner(self.root,self.db,limits={'min_free_bytes':0});self.addCleanup(dispatch.close)
        progress=self.drain(dispatch);self.assertEqual(progress['descriptor']['context']['legacy_fallback_report_id'],historical[0]['report_id'])
        for report_id,raw in before.items():self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report_id,)).fetchone()[0],raw)
    def test_pipeline_advances_fallback_without_import_or_review_changes(self):
        from robot_test_hub.pipeline import Pipeline
        pipeline=Pipeline(self.root,self.db,stream_limits=stream_analysis.Limits(min_free_bytes=0));self.addCleanup(pipeline.close)
        generate=reports.generate
        with patch('robot_test_hub.pipeline.generate',side_effect=lambda root,db,document:generate(root,db,document,max_rows=2)):
            first=pipeline.tick()
        self.assertEqual(first['state'],'indexed')
        old={row[0]:row[1] for row in self.db.execute('SELECT report_id,result_json FROM analysis_reports')}
        self.assertTrue(old);self.assertTrue(all(json.loads(raw)['framework_version']=='analysis-framework-2' for raw in old.values()))
        pending=self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0];self.assertGreater(pending,0)
        for _ in range(1000):
            self.assertEqual(pipeline.tick()['state'],'idle')
            ready=self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs WHERE report_id IS NOT NULL').fetchone()[0]
            if ready==pending:break
        else:self.fail('Worker did not publish the fallback reports')
        for report_id,raw in old.items():self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report_id,)).fetchone()[0],raw)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM analysis_reports WHERE json_extract(result_json,'$.framework_version')='stream-analysis-1'").fetchone()[0],pending)

    def test_normal_small_default_runs_remain_policy_seven_without_stream_work(self):
        actual=reports.generate(self.root,self.db,self.document)
        self.assertEqual(len(actual[0]['checks']),5);self.assertTrue(all(c['provenance']['context']['pipeline_version']=='automatic-reports-7' for c in actual[0]['checks']))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0],0)
        with self.assertRaises(ValueError):reports.selector_policy('automatic-reports-8',reports.STREAM_SELECTION)
        with self.assertRaises(ValueError):reports.analyzers(pipeline_version='automatic-reports-8')
    def test_ready_stream_identity_cannot_publish_a_forged_result_or_context(self):
        runner=self.runner();reports.schedule_stream_reports(self.root,self.db,self.document,runner=runner)
        for _ in range(20000):
            ready=runner.step()
            if ready['state']=='ready':break
        else:self.fail('No ready stream')
        for key in ('result','context','source_hashes'):
            descriptor=copy.deepcopy(ready['descriptor'])
            if key=='result':descriptor['result']['outcome']='evaluated_no_finding'
            elif key=='context':descriptor['context']['robot_id']='forged-identity'
            else:descriptor['source_hashes']=['f'*64]
            with self.subTest(key=key),self.assertRaises(ValueError):reports.publish_stream_report(self.db,ready['stream_id'],descriptor)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analyzer_jobs').fetchone()[0],0)
        self.assertIsNotNone(reports.publish_stream_report(self.db,ready['stream_id'],ready['descriptor'])['report_id'])

    def test_cancel_before_eof_publishes_nothing_then_resumes(self):
        runner=self.runner();scheduled=reports.schedule_stream_reports(self.root,self.db,self.document,runner=runner)[0]
        runner.cancel(scheduled['stream_id']);self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
        runner.resume(scheduled['stream_id']);complete=self.drain(runner,max_rows=7,max_bytes=4096)
        self.assertTrue(complete['descriptor']['full_source_verified'])
    def test_committed_chunks_and_work_state_are_in_coherent_backup(self):
        runner=self.runner(chunk_bytes=4096)
        scheduled=reports.schedule_stream_reports(self.root,self.db,self.document,runner=runner)[0]
        for _ in range(20000):
            progress=runner.step(max_rows=1,max_bytes=4096,max_seconds=.025)
            if progress['chunk_count']>0:break
        else:self.fail('No deterministic chunk sealed')
        self.assertNotEqual(progress['state'],'ready')
        saved={name:[tuple(row) for row in self.db.execute('SELECT * FROM '+name+' ORDER BY 1,2,3')] for name in ('analysis_stream_jobs','analysis_stream_sources','analysis_stream_chunks','analysis_stream_checkpoints','analysis_stream_unique','analysis_stream_reservations')}
        destination=self.base/'backup';Backup(self.root,destination).create('stream-partial');verify(destination/'stream-partial')
        manifest=json.loads((destination/'stream-partial/manifest.json').read_text())
        chunk_paths={row[0] for row in self.db.execute('SELECT path FROM analysis_stream_chunks')}
        self.assertTrue(chunk_paths<={item['path'] for item in manifest['files']})
        recovered=self.base/'recovered';restore(destination/'stream-partial',recovered)
        original=self.drain(runner)
        with closing(open_catalog(recovered)) as db:
            for name,rows in saved.items():self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY 1,2,3')],rows)
            restored_runner=self.runner(root=recovered,db=db,chunk_bytes=4096)
            restored=self.drain(restored_runner,root=recovered,db=db)
            self.assertEqual(restored['descriptor']['selection_chain'],original['descriptor']['selection_chain'])
            self.assertEqual(restored['report'],original['report'])
            restored_runner.close()
    def test_schema_nine_upgrade_installs_only_stream_metadata(self):
        self.db.close()
        from robot_test_hub import stream_tracking_store, stream_retry
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as db,db:
            for name, statement in reversed(tuple(stream_retry.SCHEMA_SQL.items())):
                ddl = statement.lstrip()
                kind = 'INDEX' if ddl.startswith('CREATE INDEX') else 'TRIGGER' if ddl.startswith('CREATE TRIGGER') else 'TABLE'
                db.execute('DROP '+kind+' IF EXISTS '+name)
            for name, statement in stream_tracking_store.SQL.items():
                ddl = statement.lstrip()
                kind = 'INDEX' if ddl.startswith('CREATE INDEX') else 'TRIGGER' if ddl.startswith('CREATE TRIGGER') else 'TABLE'
                db.execute('DROP '+kind+' IF EXISTS '+name)
            for name in stream_analysis.INDEX_SQL:db.execute('DROP INDEX IF EXISTS '+name)
            for name in reversed(tuple(stream_analysis.TABLE_SQL)):db.execute('DROP TABLE IF EXISTS '+name)
            db.execute('DELETE FROM schema_migrations WHERE version>=10');db.execute('PRAGMA user_version=9')
        with patch.object(Importer,'_check_dataset',side_effect=AssertionError('startup scan')),patch.object(stream_analysis.StreamRunner,'step',side_effect=AssertionError('startup work')):
            self.db=open_catalog(self.root)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        self.assertEqual([row[0] for row in self.db.execute('SELECT version FROM schema_migrations WHERE version>=10 ORDER BY version')],[10,11,12])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_tracking_samples').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_tracking_finalization').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_tracking_revision').fetchone()[0],0)
    def test_saved_config_limits_reach_the_pipeline_worker(self):
        from robot_test_hub.config import Config
        from robot_test_hub.demo import DemoSource
        from robot_test_hub.service import HubService
        from robot_test_hub.pipeline import Pipeline
        service=HubService(Config(data_dir=str(self.root),stream_max_spool_bytes=16*1024**2,stream_min_free_bytes=0),DemoSource())
        observed=[];original=Pipeline.__init__
        def initialize(pipeline,root,db,**options):
            observed.append(options['stream_limits']);original(pipeline,root,db,**options)
        def tick(pipeline):service.stop.set()
        try:
            with patch.object(Pipeline,'__init__',initialize),patch.object(Pipeline,'tick',tick):service._pipeline_worker()
            self.assertEqual(len(observed),1);self.assertEqual(observed[0].max_spool_bytes,16*1024**2);self.assertEqual(observed[0].min_free_bytes,0)
        finally:service.close()

    def test_service_retains_owner_while_pipeline_cleanup_is_pending(self):
        from robot_test_hub.config import Config
        from robot_test_hub.demo import DemoSource
        from robot_test_hub.service import HubService
        from robot_test_hub.pipeline import Pipeline
        service=HubService(Config(data_dir=str(self.root),shutdown_timeout=.05),DemoSource())
        entered=threading.Event();release=threading.Event()
        def tick(pipeline):service.stop.set()
        def close(pipeline):
            if release.is_set():return True
            entered.set();return False
        worker=threading.Thread(target=service._pipeline_worker);service.threads=[worker]
        try:
            with patch.object(Pipeline,'tick',tick),patch.object(Pipeline,'close',close):
                worker.start();self.assertTrue(entered.wait(2));self.assertFalse(service.close())
                with self.assertRaises(OwnershipError):DataRootOwner(self.root)
                release.set();worker.join(2);self.assertFalse(worker.is_alive());self.assertTrue(service.close())
        finally:
            release.set();worker.join(2);service.close()

    def test_pipeline_failure_survives_pending_cleanup_and_retains_owner(self):
        from robot_test_hub.config import Config
        from robot_test_hub.demo import DemoSource
        from robot_test_hub.service import HubService
        from robot_test_hub.pipeline import Pipeline
        service=HubService(Config(data_dir=str(self.root),shutdown_timeout=.05),DemoSource())
        entered=threading.Event();release=threading.Event()
        def close(pipeline):
            if release.is_set():return True
            entered.set();return False
        worker=threading.Thread(target=service._pipeline_worker);service.threads=[worker]
        try:
            with patch.object(Pipeline,'tick',side_effect=RuntimeError('indexing failed')),patch.object(Pipeline,'close',close):
                worker.start();self.assertTrue(entered.wait(2));self.assertFalse(service.close())
                self.assertEqual(service.health['indexer'],{'state':'cleanup_pending','error_code':'stream_cleanup_pending'})
                with self.assertRaises(OwnershipError):DataRootOwner(self.root)
                release.set();worker.join(2);self.assertFalse(worker.is_alive())
                self.assertEqual(service.health['indexer'],{'state':'failed','error_code':'indexer_failed'})
                self.assertTrue(service.close())
        finally:
            release.set();worker.join(2);service.close()

if __name__=='__main__':unittest.main()
