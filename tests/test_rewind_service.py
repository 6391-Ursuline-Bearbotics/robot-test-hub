"""LL4 hub integration: synthetic transport only, never a hardware endpoint."""
from contextlib import closing
from dataclasses import replace
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from robot_test_hub.backup import Backup,restore,verify
from robot_test_hub.collector import RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.rewind import RewindConfig,RewindHttpTransport
from robot_test_hub.rewind_service import load_rewind_config,RewindWorker
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService,CachedSource
from robot_test_hub.storage import open_catalog,DataRootOwner,OwnershipError,SCHEMA_VERSION
from test_foundation import SmallSource,wait_for


class LocalCamera:
    def __init__(self,config):
        self.calls=[];self.flushing=False;self.mutate=False
        self.files={"clip.avi":b"RIFF-invented-video","clip_manifest.jsonl":b'{"fidx":1,"ts_us":123}\n'}
        self.entry={"video":"clip.avi","manifest":"clip_manifest.jsonl","bootlog":None,"size":sum(map(len,self.files.values()))}
    def state(self,cancel=lambda:False):
        self.calls.append("state")
        return {"rewind":{"flushing":int(self.flushing),"storedSeconds":180},"ts_us":1000}
    is_flushing=staticmethod(RewindHttpTransport.is_flushing)
    def list_recordings(self,cancel=lambda:False):
        self.calls.append("list")
        return [dict(self.entry)]
    def flush(self,duration,cancel=lambda:False):
        self.calls.append("flush")
        return {"success":True}
    def download(self,name,destination,cancel=lambda:False,max_bytes=None):
        import hashlib
        self.calls.append("download")
        data=self.files[name];Path(destination).write_bytes(data)
        return {"size_bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
    def abort(self):self.calls.append("abort")


class RewindServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"data"
        self.camera=RewindConfig("ll4","http://127.0.0.1:5807","robot",source_type="SYNTHETIC",poll_interval_seconds=.1)

    def service(self,**options):
        source=SmallSource()
        hub_options=options.pop("hub_options",{})
        service=HubService(Config(data_dir=str(self.root),idle_delay=.05,freshness=1.,status_interval=.01,**hub_options),
            source,**options)
        self.addCleanup(service.close)
        return service,source

    def test_default_off_never_constructs_transport_and_api_redacted(self):
        with patch("robot_test_hub.rewind_service.RewindHttpTransport",side_effect=AssertionError()):
            service,_=self.service()
            self.assertEqual(service.snapshot()["rewind"]["state"],"disabled")
            self.assertFalse((self.root/"rewind").exists())

    def test_real_camera_cannot_follow_demo_status_and_owner_released(self):
        with self.assertRaises(ValueError):
            self.service(rewind_config=replace(self.camera,source_type="REAL"))
        owner=DataRootOwner(self.root);owner.close()

    def test_config_requires_explicit_optin_version_and_safe_endpoint(self):
        path=Path(self.temp.name)/"private.json"
        self.assertIsNone(load_rewind_config(None))
        base=dict(schema_version=1,enabled=True,camera_id="ll4",origin="http://127.0.0.1:5807",robot_id="robot",source_type="SYNTHETIC")
        for changes in (dict(enabled=False),dict(schema_version=True),dict(origin="http://user:secret@camera:5807"),dict(unknown="secret")):
            path.write_text(json.dumps({**base,**changes}),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"Invalid private Rewind"):
                load_rewind_config(path)
        path.write_text(json.dumps(base),encoding="utf-8")
        self.assertEqual(load_rewind_config(path),replace(self.camera,poll_interval_seconds=5.))
        path.write_text('{"schema_version":1,"enabled":true,"enabled":false}',encoding="utf-8")
        with self.assertRaises(ValueError):load_rewind_config(path)

    def test_shared_network_slot_cannot_start_robot_request_during_video(self):
        source=SmallSource();bulk=threading.Lock();cached=CachedSource(source,bulk)
        entered=threading.Event();finished=threading.Event()
        original=source.list_closed_files
        def read():
            entered.set()
            return original()
        source.list_closed_files=read
        with bulk:
            worker=threading.Thread(target=lambda:(cached.list_closed_files(),finished.set()));worker.start()
            self.assertFalse(entered.wait(.05))
        self.assertTrue(finished.wait(1));worker.join();self.assertTrue(entered.is_set())

    def test_service_archive_status_and_coherent_backup_restore(self):
        made=[]
        def factory(config):
            camera=LocalCamera(config);made.append(camera);return camera
        service,source=self.service(rewind_config=self.camera,rewind_transport_factory=factory)
        server=create_http_server(service,source,0);thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            service.start()
            wait_for(lambda:service.snapshot()["rewind"]["archived_recordings"]==1)
            status=service.snapshot()["rewind"]
            self.assertEqual(status["recent_recordings"][0]["size_bytes"],sum(map(len,made[0].files.values())))
            self.assertIsInstance(status["recent_recordings"][0]["created_utc_ns"],str)
            self.assertFalse(status["physical_camera_qualified"])
            self.assertNotIn("127.0.0.1",json.dumps(status))
            connection=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=2)
            connection.request("GET","/api/v1/rewind");response=connection.getresponse()
            self.assertEqual(response.status,200);self.assertEqual(json.loads(response.read())["archived_recordings"],1);connection.close()
            # Existing originals can be backed up coherently while workers own their archive.
            result=Backup(self.root,Path(self.temp.name)/"backup").create("ll4")
            self.assertEqual(result["state"],"complete")
            folder=Path(self.temp.name)/"backup/ll4";verify(folder)
            destination=Path(self.temp.name)/"restored";restore(folder,destination)
            self.assertEqual(len(list(destination.glob("rewind/groups/*/clip.avi"))),1)
            self.assertEqual(next(destination.glob("rewind/groups/*/clip.avi")).read_bytes(),made[0].files["clip.avi"])
        finally:
            server.shutdown();thread.join();server.server_close()
            self.assertTrue(service.close())

    def test_camera_slot_wait_is_not_a_failed_robot_transfer(self):
        from robot_test_hub.collector import Collector
        source=SmallSource();camera_network=threading.Event()
        source_cache=CachedSource(source,camera_network=camera_network)
        source_cache.update(source.status())
        self.root.mkdir()
        collector=Collector(self.root,source_cache,idle_delay=0,freshness=100)
        try:
            camera_network.set()
            for _ in range(3):collector.tick()
            self.assertEqual(collector.state,"waiting")
            self.assertIn("shared network slot",collector.reason)
            self.assertFalse(collector.io.busy)
            self.assertEqual(collector.db.execute("SELECT COUNT(*) FROM transfer_meta WHERE attempts>0").fetchone()[0],0)
            camera_network.clear()
            self.assertTrue(collector._gate())
        finally:collector.close()

    def test_status_worker_failure_revokes_camera_permission_immediately(self):
        service,source=self.service(rewind_config=self.camera,rewind_transport_factory=LocalCamera)
        service.start()
        try:
            wait_for(lambda:service.snapshot()["rewind"]["archived_recordings"]==1)
            self.assertTrue(service.rewind._permission(idle=True))
            source.fail="status"
            wait_for(lambda:service.snapshot()["workers"]["status"]["state"]=="failed")
            self.assertFalse(service.rewind._permission(idle=True))
        finally:self.assertTrue(service.close())

    def test_blocked_camera_keeps_archive_owner_until_worker_finishes(self):
        entered=threading.Event();release=threading.Event()
        class BlockedCamera(LocalCamera):
            def download(self,*args,**kwargs):
                entered.set()
                if not release.wait(5):raise RuntimeError("Test did not release camera")
                return super().download(*args,**kwargs)
        service,_=self.service(rewind_config=self.camera,rewind_transport_factory=BlockedCamera,
            hub_options={"shutdown_timeout":.05})
        service.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse(service.cached_source.bulk_ready())
            self.assertFalse(service.close())
            with self.assertRaises(OwnershipError):DataRootOwner(self.root)
        finally:
            release.set()
            wait_for(lambda:all(not thread.is_alive() for thread in service.threads))
            self.assertTrue(service.close())
        owner=DataRootOwner(self.root);owner.close()

    def test_native_migration_six_failure_rolls_back_version_and_ddl(self):
        self.root.mkdir()
        db=open_catalog(self.root)
        with db:
            for name in ("rewind_capture_requests","rewind_recordings","rewind_observations","rewind_camera_context"):
                db.execute("DROP TABLE "+name)
            db.execute("DELETE FROM schema_migrations WHERE version>=6")
            db.execute("PRAGMA user_version=5")
        db.close()
        def fail(db):
            db.execute("CREATE TABLE accidental_partial(x)")
            raise RuntimeError("Injected migration failure")
        with patch("robot_test_hub.rewind_service.install_schema",side_effect=fail):
            with self.assertRaises(RuntimeError):open_catalog(self.root)
        import sqlite3
        with closing(sqlite3.connect(self.root/"catalog.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],5)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='accidental_partial'").fetchone())
        with closing(open_catalog(self.root)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],SCHEMA_VERSION)


if __name__=="__main__":unittest.main()
