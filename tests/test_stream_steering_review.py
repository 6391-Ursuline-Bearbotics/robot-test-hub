"""Independent normalized-boundary checks for streamed recorded steering."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import stream_steering as steering
from robot_test_hub.swerve import MODULE_POSITIONS, SAMPLE_PROFILE

START = 1000000000


def context(end, **configuration):
    settings = dict(minimum_eligible_seconds=.01, minimum_coverage=.1,
                    minimum_stable_seconds=.1, settle_dwell_seconds=.1,
                    recorded_jump_limit_rad=2., threshold_revision='independent-invented')
    settings.update(configuration)
    return dict(run_id='invented-review', boot_id='invented-boot', robot_id='invented-robot',
                config_hash='b'*64, test_id='invented-test', surface='invented-flat', battery_id='invented-battery',
                mapping_revision='explicit', stream_source_type='real', runtime_mode='REAL', completeness='complete',
                component_ids={p:'module-'+str(i) for i,p in enumerate(MODULE_POSITIONS)},
                start_monotonic_ns=str(START), end_monotonic_ns=str(START+round(end*1e9)),
                analysis_plan=dict(steering_configuration=settings))


def sample(module, seconds, target=0., actual=0., **changes):
    stamp=START+round(seconds*1e9)
    value=dict(kind='swerve_sample', module_index=module, module_id='module-'+str(module),
               module_position=MODULE_POSITIONS[module], timestamp_ns=stamp, enabled=True, connected=True,
               fresh=False, validity='valid', command_speed_mps=2., measured_speed_mps=2.,
               command_angle_rad=target, measured_angle_rad=actual, command_timestamp_ns=stamp,
               measurement_timestamp_ns=stamp, command_stage='final_io_optimized_cosine_desaturated',
               sample_profile=SAMPLE_PROFILE, speed_unit='meters per second', angle_unit='radians',
               source_reference=dict(source_hash='a'*64, measured_source_hash='a'*64,
                   cycle_record_index=stamp-START, measured_record_index=stamp-START,
                   measured_updated_in_cycle=True), boundary=['a'*64,0])
    value.update(changes)
    return value


class SteeringReviewTests(unittest.TestCase):
    def run_samples(self, timeline, config=None, roundtrip=False):
        state=steering.initial_steering(config or context(timeline[-1][0]))
        values={i:[] for i in range(4)};seen=set()
        def contribute(module,error,weight):
            self.assertIs(type(weight),int);self.assertGreater(weight,0)
            values[module].append((error,weight))
        def unique(module,identity):
            key=(module,json.dumps(identity,sort_keys=True));fresh=key not in seen;seen.add(key);return fresh
        for seconds,target,actual,changes in timeline:
            for module in range(4):steering.consume_sample(state,sample(module,seconds,target,actual,**changes),contribute,unique)
            if roundtrip:state=json.loads(json.dumps(state,allow_nan=False))
        steering.flush(state)
        once=copy.deepcopy(state);steering.flush(state);self.assertEqual(state,once)
        quantiles={}
        for module,pairs in values.items():
            total=sum(weight for _,weight in pairs);cumulative=0;value=None
            for error,weight in sorted(pairs):
                cumulative+=weight
                if 100*cumulative>=95*total:value=error;break
            quantiles[str(module)+':recorded_steering']=value
        return steering.finish_steering(state,dict(full_source_verified=True,source_hashes=['a'*64]),quantiles)

    def test_final_endpoint_step_is_zero_duration_censored_once(self):
        timeline=[(0.,0.,0.,{}),(.1,0.,0.,{}),(.2,.3,0.,{})]
        output=self.run_samples(timeline,roundtrip=True)
        for module in output['coverage']['modules']:
            events=module['settling_events'];self.assertEqual(len(events),1)
            event=events[0]
            self.assertEqual(event['start_monotonic_ns'],event['end_monotonic_ns'])
            self.assertEqual(event['observed_seconds'],0.)
            self.assertEqual(event['state'],'censored');self.assertIsNone(event['settling_seconds'])
            self.assertEqual(event['censor_reason'],'stable_window_too_short')
            self.assertEqual(module['recorded_interval_count'],2)

    def test_dwell_entry_and_hysteresis_survive_every_saved_boundary(self):
        timeline=[(0.,0.,0.,{}),(.1,.3,0.,{}),(.2,.3,.3,{}),(.3,.3,.3,{}),
                  (.4,.3,.4,{}),(.5,.3,.3,{}),(.6,.3,.2,{}),(.7,.3,.3,{})]
        config=context(.7,oscillation_crossing_limit=1,settling_limit_seconds=.15)
        expected=self.run_samples(timeline,config)
        actual=self.run_samples(timeline,config,roundtrip=True)
        self.assertEqual(actual,expected)
        self.assertEqual(actual['outcome'],'finding')
        for module in actual['coverage']['modules']:
            event=module['settling_events'][0]
            self.assertEqual(event['state'],'settled');self.assertAlmostEqual(event['settling_seconds'],.1)
            self.assertEqual(module['oscillation_events'][0]['crossings'],2)
            self.assertFalse(module['physical_acquisition_time_qualified'])
            self.assertTrue(module['eligible'])

    def test_invalid_and_lifecycle_boundaries_censor_without_extending_dwell(self):
        for changes in ({'validity':'invalid'},{'boundary':['a'*64,1]}):
            with self.subTest(changes=changes):
                timeline=[(0.,0.,0.,{}),(.1,.3,0.,{}),(.2,.3,.3,{}),(.3,.3,.3,changes),
                          (.4,.3,.3,changes),(.5,.3,.3,changes)]
                output=self.run_samples(timeline,roundtrip=True)
                for module in output['coverage']['modules']:
                    event=module['settling_events'][0]
                    self.assertEqual(event['state'],'censored')
                    self.assertEqual(event['end_monotonic_ns'],str(START+200000000))
                    self.assertEqual(event['observation_end_reason'],'gap_or_invalid')
                    self.assertIsNone(event['settling_seconds'])

    def test_gap_counts_expected_time_without_joining_stable_window(self):
        timeline=[(0.,0.,0.,{}),(.1,.3,0.,{}),(.2,.3,.3,{}),(.6,.3,.3,{}),(.7,.3,.3,{})]
        output=self.run_samples(timeline,roundtrip=True)
        for module in output['coverage']['modules']:
            self.assertAlmostEqual(module['eligible_seconds'],.3)
            self.assertAlmostEqual(module['expected_enabled_seconds'],.7)
            self.assertAlmostEqual(module['fraction'],3/7)
            self.assertEqual(module['settling_events'][0]['end_monotonic_ns'],str(START+200000000))

    def test_late_warning_survives_shared_event_and_reference_exhaustion(self):
        timeline=[(i/100.,0.,.1 if i%2 else -.1,{}) for i in range(140)]
        timeline.extend([(1.4,.3,0.,{}),(1.5,.3,0.,{}),(1.6,.3,0.,{}),(1.7,.3,0.,{})])
        config=context(1.7,recorded_jump_limit_rad=.15,settling_limit_seconds=.1,minimum_stable_seconds=.1)
        output=self.run_samples(timeline,config,roundtrip=True)
        for module in output['coverage']['modules']:
            self.assertEqual(module['stored_event_count'],128)
            self.assertLessEqual(len(module['evidence_trace']),128)
            self.assertEqual(module['evidence_trace_total'],len(timeline)-1)
            self.assertEqual(module['evidence_trace_omitted'],len(timeline)-1-len(module['evidence_trace']))
            self.assertEqual(module['settling_events'],[])
            self.assertEqual(module['event_storage']['settling_events']['total'],1)
            self.assertEqual(module['event_storage']['settling_events']['omitted'],1)
        late=[f for f in output['findings'] if f['kind']=='recorded_steering_settling_limit']
        self.assertEqual(len(late),4)
        for finding in late:self.assertEqual(finding['event_count'],1)

    def test_pi_signed_speed_optimization_does_not_create_false_step(self):
        timeline=[(0.,0.,0.,{}),(.1,math.pi,math.pi,dict(command_speed_mps=-2.,measured_speed_mps=-2.)),
                  (.2,math.pi,math.pi,dict(command_speed_mps=-2.,measured_speed_mps=-2.))]
        output=self.run_samples(timeline,roundtrip=True)
        for module in output['coverage']['modules']:
            self.assertEqual(module['settling_events'],[])
            self.assertEqual(module['recorded_jump_events'],[])
        for metric in output['metrics']:
            if metric['name']=='recorded_steering_rmse_rad':self.assertEqual(metric['value'],0.)

    def test_record_boundary_keeps_expected_time_but_closes_the_response(self):
        state=steering.initial_steering(context(.3))
        for stamp in (0.,.1):
            for module in range(4):steering.consume_sample(state,sample(module,stamp),lambda *args:None,lambda *args:True)
        steering.boundary(state,'source_lifecycle_or_invalid_record')
        state=json.loads(json.dumps(state))
        for stamp in (.2,.3):
            for module in range(4):steering.consume_sample(state,sample(module,stamp),lambda *args:None,lambda *args:True)
        steering.flush(state)
        for module in state['modules']:
            self.assertEqual(module['expected_ns'],'300000000')
            self.assertEqual(module['eligible_ns'],'200000000')
            self.assertEqual(module['recorded_interval_count'],2)

    def test_strict11_build_and_config_admission_preserves_fixed10_missing_build(self):
        from test_stream_tracking import MotionContractTests
        from robot_test_hub import stream_motion_mapper as mapper
        for field in ('source_sha256','configuration_sha256'):
            for changed in (None,dict(value=None,validity='invalid'),dict(value='d'*64,validity='valid')):
                with self.subTest(field=field,changed=changed):
                    snapshot,pinned=MotionContractTests().snapshot()
                    if changed is None:snapshot['cycle']['aliases'].pop(field)
                    else:snapshot['cycle']['aliases'][field]=changed
                    strict={**pinned,'pipeline_version':'automatic-reports-11',
                            'stream_motion_build_policy':'recorded_build_and_configuration_required_1'}
                    rows,reasons=mapper.samples(snapshot,strict,'simulation')
                    self.assertTrue(any(reason.startswith('strict_recorded_'+field) for reason in reasons),reasons)
                    self.assertTrue(all(row['validity']!='valid' for row in rows))
                    if field=='source_sha256' and changed in (None,dict(value=None,validity='invalid')):
                        old_rows,old_reasons=mapper.samples(snapshot,{**pinned,'pipeline_version':'automatic-reports-10'},'simulation')
                        self.assertEqual(old_reasons,[])
                        self.assertTrue(all(row['validity']=='valid' for row in old_rows))

    def test_dense_full_references_keep_combined_checkpoint_bounded_and_exact(self):
        from test_stream_tracking import MotionContractTests
        from robot_test_hub import stream_tracking as tracking,stream_tracking_store as work
        from robot_test_hub.analysis import canonical
        from robot_test_hub.stream_motion import MotionReducer
        from robot_test_hub.stream_motion_mapper import samples
        from robot_test_hub.storage import open_catalog
        snapshot,_=MotionContractTests().snapshot()
        refs=[row['source_reference'] for row in samples(snapshot,MotionContractTests().snapshot()[1],'simulation')[0]]
        for ref in refs:
            ref['command_record_timestamp_ns']=ref['measured_record_timestamp_ns']='706272827586400'
            ref['command_record_index']=ref['measured_record_index']=402741
            for origin in ref['module_measurement_references']:
                origin.update(record_timestamp_ns='706272827586400',cycle_timestamp_ns='706272827586400',record_index=402741)
        self.assertGreater(len(canonical(refs[0])),1000)
        pinned=context(20.,recorded_jump_limit_rad=.15)
        pinned.update(stream_source_type='simulation',runtime_mode='SIM',pipeline_version='automatic-reports-11')
        pinned['analysis_plan'].update(ideal_simulation_cycle_policy=True,swerve_configuration={})
        reducer=MotionReducer();pinned.update(reducer.context_versions)
        combined=reducer.initial(pinned);seen=set()
        def unique(module,identity):
            key=(module,json.dumps(identity,sort_keys=True));fresh=key not in seen;seen.add(key);return fresh
        with tempfile.TemporaryDirectory(prefix='independent dense steering ') as folder:
            db=open_catalog(Path(folder))
            try:
                for index in range(201):
                    for module in range(4):
                        current=sample(module,index/10.,actual=.3 if index%2 else -.3,fresh=True)
                        current['source_reference']=copy.deepcopy(refs[module])
                        current['source_reference']['cycle_record_index']=index
                        row=dict(stream_source_ordinal=0,stream_line_ordinal=index)
                        tracking.consume_sample(db,'dense',1,combined['tracking'],current,row,index)
                        steering.consume_sample(combined['steering'],current,
                            lambda m,e,w:work.contribute(db,'dense',1,combined['tracking']['work'],index,row,m,'recorded_steering',e,w),unique)
                    if index%17==0:combined=json.loads(canonical(combined))
                    self.assertLess(len(canonical(combined).encode()),1024*1024)
                steering.flush(combined['steering'])
                self.assertLess(len(canonical(combined).encode()),1024*1024)
                self.assertEqual(combined['tracking']['work']['count'],200*4*3)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_tracking_samples').fetchone()[0],200*4*3)
                for module in combined['steering']['modules']:
                    self.assertEqual(module['recorded_interval_count'],200)
                    self.assertEqual(module['event_storage']['recorded_jump_events']['total'],200)
                    self.assertEqual(module['criteria']['recorded_jump_events']['count'],200)
                    self.assertEqual(module['event_storage']['recorded_jump_events']['omitted'],72)
            finally:db.close()

    def test_one_invalid_cycle_uses_configured_coverage_and_retains_later_findings(self):
        timeline=[(i/10.,0.,.3 if i%2 else -.3,{'validity':'invalid'} if i==5 else {}) for i in range(31)]
        output=self.run_samples(timeline,context(3.,minimum_coverage=.8,recorded_jump_limit_rad=.15),roundtrip=True)
        self.assertEqual(len(output['findings']),4)
        self.assertEqual(sum(metric['value'] is not None for metric in output['metrics']),20)
        for module in output['coverage']['modules']:
            self.assertTrue(module['eligible'])
            self.assertAlmostEqual(module['fraction'],28/30)
            self.assertEqual(module['recorded_interval_count'],28)

    def test_actual_mapper_identity_conflicts_hard_fail_otherwise_complete_coverage(self):
        import test_stream_tracking as fixtures
        from robot_test_hub.stream_motion import MotionReducer
        from robot_test_hub.storage import open_catalog
        for problem in ('entry_generation_conflict','source_reference_invalid'):
            with self.subTest(problem=problem),tempfile.TemporaryDirectory(prefix='independent hard identity ') as folder:
                reducer=MotionReducer();pinned=context(3.,minimum_coverage=.8,recorded_jump_limit_rad=.15)
                pinned.update(pipeline_version='automatic-reports-11');pinned.update(reducer.context_versions)
                combined=reducer.initial(pinned);snapshot,_=fixtures.MotionContractTests().snapshot()
                combined['tracking']['mapper'].update(source='a'*64,held=copy.deepcopy(snapshot['held']),index=100)
                row=copy.deepcopy(snapshot['held']['/Drive/Module0/TurnPosition'])
                row.update(kind='observation',stream_scope='in_run',record_index=101)
                if problem=='entry_generation_conflict':row['entry_id']=snapshot['held']['/Drive/Module0/DriveConnected']['entry_id']
                else:row['source_sha256']='invalid'
                db=open_catalog(Path(folder))
                try:reducer.consume_chunk(db,'identity',1,combined,row,0)
                finally:db.close()
                state=combined['steering'];self.assertIn(problem,state['unavailable'])
                for index in range(31):
                    for module in range(4):steering.consume_sample(state,sample(module,index/10.,actual=.3 if index%2 else -.3),lambda *args:None,lambda *args:True)
                steering.flush(state)
                output=steering.finish_steering(state,dict(full_source_verified=True),{str(i)+':recorded_steering':.3 for i in range(4)})
                self.assertEqual(output['outcome'],'insufficient_data')
                self.assertEqual(output['findings'],[])
                self.assertTrue(all(metric['value'] is None for metric in output['metrics']))
                for module in output['coverage']['modules']:
                    self.assertAlmostEqual(module['fraction'],1.)
                    self.assertFalse(module['eligible'])

    def test_original_missing_invalid_or_different_build_and_config_admit_no_motion(self):
        from test_auto_swerve import protocol_recording,UTC
        from robot_test_hub import reports,stream_analysis
        from robot_test_hub.importer import Importer
        from robot_test_hub.review import ReviewStore
        from robot_test_hub.runs import rebuild_from_imports
        from robot_test_hub.storage import open_catalog
        from robot_test_hub.stream_motion import PIPELINE
        for field in ('source_sha256','configuration_sha256'):
            for mutation in ('missing','invalid','different'):
                with self.subTest(field=field,mutation=mutation),tempfile.TemporaryDirectory(prefix='independent original admission ') as folder:
                    root=Path(folder).resolve();db=open_catalog(root)
                    try:
                        build='a'*64 if field!='source_sha256' else 'not-a-hash' if mutation=='invalid' else 'c'*64 if mutation=='different' else 'a'*64
                        original=protocol_recording(build=build)
                        if field=='source_sha256' and mutation=='missing':
                            original=original.replace(b'/RealMetadata/SourceSHA256',b'/RealMetadata/UnusedSHA256')
                        if field=='configuration_sha256':
                            if mutation=='missing':original=original.replace(b'/RealOutputs/TestHub/ConfigurationSHA256',b'/RealOutputs/TestHub/ConfigurationOtherX')
                            else:original=original.replace(b'b'*64,(b'!' if mutation=='invalid' else b'c')*64)
                        source=root/'invented.wpilog';source.write_bytes(original)
                        imported=Importer(root,db).import_file(source,source_type='SYNTHETIC')
                        self.assertEqual(imported['state'],'succeeded',imported)
                        document=rebuild_from_imports(root,db);store=ReviewStore(db)
                        store.record_analysis_plan(dict(id='independent-plan',robot_id='author-robot',start_utc_ns=str(UTC-1000000),end_utc_ns=None,
                            reviewer='Independent invented test',rationale='Original protocol admission only',build_hash='a'*64,config_hash='b'*64,
                            test_id='invented',surface='invented',battery_id='invented',wheel_radius_m=.05,
                            swerve_configuration={},steering_configuration={'recorded_jump_limit_rad':.5,'threshold_revision':'invented'}))
                        for index,position in enumerate(MODULE_POSITIONS):store.assign_component(dict(id='assignment-'+str(index),robot_id='author-robot',
                            component_id='module-'+str(index),location=position,start_utc_ns=str(UTC-1000000),end_utc_ns=None,reviewer='Independent',rationale='Invented assignment'))
                        runner=reports.StreamReportRunner(root,db,limits={'min_free_bytes':0,'chunk_bytes':4096})
                        try:
                            sid=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version=PIPELINE)[0]['stream_id']
                            for _ in range(10000):
                                with patch.object(stream_analysis.time,'monotonic',return_value=0):progress=reports.advance_streams(root,db,runner,max_rows=7,max_bytes=4096)
                                self.assertNotIn(progress['state'],('failed','canceled'),progress)
                                if progress.get('report'):break
                            else:self.fail('original admission stream never completed')
                            self.assertEqual(progress['report']['source_type'],'synthetic')
                            for check in progress['report']['checks']:
                                if check['analyzer_id'] not in ('swerve-tracking','recorded-steering-observation'):continue
                                self.assertEqual(check['outcome'],'insufficient_data')
                                self.assertTrue(any(r.startswith('strict_recorded_'+field) for r in check['unavailable']),check['unavailable'])
                                self.assertTrue(all(metric['value'] is None for metric in check['metrics']))
                                for module in check['coverage']['modules']:
                                    self.assertEqual(module['eligible_seconds'],0.)
                                    self.assertEqual(module.get('recorded_interval_count',module.get('sample_count')),0)
                            self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_tracking_samples WHERE stream_id=?',(sid,)).fetchone()[0],0)
                            self.assertEqual(source.read_bytes(),original)
                        finally:runner.close()
                    finally:db.close()

    def test_actual_mapper_invalid_payload_recovers_with_partial_evidence(self):
        import test_stream_tracking as fixtures
        from robot_test_hub.stream_motion import MotionReducer
        from robot_test_hub import stream_tracking_store as work
        from robot_test_hub.storage import open_catalog
        for jump_limit in (.15,2.):
            with self.subTest(jump_limit=jump_limit),tempfile.TemporaryDirectory(prefix='independent payload recovery ') as folder:
                snapshot,pinned=fixtures.MotionContractTests().snapshot()
                pinned.update(context(3.,minimum_coverage=.8,recorded_jump_limit_rad=jump_limit))
                pinned.update(stream_source_type='simulation',runtime_mode='SIM',pipeline_version='automatic-reports-11')
                pinned['analysis_plan'].update(build_hash='c'*64,config_hash='b'*64,ideal_simulation_cycle_policy=True)
                reducer=MotionReducer();pinned.update(reducer.context_versions);state=reducer.initial(pinned)
                db=open_catalog(Path(folder));ordinal=0
                def consume(row):
                    nonlocal ordinal
                    ordinal+=1
                    row.update(stream_scope='in_run',stream_source_ordinal=0,stream_line_ordinal=ordinal)
                    row['record_index' if row['kind']=='observation' else 'timestamp_record_index']=ordinal
                    reducer.consume_chunk(db,'payload',1,state,row,ordinal)
                try:
                    for held in snapshot['held'].values():
                        row=copy.deepcopy(held);row.update(kind='observation',category='input',record_timestamp_ns=str(START),cycle_timestamp_ns=str(START))
                        if row['field'].endswith('DriveVelocityRadPerSec'):row['unit']='radians per second'
                        elif row['field'].endswith('TurnPosition'):row['unit']='radians'
                        consume(row)
                    for index in range(31):
                        stamp=START+index*100000000
                        for module in range(4):
                            row=copy.deepcopy(snapshot['held'][f'/Drive/Module{module}/TurnPosition'])
                            row.update(kind='observation',category='input',unit='radians',record_timestamp_ns=str(stamp),cycle_timestamp_ns=str(stamp),
                                       value=dict(value=.3 if index%2 else -.3))
                            if index==5 and module==0:row.update(validity='invalid',value=None)
                            consume(row)
                        row=copy.deepcopy(snapshot['cycle']);row.update(kind='cycle',timestamp_ns=str(stamp));consume(row)
                    steering.flush(state['steering'])
                    while True:
                        final=work.finalize_step(db,'payload',1,state['tracking']['work'],1000,1.,lambda:0.)
                        if final['complete']:break
                    check=steering.finish_steering(state['steering'],dict(full_source_verified=True),final['quantiles'])
                    self.assertIn('invalid_motion_field:/Drive/Module0/TurnPosition',check['unavailable'])
                    self.assertEqual(check['coverage']['evaluated_modules'],4)
                    self.assertTrue(any(metric['value'] is not None for metric in check['metrics']))
                    for module in check['coverage']['modules']:
                        self.assertGreater(module['fraction'],.8)
                        self.assertTrue(module['eligible'])
                    if jump_limit==.15:self.assertEqual(len(check['findings']),4)
                    else:
                        self.assertEqual(check['outcome'],'insufficient_data')
                        self.assertEqual(check['findings'],[])
                finally:db.close()


if __name__=='__main__':unittest.main()
