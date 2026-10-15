"""Actual temporary Git commits qualify deterministic, non-executing releases."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from robot_test_hub import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='source release tests ');self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve();self.repo=self.base/'repo';self.repo.mkdir()
        self.git('init','-q');self.git('config','user.email','synthetic@example.invalid');self.git('config','user.name','Synthetic Fixture')
        self.git('config','core.autocrlf','false')
        fixtures=Path(__file__).parent/'fixtures/synthetic'
        for name in sorted(release.REQUIRED_PATHS):
            path=self.repo/name;path.parent.mkdir(parents=True,exist_ok=True)
            if name.startswith('tests/fixtures/synthetic/'):
                path.write_bytes((fixtures/path.name).read_bytes())
            else:path.write_bytes(('Invented committed source: '+name+'\n').encode())
        (self.repo/'executable.sh').write_bytes(b'#!/bin/sh\nprintf synthetic\n')
        self.git('add','--all');self.git('update-index','--chmod=+x','executable.sh');self.git('commit','-qm','Synthetic source fixture')
        self.commit=self.git('rev-parse','HEAD').strip().decode()

    def git(self,*arguments,input=None):
        result=subprocess.run(['git','-C',str(self.repo),*arguments],input=input,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
        if result.returncode:raise AssertionError('Synthetic Git command failed')
        return result.stdout

    def build(self,name='output'):
        result=release.build(self.repo,self.base/name)
        return Path(result['release_folder'])

    def rewrite(self,folder,change):
        manifest=json.loads((folder/'manifest.json').read_bytes())
        with zipfile.ZipFile(folder/'source.zip') as archive:
            entries=[(copy.copy(info),archive.read(info)) for info in archive.infolist()]
        entries=change(manifest,entries) or entries
        with zipfile.ZipFile(folder/'source.zip','w',compression=zipfile.ZIP_STORED,allowZip64=False) as archive:
            for info,data in entries:archive.writestr(info,data)
        data=(folder/'source.zip').read_bytes()
        manifest['archive'].update(size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        (folder/'manifest.json').write_bytes(release._json(manifest))

    def test_actual_git_determinism_ignores_dirty_crlf_and_untracked_private_files(self):
        first=self.build('one');before=(first/'source.zip').read_bytes();manifest=(first/'manifest.json').read_bytes()
        self.git('config','core.autocrlf','true')
        (self.repo/'README.md').write_bytes(b'Uncommitted private work\r\n')
        (self.repo/'.env').write_bytes(b'SYNTHETIC_SECRET=never_include\n')
        status=self.git('status','--porcelain=v1','-z')
        second=self.build('two')
        self.assertEqual((second/'source.zip').read_bytes(),before)
        self.assertEqual((second/'manifest.json').read_bytes(),manifest)
        self.assertEqual(self.git('status','--porcelain=v1','-z'),status)
        self.assertEqual((self.repo/'README.md').read_bytes(),b'Uncommitted private work\r\n')
        receipt=release.verify(second)
        self.assertEqual(receipt['source_commit'],self.commit)
        self.assertEqual({item['path'] for item in receipt['inventory']},set(release.REQUIRED_PATHS)|{'executable.sh'})
        self.assertEqual(next(item['git_mode'] for item in receipt['inventory'] if item['path']=='executable.sh'),'100755')
        existing=release.build(self.repo,self.base/'two')
        self.assertEqual(existing['state'],'existing')

    def test_verified_fresh_extraction_has_exact_inventory_and_never_executes_code(self):
        folder=self.build();destination=self.base/'source'
        with patch('subprocess.Popen',side_effect=AssertionError('No executed release code')):
            result=release.extract(folder,destination)
        self.assertEqual(result['state'],'extracted')
        receipt=release.verify(folder)
        self.assertEqual({p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file()},{item['path'] for item in receipt['inventory']})
        for item in receipt['inventory']:
            data=(destination/item['path']).read_bytes()
            self.assertEqual((len(data),hashlib.sha256(data).hexdigest()),(item['size_bytes'],item['sha256']))
        original=(destination/'README.md').read_bytes()
        with self.assertRaises(release.ReleaseError):release.extract(folder,destination)
        self.assertEqual((destination/'README.md').read_bytes(),original)

    def test_committed_private_data_links_and_gitlinks_fail_before_publication(self):
        (self.repo/'.env').write_bytes(b'Invented private configuration')
        self.git('add','.env');self.git('commit','-qm','Forbidden source fixture')
        with self.assertRaises(release.ReleaseError) as cm:self.build()
        self.assertEqual(cm.exception.code,'release_forbidden_source')
        self.assertFalse((self.base/'output').exists())
        for name in ('data/original.wpilog','private/source.json','raw/x.txt','settings.key','id_ed25519','video.mp4'):
            with self.subTest(name=name),self.assertRaises(release.ReleaseError):release._source_policy(name)
        self.git('rm','--cached','.env')
        blob=self.git('hash-object','-w','--stdin',input=b'target').strip().decode()
        self.git('update-index','--add','--cacheinfo','120000,'+blob+',linked')
        self.git('commit','-qm','Forbidden symbolic link fixture')
        with self.assertRaises(release.ReleaseError):self.build('linked-output')
        self.git('update-index','--force-remove','linked');self.git('update-index','--add','--cacheinfo','160000,'+self.commit+',nested')
        self.git('commit','-qm','Forbidden Git link fixture')
        with self.assertRaises(release.ReleaseError):self.build('gitlink-output')

    def test_windows_paths_case_and_file_ancestor_collisions_rejected(self):
        folder=self.build()
        original_manifest=(folder/'manifest.json').read_bytes();original_zip=(folder/'source.zip').read_bytes()
        for names in (('../escape',),('C:/escape',),('file:stream',),('CON.txt',),('COM¹.txt',),('x.',),('x ',),('a\\b',),('A/file','a/other'),('parent','parent/child')):
            with self.subTest(names=names):
                (folder/'manifest.json').write_bytes(original_manifest);(folder/'source.zip').write_bytes(original_zip)
                def change(manifest,entries):
                    for name in names:
                        data=b'invented';info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.create_system=3;info.external_attr=(stat.S_IFREG|0o644)<<16
                        entries.append((info,data));manifest['inventory'].append(dict(path=name,size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),git_mode='100644'))
                    manifest['inventory'].sort(key=lambda item:item['path']);return sorted(entries,key=lambda pair:pair[0].filename)
                self.rewrite(folder,change)
                with self.assertRaises(release.ReleaseError):release.verify(folder)
                with self.assertRaises(release.ReleaseError):release.extract(folder,self.base/'never-created')
                self.assertFalse((self.base/'never-created').exists())

    def test_rehashed_zip_payload_crc_duplicates_links_compression_and_inventory_tamper(self):
        folder=self.build();manifest=(folder/'manifest.json').read_bytes();archive=(folder/'source.zip').read_bytes()
        def duplicate(m,entries):return entries+[entries[0]]
        def link(m,entries):entries[0][0].external_attr=(stat.S_IFLNK|0o777)<<16;return entries
        def compressed(m,entries):entries[0][0].compress_type=zipfile.ZIP_DEFLATED;return entries
        def payload(m,entries):entries[0]=(entries[0][0],b'changed');return entries
        for action in (duplicate,link,compressed,payload):
            with self.subTest(action=action.__name__):
                (folder/'manifest.json').write_bytes(manifest);(folder/'source.zip').write_bytes(archive)
                self.rewrite(folder,action)
                with self.assertRaises(release.ReleaseError):release.verify(folder)
        (folder/'manifest.json').write_bytes(manifest);data=bytearray(archive);data[60]^=1
        (folder/'source.zip').write_bytes(data)
        value=json.loads(manifest);value['archive']['sha256']=hashlib.sha256(data).hexdigest();(folder/'manifest.json').write_bytes(release._json(value))
        with self.assertRaises(release.ReleaseError):release.verify(folder)

    def test_conflicts_empty_target_race_and_partial_publication_are_preserved(self):
        folder=self.build();snapshot={p.name:p.read_bytes() for p in folder.iterdir()}
        (folder/'source.zip').write_bytes(b'conflicting output')
        with self.assertRaises(release.ReleaseError) as cm:release.build(self.repo,self.base/'output')
        self.assertEqual(cm.exception.code,'release_conflict')
        self.assertEqual((folder/'source.zip').read_bytes(),b'conflicting output')
        race_output=self.base/'race';race_output.mkdir();target=race_output/('robot-test-hub-'+self.commit)
        original=Path.mkdir
        def racing(path,*args,**kwargs):
            if path==target and not path.exists():original(path)
            return original(path,*args,**kwargs)
        with patch.object(Path,'mkdir',racing),self.assertRaises(release.ReleaseError):release.build(self.repo,race_output)
        self.assertTrue(target.is_dir());self.assertEqual(list(target.iterdir()),[])
        original_sync=release._sync_dir
        def failed_sync(path):
            if path.name=='robot-test-hub-'+self.commit:raise OSError('private filesystem detail')
            return original_sync(path)
        with patch.object(release,'_sync_dir',side_effect=failed_sync),self.assertRaises(release.ReleaseError) as cm:release.build(self.repo,self.base/'partial')
        self.assertEqual(cm.exception.cleanup_state,'retained')
        partial=self.base/'partial'/('robot-test-hub-'+self.commit)
        self.assertTrue(partial.exists());self.assertFalse((partial/'manifest.json').exists())
        with self.assertRaises(release.ReleaseError):release.verify(partial)
        self.assertFalse(any(path.name.startswith('.release-stage-') for path in (self.base/'partial').iterdir()))

    def test_extraction_fault_removes_only_owned_files_and_preserves_race_intruder(self):
        folder=self.build();destination=self.base/'failed'
        with patch('robot_test_hub.release.os.chmod',side_effect=OSError('private failure')),self.assertRaises(release.ReleaseError) as cm:release.extract(folder,destination)
        self.assertEqual(cm.exception.cleanup_state,'removed');self.assertFalse(destination.exists())
        race=self.base/'race-extract';original=Path.open
        def intruder(path,*args,**kwargs):
            if path==race/'README.md' and args and args[0]=='xb':
                (race/'unowned.txt').write_bytes(b'preserve me')
                raise OSError('injected failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',intruder),self.assertRaises(release.ReleaseError) as cm:release.extract(folder,race)
        self.assertEqual(cm.exception.cleanup_state,'retained')
        self.assertEqual((race/'unowned.txt').read_bytes(),b'preserve me')

    def test_resource_caps_and_manifest_duplicates_fail_without_destination(self):
        folder=self.build()
        with patch.object(release,'MAX_FILES',1),self.assertRaises(release.ReleaseError):release.build(self.repo,self.base/'too-many')
        self.assertFalse((self.base/'too-many').exists())
        with patch.object(release,'MAX_ZIP_BYTES',1),self.assertRaises(release.ReleaseError):release.extract(folder,self.base/'too-big')
        self.assertFalse((self.base/'too-big').exists())
        raw=(folder/'manifest.json').read_bytes()
        (folder/'manifest.json').write_bytes(raw[:-2]+b',"schema_version":1}\n')
        with self.assertRaises(release.ReleaseError):release.verify(folder)

    def test_unexpected_git_stdout_failure_kills_and_reaps_owned_child(self):
        original=subprocess.Popen;children=[]
        class BrokenPipe:
            def __init__(self,stream):self.stream=stream
            def read(self,*args):raise OSError('private injected read failure')
            def close(self):self.stream.close()
        def child(*args,**kwargs):
            process=original([sys.executable,'-c','import time; time.sleep(20)'],**kwargs)
            process.stdout=BrokenPipe(process.stdout);children.append(process);return process
        before={t.ident for t in threading.enumerate()}
        with patch('robot_test_hub.release.subprocess.Popen',side_effect=child),self.assertRaises(release.ReleaseError) as cm:self.build()
        self.assertEqual(str(cm.exception),'release_build_failed')
        self.assertTrue(children)
        self.assertTrue(all(process.poll() is not None for process in children))
        self.assertEqual({t.ident for t in threading.enumerate()},before)

    def test_git_argv_uses_bounded_offline_environment(self):
        original=subprocess.Popen;seen=[]
        def opening(args,*a,**kwargs):
            self.assertIsInstance(args,list);self.assertNotIn('shell',kwargs)
            self.assertEqual(kwargs['env']['GIT_NO_LAZY_FETCH'],'1')
            self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'],'0')
            seen.append(args)
            return original(args,*a,**kwargs)
        before={t.ident for t in threading.enumerate()}
        with patch('robot_test_hub.release.subprocess.Popen',side_effect=opening):self.build()
        self.assertTrue(seen);self.assertEqual({t.ident for t in threading.enumerate()},before)
        with self.assertRaises(release.ReleaseError) as cm:release.build(self.repo,self.base/'bad-ref','--upload-pack=private')
        self.assertNotIn('private',str(cm.exception))


if __name__=='__main__':unittest.main()
