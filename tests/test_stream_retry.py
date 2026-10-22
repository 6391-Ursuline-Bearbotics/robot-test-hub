"""Exact-attempt retry contracts across fixed stream policies and storage bounds."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from robot_test_hub import reports,stream_analysis,stream_retry as retry
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.storage import open_catalog,SCHEMA_VERSION
from test_stream_review import imported_fixture

class StreamRetryTests(unittest.TestCase):
    def fixture(self,policy='automatic-reports-8'):
        temporary=tempfile.TemporaryDirectory(prefix='stream retry author ');self.addCleanup(temporary.cleanup)
        root=Path(temporary.name).resolve();db=open_catalog(root);self.addCleanup(db.close)
        imported,document=imported_fixture(root,db,cycles=12,connections_per_cycle=4)
        runner=reports.StreamReportRunner(root,db,limits={'min_free_bytes':0,'chunk_bytes':4096});self.addCleanup(runner.close)
        reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version=policy)
        sid=db.execute('SELECT stream_id FROM analysis_stream_jobs').fetchone()[0]
        for _ in range(2000):
            with patch.object(stream_analysis.time,'monotonic',return_value=0):runner.step(sid,max_rows=3)
            job=dict(db.execute('SELECT * FROM analysis_stream_jobs').fetchone())
            self.assertNotEqual(job['state'],'failed',job)
            if job['chunk_count']:break
        else:self.fail('No committed chunk')
        runner.runners[policy].cancel(sid)
        return root,db,runner,sid

    def job(self,db):return dict(db.execute('SELECT * FROM analysis_stream_jobs').fetchone())
    def payload(self,db,request='1'*32):return {'request_id':request,**retry.eligibility(self.job(db))['pins']}

    def test_all_fixed_policies_retry_empty_and_publish_only_after_fresh_full_verification(self):
        for policy in ('automatic-reports-8','automatic-reports-9','automatic-reports-10','automatic-reports-11'):
            with self.subTest(policy=policy):
                root,db,runner,sid=self.fixture(policy);old=self.job(db)
                sourcepins=[tuple(row) for row in db.execute('SELECT import_job_id,pins_json FROM analysis_stream_sources WHERE generation=1')]
                oldcheckpoint=db.execute('SELECT state_json FROM analysis_stream_checkpoints WHERE generation=1').fetchone()[0]
                retry.enqueue(db,sid,self.payload(db));self.assertEqual(retry.step(db,runner)['state'],'running')
                fresh=self.job(db)
                self.assertEqual(fresh['generation'],2)
                self.assertEqual(fresh['stream_id'],sid)
                self.assertEqual(fresh['context_json'],old['context_json'])
                self.assertEqual(fresh['verified_bytes'],0);self.assertEqual(fresh['chunk_count'],0)
                self.assertEqual([tuple(row) for row in db.execute('SELECT import_job_id,pins_json FROM analysis_stream_sources WHERE generation=2')],sourcepins)
                self.assertEqual(db.execute('SELECT state_json FROM analysis_stream_checkpoints WHERE generation=1').fetchone()[0],oldcheckpoint)
                self.assertEqual(db.execute('SELECT chunk_ordinal FROM analysis_stream_checkpoints WHERE generation=2').fetchone()[0],-1)
                # Empty ordered work still has bounded validation subphases.
                for _ in range(32):
                    with patch.object(stream_analysis.time,'monotonic',return_value=0):runner.step(sid,max_bytes=1,max_rows=1)
                    partial=self.job(db)
                    self.assertIsNone(partial['report_id'])
                    if partial['verified_bytes']:break
                self.assertEqual(partial['verified_bytes'],1)
                self.assertIsNone(partial['report_id'])
                for _ in range(2000):
                    with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=reports.advance_streams(root,db,runner,max_rows=100)
                    self.assertNotEqual(progress['state'],'failed',progress)
                    if progress.get('report_id'):break
                else:self.fail('Fresh retry did not complete')
                self.assertEqual(retry.step(db,runner)['state'],'completed')
                document=progress['report'];self.assertEqual(document['source_type'],'synthetic')
                self.assertEqual(document['checks'][0]['provenance']['generation'],2)
                retry.validate_catalog(db)
                self.assertEqual(json.loads(db.execute('SELECT job_json FROM analysis_stream_history').fetchone()[0]),old)

    def test_only_remediable_failures_and_cancellation_are_eligible(self):
        root,db,runner,sid=self.fixture()
        for code,allowed in ((None,False),('stream_free_space_unavailable',True),('stream_spool_resource_limit',True),('stream_source_changed',False),('stream_checkpoint_invalid',False),('stream_schema_invalid',False)):
            with db:db.execute("UPDATE analysis_stream_jobs SET state='failed',phase='failed',error_code=?",(code,))
            self.assertEqual(retry.eligibility(self.job(db))['eligible'],allowed)
        with db:db.execute("UPDATE analysis_stream_jobs SET state='canceled',phase='canceled',report_id=?",('a'*64,))
        with self.assertRaises(retry.RetryError):retry.enqueue(db,sid,self.payload(db))

    def test_claim_rolls_back_all_generation_rows_on_crash_then_restarts_once(self):
        root,db,runner,sid=self.fixture();before=self.job(db);retry.enqueue(db,sid,self.payload(db));original=retry._apply
        def interrupted(*args):original(*args);raise OSError('invented crash before commit')
        with patch.object(retry,'_apply',side_effect=interrupted),self.assertRaises(OSError):retry.step(db,runner)
        self.assertEqual(self.job(db),before)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_history').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_sources WHERE generation=2').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT state FROM analysis_stream_retry_requests').fetchone()[0],'pending')
        self.assertEqual(retry.step(db,runner)['result_generation'],2)
        for _ in range(3):retry.step(db,runner)
        self.assertEqual(self.job(db)['generation'],2)

    def test_equivalent_requests_deduplicate_and_conflicts_or_stale_pins_never_create_work(self):
        root,db,runner,sid=self.fixture();payload=self.payload(db)
        first=retry.enqueue(db,sid,payload)
        self.assertTrue(retry.enqueue(db,sid,payload)['idempotent'])
        second=retry.enqueue(db,sid,{**payload,'request_id':'2'*32})
        self.assertTrue(second['deduplicated']);self.assertEqual(first['request']['id'],second['request']['id'])
        with self.assertRaises(retry.RetryError):retry.enqueue(db,sid,{**payload,'expected_updated_utc_ns':'1'})
        with self.assertRaises(retry.RetryError):retry.enqueue(db,sid,{**payload,'request_id':'3'*32,'expected_generation':2})
        self.assertEqual(self.job(db)['generation'],1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_retry_requests').fetchone()[0],1)

    def test_retained_spool_charges_new_attempt_and_failure_does_not_automatically_retry(self):
        root,db,runner,sid=self.fixture();old=self.job(db);self.assertGreater(old['spool_bytes'],0)
        retry.enqueue(db,sid,self.payload(db));retry.step(db,runner)
        pages=db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
        limited=stream_analysis.StreamRunner(root,db,reducer=reports._stream_reducer('automatic-reports-8'),limits={'min_free_bytes':0,'chunk_bytes':4096,'max_spool_bytes':pages+old['spool_bytes']-1});self.addCleanup(limited.close)
        with self.assertRaises(stream_analysis.StreamError) as caught:limited._quota()
        self.assertEqual(caught.exception.code,'stream_spool_resource_limit')
        limited._failed(sid,2,'stream_spool_resource_limit')
        self.assertEqual(retry.step(db,runner)['state'],'failed')
        for _ in range(3):self.assertIsNone(retry.step(db,runner))
        self.assertEqual(self.job(db)['generation'],2)

    def test_metadata_only_schema11_upgrade_installs_empty_retry_contract(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);root=Path(temporary.name)
        db=open_catalog(root)
        with db:
            for name in reversed(tuple(retry.SCHEMA_SQL)):
                kind='TRIGGER' if name.endswith(('_update','_delete')) else 'INDEX' if name.endswith(('_pending','_run')) else 'TABLE'
                db.execute('DROP '+kind+' '+name)
            db.execute('DELETE FROM schema_migrations WHERE version=12');db.execute('PRAGMA user_version=11')
        db.close()
        with patch.object(reports,'_stream_reducer',side_effect=AssertionError('Migration must not construct evaluators')):
            with closing(open_catalog(root)) as upgraded:
                self.assertEqual(upgraded.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
                retry.validate_schema(upgraded)
                self.assertEqual(upgraded.execute('SELECT COUNT(*) FROM analysis_stream_history').fetchone()[0],0)
                self.assertEqual(upgraded.execute('SELECT COUNT(*) FROM analysis_stream_retry_requests').fetchone()[0],0)

if __name__=='__main__':unittest.main()
