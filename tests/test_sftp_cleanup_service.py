"""Synthetic SFTP close ownership through the real service and loopback HTTP."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.request import urlopen

from robot_test_hub.config import Config
from robot_test_hub.storage import DataRootOwner, OwnershipError
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.sftp_source import SFTPConfig, SFTPSource
from test_sftp_source import FreshStatus, MemoryClient, MemorySFTP, manifest_files, segment


class SFTPCleanupServiceTests(unittest.TestCase):
    def test_cancelled_read_keeps_service_owner_until_transport_close_finishes(self):
        temporary=tempfile.TemporaryDirectory()
        root=Path(temporary.name)
        read_entered=threading.Event()
        release_read=threading.Event()
        read_unwound=threading.Event()
        close_entered=threading.Event()
        collector_finished=threading.Event()
        release_close=threading.Event()
        clients=[]
        resources={}

        def cleanup():
            # Release both fixture operations before joining; never remove an
            # owned directory or hide an unsuccessful final service shutdown.
            release_read.set()
            release_close.set()
            service=resources.get('service')
            try:
                if service is not None:
                    deadline=time.monotonic()+10
                    while not service.close():
                        if time.monotonic()>=deadline:
                            self.fail('Synthetic SFTP service cleanup did not finish')
                        time.sleep(.01)
                    self.assertTrue(all(not worker.is_alive() for worker in service.threads))
            finally:
                httpd=resources.get('httpd')
                if httpd is not None:
                    httpd.shutdown()
                    httpd.server_close()
                    resources['http_thread'].join(5)
                    self.assertFalse(resources['http_thread'].is_alive())
            temporary.cleanup()
        self.addCleanup(cleanup)

        status=FreshStatus()
        payload=b'synthetic recording fixture '*100
        entry=segment(data=payload)
        files=manifest_files([entry])
        files[entry['relative_path']]=payload

        class TrackingSFTP(MemorySFTP):
            def open(inner,*args,**kwargs):
                stream=super().open(*args,**kwargs)
                is_recording=args[0].endswith('.wpilog')
                class TrackedFile:
                    def __getattr__(self,name):return getattr(stream,name)
                    def __enter__(self):return self
                    def __exit__(self,*exception):
                        result=stream.__exit__(*exception)
                        if is_recording and read_entered.is_set():
                            read_unwound.set()
                        return result
                return TrackedFile()
        remote=TrackingSFTP(files)
        def block_second_read():
            if len([name for name,size in remote.reads if name.endswith('.wpilog')])==2:
                read_entered.set()
                if not release_read.wait(20):
                    raise AssertionError('Synthetic read fixture was not released')
        remote.block=block_second_read

        class BlockingCloseClient(MemoryClient):
            def close(inner):
                close_entered.set()
                if not release_close.wait(20):
                    raise AssertionError('Synthetic transport close fixture was not released')
                super().close()
        def factory():
            client=BlockingCloseClient(remote)
            clients.append(client)
            return client
        source=SFTPSource(SFTPConfig('configured-host','reader','known-hosts','private-key','/logs','robot-6391',timeout=.05),
                          status,client_factory=factory)
        service=HubService(Config(data_dir=str(root),idle_delay=0,chunk_size=512,
            shutdown_timeout=.05,status_interval=.01,io_timeout=10),source)
        resources['service']=service
        collector_worker=service._collector_worker
        def observe_collector_exit():
            try:
                collector_worker()
            finally:
                collector_finished.set()
        service._collector_worker=observe_collector_exit
        httpd=create_http_server(service,source,0)
        resources['httpd']=httpd
        http_thread=threading.Thread(target=httpd.serve_forever,name='synthetic-sftp-cleanup-http')
        resources['http_thread']=http_thread
        http_thread.start()
        service.start()

        def checkpoint():
            with sqlite3.connect(root/'catalog.sqlite3',timeout=2) as db:
                return db.execute('SELECT offset FROM files WHERE id=?',(entry['segment_id'],)).fetchone()[0]
        def http_status():
            began=time.monotonic()
            with urlopen(f'http://127.0.0.1:{httpd.server_address[1]}/api/v1/status',timeout=2) as response:
                result=json.load(response)
            self.assertLess(time.monotonic()-began,2)
            return result

        self.assertTrue(read_entered.wait(10),'Second remote read never began')
        prior=checkpoint()
        self.assertEqual(prior,512)
        self.assertEqual(len(clients),1)
        status.enabled=True
        status.generation+=1
        self.assertTrue(close_entered.wait(5),'Cancellation watcher never entered transport close')
        release_read.set()
        self.assertTrue(read_unwound.wait(5),'Remote read body did not unwind')
        self.assertTrue(source.operation_lock.locked())
        self.assertFalse(clients[0].closed)
        self.assertFalse(service.close())
        with self.assertRaises(OwnershipError):
            DataRootOwner(root)
        # Keep transport close pending beyond the former .15-second watcher
        # join. The worker must remain owned after its request body has unwound.
        self.assertFalse(collector_finished.wait(.5))
        self.assertFalse(service.close())
        with self.assertRaises(OwnershipError):
            DataRootOwner(root)
        self.assertEqual(checkpoint(),prior)
        self.assertEqual(len(clients),1)
        self.assertEqual(len([name for name,size in remote.reads if name.endswith('.wpilog')]),2)
        result=http_status()
        self.assertEqual(result['state'],'stopping')
        self.assertIsNone(result['bytes_per_second'])
        self.assertIsNone(result['eta_seconds'])
        self.assertIn('shutdown_timeout',json.dumps(service.diagnostics.snapshot()))

        release_close.set()
        for worker in service.threads:
            worker.join(5)
        self.assertTrue(all(not worker.is_alive() for worker in service.threads))
        self.assertTrue(service.close())
        self.assertTrue(clients[0].closed)
        self.assertFalse(source.operation_lock.locked())
        self.assertEqual(checkpoint(),prior)
        partial=list(root.rglob('*.part'))
        self.assertEqual(len(partial),1)
        self.assertEqual(partial[0].read_bytes(),payload[:prior])
        owner=DataRootOwner(root)
        owner.close()


if __name__=='__main__':
    unittest.main()
