"""Independent corruption oracles and isolation checks for the offline exercise."""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub import selftest
from robot_test_hub.collector import Collector  # Bind its real storage factory before fault injection.
from urllib.parse import urlparse, unquote
import os


class SelftestReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='offline review ')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.roots=[]
        original=tempfile.mkdtemp
        def allocate(*args,**kwargs):
            kwargs['dir']=self.base
            result=original(*args,**kwargs)
            self.roots.append(Path(result).resolve())
            return result
        self.allocation=patch.object(selftest.tempfile,'mkdtemp',side_effect=allocate)
        self.allocation.start()
        self.addCleanup(self.allocation.stop)

    def check_result(self,result,failed=None):
        self.assertEqual(result['cleanup'],{'state':'passed'},result)
        self.assertTrue(all(not root.exists() for root in self.roots))
        self.assertEqual(result['success'],failed is None,result)
        if failed:
            rows={row['id']:row for row in result['stages']}
            self.assertEqual(rows[failed]['state'],'failed',result)
        encoded=json.dumps(result)
        self.assertLess(len(encoded),6000)
        self.assertNotIn(str(self.base),encoded)
        self.assertNotIn('private-fault',encoded)

    def test_interior_cycle_corruption_cannot_pass_known_timestamp_oracle(self):
        from robot_test_hub.importer import Importer
        original=Importer.iter_dataset
        changed=[]
        def corrupted(importer,*args,**kwargs):
            for row in original(importer,*args,**kwargs):
                if row['kind']=='cycle' and row['timestamp_ns']=='1040000000':
                    row=dict(row,timestamp_ns='1040000999');changed.append(True)
                yield row
        with patch.object(Importer,'iter_dataset',corrupted):
            result=selftest.run_selftest()
        self.assertTrue(changed)
        self.check_result(result,'import_index')

    def test_restored_raw_note_and_exact_report_corruption_are_detected(self):
        from robot_test_hub import backup
        original=backup.restore
        for kind in ('raw_transfer','raw_import','note','report'):
            with self.subTest(kind=kind):
                def corrupted(source,destination,*args,**kwargs):
                    result=original(source,destination,*args,**kwargs)
                    root=Path(destination)
                    if kind in ('raw_transfer','raw_import'):
                        paths=list(root.rglob('*.logdata')) if kind=='raw_transfer' else list((root/'raw').rglob('*.wpilog'))
                        self.assertEqual(len(paths),1)
                        data=paths[0].read_bytes()
                        paths[0].write_bytes(bytes([data[0]^1])+data[1:])
                    else:
                        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db,db:
                            if kind=='note':
                                row=db.execute('SELECT payload_json FROM annotation_revisions').fetchone()
                                note=json.loads(row[0]);note['event_utc_start_ns']='1791003600020000001'
                                db.execute('UPDATE annotation_revisions SET payload_json=?',(json.dumps(note),))
                            else:
                                row=db.execute('SELECT report_id,result_json FROM analysis_reports LIMIT 1').fetchone()
                                report=json.loads(row[1]);report['private-fault']='changed'
                                db.execute('UPDATE analysis_reports SET result_json=? WHERE report_id=?',(json.dumps(report),row[0]))
                    return result
                with patch.object(backup,'restore',corrupted):
                    result=selftest.run_selftest()
                self.check_result(result,'backup_restore')

    def test_every_database_is_fresh_local_and_service_network_are_unreachable(self):
        from robot_test_hub.config import Config
        from robot_test_hub.service import HubService
        from robot_test_hub.sftp_source import SFTPSource
        from robot_test_hub.status_bridge import StatusBridge
        from robot_test_hub.video_media import NativeMediaAdapter
        original=sqlite3.connect
        opened=[]
        def connect(database,*args,**kwargs):
            value=str(database)
            if value.startswith('file:'):
                value=unquote(urlparse(value).path).lstrip('/') if os.name=='nt' else unquote(urlparse(value).path)
            resolved=Path(value).resolve()
            self.assertTrue(any(root in resolved.parents for root in self.roots),resolved)
            opened.append(resolved)
            return original(database,*args,**kwargs)
        before={thread.ident for thread in threading.enumerate()}
        with ExitStack() as stack:
            for target,name in ((socket,'socket'),(socket,'create_connection'),(socket,'getaddrinfo'),(subprocess,'Popen'),(Config,'load'),(HubService,'__init__'),(SFTPSource,'__init__'),(StatusBridge,'__init__'),(NativeMediaAdapter,'__init__')):
                stack.enter_context(patch.object(target,name,side_effect=AssertionError('private-fault forbidden external access')))
            stack.enter_context(patch.object(sqlite3,'connect',side_effect=connect))
            result=selftest.run_selftest()
        self.check_result(result)
        self.assertGreater(len(opened),5)
        self.assertEqual({thread.ident for thread in threading.enumerate()},before)

    def test_catalog_close_failure_retains_root_until_manual_cleanup(self):
        from robot_test_hub import storage
        original=storage.open_catalog
        handles=[]
        class PendingClose:
            def __init__(self,connection):self.connection=connection
            def __getattr__(self,key):return getattr(self.connection,key)
            def close(self):raise OSError('private-fault pending SQL handle')
        def opened(root):
            proxy=PendingClose(original(root));handles.append(proxy)
            return proxy
        initial=len(selftest._RETAINED_CLEANUP)
        try:
            with patch.object(storage,'open_catalog',side_effect=opened):
                result=selftest.run_selftest()
            self.assertFalse(result['success'])
            self.assertEqual(result['cleanup'],{'state':'failed'})
            self.assertEqual(len(selftest._RETAINED_CLEANUP),initial+1)
            workspace,collector,db,owner=selftest._RETAINED_CLEANUP[-1]
            self.assertTrue(workspace.exists())
            self.assertIsNotNone(owner)
            with self.assertRaises(storage.OwnershipError):storage.DataRootOwner(workspace/'archive')
            self.assertNotIn('private-fault',json.dumps(result))
        finally:
            for proxy in handles:proxy.connection.close()
            while len(selftest._RETAINED_CLEANUP)>initial:
                workspace,collector,db,owner=selftest._RETAINED_CLEANUP.pop()
                if collector is not None:collector.close()
                if owner is not None:owner.close()
                shutil.rmtree(workspace)


    def test_restored_catalog_close_failure_keeps_its_distinct_owner(self):
        from robot_test_hub import storage
        original=storage.open_catalog
        handles=[]
        class PendingClose:
            def __init__(self,connection):self.connection=connection
            def __getattr__(self,key):return getattr(self.connection,key)
            def close(self):raise OSError('private-fault restored SQL handle')
        def opened(root):
            db=original(root)
            if Path(root).name!='restored':return db
            proxy=PendingClose(db);handles.append(proxy)
            return proxy
        initial=len(selftest._RETAINED_CLEANUP)
        try:
            with patch.object(storage,'open_catalog',side_effect=opened):
                result=selftest.run_selftest()
            self.assertFalse(result['success'])
            self.assertEqual(result['cleanup'],{'state':'failed'})
            self.assertTrue(handles)
            retained=selftest._RETAINED_CLEANUP[initial:]
            self.assertTrue(any(owner is not None and owner.root.name=='restored' for _,_,_,owner in retained))
            restored=self.roots[-1]/'restored'
            self.assertTrue(restored.exists())
            with self.assertRaises(storage.OwnershipError):storage.DataRootOwner(restored)
            self.assertNotIn('private-fault',json.dumps(result))
        finally:
            for proxy in handles:proxy.connection.close()
            workspaces=set()
            while len(selftest._RETAINED_CLEANUP)>initial:
                workspace,collector,db,owner=selftest._RETAINED_CLEANUP.pop()
                if collector is not None:collector.close()
                if db is not None:
                    if isinstance(db,PendingClose):db.connection.close()
                    else:db.close()
                if owner is not None:owner.close()
                if workspace is not None:workspaces.add(workspace)
            for workspace in workspaces:
                if workspace.exists():shutil.rmtree(workspace)

if __name__=='__main__':unittest.main()
