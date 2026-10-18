"""Bounded progress projections never run workers or read private source documents."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from robot_test_hub import stream_api,run_api,run_lookup
from robot_test_hub.storage import open_catalog

class StreamProgressAPITests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='stream progress api ');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.run='synthetic-progress-run'
        run_lookup.publish_document(self.db,dict(revision='synthetic-api-catalog',runs=[dict(run_id=self.run,robot_id='synthetic-robot',utc_intervals=[])],mappings=[]))
    def seed(self,count=1,**changes):
        with self.db:
            for index in range(count):
                values=dict(stream_id=str(index+1).zfill(64),run_id=self.run,generation=1,state='pending',phase='scheduled',
                    context_json=json.dumps({'private':'private source context'}),run_json='{}',source_type='synthetic',source_count=1,
                    selection_chain='a'*64,created_utc_ns=9007199254740993+index,updated_utc_ns=9007199254740993+index,**changes)
                self.db.execute('INSERT INTO analysis_stream_jobs('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',list(values.values()))
    def get(self,**query):return run_api.get(self.root,'/api/v1/runs/'+self.run+'/stream-progress',query)
    def test_exact_counts_live_pages_and_timestamp_precision(self):
        self.seed(33)
        first=self.get(limit='5');self.assertEqual((first['total'],first['returned'],first['next_offset']),(33,5,5))
        self.assertEqual(first['items'][0]['updated_utc_ns'],'9007199254741025')
        items=[];offset=0
        while True:
            page=self.get(limit='5',offset=str(offset));items.extend(page['items'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.assertEqual([row['stream_id'] for row in items],[str(i).zfill(64) for i in range(33,0,-1)])
        self.assertTrue(all(row['report_available'] is False and row['source_verification_complete'] is False for row in items))
    def test_no_worker_or_source_body_or_database_mutations_on_read(self):
        self.seed();forbidden=[]
        def authorize(action,table,column,*unused):
            if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE):forbidden.append((table,column));return sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_READ and ((table=='run_catalog_revisions' and column=='document') or (table=='analysis_stream_jobs' and column in ('context_json','run_json','descriptor_json'))):forbidden.append((table,column));return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(authorize)
        try:
            with patch('robot_test_hub.stream_analysis.StreamRunner',side_effect=AssertionError('GET constructed worker')):
                result=stream_api.page(self.db,self.run,{})
        finally:self.db.set_authorizer(lambda *unused:sqlite3.SQLITE_OK)
        self.assertEqual(result['availability'],'available');self.assertEqual(forbidden,[])
        self.assertNotIn('private source',json.dumps(result));self.assertNotIn('context_json',json.dumps(result))
    def test_error_text_is_fixed_and_inconsistent_completion_is_unavailable(self):
        self.seed(error_code='private path / school / secret')
        with self.db:self.db.execute("UPDATE analysis_stream_jobs SET state='failed',phase='failed'")
        result=self.get();self.assertEqual(result['items'][0]['reason'],'stream_processing_failed');self.assertNotIn('private path',json.dumps(result))
        with self.db:self.db.execute("UPDATE analysis_stream_jobs SET state='ready',phase='complete',error_code=NULL")
        result=self.get();self.assertEqual(result['availability'],'unavailable');self.assertIsNone(result['total']);self.assertEqual(result['items'],[])
    def test_known_empty_and_unknown_identity_remain_distinct(self):
        result=self.get();self.assertEqual((result['availability'],result['total']),('available',0))
        with self.assertRaises(run_lookup.LookupMissing):stream_api.page(self.db,'no-such-run',{})
    def test_schema_failure_and_sql_budget_do_not_repair_or_claim_zero(self):
        self.seed()
        with patch.object(stream_api,'MAX_VM_STEPS',1):result=stream_api.page(self.db,self.run,{})
        self.assertEqual(result['availability'],'unavailable');self.assertIsNone(result['total'])
        with self.db:self.db.execute('DROP TABLE analysis_stream_unique')
        result=self.get();self.assertEqual(result['availability'],'unavailable');self.assertIsNone(result['total'])
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='analysis_stream_unique'").fetchone())
    def test_invalid_query_and_missing_catalog_never_create_archive(self):
        for query in ({'limit':'0'},{'limit':'21'},{'offset':'-1'},{'limit':'01'},{'offset':'9223372036854775808'},{'extra':'1'}):
            with self.assertRaises(ValueError):self.get(**query)
        missing=self.root/'never-created'
        result=stream_api.get(missing,'/api/v1/runs/'+self.run+'/stream-progress',{})
        self.assertEqual(result['availability'],'unavailable');self.assertIsNone(result['total']);self.assertFalse(missing.exists())

if __name__=='__main__':unittest.main()
