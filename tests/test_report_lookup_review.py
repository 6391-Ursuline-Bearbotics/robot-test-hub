"""Independent report lookup ownership, CAS, transaction and backfill checks."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import report_lookup as lookup
from robot_test_hub.analysis import Analyzer, Declaration, Evidence, Runner, result
from robot_test_hub.storage import open_catalog


class ReportLookupIndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name).resolve()
        self.path=self.root/'catalog.sqlite3'
        self.db=open_catalog(self.root)
        self.addCleanup(self.db.close)
        self.sha='a'*64

    def body(self, identity='report-fixture', sha=None, padding=''):
        return json.dumps({'report_id':identity,'run_id':'run-fixture',
            'checks':[{'provenance':{'source_hashes':[sha or self.sha]}}], 'private_trace':padding})

    def insert(self, identity='report-fixture', sha=None, raw=None):
        raw=self.body(identity,sha) if raw is None else raw
        self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',(identity,'{}',raw,'100'))
        self.db.commit()
        return raw

    def generation(self, identity='report-fixture'):
        return self.db.execute('SELECT generation FROM report_lookup_jobs WHERE report_id=?',(identity,)).fetchone()[0]

    def refs(self):
        return [tuple(row) for row in self.db.execute(lookup.REFERENCES_SQL+' ORDER BY r.report_id,r.source_sha256')]

    def test_generation_cas_rejects_update_delete_reinsert_replace_and_rename_with_fk_off(self):
        self.db.execute('PRAGMA foreign_keys=OFF')
        for operation in ('update','delete_reinsert','replace','rename'):
            with self.subTest(operation=operation):
                self.db.execute('DELETE FROM analysis_reports')
                self.db.commit()
                self.insert()
                old_generation=self.generation()
                old=lookup._read_report(self.db,'report-fixture',old_generation)
                new_id='renamed-report' if operation=='rename' else 'report-fixture'
                new_body=self.body(new_id,'b'*64)
                if operation=='update':
                    self.db.execute('UPDATE analysis_reports SET result_json=? WHERE report_id=?',(new_body,'report-fixture'))
                elif operation=='delete_reinsert':
                    self.db.execute('DELETE FROM analysis_reports WHERE report_id=?',('report-fixture',))
                    self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',(new_id,'{}',new_body,'101'))
                elif operation=='replace':
                    self.db.execute('INSERT OR REPLACE INTO analysis_reports VALUES(?,?,?,?)',(new_id,'{}',new_body,'101'))
                else:
                    self.db.execute('UPDATE analysis_reports SET report_id=?,result_json=? WHERE report_id=?',(new_id,new_body,'report-fixture'))
                self.db.commit()
                self.assertFalse(lookup._publish(self.db,old))
                self.assertEqual(self.refs(),[])
                self.assertGreater(self.generation(new_id),old_generation)
                self.assertTrue(lookup.index_report(self.db,new_id))
                refs=self.refs()
                self.assertEqual(len(refs),1)
                self.assertEqual(refs[0][0],'b'*64)
                self.assertEqual(refs[0][1],new_id)
                self.assertEqual(refs[0][3],hashlib.sha256(new_body.encode()).hexdigest())

    def test_hash_and_membership_share_one_snapshot_when_writer_replaces_between_chunks(self):
        old_body=self.insert(raw=self.body(padding='old private bytes '*20000))
        generation=self.generation()
        replacement=self.body(sha='b'*64,padding='new private bytes '*30000)
        changed=[]
        with closing(sqlite3.connect(self.path)) as reader:
            def interleave(sql):
                if 'substr(CAST(result_json AS BLOB)' in sql and not changed:
                    changed.append(True)
                    self.db.execute('UPDATE analysis_reports SET result_json=? WHERE report_id=?',(replacement,'report-fixture'))
                    self.db.commit()
            reader.set_trace_callback(interleave)
            old=lookup._read_report(reader,'report-fixture',generation)
        self.assertEqual(changed,[True])
        self.assertEqual(old['error_code'],None)
        self.assertEqual(old['body_sha256'],hashlib.sha256(old_body.encode()).hexdigest())
        self.assertEqual(old['sources'],[self.sha])
        self.assertFalse(lookup._publish(self.db,old))
        self.assertTrue(lookup.index_report(self.db,'report-fixture'))
        self.assertEqual(self.refs()[0][0],'b'*64)
        self.assertEqual(self.refs()[0][3],hashlib.sha256(replacement.encode()).hexdigest())

    def test_actual_runner_report_and_lookup_do_not_commit_caller_transaction(self):
        runner=Runner(self.db)
        evidence=Evidence('fixture',(self.sha,),(),{'run_id':'run-fixture'},'mapping-fixture','reader-fixture','synthetic')
        analyzer=Analyzer(Declaration('independent-fixture','1',('fixture',)),lambda *_:result('insufficient_data'))
        self.db.execute('BEGIN IMMEDIATE')
        document=runner.report([analyzer],evidence)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT state FROM report_lookup_jobs WHERE report_id=?',(document['report_id'],)).fetchone()[0],'ready')
        with closing(sqlite3.connect(self.path)) as observer:
            self.assertEqual(observer.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
            self.assertEqual(observer.execute('SELECT COUNT(*) FROM analyzer_jobs').fetchone()[0],0)
            self.assertEqual(observer.execute('SELECT COUNT(*) FROM report_source_refs').fetchone()[0],0)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM report_lookup_jobs').fetchone()[0],0)
        self.assertEqual(self.refs(),[])

    def test_failed_report_isolated_while_other_ready_reference_remains_reachable(self):
        self.insert('bad-report',raw='private invalid JSON credential')
        good=self.insert('good-report')
        first=lookup.step(self.db)
        self.assertFalse(first['complete'])
        second=lookup.step(self.db)
        self.assertFalse(second['complete'])
        self.assertEqual(second['failed'],1)
        self.assertEqual(second['ready'],1)
        self.assertEqual(self.refs()[0][1],'good-report')
        self.assertEqual(self.refs()[0][3],hashlib.sha256(good.encode()).hexdigest())
        self.assertNotIn('private',json.dumps(second))

    def test_pending_and_running_generations_never_supply_stale_ready_references(self):
        self.insert()
        self.assertTrue(lookup.index_report(self.db,'report-fixture'))
        self.assertEqual(len(self.refs()),1)
        self.db.execute('UPDATE analysis_reports SET result_json=? WHERE report_id=?',(self.body(sha='b'*64),'report-fixture'))
        self.db.commit()
        self.assertFalse(lookup.coverage(self.db)['complete'])
        self.assertEqual(self.refs(),[])
        self.db.execute("UPDATE report_lookup_jobs SET state='running'")
        self.db.commit()
        self.assertFalse(lookup.coverage(self.db)['complete'])
        self.assertEqual(self.refs(),[])
        self.assertTrue(lookup.index_report(self.db,'report-fixture'))
        self.assertTrue(lookup.coverage(self.db)['complete'])
        self.assertEqual(self.refs()[0][0],'b'*64)

    def test_delete_ready_report_cleans_lookup_without_foreign_key_enforcement(self):
        self.insert()
        lookup.index_report(self.db,'report-fixture')
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute('DELETE FROM analysis_reports')
        self.db.commit()
        self.assertEqual(self.refs(),[])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM report_lookup_jobs').fetchone()[0],0)
        self.assertTrue(lookup.coverage(self.db)['complete'])

    def test_schema_validation_rejects_semantically_changed_literal_and_same_named_noop_trigger(self):
        original=lookup.TRIGGER_SQL['report_lookup_insert']
        self.db.execute('DROP TRIGGER report_lookup_insert')
        changed=original.replace("'pending'","'PENDING'")
        self.assertNotEqual(changed,original)
        self.db.execute(changed)
        with self.assertRaises(ValueError):
            lookup.validate_schema(self.db)
        self.db.execute('DROP TRIGGER report_lookup_insert')
        self.db.execute('CREATE TRIGGER report_lookup_insert AFTER INSERT ON analysis_reports BEGIN SELECT 1; END')
        with self.assertRaises(ValueError):
            lookup.validate_schema(self.db)

    def test_schema_validation_rejects_same_named_wrong_index_order(self):
        self.db.execute('DROP INDEX report_source_sha')
        self.db.execute('CREATE INDEX report_source_sha ON report_source_refs(report_id,source_sha256)')
        with self.assertRaises(ValueError):
            lookup.validate_schema(self.db)

    def test_legacy_discovery_is_bounded_resumes_running_after_restart_and_finishes(self):
        legacy=self.root/'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE analysis_reports(report_id TEXT PRIMARY KEY,provenance_json TEXT,result_json TEXT,created_utc_ns TEXT)')
            db.executemany('INSERT INTO analysis_reports VALUES(?,?,?,?)',
                [('legacy-'+format(number,'03d'),'{}',self.body('legacy-'+format(number,'03d')),'100') for number in range(113)])
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            with patch.object(lookup,'_read_report',side_effect=AssertionError('Migration parsed report')):
                lookup.install_schema(db)
            self.assertTrue(db.in_transaction)
            db.commit()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_lookup_jobs').fetchone()[0],0)
            first=lookup.step(db,discovery_limit=7)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_lookup_jobs').fetchone()[0],7)
            self.assertFalse(first['complete'])
            self.assertFalse(first['discovery_complete'])
            db.execute("UPDATE report_lookup_jobs SET state='running' WHERE report_id='legacy-001'")
            db.commit()
        with closing(sqlite3.connect(legacy)) as db:
            current=lookup.step(db,discovery_limit=7)
            self.assertEqual(db.execute("SELECT state FROM report_lookup_jobs WHERE report_id='legacy-001'").fetchone()[0],'ready')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_lookup_jobs').fetchone()[0],14)
            for _ in range(120):
                if current['complete']:
                    break
                current=lookup.step(db,discovery_limit=7)
            self.assertTrue(current['complete'])
            self.assertTrue(current['discovery_complete'])
            self.assertEqual(current['ready'],113)
            self.assertEqual(len(db.execute(lookup.REFERENCES_SQL).fetchall()),113)
    def test_indexed_archive_above_old_32mib_budget_pages_without_any_report_body_read(self):
        from robot_test_hub import processing_api as api
        self.db.execute('INSERT INTO import_artifacts VALUES(?,?,?,?,?,?)',
            (self.sha,10,'raw/private-unopened-path','MANUAL_LOCAL','unchecked',100))
        self.db.commit()
        expected={}
        aggregate=0
        for number in range(3):
            identity='large-report-'+str(number)
            body=self.body(identity,padding='private trace '*1000000)
            aggregate+=len(body.encode())
            expected[identity]=hashlib.sha256(body.encode()).hexdigest()
            self.insert(identity,raw=body)
            self.assertTrue(lookup.index_report(self.db,identity))
        self.assertGreater(aggregate,32*1024*1024)
        self.assertTrue(lookup.coverage(self.db)['complete'])
        original_connect=api.sqlite3.connect
        forbidden=[]
        def connect(*args,**kwargs):
            db=original_connect(*args,**kwargs)
            def authorize(action,table,column,database,trigger):
                if action==sqlite3.SQLITE_READ and table=='analysis_reports' and column=='result_json':
                    forbidden.append((table,column))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            return db
        with patch.object(api.sqlite3,'connect',new=connect):
            listing=api.get(self.root,api.PREFIX,{'limit':'1'})
            self.assertEqual(listing['items'][0]['reports']['count'],3)
            detail=api.get(self.root,api.PREFIX+'/'+self.sha,{})
            self.assertEqual(detail['recording']['reports']['state'],'available')
            refs=api.get(self.root,api.PREFIX+'/'+self.sha,{'section':'reports','limit':'2'})
            self.assertEqual(refs['total'],3)
            self.assertEqual(refs['returned'],2)
            self.assertIsNotNone(refs['next_cursor'])
            rest=api.get(self.root,api.PREFIX+'/'+self.sha,{'section':'reports','limit':'2','cursor':refs['next_cursor']})
            returned=refs['items']+rest['items']
        self.assertEqual(forbidden,[])
        self.assertEqual({row['report_id']:row['sha256'] for row in returned},expected)
        self.assertTrue(all(row['url']=='/api/v1/runs/run-fixture/reports/'+row['report_id'] for row in returned))
    def test_legacy_null_and_empty_identifiers_fail_without_blocking_valid_backfill(self):
        legacy=self.root/'legacy-invalid.sqlite3'
        with closing(sqlite3.connect(legacy)) as db:
            db.execute('CREATE TABLE analysis_reports(report_id TEXT PRIMARY KEY,provenance_json TEXT,result_json TEXT,created_utc_ns TEXT)')
            db.executemany('INSERT INTO analysis_reports VALUES(?,?,?,?)',
                [(None,'{}','{}','100'),('','{}','{}','100'),('valid-report','{}',self.body('valid-report'),'100')])
            db.commit()
            lookup.install_schema(db)
            db.commit()
            for _ in range(8):
                current=lookup.step(db,discovery_limit=1)
            self.assertEqual(current['failed'],2)
            self.assertEqual(current['ready'],1)
            self.assertEqual(current['pending'],0)
            self.assertFalse(current['complete'])
            self.assertEqual(current['state'],'failed')
            self.assertEqual(db.execute(lookup.REFERENCES_SQL).fetchone()[1],'valid-report')