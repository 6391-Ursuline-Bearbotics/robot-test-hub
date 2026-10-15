"""Read-only history acceptance with synthetic records and immutable job pins."""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import review_history as api
from robot_test_hub.review import install_schema, KINDS


class ReviewHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='review & private # ')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.path = self.root / 'catalog.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        install_schema(self.db)
        self.db.execute('CREATE TABLE analyzer_jobs(job_id TEXT PRIMARY KEY,result_json TEXT)')
        self.db.execute('CREATE TABLE analysis_reports(report_id TEXT PRIMARY KEY,result_json TEXT)')
        self.db.commit()

    def record(self, identity, revision=1, kind='maintenance', **extra):
        value = dict(id=identity, revision=revision, reviewer='fixture', rationale='synthetic only')
        value.update(extra)
        self.db.execute('INSERT INTO review_records VALUES(?,?,?,?,?)',
                        (kind, identity, revision, json.dumps(value), '1791003600000000000'))
        self.db.commit()
        return value

    def get(self, route='history', **query):
        return api.get(self.root, api.PREFIX+route, {key:str(value) for key,value in query.items()})

    def error(self, status, route='history', **query):
        with self.assertRaises(api.HistoryError) as raised:
            self.get(route, **query)
        self.assertEqual(raised.exception.status, status)
        self.assertNotIn(str(self.root), str(raised.exception))
        return raised.exception

    def job(self, identity='incident', **extra):
        value = dict(job_id=identity, analyzer_id='swerve-tracking', analyzer_version='3',
                     outcome='finding', configuration={}, provenance=dict(source_type='synthetic',
                     source_hashes=['a'*64], context=dict(run_id='run-1')),
                     findings=[dict(kind='fixture', intervals_ns=[[1,2]], source_references=[dict(record_index=3)])],
                     coverage=dict(modules=[dict(module_id='module-fl', evidence_trace=[dict(index=i) for i in range(1200)])]),
                     metrics=[], unavailable=[])
        value.update(extra)
        self.db.execute('INSERT INTO analyzer_jobs VALUES(?,?)', (identity,json.dumps(value)))
        self.db.commit()
        return value

    def test_all_six_kinds_and_more_than_100_latest_ids_are_accessible(self):
        for kind in KINDS:
            for index in range(125):
                self.record(kind+'-'+str(index),kind=kind)
            found=[]; cursor=None
            while True:
                query=dict(kind=kind,limit=20)
                if cursor:query['cursor']=cursor
                page=self.get(**query)
                found.extend(item['id'] for item in page['records'])
                self.assertTrue(page['read_only'])
                self.assertLessEqual(len(page['records']),20)
                cursor=page['next_cursor']
                if cursor is None:break
            self.assertEqual(len(found),125)
            self.assertEqual(len(set(found)),125)

    def test_snapshot_survives_appends_corrections_and_tied_timestamps(self):
        for index in range(121):self.record('event-'+str(index))
        first=self.get(kind='maintenance',limit=20)
        expected={'event-'+str(index) for index in range(121)}
        self.record('event-0',revision=2,rationale='correction after pinned listing')
        self.record('new-event')
        cursor=first['next_cursor']; items=first['records'][:]
        while cursor:
            page=self.get(kind='maintenance',limit=20,cursor=cursor)
            items.extend(page['records']); cursor=page['next_cursor']
        self.assertEqual({item['id'] for item in items},expected)
        self.assertEqual(len(items),121)
        self.assertTrue(all(item['revision']==1 for item in items))
        refreshed=self.get(kind='maintenance',limit=20)
        self.assertEqual(refreshed['records'][0]['id'],'new-event')
        self.assertEqual(refreshed['records'][1]['revision'],2)

    def test_filtered_revision_pages_and_exact_record_do_not_select_latest(self):
        for revision in range(1,61):self.record('repair',revision=revision)
        first=self.get(kind='maintenance',record_id='repair',limit=20)
        self.record('repair',revision=61)
        cursor=first['next_cursor']; revisions=[item['revision'] for item in first['records']]
        while cursor:
            page=self.get(kind='maintenance',record_id='repair',limit=20,cursor=cursor)
            revisions.extend(item['revision'] for item in page['records']); cursor=page['next_cursor']
        self.assertEqual(revisions,list(range(60,0,-1)))
        exact=self.get('record',kind='maintenance',id='repair',revision=1)
        self.assertEqual(exact['record']['revision'],1)
        self.error(404,'record',kind='maintenance',id='repair',revision=99)

    def test_cursor_scope_validation_and_database_boundary_validation(self):
        for index in range(4):self.record('repair-'+str(index))
        token=self.get(kind='maintenance',limit=1)['next_cursor']
        self.error(400,kind='assignment',cursor=token)
        self.error(400,kind='maintenance',record_id='repair-1',cursor=token)
        self.error(400,kind='maintenance',cursor=token+'x')
        state=api._decode(token,'maintenance',None)
        state['boundary']=999999
        self.error(400,kind='maintenance',cursor=api._encode(state))
        self.record('assignment',kind='assignment')
        state=api._decode(token,'maintenance',None)
        state['boundary']=self.db.execute("SELECT rowid FROM review_records WHERE kind='assignment'").fetchone()[0]
        state['highwater']=state['boundary']
        state['fence']=api._fence(self.db,state['highwater'])
        self.error(400,kind='maintenance',cursor=api._encode(state))

    def test_vacuum_and_schema_changes_invalidate_snapshot(self):
        self.record('first');self.record('second')
        token=self.get(kind='maintenance',limit=1)['next_cursor']
        self.db.execute('VACUUM')
        self.assertEqual(self.error(409,kind='maintenance',cursor=token).code,'review_history_changed')
        token=self.get(kind='maintenance',limit=1)['next_cursor']
        self.db.execute('CREATE TABLE new_schema(value INTEGER)');self.db.commit()
        self.error(409,kind='maintenance',cursor=token)

    def test_current_snapshot_fence_changes_fail_visible(self):
        self.record('first');self.record('second')
        token=self.get(kind='maintenance',limit=1)['next_cursor']
        self.db.execute("UPDATE review_records SET payload_json=? WHERE record_id='second'",
                        (json.dumps(dict(id='second',revision=1,rationale='unsupported mutation')),))
        self.db.commit()
        self.error(409,kind='maintenance',cursor=token)

    def test_bundle_embedded_review_and_maintenance_never_become_latest(self):
        self.job();self.job('validation')
        review=self.record('finding',kind='finding_review',job_id='incident',validation_job_id='validation',
                           finding_index=0,maintenance_id='repair')
        maintenance=self.record('repair',rationale='original repair')
        bundle=self.record('bundle',kind='regression_bundle',finding_review=review,maintenance=maintenance)
        self.record('finding',revision=2,kind='finding_review',job_id='missing-new-job',finding_index=0)
        self.record('repair',revision=2,rationale='later correction')
        chain=self.get('chain',kind='regression_bundle',id='bundle',revision=1)
        nodes={node['relation']:node for node in chain['nodes']}
        self.assertEqual(nodes['finding_review']['record'],review)
        self.assertEqual(nodes['finding_review']['state'],'embedded')
        self.assertEqual(nodes['maintenance']['record'],maintenance)
        self.assertEqual(nodes['incident_job']['job_id'],'incident')
        self.assertEqual(chain['revision'],bundle['revision'])

    def test_unpinned_maintenance_and_missing_jobs_are_explicit(self):
        self.record('repair',revision=9)
        self.record('finding',kind='finding_review',job_id='missing',maintenance_id='repair')
        chain=self.get('chain',kind='finding_review',id='finding',revision=1)
        nodes={node['relation']:node for node in chain['nodes']}
        self.assertEqual(nodes['maintenance']['state'],'unpinned')
        self.assertNotIn('record',nodes['maintenance'])
        self.assertNotIn('url',nodes['maintenance'])
        self.assertEqual(nodes['incident_job']['state'],'missing')
        self.assertEqual(nodes['validation_job']['state'],'missing')

    def test_job_projection_removes_traces_before_python_and_pins_report(self):
        self.job()
        self.db.execute('INSERT INTO analysis_reports VALUES(?,?)',('report',json.dumps(dict(run_id='run-1',checks=[dict(job_id='incident')]))))
        self.db.commit()
        self.record('finding',kind='finding_review',job_id='incident')
        chain=self.get('chain',kind='finding_review',id='finding',revision=1)
        job=next(node for node in chain['nodes'] if node['relation']=='incident_job')
        self.assertEqual(job['state'],'pinned')
        module=job['job']['coverage']['modules'][0]
        self.assertEqual(module['evidence_trace'],[])
        self.assertEqual(module['evidence_trace_total'],1200)
        self.assertTrue(module['evidence_trace_truncated'])
        self.assertEqual(job['job']['findings'][0]['source_references'],[])
        self.assertEqual(job['report_links'][0]['url'],'/api/v1/runs/run-1/reports/report')
        self.assertEqual(job['report_state'],'pinned')

    def test_missing_report_and_malformed_job_shape_remain_visible(self):
        self.job()
        self.record('finding',kind='finding_review',job_id='incident')
        chain=self.get('chain',kind='finding_review',id='finding',revision=1)
        node=next(node for node in chain['nodes'] if node['relation']=='incident_job')
        self.assertEqual(node['report_state'],'missing')
        self.db.execute('UPDATE analyzer_jobs SET result_json=?',(json.dumps(dict(job_id='incident',coverage=None)),))
        self.db.commit()
        chain=self.get('chain',kind='finding_review',id='finding',revision=1)
        self.assertEqual(next(n for n in chain['nodes'] if n['relation']=='incident_job')['state'],'unavailable')

    def test_no_database_creation_and_requests_do_not_mutate_catalog(self):
        absent=self.root/'not-created'
        with self.assertRaises(api.HistoryError):api.get(absent,api.PREFIX+'history',{'kind':'maintenance'})
        self.assertFalse(absent.exists())
        self.record('repair')
        before=self.path.read_bytes()
        calls=[];connect=sqlite3.connect
        def readonly(*args,**kwargs):
            self.assertTrue(kwargs.get('uri'))
            self.assertTrue(args[0].endswith('?mode=ro'))
            db=connect(*args,**kwargs);db.set_trace_callback(calls.append);return db
        with patch('robot_test_hub.review_history.sqlite3.connect',side_effect=readonly), \
             patch('socket.create_connection',side_effect=AssertionError('network')):
            self.get(kind='maintenance')
            self.get('record',kind='maintenance',id='repair',revision=1)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertIn('PRAGMA query_only=ON',calls)
        self.assertFalse(any(call.startswith(('INSERT','UPDATE','CREATE','DELETE')) for call in calls))

    def test_oversized_and_malformed_rows_are_bounded_redacted_failures(self):
        self.record('repair',rationale='x'*(api.MAX_RECORD_BYTES+1))
        self.error(503,'record',kind='maintenance',id='repair',revision=1)
        self.error(503,kind='maintenance')
        self.db.execute('UPDATE review_records SET payload_json=?',('{"id":"repair","revision":1,"revision":1}',))
        self.db.commit()
        self.error(503,'record',kind='maintenance',id='repair',revision=1)
        self.db.execute('UPDATE review_records SET payload_json=?',('not JSON',));self.db.commit()
        self.error(503,kind='maintenance')

    def test_history_summary_omits_nested_payloads(self):
        self.record('bundle',kind='regression_bundle',evidence=[dict(huge='x'*100000)])
        page=self.get(kind='regression_bundle')
        self.assertNotIn('evidence',page['records'][0]['record'])
        self.assertTrue(page['records'][0]['record']['display_projection'])
        self.assertLess(len(json.dumps(page)),2000)

    def test_strict_invalid_query_limits_kinds_ids_and_missing_revision(self):
        for query in ({'kind':'invalid'}, {'kind':'maintenance','limit':'0'},
                      {'kind':'maintenance','limit':'51'}, {'kind':'maintenance','limit':'01'},
                      {'kind':'maintenance','unknown':'x'}, {'kind':'maintenance','record_id':'../escape'}):
            with self.subTest(query=query):self.error(400,**query)
        self.error(400,'record',kind='maintenance',id='repair')
        self.error(400,'chain',kind='maintenance',id='repair',revision=1)


if __name__ == '__main__':unittest.main()
