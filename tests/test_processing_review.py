"""Independent processing-stage semantics and bounded-read regression checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import processing_api as api
from robot_test_hub.storage import open_catalog


class ProcessingIndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root=Path(self.folder.name).resolve()
        self.db=open_catalog(self.root)
        self.addCleanup(self.db.close)
        self.sha='a'*64

    def artifact(self, sha=None, source='MANUAL_LOCAL'):
        sha=sha or self.sha
        self.db.execute('INSERT INTO import_artifacts VALUES(?,?,?,?,?,?)',
                        (sha,10,'raw/private-local-path/'+sha,source,'unchecked',100))
        self.db.commit()
        return sha

    def transfer(self, identity, sha=None, *, state='complete', offset=10, verification='complete', digest=None, profile=None):
        sha=sha or self.sha
        self.db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',
                        (identity,'private-name.log',10,sha,1,offset,state,None))
        self.db.execute('INSERT INTO verification_jobs(file_id,state,digest) VALUES(?,?,?)',
                        (identity,verification,digest if digest is not None else sha))
        metadata={'format':'wpilog','format_profile':profile or api.PROFILE}
        self.db.execute('INSERT INTO manifest_snapshots(snapshot_id,observed_utc_ns,observed_monotonic_ns) VALUES(?,?,?)',
                        (self.db.execute('SELECT COALESCE(MAX(snapshot_id),0)+1 FROM manifest_snapshots').fetchone()[0],'100','100'))
        snapshot=self.db.execute('SELECT MAX(snapshot_id) FROM manifest_snapshots').fetchone()[0]
        self.db.execute('INSERT INTO manifest_items VALUES(?,?,?)',(snapshot,identity,json.dumps(metadata)))
        self.db.commit()

    def job(self, identity, state='succeeded', *, sha=None, extractor=None, mapping=None, profile=None, error=None):
        self.db.execute('INSERT INTO import_jobs(id,artifact_sha256,extractor_version,profile,mapping_revision,state,error_code,error,created_utc_ns,updated_utc_ns) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (identity,sha or self.sha,extractor or api.EXTRACTOR_VERSION,profile or api.PROFILE,mapping or api.MAPPING_REVISION,state,error,'private exception C:/credential/location',100,100))
        self.db.commit()

    def catalog(self, runs, inputs=()):
        document={'revision':'catalog-fixture','runs':runs,'inputs':list(inputs)}
        self.db.execute('INSERT OR REPLACE INTO run_catalog_revisions VALUES(?,?)',('catalog-fixture',json.dumps(document)))
        self.db.execute('INSERT OR REPLACE INTO run_catalog_state VALUES(1,?)',('catalog-fixture',))
        self.db.commit()

    def report(self, identity, hashes, run='run-fixture', **extra):
        document={'report_id':identity,'run_id':run,'checks':[{'provenance':{'source_hashes':hashes}}],**extra}
        raw=json.dumps(document)
        self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',(identity,'{}',raw,'100'))
        self.db.commit()
        return raw

    def get(self, sha=None, **query):
        return api.get(self.root,api.PREFIX+('/'+sha if sha else ''),query,indexer={'state':'failed','error_code':'private path exception'})

    def test_raw_claim_and_mismatched_verification_do_not_establish_verified_original(self):
        self.transfer('queued',state='queued',offset=0,verification='pending')
        item=self.get(self.sha)['recording']
        self.assertEqual(item['original']['state'],'expected')
        self.db.execute("UPDATE files SET state='complete',offset=10")
        self.db.execute("UPDATE verification_jobs SET state='complete',digest=?",('b'*64,))
        self.db.commit()
        item=self.get(self.sha)['recording']
        self.assertNotEqual(item['original']['state'],'verified')
        self.assertTrue(item['attention'])
        self.assertIn('verification_failed',item['attention_reasons'])

    def test_manual_import_and_multiple_transfer_references_deduplicate_original_sha(self):
        self.artifact()
        self.transfer('transfer-a')
        self.transfer('transfer-b')
        self.job('current-job')
        page=self.get(limit='20')
        self.assertEqual(page['total'],1)
        item=page['items'][0]
        self.assertEqual(item['original']['transfer_count'],2)
        self.assertEqual(item['original']['artifact_count'],1)
        self.assertEqual(item['original']['state'],'verified')
        self.assertEqual(item['indexing']['run_count'],0)
        refs=self.get(self.sha,section='transfers',limit='1')
        self.assertEqual(refs['total'],2)
        self.assertEqual(refs['returned'],1)
        self.assertIsNotNone(refs['next_cursor'])

    def test_current_failed_import_does_not_erase_older_searchable_membership(self):
        self.artifact()
        self.job('old-job',mapping='historical-mapping')
        self.job('current-job','invalid',error='invalid_format')
        self.catalog([{'run_id':'run-fixture','segment_ids':['old-job']}],[['old-job','dataset-fingerprint']])
        item=self.get(self.sha)['recording']
        self.assertEqual(item['imports']['state'],'invalid')
        self.assertEqual(item['indexing']['state'],'searchable_previous_import')
        self.assertEqual(item['indexing']['run_count'],1)
        self.assertEqual(item['indexing']['current_toolchain_run_count'],0)
        self.assertTrue(item['attention'])
        runs=self.get(self.sha,section='runs')
        self.assertEqual(runs['items'][0]['run_id'],'run-fixture')
        self.assertFalse(runs['items'][0]['current_toolchain'])
        self.assertEqual(runs['indexer']['state'],'failed')
        self.assertNotIn('private',json.dumps(runs))

    def test_reports_require_selected_source_hash_even_when_run_id_matches(self):
        self.artifact()
        self.job('job-fixture')
        self.catalog([{'run_id':'run-fixture','segment_ids':['job-fixture']}],[['job-fixture','fingerprint']])
        self.report('unrelated-report',['b'*64])
        raw=self.report('exact-report',[self.sha],private_recording_path='private path not public')
        item=self.get(self.sha)['recording']
        self.assertEqual(item['reports']['count'],1)
        refs=self.get(self.sha,section='reports')
        self.assertEqual([row['report_id'] for row in refs['items']],['exact-report'])
        self.assertEqual(refs['items'][0]['sha256'],hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn('private',json.dumps(refs))
        self.assertEqual(refs['items'][0]['url'],'/api/v1/runs/run-fixture/reports/exact-report')

    def test_more_than_100_originals_and_import_attempts_are_all_reachable(self):
        for number in range(123):
            self.artifact(format(number+1,'064x'))
        seen=[]
        query={'limit':'19','view':'all'}
        while True:
            page=self.get(**query)
            self.assertLessEqual(len(page['items']),19)
            seen.extend(row['sha256'] for row in page['items'])
            if not page['next_cursor']:
                break
            query['cursor']=page['next_cursor']
        self.assertEqual(len(seen),123)
        self.assertEqual(len(set(seen)),123)
        for number in range(107):
            self.job('attempt-'+format(number,'03d'),sha=seen[0],mapping='historical-'+str(number))
        seen_jobs=[]
        query={'section':'imports','limit':'13'}
        while True:
            page=self.get(seen[0],**query)
            seen_jobs.extend(row['id'] for row in page['items'])
            if not page['next_cursor']:
                break
            query['cursor']=page['next_cursor']
        self.assertEqual(len(seen_jobs),107)
        self.assertEqual(len(set(seen_jobs)),107)

    def test_cursors_reject_cross_filter_sha_and_section_reuse(self):
        self.artifact()
        self.artifact('b'*64)
        self.transfer('transfer-a')
        self.transfer('transfer-b')
        cursor=self.get(limit='1')['next_cursor']
        with self.assertRaises(api.ProcessingError) as invalid:
            self.get(view='attention',cursor=cursor)
        self.assertEqual(invalid.exception.status,400)
        cursor=self.get(self.sha,section='transfers',limit='1')['next_cursor']
        for selected,section in ((self.sha,'imports'),('b'*64,'transfers')):
            with self.subTest(selected=selected,section=section),self.assertRaises(api.ProcessingError) as invalid:
                self.get(selected,section=section,cursor=cursor)
            self.assertEqual(invalid.exception.status,400)

    def test_read_only_missing_storage_and_private_error_redaction(self):
        self.artifact()
        self.db.close()
        before={path.name:path.read_bytes() for path in self.root.iterdir() if path.is_file() and path.name not in ('catalog.sqlite3-wal','catalog.sqlite3-shm')}
        original=api.sqlite3.connect
        connections=[]
        def connect(database,*args,**kwargs):
            connections.append((database,kwargs))
            return original(database,*args,**kwargs)
        with patch.object(api.sqlite3,'connect',new=connect):
            self.get()
            self.get(self.sha)
        self.assertTrue(all('?mode=ro' in value[0] and value[1]['uri'] for value in connections))
        # SQLite WAL readers may create/update ephemeral read-mark sidecars;
        # the catalog bytes and application files must remain unchanged.
        after={path.name:path.read_bytes() for path in self.root.iterdir() if path.is_file() and path.name not in ('catalog.sqlite3-wal','catalog.sqlite3-shm')}
        self.assertEqual(before,after)
        missing=self.root/'missing'
        with self.assertRaises(api.ProcessingError) as unavailable:
            api.get(missing,api.PREFIX,{})
        self.assertEqual(unavailable.exception.status,503)
        self.assertFalse(missing.exists())
        self.assertNotIn(str(self.root),str(unavailable.exception))

    def test_scalar_source_hash_membership_cannot_create_report_claim(self):
        self.artifact()
        self.report('malformed-report',self.sha)
        result=self.get(self.sha)
        self.assertEqual(result['recording']['reports']['state'],'unavailable')
        self.assertIsNone(result['recording']['reports']['count'])
        self.assertNotEqual(result['recording']['original']['state'],'expected')

    def test_scalar_run_segment_membership_cannot_create_searchable_claim(self):
        self.artifact()
        self.job('job-fixture')
        self.catalog([{'run_id':'run-fixture','segment_ids':'job-fixture'}],[['job-fixture','fingerprint']])
        with self.assertRaises(api.ProcessingError) as unavailable:
            self.get(self.sha)
        self.assertEqual(unavailable.exception.status,503)

    def test_expected_checksum_mismatch_is_attention_without_verified_source_claim(self):
        self.artifact(source='TRANSFER_CHECKSUM_MISMATCH')
        self.job('mismatched-job','checksum_mismatch',error='checksum_mismatch')
        result=self.get(self.sha)
        item=result['recording']
        self.assertNotEqual(item['original']['state'],'verified')
        self.assertEqual(item['imports']['state'],'checksum_mismatch')
        self.assertTrue(item['attention'])
        self.assertIn('source_checksum_mismatch',item['attention_reasons'])
        self.assertEqual(item['indexing']['run_count'],0)
        attempts=self.get(self.sha,section='imports')
        self.assertEqual(attempts['items'][0]['state'],'checksum_mismatch')
        self.assertEqual(attempts['items'][0]['error_code'],'checksum_mismatch')
        self.assertNotIn('private',json.dumps(attempts))

    def test_large_report_payload_is_never_decoded_in_python(self):
        self.artifact()
        self.report('exact-report',[self.sha],private_trace='private secret ' * 100000)
        with patch.object(api.json,'loads',side_effect=AssertionError('Full stored JSON decoded in Python')):
            result=self.get(self.sha,section='reports')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['returned'],1)
        self.assertEqual(result['items'][0]['report_id'],'exact-report')
        self.assertLess(len(json.dumps(result)),4096)
        self.assertNotIn('private',json.dumps(result))

    def test_duplicate_source_membership_fields_are_not_silently_chosen(self):
        self.artifact()
        raw='{"run_id":"run-fixture","checks":[{"provenance":{"source_hashes":["'+self.sha+'"]}}],"checks":[]}'
        self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',('ambiguous-report','{}',raw,'100'))
        self.db.commit()
        result=self.get(self.sha)
        self.assertEqual(result['recording']['reports']['state'],'unavailable')
        self.assertIsNone(result['recording']['reports']['count'])
        self.assertNotEqual(result['recording']['original']['state'],'expected')
    def assert_reports_unknown(self, reason):
        result=self.get(self.sha)
        item=result['recording']
        self.assertEqual(item['original']['state'],'verified')
        self.assertEqual(item['imports']['state'],'succeeded')
        self.assertEqual(item['indexing']['run_count'],1)
        self.assertEqual(item['reports']['state'],'unavailable')
        self.assertIsNone(item['reports']['count'])
        self.assertEqual(item['reports']['reason'],reason)
        self.assertIsNone(result['sections']['reports']['total'])
        refs=self.get(self.sha,section='reports')
        self.assertEqual(refs['availability'],'unavailable')
        self.assertEqual(refs['reason'],reason)
        self.assertIsNone(refs['total'])
        self.assertEqual(refs['items'],[])
        self.assertEqual(refs['returned'],0)
        self.assertIsNone(refs['next_cursor'])
        runs=self.get(self.sha,section='runs')
        self.assertEqual(runs['items'][0]['run_id'],'run-fixture')

    def test_report_scan_budget_preserves_other_stages_and_returns_unknown_not_zero(self):
        self.artifact()
        self.job('job-fixture')
        self.catalog([{'run_id':'run-fixture','segment_ids':['job-fixture']}],[['job-fixture','fingerprint']])
        # Invalid JSON proves the disabled report CTE is not expanded after
        # the aggregate byte budget refuses report parsing.
        self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',('report-too-large','{}','not JSON and contains private text','100'))
        self.db.commit()
        with patch.object(api,'MAX_REPORT_SCAN_BYTES',1):
            self.assert_reports_unknown('report_metadata_scan_limit')

    def test_report_sql_interruption_preserves_other_stage_facts(self):
        self.artifact()
        self.job('job-fixture')
        self.catalog([{'run_id':'run-fixture','segment_ids':['job-fixture']}],[['job-fixture','fingerprint']])
        with patch.object(api,'_reports_status',side_effect=api.sqlite3.OperationalError('private interrupted SQL path')):
            self.assert_reports_unknown('report_metadata_work_limit')