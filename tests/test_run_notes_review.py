"""Independent snapshot and mapped-interval candidate pagination checks."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from robot_test_hub.notebook import install_schema as notes_schema
from robot_test_hub.runs import install_schema as runs_schema

EPOCH=1791003600000000000
RUN='a'*64
REV='b'*64


def iso(value):
    seconds,nanos=divmod(value,1000000000)
    return datetime.fromtimestamp(seconds,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')+'.'+str(nanos).zfill(9)+'Z'


class RunNotesReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='run note review ')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.db=sqlite3.connect(self.root/'catalog.sqlite3')
        self.addCleanup(self.db.close)
        runs_schema(self.db);notes_schema(self.db)
        self.run={'run_id':RUN,'utc_intervals':[{'utc_start_ns':str(EPOCH),'utc_end_ns':str(EPOCH+1000),'uncertainty_ns':'0'},
                {'utc_start_ns':str(EPOCH+2000),'utc_end_ns':str(EPOCH+3000),'uncertainty_ns':'0'}]}
        self.catalog(REV,self.run)

    def catalog(self,revision,run):
        document={'revision':revision,'runs':[run],'mappings':[]}
        with self.db:
            self.db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',(revision,json.dumps(document)))
            self.db.execute('INSERT OR REPLACE INTO run_catalog_state VALUES(1,?)',(revision,))

    def note(self,identity,start,end=None,revision=1):
        end=start if end is None else end
        payload={'schema_version':1,'event_id':identity,'revision':revision,'submitted_utc_ns':str(EPOCH+10000),
            'client_monotonic_ns':'123','event_utc_start_ns':str(start) if start is not None else None,
            'event_utc_end_ns':str(end) if end is not None else None,'when':{'kind':'unknown'} if start is None else {'kind':'exact_interval','start':iso(start),'end':iso(end)},
            'clock_domain':'review-device','clock_quality':'unverified_client','uncertainty_ms':1000,
            'author':'fixture','device_id':'review-device','text':'<img onerror=attack()> '+identity,'tags':['synthetic'],'source':'practice-notebook','run_id':None}
        with self.db:self.db.execute('INSERT INTO annotation_revisions VALUES(?,?,?,?,?,?,?)',(identity,revision,json.dumps(payload),EPOCH+10000,start,end,EPOCH+20000+revision))
        return payload

    def get(self,**query):
        from robot_test_hub import run_notes
        return run_notes.get(self.root,'/api/v1/runs/'+RUN+'/annotations',query)

    def test_snapshot_latest_revision_precedes_overlap_and_survives_appends_catalog_change(self):
        for i in range(137):self.note('event-'+str(i).zfill(3),EPOCH+100+i)
        self.note('both-pieces',EPOCH+500,EPOCH+2500)
        self.note('gap',EPOCH+1500)
        self.note('unknown',None)
        self.note('moved-out',EPOCH+100)
        self.note('moved-out',EPOCH+1500,revision=2)
        self.note('moved-in',EPOCH+1500)
        self.note('moved-in',EPOCH+100,revision=2)
        first=self.get(limit='17')
        self.assertEqual(first['total'],139)
        self.note('event-136',EPOCH+1500,revision=2)
        self.note('late-added',EPOCH+100)
        self.catalog('c'*64,{**self.run,'utc_intervals':[]})
        result=first;all_notes=[]
        while True:
            self.assertEqual(result['total'],139)
            self.assertEqual(result['run_catalog_revision'],REV)
            self.assertEqual(result['annotation_watermark'],first['annotation_watermark'])
            all_notes.extend(result['annotations'])
            if result['next_cursor'] is None:break
            result=self.get(limit='17',cursor=result['next_cursor'])
        identities=[note['event_id'] for note in all_notes]
        self.assertEqual(identities,sorted(set(identities)))
        self.assertEqual(len(identities),139)
        self.assertFalse({'gap','unknown','moved-out','late-added'}&set(identities))
        self.assertEqual(next(note for note in all_notes if note['event_id']=='event-136')['revision'],1)
        self.assertEqual(next(note for note in all_notes if note['event_id']=='moved-in')['revision'],2)
        self.assertEqual(next(note for note in all_notes if note['event_id']=='both-pieces')['hub_received_utc_ns'],str(EPOCH+20001))
        refreshed=self.get()
        self.assertEqual(refreshed['availability'],'unavailable')
        self.assertIsNone(refreshed['total'])

    def test_uri_readonly_no_notebook_construction_and_no_catalog_creation(self):
        from robot_test_hub.notebook import Notebook
        self.note('only',EPOCH)
        self.db.close()
        path=self.root/'catalog.sqlite3'
        before=hashlib.sha256(path.read_bytes()).hexdigest();names={p.name for p in self.root.iterdir()}
        original=sqlite3.connect;connections=[]
        def connect(database,*args,**kwargs):
            self.assertTrue(kwargs.get('uri'))
            self.assertIn('mode=ro',str(database))
            db=original(database,*args,**kwargs);connections.append(db)
            return db
        with patch.object(sqlite3,'connect',side_effect=connect),patch.object(Notebook,'__init__',side_effect=AssertionError('reader must not construct notebook')):
            result=self.get()
        self.assertEqual(result['total'],1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
        self.assertEqual({p.name for p in self.root.iterdir()},names)
        for db in connections:
            with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
        from robot_test_hub import run_notes
        missing=self.root/'missing'
        with self.assertRaises(run_notes.RunNotesError):run_notes.get(missing,'/api/v1/runs/'+RUN+'/annotations',{})
        self.assertFalse(missing.exists())

    def test_cursor_scope_strict_query_and_catalog_tampering_fail_closed(self):
        from robot_test_hub import run_notes
        for i in range(25):self.note('e'+str(i).zfill(2),EPOCH+1)
        first=self.get(limit='3');cursor=first['next_cursor']
        for query in ({'limit':'4','cursor':cursor},{'limit':'0'},{'limit':'101'},{'limit':'01'},{'cursor':'!'},{'offset':'1'}):
            with self.subTest(query=query),self.assertRaises(run_notes.RunNotesError):self.get(**query)
        row=self.db.execute('SELECT document FROM run_catalog_revisions WHERE id=?',(REV,)).fetchone()
        document=json.loads(row[0]);document['runs'][0]['utc_intervals'][0]['utc_end_ns']=str(EPOCH+900)
        with self.db:self.db.execute('UPDATE run_catalog_revisions SET document=? WHERE id=?',(json.dumps(document),REV))
        with self.assertRaises(run_notes.RunNotesError) as caught:self.get(limit='3',cursor=cursor)
        self.assertEqual(caught.exception.status,409)


    def test_actual_http_duplicate_query_and_stopping_errors_are_fixed_json(self):
        from robot_test_hub.server import create_http_server
        service=SimpleNamespace(root=self.root,stop=threading.Event(),closed=False,settings_lock=threading.RLock(),io_lifetimes=[])
        source=SimpleNamespace(status=lambda: self.fail('HTTP notes must not call source status'))
        server=create_http_server(service,source,0)
        thread=threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            for query,status,code in (('?limit=20&limit=20',400,'invalid_run_notes_query'),('',503,'run_notes_unavailable')):
                if status==503:service.stop.set()
                connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=3)
                try:
                    connection.request('GET','/api/v1/runs/'+RUN+'/annotations'+query)
                    response=connection.getresponse();body=response.read()
                finally:connection.close()
                self.assertEqual(response.status,status)
                self.assertEqual(json.loads(body),{'schema_version':1,'error_code':code})
        finally:
            server.shutdown();server.server_close();thread.join(3)
            self.assertFalse(thread.is_alive())

    def test_sql_work_limit_fails_unavailable_without_partial_notes(self):
        from robot_test_hub import run_notes
        for i in range(60):self.note('n'+str(i),EPOCH)
        with patch.object(run_notes,'MAX_VM_STEPS',0),self.assertRaises(run_notes.RunNotesError) as caught:
            self.get()
        self.assertEqual(caught.exception.status,503)
        self.assertEqual(caught.exception.code,'run_notes_unavailable')

    def test_legacy_run_detail_exposes_first_page_and_complete_paging_receipt(self):
        from robot_test_hub import run_api,analysis
        from robot_test_hub.notebook import Notebook
        analysis.install_schema(self.db);self.db.commit()
        for i in range(125):self.note('detail-'+str(i).zfill(3),EPOCH+10)
        with patch.object(Notebook,'__init__',side_effect=AssertionError('no constructor on read')),patch.object(Notebook,'list',side_effect=AssertionError('no legacy partial list')):
            result=run_api.get(self.root,'/api/v1/runs/'+RUN,{})
        self.assertEqual(len(result['annotation_candidates']),20)
        self.assertEqual(result['annotations_display']['total'],125)
        self.assertEqual(result['annotations_display']['returned'],20)
        self.assertEqual(result['annotations_display']['run_catalog_revision'],REV)
        self.assertIsNotNone(result['annotations_display']['next_cursor'])
        self.assertEqual(result['annotation_candidates'][0]['event_id'],'detail-000')
        self.assertEqual(result['annotation_candidates'][-1]['event_id'],'detail-019')

    def test_actual_http_note_budget_failure_preserves_valid_run_and_report_only(self):
        from robot_test_hub import analysis,run_notes
        from robot_test_hub.server import create_http_server
        analysis.install_schema(self.db)
        report_id='d'*64
        report={'report_id':report_id,'run_id':RUN,'source_type':'synthetic','source_hashes':[],
            'checks':[{'analyzer_id':'recording-quality','analyzer_version':'test','job_id':'e'*64,
            'outcome':'insufficient_data','configuration':{},'coverage':{},'provenance':{'context':{'run_id':RUN}},
            'findings':[],'metrics':[],'unavailable':['physical_not_qualified']}],
            'overall_health':'not_assessed','evaluated_checks':0,'unavailable_checks':1,'finding_count':0}
        with self.db:self.db.execute('INSERT INTO analysis_reports VALUES(?,?,?,?)',(report_id,'{}',json.dumps(report),'100'))
        self.note('selected',EPOCH)
        service=SimpleNamespace(root=self.root,stop=threading.Event(),closed=False,settings_lock=threading.RLock(),io_lifetimes=[])
        source=SimpleNamespace(status=lambda: self.fail('run reads must not contact source'))
        server=create_http_server(service,source,0)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        def request(path):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=3)
            try:
                connection.request('GET',path);response=connection.getresponse()
                return response.status,json.loads(response.read())
            finally:connection.close()
        try:
            with patch.object(run_notes,'MAX_VM_STEPS',0):
                status,detail=request('/api/v1/runs/'+RUN)
                self.assertEqual(status,200,detail)
                self.assertEqual(detail['run'],self.run)
                self.assertEqual(detail['reports'][0]['report_id'],report_id)
                self.assertEqual(detail['annotation_candidates'],[])
                self.assertEqual(detail['annotations_display']['availability'],'unavailable')
                self.assertIsNone(detail['annotations_display']['total'])
                self.assertIsNone(detail['annotations_display']['next_cursor'])
                status,error=request('/api/v1/runs/'+RUN+'/annotations')
                self.assertEqual(status,503,error)
                self.assertEqual(error['error_code'],'run_notes_unavailable')
                status,error=request('/api/v1/runs/'+'f'*64)
                self.assertEqual(status,404,error)
                for corrupt in ('{private-corrupt',json.dumps({'revision':REV,'runs':[self.run,self.run],'mappings':[]})):
                    with self.db:self.db.execute('UPDATE run_catalog_revisions SET document=? WHERE id=?',(corrupt,REV))
                    status,error=request('/api/v1/runs/'+RUN)
                    self.assertEqual(status,503,error)
                    self.assertNotIn('private-corrupt',json.dumps(error))
        finally:
            server.shutdown();server.server_close();thread.join(3)
            self.assertFalse(thread.is_alive())

if __name__=='__main__':unittest.main()
