"""Maintained lookup invariants using private synthetic SQLite report rows."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import report_lookup as lookup
from robot_test_hub.analysis import install_schema,Runner,Analyzer,Declaration,result
from test_analysis import evidence,PROFILE


class ReportLookupTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='lookup private ')
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name).resolve()/'catalog.sqlite3'
        self.db=sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA journal_mode=WAL')
        install_schema(self.db);self.db.commit()

    def install(self):
        with lookup.atomic(self.db):lookup.install_schema(self.db)
        lookup.validate_schema(self.db)

    def body(self,identity='report-1',sha='a'*64,run='run-1',trace=None):
        return json.dumps(dict(report_id=identity,run_id=run,checks=[dict(provenance=dict(source_hashes=[sha]),coverage=dict(trace=trace or []))]))

    def insert(self,identity='report-1',sha='a'*64,run='run-1',raw=None,replace=False):
        raw=self.body(identity,sha,run) if raw is None else raw
        self.db.execute(('INSERT OR REPLACE' if replace else 'INSERT')+' INTO analysis_reports VALUES(?,?,?,?)',(identity,'{}',raw,'1'))
        self.db.commit();return raw

    def refs(self):return self.db.execute(lookup.REFERENCES_SQL+' ORDER BY report_id,sha').fetchall()

    def test_schema_install_is_transactional_and_does_not_read_legacy_bodies(self):
        self.insert(raw='not JSON private payload')
        reads=[]
        def authorize(action,table,column,*args):
            if action==sqlite3.SQLITE_READ and table=='analysis_reports' and column=='result_json':
                reads.append(column);return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(authorize)
        try:self.install()
        finally:self.db.set_authorizer(lambda *args:sqlite3.SQLITE_OK)
        self.assertEqual(reads,[])
        self.assertFalse(lookup.coverage(self.db)['discovery_complete'])
        self.assertFalse(lookup.coverage(self.db)['complete'])
        self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports').fetchone()[0],'not JSON private payload')

    def test_schema_rollback_has_no_implicit_commit(self):
        self.db.execute('BEGIN')
        lookup.install_schema(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertFalse(lookup.has_schema(self.db))

    def test_inline_runner_report_and_jobs_rollback_with_outer_caller(self):
        self.install();runner=Runner(self.db)
        analyzer=Analyzer(Declaration('fixture','1',(PROFILE,)),lambda e,c:result('evaluated_no_finding'))
        self.db.execute('BEGIN')
        document=runner.report([analyzer],evidence())
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.refs()[0][0],'a'*64)
        self.assertEqual(self.refs()[0][1],document['report_id'])
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analyzer_jobs').fetchone()[0],0)
        self.assertEqual(self.refs(),[])
        self.assertTrue(lookup.coverage(self.db)['complete'])

    def test_runner_inline_receipt_hashes_actual_stored_canonical_bytes(self):
        self.install();runner=Runner(self.db)
        analyzer=Analyzer(Declaration('fixture','1',(PROFILE,)),lambda e,c:result('evaluated_no_finding'))
        document=runner.report([analyzer],evidence())
        raw=self.db.execute('SELECT result_json FROM analysis_reports').fetchone()[0]
        self.assertEqual(self.refs()[0][3],hashlib.sha256(raw.encode()).hexdigest())
        self.assertTrue(lookup.coverage(self.db)['complete'])
        with patch.object(lookup,'_read_report',side_effect=AssertionError('ready receipt rehashed')):
            self.assertEqual(runner.report([analyzer],evidence()),document)

    def test_existing_ignored_report_indexes_stored_body_not_fresh_document(self):
        self.install();runner=Runner(self.db)
        analyzer=Analyzer(Declaration('fixture','1',(PROFILE,)),lambda e,c:result('evaluated_no_finding'))
        document=runner.report([analyzer],evidence())
        replacement=self.body(document['report_id'],'b'*64,'retained-run')
        self.db.execute('UPDATE analysis_reports SET result_json=?',(replacement,));self.db.commit()
        runner.report([analyzer],evidence())
        self.assertEqual(self.refs()[0][0],'b'*64)
        self.assertEqual(self.refs()[0][2],'retained-run')
        self.assertEqual(self.refs()[0][3],hashlib.sha256(replacement.encode()).hexdigest())

    def test_legacy_discovery_is_bounded_and_one_report_finishes_each_step(self):
        for index in range(123):self.insert('legacy-'+str(index))
        self.install()
        first=lookup.step(self.db,discovery_limit=10)
        self.assertEqual(first['ready'],1);self.assertEqual(first['pending'],9)
        self.assertFalse(first['discovery_complete'])
        self.assertEqual(first['known_reports'],123)
        for _ in range(122):status=lookup.step(self.db,discovery_limit=10)
        self.assertTrue(status['complete']);self.assertEqual(len(self.refs()),123)

    def test_running_job_survives_crash_and_resumes_after_reopen(self):
        self.install();self.insert()
        with patch.object(lookup,'_read_report',side_effect=SystemExit('fixture crash')):
            with self.assertRaises(SystemExit):lookup.step(self.db)
        self.assertEqual(self.db.execute('SELECT state FROM report_lookup_jobs').fetchone()[0],'running')
        self.db.close();self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        self.assertTrue(lookup.step(self.db)['complete']);self.assertEqual(len(self.refs()),1)

    def test_direct_update_invalidates_refs_atomically_and_rollback_restores(self):
        self.install();self.insert();lookup.step(self.db)
        original=self.refs();generation=self.db.execute('SELECT generation FROM report_lookup_jobs').fetchone()[0]
        self.db.execute('UPDATE analysis_reports SET result_json=?',(self.body(sha='b'*64),))
        self.assertEqual(self.refs(),[])
        self.assertGreater(self.db.execute('SELECT generation FROM report_lookup_jobs').fetchone()[0],generation)
        self.db.rollback();self.assertEqual(self.refs(),original)

    def test_fk_off_delete_replace_and_rename_cannot_leave_old_refs(self):
        self.install();self.db.execute('PRAGMA foreign_keys=OFF');self.insert();lookup.step(self.db)
        previous=self.db.execute('SELECT generation FROM report_lookup_jobs').fetchone()[0]
        self.insert(sha='b'*64,replace=True)
        self.assertEqual(self.refs(),[])
        self.assertGreater(self.db.execute('SELECT generation FROM report_lookup_jobs').fetchone()[0],previous)
        lookup.step(self.db);self.assertEqual(self.refs()[0][0],'b'*64)
        self.db.execute('UPDATE analysis_reports SET report_id=?,result_json=?',('renamed',self.body('renamed','c'*64)));self.db.commit()
        self.assertEqual(self.refs(),[])
        self.assertIsNone(self.db.execute("SELECT 1 FROM report_lookup_jobs WHERE report_id='report-1'").fetchone())
        lookup.step(self.db);self.assertEqual(self.refs()[0][1],'renamed')
        self.db.execute('DELETE FROM analysis_reports');self.db.commit()
        self.assertEqual(self.refs(),[]);self.assertEqual(self.db.execute('SELECT COUNT(*) FROM report_lookup_jobs').fetchone()[0],0)

    def test_generation_cas_rejects_delete_reinsert_aba_and_rename(self):
        self.install();self.insert()
        generation=self.db.execute('SELECT generation FROM report_lookup_jobs').fetchone()[0]
        receipt=lookup._read_report(self.db,'report-1',generation)
        self.db.execute('DELETE FROM analysis_reports');self.db.commit()
        self.insert(sha='b'*64)
        self.assertFalse(lookup._publish(self.db,receipt));self.assertEqual(self.refs(),[])
        lookup.step(self.db);self.assertEqual(self.refs()[0][0],'b'*64)

    def test_replacement_between_parse_and_publish_cannot_commit_stale_receipt(self):
        self.install();self.insert();publish=lookup._publish
        def replace_before_publish(db,receipt):
            with closing(sqlite3.connect(self.path)) as other:
                with other:other.execute('UPDATE analysis_reports SET result_json=?',(self.body(sha='b'*64),))
            return publish(db,receipt)
        with patch.object(lookup,'_publish',side_effect=replace_before_publish):lookup.step(self.db)
        self.assertEqual(self.refs(),[])
        self.assertEqual(self.db.execute('SELECT state FROM report_lookup_jobs').fetchone()[0],'pending')
        lookup.step(self.db);self.assertEqual(self.refs()[0][0],'b'*64)

    def test_chunked_hash_and_metadata_use_same_read_snapshot_under_replacement(self):
        self.install();old=self.insert(raw=self.body(trace=['a'*65536]*3))
        generation=self.db.execute('SELECT generation FROM report_lookup_jobs').fetchone()[0]
        digest=hashlib.sha256();changed=[False]
        def update(block):
            digest.update(block)
            if not changed[0]:
                changed[0]=True
                with closing(sqlite3.connect(self.path)) as other:
                    with other:other.execute('UPDATE analysis_reports SET result_json=?',(self.body(sha='b'*64),))
        class Digest:
            def update(self,block):return update(block)
            def hexdigest(self):return digest.hexdigest()
        with patch.object(lookup.hashlib,'sha256',return_value=Digest()):
            receipt=lookup._read_report(self.db,'report-1',generation)
        self.assertEqual(receipt['body_sha256'],hashlib.sha256(old.encode()).hexdigest())
        self.assertEqual(receipt['sources'],['a'*64])
        self.assertFalse(lookup._publish(self.db,receipt));self.assertEqual(self.refs(),[])

    def test_bad_report_does_not_block_later_jobs_or_modify_original(self):
        self.insert('bad',raw='private malformed data');self.insert('good');self.install()
        lookup.step(self.db);status=lookup.step(self.db)
        self.assertEqual(status['failed'],1);self.assertEqual(status['ready'],1);self.assertFalse(status['complete'])
        self.assertEqual(self.db.execute("SELECT result_json FROM analysis_reports WHERE report_id='bad'").fetchone()[0],'private malformed data')
        self.assertEqual(self.refs()[0][1],'good')

    def test_unsupported_null_run_oversize_and_vm_limits_are_isolated_failures(self):
        self.install()
        self.insert('null-run',run=None);lookup.step(self.db)
        self.assertEqual(self.db.execute('SELECT error_code FROM report_lookup_jobs').fetchone()[0],'report_not_run_scoped')
        self.insert('oversize');lookup.step(self.db,max_bytes=1)
        self.insert('work-limit',raw=self.body('work-limit',trace=[dict(row=i) for i in range(2000)]));lookup.step(self.db,max_vm_steps=1)
        self.assertEqual(lookup.coverage(self.db)['failed'],3)
        self.assertEqual(self.refs(),[])
        self.assertTrue(all('private' not in row[0] for row in self.db.execute("SELECT error_code FROM report_lookup_jobs")))

    def test_critical_duplicate_or_scalar_membership_is_rejected(self):
        cases=[('{"report_id":"bad","run_id":"r","checks":[],"checks":[]}',),
            (json.dumps(dict(report_id='bad',run_id='r',checks=[dict(provenance=dict(source_hashes='a'*64))])),),
            ('{"report_id":"bad","run_id":"r","checks":[{"provenance":{"source_hashes":["'+('a'*64)+'"],"source_hashes":[]}}]}',)]
        self.install()
        for (raw,) in cases:
            self.insert('bad',raw=raw,replace=True);lookup.step(self.db)
            self.assertEqual(self.db.execute('SELECT state FROM report_lookup_jobs').fetchone()[0],'failed')
            self.assertEqual(self.refs(),[])

    def test_schema_validation_rejects_missing_or_semantically_changed_trigger(self):
        self.install();original=lookup.TRIGGER_SQL['report_lookup_insert']
        self.db.execute('DROP TRIGGER report_lookup_insert')
        self.db.execute(original.replace("'pending'","'PENDING'"))
        with self.assertRaises(ValueError):lookup.validate_schema(self.db)
        with self.assertRaises(ValueError):lookup.validate_schema(self.db)
        self.assertIn("'PENDING'",self.db.execute("SELECT sql FROM sqlite_master WHERE name='report_lookup_insert'").fetchone()[0])

    def test_no_lookup_installed_for_standalone_legacy_runner(self):
        runner=Runner(self.db)
        analyzer=Analyzer(Declaration('fixture','1',(PROFILE,)),lambda e,c:result('evaluated_no_finding'))
        self.assertEqual(runner.report([analyzer],evidence(run_id=None))['overall_health'],'not_assessed')
        self.assertFalse(lookup.has_schema(self.db))

    def test_backfill_continues_when_pipeline_import_signature_is_unchanged(self):
        from robot_test_hub.pipeline import Pipeline
        from robot_test_hub.storage import open_catalog
        with closing(open_catalog(Path(self.folder.name))) as db:
            # Real catalog/table structure; suppress import/report generators
            # only because this fixture intentionally has no recordings.
            pipeline=Pipeline(Path(self.folder.name),db)
            with patch('robot_test_hub.pipeline.rebuild_from_imports',return_value={'runs':[],'revision':'r'}), \
                 patch('robot_test_hub.pipeline.generate',return_value=[]):
                pipeline.tick()
                raw=self.body('later-report')
                with db:db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',('later-report','{}',raw,'1'))
                result=pipeline.tick()
            self.assertEqual(result['state'],'idle')
            self.assertEqual(db.execute("SELECT state FROM report_lookup_jobs WHERE report_id='later-report'").fetchone()[0],'ready')

    def test_legacy_null_and_empty_ids_fail_explicitly_and_can_be_deleted(self):
        self.insert(None,raw='private null identity')
        self.insert('',raw='private empty identity')
        self.insert('valid')
        self.install()
        for _ in range(4):status=lookup.step(self.db)
        self.assertTrue(status['discovery_complete'])
        self.assertEqual(status['failed'],2)
        self.assertFalse(status['complete'])
        self.assertEqual(self.refs()[0][1],'valid')
        self.db.execute("DELETE FROM analysis_reports WHERE report_id IS NULL OR report_id=''");self.db.commit()
        self.assertTrue(lookup.coverage(self.db)['complete'])


if __name__=='__main__':unittest.main()
