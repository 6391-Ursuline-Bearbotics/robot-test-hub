"""Independent adversarial steering observations; generated states are not hardware proof."""
import copy
import math
import json
import sqlite3
from contextlib import closing
import unittest
from dataclasses import replace
from robot_test_hub.analysis import Evidence, Runner, canonical
from robot_test_hub.report_api import detail
from robot_test_hub.swerve import adapt_robot_swerve, ROBOT_FIELDS, ROBOT_FORMAT_PROFILE
from robot_test_hub.steering import steering_configuration, steering_observation, steering_analyzer

HASH='a'*64
LO=1000000000

def mapped(states,stamps=None,held=False):
    stamps=stamps or [LO+i*100000000 for i in range(len(states))]
    rows=[dict(kind='observation',field='/.schema/struct:Rotation2d',type='structschema',value='double value'),dict(kind='observation',field='/.schema/struct:SwerveModuleVelocity',type='structschema',value='double velocity;Rotation2d angle')]
    record=0;entries={}
    for index,(stamp,state) in enumerate(zip(stamps,states)):
        target,actual=state[:2];speed=state[2] if len(state)>2 else 1.
        values=[(ROBOT_FIELDS['commands'],'struct:SwerveModuleVelocity[]',[dict(velocity=speed,angle={'value':target}) for _ in range(4)]),(ROBOT_FIELDS['measured'],'struct:SwerveModuleVelocity[]',[dict(velocity=speed,angle={'value':actual}) for _ in range(4)])]
        values.extend((f'/Drive/Module{module}/'+field,'boolean',True) for module in range(4) for field in ('DriveConnected','TurnConnected','TurnEncoderConnected'))
        if not held or index==0:
            for field,kind,value in values:
                record+=1
                rows.append(dict(kind='observation',field=field,type=kind,value=value,validity='valid',source_sha256=HASH,record_index=record,record_timestamp_ns=str(stamp),cycle_timestamp_ns=str(stamp),entry_id=entries.setdefault(field,len(entries)+1),entry_generation=1))
        record+=1
        rows.append(dict(kind='cycle',timestamp_ns=str(stamp),timestamp_record_index=record,source_sha256=HASH,aliases={'enabled':dict(value=True,validity='valid')}))
    context=dict(run_id='review-run',boot_id='review-boot',robot_id='review-robot',config_hash='b'*64,test_id='review-test',surface='invented-flat',battery_id='invented-battery',mapping_revision='explicit-map',start_monotonic_ns=str(stamps[0]),end_monotonic_ns=str(stamps[-1]),completeness='complete')
    raw=Evidence(ROBOT_FORMAT_PROFILE,(HASH,),tuple(rows),context,'explicit-map','independent-generator','real')
    return adapt_robot_swerve(raw,{i:'review-module-'+str(i) for i in range(4)})

def evaluate(evidence,**config):return steering_observation(evidence,dict(minimum_eligible_seconds=.01,minimum_coverage=0,**config))
def value(check,name):return next(m['value'] for m in check['metrics'] if m['name']==name)

class SteeringReviewTests(unittest.TestCase):
    def test_irregular_weights_real_unknown_freshness_and_no_criteria(self):
        check=evaluate(mapped([(0,1),(0,0),(0,2)],[LO,LO+100000000,LO+1000000000]),max_gap_ns=2000000000)
        self.assertAlmostEqual(value(check,'recorded_steering_rmse_rad'),math.sqrt(.1))
        self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])
        for key in ('physical_acquisition_time_qualified','physical_health_qualified','baseline_eligible'):self.assertFalse(check['coverage'][key])

    def test_wrapped_error_does_not_invent_full_turn(self):
        self.assertAlmostEqual(value(evaluate(mapped([(math.pi-.01,-math.pi+.01)]*12)),'recorded_steering_rmse_rad'),.02)

    def test_held_records_are_not_independent_physical_measurements(self):
        for module in evaluate(mapped([(0,.2)]*20,held=True))['coverage']['modules']:
            self.assertEqual(module['unique_measured_record_count'],1);self.assertIsNone(module['physical_sample_count'])
            self.assertEqual(module['recorded_interval_count'],19);self.assertEqual(module['held_measured_interval_count'],18)

    def test_duplicate_and_reversed_mapped_clocks_fail_closed(self):
        for delta in (0,-1):
            evidence=mapped([(0,0)]*12);rows=copy.deepcopy(list(evidence.rows))
            samples=[r for r in rows if r.get('kind')=='swerve_sample' and r['module_position']=='front-left']
            samples[5]['timestamp_ns']=samples[4]['timestamp_ns']+delta
            check=evaluate(replace(evidence,rows=tuple(rows)))
            self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])

    def test_forged_values_cannot_borrow_valid_raw_references(self):
        for key in ('command_angle_rad','measured_angle_rad','command_speed_mps','measured_speed_mps'):
            with self.subTest(key=key):
                evidence=mapped([(0,0)]*12);rows=copy.deepcopy(list(evidence.rows))
                for row in rows:
                    if row.get('kind')=='swerve_sample':row[key]=.8
                check=evaluate(replace(evidence,rows=tuple(rows)),sustained_error_limit_rad=.1,threshold_revision='review-1')
                self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])

    def test_invalid_reference_indices_and_ns_strings_fail_closed(self):
        for key,bad in (('command_record_index',True),('measured_record_index',-1),('cycle_record_index',1.5),('command_record_timestamp_ns','1000000000.0')):
            with self.subTest(key=key):
                evidence=mapped([(0,0)]*12);rows=copy.deepcopy(list(evidence.rows))
                for row in rows:
                    if row.get('kind')=='swerve_sample':row['source_reference'][key]=bad
                check=evaluate(replace(evidence,rows=tuple(rows)))
                self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])

    def test_pi_optimization_reversal_is_not_jump_or_step(self):
        check=evaluate(mapped([(0,0,1.)]*8+[(math.pi,math.pi,-1.)]*8),recorded_jump_limit_rad=.1,settling_limit_seconds=.2,threshold_revision='review-1')
        self.assertFalse(check['findings']);self.assertAlmostEqual(value(check,'recorded_angle_jump_max_rad'),0)
        self.assertTrue(all(not m['settling_events'] for m in check['coverage']['modules']))

    def test_target_change_censors_and_does_not_fabricate_zero_settling(self):
        check=evaluate(mapped([(0,0)]*3+[(.5,0)]*3+[(1.,0)]*8),minimum_stable_seconds=.5)
        event=check['coverage']['modules'][0]['settling_events'][0]
        self.assertEqual(event['state'],'censored');self.assertIsNone(event['settling_seconds']);self.assertFalse(check['findings'])

    def test_final_endpoint_step_is_censored_and_cannot_pass_settling_criterion(self):
        check=evaluate(mapped([(0,0)]*8+[(.5,0)]),settling_limit_seconds=.25,threshold_revision='review-1')
        event=check['coverage']['modules'][0]['settling_events'][-1]
        self.assertEqual(event['state'],'censored');self.assertEqual(event['observed_seconds'],0)
        self.assertIsNone(event['settling_seconds']);self.assertEqual(check['outcome'],'insufficient_data')
        self.assertFalse(check['findings'])

    def test_gap_does_not_invent_angle_jump(self):
        stamps=[LO+i*100000000 for i in range(8)]+[LO+5000000000+i*100000000 for i in range(8)]
        check=evaluate(mapped([(0,0)]*8+[(0,1)]*8,stamps),recorded_jump_limit_rad=.1,threshold_revision='review-1')
        self.assertFalse(check['findings']);self.assertAlmostEqual(value(check,'recorded_angle_jump_max_rad'),0)
        self.assertTrue(all(m['exclusions'].get('gap_exceeds_bound') for m in check['coverage']['modules']))

    def test_config_rejects_types_nonfinite_negative_and_unrevisioned_limits(self):
        cases=(('max_gap_ns',True),('max_gap_ns',1.5),('min_demand_mps',-1),('settle_dwell_seconds',float('inf')),('minimum_coverage',float('nan')),('settle_band_rad',10**1000),('oscillation_crossing_limit',True),('recorded_jump_limit_rad',.1))
        for key,bad in cases:
            with self.subTest(key=key),self.assertRaises(ValueError):steering_configuration({key:bad})

    def test_actual_dense_oscillation_report_remains_displayable_with_truthful_budgets(self):
        states=[]
        for block in range(8):
            target=0. if block%2==0 else .5
            states.extend((target,target+(.1 if index%2 else -.1)) for index in range(110))
        evidence=mapped(states);original=canonical(evidence.rows)
        self.assertEqual(len(evidence.rows),16722)
        with closing(sqlite3.connect(':memory:')) as db:
            report=Runner(db).report([steering_analyzer()],evidence,
                {'recorded-steering-observation':{'oscillation_crossing_limit':4,'threshold_revision':'resource-review'}})
            check=report['checks'][0]
            self.assertEqual(check['outcome'],'finding')
            self.assertAlmostEqual(value(check,'recorded_steering_rmse_rad'),.1)
            self.assertEqual(value(check,'recorded_oscillation_crossings'),871)
            findings=check['findings'];self.assertEqual(len(findings),4)
            self.assertTrue(all(f['event_count']==8 for f in findings))
            self.assertLessEqual(sum(len(f['source_references']) for f in findings),128)
            self.assertEqual(check['coverage']['finding_references_total'],3484)
            self.assertEqual(check['coverage']['finding_references_stored'],128)
            self.assertEqual(check['coverage']['finding_references_omitted'],3356)
            collections=('settling_events','oscillation_events','recorded_jump_events','sustained_error_intervals')
            for module in check['coverage']['modules']:
                self.assertEqual(module['event_totals']['oscillation_events'],8)
                self.assertEqual(module['event_storage']['oscillation_events'],dict(total=8,stored=8,omitted=0,references_total=871,references_stored=128,references_omitted=743))
                self.assertTrue(module['event_references_truncated'])
                stored=[event for name in collections for event in module[name]]
                self.assertLessEqual(len(stored),128)
                self.assertLessEqual(sum(len(event['source_references']) for event in stored),128)
                for event in stored:
                    self.assertGreaterEqual(event['source_references_total'],len(event['source_references']))
                    self.assertEqual(event['source_references_truncated'],event['source_references_total']>len(event['source_references']))
                self.assertEqual(module['evidence_trace_total'],879)
                self.assertEqual(len(module['evidence_trace']),128)
                self.assertTrue(module['evidence_trace_truncated'])
            response=detail(db,evidence.context['run_id'],report['report_id'])
            self.assertLessEqual(len(json.dumps(response,separators=(',',':')).encode()),4*1024*1024)
            visible=response['report']['checks'][0]
            self.assertEqual([f['event_count'] for f in visible['findings']],[8]*4)
            for original_finding,projected in zip(findings,visible['findings']):
                self.assertEqual(projected['source_references_total'],original_finding['source_references_total'])
                self.assertEqual(projected['source_references_total'],len(projected['source_references'])+projected['source_references_omitted'])
                self.assertEqual(projected['source_references_available'],len(original_finding['source_references']))
                self.assertTrue(projected['source_references_truncated'])
            self.assertTrue(all(m['evidence_trace_total']==879 and m['evidence_trace_truncated'] for m in visible['coverage']['modules']))
            self.assertEqual(json.loads(db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(report['report_id'],)).fetchone()[0]),report)
        self.assertEqual(canonical(evidence.rows),original)

    def test_many_warning_intervals_keep_observed_totals_after_projection(self):
        evidence=mapped([(0,.1 if index%2 else -.1) for index in range(300)])
        with closing(sqlite3.connect(':memory:')) as db:
            report=Runner(db).report([steering_analyzer()],evidence,
                {'recorded-steering-observation':{'recorded_jump_limit_rad':.1,'threshold_revision':'resource-review'}})
            response=detail(db,evidence.context['run_id'],report['report_id'])
            findings=response['report']['checks'][0]['findings']
            self.assertEqual(len(findings),4)
            for finding in findings:
                self.assertEqual(finding['event_count'],299)
                self.assertEqual(finding['intervals_ns_total'],299)
                self.assertEqual(finding['intervals_ns_available'],128)
                self.assertEqual(len(finding['intervals_ns']),128)
                self.assertEqual(finding['intervals_ns_omitted'],171)
                self.assertTrue(finding['intervals_ns_truncated'])

    def test_bad_settling_after_event_storage_cap_still_finds(self):
        states=[(0,0)]*7;target=0.
        for _ in range(260):
            target=.5 if target==0 else 0.;states.extend([(target,target)]*7)
        target=.5 if target==0 else 0.;states.extend([(target,0. if target else .5)]*10)
        check=evaluate(mapped(states),settling_limit_seconds=.25,threshold_revision='review-1')
        self.assertEqual(check['outcome'],'finding')
        self.assertTrue(any(f['kind']=='recorded_steering_settling_limit' for f in check['findings']))
        self.assertTrue(all(len(m['settling_events'])<=128 for m in check['coverage']['modules']))
        self.assertTrue(all(m['event_totals']['settling_events']==261 for m in check['coverage']['modules']))
        self.assertTrue(all(m['events_truncated']['settling_events'] for m in check['coverage']['modules']))
        self.assertTrue(all(f['event_count']==1 for f in check['findings']))

if __name__=='__main__':unittest.main()
