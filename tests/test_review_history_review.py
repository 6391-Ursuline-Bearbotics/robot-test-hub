"""Independent bounded-history, snapshot and immutable-reference checks."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import review_history as history
from robot_test_hub.review import install_schema


class ReviewHistoryIndependentTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.database = self.root / 'catalog.sqlite3'
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        install_schema(self.db)
        self.db.execute('CREATE TABLE analyzer_jobs(job_id TEXT PRIMARY KEY,result_json TEXT)')
        self.db.execute('CREATE TABLE analysis_reports(report_id TEXT PRIMARY KEY,result_json TEXT)')
        self.db.commit()

    def add(self, kind, identity, revision=1, created='100', **fields):
        value = dict(id=identity, revision=revision, **fields)
        self.db.execute('INSERT INTO review_records VALUES(?,?,?,?,?)',
                        (kind, identity, revision, json.dumps(value), created))
        self.db.commit()
        return value

    def get(self, route='history', **query):
        return history.get(self.root, '/api/v1/review/' + route, query)

    def walk(self, **query):
        result = self.get(**query)
        records = list(result['records'])
        snapshot = result['snapshot']
        while result['next_cursor']:
            result = self.get(**dict(query, cursor=result['next_cursor']))
            self.assertEqual(result['snapshot'], snapshot)
            records.extend(result['records'])
        return records

    def job(self, identity, **fields):
        value = dict(job_id=identity, provenance={'context': {'run_id':'run-fixture'}},
                     findings=[], coverage={'modules':[]}, **fields)
        self.db.execute('INSERT INTO analyzer_jobs VALUES(?,?)', (identity, json.dumps(value)))
        self.db.commit()
        return value

    def test_more_than_100_identities_tied_timestamps_and_active_corrections(self):
        for number in range(137):
            self.add('maintenance', 'repair-' + str(number))
        first = self.get(kind='maintenance', limit='17')
        self.assertEqual(len(first['records']), 17)
        self.add('maintenance', 'repair-0', 2, rationale='Correction after pinned first page')
        self.add('maintenance', 'new-during-pagination')
        result = first
        records = list(first['records'])
        while result['next_cursor']:
            result = self.get(kind='maintenance', limit='17', cursor=result['next_cursor'])
            self.assertEqual(result['snapshot'], first['snapshot'])
            records.extend(result['records'])
        self.assertEqual(len(records), 137)
        self.assertEqual(len({record['id'] for record in records}), 137)
        self.assertEqual(next(record for record in records if record['id']=='repair-0')['revision'], 1)
        self.assertNotIn('new-during-pagination', {record['id'] for record in records})
        fresh = self.walk(kind='maintenance', limit='50')
        self.assertEqual(len(fresh), 138)
        self.assertEqual(next(record for record in fresh if record['id']=='repair-0')['revision'], 2)

    def test_filtered_history_reaches_every_revision_without_new_append_leakage(self):
        for revision in range(1, 132):
            self.add('assignment', 'physical-module', revision)
        first = self.get(kind='assignment', record_id='physical-module', limit='23')
        self.add('assignment', 'physical-module', 132)
        result = first
        revisions = [record['revision'] for record in first['records']]
        while result['next_cursor']:
            result = self.get(kind='assignment', record_id='physical-module', limit='23', cursor=result['next_cursor'])
            revisions.extend(record['revision'] for record in result['records'])
        self.assertEqual(revisions, list(range(131,0,-1)))
        detail = self.get('record', kind='assignment', id='physical-module', revision='1')
        self.assertEqual(detail['record']['revision'], 1)

    def test_cursor_scope_schema_and_vacuum_invalidations(self):
        for number in range(5):
            self.add('maintenance', 'm-' + str(number))
        cursor = self.get(kind='maintenance', limit='1')['next_cursor']
        for query in ({'kind':'assignment'}, {'kind':'maintenance','record_id':'m-1'},
                      {'kind':'maintenance','cursor':cursor+'='}, {'kind':'maintenance','cursor':'not-a-cursor'}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.get(**dict({'cursor':cursor}, **query))
        self.db.execute('CREATE INDEX independent_history_index ON review_records(record_id)')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.get(kind='maintenance', cursor=cursor)
        cursor = self.get(kind='maintenance', limit='1')['next_cursor']
        self.db.execute('VACUUM')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.get(kind='maintenance', cursor=cursor)

    def test_invalid_query_values_rejected_and_missing_database_not_created(self):
        for query in ({}, {'kind':'unknown'}, {'kind':'maintenance','limit':'00'},
                      {'kind':'maintenance','limit':'51'}, {'kind':'maintenance','limit':True},
                      {'kind':'maintenance','record_id':'a/b'}, {'kind':'maintenance','extra':'x'}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.get(**query)
        empty = self.root / 'absent-archive'
        with self.assertRaises(history.HistoryError) as missing:
            history.get(empty, '/api/v1/review/history', {'kind':'maintenance'})
        self.assertEqual(missing.exception.status, 503)
        self.assertFalse(empty.exists())

    def test_history_and_detail_leave_database_bytes_and_files_unchanged(self):
        self.add('maintenance', 'repair', rationale='Original evidence')
        before_files = {path.name for path in self.root.iterdir()}
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        original_connect = sqlite3.connect
        calls = []
        def connect(database, *args, **kwargs):
            calls.append((database, kwargs))
            return original_connect(database, *args, **kwargs)
        with patch.object(history.sqlite3, 'connect', new=connect):
            self.get(kind='maintenance')
            self.get('record', kind='maintenance', id='repair', revision='1')
        self.assertTrue(all('?mode=ro' in call[0] and call[1]['uri'] for call in calls))
        self.assertEqual(before_files, {path.name for path in self.root.iterdir()})
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_oversized_record_is_rejected_before_json_decoding(self):
        self.add('maintenance', 'huge', rationale='x' * (history.MAX_RECORD_BYTES+1))
        with patch.object(history, '_json', side_effect=AssertionError('Oversized record decoded')):
            with self.assertRaises(ValueError):
                self.get('record', kind='maintenance', id='huge', revision='1')

    def test_malformed_payload_identity_duplicates_and_nonfinite_values_are_visible(self):
        for raw in ('{"id":"bad","revision":1,"revision":1}',
                    '{"id":"different","revision":1}', '{"id":"bad","revision":1,"x":NaN}', 'null'):
            self.db.execute('DELETE FROM review_records')
            self.db.execute('INSERT INTO review_records VALUES(?,?,?,?,?)', ('maintenance','bad',1,raw,'100'))
            self.db.commit()
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.get('record', kind='maintenance', id='bad', revision='1')

    def test_bundle_chain_preserves_embedded_revision_and_never_resolves_latest(self):
        old_review = self.add('finding_review','finding',1, job_id='incident-old', maintenance_id='repair', validation_job_id='validation-old')
        old_repair = self.add('maintenance','repair',1, rationale='Saved repair')
        self.add('regression_bundle','bundle',1,finding_review=old_review,maintenance=old_repair)
        self.add('finding_review','finding',2,job_id='incident-new',maintenance_id='repair')
        self.add('maintenance','repair',2,rationale='Later corrected repair')
        result = self.get('chain',kind='regression_bundle',id='bundle',revision='1')
        nodes = {node['relation']:node for node in result['nodes']}
        self.assertEqual(nodes['finding_review']['state'],'embedded')
        self.assertEqual(nodes['finding_review']['record'],old_review)
        self.assertEqual(nodes['maintenance']['record'],old_repair)
        self.assertEqual(nodes['incident_job']['job_id'],'incident-old')
        self.assertEqual(nodes['incident_job']['state'],'missing')
        self.assertEqual(nodes['validation_job']['job_id'],'validation-old')
        unpinned = self.get('chain',kind='finding_review',id='finding',revision='1')
        repair = next(node for node in unpinned['nodes'] if node['relation']=='maintenance')
        self.assertEqual(repair['state'],'unpinned')
        self.assertNotIn('record',repair)

    def test_job_projection_omits_large_trace_and_keeps_missing_report_explicit(self):
        self.add('finding_review','finding',job_id='incident')
        value = dict(job_id='incident',provenance={'context':{'run_id':'run-fixture'}},
                     coverage={'modules':[{'evidence_trace':[{'privateTrace':'x'*20000} for _ in range(100)]}]},
                     findings=[{'intervals_ns':[['0','1'] for _ in range(600)],'source_references':[{'privateSource':'fixture'}]}])
        self.db.execute('INSERT INTO analyzer_jobs VALUES(?,?)',('incident',json.dumps(value)))
        self.db.commit()
        result = self.get('chain',kind='finding_review',id='finding',revision='1')
        node = next(node for node in result['nodes'] if node['relation']=='incident_job')
        self.assertEqual(node['state'],'pinned')
        self.assertEqual(node['report_state'],'missing')
        self.assertEqual(node['job']['coverage']['modules'][0]['evidence_trace'],[])
        self.assertEqual(node['job']['coverage']['modules'][0]['evidence_trace_total'],100)
        self.assertEqual(node['job']['findings'][0]['intervals_ns'],[])
        self.assertNotIn('privateTrace',json.dumps(result))
        self.assertNotIn('privateSource',json.dumps(result))
    def test_malformed_analysis_shapes_are_explicit_unavailable_nodes(self):
        self.add('finding_review','finding',job_id='incident')
        for value in ({'job_id':'incident','provenance':None},
                      {'job_id':'incident','coverage':{'modules':[None]}},
                      {'job_id':'incident','findings':'not-an-array'},
                      {'job_id':'incident','provenance':{'context':[]}},
                      {'job_id':'incident','findings':[{'source_references':[1]}] * 65}):
            with self.subTest(value=value):
                self.db.execute('INSERT OR REPLACE INTO analyzer_jobs VALUES(?,?)',('incident',json.dumps(value)))
                self.db.commit()
                result=self.get('chain',kind='finding_review',id='finding',revision='1')
                node=next(node for node in result['nodes'] if node['relation']=='incident_job')
                self.assertEqual(node['state'],'unavailable')
                self.assertNotIn('job',node)

    def test_missing_exact_revision_never_silently_returns_latest(self):
        self.add('maintenance','repair',2,rationale='Existing later revision')
        with self.assertRaises(history.HistoryError) as missing:
            self.get('record',kind='maintenance',id='repair',revision='1')
        self.assertEqual(missing.exception.status,404)
        self.assertEqual(missing.exception.code,'review_record_not_found')
        for revision in ('01','0','-1','1.0','true'):
            with self.subTest(revision=revision), self.assertRaises(history.HistoryError) as invalid:
                self.get('record',kind='maintenance',id='repair',revision=revision)
            self.assertEqual(invalid.exception.status,400)

    def test_report_link_truncation_distinguishes_exact_boundary_from_more(self):
        self.add('finding_review','finding',job_id='incident')
        self.job('incident')
        for number in range(20):
            value={'run_id':'run-'+str(number),'checks':[{'job_id':'incident'}]}
            self.db.execute('INSERT INTO analysis_reports VALUES(?,?)',('report-'+str(number),json.dumps(value)))
        self.db.commit()
        result=self.get('chain',kind='finding_review',id='finding',revision='1')
        node=next(node for node in result['nodes'] if node['relation']=='incident_job')
        self.assertEqual(len(node['report_links']),20)
        self.assertFalse(node['report_links_truncated'])
        self.db.execute('INSERT INTO analysis_reports VALUES(?,?)',('report-extra',json.dumps({'run_id':'run-extra','checks':[{'job_id':'incident'}]})))
        self.db.commit()
        result=self.get('chain',kind='finding_review',id='finding',revision='1')
        node=next(node for node in result['nodes'] if node['relation']=='incident_job')
        self.assertEqual(len(node['report_links']),20)
        self.assertTrue(node['report_links_truncated'])
    def test_duplicate_original_record_keys_are_not_hidden_by_summary_projection(self):
        raw='{"id":"bad","revision":1,"reviewer":"first","reviewer":"conflicting"}'
        self.db.execute('INSERT INTO review_records VALUES(?,?,?,?,?)',('maintenance','bad',1,raw,'100'))
        self.db.commit()
        with self.assertRaises(history.HistoryError) as malformed:
            self.get(kind='maintenance')
        self.assertEqual(malformed.exception.status,503)
    def test_boolean_original_revision_is_not_coerced_to_integer_by_sql_projection(self):
        raw='{"id":"bad","revision":true}'
        self.db.execute('INSERT INTO review_records VALUES(?,?,?,?,?)',('maintenance','bad',1,raw,'100'))
        self.db.commit()
        with self.assertRaises(history.HistoryError) as malformed:
            self.get(kind='maintenance')
        self.assertEqual(malformed.exception.status,503)