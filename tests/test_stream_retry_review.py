"""Independent retry lineage, ownership, recovery and backup adversaries."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import reports,stream_analysis,stream_retry as retry
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog


class StreamRetryReviewTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='independent stream retry ');self.addCleanup(temp.cleanup)
        self.base=Path(temp.name).resolve();self.root=self.base/'archive';self.root.mkdir()
        self.db=open_catalog(self.root);self.addCleanup(lambda:self.db.close())
        source=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
        self.assertEqual(Importer(self.root,self.db).import_file(source,source_type='SYNTHETIC')['state'],'succeeded')
        full=rebuild_from_imports(self.root,self.db);document={**full,'runs':full['runs'][:1]}
        self.runner=reports.StreamReportRunner(self.root,self.db,limits={'min_free_bytes':0,'chunk_bytes':4096})
        self.addCleanup(self.runner.close)
        reports.schedule_stream_reports(self.root,self.db,document,runner=self.runner,pipeline_version='automatic-reports-11')
        self.sid=self.db.execute('SELECT stream_id FROM analysis_stream_jobs').fetchone()[0]
        for _ in range(2000):
            with patch.object(stream_analysis.time,'monotonic',return_value=0):self.runner.step(self.sid,max_rows=4)
            job=self.job()
            self.assertNotEqual(job['state'],'failed',job)
            if job['chunk_count']:break
        else:self.fail('No committed original-source chunk')
        self.runner.runners['automatic-reports-11'].cancel(self.sid)

    def job(self):return dict(self.db.execute('SELECT * FROM analysis_stream_jobs WHERE stream_id=?',(self.sid,)).fetchone())
    def payload(self,request_id='1'*32):
        return {'request_id':request_id,**retry.eligibility(self.job())['pins']}
    def enqueue(self,request_id='1'*32):return retry.enqueue(self.db,self.sid,self.payload(request_id))
    def receipt(self):return dict(self.db.execute('SELECT * FROM analysis_stream_retry_requests').fetchone())
    def old_rows(self):
        tables=('analysis_stream_sources','analysis_stream_chunks','analysis_stream_checkpoints','analysis_stream_unique','analysis_stream_reservations')
        return {table:[tuple(row) for row in self.db.execute('SELECT * FROM '+table+' WHERE stream_id=? AND generation=1 ORDER BY rowid',(self.sid,))] for table in tables}
    def claim(self):self.enqueue();return retry.step(self.db,self.runner)

    def test_close_pending_retains_exact_attempt_and_durable_queue(self):
        before=self.job();old=self.old_rows();self.enqueue()
        with patch.object(self.runner,'close',return_value=False):
            progress=retry.step(self.db,self.runner)
        self.assertEqual(progress['state'],'cleanup_pending')
        self.assertEqual(self.job(),before);self.assertEqual(self.old_rows(),old)
        self.assertEqual(self.receipt()['state'],'pending')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_history').fetchone()[0],0)

    def test_restart_claim_is_once_only_and_keeps_old_generation_exact(self):
        before=self.job();old=self.old_rows();payload=self.payload();self.enqueue()
        self.assertTrue(self.runner.close());self.db.close();self.db=open_catalog(self.root)
        runner=reports.StreamReportRunner(self.root,self.db,limits={'min_free_bytes':0,'chunk_bytes':4096});self.addCleanup(runner.close)
        receipt=retry.step(self.db,runner);self.assertEqual(receipt['state'],'running')
        fresh=self.job();self.assertEqual(fresh['generation'],2)
        for key in ('context_json','run_json','source_type','source_count','stream_id'):self.assertEqual(fresh[key],before[key])
        for key in ('verified_bytes','replay_bytes','scanned_rows','selected_rows','selected_bytes','chunk_count','spool_bytes'):self.assertEqual(fresh[key],0)
        self.assertEqual(self.old_rows(),old)
        self.assertEqual(json.loads(self.db.execute('SELECT job_json FROM analysis_stream_history').fetchone()[0]),before)
        self.assertTrue(retry.enqueue(self.db,self.sid,payload)['idempotent'])
        retry.step(self.db,runner);self.assertEqual(self.job()['generation'],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_history').fetchone()[0],1)
        retry.validate_catalog(self.db)

    def test_queued_source_pin_change_is_rejected_without_new_generation(self):
        self.enqueue();row=self.db.execute('SELECT pins_json FROM analysis_stream_sources').fetchone()[0]
        pin=json.loads(row);pin['source_type']='MANUAL_LOCAL'
        self.db.execute('UPDATE analysis_stream_sources SET pins_json=?',(json.dumps(pin,sort_keys=True,separators=(',',':')),));self.db.commit()
        receipt=retry.step(self.db,self.runner)
        self.assertEqual(receipt['state'],'superseded');self.assertEqual(self.job()['generation'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_history').fetchone()[0],0)

    def test_retry_reads_changed_original_from_zero_and_never_publishes(self):
        before=self.job();self.enqueue()
        pin=json.loads(self.db.execute('SELECT pins_json FROM analysis_stream_sources').fetchone()[0]);source=self.root/pin['files']['raw']['path']
        raw=source.read_bytes();source.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        retry.step(self.db,self.runner)
        for _ in range(2000):
            with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=self.runner.step(self.sid,max_rows=4)
            if progress['state']=='failed':break
        else:self.fail('Changed original accepted')
        self.assertEqual(self.job()['generation'],2);self.assertIsNone(self.job()['report_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
        self.assertEqual(json.loads(self.db.execute('SELECT job_json FROM analysis_stream_history').fetchone()[0]),before)
        retry.step(self.db,self.runner);self.assertEqual(self.receipt()['state'],'failed')

    def test_forged_completed_receipt_cannot_pass_catalog_validation(self):
        self.enqueue()
        self.db.execute("UPDATE analysis_stream_retry_requests SET state='completed'");self.db.commit()
        with self.assertRaises(ValueError):retry.validate_catalog(self.db)

    def test_history_is_immutable_and_rehashed_wrong_snapshot_is_rejected(self):
        self.claim()
        with self.assertRaisesRegex(Exception,'stream_history_immutable'):
            self.db.execute("UPDATE analysis_stream_history SET phase='forged'")
        self.db.rollback()
        self.db.execute('DROP TRIGGER analysis_stream_history_update')
        job=json.loads(self.db.execute('SELECT job_json FROM analysis_stream_history').fetchone()[0]);job['context_json']=job['context_json'].replace('synthetic','simulation')
        raw=json.dumps(job,sort_keys=True,separators=(',',':'))
        self.db.execute('UPDATE analysis_stream_history SET context_json=?,job_json=?,job_sha256=?',(job['context_json'],raw,hashlib.sha256(raw.encode()).hexdigest()))
        self.db.execute(retry.SCHEMA_SQL['analysis_stream_history_update']);self.db.commit()
        with self.assertRaises(ValueError):retry.validate_catalog(self.db)

    def test_coordinated_history_and_receipt_rehash_cannot_invent_committed_chunks(self):
        self.claim();history=dict(self.db.execute('SELECT * FROM analysis_stream_history').fetchone())
        job=json.loads(history['job_json']);job['chunk_count']+=1
        raw=json.dumps(job,sort_keys=True,separators=(',',':'))
        sha=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.db.execute('DROP TRIGGER analysis_stream_history_update')
        self.db.execute('UPDATE analysis_stream_history SET chunk_count=?,job_json=?,job_sha256=?',
                        (job['chunk_count'],raw,hashlib.sha256(raw.encode()).hexdigest()))
        self.db.execute('UPDATE analysis_stream_retry_requests SET attempt_sha256=?',(sha([job,history['pins_sha256']]),))
        self.db.execute(retry.SCHEMA_SQL['analysis_stream_history_update']);self.db.commit()
        with self.assertRaises(ValueError):retry.validate_catalog(self.db)

    def historical_partial(self):
        relative=f'analysis/streams/{self.sid}/g1/uncommitted.partial';data=b'incomplete retained evidence\x00\xff'
        (self.root/relative).write_bytes(data);reserved=len(data)+100
        self.db.execute('INSERT INTO analysis_stream_reservations VALUES(?,?,?,?)',(self.sid,1,relative,reserved))
        self.db.execute('UPDATE analysis_stream_jobs SET spool_bytes=spool_bytes+? WHERE stream_id=?',(reserved,self.sid));self.db.commit()
        return relative,data

    def test_backup_restore_preserves_old_chunks_markers_partial_and_running_receipt(self):
        from robot_test_hub.backup import Backup,restore,verify
        partial,data=self.historical_partial();old=self.old_rows();self.claim()
        required={row['path']:(self.root/row['path']).read_bytes() for row in self.db.execute('SELECT path FROM analysis_stream_chunks WHERE generation=1')}
        marker=f'analysis/streams/{self.sid}/g1/.owner';required[marker]=(self.root/marker).read_bytes();required[partial]=data
        destination=self.base/'backups';Backup(self.root,destination).create('review-retained')
        generation=destination/'review-retained';self.assertEqual(verify(generation)['state'],'verified')
        restored=self.base/'restored';restore(generation,restored)
        for relative,expected in required.items():self.assertEqual((restored/relative).read_bytes(),expected)
        with closing(open_catalog(restored)) as db:
            retry.validate_catalog(db)
            self.assertEqual(db.execute('SELECT state FROM analysis_stream_retry_requests').fetchone()[0],'running')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_history').fetchone()[0],1)
            runner=reports.StreamReportRunner(restored,db,limits={'min_free_bytes':0,'chunk_bytes':4096})
            try:
                retry.step(db,runner)
                self.assertEqual(db.execute('SELECT generation FROM analysis_stream_jobs').fetchone()[0],2)
                for _ in range(2000):
                    with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=reports.advance_streams(restored,db,runner)
                    self.assertNotEqual(progress.get('state'),'failed',progress)
                    if progress.get('report') is not None:break
                else:self.fail('Restored fresh attempt did not complete')
                retry.step(db,runner)
                self.assertEqual(db.execute('SELECT state FROM analysis_stream_retry_requests').fetchone()[0],'completed')
                retry.validate_catalog(db)
            finally:self.assertTrue(runner.close())
        self.assertEqual(self.old_rows(),old)

    def test_backup_rejects_historical_partial_larger_than_its_reservation(self):
        from robot_test_hub.backup import Backup,BackupError
        relative,data=self.historical_partial();self.claim()
        (self.root/relative).write_bytes(data+b'x'*101)
        with self.assertRaisesRegex(BackupError,'Historical reserved evidence exceeds its allowance'):
            Backup(self.root,self.base/'backups').create('review-oversize')

    def test_old_generation_storage_charge_survives_reset_current_spool(self):
        from robot_test_hub.stream_quality import RecordingQualityReducer
        self.historical_partial();old_spool=self.job()['spool_bytes'];self.assertGreater(old_spool,0)
        self.claim();self.assertEqual(self.job()['spool_bytes'],0)
        pages=self.db.execute('PRAGMA page_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]
        runner=stream_analysis.StreamRunner(self.root,self.db,reducer=RecordingQualityReducer(),limits={'min_free_bytes':0,'max_spool_bytes':pages+old_spool-1})
        try:
            with self.assertRaisesRegex(stream_analysis.StreamError,'stream_spool_resource_limit'):runner._quota()
        finally:self.assertTrue(runner.close())

    def test_legacy_generation_seven_can_start_a_contiguous_retry_lineage(self):
        self.assertTrue(self.runner.close())
        for table in self.old_rows():self.db.execute('UPDATE '+table+' SET generation=7 WHERE stream_id=?',(self.sid,))
        for table in ('analysis_stream_chunks','analysis_stream_reservations'):
            self.db.execute("UPDATE "+table+" SET path=replace(path,'/g1/','/g7/') WHERE stream_id=?",(self.sid,))
        old_directory=self.root/f'analysis/streams/{self.sid}/g1';new_directory=old_directory.with_name('g7');old_directory.rename(new_directory)
        (new_directory/'.owner').write_text(json.dumps({'stream_id':self.sid,'generation':7},sort_keys=True,separators=(',',':')),encoding='utf-8')
        self.db.execute('UPDATE analysis_stream_jobs SET generation=7 WHERE stream_id=?',(self.sid,));self.db.commit()
        self.claim();self.assertEqual(self.job()['generation'],8)
        retry.validate_catalog(self.db)


if __name__=='__main__':unittest.main()
