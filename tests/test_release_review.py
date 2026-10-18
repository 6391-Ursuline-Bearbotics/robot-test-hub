"""Independent release boundary, extraction and child lifecycle regressions."""
from contextlib import closing
import hashlib
import json
import os
import threading
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


class ReleaseReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='release review ')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()

    def archive(self,extra=(),*,compression=zipfile.ZIP_STORED,mode=None):
        from robot_test_hub import release
        folder=self.root/('group-'+str(len(list(self.root.glob('group-*')))))
        folder.mkdir()
        records=[(name,(Path(__file__).resolve().parents[1]/name).read_bytes() if name in release.PUBLIC_FIXTURES else ('public '+name).encode()) for name in release.REQUIRED_PATHS]+list(extra)
        records.sort()
        inventory=[]
        with zipfile.ZipFile(folder/'source.zip','w') as archive:
            for name,data in records:
                info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.create_system=3
                info.external_attr=(stat.S_IFREG|0o644)<<16
                if mode is not None and name=='extra.py':info.external_attr=mode<<16
                info.compress_type=compression
                archive.writestr(info,data)
                inventory.append({'path':name,'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'git_mode':'100644'})
        encoded=(folder/'source.zip').read_bytes()
        manifest={'schema_version':1,'tool_version':release.TOOL_VERSION,'publication':release.PUBLICATION,
            'source_commit':'a'*40,'source_tree':'b'*40,'inventory':inventory,
            'archive':{'path':'source.zip','size_bytes':len(encoded),'sha256':hashlib.sha256(encoded).hexdigest()}}
        (folder/'manifest.json').write_bytes((json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode())
        return folder

    def committed_git(self,repo):
        from robot_test_hub import release
        data={name:(Path(__file__).resolve().parents[1]/name).read_bytes() if name in release.PUBLIC_FIXTURES else ('committed '+name).encode() for name in release.REQUIRED_PATHS}
        objects={hashlib.sha1(b'blob '+str(len(value)).encode()+b'\x00'+value).hexdigest():value for value in data.values()}
        by_name={name:hashlib.sha1(b'blob '+str(len(value)).encode()+b'\x00'+value).hexdigest() for name,value in data.items()}
        def git(source,*args,maximum):
            self.assertEqual(source,repo)
            if args==('rev-parse','--show-toplevel'):return str(repo).encode()+b'\n'
            if args[0]=='rev-parse':return (('b'*40 if args[-1].endswith('^{tree}') else 'a'*40)+'\n').encode()
            if args[0]=='ls-tree':return b''.join(('100644 blob '+by_name[name]+' '+str(len(data[name]))+'\t'+name+'\x00').encode() for name in sorted(data))
            if args[:2]==('cat-file','blob'):return objects[args[2]]
            self.fail(args)
        return patch.object(release,'_git',side_effect=git),data

    def child(self,code):
        from robot_test_hub import release_rehearsal as rehearsal
        original=subprocess.Popen;children=[];commands=[]
        def launch(args,**kwargs):
            commands.append((args,kwargs))
            process=original([sys.executable,'-I','-c',code],**kwargs)
            children.append(process)
            return process
        return patch.object(rehearsal.subprocess,'Popen',side_effect=launch),children,commands

    def test_rehearsal_rejects_malformed_child_receipts_without_echoing_payload(self):
        from robot_test_hub import release_rehearsal as rehearsal
        valid={'schema_version':1,'success':True,'schema':7,'receipt_sha256':'a'*64,'report_count':2}
        cases=[{**valid,'schema':'C:/private-secret'}, {**valid,'schema':True}, {**valid,'schema_version':True},
               {**valid,'receipt_sha256':'private-secret'}, {**valid,'report_count':-1}, {**valid,'unexpected':'private-secret'}]
        for receipt in cases:
            with self.subTest(receipt=receipt):
                stub,children,commands=self.child('print('+repr(json.dumps(receipt))+')')
                with stub,self.assertRaises(rehearsal.RehearsalError) as caught:
                    rehearsal._run(Path(sys.executable),self.root,self.root,'prepare')
                self.assertNotIn('private-secret',str(caught.exception))
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertEqual(commands[0][0][1:3],['-I','-c'])
                self.assertEqual(Path(commands[0][1]['env']['TEMP']).parent,self.root)
                self.assertTrue(Path(commands[0][1]['env']['TEMP']).is_dir())
                self.assertNotIn('PYTHONPATH',commands[0][1]['env'])

    def test_timed_out_and_excess_output_children_are_reaped(self):
        from robot_test_hub import release_rehearsal as rehearsal
        for code,timeout in (('import time;time.sleep(30)',0.05),('import sys;sys.stdout.write("private-secret"*10000);sys.stdout.flush()',5)):
            with self.subTest(code=code):
                stub,children,_=self.child(code)
                before=list(rehearsal._ACTIVE_CHILDREN)
                with stub,patch.object(rehearsal,'CHILD_TIMEOUT',timeout),self.assertRaises(rehearsal.RehearsalError) as caught:
                    rehearsal._run(Path(sys.executable),self.root,self.root,'restore')
                self.assertNotIn('private-secret',str(caught.exception))
                self.assertTrue(children)
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertEqual(rehearsal._ACTIVE_CHILDREN,before)
                # Production cleanup confirms release of inherited Windows handles.
                self.assertFalse((self.root/'child.stdout').exists())
                self.assertFalse((self.root/'child.stderr').exists())


    def test_output_sharing_violation_retains_ownership_until_files_are_released(self):
        from robot_test_hub import release_rehearsal as rehearsal
        stub,children,_=self.child('import time;time.sleep(30)')
        original=Path.unlink
        attempts=[]
        def unlink(path,*args,**kwargs):
            if path == self.root/'child.stdout':
                self.assertTrue(children and children[0].poll() is not None)
                self.assertIn((self.root,children[0]),rehearsal._ACTIVE_CHILDREN)
                attempts.append(True)
                if len(attempts)<3:
                    error=PermissionError('inherited output still closing')
                    error.winerror=32
                    raise error
            return original(path,*args,**kwargs)
        with stub,patch.object(rehearsal,'CHILD_TIMEOUT',.05),patch.object(Path,'unlink',unlink),patch.object(rehearsal,'time',wraps=rehearsal.time) as cleanup_clock,patch.object(cleanup_clock,'sleep') as pause:
            with self.assertRaisesRegex(rehearsal.RehearsalError,'rehearsal_child_limit'):
                rehearsal._run(Path(sys.executable),self.root,self.root,'restore')
        # Patch only the hub clock binding: POSIX subprocess.wait uses the
        # shared time module too, and must keep its own real backoff sleeps.
        self.assertEqual(len(attempts),3)
        self.assertEqual(pause.call_count,2)
        self.assertEqual(rehearsal._ACTIVE_CHILDREN,[])
        self.assertFalse((self.root/'child.stdout').exists())
        self.assertFalse((self.root/'child.stderr').exists())

    def test_output_cleanup_deadline_retains_child_and_does_not_retry_other_errors(self):
        from robot_test_hub import release_rehearsal as rehearsal
        for winerror in (32,5):
            with self.subTest(winerror=winerror):
                stub,children,_=self.child('import json;print(json.dumps(dict(schema_version=1,success=True)))')
                error=PermissionError('private output path')
                error.winerror=winerror
                original=Path.unlink
                def unlink(path,*args,**kwargs):
                    if path == self.root/'child.stdout':raise error
                    return original(path,*args,**kwargs)
                try:
                    with stub,patch.object(Path,'unlink',unlink),patch.object(rehearsal,'CHILD_OUTPUT_CLEANUP_TIMEOUT',0),patch.object(rehearsal,'time',wraps=rehearsal.time) as cleanup_clock,patch.object(cleanup_clock,'sleep') as pause:
                        with self.assertRaisesRegex(rehearsal.RehearsalError,'^rehearsal_child_cleanup_failed$'):
                            rehearsal._run(Path(sys.executable),self.root,self.root,'restore')
                    pause.assert_not_called()
                    self.assertTrue(all(child.poll() is not None for child in children))
                    self.assertEqual(rehearsal._ACTIVE_CHILDREN,[(self.root,children[0])])
                    self.assertTrue((self.root/'child.stdout').is_file())
                finally:
                    for child in children:
                        if (self.root,child) in rehearsal._ACTIVE_CHILDREN:
                            rehearsal._ACTIVE_CHILDREN.remove((self.root,child))

    def test_real_driver_detects_changed_original_before_upgrade(self):
        from robot_test_hub import release_rehearsal as rehearsal
        from robot_test_hub.selftest import _sample
        source=Path(__file__).resolve().parents[1]
        (self.root/'sample.wpilog').write_bytes(_sample())
        prepared=rehearsal._run(Path(sys.executable),source,self.root,'prepare')
        self.assertEqual(prepared['schema'],8)
        self.assertEqual(prepared['report_count'],2)
        originals=list((self.root/'archive/raw').rglob('*.wpilog'))
        self.assertEqual(len(originals),1)
        payload=originals[0].read_bytes()
        originals[0].write_bytes(payload[:-1]+bytes([payload[-1]^1]))
        with self.assertRaises(rehearsal.RehearsalError) as caught:
            rehearsal._run(Path(sys.executable),source,self.root,'upgrade')
        self.assertEqual(str(caught.exception),'rehearsal_child_failed')
        self.assertEqual(rehearsal._ACTIVE_CHILDREN,[])
        # Failure never rewrites or repairs the changed original silently.
        self.assertEqual(originals[0].read_bytes(),payload[:-1]+bytes([payload[-1]^1]))

    def test_rehashed_hostile_paths_and_file_directory_aliases_never_extract(self):
        from robot_test_hub import release
        valid=self.archive()
        self.assertIs(release.verify(valid)['verified'],True)
        attacks=[[(name,b'x')] for name in ('../escape.py','AUX.txt','COM\u00b9.cfg','assets/cache.','assets/file:stream','assets\\x.py','assets/zero\x00.py')]
        attacks.extend(([('thing',b'a'),('thing/child.py',b'b')], [('Case/a.py',b'a'),('case/b.py',b'b')], [('DUP.py',b'a'),('dup.py',b'b')]))
        for index,entries in enumerate(attacks):
            with self.subTest(entries=entries):
                folder=self.archive(entries)
                destination=self.root/('must-not-exist-'+str(index))
                with self.assertRaises(release.ReleaseError):release.extract(folder,destination)
                self.assertFalse(destination.exists())
        self.assertFalse((self.root/'escape.py').exists())

    def test_rehashed_symlink_mode_compression_duplicates_and_resource_caps_reject(self):
        from robot_test_hub import release
        with self.assertWarns(UserWarning):duplicate=self.archive([('repeat.py',b'a'),('repeat.py',b'b')])
        folders=[self.archive([('extra.py',b'../outside')],mode=stat.S_IFLNK|0o777),
                 self.archive([('extra.py',b'public')],compression=zipfile.ZIP_DEFLATED),duplicate]
        for folder in folders:
            with self.subTest(folder=folder),self.assertRaises(release.ReleaseError):release.verify(folder)
        valid=self.archive()
        with patch.object(release,'MAX_FILES',1),patch.object(release.zipfile,'ZipFile',side_effect=AssertionError('inventory resource cap must precede ZIP allocation')):
            with self.assertRaises(release.ReleaseError):release.extract(valid,self.root/'limited')
        self.assertFalse((self.root/'limited').exists())

    def test_existing_destination_and_tampered_payload_preserve_all_conflicts(self):
        from robot_test_hub import release
        folder=self.archive([('extra.py',b'preserved')]);destination=self.root/'existing';destination.mkdir()
        sentinel=destination/'operator.txt';sentinel.write_text('private sentinel')
        with self.assertRaises(release.ReleaseError):release.extract(folder,destination)
        self.assertEqual(sentinel.read_text(),'private sentinel')
        raw=bytearray((folder/'source.zip').read_bytes());raw[50]^=1
        (folder/'source.zip').write_bytes(raw)
        with self.assertRaises(release.ReleaseError):release.extract(folder,self.root/'tampered')
        self.assertFalse((self.root/'tampered').exists())

    def test_build_uses_committed_blobs_deterministically_and_preserves_conflicts(self):
        from robot_test_hub import release
        repo=self.root/'repo';repo.mkdir();(repo/'README.md').write_text('uncommitted private-secret')
        stub,committed=self.committed_git(repo)
        with stub:
            first=release.build(repo,self.root/'one');second=release.build(repo,self.root/'two')
            a=Path(first['release_folder']);b=Path(second['release_folder'])
            self.assertEqual((a/'source.zip').read_bytes(),(b/'source.zip').read_bytes())
            self.assertEqual((a/'manifest.json').read_bytes(),(b/'manifest.json').read_bytes())
            with zipfile.ZipFile(a/'source.zip') as archive:self.assertEqual(archive.read('README.md'),committed['README.md'])
            self.assertEqual((repo/'README.md').read_text(),'uncommitted private-secret')
            self.assertEqual(release.build(repo,self.root/'one')['state'],'existing')
            changed=(a/'manifest.json').read_bytes()+b' '
            (a/'manifest.json').write_bytes(changed)
            with self.assertRaises(release.ReleaseError):release.build(repo,self.root/'one')
            self.assertEqual((a/'manifest.json').read_bytes(),changed)

    def test_publication_failure_retains_invalid_group_and_extraction_cleans_owned_files(self):
        from robot_test_hub import release
        repo=self.root/'repo';repo.mkdir();stub,_=self.committed_git(repo)
        original=shutil.copyfileobj;calls=[]
        def copy(source,destination,*args):
            calls.append(True)
            if len(calls)==2:raise OSError('private-secret interrupted publication')
            return original(source,destination,*args)
        output=self.root/'output'
        with stub,patch.object(release.shutil,'copyfileobj',side_effect=copy),self.assertRaises(release.ReleaseError) as caught:
            release.build(repo,output)
        self.assertEqual(caught.exception.cleanup_state,'retained')
        self.assertNotIn('private-secret',str(caught.exception))
        groups=list(output.iterdir());self.assertEqual(len(groups),1)
        with self.assertRaises(release.ReleaseError):release.verify(groups[0])
        good=self.archive();destination=self.root/'extract-failed'
        with patch.object(release.os,'fsync',side_effect=OSError('private-secret fsync')),self.assertRaises(release.ReleaseError):release.extract(good,destination)
        self.assertFalse(destination.exists())

    def test_git_read_failure_reaps_owned_child_and_disables_lazy_network_fetch(self):
        from robot_test_hub import release
        original=subprocess.Popen;children=[];seen=[];before={t.ident for t in threading.enumerate()}
        class BrokenRead:
            def __init__(self,stream):self.stream=stream
            def read(self,*args):raise OSError('injected read failure')
            def close(self):self.stream.close()
        def launch(args,**kwargs):
            seen.append(kwargs['env'])
            process=original([sys.executable,'-I','-c','import time;time.sleep(30)'],**kwargs)
            process.stdout=BrokenRead(process.stdout);children.append(process);return process
        with patch.object(release.subprocess,'Popen',side_effect=launch),self.assertRaises(OSError):release._git(self.root,'rev-parse','HEAD',maximum=100)
        self.assertEqual(seen[0]['GIT_NO_LAZY_FETCH'],'1')
        self.assertEqual(seen[0]['GIT_TERMINAL_PROMPT'],'0')
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual({t.ident for t in threading.enumerate()},before)

if __name__=='__main__':unittest.main()
