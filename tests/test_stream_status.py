"""Bounded incremental status policies and durable checkpoint equivalence."""
import copy
import json
import sqlite3
import unittest
from robot_test_hub.stream_status import RecordedStatusReducer
from robot_test_hub import stream_mapper
from robot_test_hub.stream_analysis import install_schema
from test_system_status import Rows as SystemRows,LO,HI,BATTERY,COUNT,FLAG,evaluate as system_reference
from test_phoenix_status import Rows as PhoenixRows,evaluate as phoenix_reference

CONTEXT=dict(start_monotonic_ns=str(LO),end_monotonic_ns=str(HI),completeness='complete')


def feed(rows,context=None,restart_every=0):
    reducer=RecordedStatusReducer();state=reducer.initial(context or CONTEXT)
    db=sqlite3.connect(':memory:');install_schema(db)
    try:
        for index,original in enumerate(rows):
            row=copy.deepcopy(original)
            row['stream_scope']='before_run' if row.get('before_run') else 'in_run'
            reducer.consume(db,'test',1,state,row)
            if restart_every and index%restart_every==0:
                state=json.loads(json.dumps(state,allow_nan=False));reducer=RecordedStatusReducer()
        descriptor=dict(full_source_verified=True,source_type='synthetic',source_hashes=['a'*64],source_count=1,source_hazards={'total':0,'references':[],'truncated':False})
        return reducer.finish(state,descriptor),state,db.execute('SELECT count(*) FROM analysis_stream_unique').fetchone()[0]
    finally:db.close()


class StreamStatusTests(unittest.TestCase):
    def test_complete_system_held_records_match_source_reviewed_policy(self):
        rows=SystemRows().snapshot(LO,initial=True).snapshot(LO+100000000).snapshot(HI)
        actual,state,_=feed(rows.rows);check=actual['checks']['system-status-observation'];old=system_reference(rows)
        self.assertEqual(check['outcome'],old['outcome']);self.assertEqual(check['metrics'],old['metrics'])
        self.assertEqual(check['coverage']['fields'],old['coverage']['fields'])
        self.assertLess(len(json.dumps(state)),300000)

    def test_counter_and_true_flag_deduplicate_held_values(self):
        rows=SystemRows().snapshot(LO,initial=True).snapshot(LO+100000000,{COUNT:4,FLAG:True}).snapshot(HI)
        check=feed(rows.rows)[0]['checks']['system-status-observation']
        self.assertEqual(sorted((f['kind'],f['observations']) for f in check['findings']),[('recorded_system_fault_counter_increased',1),('recorded_system_fault_true',1)])

    def test_irregular_left_held_weights_and_endpoint(self):
        rows=SystemRows().snapshot(LO,{BATTERY:10},initial=True).snapshot(LO+50000000,{BATTERY:12}).snapshot(HI,{BATTERY:100})
        check=feed(rows.rows)[0]['checks']['system-status-observation'];metric=check['metrics'][2]
        self.assertAlmostEqual(metric['value'],11.5);self.assertEqual(metric['sample_count'],2);self.assertEqual(metric['eligible_seconds'],.2)

    def test_pre_run_counter_never_seeds_comparison(self):
        rows=SystemRows().snapshot(LO-100000000,{COUNT:100},initial=True,before=True).snapshot(LO).snapshot(LO+100000000,{COUNT:4}).snapshot(HI,{COUNT:5})
        check=feed(rows.rows)[0]['checks']['system-status-observation']
        self.assertEqual(len(check['findings']),1);self.assertEqual(check['findings'][0]['source_references'][0]['recorded_change'],'1')

    def test_nanosecond_cycle_association_qualifies_first_counter(self):
        lo=LO+123;hi=HI+123;rows=SystemRows().snapshot(lo,initial=True).snapshot(hi,{COUNT:1})
        for row in rows.rows:
            if row['kind']=='observation':row['record_timestamp_ns']=str(int(row['record_timestamp_ns'])//1000*1000)
        check=feed(rows.rows,{**CONTEXT,'start_monotonic_ns':str(lo),'end_monotonic_ns':str(hi)})[0]['checks']['system-status-observation']
        self.assertEqual(check['findings'][0]['kind'],'recorded_system_fault_counter_increased')

    def test_lifecycle_and_gap_break_comparison_without_erasing_held_flags(self):
        rows=SystemRows().snapshot(LO,initial=True).snapshot(LO+100000000,{FLAG:True,COUNT:2})
        rows.control(COUNT).snapshot(HI,{COUNT:0},generation=2)
        check=feed(rows.rows)[0]['checks']['system-status-observation']
        self.assertFalse(any(f['kind'].endswith('decreased') for f in check['findings']))
        rows=SystemRows().snapshot(LO,{FLAG:True},initial=True).snapshot(LO+300000000,{COUNT:3})
        check=feed(rows.rows,{**CONTEXT,'end_monotonic_ns':str(LO+300000000)})[0]['checks']['system-status-observation']
        self.assertEqual(check['metrics'][2]['value'],None);self.assertEqual(check['findings'][0]['observations'],1)

    def test_complete_phoenix_matches_bounded_reference_counts(self):
        rows=PhoenixRows().snapshot(1,LO).snapshot(2,LO+100000000).snapshot(3,HI)
        check=feed(rows.rows)[0]['checks']['phoenix-status-observation'];old=phoenix_reference(rows)
        self.assertEqual(check['outcome'],old['outcome']);self.assertEqual(check['metrics'],old['metrics'])
        self.assertEqual([s['distinct_sdk_observations'] for s in check['coverage']['module_observations']],[3]*4)

    def test_false_status_survives_missing_timing_and_is_deduplicated(self):
        field='/Drive/Module0/PhoenixDriveVelocityStatusOk'
        rows=PhoenixRows().snapshot(1,LO,overrides={field:{'value':False},'/Drive/Module0/PhoenixObservationClockValid':{'value':False}}).snapshot(2,HI,held=(field,))
        check=feed(rows.rows)[0]['checks']['phoenix-status-observation']
        self.assertEqual(check['outcome'],'finding');self.assertEqual(check['findings'][0]['observed_false_status_records'],1)
        self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])

    def test_json_checkpoint_scheduling_invariance(self):
        rows=PhoenixRows().snapshot(1,LO).snapshot(2,LO+100000000).snapshot(3,HI)
        first=feed(rows.rows)[0]
        for interval in (1,7,53,257):self.assertEqual(feed(rows.rows,restart_every=interval)[0],first)

    def test_huge_finite_voltage_stays_finite(self):
        rows=SystemRows().snapshot(LO,{BATTERY:1e308},initial=True).snapshot(LO+100000000,{BATTERY:-1e308}).snapshot(HI,{BATTERY:1e308})
        value=feed(rows.rows)[0]['checks']['system-status-observation']['metrics'][2]['value']
        self.assertEqual(value,0.0)

    def test_irrelevant_lifecycle_does_not_grow_checkpoint(self):
        reducer=RecordedStatusReducer();state=reducer.initial(CONTEXT);db=sqlite3.connect(':memory:');install_schema(db)
        try:
            for entry in range(10000):reducer.consume(db,'test',1,state,dict(kind='control',control='start',source_sha256='a'*64,field='/Irrelevant',entry_id=entry+1,entry_generation=1,record_index=entry,record_timestamp_ns=str(LO),stream_scope='in_run'))
            self.assertEqual(state['mapper']['held'],{});self.assertEqual(state['mapper']['epochs'],{})
        finally:db.close()

    def test_version_mismatch_is_explicit(self):
        with self.assertRaisesRegex(ValueError,'version_mismatch'):RecordedStatusReducer().initial({**CONTEXT,'stream_mapper_version':'wrong'})

    def test_sequence_lifecycle_restart_is_new_observation_not_regression(self):
        rows=PhoenixRows().snapshot(5,LO)
        field='/Drive/Module0/PhoenixObservationSequence'
        entry=rows.entries[('a'*64,field)]
        rows.rows.append(dict(kind='control',control='finish',source_sha256='a'*64,field=field,entry_id=entry,entry_generation=1,record_index=rows.index['a'*64],record_timestamp_ns=str(LO+50000000)))
        rows.index['a'*64]+=1
        rows.snapshot(1,LO+100000000,overrides={field:{'entry_generation':2}}).snapshot(2,HI,overrides={field:{'entry_generation':2}})
        check=feed(rows.rows)[0]['checks']['phoenix-status-observation']
        summary=check['coverage']['module_observations'][0]
        self.assertEqual(summary['sequence_regressions'],0)
        self.assertEqual(summary['distinct_sdk_observations'],3)

    def test_status_requires_eof_and_full_source_order_qualification(self):
        rows=SystemRows().snapshot(LO,initial=True).snapshot(HI)
        reducer=RecordedStatusReducer();state=reducer.initial(CONTEXT)
        db=sqlite3.connect(':memory:');install_schema(db)
        try:
            for original in rows.rows:reducer.consume(db,'test',1,state,{**original,'stream_scope':'in_run'})
            descriptor=dict(full_source_verified=False,source_hazards={'total':0})
            check=reducer.finish(copy.deepcopy(state),descriptor)['checks']['system-status-observation']
            self.assertEqual(check['outcome'],'insufficient_data');self.assertIn('stream_sources_not_fully_verified',check['unavailable'])
            descriptor.update(full_source_verified=True,source_hazards={'total':1})
            check=reducer.finish(state,descriptor)['checks']['system-status-observation']
            self.assertEqual(check['outcome'],'insufficient_data');self.assertIn('full_source_order_or_reference_unqualified',check['unavailable'])
        finally:db.close()

    def test_many_warning_records_keep_exact_totals_and_shared_reference_budget(self):
        rows=SystemRows();count=300
        for index in range(count):rows.snapshot(LO+index*1000000,{COUNT:index,FLAG:True},initial=index==0)
        check,state,_=feed(rows.rows,{**CONTEXT,'end_monotonic_ns':str(LO+(count-1)*1000000)})
        check=check['checks']['system-status-observation']
        self.assertEqual(sum(f['observations'] for f in check['findings']),599)
        refs=sum(sum(isinstance(v,dict) for v in ref.values()) for finding in check['findings'] for ref in finding['source_references'])
        refs+=sum(summary['latest_reference'] is not None for summary in check['coverage']['fields'])
        self.assertLessEqual(refs,128)
        for finding in check['findings']:
            self.assertEqual(finding['source_references_total'],len(finding['source_references'])+finding['source_references_omitted'])
            self.assertTrue(finding['source_references_truncated'])
        self.assertLess(len(json.dumps(state)),300000)

    def test_invalid_timing_breaks_continuity_without_recounting_same_held_sequence(self):
        clock='/Drive/Module0/PhoenixObservationClockValid'
        sequence='/Drive/Module0/PhoenixObservationSequence'
        rows=PhoenixRows().snapshot(1,LO).snapshot(1,LO+100000000,held=(sequence,),overrides={clock:{'value':False}}).snapshot(1,HI,held=(sequence,))
        check=feed(rows.rows)[0]['checks']['phoenix-status-observation']
        summary=check['coverage']['module_observations'][0]
        self.assertEqual(summary['timing_cycles'],2)
        self.assertEqual(summary['distinct_sdk_observations'],1)
        self.assertEqual(summary['held_snapshot_cycles'],1)
        self.assertEqual(summary['sequence_regressions'],0)
