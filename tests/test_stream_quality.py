"""Bounded full-source quality reducer scope, exact counters and checkpoint tests."""
import copy
import json
import sqlite3
import unittest
from contextlib import closing
from robot_test_hub.stream_quality import RecordingQualityReducer,REFERENCE_LIMIT

SHA='a'*64

def cycle(stamp,scope='in_run',epoch=True,source=SHA):
    return dict(kind='cycle',timestamp_ns=str(stamp),stream_scope=scope,source_sha256=source,
        record_index=stamp,aliases={'epoch_valid':{'value':epoch}})

def observation(index,scope='in_run',**changes):
    return dict(kind='observation',source_sha256=SHA,record_index=index,stream_scope=scope,
        field='/Synthetic/Signal',validity='valid',**changes)

class StreamQualityTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE analysis_stream_unique(stream_id TEXT,generation INTEGER,namespace TEXT,key TEXT,PRIMARY KEY(stream_id,generation,namespace,key))')
        self.reducer=RecordingQualityReducer()
        self.context=dict(start_monotonic_ns='1000000000',end_monotonic_ns='1400000000',completeness='complete')
    def reduce(self,rows,context=None,verified=True):
        state=self.reducer.initial(context or self.context)
        for row in rows:state=self.reducer.consume(self.db,'synthetic-stream',1,state,row)
        return state,self.reducer.finish(state,{'full_source_verified':verified})
    def metric(self,check,name):return next(m['value'] for m in check['metrics'] if m['name']==name)
    def test_run_scope_excludes_long_prefix_suffix_faults_and_gaps(self):
        rows=[cycle(0,'before_run'),observation(1,'before_run',fresh=False),cycle(1000000000),cycle(1200000000),cycle(1400000000),
            observation(2,'after_run',fresh=False),cycle(9000000000,'after_run')]
        state,check=self.reduce(rows)
        self.assertEqual(self.metric(check,'recording_span_seconds'),.4);self.assertEqual(self.metric(check,'valid_cycle_coverage'),1)
        self.assertEqual(check['coverage']['cycle_count'],3);self.assertEqual(self.metric(check,'gap_count'),0);self.assertFalse(check['findings'])
        self.assertEqual(state['observation_count'],0)
    def test_after_run_clock_regression_is_qualified_separately_from_run_counts(self):
        _,check=self.reduce([cycle(1000000000),cycle(1200000000),cycle(1400000000),cycle(2000000000,'after_run'),cycle(1900000000,'after_run')])
        self.assertEqual(check['coverage']['cycle_count'],3);self.assertFalse(check['findings']);self.assertEqual(check['outcome'],'insufficient_data')
        self.assertEqual(check['coverage']['source_order_hazards']['by_scope'],{'after_run':1})
        self.assertIn('full_source_clock_order_unqualified',check['unavailable'])
    def test_exact_dedup_across_observation_and_many_samples_uses_disk(self):
        false=dict(kind='observation',source_sha256=SHA,record_index=7,stream_scope='in_run',field='/Drive/Module0/DriveConnected',validity='valid',type='boolean',value=False)
        sample=dict(kind='sample',source_sha256=SHA,stream_scope='in_run',sensor_availability='disconnected',connection_sources={false['field']:{k:v for k,v in false.items() if k not in ('kind','field')}})
        state,check=self.reduce([cycle(1000000000),false]+[sample]*10000+[cycle(1400000000)])
        finding=next(f for f in check['findings'] if f['kind']=='disconnected_observations')
        self.assertEqual(finding['count'],1);self.assertEqual(check['coverage']['disconnected_source_count'],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_unique').fetchone()[0],1)
    def test_bounded_examples_and_exact_counts_survive_checkpoint_serialization(self):
        state=self.reducer.initial(self.context)
        rows=[cycle(1000000000)]+[observation(i,fresh=False) for i in range(10000)]+[cycle(1400000000)]
        for index,row in enumerate(rows):
            if index==5000:state=json.loads(json.dumps(state))
            state=self.reducer.consume(self.db,'synthetic-stream',1,state,row)
        check=self.reducer.finish(state,{'full_source_verified':True});finding=next(f for f in check['findings'] if f['kind']=='stale_observations')
        self.assertEqual(finding['count'],10000);self.assertEqual(len(finding['source_references']),REFERENCE_LIMIT)
        self.assertEqual(finding['source_references_omitted'],10000-REFERENCE_LIMIT)
        self.assertLess(len(json.dumps(state)),64000)
    def test_source_boundary_resets_gap_and_connection_uniqueness_is_source_scoped(self):
        rows=[cycle(1000000000),observation(1,connected=False),cycle(1400000000,source='b'*64)]
        _,check=self.reduce(rows)
        self.assertEqual(self.metric(check,'gap_count'),0);self.assertEqual(check['coverage']['valid_seconds'],0)
    def test_incomplete_or_unverified_input_cannot_publish_complete_no_finding(self):
        _,check=self.reduce([cycle(1000000000),cycle(1200000000)],verified=False)
        self.assertEqual(check['outcome'],'insufficient_data');self.assertEqual(check['unavailable'],['stream_sources_not_fully_verified'])
        _,check=self.reduce([cycle(1000000000),cycle(1200000000)])
        self.assertEqual(check['outcome'],'insufficient_data');self.assertIn('run_endpoint_coverage_unavailable',check['unavailable'])
    def test_gap_counts_and_reference_omission_are_exact_without_final_extrapolation(self):
        context=dict(start_monotonic_ns='0',end_monotonic_ns=str(300000000*300),completeness='complete')
        _,check=self.reduce([cycle(i*300000000) for i in range(301)],context)
        gap=next(f for f in check['findings'] if f['kind']=='recording_gap')
        self.assertEqual(gap['count'],300);self.assertEqual(gap['intervals_ns_total'],300);self.assertEqual(gap['intervals_ns_omitted'],172)
        self.assertEqual(check['coverage']['valid_seconds'],0);self.assertEqual(self.metric(check,'recording_span_seconds'),90)
    def test_invalid_cycle_timestamp_is_not_a_valid_wall_clock_cycle(self):
        _,check=self.reduce([cycle('invalid')])
        self.assertEqual(check['coverage']['cycle_count'],1);self.assertEqual(check['coverage']['wall_clock_valid_cycles'],0)
        self.assertEqual(check['findings'][0]['kind'],'invalid_cycle_timestamps')

    def test_rollback_removes_dedupe_with_reducer_checkpoint_owner(self):
        self.db.commit();self.db.execute('BEGIN');state=self.reducer.initial(self.context)
        state=self.reducer.consume(self.db,'synthetic-stream',1,state,observation(1,connected=False));self.db.rollback()
        fresh=self.reducer.initial(self.context);fresh=self.reducer.consume(self.db,'synthetic-stream',1,fresh,observation(1,connected=False))
        self.assertEqual(fresh['warnings']['disconnected_observations']['total'],1)

if __name__=='__main__':unittest.main()
