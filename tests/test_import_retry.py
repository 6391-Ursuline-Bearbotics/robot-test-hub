"""Exact-attempt local retries retain immutable originals and durable audit."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from robot_test_hub import import_retry as retry
from robot_test_hub.importer import Importer
from robot_test_hub.storage import open_catalog, SCHEMA_VERSION, SchemaError
from test_wpilog import FIXTURES


class ImportRetryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='retry local ')
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.db=open_catalog(self.root)
        self.addCleanup(lambda:self.db.close())
        self.importer=Importer(self.root,self.db)
        with patch('robot_test_hub.importer.extract',side_effect=OSError('private source name')):
            self.job=self.importer.import_file(FIXTURES/'alpha7-main.wpilog',source_type='SYNTHETIC')
        self.assertEqual(self.job['state'],'failed')
        self.payload=dict(request_id=uuid.uuid4().hex,**retry.eligibility(self.job)['pins'])

    def queue(self,payload=None):
        return retry.enqueue(self.db,self.job['id'],payload or self.payload)

    def audit(self):
        return dict(self.db.execute('SELECT * FROM import_retry_requests WHERE id=?',(self.payload['request_id'],)).fetchone())

    def test_enqueue_is_metadata_only_and_deduplicated_across_restart(self):
        with patch('robot_test_hub.importer._digest',side_effect=AssertionError('no hashing')),patch('robot_test_hub.importer.extract',side_effect=AssertionError('no decoding')):
            first=self.queue()
            self.assertEqual(first['request']['state'],'pending')
            self.assertEqual(self.importer.get_job(self.job['id']),self.job)
            self.assertTrue(self.queue()['idempotent'])
            second=self.queue(dict(self.payload,request_id=uuid.uuid4().hex))
            self.assertTrue(second['deduplicated'])
            self.assertEqual(second['request']['id'],first['request']['id'])
        self.db.close();self.db=open_catalog(self.root)
        self.assertEqual(self.queue()['request']['id'],first['request']['id'])
        bad=dict(self.payload,source_sha256='a'*64)
        with self.assertRaises(retry.RetryError) as cm:self.queue(bad)
        self.assertEqual(cm.exception.status,409)

    def test_success_keeps_original_provenance_and_exact_audit(self):
        raw=next((self.root/'raw').rglob('*.wpilog'))
        before=raw.read_bytes()
        requests=[tuple(row) for row in self.db.execute('SELECT * FROM import_requests')]
        artifact=tuple(self.db.execute('SELECT * FROM import_artifacts').fetchone())
        self.queue();retry.step(self.root,self.db)
        job=self.importer.get_job(self.job['id'])
        self.assertEqual((job['state'],job['attempts']),('succeeded',self.job['attempts']+1))
        audit=self.audit()
        self.assertEqual(audit['state'],'completed')
        self.assertEqual(audit['result_updated_utc_ns'],str(job['updated_utc_ns']))
        self.assertEqual(audit['result_attempts'],job['attempts'])
        self.assertEqual(raw.read_bytes(),before)
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM import_requests')],requests)
        after=self.db.execute('SELECT * FROM import_artifacts').fetchone()
        # Only format_state changes from unchecked to validated.
        self.assertEqual(after['source_type'],'SYNTHETIC')
        self.assertEqual(after['relative_path'],artifact[2])
        self.assertEqual(len(list((self.root/'raw').rglob('*.wpilog'))),1)
        self.assertTrue(self.queue()['idempotent'])
        self.assertIsNone(retry.step(self.root,self.db))

    def test_restart_after_claim_or_mid_decode_resumes_same_attempt(self):
        self.queue()
        with patch.object(Importer,'retry_existing',side_effect=SystemExit):
            with self.assertRaises(SystemExit):retry.step(self.root,self.db)
        claimed=self.importer.get_job(self.job['id'])
        self.assertEqual(self.audit()['state'],'running')
        self.db.close();self.db=open_catalog(self.root);self.importer=Importer(self.root,self.db)
        with patch('robot_test_hub.importer.extract',side_effect=SystemExit):
            with self.assertRaises(SystemExit):retry.step(self.root,self.db)
        self.assertEqual(self.importer.get_job(self.job['id'])['attempts'],claimed['attempts'])
        retry.step(self.root,self.db)
        self.assertEqual(self.audit()['state'],'completed')
        self.assertEqual(self.importer.get_job(self.job['id'])['attempts'],claimed['attempts'])

    def test_pending_or_running_changed_attempt_never_retargets(self):
        for running in (False,True):
            with self.subTest(running=running):
                self.db.execute('DELETE FROM import_retry_requests');self.db.commit()
                with self.db:self.db.execute("UPDATE import_jobs SET state='failed',attempts=?,updated_utc_ns=?,error_code='import_io_or_integrity' WHERE id=?",(self.job['attempts'],self.job['updated_utc_ns'],self.job['id']))
                self.queue()
                if running:
                    with patch.object(Importer,'retry_existing',side_effect=SystemExit):
                        with self.assertRaises(SystemExit):retry.step(self.root,self.db)
                with self.db:self.db.execute('UPDATE import_jobs SET updated_utc_ns=updated_utc_ns+1 WHERE id=?',(self.job['id'],))
                with patch('robot_test_hub.importer.extract',side_effect=AssertionError('no decode')):retry.step(self.root,self.db)
                self.assertEqual(self.audit()['state'],'superseded')

    def test_missing_changed_or_redirected_raw_fails_without_rearchive(self):
        raw=next((self.root/'raw').rglob('*.wpilog'));original=raw.read_bytes()
        for kind in ('missing','changed','redirected'):
            with self.subTest(kind=kind):
                self.db.execute('DELETE FROM import_retry_requests');self.db.commit()
                with self.db:
                    self.db.execute("UPDATE import_jobs SET state='failed',attempts=?,updated_utc_ns=?,error_code='import_io_or_integrity' WHERE id=?",(self.job['attempts'],self.job['updated_utc_ns'],self.job['id']))
                    self.db.execute('UPDATE import_artifacts SET relative_path=?',(str(raw.relative_to(self.root)).replace(chr(92),'/'),))
                raw.write_bytes(original)
                if kind=='missing':raw.unlink()
                elif kind=='changed':raw.write_bytes(b'changed')
                else:
                    with self.db:self.db.execute("UPDATE import_artifacts SET relative_path='../private.wpilog'")
                self.queue()
                with patch.object(Importer,'_archive',side_effect=AssertionError('no rearchive')),patch('robot_test_hub.importer.extract',side_effect=AssertionError('no decode')):
                    retry.step(self.root,self.db)
                self.assertEqual(self.audit()['state'],'failed')
                self.assertEqual(self.audit()['error_code'],'import_io_or_integrity')
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM import_requests').fetchone()[0],1)

    def test_strict_pins_and_ineligible_states(self):
        for key,value in (('expected_updated_utc_ns',float(self.job['updated_utc_ns'])),('expected_updated_utc_ns','01'),('expected_attempts',True),('extra','x')):
            with self.subTest(key=key,value=value):
                bad=dict(self.payload);bad[key]=value
                with self.assertRaises(retry.RetryError) as cm:self.queue(bad)
                self.assertEqual(cm.exception.status,400)
        for state,code in (('invalid','invalid_format'),('unsupported','unsupported_schema'),('checksum_mismatch','checksum_mismatch'),('succeeded',None),('running',None),('failed','resource_limit')):
            with self.subTest(state=state,code=code):
                with self.db:self.db.execute('UPDATE import_jobs SET state=?,error_code=? WHERE id=?',(state,code,self.job['id']))
                with self.assertRaises(retry.RetryError) as cm:self.queue()
                self.assertEqual(cm.exception.status,409)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM import_retry_requests').fetchone()[0],0)

    def test_stale_finish_does_not_overwrite_concurrent_job(self):
        from robot_test_hub.importer import extract
        def changed(*args):
            with self.db:self.db.execute("UPDATE import_jobs SET mapping_revision='future' WHERE id=?",(self.job['id'],))
            return extract(*args)
        self.queue()
        with patch('robot_test_hub.importer.extract',side_effect=changed):retry.step(self.root,self.db)
        self.assertEqual(self.audit()['state'],'superseded')
        job=self.importer.get_job(self.job['id'])
        self.assertEqual((job['state'],job['mapping_revision']),('running','future'))
        self.assertIsNone(job['dataset_path'])

    def test_source_change_before_raw_validation_cannot_redirect_io(self):
        self.queue()
        original=Importer._registered_raw
        expected=self.job['artifact_sha256']
        with self.db:self.db.execute("INSERT INTO import_artifacts(sha256,size_bytes,relative_path,source_type,created_utc_ns) VALUES(?,0,'raw/replacement.wpilog','SYNTHETIC',1)",('a'*64,))
        seen=[]
        def changed(importer,job_id,*,expected_sha256):
            seen.append(expected_sha256)
            with self.db:self.db.execute("UPDATE import_jobs SET artifact_sha256=? WHERE id=?",('a'*64,job_id))
            return original(importer,job_id,expected_sha256=expected_sha256)
        with patch.object(Importer,'_registered_raw',changed),patch('robot_test_hub.importer._digest',side_effect=AssertionError('no raw hash')),patch('robot_test_hub.importer.extract',side_effect=AssertionError('no extraction')),patch.object(Path,'lstat',side_effect=AssertionError('no raw path access')):
            retry.step(self.root,self.db)
        self.assertEqual(seen,[expected])
        self.assertEqual(self.audit()['state'],'superseded')
        self.assertIsNone(self.importer.get_job(self.job['id'])['dataset_path'])

    def test_receipt_storage_corruption_bounded_and_redacted(self):
        self.queue()
        for column,value in (('profile','C:/private/name'),('source_sha256','private'),('result_state','C:/private'),('result_attempts',-1),('result_updated_utc_ns','1.0'),('mapping_revision','X'*1000000)):
            with self.subTest(column=column):
                original=self.audit()[column]
                with self.db:self.db.execute('UPDATE import_retry_requests SET '+column+'=? WHERE id=?',(value,self.payload['request_id']))
                with self.assertRaises(retry.RetryError) as cm:self.queue()
                self.assertEqual((cm.exception.status,str(cm.exception)),(503,'import_retry_storage_unavailable'))
                with self.db:self.db.execute('UPDATE import_retry_requests SET '+column+'=? WHERE id=?',(original,self.payload['request_id']))

    def test_job_and_audit_completion_rollback_together(self):
        self.queue()
        with patch('robot_test_hub.import_retry.complete',side_effect=SystemExit):
            with self.assertRaises(SystemExit):retry.step(self.root,self.db)
        current=self.importer.get_job(self.job['id'])
        self.assertEqual((current['state'],self.audit()['state']),('running','running'))
        self.assertIsNone(current['dataset_path'])
        claimed_attempt=current['attempts']
        retry.step(self.root,self.db)
        self.assertEqual(self.audit()['state'],'completed')
        self.assertEqual(self.importer.get_job(self.job['id'])['attempts'],claimed_attempt)

    def test_backup_pending_and_running_audit_resume_without_new_attempt(self):
        from robot_test_hub.backup import Backup,restore,verify
        self.queue()
        for state in ('pending','running'):
            if state=='running':
                with patch.object(Importer,'retry_existing',side_effect=SystemExit):
                    with self.assertRaises(SystemExit):retry.step(self.root,self.db)
            parent=self.root.parent
            destination=parent/(self.root.name+' backup '+state)
            recovered=parent/(self.root.name+' restored '+state)
            self.addCleanup(lambda p=destination:__import__('shutil').rmtree(p,ignore_errors=True))
            self.addCleanup(lambda p=recovered:__import__('shutil').rmtree(p,ignore_errors=True))
            Backup(self.root,destination).create(state)
            verify(destination/state)
            restore(destination/state,recovered)
            with closing(open_catalog(recovered)) as db:
                audit=dict(db.execute('SELECT * FROM import_retry_requests').fetchone())
                self.assertEqual(audit,self.audit())
                retry.step(recovered,db)
                result=dict(db.execute('SELECT * FROM import_retry_requests').fetchone())
                self.assertEqual((result['state'],result['result_attempts']),('completed',self.job['attempts']+1))
                retry.validate_schema(db)

    def test_schema_upgrade_and_schema_validation(self):
        self.db.close()
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as db,db:
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'analysis_stream_%'").fetchall():db.execute('DROP TABLE "'+name+'"')
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'run_lookup_%'").fetchall():db.execute('DROP TRIGGER "'+name+'"')
            for table in ('run_lookup_intervals','run_lookup_runs','run_lookup_jobs','run_lookup_state'):db.execute('DROP TABLE IF EXISTS '+table)
            db.execute('DELETE FROM schema_migrations WHERE version>=9')
            db.execute('DROP TABLE import_retry_requests')
            db.execute('DELETE FROM schema_migrations WHERE version=8')
            db.execute('PRAGMA user_version=7')
        self.db=open_catalog(self.root)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        retry.validate_schema(self.db)
        self.assertEqual(dict(self.db.execute('SELECT * FROM import_jobs WHERE id=?',(self.job['id'],)).fetchone()),self.job)
        with self.db:
            self.db.execute('DROP INDEX import_retry_job')
            self.db.execute('CREATE INDEX import_retry_job ON import_retry_requests(state)')
        self.db.close()
        with self.assertRaises(SchemaError):open_catalog(self.root)
        self.db=sqlite3.connect(self.root/'catalog.sqlite3')


if __name__=='__main__':unittest.main()
