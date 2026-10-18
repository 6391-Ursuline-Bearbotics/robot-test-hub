"""Independent indexed run lookup tests; no source, hardware, or GET repair."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import run_lookup, run_notes, run_api, report_api
from robot_test_hub.runs import RunCatalog, canonical
from robot_test_hub.storage import open_catalog

U=9007199254740993

def run(identity,robot='review-robot',pieces=None,**extra):
    return dict(run_id=identity,robot_id=robot,boot_id='review-boot',start_monotonic_ns='0',end_monotonic_ns='1',
        utc_intervals=[dict(utc_start_ns=str(a),utc_end_ns=str(b),uncertainty_ns=str(u)) for a,b,u in (pieces or [])],**extra)

class RunLookupReviewTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='indexed run review ');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.db=open_catalog(self.root);self.addCleanup(self.db.close)

    def source(self,revision,rows,*,raw=None):
        document=dict(revision=revision,runs=rows,mappings=[])
        raw=canonical(document) if raw is None else raw
        with self.db:
            self.db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',(revision,raw))
            self.db.execute('INSERT INTO run_catalog_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision',(revision,))
        return document,raw

    def ready(self):
        for _ in range(100):
            run_lookup.tick(self.db,limit=50)
            result=RunCatalog(self.db).search(limit=1)
            if result.get('availability')=='available':return result
        self.fail('bounded worker did not finish test fixture')

    def test_exact_large_integer_uncertainty_disjoint_overlap_dedup_and_source_order(self):
        rows=[run('late',pieces=[(U+100,U+110,1),(U+200,U+210,1)]),run('early',pieces=[(U,U+10,1),(U,U+10,1)]),run('unknown'),run('other','other-robot',[(U,U+10,1)])]
        self.source('revision-exact',rows);self.ready();catalog=RunCatalog(self.db)
        self.assertEqual([r['run_id'] for r in catalog.search()['items']],['late','early','unknown','other'])
        hit=catalog.search(utc_from_ns=U+11,utc_to_ns=U+11,robot='review-robot')
        self.assertEqual((hit['total'],[r['run_id'] for r in hit['items']]),(1,['early']))
        self.assertEqual(catalog.search(utc_from_ns=U+50,utc_to_ns=U+50)['total'],0)
        self.assertEqual(catalog.search(utc_from_ns=U+150,utc_to_ns=U+150)['total'],0)
        self.assertEqual([r['run_id'] for r in catalog.search(utc_from_ns=U+150,utc_to_ns=U+150,include_unknown=True)['items']],['unknown'])
        self.assertEqual(catalog.search(offset=99,limit=1)['total'],4)

    def test_signed_extreme_uncertainty_uses_integer_bounds(self):
        minimum,maximum=-(1<<63),(1<<63)-1
        self.source('revision-extreme',[run('negative',pieces=[(minimum+1,minimum+2,10)]),run('positive',pieces=[(maximum-2,maximum-1,10)])]);self.ready()
        catalog=RunCatalog(self.db)
        for endpoint,identity in ((minimum,'negative'),(maximum,'positive')):
            result=catalog.search(utc_from_ns=endpoint,utc_to_ns=endpoint)
            self.assertEqual((result['total'],result['items'][0]['run_id']),(1,identity))

    def test_get_operations_deny_source_document_reads_and_all_writes(self):
        self.source('revision-readonly',[run('selected',pieces=[(U,U+10,0)])]);self.ready()
        denied=[]
        writes={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_CREATE_INDEX,sqlite3.SQLITE_CREATE_TRIGGER,sqlite3.SQLITE_DROP_TABLE}
        def authorizer(action,table,column,database,source):
            if action in writes or action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document':
                denied.append((action,table,column));return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(authorizer)
        try:
            result=RunCatalog(self.db).search(robot='review-robot',offset=0,limit=1)
            self.assertEqual(result['total'],1)
            selected,revision=run_notes.select_run(self.db,'selected')
            self.assertEqual((selected['run_id'],revision),('selected','revision-readonly'))
            page=run_notes.page(self.db,'selected',{})
            self.assertEqual((page['availability'],page['total']),('available',0))
        finally:self.db.set_authorizer(lambda *unused:sqlite3.SQLITE_OK)
        self.assertEqual(denied,[])

    def test_actual_run_list_and_detail_gets_never_read_original_document_or_write(self):
        self.source('revision-http',[run('selected',pieces=[(U,U+10,0)])]);self.ready()
        original_connect=sqlite3.connect;denied=[]
        def authorize(action,table,column,*unused):
            if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) or action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document':
                denied.append((action,table,column));return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        def connect(*args,**kwargs):
            connection=original_connect(*args,**kwargs)
            # SQLite declares its connection-local json_each virtual table on
            # first use; bootstrap that read-only module before auditing GET SQL.
            connection.execute("SELECT value FROM json_each('[]')").fetchall()
            connection.set_authorizer(authorize);return connection
        with patch.object(sqlite3,'connect',connect):
            listed=run_api.get(self.root,'/api/v1/runs',{})
            selected=run_api.get(self.root,'/api/v1/runs/selected',{})
        self.assertEqual(listed['availability'],'available');self.assertEqual(listed['total'],1)
        self.assertEqual(selected['run']['run_id'],'selected');self.assertEqual(selected['annotation_candidates'],[])
        self.assertEqual((selected['annotations_display']['availability'],selected['annotations_display']['total']),('available',0))
        self.assertEqual(denied,[])

    def test_missing_or_mutated_lookup_schema_is_unavailable_and_never_repaired_by_read(self):
        self.source('revision-schema',[run('selected')]);self.ready()
        self.db.execute('DROP INDEX run_lookup_utc');self.db.commit()
        result=RunCatalog(self.db).search()
        self.assertEqual(result['availability'],'unavailable');self.assertIsNone(result['total'])
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE name='run_lookup_utc'").fetchone())
        with self.assertRaises(run_lookup.LookupUnavailable):run_lookup.select_run(self.db,'selected')

    def test_legacy_backfill_keeps_exact_selected_object_hash_and_original_bytes(self):
        document=dict(revision='revision-spaced',runs=[run('selected',pieces=[(U,U+10,0)],label='Invented whitespace')],mappings=[])
        raw=json.dumps(document,indent=3,ensure_ascii=False)
        self.source(document['revision'],document['runs'],raw=raw)
        expected=self.db.execute("SELECT r.value FROM run_catalog_revisions c,json_each(c.document,'$.runs') r WHERE c.id=?",(document['revision'],)).fetchone()[0]
        self.ready();selected,digest,revision=run_lookup.select_run(self.db,'selected')
        self.assertEqual(selected,document['runs'][0]);self.assertEqual(digest,hashlib.sha256(expected.encode()).hexdigest())
        self.assertEqual(revision,document['revision'])
        self.assertEqual(self.db.execute('SELECT document FROM run_catalog_revisions WHERE id=?',(revision,)).fetchone()[0],raw)

    def test_direct_source_update_delete_and_same_identity_reinsert_never_return_stale(self):
        document,_=self.source('revision-changing',[run('before',pieces=[(0,10,0)])]);self.ready()
        document['runs']=[run('after',pieces=[(20,30,0)])]
        with self.db:self.db.execute('UPDATE run_catalog_revisions SET document=? WHERE id=?',(canonical(document),'revision-changing'))
        pending=RunCatalog(self.db).search();self.assertIsNone(pending['total']);self.assertEqual(pending['items'],[])
        self.ready();self.assertEqual(RunCatalog(self.db).search()['items'][0]['run_id'],'after')
        self.db.execute('PRAGMA foreign_keys=OFF')
        with self.db:self.db.execute("DELETE FROM run_catalog_revisions WHERE id='revision-changing'")
        missing=RunCatalog(self.db).search();self.assertIsNone(missing['total'])
        with self.db:self.db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',('revision-changing',canonical(document)))
        self.assertIsNone(RunCatalog(self.db).search()['total'])
        self.ready();self.assertEqual(RunCatalog(self.db).search()['total'],1)

    def test_query_types_overflow_and_budget_fail_without_partial_empty_success(self):
        self.source('revision-query',[run('selected',pieces=[(0,10,0)])]);self.ready();catalog=RunCatalog(self.db)
        cases=({'offset':True},{'limit':True},{'limit':201},{'offset':-1},{'utc_from_ns':True,'utc_to_ns':1},
               {'utc_from_ns':0,'utc_to_ns':1.0},{'utc_from_ns':-(1<<63)-1,'utc_to_ns':0},{'utc_from_ns':0,'utc_to_ns':1<<63},
               {'include_unknown':1},{'robot':True})
        for arguments in cases:
            with self.subTest(arguments=arguments),self.assertRaises(ValueError):catalog.search(**arguments)
        result=run_lookup.search(self.db,max_vm_steps=0)
        self.assertIsNone(result['total']);self.assertEqual(result['items'],[])
        self.assertNotEqual(result.get('availability'),'available')

    def test_partial_backfill_is_not_a_successful_partial_page(self):
        self.source('revision-many',[run('run-%03d'%index) for index in range(101)])
        run_lookup.tick(self.db,limit=3)
        result=RunCatalog(self.db).search()
        self.assertEqual(result['availability'],'partial');self.assertIsNone(result['total']);self.assertEqual(result['items'],[])
        with self.assertRaises(run_lookup.LookupUnavailable):run_lookup.select_run(self.db,'run-000')
        self.ready();self.assertEqual(RunCatalog(self.db).search(offset=100,limit=1)['items'][0]['run_id'],'run-100')

    def test_duplicate_catalog_run_identity_never_supplies_partial_index(self):
        self.source('revision-duplicate',[run('duplicate'),run('duplicate')])
        run_lookup.tick(self.db)
        result=RunCatalog(self.db).search()
        self.assertEqual(result['availability'],'unavailable');self.assertIsNone(result['total']);self.assertEqual(result['items'],[])
        with self.assertRaises(run_lookup.LookupUnavailable):run_lookup.select_run(self.db,'duplicate')

    def test_large_selected_page_rejects_before_returning_a_partial_count(self):
        self.source('revision-large',[run('large-'+str(index),label='x'*900000) for index in range(5)])
        self.ready();catalog=RunCatalog(self.db)
        self.assertEqual(catalog.search(limit=4)['total'],5)
        too_large=catalog.search(limit=5)
        self.assertIsNone(too_large['total']);self.assertEqual(too_large['items'],[])
        self.assertEqual(too_large['availability'],'unavailable')
        self.assertEqual(run_lookup.select_run(self.db,'large-4')[0]['run_id'],'large-4')

    def test_wal_reader_keeps_old_ready_revision_until_its_snapshot_ends(self):
        self.source('revision-before',[run('before')]);self.ready()
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as reader:
            reader.execute('BEGIN')
            self.assertEqual(RunCatalog(reader).search()['items'][0]['run_id'],'before')
            run_lookup.publish_document(self.db,dict(revision='revision-after',runs=[run('after')],mappings=[]))
            self.assertEqual(RunCatalog(reader).search()['items'][0]['run_id'],'before')
            reader.rollback()
            self.assertEqual(RunCatalog(reader).search()['items'][0]['run_id'],'after')

    def test_pinned_note_cursor_keeps_old_revision_payload_and_watermark(self):
        from robot_test_hub.notebook import Notebook
        from test_run_notes import note
        self.source('revision-notes-old',[run('selected',pieces=[(1000,2000,0)])]);self.ready()
        notebook=Notebook(self.root/'catalog.sqlite3',clock_ns=lambda:9007199254740993)
        for index in range(25):notebook.save(note('review-note-%02d'%index,1800))
        first=run_notes.get(self.root,'/api/v1/runs/selected/annotations',{})
        self.assertEqual(first['total'],25);self.assertIsNotNone(first['next_cursor'])
        notebook.save(note('review-note-24',9000,2),expected_previous_revision=1)
        notebook.save(note('new-after-snapshot',1800))
        run_lookup.publish_document(self.db,dict(revision='revision-notes-new',runs=[run('selected',pieces=[(8000,10000,0)])],mappings=[]))
        old=run_notes.get(self.root,'/api/v1/runs/selected/annotations',{'cursor':first['next_cursor']})
        self.assertEqual(old['run_catalog_revision'],'revision-notes-old');self.assertEqual(old['annotation_watermark'],first['annotation_watermark'])
        self.assertEqual(old['total'],25);self.assertEqual(old['annotations'][-1]['event_id'],'review-note-24');self.assertEqual(old['annotations'][-1]['revision'],1)
        fresh=run_notes.get(self.root,'/api/v1/runs/selected/annotations',{})
        self.assertEqual((fresh['run_catalog_revision'],fresh['total']),('revision-notes-new',1))

    def test_nested_selected_run_lookup_does_not_remove_outer_note_query_budget(self):
        self.source('revision-budget',[run('selected',pieces=[(0,10,0)])]);self.ready()
        class RecordedConnection:
            def __init__(self,db):self.db=db;self.callback=None;self.changes=[];self.candidate_handler=None
            def __getattr__(self,name):return getattr(self.db,name)
            def set_progress_handler(self,callback,steps):
                self.callback=callback;self.changes.append((callback,steps));return self.db.set_progress_handler(callback,steps)
            def execute(self,sql,*args):
                if sql.startswith('WITH latest AS MATERIALIZED'):
                    self.candidate_handler=self.callback;self.changes_at_candidates=len(self.changes)
                return self.db.execute(sql,*args)
        recorded=RecordedConnection(self.db)
        page=run_notes.page(recorded,'selected',{})
        self.assertEqual(page['total'],0);self.assertIsNotNone(recorded.candidate_handler)
        self.assertEqual(recorded.changes_at_candidates,1)
        recorded.changes=[]
        run_lookup.select_run(recorded,'selected')
        self.assertEqual(len(recorded.changes),2)
        self.assertIsNotNone(recorded.changes[0][0]);self.assertIsNone(recorded.changes[-1][0])

    def test_unusable_clock_preserves_raw_run_and_has_no_partial_interval_subset(self):
        rows=[run('null-clock'),run('bad-clock'),run('reversed-clock')]
        rows[0]['utc_intervals']=None
        rows[1]['utc_intervals']=[dict(utc_start_ns=str(U),utc_end_ns=str(U+10),uncertainty_ns='0'),dict(utc_start_ns='bad',utc_end_ns=str(U+20),uncertainty_ns='0')]
        rows[2]['utc_intervals']=[dict(utc_start_ns='20',utc_end_ns='10',uncertainty_ns='0')]
        document=dict(revision='revision-unusable-clock',runs=rows,mappings=[])
        raw=json.dumps(document,indent=2)
        self.source(document['revision'],rows,raw=raw);self.ready()
        catalog=RunCatalog(self.db)
        self.assertEqual(catalog.search(utc_from_ns=U,utc_to_ns=U)['total'],0)
        self.assertEqual(catalog.search(utc_from_ns=U,utc_to_ns=U,include_unknown=True)['total'],3)
        for original in rows:
            selected,digest,_=run_lookup.select_run(self.db,original['run_id'])
            self.assertEqual(selected,original)
            sqlraw=self.db.execute("SELECT r.value FROM run_catalog_revisions c,json_each(c.document,'$.runs') r WHERE json_extract(r.value,'$.run_id')=?",(original['run_id'],)).fetchone()[0]
            self.assertEqual(digest,hashlib.sha256(sqlraw.encode()).hexdigest())
            page=run_notes.get(self.root,'/api/v1/runs/'+original['run_id']+'/annotations',{})
            self.assertEqual((page['availability'],page['reason'],page['total']),('unavailable','run_clock_unavailable',None))
        self.assertEqual(self.db.execute('SELECT document FROM run_catalog_revisions WHERE id=?',(document['revision'],)).fetchone()[0],raw)

    def test_clock_piece_resource_overflow_is_unavailable_not_unknown_clock(self):
        document,raw=self.source('revision-too-many-pieces',[run('too-many',pieces=[(0,10,0)]*257)])
        run_lookup.tick(self.db)
        result=RunCatalog(self.db).search(include_unknown=True)
        self.assertEqual(result['availability'],'unavailable');self.assertIsNone(result['total']);self.assertEqual(result['items'],[])
        self.assertEqual(self.db.execute('SELECT document FROM run_catalog_revisions WHERE id=?',(document['revision'],)).fetchone()[0],raw)

    def test_known_no_report_history_is_empty_readonly_and_keeps_valid_notes(self):
        from robot_test_hub.notebook import Notebook
        from test_run_notes import note
        self.source('revision-no-reports',[run('selected',pieces=[(1000,2000,0)])]);self.ready()
        Notebook(self.root/'catalog.sqlite3',clock_ns=lambda:9000).save(note('kept-note',1800))
        before=run_api.get(self.root,'/api/v1/runs/selected',{})
        self.assertEqual([n['event_id'] for n in before['annotation_candidates']],['kept-note'])
        denied=[]
        def authorize(action,table,column,*unused):
            if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) or action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document':
                denied.append((action,table,column));return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(authorize)
        try:history=report_api.get(self.db,'/api/v1/runs/selected/reports',{})
        finally:self.db.set_authorizer(lambda *unused:sqlite3.SQLITE_OK)
        self.assertEqual((history['schema_version'],history['run_id'],history['items'],history['total'],history['returned'],history['limit']),(1,'selected',[],0,0,20))
        self.assertIsNone(history['next_cursor']);self.assertIsNone(history['previous_cursor'])
        self.assertIsInstance(history['first_cursor'],str);self.assertEqual(len(history['snapshot_id']),64)
        self.assertEqual(denied,[])
        replay=report_api.get(self.db,'/api/v1/runs/selected/reports',{'cursor':history['first_cursor']})
        self.assertEqual(replay,history)
        self.assertEqual(run_api.get(self.root,'/api/v1/runs/selected',{})['annotation_candidates'],before['annotation_candidates'])

    def test_no_report_unknown_or_pending_run_is_not_successfully_empty(self):
        self.source('revision-report-errors',[run('selected')]);self.ready()
        with self.assertRaises(run_lookup.LookupMissing) as unknown:report_api.history(self.db,'absent',{})
        self.assertEqual(unknown.exception.status,404)
        with self.db:self.db.execute("UPDATE run_catalog_revisions SET document=document WHERE id='revision-report-errors'")
        with self.assertRaises(run_lookup.LookupUnavailable) as pending:report_api.history(self.db,'selected',{})
        self.assertEqual(pending.exception.status,503)

    def test_retained_report_history_never_requires_current_run_lookup(self):
        self.source('revision-report-retained',[run('current')]);self.ready()
        check=dict(analyzer_id='recording-quality',analyzer_version='1',job_id='retained-job',outcome='insufficient_data',coverage={},provenance={},findings=[],metrics=[],unavailable=[])
        report=dict(report_id='retained-report',run_id='historical-run',source_type='synthetic',checks=[check],evaluated_checks=0,unavailable_checks=1,finding_count=0,overall_health='not_assessed')
        raw=canonical(report)
        with self.db:
            self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',(report['report_id'],'{}',raw,'100'))
            self.db.execute("UPDATE run_catalog_revisions SET document=document WHERE id='revision-report-retained'")
        def authorize(action,table,column,*unused):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' else sqlite3.SQLITE_OK
        self.db.set_authorizer(authorize)
        try:
            with patch.object(run_lookup,'select_run',side_effect=AssertionError('Saved report history must not select current run')):
                history=report_api.history(self.db,'historical-run',{})
        finally:self.db.set_authorizer(lambda *unused:sqlite3.SQLITE_OK)
        self.assertEqual((history['total'],history['returned']),(1,1))
        self.assertEqual(history['items'][0]['report_id'],'retained-report')
        self.assertEqual(self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report['report_id'],)).fetchone()[0],raw)

    def test_actual_budget_exhaustion_does_not_block_verified_import_pipeline(self):
        from robot_test_hub.collector import Collector
        from robot_test_hub.importer import Importer
        from robot_test_hub.pipeline import Pipeline
        from test_pipeline import FixtureSource
        collector=Collector(self.root,FixtureSource(),idle_delay=0)
        try:
            collector.tick();self.assertEqual(collector.snapshot()['completed_files'],1)
        finally:collector.close()
        _,raw=self.source('poisoned-legacy',[run('legacy-'+str(index)) for index in range(1000)])
        original_tick=run_lookup.tick
        pipeline=Pipeline(self.root,self.db)
        with patch.object(run_lookup,'tick',side_effect=lambda db:original_tick(db,max_vm_steps=1000)):
            result=pipeline.tick()
            again=pipeline.tick()
        self.assertEqual((result['state'],result['imports'],result['runs'],result['reports']),('indexed',1,2,2))
        self.assertEqual(again['state'],'idle')
        jobs=Importer(self.root,self.db).list_jobs();self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['state'],'succeeded')
        failed=self.db.execute("SELECT state,error_code,next_ordinal FROM run_lookup_jobs WHERE revision='poisoned-legacy'").fetchone()
        self.assertEqual(tuple(failed),('failed','run_lookup_resource_limit',0))
        self.assertEqual(self.db.execute("SELECT document FROM run_catalog_revisions WHERE id='poisoned-legacy'").fetchone()[0],raw)
        self.assertEqual(RunCatalog(self.db).search()['total'],2)

    def test_budget_failure_does_not_mark_replacement_generation(self):
        document,_=self.source('revision-budget-replaced',[run('old-'+str(index)) for index in range(1000)])
        old_generation=self.db.execute('SELECT generation FROM run_lookup_jobs WHERE revision=?',(document['revision'],)).fetchone()[0]
        original_fail=run_lookup._fail_resource;observed=[]
        def replace_before_mark(db,job):
            self.assertFalse(db.in_transaction)
            replacement=dict(document,runs=[run('replacement')])
            with db:db.execute('UPDATE run_catalog_revisions SET document=? WHERE id=?',(canonical(replacement),document['revision']))
            observed.append(tuple(job))
            return original_fail(db,job)
        with patch.object(run_lookup,'_fail_resource',side_effect=replace_before_mark):
            state=run_lookup.tick(self.db,max_vm_steps=1000)
        self.assertEqual(len(observed),1);self.assertEqual(state['failed'],0)
        row=self.db.execute('SELECT generation,state,error_code FROM run_lookup_jobs WHERE revision=?',(document['revision'],)).fetchone()
        self.assertGreater(row[0],old_generation);self.assertEqual((row[1],row[2]),('pending',None))
        run_lookup.tick(self.db)
        self.assertEqual(RunCatalog(self.db).search()['items'][0]['run_id'],'replacement')

    def test_nonbudget_sql_failure_stays_visible_and_is_not_terminal_resource_failure(self):
        document,_=self.source('revision-sql-fault',[run('selected')])
        class SQLFaultConnection:
            def __init__(self,db):self.db=db
            def __getattr__(self,name):return getattr(self.db,name)
            def execute(self,sql,*args):
                if 'json_each(c.document' in sql:raise sqlite3.OperationalError('injected storage failure')
                return self.db.execute(sql,*args)
        with patch.object(run_lookup,'_fail_resource',side_effect=AssertionError('SQL error is not a work budget failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError,'injected storage failure'):
                run_lookup.tick(SQLFaultConnection(self.db))
        row=self.db.execute('SELECT state,error_code FROM run_lookup_jobs WHERE revision=?',(document['revision'],)).fetchone()
        self.assertEqual(tuple(row),('pending',None))
        self.assertFalse(self.db.in_transaction)
        run_lookup.tick(self.db)
        self.assertEqual(RunCatalog(self.db).search()['total'],1)

    def test_worker_rejects_caller_transaction_before_sql_and_preserves_unrelated_edits(self):
        self.source('revision-nested-budget',[run('legacy-'+str(index)) for index in range(1000)])
        self.db.execute('BEGIN')
        self.db.execute("INSERT INTO settings(key,value) VALUES('review-unrelated','preserved')")
        statements=[];self.db.set_trace_callback(statements.append)
        try:
            with self.assertRaisesRegex(ValueError,'requires_owned_transaction'):
                run_lookup.tick(self.db,max_vm_steps=1000)
        finally:self.db.set_trace_callback(None)
        self.assertEqual(statements,[]);self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='review-unrelated'").fetchone()[0],'preserved')
        self.assertEqual(tuple(self.db.execute("SELECT state,error_code FROM run_lookup_jobs WHERE revision='revision-nested-budget'").fetchone()),('pending',None))
        self.db.rollback()
        self.assertIsNone(self.db.execute("SELECT value FROM settings WHERE key='review-unrelated'").fetchone())

    def test_outer_transaction_rollback_removes_new_publication_and_keeps_old_ready(self):
        self.source('revision-before',[run('before')]);self.ready()
        self.db.execute('BEGIN')
        document=dict(revision='revision-rolled-back',runs=[run('rolled-back')],mappings=[])
        self.db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',(document['revision'],canonical(document)))
        self.db.execute('UPDATE run_catalog_state SET revision=?',(document['revision'],))
        run_lookup.index_document(self.db,document['revision'],document)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        result=RunCatalog(self.db).search();self.assertEqual(result['availability'],'available')
        self.assertEqual(result['revision'],'revision-before');self.assertEqual(result['items'][0]['run_id'],'before')
        self.assertIsNone(self.db.execute('SELECT id FROM run_catalog_revisions WHERE id=?',(document['revision'],)).fetchone())

if __name__=='__main__':unittest.main()
