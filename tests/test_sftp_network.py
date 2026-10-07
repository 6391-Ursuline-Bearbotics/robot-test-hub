"""Actual SSH authentication/SFTP on ephemeral loopback, with synthetic data only."""
import hashlib
import json
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest

try:
    import paramiko
except ImportError:
    paramiko=None

from robot_test_hub.collector import Collector
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.sftp_source import SFTPConfig, SFTPSource
from robot_test_hub.storage import open_catalog
from robot_test_hub.transfer import AuthenticationError, Cancellation, TransferCancelled
from test_sftp_source import FreshStatus, manifest_files, segment


class LoopbackSFTP:
    def __init__(self,folder,files):
        self.files=files;self.requests=[];self.connections=0
        self.read_started=threading.Event();self.release=threading.Event();self.block=False
        self.stop=threading.Event();self.transports=[];self.threads=[]
        self.host_key=paramiko.RSAKey.generate(2048)
        self.client_key=paramiko.RSAKey.generate(2048)
        self.key_path=Path(folder)/'client_key';self.client_key.write_private_key_file(str(self.key_path))
        self.listener=socket.socket();self.listener.bind(('127.0.0.1',0));self.listener.listen(5);self.listener.settimeout(.1)
        self.port=self.listener.getsockname()[1]
        self.known_hosts=Path(folder)/'known_hosts'
        self.known_hosts.write_text(f'[127.0.0.1]:{self.port} {self.host_key.get_name()} {self.host_key.get_base64()}\n')
        owner=self
        class Auth(paramiko.ServerInterface):
            def check_auth_publickey(self,username,key):
                return paramiko.AUTH_SUCCESSFUL if username=='reader' and key==owner.client_key else paramiko.AUTH_FAILED
            def get_allowed_auths(self,username):return 'publickey'
            def check_channel_request(self,kind,channel_id):
                return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        class Files(paramiko.SFTPServerInterface):
            def canonicalize(self,path):return path
            def stat(self,path):
                name=path.removeprefix('/logs/')
                if not path.startswith('/logs/') or name not in owner.files:return paramiko.SFTP_NO_SUCH_FILE
                attributes=paramiko.SFTPAttributes();attributes.st_mode=stat.S_IFREG|0o444;attributes.st_size=len(owner.files[name])
                return attributes
            lstat=stat
            def open(self,path,flags,attributes):
                name=path.removeprefix('/logs/')
                if flags&3 or not path.startswith('/logs/') or name not in owner.files:return paramiko.SFTP_PERMISSION_DENIED
                class Handle(paramiko.SFTPHandle):
                    def read(self,offset,length):
                        owner.requests.append((name,offset,length))
                        if name.endswith('.wpilog') and owner.block:
                            owner.read_started.set();owner.release.wait(3)
                        return owner.files[name][offset:offset+length]
                return Handle(flags)
        self.auth,self.files_type=Auth,Files
        self.thread=threading.Thread(target=self._accept,daemon=True);self.thread.start()
    def _accept(self):
        while not self.stop.is_set():
            try:client,_=self.listener.accept()
            except socket.timeout:continue
            except OSError:return
            self.connections+=1
            transport=paramiko.Transport(client);self.transports.append(transport)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler('sftp',paramiko.SFTPServer,self.files_type)
            def session(connection=transport):
                try:
                    connection.start_server(server=self.auth())
                    while connection.is_active() and not self.stop.wait(.02):pass
                except (EOFError,OSError,paramiko.SSHException):pass
                finally:connection.close()
            thread=threading.Thread(target=session,daemon=True);self.threads.append(thread);thread.start()
    def close(self):
        self.stop.set();self.release.set();self.listener.close()
        for transport in self.transports:transport.close()
        self.thread.join(2)
        for thread in self.threads:thread.join(2)


@unittest.skipIf(paramiko is None,'Install the optional pinned sftp extra for loopback SSH qualification')
class SFTPNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.payload=(Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog').read_bytes()
        self.entry=segment('synthetic-recording',data=self.payload)
        files=manifest_files([self.entry]);files[self.entry['relative_path']]=self.payload
        self.server=LoopbackSFTP(self.root,files);self.addCleanup(self.server.close);self.addCleanup(self.temp.cleanup)
        self.status=FreshStatus()
        self.config=SFTPConfig('127.0.0.1','reader',str(self.server.known_hosts),str(self.server.key_path),
            '/logs','robot-6391',port=self.server.port,timeout=.5)
        self.source=SFTPSource(self.config,self.status);self.addCleanup(self.source.close)
    def token(self):return Cancellation(((self.status.boot,self.status.generation),0),time.monotonic()+3)

    def test_actual_sftp_archive_resume_enable_and_import_pipeline(self):
        data_root=self.root/'hub';collector=Collector(data_root,self.source,idle_delay=0,chunk_size=512)
        try:
            collector.tick()
            offset=collector.snapshot()['files'][0]['offset'];self.assertEqual(offset,512)
            requests=len(self.server.requests);self.status.enabled=True;self.status.generation+=1
            collector.tick();self.assertEqual(len(self.server.requests),requests)
            collector.close()
            self.status.enabled=False;self.status.generation+=1
            collector=Collector(data_root,self.source,idle_delay=0,chunk_size=512)
            self.assertEqual(collector.snapshot()['files'][0]['offset'],offset)
            for _ in range(100):
                collector.tick()
                if collector.snapshot()['completed_files']==1:break
            self.assertEqual(collector.snapshot()['completed_files'],1)
            self.assertEqual((data_root/'archive/synthetic-recording.logdata').read_bytes(),self.payload)
            collector.close()
            db=open_catalog(data_root)
            try:
                indexed=Pipeline(data_root,db).tick();self.assertEqual(indexed['runs'],2)
            finally:db.close()
            self.assertEqual(self.server.connections,1)
            file_reads=[r for r in self.server.requests if r[0].endswith('.wpilog')]
            self.assertTrue(all(r[2]<=512 for r in file_reads))
            self.assertIn((self.entry['relative_path'],offset,512),file_reads)
        finally:collector.close()

    def test_actual_channel_cancellation_during_delayed_read_returns_no_chunk(self):
        self.source.discover_closed(None,100,self.token());self.server.block=True;errors=[]
        def read():
            try:self.source.read(self.entry['segment_id'],0,512,cancellation=self.token())
            except BaseException as exc:errors.append(exc)
        thread=threading.Thread(target=read);thread.start()
        self.assertTrue(self.server.read_started.wait(2))
        self.status.enabled=True;self.status.generation+=1
        thread.join(2);self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],TransferCancelled)
        self.server.release.set()

    def test_unplanned_disconnect_with_fresh_status_retries_from_durable_offset(self):
        collector=Collector(self.root/'hub',self.source,idle_delay=0,chunk_size=512,
                            retry_initial=.01,retry_max=.02,jitter=lambda:1)
        self.addCleanup(collector.close)
        collector.tick()
        offset=collector.snapshot()['files'][0]['offset']
        self.assertEqual(offset,512)
        self.server.block=True
        disconnected=threading.Event()
        def drop_connection():
            if self.server.read_started.wait(2):
                for transport in self.server.transports:transport.close()
                disconnected.set()
        thread=threading.Thread(target=drop_connection)
        thread.start()
        try:
            # This must stay a recoverable file failure, not escape tick and
            # terminate the service worker while robot permission remains fresh.
            collector.tick()
        finally:
            thread.join(2)
            self.server.block=False
            self.server.release.set()
        self.assertTrue(disconnected.is_set())
        row=collector.snapshot()['files'][0]
        self.assertEqual(row['offset'],offset)
        self.assertEqual(row['state'],'queued')
        self.assertEqual(row['error_code'],'transient')
        self.assertIsNone(self.source.session)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            collector.tick()
            if collector.snapshot()['completed_files']==1:break
            time.sleep(.005)
        self.assertEqual(collector.snapshot()['completed_files'],1)
        self.assertEqual((self.root/'hub/archive/synthetic-recording.logdata').read_bytes(),self.payload)
        self.assertGreaterEqual(self.server.connections,2)
        self.assertGreaterEqual(self.server.requests.count((self.entry['relative_path'],offset,512)),2)

    def test_wrong_host_key_and_unlisted_host_fail_auth_without_file_reads(self):
        key=paramiko.RSAKey.generate(2048)
        self.server.known_hosts.write_text(f'[127.0.0.1]:{self.server.port} {key.get_name()} {key.get_base64()}\n')
        with self.assertRaises(AuthenticationError):self.source.discover_closed(None,100,self.token())
        self.server.known_hosts.write_text('')
        with self.assertRaises(AuthenticationError):self.source.discover_closed(None,100,self.token())
        self.assertEqual(self.server.requests,[])

    def test_wrong_credential_is_attention_and_stale_status_prevents_reconnect(self):
        key=paramiko.RSAKey.generate(2048);key.write_private_key_file(str(self.server.key_path))
        with self.assertRaises(AuthenticationError):self.source.discover_closed(None,100,self.token())
        connections=self.server.connections;self.status.stale=True
        with self.assertRaises(TransferCancelled):self.source.discover_closed(None,100,self.token())
        self.assertEqual(self.server.connections,connections);self.assertEqual(self.server.requests,[])
