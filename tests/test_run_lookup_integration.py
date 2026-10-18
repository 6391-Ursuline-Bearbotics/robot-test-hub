"""Schema, worker, read-only serving and coherent lookup backup integration."""
from contextlib import closing
import hashlib
import http.client
import threading
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import storage, run_lookup, run_api
from robot_test_hub.backup import Backup, BackupError, restore, verify
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.runs import RunCatalog, derive_runs
from robot_test_hub.storage import DataRootOwner, OwnershipError, SchemaError, open_catalog
from test_runs import cycle

TABLES=('run_lookup_state','run_lookup_jobs','run_lookup_runs','run_lookup_intervals')

def remove_lookup(db):
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'run_lookup_%'").fetchall():
        db.execute('DROP TRIGGER "'+name+'"')
    for name in reversed(TABLES):db.execute('DROP TABLE IF EXISTS '+name)
    db.execute('DELETE FROM schema_migrations WHERE version>=9');db.execute('PRAGMA user_version=8')

def document(count=120):
    rows=[]
    for i in range(count):
        t=i*100000000
        rows.extend((cycle(t,False),cycle(t+20000000,True),cycle(t+40000000,False)))
    return derive_runs([('invented-legacy',rows)])

def snapshot(db):return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')] for table in TABLES}

class RunLookupIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='run lookup integration ');self.addCleanup(temp.cleanup)
        self.base=Path(temp.name).resolve();self.root=self.base/'archive';self.root.mkdir()
        self.db=open_catalog(self.root);self.addCleanup(lambda:self.db.close())
    def legacy(self,count=120):
        self.db.close()
        doc=document(count);raw=json.dumps(doc,indent=1)
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as db,db:
            remove_lookup(db)
            db.execute('INSERT INTO run_catalog_revisions VALUES(?,?)',(doc['revision'],raw))
            db.execute('INSERT INTO run_catalog_state VALUES(1,?)',(doc['revision'],))
        return doc,raw
    def finish(self,db=None):
        db=db or self.db
        for _ in range(1000):
            result=run_lookup.tick(db,limit=17)
            states=db.execute("SELECT state FROM run_lookup_jobs").fetchall()
            if states and all(row[0]=='ready' for row in states):return result
        self.fail('bounded lookup backfill did not finish')
    def test_schema_nine_migration_has_no_document_reads_and_preserves_bytes(self):
        doc,raw=self.legacy();original=sqlite3.connect;denied=[]
        def connect(*args,**kwargs):
            db=original(*args,**kwargs)
            def authorize(action,table,column,*unused):
                if action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document':denied.append(column);return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize);return db
        with patch.object(storage.sqlite3,'connect',side_effect=connect):self.db=open_catalog(self.root)
        self.db.set_authorizer(lambda *args:sqlite3.SQLITE_OK)
        self.assertEqual(denied,[]);self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0],9)
        self.assertEqual(self.db.execute('SELECT document FROM run_catalog_revisions').fetchone()[0],raw)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM run_lookup_runs').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM schema_migrations WHERE version=9').fetchone()[0],1)
    def test_failed_schema_nine_migration_rolls_back_and_retries(self):
        doc,raw=self.legacy(3);install=run_lookup.install_schema
        def failed(db):install(db);raise RuntimeError('injected migration failure')
        with patch.object(run_lookup,'install_schema',side_effect=failed),self.assertRaises(RuntimeError):open_catalog(self.root)
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],8)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='run_lookup_state'").fetchone())
            self.assertEqual(db.execute('SELECT document FROM run_catalog_revisions').fetchone()[0],raw)
        self.db=open_catalog(self.root);run_lookup.validate_schema(self.db)
    def test_midbackfill_backup_restore_preserves_progress_and_exact_old_payloads(self):
        doc,raw=self.legacy();self.db=open_catalog(self.root);run_lookup.tick(self.db,limit=7)
        before=snapshot(self.db);self.assertGreater(self.db.execute('SELECT COUNT(*) FROM run_lookup_runs').fetchone()[0],0)
        self.assertLess(self.db.execute('SELECT COUNT(*) FROM run_lookup_runs').fetchone()[0],len(doc['runs']))
        destination=self.base/'backup';Backup(self.root,destination).create('partial');verify(destination/'partial')
        recovered=self.base/'recovered';restore(destination/'partial',recovered)
        with closing(open_catalog(recovered)) as db:
            self.assertEqual(snapshot(db),before);self.finish(db)
            self.assertEqual(db.execute('SELECT document FROM run_catalog_revisions').fetchone()[0],raw)
            identity=doc['runs'][-1]['run_id'];expected_raw=db.execute("SELECT r.value FROM run_catalog_revisions c,json_each(c.document,'$.runs') r WHERE json_extract(r.value,'$.run_id')=?",(identity,)).fetchone()[0]
            run,digest,revision=run_lookup.select_run(db,identity,revision=doc['revision'])
            self.assertEqual(run,doc['runs'][-1]);self.assertEqual(digest,hashlib.sha256(expected_raw.encode()).hexdigest())
    def test_schema_eight_backup_stays_eight_until_normal_open(self):
        doc,raw=self.legacy(3);destination=self.base/'backup';Backup(self.root,destination).create('eight')
        self.assertEqual(json.loads((destination/'eight/manifest.json').read_text())['catalog_schema_version'],8)
        recovered=self.base/'recovered';restore(destination/'eight',recovered)
        with closing(sqlite3.connect(recovered/'catalog.sqlite3')) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],8)
        with closing(open_catalog(recovered)) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],9)
        self.db=sqlite3.connect(self.root/'catalog.sqlite3')
    def test_new_writer_ready_search_and_selection_deny_catalog_body_read(self):
        rows=[cycle(0,False),cycle(20000000,True),cycle(40000000,False)]
        doc=RunCatalog(self.db).rebuild([('invented-writer',rows)]);identity=doc['runs'][0]['run_id'];denied=[]
        original=sqlite3.connect
        def connect(*args,**kwargs):
            db=original(*args,**kwargs)
            def authorize(action,table,column,*unused):
                if action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document':denied.append(column);return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize);return db
        with patch('robot_test_hub.run_api.sqlite3.connect',side_effect=connect):
            page=run_api.get(self.root,'/api/v1/runs',{})
            selected=run_api.get(self.root,'/api/v1/runs/'+identity,{})
        self.assertEqual(denied,[]);self.assertEqual(page['total'],1);self.assertEqual(selected['run'],doc['runs'][0])
    def test_new_writer_catalog_above_64_mib_serves_without_catalog_body_read(self):
        doc=document(3);doc['invented_large_metadata']='x'*(64*1024*1024)
        run_lookup.publish_document(self.db,doc)
        self.assertGreater(self.db.execute('SELECT length(CAST(document AS BLOB)) FROM run_catalog_revisions WHERE id=?',(doc['revision'],)).fetchone()[0],64*1024*1024)
        def authorize(action,table,column,*unused):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document' else sqlite3.SQLITE_OK
        self.db.set_authorizer(authorize)
        try:
            self.assertEqual(RunCatalog(self.db).search()['total'],3)
            self.assertEqual(run_lookup.select_run(self.db,doc['runs'][-1]['run_id'])[0],doc['runs'][-1])
        finally:self.db.set_authorizer(lambda *unused:sqlite3.SQLITE_OK)

    def test_legacy_oversize_is_unavailable_and_preserves_original_bytes(self):
        doc,raw=self.legacy(3);self.db=open_catalog(self.root)
        with patch.object(run_lookup,'MAX_CATALOG_BYTES',100):run_lookup.tick(self.db)
        self.assertEqual(self.db.execute('SELECT state FROM run_lookup_jobs WHERE revision=?',(doc['revision'],)).fetchone()[0],'failed')
        result=RunCatalog(self.db).search();self.assertIsNone(result['total']);self.assertEqual(result['availability'],'unavailable')
        self.assertEqual(self.db.execute('SELECT document FROM run_catalog_revisions').fetchone()[0],raw)

    def test_http_pending_lookup_returns_fixed_503_without_empty_success_or_repair(self):
        from robot_test_hub.config import Config
        from robot_test_hub.demo import DemoSource
        from robot_test_hub.server import create_http_server
        from robot_test_hub.service import HubService
        doc,raw=self.legacy(3);self.db=open_catalog(self.root)
        service=HubService(Config(data_dir=str(self.root)),DemoSource());self.addCleanup(service.close)
        server=create_http_server(service,service.source,0)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        self.addCleanup(lambda:(server.shutdown(),thread.join(2),server.server_close()))
        before=snapshot(self.db)
        for path in ('/api/v1/runs','/api/v1/runs/'+doc['runs'][0]['run_id']):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            try:
                connection.request('GET',path);response=connection.getresponse();payload=json.loads(response.read())
                self.assertEqual(response.status,503,payload);self.assertTrue(payload['error_code'].startswith('run_lookup_') or payload['error_code']=='run_notes_unavailable')
                self.assertNotIn('items',payload);self.assertNotIn('total',payload)
            finally:connection.close()
        self.assertEqual(snapshot(self.db),before)

    def test_pipeline_backfills_without_import_or_review_change_and_root_ownership(self):
        doc,raw=self.legacy(220);self.db=open_catalog(self.root);pipeline=Pipeline(self.root,self.db)
        pipeline.tick()
        initial=self.db.execute('SELECT COUNT(*) FROM run_lookup_runs WHERE revision=?',(doc['revision'],)).fetchone()[0]
        for _ in range(10):pipeline.tick()
        after=self.db.execute('SELECT COUNT(*) FROM run_lookup_runs WHERE revision=?',(doc['revision'],)).fetchone()[0]
        self.assertGreater(after,initial);self.assertEqual(after,len(doc['runs']))
        with closing(DataRootOwner(self.root)):
            with self.assertRaises(OwnershipError):DataRootOwner(self.root)
    def test_legacy_job_vm_exhaustion_does_not_stop_import_or_new_catalog_publication(self):
        from robot_test_hub.importer import Importer
        from robot_test_hub import import_retry
        doc,raw=self.legacy(120);self.db=open_catalog(self.root)
        source=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
        imported=Importer(self.root,self.db).import_file(source,source_type='SYNTHETIC')
        self.assertEqual(imported['state'],'succeeded')
        original_tick=run_lookup.tick
        def selected_job_budget(db):return original_tick(db,max_vm_steps=1000)
        pipeline=Pipeline(self.root,self.db)
        with patch.object(run_lookup,'tick',side_effect=selected_job_budget),patch.object(import_retry,'step',wraps=import_retry.step) as downstream:
            result=pipeline.tick()
            self.assertEqual(downstream.call_count,1)
            self.assertEqual(result['state'],'indexed');self.assertGreater(result['runs'],0);self.assertGreater(result['reports'],0)
            self.assertEqual(pipeline.tick()['state'],'idle');self.assertEqual(downstream.call_count,2)
        failed=self.db.execute('SELECT state,next_ordinal,error_code FROM run_lookup_jobs WHERE revision=?',(doc['revision'],)).fetchone()
        self.assertEqual(tuple(failed),('failed',0,'run_lookup_resource_limit'))
        self.assertEqual(self.db.execute('SELECT document FROM run_catalog_revisions WHERE id=?',(doc['revision'],)).fetchone()[0],raw)
        ready=self.db.execute('SELECT j.state FROM run_catalog_state s JOIN run_lookup_jobs j ON j.revision=s.revision WHERE s.id=1').fetchone()[0]
        self.assertEqual(ready,'ready');self.assertEqual(RunCatalog(self.db).search()['total'],result['runs'])

    def test_missing_or_modified_lookup_contract_rejected_by_open_and_backup(self):
        with self.db:self.db.execute('DROP TABLE run_lookup_intervals')
        self.db.close()
        with self.assertRaises(SchemaError):open_catalog(self.root)
        with self.assertRaises(BackupError):Backup(self.root,self.base/'backup').create('invalid')
        self.db=sqlite3.connect(self.root/'catalog.sqlite3')

if __name__=='__main__':unittest.main()
