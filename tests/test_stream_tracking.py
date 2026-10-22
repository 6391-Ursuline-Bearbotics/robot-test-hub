"""Pipeline-10 tests: explicit normalized contracts and invented original WPILOGs."""
from contextlib import closing
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import reports,stream_analysis
from robot_test_hub import stream_tracking as tracking
from robot_test_hub import stream_tracking_store as work
from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
from robot_test_hub.swerve import MODULE_POSITIONS,SAMPLE_PROFILE
import test_auto_swerve as invented

START=1000000000

def contract():
    return dict(run_id='normalized-test',boot_id='invented-boot',robot_id='invented-robot',config_hash='b'*64,
        test_id='unit-contract',surface='invented-flat',battery_id='invented-battery',mapping_revision='explicit',
        stream_source_type='simulation',runtime_mode='SIM',component_ids={p:'module-'+str(i) for i,p in enumerate(MODULE_POSITIONS)},
        start_monotonic_ns=str(START),end_monotonic_ns=str(START+20),completeness='complete',
        analysis_plan=dict(ideal_simulation_cycle_policy=True,swerve_configuration=dict(minimum_eligible_seconds=1e-9,
            minimum_coverage=1,drive_error_limit_mps=.5,sustained_error_seconds=1e-9,threshold_revision='invented-1')))

def sample(module,stamp,error=1.,boundary=0):
    return dict(kind='swerve_sample',module_index=module,module_id='module-'+str(module),module_position=MODULE_POSITIONS[module],
        timestamp_ns=stamp,enabled=True,connected=True,fresh=True,validity='valid',command_speed_mps=2.,measured_speed_mps=2.+error,
        command_angle_rad=0.,measured_angle_rad=.1,command_timestamp_ns=stamp,measurement_timestamp_ns=stamp,
        command_stage='final_io_optimized_cosine_desaturated',sample_profile=SAMPLE_PROFILE,speed_unit='meters per second',
        angle_unit='radians',source_reference=dict(source_hash='a'*64,cycle_record_index=stamp-START),boundary=['a'*64,boundary])

class TrackingStoreTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='tracking normalized ');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.db=open_catalog(self.root);self.addCleanup(self.db.close)
    def reduced(self,stamps=(START,START+19,START+20),errors=(1.,9.,0.),context=None):
        state=tracking.initial_tracking(context or contract())
        for index,(stamp,error) in enumerate(zip(stamps,errors)):
            state['cycles']+=1;state['first']=str(stamp) if state['first'] is None else state['first'];state['last']=str(stamp)
            with self.db:
                for module in range(4):
                    tracking.consume_sample(self.db,'test',1,state,sample(module,stamp,error),
                        dict(stream_source_ordinal=0,stream_line_ordinal=index),index)
        return state
    def finish(self,state,limit=1):
        turns=0
        while turns<100:
            with self.db:final=work.finalize_step(self.db,'test',1,state['work'],limit,1.,lambda:0.)
            turns+=1
            if final['complete']:return final,turns
        self.fail('bounded finalization did not complete')
    def test_exact_integer_boundary_irregular_weights_and_stable_rmse(self):
        state=self.reduced();final,turns=self.finish(state)
        self.assertGreater(turns,1)
        for module in range(4):self.assertEqual(final['quantiles'][str(module)+':drive'],1.)
        output=tracking.finish_tracking(state,dict(full_source_verified=True,source_hashes=['a'*64]),final)
        self.assertEqual(output['outcome'],'finding',output)
        values=[m['value'] for m in output['metrics'] if m['name']=='drive_rmse_mps']
        self.assertEqual(len(values),4)
        for value in values:self.assertAlmostEqual(value,math.sqrt(5))
        self.assertEqual(len(output['findings']),4)
        self.assertIn('100_cumulative_ns_ge_95_total_ns',output['coverage']['quantile_policy'])
    def test_qualified_baseline_only_criteria_are_reported_truthfully(self):
        context=contract();context['analysis_plan']['swerve_configuration'].update(drive_error_limit_mps=None,steering_error_limit_rad=None)
        state=self.reduced(context=context);final,_=self.finish(state)
        comparison=dict(name='drive_rmse_mps',module_id='module-0',observed=.1,engineering_upper_limit=.2)
        with patch.object(tracking,'compare_baseline',return_value=([comparison],[],[])):
            output=tracking.finish_tracking(state,dict(full_source_verified=True,source_hashes=['a'*64]),final)
        self.assertEqual(output['outcome'],'evaluated_no_finding')
        self.assertTrue(output['coverage']['baseline_finding_criteria_evaluated'])
        self.assertFalse(output['coverage']['engineering_finding_criteria_configured'])
        self.assertNotIn('tracking_finding_criteria_not_configured_or_available',output['unavailable'])
    def test_equal_errors_tie_order_and_restart_cursor(self):
        state=self.reduced(errors=(1.,1.,0.))
        with self.db:first=work.finalize_step(self.db,'test',1,state['work'],1,1.,lambda:0.)
        self.assertFalse(first['complete']);self.assertEqual(first['metric_index'],1)
        # New SQLite connection resumes the committed percentile cursor.
        self.db.close();self.db=open_catalog(self.root);self.addCleanup(self.db.close)
        final,_=self.finish(state)
        self.assertEqual(final['quantiles']['0:drive'],1.)
        valid=work.validation_initial()
        for _ in range(100):
            if work.validate_step(self.db,'test',1,state['work'],valid,1,1.,lambda:0.):break
        else:self.fail('work validation did not complete')
        self.assertEqual(valid['chain'],state['work']['chain'])
    def test_sample_tamper_missing_row_and_finalization_hash_are_rejected(self):
        state=self.reduced()
        for change in ('error','missing'):
            with self.subTest(change=change):
                self.db.execute('SAVEPOINT mutation')
                if change=='error':self.db.execute("UPDATE analysis_tracking_samples SET error=99 WHERE sequence=0")
                else:self.db.execute('DELETE FROM analysis_tracking_samples WHERE sequence=0')
                with self.assertRaises(ValueError):
                    work.validate_step(self.db,'test',1,state['work'],work.validation_initial(),100,1.,lambda:0.)
                self.db.execute('ROLLBACK TO mutation');self.db.execute('RELEASE mutation')
        self.finish(state)
        with self.db:self.db.execute("UPDATE analysis_tracking_finalization SET state_json='{}'")
        with self.assertRaises(ValueError):work.finalize_step(self.db,'test',1,state['work'],1,1.,lambda:0.)
    def test_gap_and_boundary_do_not_bridge_or_improve_denominator(self):
        state=tracking.initial_tracking(contract())
        for i,(stamp,boundary) in enumerate(((START,0),(START+10,1),(START+20,1))):
            for module in range(4):
                with self.db:tracking.consume_sample(self.db,'test',1,state,sample(module,stamp,boundary=boundary),
                                                     dict(stream_source_ordinal=0,stream_line_ordinal=i),i)
        for module in state['modules']:
            self.assertEqual(module['expected_ns'],'20');self.assertEqual(module['eligible_ns'],'10')
            self.assertEqual(module['exclusions'],{'source_lifecycle_boundary':1})
    def test_finite_extreme_moments_and_error_overflow_never_publish_infinity(self):
        state=self.reduced(errors=(1e150,1e150,0.));final,_=self.finish(state)
        output=tracking.finish_tracking(state,dict(full_source_verified=True,source_hashes=['a'*64]),final)
        for value in [m['value'] for m in output['metrics'] if m['value'] is not None]:self.assertTrue(math.isfinite(value))
        other=tracking.initial_tracking(contract())
        a=sample(0,START);a.update(command_speed_mps=-1e308,measured_speed_mps=1e308)
        b=sample(0,START+20)
        with self.db:
            tracking.consume_sample(self.db,'overflow',1,other,a,dict(stream_source_ordinal=0,stream_line_ordinal=0),0)
            tracking.consume_sample(self.db,'overflow',1,other,b,dict(stream_source_ordinal=0,stream_line_ordinal=1),1)
        self.assertEqual(other['modules'][0]['exclusions'],{'nonfinite_error_difference':1})
    def test_positive_work_and_mid_percentile_cursor_survive_sqlite_backup(self):
        state=self.reduced(errors=(1.,9.,0.))
        with self.db:first=work.finalize_step(self.db,'test',1,state['work'],1,1.,lambda:0.)
        self.assertFalse(first['complete'])
        import sqlite3
        path=self.root/'normalized-copy.sqlite3'
        with closing(sqlite3.connect(path)) as destination:self.db.backup(destination)
        with closing(sqlite3.connect(path)) as restored:
            expected=work.validation_initial()
            while not work.validate_step(restored,'test',1,state['work'],expected,2,1.,lambda:0.):pass
            while True:
                with restored:final=work.finalize_step(restored,'test',1,state['work'],2,1.,lambda:0.)
                if final['complete']:break
            self.assertEqual(final,self.finish(state,limit=1)[0])
    def test_trace_and_interval_examples_have_exact_totals_and_shared_budget(self):
        context=contract();context['end_monotonic_ns']=str(START+200)
        stamps=tuple(START+i for i in range(201))
        state=self.reduced(stamps=stamps,errors=(1.,)*len(stamps),context=context)
        while True:
            with self.db:final=work.finalize_step(self.db,'test',1,state['work'],100,1.,lambda:0.)
            if final['complete']:break
        output=tracking.finish_tracking(state,dict(full_source_verified=True,source_hashes=['a'*64]),final)
        modules=output['coverage']['modules']
        self.assertEqual(sum(len(m['evidence_trace']) for m in modules),128)
        self.assertEqual(sum(m['evidence_trace_total'] for m in modules),800)
        for module in modules:
            self.assertEqual(module['evidence_trace_retained']+module['evidence_trace_omitted'],module['evidence_trace_total'])
            self.assertTrue(module['evidence_trace_truncated'])
    def test_known_disabled_terminal_does_not_require_motion_but_unknown_authority_does(self):
        for terminal in (False,None):
            context=contract();context['analysis_plan']['swerve_configuration']['minimum_coverage']=.5
            state=tracking.initial_tracking(context)
            for index,stamp in enumerate((START,START+10,START+20)):
                state['cycles']+=1;state['first']=str(START);state['last']=str(stamp)
                for module in range(4):
                    current=sample(module,stamp)
                    if index==2:current.update(enabled=terminal,validity='missing_or_invalid',command_speed_mps=None,measured_speed_mps=None)
                    with self.db:tracking.consume_sample(self.db,'terminal-'+str(terminal),1,state,current,dict(stream_source_ordinal=0,stream_line_ordinal=index),index)
            while True:
                with self.db:final=work.finalize_step(self.db,'terminal-'+str(terminal),1,state['work'],10,1.,lambda:0.)
                if final['complete']:break
            output=tracking.finish_tracking(state,dict(full_source_verified=True,source_hashes=['a'*64]),final)
            if terminal is False:
                self.assertEqual(output['coverage']['evaluated_modules'],4)
                for module in output['coverage']['modules']:
                    self.assertEqual(module['required_enabled_cycles'],2)
                    self.assertEqual(module['mapped_enabled_cycles'],2)
                    self.assertEqual(module['expected_enabled_seconds'],20/1e9)
                    self.assertEqual(module['fraction'],.5)
            else:
                self.assertEqual(output['outcome'],'insufficient_data')
                self.assertIn('enabled_status_unknown_or_invalid',output['unavailable'])
    def test_covering_ordered_index_does_not_build_temp_sort(self):
        plans=self.db.execute('EXPLAIN QUERY PLAN SELECT error,sequence,duration_ns FROM analysis_tracking_samples INDEXED BY analysis_tracking_order '
                              'WHERE stream_id=? AND generation=? AND module=? AND metric=? AND (error,sequence)>(?,?) '
                              'ORDER BY error,sequence LIMIT ?',('test',1,0,'drive',0.,0,1)).fetchall()
        text=' '.join(row[3] for row in plans)
        self.assertIn('COVERING INDEX analysis_tracking_order',text);self.assertNotIn('TEMP B-TREE',text)

class MotionContractTests(unittest.TestCase):
    def snapshot(self):
        from robot_test_hub import stream_motion_mapper as motion
        context=contract();context['wheel_radius_m']=.0498348
        context['stream_motion_measurement_source']='module_io_if_array_absent'
        context['analysis_plan'].update(config_hash=context['config_hash'],build_hash='c'*64)
        stamp=START+10;held={}
        def observation(field,value,kind='boolean'):
            return dict(field=field,value=value,type=kind,validity='valid',record_index=len(held)+1,entry_id=len(held)+1,
                entry_generation=1,record_timestamp_ns=str(stamp),cycle_timestamp_ns=str(stamp),source_sha256='a'*64)
        for field,value in motion.SCHEMAS.items():held[field]=observation(field,value,'structschema')
        held['/RealOutputs/SwerveStates/SetpointsOptimized']=observation('/RealOutputs/SwerveStates/SetpointsOptimized',[dict(velocity=2.,angle=dict(value=0.))]*4,'struct:SwerveModuleVelocity[]')
        for module in range(4):
            for field in ('DriveConnected','TurnConnected','TurnEncoderConnected'):
                name=f'/Drive/Module{module}/'+field;held[name]=observation(name,True)
            name=f'/Drive/Module{module}/DriveVelocityRadPerSec';held[name]=observation(name,1.5/.0498348,'double')
            name=f'/Drive/Module{module}/TurnPosition';held[name]=observation(name,dict(value=.1),'struct:Rotation2d')
        cycle=dict(timestamp_ns=str(stamp),timestamp_record_index=100,source_sha256='a'*64,
                   aliases=dict(enabled=dict(value=True,validity='valid'),configuration_sha256=dict(value=context['config_hash'],validity='valid'),
                       source_sha256=dict(value='c'*64,validity='valid'),runtime_mode=dict(value='SIM',validity='valid')))
        return dict(cycle=cycle,held=held,boundary=1,measurement_array_seen=False),context
    def test_scalar_mapping_requires_explicit_source_and_radius_and_preserves_origins(self):
        from robot_test_hub import stream_motion_mapper as motion
        snapshot,context=self.snapshot()
        samples,reasons=motion.samples(snapshot,context,'simulation')
        self.assertEqual(reasons,[])
        self.assertEqual(len(samples),4)
        for row in samples:
            self.assertEqual(row['validity'],'valid');self.assertAlmostEqual(row['measured_speed_mps'],1.5)
            self.assertAlmostEqual(row['measured_angle_rad'],.1)
            refs=row['source_reference']['module_measurement_references']
            self.assertEqual(len(refs),2)
            self.assertTrue(refs[0]['field'].endswith('DriveVelocityRadPerSec'))
            self.assertTrue(refs[1]['field'].endswith('TurnPosition'))
        for changed in ({'wheel_radius_m':None},{'stream_motion_measurement_source':'measured_array'}):
            rows,_=motion.samples(snapshot,{**context,**changed},'simulation')
            self.assertTrue(all(row['validity']!='valid' for row in rows))
        snapshot['measurement_array_seen']=True
        rows,_=motion.samples(snapshot,context,'simulation')
        self.assertTrue(all(row['validity']!='valid' for row in rows))
    def test_source_classification_never_promotes_synthetic_or_real_freshness(self):
        from robot_test_hub import stream_motion_mapper as motion
        snapshot,context=self.snapshot()
        for source in ('synthetic','real','historical'):
            with self.subTest(source=source):
                rows,_=motion.samples(snapshot,context,source)
                self.assertTrue(all(row['fresh'] is False and row['measurement_timestamp_ns'] is None for row in rows))
    def test_optional_plan_selector_preserves_old_plan_bytes_and_rejects_unknown(self):
        from robot_test_hub.analysis_plan import validate_plan
        plan=dict(id='plan',robot_id='robot',start_utc_ns='1000000000',end_utc_ns=None,reviewer='Invented test',rationale='Unit policy only',
            build_hash='a'*64,config_hash='b'*64,test_id='test',surface='invented',battery_id='battery',wheel_radius_m=.0498348,swerve_configuration={})
        old=validate_plan(plan);self.assertNotIn('stream_tracking_measurement_source',old)
        selected=validate_plan({**plan,'stream_tracking_measurement_source':'module_io_if_array_absent'})
        self.assertEqual(selected['stream_tracking_measurement_source'],'module_io_if_array_absent')
        for bad in ('guess',True,None):
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate_plan({**plan,'stream_tracking_measurement_source':bad})

class TrackingOriginalTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory(prefix='tracking original synthetic ');self.addCleanup(temp.cleanup)
        root=Path(temp.name).resolve();db=open_catalog(root);self.addCleanup(db.close)
        source=root/'invented.wpilog';source.write_bytes(invented.protocol_recording())
        imported=Importer(root,db).import_file(source,source_type='SYNTHETIC');self.assertEqual(imported['state'],'succeeded')
        document=rebuild_from_imports(root,db)
        return root,db,document
    def runner(self,root,db,**options):
        runner=reports.StreamReportRunner(root,db,limits={'min_free_bytes':0,'chunk_bytes':4096,**options});self.addCleanup(runner.close)
        return runner
    def drain(self,root,db,runner,**budgets):
        for _ in range(10000):
            with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=reports.advance_streams(root,db,runner,**budgets)
            self.assertNotIn(progress['state'],('failed','canceled'),progress)
            if progress.get('report'):return progress
        self.fail('no final report')
    def test_original_pipeline10_preserves_synthetic_source_and_historical_status_dispatch(self):
        root,db,document=self.fixture();runner=self.runner(root,db)
        for version in ('automatic-reports-8','automatic-reports-9','automatic-reports-10'):
            reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version=version)
            progress=self.drain(root,db,runner,max_rows=3,max_bytes=2048)
            report=progress['report'];self.assertEqual(report['source_type'],'synthetic')
            self.assertEqual(len(report['checks']),5)
            check=next(c for c in report['checks'] if c['analyzer_id']=='swerve-tracking')
            self.assertEqual(check['analyzer_version'],tracking.VERSION if version.endswith('10') else 'stream-unconverted-1')
            self.assertEqual(check['outcome'],'insufficient_data')
            if version.endswith('10'):
                self.assertIn('physical_acquisition_time_not_qualified',check['unavailable'])
                self.assertTrue(check['coverage']['streamed_incident_proof_available'] is False)
                self.assertIn('stream_reduction',progress['descriptor'])
    def test_restart_mid_source_and_during_finalization_keeps_exact_report(self):
        root,db,document=self.fixture();runner=self.runner(root,db)
        scheduled=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version='automatic-reports-10')[0]
        for _ in range(1000):
            with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=runner.step(scheduled['stream_id'],max_bytes=4096,max_rows=1)
            if progress['chunk_count']>=1:break
        self.assertGreater(progress['chunk_count'],0)
        runner.close();runner=self.runner(root,db)
        for _ in range(1000):
            with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=runner.step(scheduled['stream_id'],max_bytes=4096,max_rows=1)
            if progress['phase']=='reducer_finalize':break
        self.assertEqual(progress['phase'],'reducer_finalize',progress)
        with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=runner.step(scheduled['stream_id'],max_bytes=4096,max_rows=1)
        self.assertEqual(progress['phase'],'reducer_finalize')
        runner.close();runner=self.runner(root,db)
        completed=self.drain(root,db,runner,max_bytes=4096,max_rows=1)
        self.assertEqual(completed['report']['source_type'],'synthetic')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_tracking_finalization').fetchone()[0],1)
    def test_coherent_backup_restores_finalization_and_historical_contracts(self):
        from robot_test_hub.backup import Backup,restore,verify
        root,db,document=self.fixture();runner=self.runner(root,db)
        sid=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version='automatic-reports-10')[0]['stream_id']
        for _ in range(1000):
            with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=runner.step(sid,max_rows=1,max_bytes=4096)
            if progress['phase']=='reducer_finalize':break
        with patch.object(stream_analysis.time,'monotonic',return_value=0):runner.step(sid,max_rows=1,max_bytes=4096)
        external=tempfile.TemporaryDirectory(prefix='tracking backup synthetic ');self.addCleanup(external.cleanup)
        destination=Path(external.name)/'backup'
        Backup(root,destination).create('tracking-partial');verify(destination/'tracking-partial')
        recovered=Path(external.name)/'restored'
        restore(destination/'tracking-partial',recovered)
        original=self.drain(root,db,runner,max_rows=2,max_bytes=4096)
        with closing(open_catalog(recovered)) as restored:
            other=reports.StreamReportRunner(recovered,restored,limits={'min_free_bytes':0,'chunk_bytes':4096})
            try:complete=self.drain(recovered,restored,other,max_rows=3,max_bytes=4096)
            finally:other.close()
        self.assertEqual(original['report'],complete['report'])
    def test_wal_limit_and_transaction_growth_fail_before_publication(self):
        root,db,document=self.fixture()
        runner=self.runner(root,db,work_wal_bytes=1)
        sid=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version='automatic-reports-10')[0]['stream_id']
        progress=runner.step(sid)
        self.assertEqual((progress['state'],progress['error_code']),('failed','stream_spool_resource_limit'))
        self.assertIsNone(db.execute('SELECT report_id FROM analysis_stream_jobs WHERE stream_id=?',(sid,)).fetchone()[0])
    def test_transaction_growth_failure_rolls_back_new_work_rows(self):
        root,db,document=self.fixture()
        runner=stream_analysis.StreamRunner(root,db,reducer=tracking.TrackingReducer(),limits={'min_free_bytes':0,'work_transaction_bytes':1})
        self.addCleanup(runner.close)
        with self.assertRaises(stream_analysis.StreamError):
            with runner._reduction_transaction():
                db.execute('INSERT INTO analysis_stream_unique VALUES(?,?,?,?)',('rollback-probe',1,'probe','x'*100000))
        self.assertIsNone(db.execute("SELECT 1 FROM analysis_stream_unique WHERE stream_id='rollback-probe'").fetchone())
    def test_new_schema_and_quota_exhaustion_fail_visible(self):
        root,db,document=self.fixture()
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],11)
        work.validate_schema(db)
        runner=self.runner(root,db,max_spool_bytes=1)
        scheduled=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version='automatic-reports-10')[0]
        progress=runner.step(scheduled['stream_id'])
        self.assertEqual((progress['state'],progress['error_code']),('failed','stream_spool_resource_limit'))
        self.assertIsNone(db.execute('SELECT report_id FROM analysis_stream_jobs').fetchone()[0])

if __name__=='__main__':unittest.main()
