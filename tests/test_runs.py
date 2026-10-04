import sqlite3
import unittest

from robot_test_hub.runs import RunCatalog, derive_runs, install_schema


def cycle(t, enabled, mode='TELEOPERATED', *, boot='b', epoch=1791003600000000, valid=True, run=None):
    values = {'enabled':enabled,'robot_mode':mode,'boot_id':boot,'robot_id':'synthetic',
              'epoch_us':epoch+t//1000,'epoch_valid':valid,'run_id':run}
    return {'kind':'cycle','timestamp_ns':str(t),
            'aliases':{k:{'value':v,'updated_in_cycle':True,'record_index':1} for k,v in values.items()}}


class RunTests(unittest.TestCase):
    def test_multiple_files_one_run_and_multiple_runs_one_file(self):
        a = [cycle(0,False), cycle(20_000_000,True,'AUTONOMOUS'), cycle(40_000_000,True,'AUTONOMOUS')]
        b = [cycle(40_000_000,True,'AUTONOMOUS'),cycle(60_000_000,True),cycle(80_000_000,False),
             cycle(100_000_000,True),cycle(120_000_000,False)]
        doc = derive_runs([('a',a),('b',b)])
        self.assertEqual(len(doc['runs']),2)
        first, second = doc['runs']
        self.assertEqual(first['segment_ids'],['a','b'])
        self.assertEqual([x['mode'] for x in first['phases']],['AUTONOMOUS','TELEOPERATED'])
        self.assertEqual(first['completeness'],'complete')
        self.assertEqual(first['wall_clock_quality'],'anchored')
        self.assertEqual(second['start_monotonic_ns'],'100000000')

    def test_gaps_reboots_unknown_state_and_ending_enabled_are_incomplete(self):
        a = [cycle(0,True),cycle(20_000_000,True),cycle(1_000_000_000,True),cycle(1_020_000_000,None)]
        b = [cycle(0,False,boot='other'),cycle(20_000_000,True,boot='other')]
        doc=derive_runs([('a',a),('b',b)])
        self.assertEqual(len(doc['runs']),3)
        self.assertTrue(all(r['completeness']=='incomplete' for r in doc['runs']))
        self.assertEqual([r['end_reason'] for r in doc['runs']],['recording_gap','unknown_state','end_of_recording'])
        self.assertNotEqual(doc['runs'][0]['run_id'],doc['runs'][2]['run_id'])

    def test_discontinuity_search_does_not_bridge_utc_jump(self):
        rows=[cycle(0,False),cycle(20_000_000,True),cycle(40_000_000,True,epoch=1791003602000000),
              cycle(60_000_000,False,epoch=1791003602000000)]
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        catalog=RunCatalog(db)
        first=catalog.rebuild([('a',rows)])
        second=catalog.rebuild([('a',rows)])
        self.assertEqual(first['revision'],second['revision'])
        self.assertEqual(db.execute('SELECT count(*) FROM run_catalog_revisions').fetchone()[0],1)
        self.assertEqual(catalog.search(utc_from_ns=1791003601000000000,utc_to_ns=1791003601000000000)['total'],0)
        self.assertEqual(catalog.search(utc_from_ns=1791003602040000000,utc_to_ns=1791003602040000000)['total'],1)
        changed=catalog.rebuild([('a',rows)],max_gap_ns=100_000_000)
        self.assertNotEqual(first['revision'],changed['revision'])
        self.assertEqual(db.execute('SELECT count(*) FROM run_catalog_revisions').fetchone()[0],2)

    def test_unknown_clock_accessible_and_conflicts_do_not_merge(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        catalog=RunCatalog(db)
        rows=[cycle(0,False,valid=False),cycle(20_000_000,True,valid=False),cycle(40_000_000,False,valid=False)]
        catalog.rebuild([('a',rows)])
        self.assertEqual(catalog.search()['total'],1)
        self.assertEqual(catalog.search(utc_from_ns=1,utc_to_ns=2)['total'],0)
        self.assertEqual(catalog.search(utc_from_ns=1,utc_to_ns=2,include_unknown=True)['total'],1)
        doc=derive_runs([('a',rows),('b',[cycle(20_000_000,False,valid=False)])])
        self.assertEqual(len(doc['runs']),0)

    def test_held_epoch_is_not_a_new_anchor(self):
        rows=[cycle(0,False),cycle(20_000_000,True),cycle(40_000_000,False)]
        rows[1]['aliases']['epoch_us']['updated_in_cycle']=False
        rows[2]['aliases']['epoch_us']['updated_in_cycle']=False
        doc=derive_runs([('a',rows)])
        self.assertEqual(doc['runs'][0]['wall_clock_quality'],'unavailable')

    def test_invalid_fresh_epoch_splits_mapping_and_overlap_order_is_stable(self):
        rows=[cycle(0,False),cycle(20_000_000,True),cycle(40_000_000,False)]
        rows[1]['aliases']['epoch_us']['value']='NaN'
        doc=derive_runs([('a',rows)])
        self.assertEqual(len(doc['mappings'][0]['pieces']),2)
        self.assertEqual(doc['mappings'][0]['rejected'][0]['reason'],'invalid_epoch')
        fresh=cycle(20_000_000,True)
        held=cycle(20_000_000,True)
        held['aliases']['epoch_us']['updated_in_cycle']=False
        a=('a',[cycle(0,False),fresh])
        b=('b',[held,cycle(40_000_000,False)])
        self.assertEqual(derive_runs([a,b]),derive_runs([b,a]))
