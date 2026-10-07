import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from robot_test_hub.backup import BackupScheduler
from robot_test_hub.config import Config, ConfigError
from robot_test_hub.demo import DemoSource
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Worker did not reach expected state')
        time.sleep(.01)


class BackupSchedulerTests(unittest.TestCase):
    def test_scheduler_interval_failure_retry_restart_and_no_generation_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); root=base/'source'; root.mkdir(); destination=base/'replica'
            db=open_catalog(root); db.close()
            ns=[1_000_000_000_000]; mono=[0]
            kwargs=dict(interval=10,retry=2,clock_ns=lambda:ns[0],monotonic=lambda:mono[0])
            worker=BackupScheduler(root,destination,**kwargs)
            destination.write_text('offline fixture')
            self.assertTrue(worker.tick())
            generation=worker.snapshot['active_generation']
            self.assertEqual(worker.snapshot['state'],'retry_wait')
            self.assertFalse(worker.tick())
            self.assertEqual(len(worker.backup.status()),1)
            destination.unlink()  # only fixture cleanup; backup itself never deletes.
            restarted=BackupScheduler(root,destination,**kwargs)
            self.assertTrue(restarted.tick())
            self.assertEqual(restarted.snapshot['last_generation'],generation)
            self.assertFalse(restarted.snapshot['failure_domain_qualified'])
            ns[0]+=9_000_000_000;mono[0]+=9
            self.assertFalse(restarted.tick())
            ns[0]+=1_000_000_000;mono[0]+=1
            self.assertTrue(restarted.tick())
            self.assertEqual(len(restarted.backup.status()),2)
            self.assertTrue((destination/generation/'manifest.json').exists())

    def test_configuration_opt_in_and_types(self):
        self.assertIsNone(Config().backup_destination)
        for value in ('',True,{},'data/demo','data/demo/backup'):
            with self.assertRaises(ConfigError):
                Config(backup_destination=value)
        for key in ('backup_interval','backup_retry'):
            for value in (True,0,-1,float('nan'),float('inf'),'1'):
                with self.assertRaises(ConfigError):
                    Config(**{key:value})

    def test_service_default_disabled_creates_no_backup_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            service=HubService(Config(data_dir=folder),DemoSource())
            try:
                service.start()
                self.assertEqual(service.snapshot()['backup']['state'],'disabled')
                self.assertEqual(service.snapshot()['workers']['backup']['state'],'disabled')
                self.assertFalse((Path(folder)/'exports/backup-jobs').exists())
            finally:
                self.assertTrue(service.close())

    def test_enabled_service_backup_status_api_and_originals_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'source';destination=base/'replica'
            config=Config(data_dir=str(root),backup_destination=str(destination),backup_interval=3600,backup_retry=.1)
            service=HubService(config,DemoSource())
            service.set_paused(True)
            server=create_http_server(service,service.source,0)
            http_thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});http_thread.start()
            try:
                service.start()
                wait_for(lambda:service.snapshot()['backup'].get('last_completed_utc_ns') is not None)
                snapshot=service.snapshot()['backup']
                self.assertTrue(snapshot['enabled']);self.assertGreaterEqual(snapshot['capture_age_seconds'],0)
                self.assertNotIn('destination',snapshot)
                connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=2)
                try:
                    connection.request('GET','/api/v1/backup');response=connection.getresponse()
                    self.assertEqual(response.status,200)
                    payload=json.loads(response.read())
                    self.assertEqual(payload['last_generation'],snapshot['last_generation'])
                    self.assertFalse(payload['failure_domain_qualified'])
                finally:
                    connection.close()
                self.assertTrue((destination/snapshot['last_generation']/'manifest.json').is_file())
                self.assertTrue(service.paused.is_set())
            finally:
                server.shutdown();http_thread.join(2);server.server_close();self.assertTrue(service.close())

    def test_local_backup_does_not_block_pause_and_shutdown_retains_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);entered=threading.Event();release=threading.Event()
            def blocked(backup,generation,**kwargs):
                entered.set();release.wait(3)
                raise OSError('credential text must not be exported')
            with patch('robot_test_hub.backup.Backup.create',blocked):
                service=HubService(Config(data_dir=str(base/'source'),backup_destination=str(base/'replica'),shutdown_timeout=.05),DemoSource())
                try:
                    service.start();self.assertTrue(entered.wait(2))
                    service.set_paused(True)
                    self.assertTrue(service.snapshot()['paused_by_operator'])
                    self.assertEqual(service.snapshot()['backup']['state'],'running')
                    self.assertFalse(service.close())
                    self.assertFalse(service.owner.file.closed)
                finally:
                    release.set();wait_for(lambda:not any(thread.is_alive() for thread in service.threads));self.assertTrue(service.close())
                self.assertNotIn('credential',json.dumps(service.backup_cache))


if __name__=='__main__':
    unittest.main()

