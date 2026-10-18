"""Bounded policy dispatch isolates resource exhaustion from corruption."""
from pathlib import Path
import sqlite3,tempfile,unittest
from unittest.mock import patch
from robot_test_hub import reports
from robot_test_hub.stream_analysis import StreamError
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
PUBLIC=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'

class StreamDispatchBudgetTests(unittest.TestCase):
 def setUp(self):
  temporary=tempfile.TemporaryDirectory(prefix='stream dispatcher budget ');self.addCleanup(temporary.cleanup)
  self.root=Path(temporary.name);self.db=open_catalog(self.root);self.addCleanup(self.db.close)
  Importer(self.root,self.db).import_file(PUBLIC,source_type='SYNTHETIC')
  document=rebuild_from_imports(self.root,self.db);document={**document,'runs':document['runs'][:1]}
  self.scheduled=reports.generate(self.root,self.db,document,pipeline_version='automatic-reports-8')[0]
  self.runner=reports.StreamReportRunner(self.root,self.db,limits={'min_free_bytes':0});self.addCleanup(self.runner.close)
 def test_metadata_budget_exhaustion_preserves_job_and_allows_next_turn(self):
  before=tuple(self.db.execute('SELECT state,phase,selected_rows FROM analysis_stream_jobs').fetchone())
  child=self.runner.runners[reports.STREAM_VERSION]
  with patch.object(reports,'STREAM_DISPATCH_SQL_STEP_LIMIT',0),patch.object(reports,'STREAM_DISPATCH_SQL_STEP_INTERVAL',1),patch.object(child,'step',side_effect=AssertionError('No partial candidate may execute')):
   with self.assertRaises(StreamError) as failure:self.runner.step()
  self.assertEqual(failure.exception.code,'stream_sql_resource_limit')
  self.assertEqual(tuple(self.db.execute('SELECT state,phase,selected_rows FROM analysis_stream_jobs').fetchone()),before)
  self.assertNotEqual(self.runner.step()['state'],'unavailable')
 def test_dispatch_guard_is_cleared_before_child_and_sql_errors_remain_visible(self):
  def child(*args,**kwargs):
   total=self.db.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<20000) SELECT SUM(x) FROM n').fetchone()[0]
   return {'state':'child','total':total}
  with patch.object(reports,'STREAM_DISPATCH_SQL_STEP_LIMIT',10000),patch.object(reports,'STREAM_DISPATCH_SQL_STEP_INTERVAL',1),patch.object(self.runner.runners[reports.STREAM_VERSION],'step',side_effect=child):
   self.assertEqual(self.runner.step()['total'],200010000)
  with self.db:self.db.execute('DROP TABLE analysis_stream_jobs')
  with self.assertRaises(sqlite3.DatabaseError):self.runner.step()
  self.assertEqual(self.db.execute('SELECT 1').fetchone()[0],1)
 def test_outer_transaction_rejection_preserves_callers_progress_handler(self):
  calls=0
  def callback():
   nonlocal calls
   calls+=1;return 0
  self.db.set_progress_handler(callback,1)
  try:
   self.db.execute('BEGIN');before=calls
   with self.assertRaisesRegex(ValueError,'owned_transaction'):self.runner.step()
   self.db.execute('SELECT 1');self.assertGreater(calls,before);self.assertTrue(self.db.in_transaction)
  finally:self.db.rollback();self.db.set_progress_handler(None,0)

if __name__=='__main__':unittest.main()
