"""Synthetic catalog-only flow status; no recording bytes or live endpoints."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import processing_api as api
from robot_test_hub.storage import open_catalog
from robot_test_hub import report_lookup
from robot_test_hub.wpilog import PROFILE, EXTRACTOR_VERSION, MAPPING_REVISION


class ProcessingApiTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='processing private & # ')
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name).resolve()
        self.db=open_catalog(self.root)
        self.addCleanup(self.db.close)
        self.sequence=0
        self.db.execute("INSERT INTO manifest_snapshots(snapshot_id,complete,observed_utc_ns,observed_monotonic_ns) VALUES(1,1,'1','1')")
        self.db.commit()

    def sha(self,index=1):return hashlib.sha256(('recording-'+str(index)).encode()).hexdigest()

    def transfer(self,sha=None,identity=None,state='complete',offset=10,verified=True,format='wpilog',profile=PROFILE):
        sha=sha or self.sha()
        self.sequence+=1;identity=identity or 'transfer-'+str(self.sequence)
        self.db.execute('INSERT INTO files(id,name,size,sha256,created_at,offset,state,error) VALUES(?,?,?,?,?,?,?,?)',
            (identity,'private-password@robot/path',10,sha,1,offset,state,'private exception'))
        self.db.execute('INSERT INTO manifest_items VALUES(?,?,?)',(1,identity,json.dumps(dict(format=format,format_profile=profile))))
        if verified:self.db.execute("INSERT INTO verification_jobs(file_id,state,digest) VALUES(?,'complete',?)",(identity,sha))
        self.db.commit();return sha

    def job(self,sha=None,identity=None,state='succeeded',profile=PROFILE,mapping=MAPPING_REVISION,extractor=EXTRACTOR_VERSION,source='SYNTHETIC'):
        sha=sha or self.sha();self.sequence+=1;identity=identity or 'job-'+str(self.sequence)
        self.db.execute('INSERT OR IGNORE INTO import_artifacts VALUES(?,?,?,?,?,?)',(sha,10,'private/'+sha,source,'valid',1))
        self.db.execute('INSERT OR IGNORE INTO import_requests VALUES(?,?,?,?,?)',(sha,'private-camera-password',source,'',1))
        self.db.execute('INSERT INTO import_jobs(id,artifact_sha256,extractor_version,profile,mapping_revision,state,error_code,error,created_utc_ns,updated_utc_ns) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (identity,sha,extractor,profile,mapping,state,'unsupported_profile' if state=='unsupported' else None,'private-password',1,2))
        self.db.commit();return identity

    def catalog(self,jobs=(),runs=(),revision='catalog-1'):
        doc=dict(revision=revision,inputs=[[job,'a'*64] for job in jobs],runs=list(runs))
        self.db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',(revision,json.dumps(doc)))
        self.db.execute('INSERT OR REPLACE INTO run_catalog_state VALUES(1,?)',(revision,));self.db.commit()

    def report(self,sha=None,identity='report-1',run='run-1',trace=None):
        sha=sha or self.sha()
        doc=dict(report_id=identity,run_id=run,checks=[dict(provenance=dict(source_hashes=[sha]),coverage=dict(trace=trace or []))])
        raw=json.dumps(doc)
        self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',(identity,'{}',raw,'1'));self.db.commit()
        report_lookup.index_report(self.db,identity)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self,sha=None,**query):
        return api.get(self.root,api.PREFIX+('/'+sha if sha else ''),{key:str(value) for key,value in query.items()},indexer=dict(state='running'))

    def detail(self,sha=None):return self.get(sha or self.sha())['recording']

    def error(self,status,sha=None,**query):
        with self.assertRaises(api.ProcessingError) as raised:self.get(sha,**query)
        self.assertEqual(raised.exception.status,status)
        self.assertNotIn('private',str(raised.exception));return raised.exception

    def test_more_than_100_hashes_reachable_and_sha_deduplicated(self):
        for index in range(125):self.transfer(self.sha(index))
        self.transfer(self.sha(1));self.job(self.sha(1))
        found=[];cursor=None
        while True:
            page=self.get(**({'cursor':cursor} if cursor else {}));found.extend(item['sha256'] for item in page['items'])
            self.assertEqual(page['counts']['recordings'],125)
            cursor=page['next_cursor']
            if not cursor:break
        self.assertEqual(len(found),125);self.assertEqual(len(set(found)),125)
        detail=self.detail();self.assertEqual(detail['original']['transfer_count'],2)
        self.assertEqual(detail['original']['artifact_count'],1)

    def test_expected_partial_and_missing_verification_cannot_claim_verified(self):
        self.transfer(state='queued',offset=0,verified=False)
        self.assertEqual(self.detail()['original']['state'],'expected')
        self.db.execute("UPDATE files SET offset=5,state='downloading'");self.db.commit()
        self.assertEqual(self.detail()['original']['state'],'unverified')
        self.db.execute("UPDATE files SET offset=10,state='complete'");self.db.commit()
        self.assertEqual(self.detail()['original']['state'],'unverified')
        self.db.execute("INSERT INTO verification_jobs(file_id,state,digest) SELECT id,'complete',? FROM files",('b'*64,));self.db.commit()
        value=self.detail();self.assertNotEqual(value['original']['state'],'verified')
        self.assertTrue(value['attention'])

    def test_opaque_demo_is_not_applicable_and_not_an_import_failure(self):
        self.transfer(format='opaque',profile=None)
        item=self.detail();self.assertEqual(item['imports']['state'],'not_applicable')
        self.assertEqual(item['indexing']['state'],'not_applicable');self.assertFalse(item['attention'])
        self.assertEqual(self.get(view='attention')['total'],0)

    def test_exact_current_toolchain_failure_does_not_hide_retained_old_runs(self):
        old=self.job(identity='old-job',mapping='old-mapping')
        self.job(identity='new-job',state='invalid')
        self.catalog([old],[dict(run_id='retained-run',segment_ids=[old])])
        value=self.detail();self.assertEqual(value['imports']['state'],'invalid')
        self.assertEqual(value['indexing']['run_count'],1)
        self.assertEqual(value['indexing']['state'],'searchable_previous_import')
        jobs=self.get(self.sha(),section='imports')['items'];self.assertEqual(len(jobs),2)
        self.assertEqual(sum(j['supported_current_toolchain'] for j in jobs),1)

    def test_multiple_declared_profiles_have_separate_outcomes(self):
        self.transfer(identity='transfer-a')
        self.transfer(identity='transfer-b',profile='unqualified-profile')
        self.job(identity='good')
        self.job(identity='unsupported',profile='unqualified-profile',state='unsupported')
        item=self.detail();self.assertEqual(item['imports']['state'],'mixed')
        self.assertEqual(item['imports']['expected_profiles'],2)
        self.assertEqual(item['imports']['current_jobs'],2)
        self.assertTrue(item['attention']);self.assertEqual(self.get(view='attention')['counts']['attention'],1)

    def test_pending_new_policy_keeps_old_index_facts_and_no_runs_differs(self):
        old=self.job(identity='old',mapping='previous')
        self.job(identity='pending',state='pending')
        self.catalog([old],[])
        value=self.detail();self.assertEqual(value['imports']['state'],'pending')
        self.assertEqual(value['indexing']['state'],'no_runs')
        self.assertEqual(value['reports']['state'],'absent')

    def test_imported_not_indexed_is_not_a_searchable_health_pass(self):
        self.job()
        value=self.detail();self.assertEqual(value['imports']['state'],'succeeded')
        self.assertEqual(value['indexing']['state'],'not_indexed')
        self.assertFalse(self.get()['hardware_qualified'])
        self.assertEqual(self.get()['overall_health'],'not_assessed')

    def test_report_membership_uses_exact_source_hash_not_just_matching_run(self):
        job=self.job();self.catalog([job],[dict(run_id='run-1',segment_ids=[job])])
        expected=self.report()
        self.report(self.sha(999),identity='unrelated-report')
        page=self.get(self.sha(),section='reports')
        self.assertEqual(page['total'],1);self.assertEqual(page['items'][0]['sha256'],expected)
        self.assertEqual(page['items'][0]['report_id'],'report-1')
        self.assertEqual(page['items'][0]['url'],'/api/v1/runs/run-1/reports/report-1')

    def test_hash_detail_default_has_counts_without_decoding_or_hashing_reports(self):
        self.job();self.report(trace=[dict(private='camera-secret',index=i) for i in range(1000)])
        with patch('robot_test_hub.processing_api.json.loads',side_effect=AssertionError('payload decoded')):
            detail=self.get(self.sha())
        self.assertEqual(detail['sections']['reports']['total'],1)
        self.assertNotIn('camera-secret',json.dumps(detail))

    def test_all_reference_sections_paginate_and_cursors_are_scope_bound(self):
        for index in range(61):self.transfer(identity='transfer-'+str(index))
        first=self.get(self.sha(),section='transfers',limit=20)
        cursor=first['next_cursor'];ids=[item['id'] for item in first['items']]
        while cursor:
            page=self.get(self.sha(),section='transfers',limit=20,cursor=cursor)
            ids.extend(item['id'] for item in page['items']);cursor=page['next_cursor']
        self.assertEqual(len(set(ids)),61)
        self.error(400,self.sha(),section='imports',cursor=first['next_cursor'])
        self.error(400,cursor=first['next_cursor'])

    def test_import_checksum_mismatch_is_visible_and_never_verified(self):
        self.job(state='checksum_mismatch',source='TRANSFER_CHECKSUM_MISMATCH')
        value=self.detail();self.assertEqual(value['imports']['state'],'checksum_mismatch')
        self.assertEqual(value['original']['state'],'unverified');self.assertTrue(value['attention'])

    def test_global_attention_filter_finds_bad_recordings_beyond_current_page(self):
        for index in range(105):self.transfer(self.sha(index),format='opaque',profile=None)
        self.job(self.sha(999),state='unsupported')
        page=self.get();self.assertEqual(page['counts']['attention'],1)
        attention=self.get(view='attention');self.assertEqual(attention['items'][0]['sha256'],self.sha(999))
        self.assertEqual(attention['total'],1)
        self.error(400,view='attention',cursor=page['next_cursor'])

    def test_no_names_paths_raw_errors_or_malicious_profile_text_exported(self):
        self.transfer(profile='rtsp://private-password@camera/path')
        self.job(profile='rtsp://private-password@camera/path',state='unsupported')
        for query in ({},{'section':'transfers'},{'section':'imports'}):
            self.assertNotIn('private-password',json.dumps(self.get(self.sha(),**query)))
        self.assertEqual(self.get(self.sha(),section='imports')['items'][0]['profile'],'unavailable')

    def test_missing_and_malformed_queries_are_redacted(self):
        self.error(404,self.sha())
        for query in ({'limit':'0'},{'limit':'51'},{'limit':'01'},{'view':'bad'},{'unknown':'x'},{'cursor':'x'*2050}):
            with self.subTest(query=query):self.error(400,**query)
        self.error(400,self.sha(),limit=20)
        self.error(400,self.sha(),section='raw')

    def test_malformed_scalar_run_or_report_membership_cannot_claim_inclusion(self):
        job=self.job();self.catalog([job],[dict(run_id='run-1',segment_ids=job)])
        self.error(503)
        self.db.execute('DELETE FROM run_catalog_state');self.db.commit()
        self.report()
        doc=dict(run_id='run-1',checks=[dict(provenance=dict(source_hashes=self.sha()))])
        self.db.execute('UPDATE analysis_reports SET result_json=?',(json.dumps(doc),));self.db.commit()
        value=self.get()['items'][0]['reports']
        self.assertEqual(value['state'],'unavailable');self.assertIsNone(value['count'])

    def test_huge_metadata_and_documents_are_bounded_before_python(self):
        self.transfer()
        self.db.execute('UPDATE manifest_items SET metadata_json=?',('x'*40000,));self.db.commit()
        self.assertTrue(self.detail()['attention'])
        self.report(trace=['private'*1000])
        self.db.execute("UPDATE analysis_reports SET result_json=result_json");self.db.commit()
        report_lookup.step(self.db,max_bytes=100)
        value=self.get()['items'][0]
        self.assertEqual(value['reports']['state'],'unavailable')
        self.assertIsNone(value['reports']['count'])
        self.assertEqual(value['original']['state'],'verified')

    def test_incomplete_lookup_preserves_known_refs_without_claiming_full_count(self):
        self.job();self.report(trace=['x'*1000])
        self.db.execute("INSERT INTO analysis_reports VALUES('bad','{}','invalid','1')");self.db.commit()
        for failed in (False,True):
            if failed:report_lookup.step(self.db)
            item=self.detail()
            self.assertEqual(item['imports']['state'],'succeeded')
            self.assertEqual(item['reports']['state'],'partial')
            self.assertIsNone(item['reports']['count'])
            self.assertEqual(item['reports']['known_count'],1)
            detail=self.get(self.sha())
            self.assertIsNone(detail['sections']['reports']['total'])
            page=self.get(self.sha(),section='reports')
            self.assertEqual(page['availability'],'partial')
            self.assertIsNone(page['total'])
            self.assertEqual(page['known_total'],1)
            self.assertEqual(page['items'][0]['report_id'],'report-1')
            self.assertEqual(page['reason'],'report_lookup_failed' if failed else 'report_lookup_pending')

    def test_large_aggregate_reports_remain_reachable_without_get_body_reads(self):
        self.job()
        for i in range(5):self.report(identity='large-'+str(i),trace=['x'*(7*1024*1024)])
        statements=[];connect=sqlite3.connect
        def traced(*args,**kwargs):
            db=connect(*args,**kwargs);db.set_trace_callback(statements.append);return db
        with patch('robot_test_hub.processing_api.sqlite3.connect',side_effect=traced):
            page=self.get(self.sha(),section='reports')
        self.assertEqual(page['total'],5)
        self.assertEqual(len(page['items']),5)
        self.assertFalse(any('result_json' in sql.lower() for sql in statements))

    def test_indexer_failure_stopped_and_restart_are_distinct_from_collection(self):
        self.job()
        before=self.get()
        result=api.get(self.root,api.PREFIX,{},indexer=dict(state='failed',error_code='private failure'))
        self.assertEqual(result['indexer'],dict(state='failed',error_code='indexer_failed'))
        stopped=api.get(self.root,api.PREFIX,{},indexer=dict(state='stopped'))
        self.assertEqual(stopped['indexer']['state'],'stopped')
        self.assertEqual(result['items'],before['items'])
        self.assertEqual(self.get()['items'],before['items'])

    def test_readonly_no_missing_database_creation_or_catalog_changes(self):
        absent=self.root/'absent'
        with self.assertRaises(api.ProcessingError):api.get(absent,api.PREFIX,{})
        self.assertFalse(absent.exists())
        self.transfer();self.job()
        before=list(self.db.iterdump());connect=sqlite3.connect;statements=[]
        def readonly(*args,**kwargs):
            self.assertTrue(kwargs.get('uri'));self.assertTrue(args[0].endswith('?mode=ro'))
            db=connect(*args,**kwargs);db.set_trace_callback(statements.append);return db
        with patch('robot_test_hub.processing_api.sqlite3.connect',side_effect=readonly), \
             patch('socket.create_connection',side_effect=AssertionError('network')):
            self.get();self.get(self.sha(),section='imports')
        self.assertEqual(list(self.db.iterdump()),before)
        self.assertIn('PRAGMA query_only=ON',statements)
        self.assertFalse(any(s.startswith(('INSERT','UPDATE','CREATE','DELETE')) for s in statements))


if __name__=='__main__':unittest.main()
