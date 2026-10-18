"""Indexed search/selection known outcomes and bounded writer lifecycle tests."""
from contextlib import closing
import hashlib,json,sqlite3,unittest
from unittest.mock import patch
from robot_test_hub import run_lookup as lookup
from robot_test_hub.runs import RunCatalog,install_schema


def run(identity='r',robot='6391',intervals=None):
 return dict(run_id=identity,robot_id=robot,utc_intervals=[] if intervals is None else [dict(utc_start_ns=str(a),utc_end_ns=str(b),uncertainty_ns=str(c)) for a,b,c in intervals])


def document(identity='revision',runs=None):return dict(revision=identity,runs=runs or [],mappings=[],version='runs-1')


class RunLookupTests(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
  install_schema(self.db);lookup.install_schema(self.db);self.db.commit()

 def seed(self,doc,raw=None):
  self.db.execute('INSERT INTO run_catalog_revisions VALUES (?,?)',(doc['revision'],json.dumps(doc) if raw is None else raw))
  self.db.execute('INSERT INTO run_catalog_state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision',(doc['revision'],));self.db.commit()

 def test_new_publication_is_ready_atomic_and_original_bytes_unchanged(self):
  doc=document(runs=[run('one',intervals=[(10,20,2)]),run('two')])
  lookup.publish_document(self.db,doc)
  raw=self.db.execute('SELECT document FROM run_catalog_revisions').fetchone()[0]
  self.assertEqual(raw,json.dumps(doc,sort_keys=True,separators=(',',':')))
  self.assertEqual(RunCatalog(self.db).search()['items'],doc['runs'])
  chosen,digest,revision=lookup.select_run(self.db,'one')
  original=self.db.execute("SELECT value FROM run_catalog_revisions,json_each(document,'$.runs') WHERE json_extract(value,'$.run_id')='one'").fetchone()[0]
  self.assertEqual(digest,hashlib.sha256(original.encode()).hexdigest())
  self.assertEqual(chosen,doc['runs'][0]);self.assertEqual(revision,'revision')
  self.db.execute('BEGIN');lookup.publish_document(self.db,document('later',[run('later')]))
  self.db.rollback()
  self.assertEqual(lookup.search(self.db)['revision'],'revision')
  self.assertEqual(self.db.execute('SELECT document FROM run_catalog_revisions').fetchone()[0],raw)

 def test_search_integer_overlap_any_piece_unknown_dedupe_and_stable_order(self):
  base=1791003600000000000
  doc=document(runs=[run('last','other',[(base,base+10,2)]),run('first','6391',[(base,base+5,2),(base+100,base+110,2)]),run('unknown')])
  lookup.publish_document(self.db,doc)
  found=lookup.search(self.db,utc_from_ns=base-2,utc_to_ns=base+103)
  self.assertEqual([r['run_id'] for r in found['items']],['last','first']);self.assertEqual(found['total'],2)
  self.assertEqual(lookup.search(self.db,utc_from_ns=base+50,utc_to_ns=base+50)['total'],0)
  unknown=lookup.search(self.db,utc_from_ns=base+50,utc_to_ns=base+50,include_unknown=True)
  self.assertEqual([r['run_id'] for r in unknown['items']],['unknown'])
  page=lookup.search(self.db,robot='6391',limit=1)
  self.assertEqual(page['total'],2);self.assertEqual(page['next_offset'],1)
  self.assertEqual(lookup.search(self.db,robot='6391',offset=1,limit=1)['items'][0]['run_id'],'unknown')

 def test_backfill_is_partial_bounded_and_current_has_priority(self):
  for index in range(60):
   doc=document('old-'+str(index),[run('old')]);self.db.execute('INSERT INTO run_catalog_revisions VALUES (?,?)',(doc['revision'],json.dumps(doc)))
  current=document('zz-current',[run('r'+str(i)) for i in range(4)])
  self.seed(current)
  self.assertIsNone(lookup.search(self.db)['total'])
  state=lookup.tick(self.db,limit=2)
  self.assertEqual(state['partial'],1)
  pending=lookup.search(self.db);self.assertEqual(pending['availability'],'partial');self.assertIsNone(pending['total'])
  lookup.tick(self.db,limit=2)
  self.assertEqual(lookup.search(self.db)['total'],4)
  self.assertGreater(lookup.coverage(self.db)['pending'],0)

 def test_historical_sql_serialization_hash_is_preserved_and_old_revision_reachable(self):
  doc=document('old',[run('one')]);raw='{ "runs" : [ { "robot_id" : "6391", "utc_intervals":[], "run_id" : "one" } ], "revision":"old" }'
  self.seed(doc,raw)
  sqlraw=self.db.execute("SELECT value FROM run_catalog_revisions,json_each(document,'$.runs')").fetchone()[0]
  lookup.tick(self.db)
  selected,digest,_=lookup.select_run(self.db,'one','old')
  self.assertEqual(digest,hashlib.sha256(sqlraw.encode()).hexdigest())
  lookup.publish_document(self.db,document('new',[run('new')]))
  self.assertEqual(lookup.select_run(self.db,'one','old')[0],selected)
  self.assertEqual(self.db.execute("SELECT document FROM run_catalog_revisions WHERE id='old'").fetchone()[0],raw)

 def test_schema_generation_and_derived_mutations_never_serve_stale_rows(self):
  lookup.publish_document(self.db,document(runs=[run()]))
  self.db.execute("UPDATE run_catalog_revisions SET document=document WHERE id='revision'");self.db.commit()
  self.assertEqual(lookup.search(self.db)['availability'],'pending')
  lookup.tick(self.db)
  self.db.execute("UPDATE run_lookup_runs SET robot_id='wrong'");self.db.commit()
  self.assertEqual(lookup.search(self.db)['availability'],'unavailable')
  with self.assertRaises(lookup.LookupUnavailable):lookup.select_run(self.db,'r')
  self.db.execute('DROP INDEX run_lookup_utc')
  self.assertEqual(lookup.search(self.db)['reason'],'run_lookup_schema_invalid')

 def test_rewritten_job_generation_or_count_does_not_look_empty(self):
  for column in ('generation','run_count'):
   with self.subTest(column=column):
    lookup.publish_document(self.db,document(runs=[run()]))
    self.db.execute('UPDATE run_lookup_jobs SET '+column+'='+column+'+1');self.db.commit()
    page=lookup.search(self.db)
    self.assertEqual(page['availability'],'unavailable');self.assertIsNone(page['total'])
    with self.assertRaises(lookup.LookupUnavailable):lookup.select_run(self.db,'r')

 def test_ready_queries_deny_source_document_reads_and_any_writes(self):
  lookup.publish_document(self.db,document(runs=[run()]))
  def authorize(action,table,column,*rest):
   if action==sqlite3.SQLITE_READ and table=='run_catalog_revisions' and column=='document':return sqlite3.SQLITE_DENY
   if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE):return sqlite3.SQLITE_DENY
   return sqlite3.SQLITE_OK
  self.db.set_authorizer(authorize)
  self.assertEqual(lookup.search(self.db)['total'],1)
  self.assertEqual(lookup.select_run(self.db,'r')[0]['run_id'],'r')

 def test_empty_and_invalid_backfill_are_not_false_empty_or_success(self):
  self.assertEqual(lookup.search(self.db)['total'],0)
  self.seed(document(),'{broken')
  self.assertIsNone(lookup.search(self.db)['total'])
  state=lookup.tick(self.db)
  self.assertEqual(state['failed'],1)
  self.assertEqual(lookup.search(self.db)['availability'],'unavailable')

 def test_missing_or_malformed_clock_preserves_selected_evidence_as_all_unknown(self):
  good=dict(utc_start_ns='10',utc_end_ns='20',uncertainty_ns='0')
  cases=(None,{'unsupported':'clock'},[good,{'utc_start_ns':'bad'}],[good,dict(good,uncertainty_ns='-1')])
  for index,pieces in enumerate(cases):
   with self.subTest(pieces=pieces):
    selected=dict(run(),utc_intervals=pieces)
    doc=document('clock-'+str(index),[selected]);lookup.publish_document(self.db,doc)
    self.assertEqual(lookup.select_run(self.db,'r')[0],selected)
    self.assertEqual(lookup.search(self.db,utc_from_ns=15,utc_to_ns=15)['total'],0)
    unknown=lookup.search(self.db,utc_from_ns=15,utc_to_ns=15,include_unknown=True)
    self.assertEqual(unknown['items'],[selected]);self.assertEqual(unknown['total'],1)
    self.assertEqual(self.db.execute('SELECT interval_count FROM run_lookup_runs WHERE revision=?',(doc['revision'],)).fetchone()[0],0)
  selected=run();selected.pop('utc_intervals')
  lookup.publish_document(self.db,document('missing-clock',[selected]))
  self.assertEqual(lookup.select_run(self.db,'r')[0],selected)
  self.assertEqual(lookup.search(self.db,utc_from_ns=15,utc_to_ns=15,include_unknown=True)['total'],1)

 def test_excess_clock_piece_count_remains_resource_unavailable(self):
  selected=run(intervals=[(10,20,0)]*(lookup.MAX_INTERVALS+1))
  doc=document('too-many-clock-pieces',[selected]);self.seed(doc)
  state=lookup.tick(self.db)
  self.assertEqual(state['failed'],1)
  self.assertIsNone(lookup.search(self.db,include_unknown=True)['total'])
  with self.assertRaises(lookup.LookupUnavailable):lookup.select_run(self.db,'r')

 def test_actual_job_vm_exhaustion_is_terminal_and_preserves_committed_fragments(self):
  doc=document('large-legacy',[run('r'+str(i)) for i in range(100)]);self.seed(doc)
  lookup.tick(self.db,limit=1)
  before=self.db.execute('SELECT ordinal,raw_json FROM run_lookup_runs').fetchall()
  raw=self.db.execute('SELECT document FROM run_catalog_revisions').fetchone()[0]
  state=lookup.tick(self.db,max_vm_steps=1000)
  self.assertEqual(state['failed'],1)
  self.assertEqual(self.db.execute('SELECT state,error_code,next_ordinal FROM run_lookup_jobs').fetchone(),
      ('failed','run_lookup_resource_limit',1))
  self.assertEqual(self.db.execute('SELECT ordinal,raw_json FROM run_lookup_runs').fetchall(),before)
  self.assertEqual(self.db.execute('SELECT document FROM run_catalog_revisions').fetchone()[0],raw)
  self.assertEqual(lookup.tick(self.db,max_vm_steps=1000)['failed'],1)
  lookup.publish_document(self.db,document('new-publication',[run('new')]))
  self.assertEqual(lookup.search(self.db)['items'][0]['run_id'],'new')
  self.assertEqual(lookup.tick(self.db)['ready'],1)

 def test_resource_failure_marker_does_not_target_replaced_source_generation(self):
  doc=document('replace-me',[run('r'+str(i)) for i in range(100)]);self.seed(doc)
  original=lookup._fail_resource
  replaced=[]
  def replace(db,job):
   self.assertFalse(db.in_transaction)
   with db:db.execute("UPDATE run_catalog_revisions SET document=document WHERE id='replace-me'")
   replaced.append(db.execute('SELECT generation FROM run_lookup_jobs').fetchone()[0])
   return original(db,job)
  with patch.object(lookup,'_fail_resource',side_effect=replace):
   state=lookup.tick(self.db,max_vm_steps=1000)
  self.assertEqual(state['failed'],0)
  self.assertEqual(self.db.execute('SELECT state,generation FROM run_lookup_jobs').fetchone(),('pending',replaced[0]))
  for _ in range(2):lookup.tick(self.db)
  self.assertEqual(lookup.search(self.db)['total'],100)

 def test_worker_refuses_caller_transaction_before_any_work_or_commit(self):
  self.seed(document(runs=[run()]))
  self.db.execute('CREATE TABLE sentinel(value)');self.db.commit()
  self.db.execute('BEGIN');self.db.execute('INSERT INTO sentinel VALUES (1)')
  statements=[];self.db.set_trace_callback(statements.append)
  try:
   with self.assertRaisesRegex(ValueError,'^run_lookup_worker_requires_owned_transaction$'):
    lookup.tick(self.db,max_vm_steps=1000)
  finally:self.db.set_trace_callback(None)
  self.assertEqual(statements,[])
  self.assertTrue(self.db.in_transaction)
  self.assertEqual(self.db.execute('SELECT value FROM sentinel').fetchall(),[(1,)])
  self.assertEqual(self.db.execute('SELECT state FROM run_lookup_jobs').fetchone()[0],'pending')
  self.db.rollback()
  self.assertEqual(self.db.execute('SELECT value FROM sentinel').fetchall(),[])

 def test_generic_sql_errors_and_uncaptured_metadata_failures_propagate(self):
  self.seed(document(runs=[run()]))
  with patch.object(lookup,'_process_job',side_effect=sqlite3.OperationalError('injected I/O failure')):
   with self.assertRaises(sqlite3.OperationalError):lookup.tick(self.db)
  self.assertEqual(self.db.execute('SELECT state FROM run_lookup_jobs').fetchone()[0],'pending')
  with lookup._bounded(self.db):
   with self.assertRaises(sqlite3.OperationalError):self.db.execute('SELECT * FROM missing_table')
  self.db.execute('DROP INDEX run_lookup_utc');self.db.commit()
  with self.assertRaises(lookup.LookupUnavailable):lookup.tick(self.db)
  self.assertEqual(self.db.execute('SELECT state FROM run_lookup_jobs').fetchone()[0],'pending')

 def test_resource_admission_and_input_type_validation(self):
  doc=document(runs=[dict(run(str(i)),padding='x'*2000) for i in range(4)])
  with patch.object(lookup,'MAX_CATALOG_BYTES',100):lookup.publish_document(self.db,doc)
  with patch.object(lookup,'MAX_PAGE_BYTES',3000):
   page=lookup.search(self.db)
   self.assertEqual(page['availability'],'unavailable');self.assertIsNone(page['total'])
  self.assertIsNone(lookup.search(self.db,max_vm_steps=0)['total'])
  for query in ({'offset':True},{'limit':False},{'utc_from_ns':True,'utc_to_ns':2},{'include_unknown':1},{'utc_from_ns':-(1<<63)-1,'utc_to_ns':0}):
   with self.subTest(query=query),self.assertRaises(ValueError):lookup.search(self.db,**query)
  bad=document('bad',[run('same'),run('same')])
  with self.assertRaises(sqlite3.IntegrityError):lookup.publish_document(self.db,bad)
  self.assertIsNone(self.db.execute("SELECT 1 FROM run_catalog_revisions WHERE id='bad'").fetchone())

if __name__=='__main__':unittest.main()
