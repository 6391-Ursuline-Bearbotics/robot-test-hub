import json
import sqlite3
import unittest
from robot_test_hub.analysis import Runner
from robot_test_hub.review import ReviewStore, install_schema
from robot_test_hub.swerve import compare_baseline, swerve_analyzer, SAMPLE_PROFILE
from test_analysis import evidence, series, PROFILE, HASH, CONTEXT

EPOCH=1791003600000000000


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.runner=Runner(self.db)
        self.store=ReviewStore(self.db)
        self.assignment={'id':'assignment-fl','component_id':'component-fl','robot_id':'synthetic-6391',
            'location':'front-left','start_utc_ns':str(EPOCH-1_000_000_000),'end_utc_ns':None,
            'reviewer':'synthetic-reviewer','rationale':'Synthetic physical identity oracle'}
        self.store.assign_component(self.assignment)
        self.jobs=[self.job(i,error=.02*i) for i in range(3)]
        self.approval={'id':'baseline-1','label':'Synthetic D1 known-good','job_ids':[j['job_id'] for j in self.jobs],
            'reviewer':'synthetic-reviewer','rationale':'Explicit fixture healthy selection after synthetic inspection','exclusions':{}}

    def job(self,i,error=0,**overrides):
        context={**CONTEXT,'run_id':'run-'+str(i),'start_utc_ns':str(EPOCH+i*3_000_000_000),
            'end_utc_ns':str(EPOCH+i*3_000_000_000+2_000_000_000),'component_ids':{'front-left':'component-fl'},
            'build_hash':'c'*64,'wheel_radius_m':.0508,'command_stage':'final_io_optimized_cosine_desaturated',
            'sample_profile':SAMPLE_PROFILE,**overrides}
        rows=series(module='component-fl',module_position='front-left',speed=1,measured=1+error)
        config={'drive_error_limit_mps':.5,'threshold_revision':'synthetic-limits-1'}
        return self.runner.run(swerve_analyzer([PROFILE]),evidence(rows,**context),config)

    def test_approval_is_explicit_immutable_and_does_not_drift(self):
        baseline=self.store.approve_baseline(self.approval)
        self.assertEqual(baseline['independent_runs'],['run-0','run-1','run-2'])
        distribution=next(v for v in baseline['distributions'] if v['name']=='drive_rmse_mps')
        self.assertAlmostEqual(distribution['median'],.02)
        self.assertAlmostEqual(distribution['mad'],.02)
        self.job(3,error=.2)
        self.assertEqual(self.store.approve_baseline(self.approval),baseline)
        self.assertEqual(len(self.store.records('baseline')),1)
        with self.assertRaises(ValueError):
            self.store.approve_baseline({**self.approval,'rationale':'changed selection'})
        self.assertEqual(len(baseline['assignment_evidence']),1)

    def test_incompatible_context_and_unknown_physical_assignment_are_excluded(self):
        incompatible=self.job(3,battery_id='different-battery')
        with self.assertRaisesRegex(ValueError,'incompatible'):
            self.store.approve_baseline({**self.approval,'job_ids':[self.jobs[0]['job_id'],self.jobs[1]['job_id'],incompatible['job_id']]})
        missing=self.job(4,component_ids={'front-left':'unassigned'})
        with self.assertRaisesRegex(ValueError,'assignment'):
            self.store.approve_baseline({**self.approval,'job_ids':[missing['job_id'],self.jobs[1]['job_id'],self.jobs[2]['job_id']]})
        with self.assertRaises(ValueError):
            self.store.approve_baseline({**self.approval,'job_ids':[self.jobs[0]['job_id']]*3})

    def test_component_swap_revisions_and_overlap_prevention(self):
        with self.assertRaisesRegex(ValueError,'overlap'):
            self.store.assign_component({**self.assignment,'id':'assignment-new','component_id':'new-component'})
        close={**self.assignment,'revision':2,'expected_revision':1,'end_utc_ns':str(EPOCH+7_000_000_000)}
        self.store.assign_component(close)
        self.store.assign_component({**self.assignment,'id':'assignment-new','component_id':'new-component',
                                     'start_utc_ns':str(EPOCH+7_000_000_000)})
        self.assertEqual(len(self.store.records('assignment')),2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM review_records WHERE kind='assignment'").fetchone()[0],3)
        with self.assertRaisesRegex(ValueError,'entire run'):
            self.store.approve_baseline(self.approval)

    def test_maintenance_splits_run_and_is_revisioned_record_only(self):
        payload={'id':'maintenance','robot_id':'synthetic-6391','kind':'physical_repair','reviewer':'fixture',
                 'rationale':'Synthetic repair record; no physical action','effective_utc_ns':str(EPOCH+1_000_000_000),
                 'component_id':'component-fl'}
        a=self.store.record_maintenance(payload)
        self.assertEqual(a['action_scope'],'record_only')
        self.assertEqual(self.store.record_maintenance(payload),a)
        with self.assertRaisesRegex(ValueError,'splits'):
            self.store.approve_baseline(self.approval)

    def test_maintenance_between_runs_splits_cohort(self):
        self.store.record_maintenance({'id':'between-runs','robot_id':'synthetic-6391',
            'kind':'physical_repair','reviewer':'fixture','rationale':'Repair between runs',
            'effective_utc_ns':str(EPOCH+2_500_000_000),'component_id':'component-fl'})
        with self.assertRaisesRegex(ValueError,'splits the selected cohort'):
            self.store.approve_baseline(self.approval)

    def test_baseline_rejects_mixed_analyzer_configuration(self):
        context={**self.jobs[2]['provenance']['context'],'run_id':'run-policy'}
        changed=self.runner.run(swerve_analyzer([PROFILE]),
            evidence(series(module='component-fl',module_position='front-left',speed=1,measured=1),**context),
            {'drive_error_limit_mps':100,'threshold_revision':'different-policy'})
        with self.assertRaisesRegex(ValueError,'analyzer versions or configurations'):
            self.store.approve_baseline({**self.approval,'job_ids':
                [self.jobs[0]['job_id'],self.jobs[1]['job_id'],changed['job_id']]})

    def test_independence_requires_nonoverlapping_run_intervals(self):
        repeated=self.job(3,start_utc_ns=str(EPOCH),end_utc_ns=str(EPOCH+2_000_000_000))
        with self.assertRaisesRegex(ValueError,'overlapping UTC intervals'):
            self.store.approve_baseline({**self.approval,'job_ids':
                [self.jobs[0]['job_id'],self.jobs[1]['job_id'],repeated['job_id']]})

    def test_finding_review_repair_validation_chain_and_bundle(self):
        incident=self.job(3,error=1)
        validation=self.job(4)
        repair=self.store.record_maintenance({'id':'repair','robot_id':'synthetic-6391','kind':'physical_repair',
            'component_id':'component-fl','reviewer':'fixture','rationale':'Synthetic repair reference',
            'effective_utc_ns':str(EPOCH+11_500_000_000)})
        payload={'id':'review','job_id':incident['job_id'],'finding_index':0,'disposition':'confirmed_hardware',
            'reviewer':'fixture','rationale':'Synthetic externally confirmed fixture','maintenance_id':repair['id'],
            'validation_job_id':validation['job_id']}
        reviewed=self.store.review_finding(payload)
        self.assertEqual(self.store.review_finding(payload),reviewed)
        edited=self.store.review_finding({**payload,'revision':2,'expected_revision':1,'disposition':'expected_behavior',
            'rationale':'Synthetic correction retained'})
        self.assertEqual(edited['revision'],2)
        bundle=self.store.regression_bundle({'id':'bundle','reviewer':'fixture','finding_review_id':'review'})
        self.assertFalse(bundle['contains_raw_data'])
        self.assertEqual(len(bundle['evidence']),2)
        self.assertEqual(bundle['finding_review']['revision'],2)
        self.assertEqual(bundle['evidence'][0]['source_hashes'],[HASH])
        self.assertIn('physical repair',bundle['replay_limitation'])

    def test_validation_must_be_independent_later_run(self):
        incident=self.job(3,error=1)
        payload={'id':'review','job_id':incident['job_id'],'finding_index':0,'disposition':'unresolved',
                 'reviewer':'fixture','rationale':'Synthetic incident'}
        with self.assertRaisesRegex(ValueError,'independent'):
            self.store.review_finding({**payload,'validation_job_id':incident['job_id']})
        with self.assertRaisesRegex(ValueError,'later'):
            self.store.review_finding({**payload,'validation_job_id':self.jobs[0]['job_id']})

    def test_robust_comparison_requires_approved_context_and_revision(self):
        self.store.approve_baseline(self.approval)
        baseline=self.store.get_baseline(self.approval['id'])
        observed=self.job(3,error=1)
        comparisons,findings,unavailable=compare_baseline(observed['metrics'],observed['provenance']['context'],
            'synthetic',baseline,{'threshold_revision':'synthetic-statistical-floor-1'})
        self.assertTrue(comparisons)
        self.assertTrue(findings)
        drive=next(v for v in comparisons if v['name']=='drive_rmse_mps')
        self.assertAlmostEqual(drive['baseline_median'],.02)
        self.assertAlmostEqual(drive['engineering_upper_limit'],.14)
        self.assertFalse(drive['thresholds_qualified_on_hardware'])
        _,bad,why=compare_baseline(observed['metrics'],{**observed['provenance']['context'],'battery_id':'different'},'synthetic',baseline)
        self.assertFalse(bad)
        self.assertIn('baseline_context_incompatible',why)
        _,bad,why=compare_baseline(observed['metrics'],observed['provenance']['context'],'synthetic',baseline)
        self.assertFalse(bad)
        self.assertIn('baseline_finding_threshold_revision_unconfigured',why)

    def test_later_comparison_requires_loaded_history_and_rejects_repair_boundary(self):
        approved=self.store.approve_baseline(self.approval)
        observed=self.job(4,error=1)
        args=(observed['metrics'],observed['provenance']['context'],'synthetic')
        config={'threshold_revision':'comparison-policy'}
        _,findings,why=compare_baseline(*args,approved,config)
        self.assertFalse(findings)
        self.assertIn('baseline_maintenance_assignment_history_unavailable',why)
        before=self.store.get_baseline(approved['id'])
        self.assertTrue(compare_baseline(*args,before,config)[1])
        self.store.record_maintenance({'id':'later-repair','robot_id':'synthetic-6391',
            'kind':'physical_repair','reviewer':'fixture','rationale':'Repair after approval',
            'effective_utc_ns':str(EPOCH+9_000_000_000),'component_id':'component-fl'})
        after=self.store.get_baseline(approved['id'])
        self.assertNotEqual(before['comparison_history_revision'],after['comparison_history_revision'])
        _,findings,why=compare_baseline(*args,after,config)
        self.assertFalse(findings)
        self.assertIn('baseline_maintenance_configuration_boundary',why)
        self.assertEqual(self.store.records('baseline')[0],approved)

    def test_later_comparison_rejects_changed_assignment_with_identical_context(self):
        approved=self.store.approve_baseline(self.approval)
        self.store.assign_component({**self.assignment,'revision':2,'expected_revision':1,
            'end_utc_ns':str(EPOCH+9_000_000_000)})
        self.store.assign_component({**self.assignment,'id':'new-assignment',
            'start_utc_ns':str(EPOCH+9_000_000_000)})
        observed=self.job(4,error=1)
        _,findings,why=compare_baseline(observed['metrics'],observed['provenance']['context'],
            'synthetic',self.store.get_baseline(approved['id']),{'threshold_revision':'policy'})
        self.assertFalse(findings)
        self.assertIn('baseline_component_assignment_boundary',why)

    def test_schema_rollback_and_snapshot_types(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('BEGIN')
        install_schema(db)
        db.rollback()
        self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='review_records'").fetchall(),[])
        snapshot=self.store.snapshot()
        self.assertEqual(snapshot['action_scope'],'local_review_records_only')
        self.assertEqual(snapshot['dispositions'][0],'confirmed_hardware')
        self.assertEqual(len(snapshot['analysis_jobs']),3)

import tempfile
import threading
from pathlib import Path


class ConcurrentReviewTests(unittest.TestCase):
    def test_concurrent_connections_cannot_overlap_assignments(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'review.sqlite3'
            db=sqlite3.connect(path)
            ReviewStore(db)
            db.close()
            barrier=threading.Barrier(2)
            outcomes=[]
            def attempt(index):
                connection=sqlite3.connect(path,timeout=2)
                try:
                    store=ReviewStore(connection)
                    payload={'id':'assignment-'+str(index),'component_id':'component-'+str(index),
                        'robot_id':'robot','location':'front-left','start_utc_ns':str(EPOCH),'end_utc_ns':None,
                        'reviewer':'fixture','rationale':'Concurrency fixture'}
                    barrier.wait(2)
                    try:
                        store.assign_component(payload)
                        outcomes.append('saved')
                    except ValueError:
                        outcomes.append('conflict')
                finally:
                    connection.close()
            threads=[threading.Thread(target=attempt,args=(i,)) for i in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(3)
            self.assertEqual(sorted(outcomes),['conflict','saved'])
            db=sqlite3.connect(path)
            self.assertEqual(len(ReviewStore(db).records('assignment')),1)
            db.close()

    def test_concurrent_revision_content_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'review.sqlite3'
            initial={'id':'assignment','component_id':'component','robot_id':'robot','location':'front-left',
                'start_utc_ns':str(EPOCH),'end_utc_ns':None,'reviewer':'fixture','rationale':'Initial'}
            db=sqlite3.connect(path)
            ReviewStore(db).assign_component(initial)
            db.close()
            barrier=threading.Barrier(2)
            outcomes=[]
            def attempt(index):
                connection=sqlite3.connect(path,timeout=2)
                try:
                    store=ReviewStore(connection)
                    barrier.wait(2)
                    try:
                        store.assign_component({**initial,'revision':2,'expected_revision':1,
                            'rationale':'Concurrent correction '+str(index)})
                        outcomes.append('saved')
                    except ValueError:
                        outcomes.append('conflict')
                finally:connection.close()
            threads=[threading.Thread(target=attempt,args=(i,)) for i in range(2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(3)
            self.assertEqual(sorted(outcomes),['conflict','saved'])
            db=sqlite3.connect(path)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_records').fetchone()[0],2)
            db.close()

    def test_caller_transaction_is_not_committed_by_mutation(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        store=ReviewStore(db)
        db.execute('BEGIN IMMEDIATE')
        store.assign_component({'id':'assignment','component_id':'component','robot_id':'robot','location':'front-left',
            'start_utc_ns':str(EPOCH),'end_utc_ns':None,'reviewer':'fixture','rationale':'Transactional fixture'})
        self.assertTrue(db.in_transaction)
        db.rollback()
        self.assertEqual(store.records('assignment'),[])
