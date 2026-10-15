"""Independent exact-attempt retry, durability and immutable-original checks."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import import_retry as retry
from robot_test_hub.importer import Importer
from robot_test_hub.storage import open_catalog


FIXTURE=Path(__file__).resolve().parent/'fixtures'/'synthetic'/'alpha7-main.wpilog'


class ImportRetryIndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name).resolve()
        self.db=open_catalog(self.root)
        self.addCleanup(lambda:self.db.close())
        self.importer=Importer(self.root,self.db)
        with patch('robot_test_hub.importer.extract',side_effect=OSError('private original credential path')):
            self.job=self.importer.import_file(FIXTURE,source_type='SYNTHETIC')
        self.assertEqual((self.job['state'],self.job['error_code']),('failed','import_io_or_integrity'))
        self.raw=self.root/self.db.execute('SELECT relative_path FROM import_artifacts').fetchone()[0]
        self.original=self.raw.read_bytes()
        self.pins=retry.eligibility(self.job)['pins']
        self.payload=dict(request_id='1'*32,**self.pins)

    def enqueue(self,payload=None):
        return retry.enqueue(self.db,self.job['id'],payload or self.payload)

    def request(self):
        row=self.db.execute('SELECT * FROM import_retry_requests WHERE id=?',(self.payload['request_id'],)).fetchone()
        return dict(row) if row else None

    def test_enqueue_dedupes_without_job_or_local_source_work_and_survives_lost_response(self):
        before=dict(self.importer.get_job(self.job['id']))
        claims=[tuple(row) for row in self.db.execute('SELECT * FROM import_requests')]
        with patch('robot_test_hub.importer._digest',side_effect=AssertionError('HTTP hashed original')), \
             patch('robot_test_hub.importer.extract',side_effect=AssertionError('HTTP extracted original')), \
             patch.object(Importer,'retry_existing',side_effect=AssertionError('HTTP invoked worker')):
            first=self.enqueue()
            duplicate=self.enqueue()
            other=self.enqueue(dict(self.payload,request_id='2'*32))
        self.assertEqual(self.importer.get_job(self.job['id']),before)
        self.assertFalse(first['idempotent'])
        self.assertTrue(duplicate['idempotent'])
        self.assertTrue(other['deduplicated'])
        self.assertEqual(other['request']['id'],self.payload['request_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM import_retry_requests').fetchone()[0],1)
        retry.step(self.root,self.db)
        done=self.importer.get_job(self.job['id'])
        self.assertIn(done['state'],('succeeded','succeeded_with_unsupported'))
        self.assertEqual(done['attempts'],before['attempts']+1)
        self.assertEqual(self.request()['state'],'completed')
        # A replay after worker completion must return its original audit,
        # rather than reject old pins or launch a new attempt.
        replay=self.enqueue()
        self.assertTrue(replay['idempotent'])
        self.assertEqual(replay['request']['state'],'completed')
        self.assertIsNone(retry.step(self.root,self.db))
        self.assertEqual(self.importer.get_job(self.job['id'])['attempts'],done['attempts'])
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM import_requests')],claims)
        self.assertEqual(self.raw.read_bytes(),self.original)

    def test_request_id_cannot_be_reused_with_other_attempt_pins(self):
        self.enqueue()
        before=self.request()
        for changes in ({'expected_attempts':self.pins['expected_attempts']+1},
                        {'expected_updated_utc_ns':str(int(self.pins['expected_updated_utc_ns'])+1)},
                        {'source_sha256':'b'*64}):
            with self.subTest(changes=changes),self.assertRaises(retry.RetryError) as conflict:
                self.enqueue(dict(self.payload,**changes))
            self.assertEqual(conflict.exception.status,409)
            self.assertEqual(self.request(),before)

    def test_noneligible_states_codes_and_versions_never_create_audit_or_reset_job(self):
        cases=[{'state':'invalid','error_code':'invalid_format'},
               {'state':'unsupported','error_code':'unsupported_profile'},
               {'state':'checksum_mismatch','error_code':'checksum_mismatch'},
               {'state':'succeeded','error_code':None},{'state':'running','error_code':None},
               {'state':'pending','error_code':None},{'error_code':'resource_limit'},
               {'profile':'unqualified-profile'},{'mapping_revision':'old-mapping'},
               {'extractor_version':'old-reader'}]
        original=dict(self.job)
        for change in cases:
            job=dict(original,**change)
            keys=('state','error_code','profile','mapping_revision','extractor_version')
            self.db.execute('UPDATE import_jobs SET '+','.join(key+'=?' for key in keys)+' WHERE id=?',
                            (*(job[key] for key in keys),job['id']))
            self.db.commit()
            before=self.importer.get_job(job['id'])
            pins=retry.eligibility(before)['pins']
            self.assertFalse(retry.eligibility(before)['eligible'])
            with self.subTest(change=change),self.assertRaises(retry.RetryError) as rejected:
                self.enqueue(dict(request_id='1'*32,**pins))
            self.assertEqual(rejected.exception.status,409)
            self.assertEqual(self.importer.get_job(job['id']),before)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM import_retry_requests').fetchone()[0],0)

    def test_pending_request_superseded_by_new_attempt_without_import_io(self):
        self.enqueue()
        self.db.execute('UPDATE import_jobs SET attempts=attempts+1,updated_utc_ns=updated_utc_ns+1 WHERE id=?',(self.job['id'],))
        self.db.commit()
        before=self.importer.get_job(self.job['id'])
        with patch.object(Importer,'retry_existing',side_effect=AssertionError('Retargeted request')):
            retry.step(self.root,self.db)
        self.assertEqual(self.request()['state'],'superseded')
        self.assertEqual(self.importer.get_job(self.job['id']),before)
        self.assertEqual(self.raw.read_bytes(),self.original)

    def test_new_attempt_between_claim_and_execution_is_not_retargeted(self):
        self.enqueue()
        actual=Importer.retry_existing
        def concurrent(importer,job_id,**claim):
            self.db.execute("UPDATE import_jobs SET state='failed',attempts=attempts+1,error_code='extractor_failure',updated_utc_ns=updated_utc_ns+1 WHERE id=?",(job_id,))
            self.db.commit()
            return actual(importer,job_id,**claim)
        with patch.object(Importer,'retry_existing',new=concurrent), \
             patch('robot_test_hub.importer.extract',side_effect=AssertionError('Retargeted newer attempt')):
            retry.step(self.root,self.db)
        self.assertEqual(self.request()['state'],'superseded')
        self.assertEqual(self.importer.get_job(self.job['id'])['attempts'],self.job['attempts']+2)
        self.assertEqual(self.raw.read_bytes(),self.original)

    def test_restart_after_claim_resumes_owned_attempt_without_increment(self):
        self.enqueue()
        with patch.object(Importer,'retry_existing',side_effect=SystemExit('synthetic process loss')):
            with self.assertRaises(SystemExit):
                retry.step(self.root,self.db)
        claimed=self.importer.get_job(self.job['id'])
        self.assertEqual(claimed['state'],'running')
        self.assertEqual(self.request()['state'],'running')
        self.db.close()
        self.db=open_catalog(self.root)
        self.importer=Importer(self.root,self.db)
        retry.step(self.root,self.db)
        finished=self.importer.get_job(self.job['id'])
        self.assertEqual(finished['attempts'],claimed['attempts'])
        self.assertIn(finished['state'],('succeeded','succeeded_with_unsupported'))
        self.assertEqual(self.request()['state'],'completed')

    def test_restart_after_derivative_publication_reuses_exact_immutable_outputs(self):
        self.enqueue()
        with patch.object(Importer,'_finish',side_effect=SystemExit('synthetic loss before outcome commit')):
            with self.assertRaises(SystemExit):
                retry.step(self.root,self.db)
        published={path.relative_to(self.root):path.read_bytes() for path in (self.root/'derived').rglob('*') if path.is_file()}
        self.assertTrue(published)
        claimed=self.importer.get_job(self.job['id'])
        self.assertEqual(claimed['state'],'running')
        self.db.close()
        self.db=open_catalog(self.root)
        self.importer=Importer(self.root,self.db)
        retry.step(self.root,self.db)
        finished=self.importer.get_job(self.job['id'])
        self.assertEqual(finished['attempts'],claimed['attempts'])
        self.assertEqual(self.request()['state'],'completed')
        self.assertEqual(published,{path.relative_to(self.root):path.read_bytes() for path in (self.root/'derived').rglob('*') if path.is_file()})

    def test_tampered_raw_fails_exact_request_without_creating_new_source_or_provenance(self):
        self.enqueue()
        changed=self.original+b'tampered fixture bytes'
        self.raw.write_bytes(changed)
        artifacts=[tuple(row) for row in self.db.execute('SELECT * FROM import_artifacts')]
        claims=[tuple(row) for row in self.db.execute('SELECT * FROM import_requests')]
        retry.step(self.root,self.db)
        job=self.importer.get_job(self.job['id'])
        self.assertEqual((job['state'],job['error_code']),('failed','import_io_or_integrity'))
        self.assertEqual(job['artifact_sha256'],self.job['artifact_sha256'])
        self.assertEqual(self.request()['state'],'failed')
        self.assertEqual(self.raw.read_bytes(),changed)
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM import_artifacts')],artifacts)
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM import_requests')],claims)

    def test_escaping_registered_path_fails_without_reading_outside_or_poisoning_next_request(self):
        self.enqueue()
        outside=self.root.parent/(self.root.name+'-outside.wpilog')
        outside.write_bytes(b'private unrelated bytes')
        self.addCleanup(lambda:outside.unlink(missing_ok=True))
        self.db.execute('UPDATE import_artifacts SET relative_path=? WHERE sha256=?',('../'+outside.name,self.job['artifact_sha256']))
        self.db.commit()
        with patch('robot_test_hub.importer._digest',side_effect=AssertionError('Read escaping raw path')):
            retry.step(self.root,self.db)
        self.assertEqual(self.request()['state'],'failed')
        self.assertEqual(self.importer.get_job(self.job['id'])['error_code'],'import_io_or_integrity')
        self.assertEqual(outside.read_bytes(),b'private unrelated bytes')
        self.assertEqual(self.raw.read_bytes(),self.original)
        self.assertNotIn('private',json.dumps(retry.project_request(self.request())))

    def test_malformed_receipt_fields_never_project_private_or_unbounded_values(self):
        self.enqueue()
        original=self.request()
        for key,value in (('profile','C:/private/credentials'),('source_sha256','private-secret'),
                          ('result_state','private filesystem traceback'),('expected_attempts',True),
                          ('result_updated_utc_ns','private time path')):
            with self.subTest(key=key),self.assertRaises(ValueError) as invalid:
                retry.project_request(dict(original,**{key:value}))
            self.assertNotIn('private',str(invalid.exception))

    def test_enqueue_respects_caller_transaction_rollback(self):
        self.db.execute('BEGIN IMMEDIATE')
        self.enqueue()
        self.assertTrue(self.db.in_transaction)
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as reader:
            self.assertEqual(reader.execute('SELECT COUNT(*) FROM import_retry_requests').fetchone()[0],0)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM import_retry_requests').fetchone()[0],0)
    def test_one_failed_original_does_not_block_next_request_and_only_one_runs_per_step(self):
        self.enqueue()
        with patch('robot_test_hub.importer.extract',side_effect=OSError('synthetic initial reader failure')):
            second=self.importer.import_file(FIXTURE.with_name('alpha7-boot-b.wpilog'),source_type='SYNTHETIC')
        second_payload=dict(request_id='2'*32,**retry.eligibility(second)['pins'])
        retry.enqueue(self.db,second['id'],second_payload)
        self.raw.unlink()
        retry.step(self.root,self.db)
        self.assertEqual(self.request()['state'],'failed')
        queued=self.db.execute('SELECT state FROM import_retry_requests WHERE id=?',('2'*32,)).fetchone()[0]
        self.assertEqual(queued,'pending')
        self.assertEqual(self.importer.get_job(second['id'])['attempts'],second['attempts'])
        retry.step(self.root,self.db)
        outcome=self.db.execute('SELECT state FROM import_retry_requests WHERE id=?',('2'*32,)).fetchone()[0]
        self.assertEqual(outcome,'completed')
        self.assertIn(self.importer.get_job(second['id'])['state'],('succeeded','succeeded_with_unsupported'))
        self.assertEqual(self.importer.get_job(second['id'])['attempts'],second['attempts']+1)
        self.assertFalse(self.raw.exists())
    def test_source_substitution_after_claim_is_superseded_before_raw_or_extraction_io(self):
        replacement_sha,_,_,_=self.importer._archive(FIXTURE.with_name('alpha7-boot-b.wpilog'),'SYNTHETIC',None)
        self.assertNotEqual(replacement_sha,self.job['artifact_sha256'])
        self.enqueue()
        from robot_test_hub.importer import _digest
        actual=Importer.retry_existing
        def substitute(importer,job_id,**claim):
            # Preserve attempt and updated timestamp so this specifically tests
            # source identity binding, independently of normal attempt CAS.
            self.db.execute('UPDATE import_jobs SET artifact_sha256=? WHERE id=?',(replacement_sha,job_id))
            self.db.commit()
            return actual(importer,job_id,**claim)
        with patch.object(Importer,'retry_existing',new=substitute), \
             patch('robot_test_hub.importer._digest',wraps=_digest) as digest, \
             patch('robot_test_hub.importer.extract',side_effect=AssertionError('Decoded substituted source')) as extract:
            retry.step(self.root,self.db)
        extract.assert_not_called()
        digest.assert_not_called()
        self.assertEqual(self.request()['state'],'superseded')
        self.assertEqual(self.raw.read_bytes(),self.original)