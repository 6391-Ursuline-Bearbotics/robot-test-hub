"""Actual imported streams, deterministic batches, durability and admission bounds."""
from contextlib import closing
import copy,json,os,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from robot_test_hub.storage import open_catalog
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.stream_analysis import StreamRunner,Limits,StreamError,_json
from robot_test_hub.stream_quality import RecordingQualityReducer
PUBLIC=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
class StreamSelectionTests(unittest.TestCase):
 def setUp(self):
  temporary=tempfile.TemporaryDirectory(prefix='stream selection ');self.addCleanup(temporary.cleanup)
  self.base=Path(temporary.name);self.root=self.base/'archive';self.root.mkdir();self.db=open_catalog(self.root);self.addCleanup(self.db.close)
  self.imported=Importer(self.root,self.db).import_file(PUBLIC,source_type='SYNTHETIC')
  self.run=rebuild_from_imports(self.root,self.db)['runs'][0]
 def runner(self,root=None,db=None,**limits):
  runner=StreamRunner(root or self.root,db or self.db,reducer=RecordingQualityReducer(),limits={'min_free_bytes':0,'chunk_bytes':4096,**limits})
  self.addCleanup(runner.close);return runner
 def drain(self,runner,stream_id,**budget):
  for _ in range(20000):
   result=runner.step(stream_id,**budget)
   if result['state'] in ('ready','failed','canceled'):return result
  self.fail('bounded stream turns did not finish')
 def test_actual_full_stream_does_not_call_legacy_read_admission(self):
  runner=self.runner()
  with patch.object(Importer,'read_manifest',side_effect=AssertionError('legacy manifest')),patch.object(Importer,'iter_dataset',side_effect=AssertionError('legacy dataset')),patch.object(Importer,'_check_dataset',side_effect=AssertionError('legacy verify')):
   scheduled=runner.schedule(self.run,{})
   result=self.drain(runner,scheduled['stream_id'],max_rows=7,max_bytes=1024,max_seconds=.1)
  self.assertEqual(result['state'],'ready',result)
  descriptor=result['descriptor'];self.assertTrue(descriptor['full_source_verified'])
  self.assertEqual(descriptor['source_type'],'synthetic');self.assertEqual(descriptor['result']['outcome'],'finding')
  dataset=self.root/self.imported['dataset_path']
  with dataset.open('rb') as source:count=sum(1 for _ in source)
  self.assertEqual(descriptor['scanned_rows'],count)
  self.assertGreater(descriptor['chunk_count'],1)
  self.assertTrue(runner.close());self.assertFalse(runner.cleanup_pending)
 def test_same_bytes_fresh_root_and_different_batches_have_same_identity_chain_result(self):
  first=self.runner();stream_id=first.schedule(self.run,{})['stream_id']
  left=self.drain(first,stream_id,max_rows=9,max_bytes=2048,max_seconds=.1)
  root=self.base/'other';root.mkdir()
  with closing(open_catalog(root)) as db:
   imported=Importer(root,db).import_file(PUBLIC,source_type='SYNTHETIC');run=rebuild_from_imports(root,db)['runs'][0]
   second=self.runner(root,db);other=second.schedule(run,{})
   self.assertEqual(stream_id,other['stream_id'])
   right=self.drain(second,stream_id,max_rows=100,max_bytes=15000,max_seconds=.1)
  self.assertEqual(left['state'],'ready');self.assertEqual(right['state'],'ready')
  self.assertEqual(left['selection_chain'],right['selection_chain'])
  self.assertEqual(left['descriptor']['input_digest'],right['descriptor']['input_digest'])
  self.assertEqual(left['descriptor']['result'],right['descriptor']['result'])
 def test_cancel_new_runner_replays_persisted_chunks_without_duplicate_unique_counts(self):
  runner=self.runner();stream_id=runner.schedule(self.run,{})['stream_id']
  for _ in range(100):
   progress=runner.step(stream_id,max_rows=20,max_bytes=4096,max_seconds=.1)
   if progress['chunk_count']>=3:break
  self.assertGreaterEqual(progress['chunk_count'],3)
  count=self.db.execute('SELECT COUNT(*) FROM analysis_stream_unique WHERE stream_id=?',(stream_id,)).fetchone()[0]
  self.assertEqual(runner.cancel(stream_id)['state'],'canceled');runner.close()
  restarted=self.runner();restarted.resume(stream_id)
  result=self.drain(restarted,stream_id,max_rows=11,max_bytes=2048,max_seconds=.1)
  self.assertEqual(result['state'],'ready',result);self.assertGreater(result['replay_bytes'],0)
  final=result['descriptor']['result'];self.assertEqual(final['coverage']['disconnected_source_count'],1)
  self.assertGreaterEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_unique WHERE stream_id=?',(stream_id,)).fetchone()[0],count)
 def test_reordered_unscoped_prefix_keys_keep_selection_but_fail_full_byte_hash(self):
  runner=self.runner();stream_id=runner.schedule(self.run,{})['stream_id']
  for _ in range(100):
   progress=runner.step(stream_id,max_rows=20,max_bytes=4096,max_seconds=.1)
   if progress['chunk_count']>=2:break
  runner.close()
  path=self.root/self.imported['dataset_path'];data=path.read_bytes();first,rest=data.split(b'\n',1)
  value=_json(first);changed=json.dumps(dict(reversed(list(value.items()))),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
  self.assertEqual(len(changed),len(first));self.assertNotEqual(changed,first)
  path.write_bytes(changed+b'\n'+rest)
  resumed=self.runner();result=self.drain(resumed,stream_id,max_rows=20,max_bytes=4096,max_seconds=.1)
  self.assertEqual(result['state'],'failed');self.assertEqual(result['error_code'],'stream_source_hash_mismatch')
 def test_checkpoint_and_chunk_conflicts_are_not_repaired_or_successful(self):
  runner=self.runner();stream_id=runner.schedule(self.run,{})['stream_id']
  for _ in range(100):
   progress=runner.step(stream_id,max_rows=20,max_bytes=4096,max_seconds=.1)
   if progress['chunk_count']>=1:break
  runner.close()
  chunk=self.db.execute('SELECT path FROM analysis_stream_chunks WHERE stream_id=? ORDER BY ordinal LIMIT 1',(stream_id,)).fetchone()[0]
  path=self.root/chunk;original=path.read_bytes();corrupt=b'X'+original[1:];path.write_bytes(corrupt)
  result=self.drain(self.runner(),stream_id,max_rows=20,max_bytes=4096,max_seconds=.1)
  self.assertEqual(result['state'],'failed');self.assertEqual(result['error_code'],'stream_chunk_conflict')
  self.assertEqual(path.read_bytes(),corrupt)
 def test_spool_and_free_space_fail_explicitly_without_partial_ready(self):
  for kind in ('quota','free'):
   with self.subTest(kind=kind):
    runner=self.runner(max_spool_bytes=1) if kind=='quota' else self.runner(min_free_bytes=10**30)
    stream_id=runner.schedule(self.run,{'test_kind':kind})['stream_id']
    result=self.drain(runner,stream_id)
    self.assertEqual(result['state'],'failed');self.assertIn(result['error_code'],('stream_spool_resource_limit','stream_free_space_unavailable'))
    self.assertNotIn('descriptor',result)
 def test_failed_close_retains_strong_handle_ownership_until_retry_succeeds(self):
  runner=self.runner();stream_id=runner.schedule(self.run,{})['stream_id']
  original=Path.open;handles=[]
  class PendingClose:
   def __init__(self,handle):self.handle=handle;self.allow=False
   def __getattr__(self,name):return getattr(self.handle,name)
   def close(self):
    if not self.allow:raise OSError('injected pending close')
    self.handle.close()
  def opened(path,*args,**kwargs):
   handle=original(path,*args,**kwargs)
   if path.suffix=='.wpilog' and args and args[0]=='rb':
    handle=PendingClose(handle);handles.append(handle)
   return handle
  with patch.object(Path,'open',opened):result=runner.step(stream_id,max_bytes=32,max_seconds=.1)
  self.assertEqual(result['state'],'failed')
  self.assertEqual(len(handles),1);self.assertFalse(handles[0].closed)
  self.assertTrue(runner.cleanup_pending);self.assertIn(handles[0],runner._pending_handles)
  self.assertFalse(runner.close());self.assertFalse(handles[0].closed)
  handles[0].allow=True
  self.assertTrue(runner.close());self.assertTrue(handles[0].closed);self.assertFalse(runner.cleanup_pending)

 def test_small_byte_turns_and_fragmented_manifest_preserve_full_completion(self):
  runner=self.runner();stream_id=runner.schedule(self.run,{})['stream_id'];previous=0
  for _ in range(10000):
   progress=runner.step(stream_id,max_rows=3,max_bytes=257,max_seconds=.1)
   self.assertLessEqual(progress['verified_bytes']-previous,257);previous=progress['verified_bytes']
   if progress['state'] in ('ready','failed'):break
  self.assertEqual(progress['state'],'ready',progress)
  manifest=progress['descriptor']['final_manifest'];path=self.root/manifest['path']
  self.assertEqual(path.stat().st_size,manifest['size_bytes'])
  import hashlib
  self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),manifest['sha256'])
  reserved=self.db.execute('SELECT SUM(size_bytes) FROM analysis_stream_reservations WHERE stream_id=?',(stream_id,)).fetchone()[0]
  self.assertEqual(reserved,progress['spool_bytes'])

 def test_context_identity_mismatch_and_source_count_are_rejected(self):
  runner=self.runner()
  with self.assertRaises(StreamError):runner.schedule(self.run,{'run_id':'somebody-else'})
  bad={**self.run,'run_id':'not a safe id'}
  with self.assertRaises(StreamError):runner.schedule(bad,{})
  bad={**self.run,'segment_ids':['a'*64]*129}
  with self.assertRaises(StreamError):runner.schedule(bad,{})
 def test_effective_chunk_policy_is_pinned_and_read_inventory_is_paged(self):
  runner=self.runner();stream_id=runner.schedule(self.run,{})['stream_id']
  other=StreamRunner(self.root,self.db,reducer=RecordingQualityReducer(),limits=dict(min_free_bytes=0,chunk_bytes=8192));self.addCleanup(other.close)
  self.assertNotEqual(stream_id,other.schedule(self.run,{})['stream_id'])
  result=self.drain(runner,stream_id);self.assertEqual(result['state'],'ready')
  page=runner.inventory(stream_id,limit=2);self.assertEqual(len(page['items']),2)
  self.assertEqual(page['total'],result['chunk_count']);self.assertEqual(page['next_after'],1)

if __name__=='__main__':unittest.main()
