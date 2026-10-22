"""Manual snapshot safety using only invented in-memory source bytes."""
from contextlib import closing, nullcontext
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from robot_test_hub import pull
from robot_test_hub.storage import open_catalog
from test_pull import Attr, Client, FakeSFTP, FIXTURES, Permission
from robot_test_hub.sftp_source import SFTPConfig

class Gate:
    def __init__(self):
        self.reason = None
    def check(self):
        if self.reason:
            raise RuntimeError(self.reason)

class PullSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-snapshot-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = open_catalog(self.root)
        self.addCleanup(self.db.close)
        self.config = SFTPConfig('synthetic.invalid','reader',__file__,__file__,'/logs','synthetic-one',max_read=17)
        self.payload = (FIXTURES/'alpha7-main.wpilog').read_bytes()
        self.gate = Gate()
    def collect(self, source, **changes):
        return pull.collect(self.root,self.db,changes.pop('config',self.config),permission=self.gate,
                            bulk_slot=nullcontext,include_newest=True,
                            connect_fn=lambda config:(Client(),source),**changes)
    def receipts(self):
        return [json.loads(path.read_text()) for path in self.root.rglob('*.json') if 'sessions' in path.parts]
    def test_capability_missing_or_denied_fails_before_connection(self):
        connect = Mock(side_effect=AssertionError('no network permitted'))
        for options in ({},dict(permission=Permission()),dict(permission=Gate(),bulk_slot=None)):
            with self.subTest(options=options), self.assertRaises(pull.PullError):
                pull.collect(self.root,self.db,self.config,connect_fn=connect,**options)
        self.gate.reason = 'offline'
        with self.assertRaises(pull.PermissionLost):
            pull.collect(self.root,self.db,self.config,permission=self.gate,bulk_slot=nullcontext,connect_fn=connect)
        connect.assert_not_called()
    def test_every_returned_block_is_bounded_and_permission_checked(self):
        owner=self
        class Source(FakeSFTP):
            def open(self,path,mode):
                class Handle(io.BytesIO):
                    def read(handle,length):
                        owner.assertLessEqual(length,owner.config.max_read)
                        owner.assertLessEqual(length,pull.CHUNK)
                        return super().read(length)
                return Handle(self.files[path.rsplit('/',1)[-1]])
        result=self.collect(Source({'capture.wpilog':self.payload}))
        self.assertEqual(result['state'],'complete',result)
        self.assertEqual(result['imports'],{'imported':1})
        record=next(r for r in self.receipts() if r['state']=='captured')
        self.assertFalse(record['closed_segment_verified'])
        self.assertEqual(record['source_type'],'MANUAL_LOCAL')
        self.assertEqual(Path(record['snapshot_path']).read_bytes(),self.payload)
    def test_enable_stale_pause_and_shutdown_during_read_discard_returned_bytes(self):
        for reason in ('enabled','stale','operator_pause','shutdown'):
            with self.subTest(reason=reason):
                self.gate.reason=None
                owner=self
                class Source(FakeSFTP):
                    def open(self,path,mode):
                        class Handle(io.BytesIO):
                            def read(handle,length):
                                owner.gate.reason=reason
                                return super().read(length)
                        self.handle=Handle(self.files[path.rsplit('/',1)[-1]])
                        return self.handle
                source=Source({'capture.wpilog':self.payload})
                result=self.collect(source)
                self.assertEqual(result['state'],'interrupted')
                self.assertEqual(result['downloaded'],0)
                self.assertEqual(result['imports'],{})
                self.assertTrue(source.closed)
                self.assertTrue(source.handle.closed)
        parts=list(self.root.rglob('*.part'))
        self.assertEqual(len(parts),4)
        self.assertTrue(all(p.stat().st_size==0 for p in parts))
    def test_durable_prefix_retained_and_never_resumed(self):
        owner=self
        class Source(FakeSFTP):
            def open(self,path,mode):
                class Handle(io.BytesIO):
                    def read(handle,length):
                        if handle.tell():owner.gate.reason='enabled'
                        return super().read(length)
                return Handle(self.files[path.rsplit('/',1)[-1]])
        first=self.collect(Source({'capture.wpilog':self.payload}))
        self.assertEqual(first['state'],'interrupted')
        part=next(self.root.rglob('*.part'))
        self.assertEqual(part.read_bytes(),self.payload[:17])
        self.gate.reason=None
        second=self.collect(FakeSFTP({'capture.wpilog':self.payload}))
        self.assertEqual(second['state'],'complete')
        self.assertEqual(part.read_bytes(),self.payload[:17])
        self.assertEqual(len(list((self.root/'pull-staging/sessions').iterdir())),2)
    def test_equal_size_name_replacements_and_two_robot_sources_are_recaptured(self):
        payloads=[self.payload, self.payload[:-1]+bytes([self.payload[-1]^1])]
        first=self.collect(FakeSFTP({'same.wpilog':payloads[0]}))
        second=self.collect(FakeSFTP({'same.wpilog':payloads[1]}))
        third=self.collect(FakeSFTP({'same.wpilog':payloads[0]}),config=replace(self.config,robot_id='synthetic-two'))
        self.assertTrue(all(r['downloaded']==1 and r['already_pulled']==0 for r in (first,second,third)))
        records=[r for r in self.receipts() if r['state']=='captured']
        self.assertEqual(len(records),3)
        self.assertEqual({Path(r['snapshot_path']).read_bytes() for r in records},set(payloads))
        self.assertEqual({r['source']['robot_id'] for r in records},{'synthetic-one','synthetic-two'})
    def test_changed_mtime_same_size_is_not_imported(self):
        class Source(FakeSFTP):
            changed=False
            def open(self,path,mode):
                handle=super().open(path,mode)
                self.changed=True
                return handle
            def stat(self,path):
                info=super().stat(path)
                if self.changed:info.st_mtime+=1
                return info
        result=self.collect(Source({'capture.wpilog':self.payload}))
        self.assertEqual(result['downloaded'],0)
        self.assertEqual(result['imports'],{})
        self.assertEqual(result['problems'],[{'file':'capture.wpilog','reason':'source_changed'}])
        self.assertEqual(next(self.root.rglob('*.part')).read_bytes(),self.payload)
        record=next(r for r in self.receipts() if r.get('file')=='capture.wpilog')
        self.assertNotEqual(record['before']['mtime'],record['after']['mtime'])
    def test_short_read_and_disconnect_preserve_partial_without_false_complete(self):
        for disconnect in (False,True):
            with self.subTest(disconnect=disconnect):
                class Source(FakeSFTP):
                    def open(self,path,mode):
                        class Handle(io.BytesIO):
                            def read(handle,length):
                                if handle.tell():
                                    if disconnect:raise OSError('synthetic disconnection')
                                    return b''
                                return super().read(length)
                        return Handle(self.files[path.rsplit('/',1)[-1]])
                result=self.collect(Source({'capture.wpilog':self.payload}))
                self.assertEqual(result['state'],'attention')
                self.assertEqual(result['downloaded'],0)
                self.assertEqual(result['imports'],{})
        self.assertTrue(all(p.stat().st_size==17 for p in self.root.rglob('*.part')))
    def test_empty_listing_and_empty_file_are_attention(self):
        for files in ({},{'empty.wpilog':b''}):
            with self.subTest(files=files):
                result=self.collect(FakeSFTP(files))
                self.assertEqual(result['state'],'attention')
                self.assertEqual(result['downloaded'],0)
    def test_listing_limit_reports_unknown_incomplete_and_never_opens_log(self):
        class Source(FakeSFTP):
            def listdir_attr(self,root):
                return [Attr('ignored.txt',1,1)]*(pull.MAX_LISTED_ENTRIES+1)
            def open(self,*args):
                raise AssertionError('incomplete listing must not download')
        result=self.collect(Source({}))
        self.assertEqual(result['state'],'interrupted')
        self.assertIsNone(result['listed'])
        self.assertFalse(result['listing_complete'])
        self.assertIn('listing_entry_limit',result['problems'][0]['reason'])
    def test_earlier_capture_imported_when_later_file_loses_permission(self):
        owner=self
        class Source(FakeSFTP):
            def open(self,path,mode):
                if path.endswith('second.wpilog'):
                    class Handle(io.BytesIO):
                        def read(handle,length):
                            owner.gate.reason='shutdown'
                            return super().read(length)
                    return Handle(self.files['second.wpilog'])
                return super().open(path,mode)
        result=self.collect(Source({'first.wpilog':self.payload,'second.wpilog':self.payload}))
        self.assertEqual(result['state'],'interrupted')
        self.assertEqual(result['downloaded'],1)
        self.assertEqual(result['imports'],{'imported':1})
        self.assertEqual(result['files'],[{'file':'first.wpilog','state':'succeeded'}])
        self.assertTrue(all(not Path(path).is_absolute() for path in result['receipts']))
        self.assertTrue(all((self.root/path).is_file() for path in result['receipts']))
    def test_timeout_closes_blocked_read_and_prevents_further_remote_calls(self):
        released=threading.Event()
        class ClosingClient:
            def close(self):released.set()
        class Source(FakeSFTP):
            stats=0
            def stat(self,path):
                self.stats+=1
                if self.stats>1:raise AssertionError('remote operation after timed out session')
                return super().stat(path)
            def open(self,path,mode):
                class Handle(io.BytesIO):
                    def read(handle,length):
                        if not released.wait(2):raise AssertionError('watcher did not time out')
                        return super().read(length)
                return Handle(self.files[path.rsplit('/',1)[-1]])
        source=Source({'first.wpilog':self.payload,'second.wpilog':self.payload})
        result=pull.collect(self.root,self.db,replace(self.config,timeout=.05),permission=self.gate,
                            bulk_slot=nullcontext,include_newest=True,connect_fn=lambda config:(ClosingClient(),source))
        self.assertNotEqual(result['state'],'complete')
        self.assertEqual(source.stats,1)
        self.assertEqual(result['downloaded'],0)
        self.assertTrue(source.closed)
        self.assertFalse(any(t.name=='hub-pull-permission' for t in threading.enumerate()))
    def test_permission_revoked_by_connect_return_closes_both_resources(self):
        class Resource:
            closed=False
            def close(self):self.closed=True
        client,sftp=Resource(),Resource()
        def connect(config):
            self.gate.reason='shutdown'
            return client,sftp
        result=pull.collect(self.root,self.db,self.config,permission=self.gate,bulk_slot=nullcontext,connect_fn=connect)
        self.assertEqual(result['state'],'interrupted')
        self.assertTrue(client.closed and sftp.closed)
        self.assertFalse(any(t.name=='hub-pull-permission' for t in threading.enumerate()))

if __name__=='__main__':unittest.main()
