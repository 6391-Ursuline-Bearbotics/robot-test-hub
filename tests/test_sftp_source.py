"""Transport contract tests; genuine loopback SFTP tests are in test_sftp_network."""
import hashlib
from dataclasses import replace
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from robot_test_hub.collector import RobotStatus
from robot_test_hub.sftp_source import SFTPConfig, SFTPSource, SourceAttention
from robot_test_hub.transfer import BoundedIO, Cancellation, IdentityError, TransferCancelled, TransferError, TransferTimeout
from robot_test_hub.wpilog import PROFILE


def encode(value):
    return json.dumps(value,sort_keys=True).encode()


def segment(identity='segment-1',data=b'robot recording',**changes):
    result=dict(segment_id=identity,robot_id='robot-6391',boot_id='boot-old',relative_path=identity+'.wpilog',
        original_name=identity+'.wpilog',state='closed',sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),created_at='2026-10-06T18:00:00Z',format='wpilog',format_profile=PROFILE)
    result.update(changes)
    return result


def manifest_files(entries,**changes):
    page=encode(dict(schema_version=1,entries=entries));digest=hashlib.sha256(page).hexdigest()
    path='manifest-page-'+digest+'.json'
    root=dict(schema_version=1,robot_id='robot-6391',boot_id='boot-current',manifest_revision='1',segments_complete=True,
        pages=[dict(relative_path=path,sha256=digest,size_bytes=len(page))],open_segment=None)
    root.update(changes)
    return {'manifest.json':encode(root),path:page}


class FreshStatus:
    def __init__(self):
        self.enabled=False;self.generation=1;self.boot='boot-current';self.stale=False
    def status(self):
        return RobotStatus(self.enabled,time.monotonic()-(10 if self.stale else 0),self.boot,self.generation,not self.enabled)


class MemorySFTP:
    def __init__(self,files):
        self.files=files;self.reads=[];self.symlinks=set();self.block=None
    def get_channel(self):return self
    def settimeout(self,timeout):self.timeout=timeout
    def normalize(self,path):return path
    def lstat(self,path):
        name=path.split('/')[-1]
        if name not in self.files:raise FileNotFoundError()
        return SimpleNamespace(st_mode=stat.S_IFLNK if name in self.symlinks else stat.S_IFREG,st_size=len(self.files[name]))
    def open(self,path,mode,bufsize):
        owner=self;name=path.split('/')[-1]
        class File(io.BytesIO):
            def read(self,size=-1):
                owner.reads.append((name,size))
                value=super().read(size)
                if name.endswith('.wpilog') and owner.block is not None:owner.block()
                return value
        return File(self.files[name])


class MemoryClient:
    def __init__(self,sftp):self.sftp=sftp;self.connects=0;self.closed=False
    def connect(self,**kwargs):self.connects+=1;self.arguments=kwargs
    def open_sftp(self):return self.sftp
    def close(self):self.closed=True


class SFTPSourceTests(unittest.TestCase):
    def setUp(self):
        self.status=FreshStatus();self.payload=b'recording'*10000
        self.entry=segment(data=self.payload)
        files=manifest_files([self.entry]);files[self.entry['relative_path']]=self.payload
        self.remote=MemorySFTP(files);self.clients=[]
        def factory():
            client=MemoryClient(self.remote);self.clients.append(client);return client
        self.source=SFTPSource(SFTPConfig('configured-host','reader','known-hosts','private-key','/logs','robot-6391'),
            self.status,client_factory=factory)
        self.addCleanup(self.source.close)
    def token(self):return Cancellation(((self.status.boot,self.status.generation),0),time.monotonic()+3)

    def test_real_manifest_dates_pagination_and_connection_reuse(self):
        page=self.source.discover_closed(None,100,self.token())
        self.assertTrue(page.complete);self.assertEqual(page.files[0].created_at,1791309600.)
        for offset in (0,32768):
            data=self.source.read(self.entry['segment_id'],offset,32768,cancellation=self.token())
            self.assertEqual(data,self.payload[offset:offset+32768])
        self.assertEqual(len(self.clients),1)
        self.assertEqual(self.clients[0].connects,1)
        self.assertFalse(self.clients[0].arguments['allow_agent'])
        self.assertFalse(self.clients[0].arguments['look_for_keys'])
        self.assertTrue(all(size<=32768 for name,size in self.remote.reads))

    def test_disabled_gate_revokes_midread_and_reconnects_without_returning_data(self):
        self.source.discover_closed(None,100,self.token())
        def enable():self.status.enabled=True;self.status.generation+=1
        self.remote.block=enable
        with self.assertRaises(TransferCancelled):
            self.source.read(self.entry['segment_id'],0,32768,cancellation=self.token())
        self.assertTrue(self.clients[0].closed)
        self.status.enabled=False;self.status.generation+=1;self.remote.block=None
        self.assertEqual(self.source.read(self.entry['segment_id'],0,100,cancellation=self.token()),self.payload[:100])
        self.assertEqual(len(self.clients),2)

    def test_stale_enabled_unknown_and_generation_revoke_before_network(self):
        for enabled,stale in ((True,False),(None,False),(False,True)):
            self.status.enabled=enabled;self.status.stale=stale
            with self.assertRaises(TransferCancelled):self.source.discover_closed(None,100,self.token())
        self.assertEqual(len(self.clients),0)

    def test_pinned_revision_pages_hold_during_root_update_and_pending_bytes_remain_separate(self):
        entries=[segment('a'),segment('b'),segment('c',state='closed_pending_digest',sha256=None)]
        self.remote.files.update(manifest_files(entries,open_segment={'size_bytes':500}))
        first=self.source.discover_closed(None,1,self.token())
        self.assertFalse(first.complete);self.assertEqual(first.open_bytes,500)
        self.remote.files.update(manifest_files([],manifest_revision='99'))
        second=self.source.discover_closed(first.next_cursor,100,self.token())
        self.assertTrue(second.complete);self.assertEqual(second.revision,first.revision)
        self.assertEqual([f.id for f in second.files],['b']);self.assertEqual(second.pending_digest_bytes,15)
        repeated=self.source.discover_closed(first.next_cursor,100,self.token())
        self.assertEqual(repeated.pending_digest_bytes,15)
        for position,offset in ((999,0),(0,999)):
            with self.assertRaises(ValueError):
                self.source.discover_closed(json.dumps([first.revision,position,offset]),100,self.token())

    def test_tampered_page_symlink_identity_and_read_range_rejected(self):
        root=json.loads(self.remote.files['manifest.json']);page=root['pages'][0]['relative_path']
        self.remote.files[page]=b'x'*len(self.remote.files[page])
        with self.assertRaises(IdentityError):self.source.discover_closed(None,100,self.token())
        self.remote.files.update(manifest_files([self.entry]))
        self.source.discover_closed(None,100,self.token())
        with self.assertRaises(ValueError):self.source.read(self.entry['segment_id'],0,262145,cancellation=self.token())
        self.remote.symlinks.add(self.entry['relative_path'])
        with self.assertRaises(IdentityError):self.source.read(self.entry['segment_id'],0,10,cancellation=self.token())

    def test_bad_manifest_metadata_and_orphans_cannot_claim_caught_up(self):
        cases=[({'segments_complete':False},[self.entry],ValueError),
               ({'boot_id':'different-boot'},[self.entry],IdentityError),
               ({},[segment(relative_path='../outside.wpilog')],ValueError),
               ({},[segment(created_at='2026-10-06T18:00:00')],ValueError),
               ({},[segment(state='orphan_incomplete')],SourceAttention),
               ({},[{k:v for k,v in self.entry.items() if k!='format_profile'}],ValueError)]
        for root,entries,error in cases:
            with self.subTest(root=root,entries=entries):
                self.remote.files.update(manifest_files(entries,**root))
                with self.assertRaises(error):self.source.discover_closed(None,100,self.token())

    def test_explicit_configuration_uses_local_file_references_and_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'known').write_text('synthetic');(root/'key').write_text('synthetic')
            value=dict(schema_version=1,host='explicit-host',username='reader',known_hosts='known',private_key='key',
                       log_root='/logs',robot_id='robot-6391')
            path=root/'source.json';path.write_text(json.dumps(value))
            config=SFTPConfig.load(path)
            self.assertEqual(config.private_key,str((root/'key').resolve()))
            value['password']='not-accepted';path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):SFTPConfig.load(path)

    def test_transport_eof_is_redacted_retryable_but_unrecognized_errors_are_preserved(self):
        original=self.remote.open
        for error,expected in ((EOFError('private-provider-details'),TransferError),
                               (RuntimeError('implementation-error'),RuntimeError)):
            def fail(*args,**kwargs):raise error
            self.remote.open=fail
            with self.assertRaises(expected) as raised:
                self.source.discover_closed(None,100,self.token())
            if expected is TransferError:
                self.assertTrue(raised.exception.retryable)
                self.assertNotIn('private-provider-details',str(raised.exception))
            else:
                self.assertIs(raised.exception,error)
            self.assertIsNone(self.source.session)
        self.remote.open=original
        self.assertTrue(self.source.discover_closed(None,100,self.token()).complete)

    def test_blocked_read_cancel_close_retains_operation_and_bounded_io_slot(self):
        self.source.config=replace(self.source.config,timeout=.05)
        self.source.discover_closed(None,100,self.token())
        client=self.clients[0]
        read_entered=threading.Event();release_read=threading.Event()
        close_entered=threading.Event();release_close=threading.Event()
        def block_read():
            read_entered.set()
            if not release_read.wait(3):raise RuntimeError('Read coordination timed out')
        def block_close():
            close_entered.set()
            if not release_close.wait(3):raise RuntimeError('Close coordination timed out')
            client.closed=True
        self.remote.block=block_read;client.close=block_close
        token=self.token();slot=BoundedIO();results=[]
        def call():
            try:results.append(slot.call(lambda:self.source.read(self.entry['segment_id'],0,100,
                cancellation=token),token,2,length=100))
            except Exception as exc:results.append(exc)
        caller=threading.Thread(target=call);caller.start()
        try:
            self.assertTrue(read_entered.wait(1))
            token.cancel()
            self.assertTrue(close_entered.wait(1))
            caller.join(1)
            self.assertFalse(caller.is_alive())
            self.assertIsInstance(results[0],TransferCancelled)
            release_read.set()
            # Longer than the old .05+.1 watcher join: cleanup must still own
            # the slot even though the native read and caller have returned.
            self.assertFalse(slot.done.wait(.25))
            self.assertTrue(slot.busy);self.assertEqual(slot.outstanding_bytes,100)
            self.assertTrue(self.source.operation_lock.locked())
            reads=list(self.remote.reads)
            with self.assertRaises(TransferError):
                self.source.read(self.entry['segment_id'],0,100,cancellation=self.token())
            with self.assertRaises(TransferTimeout):slot.call(lambda:b'new',self.token(),1)
            self.assertEqual(self.remote.reads,reads)
            self.assertEqual(len(self.clients),1)
            self.assertFalse(self.source.status().enabled)
        finally:
            release_read.set();release_close.set();caller.join(2)
            if slot.thread is not None:slot.thread.join(2)
            self.remote.block=None
        self.assertFalse(slot.busy)
        self.assertFalse(self.source.operation_lock.locked())
        self.assertTrue(client.closed)
        self.assertEqual(self.source.read(self.entry['segment_id'],0,100,cancellation=self.token()),self.payload[:100])
        self.assertEqual(len(self.clients),2)
        self.assertFalse(self.clients[1].closed)

    def test_connect_watcher_cleanup_finishes_before_operation_returns(self):
        self.source.config=replace(self.source.config,timeout=.05)
        connect_entered=threading.Event();release_connect=threading.Event()
        close_entered=threading.Event();release_close=threading.Event();finished=threading.Event()
        client=MemoryClient(self.remote)
        def connect(**kwargs):
            connect_entered.set()
            if not release_connect.wait(3):raise RuntimeError('Connect coordination timed out')
        def close():
            close_entered.set()
            if not release_close.wait(3):raise RuntimeError('Close coordination timed out')
            client.closed=True
        client.connect=connect;client.close=close
        original_factory=self.source.client_factory
        self.source.client_factory=lambda:client
        token=self.token();results=[]
        def discover():
            try:results.append(self.source.discover_closed(None,100,token))
            except Exception as exc:results.append(exc)
            finally:finished.set()
        worker=threading.Thread(target=discover);worker.start()
        try:
            self.assertTrue(connect_entered.wait(1))
            token.cancel()
            self.assertTrue(close_entered.wait(1))
            release_connect.set()
            self.assertFalse(finished.wait(.25))
            self.assertTrue(self.source.operation_lock.locked())
            with self.assertRaises(TransferError):self.source.discover_closed(None,100,self.token())
            self.assertEqual(self.remote.reads,[])
        finally:
            release_connect.set();release_close.set();worker.join(2)
            self.source.client_factory=original_factory
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(results[0],TransferCancelled)
        self.assertTrue(client.closed)
        self.assertFalse(self.source.operation_lock.locked())
        self.assertTrue(self.source.discover_closed(None,100,self.token()).complete)
        self.assertFalse(self.clients[0].closed)

    def test_delayed_old_client_disconnect_cannot_close_successor(self):
        self.source.discover_closed(None,100,self.token())
        old_client=self.clients[0]
        self.source._disconnect_transport(old_client)
        self.source.discover_closed(None,100,self.token())
        successor=self.clients[1];session=self.source.session
        self.source._disconnect_transport(old_client)
        self.assertIs(self.source.active_client,successor)
        self.assertIs(self.source.session,session)
        self.assertFalse(successor.closed)
        self.assertEqual(self.source.read(self.entry['segment_id'],0,100,cancellation=self.token()),self.payload[:100])

    def test_failed_native_close_retries_cleanup_and_keeps_slot_until_success(self):
        self.source.config=replace(self.source.config,timeout=.05)
        for failure in (OSError('private transport failure'),RuntimeError('private cleanup failure')):
            with self.subTest(failure=type(failure).__name__):
                self.source.discover_closed(None,100,self.token())
                client=self.clients[-1]
                read_entered=threading.Event();release_read=threading.Event()
                retry_entered=threading.Event();release_close=threading.Event()
                attempts=[];results=[]
                def block_read():
                    read_entered.set()
                    if not release_read.wait(3):raise RuntimeError('Read coordination timed out')
                def failing_close():
                    attempts.append(1)
                    if len(attempts)==1:raise failure
                    retry_entered.set()
                    if not release_close.wait(3):raise RuntimeError('Close coordination timed out')
                    client.closed=True
                self.remote.block=block_read;client.close=failing_close
                token=self.token();slot=BoundedIO()
                def call():
                    try:results.append(slot.call(lambda:self.source.read(self.entry['segment_id'],0,100,
                        cancellation=token),token,2,length=100))
                    except Exception as exc:results.append(exc)
                caller=threading.Thread(target=call);caller.start()
                try:
                    self.assertTrue(read_entered.wait(1));token.cancel()
                    self.assertTrue(retry_entered.wait(1))
                    release_read.set();caller.join(1)
                    self.assertFalse(caller.is_alive())
                    self.assertIsInstance(results[0],TransferCancelled)
                    self.assertFalse(slot.done.wait(.25))
                    self.assertTrue(slot.busy)
                    self.assertTrue(self.source.operation_lock.locked())
                    self.assertEqual(self.source.cleanup_pending,1)
                    self.assertFalse(self.source.cleanup_finished.is_set())
                    with self.assertRaises(TransferError):
                        self.source.read(self.entry['segment_id'],0,100,cancellation=self.token())
                    self.source.cancel()  # Detached cleanup remains owned; no false completion.
                    self.assertTrue(slot.busy)
                finally:
                    release_read.set();release_close.set();caller.join(2)
                    if slot.thread is not None:slot.thread.join(2)
                    self.remote.block=None
                self.assertFalse(slot.busy)
                self.assertEqual(self.source.cleanup_pending,0)
                self.assertTrue(self.source.cleanup_finished.is_set())
                self.assertTrue(client.closed)
                self.assertEqual(len(attempts),2)
                self.assertEqual(self.source.read(self.entry['segment_id'],0,100,cancellation=self.token()),self.payload[:100])
                self.assertFalse(self.clients[-1].closed)
