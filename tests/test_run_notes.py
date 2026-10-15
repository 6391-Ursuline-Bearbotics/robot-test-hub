"""All run-note candidates remain reachable at exact saved snapshot revisions."""
import base64
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import run_notes,run_api
from robot_test_hub.notebook import Notebook
from robot_test_hub.storage import open_catalog


def note(identity,point=1800,revision=1,text='Invented observation'):
    return dict(schema_version=1,event_id=identity,revision=revision,submitted_utc_ns=str(point) if point is not None else None,
        client_monotonic_ns='100',event_utc_start_ns=str(point) if point is not None else None,event_utc_end_ns=str(point) if point is not None else None,
        when={'kind':'now' if point is not None else 'unknown'},clock_domain='synthetic-clock',clock_quality='unverified_client' if point is not None else 'unknown',
        uncertainty_ms=1000 if point is not None else None,text=text,tags=['synthetic'],source='practice-notebook',run_id=None)


def catalog(db,revision='catalog-1',intervals=None):
    if intervals is None:intervals=[(1000,2000),(1500,2500),(5000,6000)]
    run={'run_id':'run-a','utc_intervals':[dict(utc_start_ns=str(a),utc_end_ns=str(b),uncertainty_ns='1000000000') for a,b in intervals]}
    document=dict(revision=revision,runs=[run],mappings=[])
    with db:
        db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',(revision,json.dumps(document)))
        db.execute('INSERT OR REPLACE INTO run_catalog_state VALUES(1,?)',(revision,))
    return document


class RunNotesTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='run note candidates ');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        self.document=catalog(self.db)
        self.book=Notebook(self.root/'catalog.sqlite3',clock_ns=lambda:9007199254740993)
        for index in range(135):self.book.save(note('note-%03d'%index))
        self.book.save(note('gap',3000));self.book.save(note('unknown',None));self.book.save(note('outside',900))

    def get(self,**query):return run_notes.get(self.root,'/api/v1/runs/run-a/annotations',query)

    def all_pages(self,first=None):
        page=first or self.get();items=[]
        while True:
            items.extend(page['annotations'])
            self.assertLessEqual(page['returned'],page['limit'])
            if not page['next_cursor']:break
            page=self.get(limit=str(page['limit']),cursor=page['next_cursor'])
        return items

    def test_more_than_100_multiple_pieces_deduplicate_and_preserve_exact_saved_times(self):
        first=self.get()
        self.assertEqual((first['availability'],first['total'],first['returned'],first['limit']),('available',135,20,20))
        items=self.all_pages(first)
        self.assertEqual([item['event_id'] for item in items],['note-%03d'%i for i in range(135)])
        self.assertTrue(all(item['revision']==1 and item['hub_received_utc_ns']=='9007199254740993' and item['event_utc_start_ns']=='1800' for item in items))
        self.assertEqual(first['candidate_basis'],run_notes.BASIS)
        self.assertIs(first['clock_uncertainty_applied'],False)
        self.assertNotIn('gap',[item['event_id'] for item in items])
        self.assertNotIn('outside',[item['event_id'] for item in items])

    def test_appended_edits_and_new_notes_do_not_shadow_snapshot(self):
        first=self.get()
        self.book.save(note('note-125',99999,2,'Correction outside'),expected_previous_revision=1)
        self.book.save(note('outside',1800,2,'Correction inside'),expected_previous_revision=1)
        self.book.save(note('new-after-snapshot'))
        old=self.all_pages(first)
        self.assertEqual(len(old),135)
        self.assertEqual(next(item for item in old if item['event_id']=='note-125')['revision'],1)
        self.assertNotIn('outside',[item['event_id'] for item in old])
        current=self.all_pages()
        self.assertEqual(len(current),136)
        self.assertNotIn('note-125',[item['event_id'] for item in current])
        self.assertEqual(next(item for item in current if item['event_id']=='outside')['revision'],2)

    def test_latest_revision_selected_before_overlap_filter(self):
        self.book.save(note('inside-to-outside'))
        self.book.save(note('inside-to-outside',99999,2),expected_previous_revision=1)
        self.book.save(note('outside-to-inside',99999))
        self.book.save(note('outside-to-inside',1800,2),expected_previous_revision=1)
        items=self.all_pages()
        self.assertNotIn('inside-to-outside',[item['event_id'] for item in items])
        self.assertEqual(next(item for item in items if item['event_id']=='outside-to-inside')['revision'],2)

    def test_cursor_keeps_old_immutable_run_revision_after_current_changes(self):
        first=self.get();catalog(self.db,'catalog-2',[(5000,6000)])
        remaining=self.get(cursor=first['next_cursor'])
        self.assertEqual((remaining['run_catalog_revision'],remaining['annotation_watermark'],remaining['total']),('catalog-1',first['annotation_watermark'],135))
        fresh=self.get()
        self.assertEqual((fresh['run_catalog_revision'],fresh['total']),('catalog-2',0))
        with self.db:self.db.execute("DELETE FROM run_catalog_revisions WHERE id='catalog-1'")
        with self.assertRaises(run_notes.RunNotesError) as cm:self.get(cursor=first['next_cursor'])
        self.assertEqual(cm.exception.status,409)

    def test_unknown_invalid_and_empty_clock_are_unavailable_not_zero(self):
        for index,intervals in enumerate(([],None,[{'utc_start_ns':'bad','utc_end_ns':'2000','uncertainty_ns':'1'}])):
            revision='unknown-'+str(index)
            doc={'revision':revision,'runs':[{'run_id':'run-a','utc_intervals':intervals}]}
            with self.db:
                self.db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',(revision,json.dumps(doc)))
                self.db.execute('UPDATE run_catalog_state SET revision=?',(revision,))
            page=self.get()
            self.assertEqual((page['availability'],page['reason'],page['total'],page['annotations'],page['next_cursor']),('unavailable','run_clock_unavailable',None,[],None))

    def test_detail_is_bounded_and_does_not_use_notebook_store(self):
        with patch.object(Notebook,'__init__',side_effect=AssertionError('No store')),patch.object(Notebook,'list',side_effect=AssertionError('No old loop')):
            result=run_api.get(self.root,'/api/v1/runs/run-a',{},object())
        self.assertEqual(len(result['annotation_candidates']),20)
        display=result['annotations_display']
        self.assertEqual((display['schema_version'],display['run_id'],display['total'],display['returned']),(1,'run-a',135,20))
        self.assertEqual(result['run_catalog_revision'],display['run_catalog_revision'])
        self.assertNotIn('annotations',display)

    def test_candidate_budget_failure_preserves_valid_run_detail(self):
        with patch.object(run_notes,'MAX_VM_STEPS',0):
            detail=run_api.get(self.root,'/api/v1/runs/run-a',{})
            self.assertEqual(detail['run'],self.document['runs'][0])
            self.assertEqual(detail['annotation_candidates'],[])
            self.assertEqual((detail['annotations_display']['availability'],detail['annotations_display']['reason'],detail['annotations_display']['total']),('unavailable','run_notes_unavailable',None))
            self.assertIsNone(detail['annotations_display']['annotation_watermark'])
            with self.assertRaises(run_notes.RunNotesError) as cm:self.get()
            self.assertEqual(cm.exception.status,503)
            with self.assertRaises(run_notes.RunNotesError) as cm:run_api.get(self.root,'/api/v1/runs/missing',{})
            self.assertEqual(cm.exception.status,404)
            with self.db:self.db.execute("UPDATE run_catalog_revisions SET document='private malformed document'")
            with self.assertRaises(run_notes.RunNotesError) as cm:run_api.get(self.root,'/api/v1/runs/run-a',{})
            self.assertEqual((cm.exception.status,str(cm.exception)),(503,'run_notes_unavailable'))

    def test_strict_query_and_cursor_scopes(self):
        first=self.get(limit='5')
        for query in ({'limit':'0'},{'limit':'101'},{'limit':'01'},{'limit':20},{'unknown':'x'},{'cursor':'!'},{'cursor':'x'*4097},{'cursor':first['next_cursor']},{'limit':'6','cursor':first['next_cursor']}):
            with self.subTest(query=query),self.assertRaises(run_notes.RunNotesError) as cm:self.get(**query)
            self.assertEqual(cm.exception.status,400)
        with self.assertRaises(run_notes.RunNotesError) as cm:run_notes.get(self.root,'/api/v1/runs/different/annotations',{'limit':'5','cursor':first['next_cursor']})
        self.assertEqual(cm.exception.status,400)
        state=json.loads(base64.urlsafe_b64decode(first['next_cursor']))
        state['after']=['not-a-boundary',1,1]
        token=base64.urlsafe_b64encode(json.dumps(state).encode()).decode()
        with self.assertRaises(run_notes.RunNotesError) as cm:self.get(limit='5',cursor=token)
        self.assertEqual(cm.exception.status,400)
        with self.db:self.db.execute('CREATE INDEX schema_change_test ON annotation_revisions(hub_received_utc_ns)')
        with self.assertRaises(run_notes.RunNotesError) as cm:self.get(limit='5',cursor=first['next_cursor'])
        self.assertEqual(cm.exception.status,409)

    def test_queries_are_readonly_close_connections_and_missing_db_is_not_created(self):
        original=sqlite3.connect;opened=[]
        def connect(*args,**kwargs):
            self.assertTrue(kwargs.get('uri'));self.assertIn('mode=ro',args[0])
            db=original(*args,**kwargs);opened.append(db)
            def authorize(action,*unused):
                # SQLite authorizes ephemeral JSON1 virtual-table registration as
                # sqlite_master UPDATE, even on mode=ro/query_only connections.
                if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_CREATE_TABLE) and not (action==sqlite3.SQLITE_UPDATE and unused[0]=='sqlite_master'):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize);return db
        before=[tuple(row) for row in self.db.execute('SELECT * FROM annotation_revisions')]
        with patch('robot_test_hub.run_notes.sqlite3.connect',side_effect=connect):self.assertEqual(self.get()['total'],135)
        for db in opened:
            with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM annotation_revisions')],before)
        missing=self.root/'missing';missing.mkdir()
        with self.assertRaises(run_notes.RunNotesError) as cm:run_notes.get(missing,'/api/v1/runs/run-a/annotations',{})
        self.assertEqual(cm.exception.status,503)
        self.assertEqual(list(missing.iterdir()),[])
        with self.assertRaises(sqlite3.Error):run_api.get(missing,'/api/v1/runs',{})
        self.assertEqual(list(missing.iterdir()),[])

    def test_budget_and_malformed_or_oversized_selected_payload_fail_redacted(self):
        with patch.object(run_notes,'MAX_VM_STEPS',0),self.assertRaises(run_notes.RunNotesError) as cm:self.get()
        self.assertEqual((cm.exception.status,cm.exception.code),(503,'run_notes_unavailable'))
        for payload in ('C:/private/password=secret','{"event_id":"note-000","revision":1,"text":"first","text":"ambiguous"}','x'*(run_notes.MAX_NOTE_BYTES+1)):
            with self.db:self.db.execute("UPDATE annotation_revisions SET payload_json=? WHERE event_id='note-000'",(payload,))
            with self.assertRaises(run_notes.RunNotesError) as cm:self.get()
            self.assertEqual(str(cm.exception),'run_notes_unavailable')


if __name__=='__main__':unittest.main()
