"""Windows stat/fstat timestamp normalization retains exact mutation guards."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import json,os,tempfile,unittest
from unittest.mock import patch
from robot_test_hub import stream_analysis
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
from robot_test_hub.stream_quality import RecordingQualityReducer
PUBLIC=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'

class StreamWindowsIdentityTests(unittest.TestCase):
 def info(self,*,ctime=40,birth=10,**changes):
  values=dict(st_dev=7,st_ino=2**100+3,st_size=20,st_mtime_ns=30,st_ctime_ns=ctime)
  if birth is not None:values['st_birthtime_ns']=birth
  return SimpleNamespace(**{**values,**changes})
 def test_windows_cross_api_birthtime_matches_without_losing_identity(self):
  with patch.object(stream_analysis,'_WINDOWS_STAT',True):
   expected=stream_analysis._sig(self.info(ctime=10))
   self.assertEqual(stream_analysis._sig(self.info(ctime=40)),expected)
   for field in ('st_dev','st_ino','st_size','st_mtime_ns','st_birthtime_ns'):
    with self.subTest(field=field):self.assertNotEqual(stream_analysis._sig(self.info(**{field:999})),expected)
   self.assertEqual(stream_analysis._sig(self.info(ctime=10,birth=None)),expected)
 def test_posix_retains_metadata_change_time_even_when_birthtime_exists(self):
  with patch.object(stream_analysis,'_WINDOWS_STAT',False):
   self.assertNotEqual(stream_analysis._sig(self.info(ctime=10)),stream_analysis._sig(self.info(ctime=40)))
 def prepare(self):
  temp=tempfile.TemporaryDirectory(prefix='stream Windows identity ');self.addCleanup(temp.cleanup)
  root=Path(temp.name);db=open_catalog(root);self.addCleanup(db.close)
  job=Importer(root,db).import_file(PUBLIC,source_type='SYNTHETIC');self.assertEqual(job['state'],'succeeded')
  run=rebuild_from_imports(root,db)['runs'][0]
  runner=stream_analysis.StreamRunner(root,db,reducer=RecordingQualityReducer(),limits={'min_free_bytes':0});self.addCleanup(runner.close)
  scheduled=runner.schedule(run,{})
  return root,db,job,runner,scheduled['stream_id']
 def drain(self,runner,stream_id):
  for _ in range(20000):
   progress=runner.step(stream_id,max_rows=7,max_bytes=4096)
   if progress['state'] in ('ready','failed'):return progress
  self.fail('No terminal state')
 def changed_ctime(self,info,delta):
  return self.info(st_dev=info.st_dev,st_ino=info.st_ino,st_size=info.st_size,st_mtime_ns=info.st_mtime_ns,
                   ctime=info.st_ctime_ns+delta,birth=getattr(info,'st_birthtime_ns',info.st_ctime_ns))
 def test_full_stream_accepts_windows_path_creation_vs_handle_change_time(self):
  root,db,job,runner,stream_id=self.prepare();original=os.fstat
  with patch.object(stream_analysis,'_WINDOWS_STAT',True),patch.object(stream_analysis.os,'fstat',side_effect=lambda fd:self.changed_ctime(original(fd),12345)):
   complete=self.drain(runner,stream_id)
  self.assertEqual(complete['state'],'ready',complete);self.assertTrue(complete['descriptor']['full_source_verified'])
  self.assertGreater(complete['verified_bytes'],0)
 def test_same_handle_change_time_mutation_is_still_rejected(self):
  root,db,job,runner,stream_id=self.prepare();original=os.fstat;calls=0
  def mutate(fd):
   nonlocal calls
   calls+=1;return self.changed_ctime(original(fd),1000*calls)
  with patch.object(stream_analysis,'_WINDOWS_STAT',True),patch.object(stream_analysis.os,'fstat',side_effect=mutate):
   complete=self.drain(runner,stream_id)
  self.assertEqual((complete['state'],complete['error_code']),('failed','stream_source_changed'))
  self.assertEqual(complete['verified_bytes'],0)
 def test_change_time_mutation_between_turns_is_still_rejected(self):
  root,db,job,runner,stream_id=self.prepare();original=os.fstat;calls=0
  def mutate(fd):
   nonlocal calls
   calls+=1;return self.changed_ctime(original(fd),1000*((calls-1)//2))
  with patch.object(stream_analysis,'_WINDOWS_STAT',True),patch.object(stream_analysis.os,'fstat',side_effect=mutate):
   first=runner.step(stream_id,max_bytes=1)
   self.assertNotEqual(first['state'],'failed',first);self.assertEqual(first['verified_bytes'],1)
   complete=runner.step(stream_id,max_bytes=1)
  self.assertEqual((complete['state'],complete['error_code']),('failed','stream_source_changed'))

 def test_change_time_mutation_after_verification_is_rejected_before_ready(self):
  root,db,job,runner,stream_id=self.prepare();original=os.fstat;changed=False
  def mutate(fd):return self.changed_ctime(original(fd),1000 if changed else 0)
  with patch.object(stream_analysis,'_WINDOWS_STAT',True),patch.object(stream_analysis.os,'fstat',side_effect=mutate):
   for _ in range(20000):
    progress=runner.step(stream_id,max_bytes=4096)
    if progress['phase']=='manifest_publish':break
    self.assertNotEqual(progress['state'],'failed',progress)
   else:self.fail('No finalization phase')
   changed=True;complete=self.drain(runner,stream_id)
  self.assertEqual((complete['state'],complete['error_code']),('failed','stream_source_changed'))
  self.assertGreater(complete['verified_bytes'],0)
 @unittest.skipUnless(os.name!='nt' or hasattr(os.stat(__file__),'st_birthtime_ns'),'Windows before 3.12 exposes creation time only')
 def test_rewriting_already_hashed_prefix_with_restored_mtime_is_rejected(self):
  root,db,job,runner,stream_id=self.prepare()
  raw=root/json.loads(db.execute('SELECT pins_json FROM analysis_stream_sources WHERE stream_id=?',(stream_id,)).fetchone()[0])['files']['raw']['path']
  first=runner.step(stream_id,max_bytes=16);self.assertEqual(first['verified_bytes'],16)
  info=raw.stat()
  with raw.open('r+b') as target:
   first_byte=target.read(1);target.seek(0);target.write(bytes([first_byte[0]^1]));target.flush();os.fsync(target.fileno())
  os.utime(raw,ns=(info.st_atime_ns,info.st_mtime_ns))
  complete=self.drain(runner,stream_id)
  self.assertEqual((complete['state'],complete['error_code']),('failed','stream_source_changed'))

 def test_same_content_size_mtime_replacement_between_stat_and_open_is_rejected(self):
  root,db,job,runner,stream_id=self.prepare();raw=root/json.loads(db.execute('SELECT pins_json FROM analysis_stream_sources WHERE stream_id=?',(stream_id,)).fetchone()[0])['files']['raw']['path'];original=runner._file;replaced=False
  @contextmanager
  def replace(path,mode):
   nonlocal replaced
   if path==raw and mode=='rb' and not replaced:
    replaced=True;info=path.stat();replacement=path.with_name(path.name+'.replacement');replacement.write_bytes(path.read_bytes())
    os.utime(replacement,ns=(info.st_atime_ns,info.st_mtime_ns));os.replace(replacement,path)
   with original(path,mode) as stream:yield stream
  with patch.object(runner,'_file',replace):complete=self.drain(runner,stream_id)
  self.assertTrue(replaced)
  self.assertEqual((complete['state'],complete['error_code']),('failed','stream_source_changed'))
  self.assertEqual(complete['verified_bytes'],0)

if __name__=='__main__':unittest.main()
