"""Transport contract tests; genuine loopback SFTP tests are in test_sftp_network."""
import hashlib
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
from robot_test_hub.transfer import Cancellation, IdentityError, TransferCancelled, TransferError
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
