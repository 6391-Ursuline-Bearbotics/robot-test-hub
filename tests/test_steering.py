"""Known synthetic logger-state outcomes; no physical measurement qualification."""
from dataclasses import replace
import copy
import math
import unittest
from robot_test_hub.analysis import Evidence
from robot_test_hub.steering import steering_configuration, steering_observation, steering_analyzer, MAX_ROWS
from robot_test_hub.swerve import adapt_robot_swerve, ROBOT_FIELDS, ROBOT_FORMAT_PROFILE

HASH='a'*64


def mapped(stamps=None, targets=None, measured=None, speeds=None, *, held=False, source_type='real'):
    stamps=stamps or [i*100000000 for i in range(13)]
    targets=targets or [0.]*len(stamps)
    measured=measured or [0.]*len(stamps)
    speeds=speeds or [1.]*len(stamps)
    rows=[]
    def observation(field,value,kind,stamp):
        rows.append(dict(kind='observation',field=field,value=value,type=kind,validity='valid',source_sha256=HASH,
            record_index=len(rows),record_timestamp_ns=str(stamp),cycle_timestamp_ns=str(stamp)))
    observation('/.schema/struct:SwerveModuleVelocity','double velocity;Rotation2d angle','structschema',0)
    observation('/.schema/struct:Rotation2d','double value','structschema',0)
    for i in range(4):
        for field in ('DriveConnected','TurnConnected','TurnEncoderConnected'):
            observation(f'/Drive/Module{i}/'+field,True,'boolean',0)
    for i,stamp in enumerate(stamps):
        if not held or i==0:
            observation(ROBOT_FIELDS['commands'],[dict(velocity=speeds[i],angle={'value':targets[i]}) for _ in range(4)],'struct:SwerveModuleVelocity[]',stamp)
            observation(ROBOT_FIELDS['measured'],[dict(velocity=speeds[i],angle={'value':measured[i]}) for _ in range(4)],'struct:SwerveModuleVelocity[]',stamp)
        rows.append(dict(kind='cycle',timestamp_ns=str(stamp),timestamp_record_index=len(rows),source_sha256=HASH,
            aliases={'enabled':dict(value=True,validity='valid')}))
    context=dict(run_id='run',boot_id='boot',robot_id='6391',config_hash='b'*64,test_id='test',surface='tile',battery_id='battery',
        mapping_revision='mapping',start_monotonic_ns=str(stamps[0]),end_monotonic_ns=str(stamps[-1]),completeness='complete')
    evidence=Evidence(ROBOT_FORMAT_PROFILE,(HASH,),tuple(rows),context,'mapping','importer',source_type)
    return adapt_robot_swerve(evidence,{i:'physical-'+str(i) for i in range(4)})


def module(output):return output['coverage']['modules'][0]


def value(output,name):
    return next(m['value'] for m in output['metrics'] if m['module_position']=='front-left' and m['name']==name)


class SteeringTests(unittest.TestCase):
    def test_configuration_is_explicit_typed_and_revisioned(self):
        config=steering_configuration({})
        self.assertEqual(config['max_gap_ns'],250000000)
        self.assertIsNone(config['settling_limit_seconds'])
        for settings in ({'bogus':1},{'max_gap_ns':True},{'minimum_coverage':1.1},{'settle_band_rad':float('inf')},
                         {'oscillation_crossing_limit':1.5},{'settling_limit_seconds':1},{'threshold_revision':' '},
                         {'command_step_rad':.01},{'min_demand_mps':10**1000}):
            with self.subTest(settings=settings),self.assertRaises(ValueError):steering_configuration(settings)
        self.assertEqual(steering_analyzer().declaration.analyzer_id,'recorded-steering-observation')
        self.assertEqual(steering_analyzer().declaration.version,'1')

    def test_real_held_metrics_are_descriptive_and_no_criteria_is_insufficient(self):
        evidence=mapped(held=True)
        output=steering_observation(evidence,{})
        self.assertEqual(output['outcome'],'insufficient_data')
        self.assertEqual(value(output,'recorded_steering_rmse_rad'),0.)
        self.assertGreater(module(output)['held_measured_interval_count'],0)
        self.assertEqual(module(output)['unique_measured_record_count'],1)
        self.assertIsNone(module(output)['physical_sample_count'])
        self.assertFalse(output['coverage']['physical_acquisition_time_qualified'])
        self.assertFalse(output['coverage']['baseline_eligible'])
        self.assertFalse(any(row['fresh'] for row in evidence.rows if row.get('kind')=='swerve_sample'))

    def test_irregular_windows_use_time_weighting_and_recorded_rate(self):
        output=steering_observation(mapped([0,100000000,200000000,400000000],measured=[0,1,0,0]),
            {'minimum_eligible_seconds':.1,'recorded_jump_limit_rad':.5,'threshold_revision':'synthetic'})
        self.assertAlmostEqual(value(output,'recorded_steering_rmse_rad'),.5)
        self.assertEqual(value(output,'recorded_steering_p95_absolute_error_rad'),1)
        self.assertEqual(value(output,'recorded_angle_rate_max_rad_per_second'),10)
        self.assertEqual(output['outcome'],'finding')
        self.assertEqual(module(output)['event_totals']['recorded_jump_events'],2)

    def test_step_settles_after_known_recorded_delay_and_dwell(self):
        output=steering_observation(mapped(targets=[0]+[1]*12,measured=[0]*3+[1]*10),
            {'settling_limit_seconds':.1,'threshold_revision':'synthetic'})
        event=module(output)['settling_events'][0]
        self.assertEqual(event['state'],'settled')
        self.assertAlmostEqual(event['settling_seconds'],.2)
        self.assertAlmostEqual(value(output,'recorded_settling_max_seconds'),.2)
        self.assertEqual(output['outcome'],'finding')
        self.assertTrue(all(not finding['physical_health_qualified'] for finding in output['findings']))

    def test_run_end_and_changing_target_steps_are_censored_never_zero(self):
        output=steering_observation(mapped(targets=[0]+[1]*12),{'settling_limit_seconds':.2,'threshold_revision':'synthetic'})
        self.assertEqual(module(output)['settling_events'][0]['state'],'censored')
        self.assertEqual(module(output)['settling_events'][0]['censor_reason'],'run_end')
        self.assertIsNone(value(output,'recorded_settling_max_seconds'))
        self.assertEqual(output['outcome'],'finding')
        late=steering_observation(mapped(targets=[0]*12+[1]),{})
        self.assertEqual(module(late)['settling_events'][0]['state'],'censored')
        self.assertIsNone(module(late)['settling_events'][0]['settling_seconds'])
        changing=steering_observation(mapped(targets=[i*.2 for i in range(13)]),{})
        self.assertTrue(all(e['state']=='censored' for e in module(changing)['settling_events']))

    def test_signed_hysteresis_crossings_require_stable_target(self):
        errors=[.1 if i%2 else -.1 for i in range(13)]
        config={'oscillation_crossing_limit':4,'threshold_revision':'synthetic'}
        output=steering_observation(mapped(measured=errors),config)
        self.assertEqual(value(output,'recorded_oscillation_crossings'),11)
        self.assertEqual(output['outcome'],'finding')
        varying=steering_observation(mapped(targets=[i*.2 for i in range(13)],measured=[i*.2+errors[i] for i in range(13)]),config)
        self.assertIsNone(value(varying,'recorded_oscillation_crossings'))
        self.assertNotEqual(varying['outcome'],'evaluated_no_finding')

    def test_wrap_and_optimized_pi_reversals_do_not_create_false_jump_or_step(self):
        nearpi=[math.pi-.01 if i%2 else -math.pi+.01 for i in range(13)]
        output=steering_observation(mapped(targets=nearpi,measured=nearpi),
            {'recorded_jump_limit_rad':.1,'threshold_revision':'synthetic'})
        self.assertEqual(output['outcome'],'evaluated_no_finding')
        targets=[0]*6+[math.pi]*7;speeds=[1]*6+[-1]*7
        reversed_output=steering_observation(mapped(targets=targets,measured=targets,speeds=speeds),
            {'recorded_jump_limit_rad':.1,'threshold_revision':'synthetic'})
        self.assertEqual(reversed_output['outcome'],'evaluated_no_finding')
        self.assertEqual(module(reversed_output)['settling_events'],[])
        self.assertAlmostEqual(value(reversed_output,'recorded_angle_jump_max_rad'),0)

    def test_missing_step_events_remain_explicitly_unavailable(self):
        output=steering_observation(mapped(),{'settling_limit_seconds':1,'threshold_revision':'synthetic'})
        self.assertEqual(output['outcome'],'insufficient_data')
        self.assertTrue(any(reason.startswith('command_step_events_unavailable') for reason in output['unavailable']))

    def test_invalid_endpoint_gap_and_nonadvancing_stamps_break_windows(self):
        evidence=mapped()
        rows=copy.deepcopy(list(evidence.rows))
        for row in rows:
            if row.get('kind')=='swerve_sample' and row['timestamp_ns']==600000000:row['connected']=False
        output=steering_observation(replace(evidence,rows=tuple(rows)),{'minimum_eligible_seconds':.5})
        self.assertAlmostEqual(module(output)['eligible_seconds'],1.)
        gap=steering_observation(mapped([0,100000000,800000000,900000000]),{'minimum_eligible_seconds':.1})
        self.assertAlmostEqual(module(gap)['eligible_seconds'],.2)
        self.assertLess(module(gap)['fraction'],.8)
        bad=copy.deepcopy(list(evidence.rows))
        sample_indices=[i for i,r in enumerate(bad) if r.get('kind')=='swerve_sample' and r['module_position']=='front-left']
        bad[sample_indices[2]]['timestamp_ns']=bad[sample_indices[1]]['timestamp_ns']
        out=steering_observation(replace(evidence,rows=tuple(bad)),{})
        self.assertEqual(out['outcome'],'insufficient_data')
        self.assertIn('nonadvancing_module_timestamps',out['unavailable'])

    def test_forged_values_references_component_or_context_never_pass(self):
        evidence=mapped()
        for key,change in (('command_angle_rad',1),('timestamp_ns',True),('module_id','other')):
            rows=copy.deepcopy(list(evidence.rows));row=next(r for r in rows if r.get('kind')=='swerve_sample');row[key]=change
            output=steering_observation(replace(evidence,rows=tuple(rows)),{'recorded_jump_limit_rad':1,'threshold_revision':'synthetic'})
            self.assertNotEqual(output['outcome'],'evaluated_no_finding')
        rows=copy.deepcopy(list(evidence.rows))
        for row in rows:
            if row.get('kind')=='swerve_sample':row['source_reference']['measured_source_hash']='f'*64
        output=steering_observation(replace(evidence,rows=tuple(rows)),{})
        self.assertEqual(output['coverage']['evaluated_modules'],0)
        context={**evidence.context};context.pop('component_ids')
        self.assertEqual(steering_observation(replace(evidence,context=context),{})['outcome'],'insufficient_data')

    def test_trace_bounds_do_not_change_metric_aggregate(self):
        stamps=[i*10000000 for i in range(301)]
        output=steering_observation(mapped(stamps,measured=[.2]*301),{})
        self.assertAlmostEqual(value(output,'recorded_steering_rmse_rad'),.2)
        self.assertTrue(module(output)['evidence_trace_truncated'])
        self.assertEqual(len(module(output)['evidence_trace']),128)
        self.assertEqual(module(output)['recorded_interval_count'],300)

    def test_raw_lifecycle_change_resets_windows_before_new_values(self):
        evidence=mapped(measured=[0]*6+[1]*7)
        raw=[copy.deepcopy(row) for row in evidence.rows if row.get('kind')!='swerve_sample']
        location=next(i for i,row in enumerate(raw) if row.get('kind')=='observation'
            and row.get('field')==ROBOT_FIELDS['commands'] and row['record_timestamp_ns']=='600000000')
        raw.insert(location,dict(kind='control',control='metadata',field=ROBOT_FIELDS['commands'],source_sha256=HASH))
        remapped=adapt_robot_swerve(replace(evidence,rows=tuple(raw)),{i:'physical-'+str(i) for i in range(4)})
        output=steering_observation(remapped,{'recorded_jump_limit_rad':.1,'threshold_revision':'synthetic'})
        self.assertFalse(output['findings'])
        self.assertAlmostEqual(value(output,'recorded_angle_jump_max_rad'),0)
        self.assertIn('source_lifecycle_or_missing_cycle_boundary',module(output)['exclusions'])

    def test_huge_finite_angles_do_not_overflow_and_raw_booleans_are_invalid(self):
        output=steering_observation(mapped(targets=[1e308]*13,measured=[-1e308]*13),{})
        self.assertTrue(math.isfinite(value(output,'recorded_steering_rmse_rad')))
        evidence=mapped();rows=copy.deepcopy(list(evidence.rows))
        for row in rows:
            if row.get('kind')=='observation' and row.get('field')==ROBOT_FIELDS['commands']:
                row['value'][0]['angle']['value']=False
        output=steering_observation(replace(evidence,rows=tuple(rows)),{})
        self.assertEqual(output['coverage']['modules'][0]['eligible_seconds'],0)

    def test_shared_event_and_reference_budgets_preserve_late_criteria(self):
        stamps=[i*10000000 for i in range(301)]
        output=steering_observation(mapped(stamps,measured=[.1 if i%2 else -.1 for i in range(301)]),
            {'recorded_jump_limit_rad':.1,'oscillation_crossing_limit':2,'threshold_revision':'synthetic'})
        collections=('settling_events','oscillation_events','recorded_jump_events','sustained_error_intervals')
        for report in output['coverage']['modules']:
            events=[event for name in collections for event in report[name]]
            self.assertEqual(len(events),128)
            self.assertEqual(sum(len(event['source_references']) for event in events),128)
            self.assertEqual(report['event_count_total'],301)
            self.assertEqual(report['event_count_omitted'],173)
            self.assertEqual(report['event_references_total'],899)
            self.assertEqual(report['event_references_omitted'],771)
            self.assertEqual(report['event_storage']['oscillation_events']['stored'],0)
            self.assertTrue(report['events_truncated']['oscillation_events'])
            for event in events:
                self.assertEqual(event['source_references_total']-len(event['source_references']),event['source_references_omitted'])
        self.assertEqual(value(output,'recorded_oscillation_crossings'),299)
        self.assertTrue(any(f['kind']=='recorded_steering_error_crossings' for f in output['findings']))
        self.assertEqual(sum(len(f['source_references']) for f in output['findings']),128)
        self.assertEqual(output['coverage']['finding_references_stored'],128)
        self.assertEqual(output['coverage']['finding_references_total'],899*4)
        for finding in output['findings']:
            self.assertEqual(finding['source_references_total']-len(finding['source_references']),finding['source_references_omitted'])
            self.assertEqual(finding['interval_count'],finding['event_count'])
            self.assertEqual(finding['intervals_ns_total'],finding['event_count'])
            self.assertEqual(finding['intervals_ns_truncated'],finding['event_count']>len(finding['intervals_ns']))

    def test_source_type_and_resource_limits_stay_explicit(self):
        real=steering_observation(mapped(),{})
        simulation=steering_observation(mapped(source_type='simulation'),{})
        self.assertEqual(real['coverage']['source_type'],'real')
        self.assertEqual(simulation['coverage']['source_type'],'simulation')
        self.assertFalse(simulation['coverage']['physical_acquisition_time_qualified'])
        evidence=mapped()
        evidence=replace(evidence,rows=evidence.rows+({'kind':'ignored'},)*MAX_ROWS)
        self.assertIn('report_resource_limit_reached',steering_observation(evidence,{})['unavailable'])


if __name__=='__main__':unittest.main()
