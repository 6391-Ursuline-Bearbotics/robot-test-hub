"""Recorded logger electrical observations, with no physical-health qualification."""
import copy
import sqlite3
import unittest
from robot_test_hub.analysis import Evidence,Runner
from robot_test_hub.system_status import (BATTERY,FIELDS,FAULTS,SPECIFICATIONS,MAX_ROWS,REFERENCE_LIMIT,
    MAX_CYCLE_GAP_NS,system_status,system_status_analyzer)
from robot_test_hub.wpilog import PROFILE

A,B='a'*64,'b'*64
LO,HI=1000000000,1200000000
FLAG='/SystemStats/Faults/Brownout'
COUNT='/SystemStats/FaultCounts/Brownout'


def initial_values():
    return {field:12.0 if field==BATTERY else False if spec[0]=='boolean' else 0 for field,spec in SPECIFICATIONS.items()}


class Rows:
    def __init__(self):
        self.rows=[];self.index={};self.ids={field:i+1 for i,field in enumerate(sorted(FIELDS))}

    def snapshot(self,ns,updates=None,*,initial=False,before=False,source=A,overrides=None,generation=1):
        self.index.setdefault(source,0)
        cycle_index=self.index[source]
        if not before:self.index[source]+=1
        values=initial_values() if initial else {}
        values.update(updates or {})
        for field,value in values.items():
            kind,unit=SPECIFICATIONS[field]
            row=dict(kind='observation',source_sha256=source,field=field,type=kind,unit=unit,category='input',validity='valid',value=value,
                record_index=self.index[source],entry_id=self.ids[field],entry_generation=generation,record_timestamp_ns=str(ns),cycle_timestamp_ns=str(ns),before_run=before)
            row.update((overrides or {}).get(field,{}));self.rows.append(row);self.index[source]+=1
        if not before:self.rows.append(dict(kind='cycle',source_sha256=source,timestamp_ns=str(ns),timestamp_record_index=cycle_index,aliases={}))
        return self

    def control(self,field,kind='finish',*,source=A,generation=1,entry_id=None):
        self.index.setdefault(source,0)
        self.rows.append(dict(kind='control',source_sha256=source,field=field,control=kind,record_index=self.index[source],entry_id=entry_id or self.ids[field],entry_generation=generation,record_timestamp_ns=str(LO)))
        self.index[source]+=1;return self

    def evidence(self,**context):
        return Evidence(PROFILE,tuple(sorted({row['source_sha256'] for row in self.rows})),tuple(self.rows),
            {**dict(run_id='system-run',robot_id='synthetic-6391',boot_id='synthetic-boot',runtime_mode='SIM',start_monotonic_ns=str(LO),end_monotonic_ns=str(HI),completeness='complete'),**context},
            'alpha7-aliases-2','synthetic-test','synthetic')


def evaluate(rows,**context):return system_status(rows.evidence(**context),{})

def metric(check,name):return next(value for value in check['metrics'] if value['name']==name)


class SystemStatusTests(unittest.TestCase):
    def clear(self):return Rows().snapshot(LO,initial=True).snapshot(LO+100000000).snapshot(HI)

    def test_complete_held_logger_states_are_not_physical_health_or_freshness(self):
        check=evaluate(self.clear());self.assertEqual(check['outcome'],'evaluated_no_finding')
        coverage=check['coverage'];self.assertTrue(coverage['complete_recorded_profile']);self.assertEqual(coverage['expected_cycles'],3)
        self.assertFalse(coverage['physical_acquisition_time_qualified']);self.assertFalse(coverage['counter_semantics_qualified']);self.assertFalse(coverage['voltage_fault_limits_configured'])
        self.assertEqual(coverage['scope'],'recorded_system_status_only')
        battery=next(field for field in coverage['fields'] if field['field']==BATTERY)
        self.assertEqual((battery['covered_cycles'],battery['held_cycles'],battery['distinct_records']),(3,2,1))
        self.assertEqual(metric(check,'recorded_battery_voltage_time_weighted_mean')['value'],12.0)
        with sqlite3.connect(':memory:') as db:
            report=Runner(db).report([system_status_analyzer()],self.clear().evidence())
        self.assertEqual(report['overall_health'],'not_assessed')

    def test_voltage_mean_is_time_weighted_left_held_and_has_no_sag_threshold(self):
        rows=Rows().snapshot(LO,{BATTERY:12.0},initial=True).snapshot(LO+50000000,{BATTERY:10.0}).snapshot(HI,{BATTERY:1.0})
        check=evaluate(rows);self.assertEqual(check['outcome'],'evaluated_no_finding')
        self.assertEqual(metric(check,'recorded_battery_voltage_min')['value'],1.0)
        self.assertEqual(metric(check,'recorded_battery_voltage_max')['value'],12.0)
        self.assertAlmostEqual(metric(check,'recorded_battery_voltage_time_weighted_mean')['value'],10.5)
        self.assertAlmostEqual(metric(check,'recorded_battery_voltage_time_weighted_mean')['eligible_seconds'],.2)

    def test_true_flag_is_recorded_warning_with_partial_missing_coverage(self):
        rows=Rows().snapshot(LO,{FLAG:True}).snapshot(HI)
        check=evaluate(rows);self.assertEqual(check['outcome'],'finding')
        self.assertFalse(check['coverage']['complete_recorded_profile'])
        warning=next(finding for finding in check['findings'] if finding['kind']=='recorded_system_fault_true')
        self.assertEqual(warning['signal'],'Brownout');self.assertEqual(warning['observations'],1)
        self.assertFalse(warning['physical_health_qualified'])
        self.assertIn('latched',warning['observed_behavior'])

    def test_before_run_true_is_held_with_exact_source_references_not_new_fault(self):
        rows=Rows().snapshot(LO-100000000,{FLAG:True},initial=True,before=True).snapshot(LO).snapshot(HI)
        check=evaluate(rows);self.assertEqual(check['outcome'],'finding')
        warning=next(finding for finding in check['findings'] if finding['kind']=='recorded_system_fault_true')
        self.assertEqual(warning['observations'],1)
        reference=warning['source_references'][0]['observation']
        self.assertTrue(reference['before_run']);self.assertTrue(reference['held_logger_state']);self.assertEqual(reference['source_sha256'],A)
        self.assertEqual(reference['record_timestamp_ns'],str(LO-100000000));self.assertEqual(reference['current_cycle_timestamp_ns'],str(LO))

    def test_counts_report_exact_positive_change_and_decrease_not_reset_or_event_count(self):
        rows=Rows().snapshot(LO,{COUNT:9},initial=True).snapshot(LO+100000000,{COUNT:12}).snapshot(HI,{COUNT:4})
        check=evaluate(rows);self.assertEqual(check['outcome'],'finding')
        increase=next(finding for finding in check['findings'] if finding['kind']=='recorded_system_fault_counter_increased')
        decrease=next(finding for finding in check['findings'] if finding['kind']=='recorded_system_fault_counter_decreased')
        self.assertEqual(increase['source_references'][0]['recorded_change'],'3')
        self.assertEqual(decrease['source_references'][0]['recorded_change'],'-8')
        self.assertFalse(decrease['source_references'][0]['physical_reset_qualified'])
        self.assertFalse(increase['source_references'][0]['physical_event_count_qualified'])

    def test_before_run_counts_and_gap_never_supply_physical_run_delta(self):
        rows=Rows().snapshot(LO-100000000,{COUNT:9},initial=True,before=True).snapshot(LO).snapshot(LO+100000000,{COUNT:12}).snapshot(HI)
        check=evaluate(rows);self.assertFalse(any('counter_' in finding['kind'] for finding in check['findings']))
        rows=Rows().snapshot(LO,{COUNT:9},initial=True).snapshot(LO+MAX_CYCLE_GAP_NS+1,{COUNT:12})
        check=evaluate(rows,end_monotonic_ns=str(LO+MAX_CYCLE_GAP_NS+1))
        self.assertEqual(check['outcome'],'insufficient_data');self.assertIn('logger_cycle_gap',check['unavailable'])
        self.assertFalse(check['findings']);self.assertEqual(metric(check,'recorded_battery_voltage_time_weighted_mean')['value'],None)

    def test_false_missing_or_invalid_values_are_not_coverage_or_zero(self):
        check=evaluate(Rows().snapshot(LO,{FLAG:False}).snapshot(HI));self.assertEqual(check['outcome'],'insufficient_data')
        for field,bad in [(FLAG,1),(COUNT,True),(COUNT,-1),(COUNT,1<<63),(COUNT,'01'),(BATTERY,True)]:
            with self.subTest(field=field,value=bad):
                rows=Rows().snapshot(LO,initial=True,updates={field:bad}).snapshot(HI)
                self.assertEqual(evaluate(rows)['outcome'],'insufficient_data')
        rows=Rows().snapshot(LO,initial=True);evidence=rows.evidence();evidence.rows[0]['value']=float('inf')
        self.assertEqual(system_status(evidence,{})['outcome'],'insufficient_data')
        for change in ({'unit':None},{'unit':'amps'},{'type':'float'},{'category':'replay_output'},{'validity':'invalid'}):
            with self.subTest(change=change):
                rows=Rows().snapshot(LO,initial=True,overrides={BATTERY:change}).snapshot(HI)
                self.assertEqual(evaluate(rows)['outcome'],'insufficient_data')

    def test_finish_reuse_and_other_source_never_reuse_stale_states(self):
        rows=Rows().snapshot(LO,initial=True).control(FLAG).snapshot(HI)
        self.assertEqual(evaluate(rows)['outcome'],'insufficient_data')
        rows=Rows().snapshot(LO,{COUNT:9},initial=True).control(COUNT).control(COUNT,'start',generation=2).snapshot(HI,{COUNT:1},generation=2)
        check=evaluate(rows);self.assertFalse(any('counter_' in finding['kind'] for finding in check['findings']))
        rows=Rows().snapshot(LO,initial=True).snapshot(HI,source=B)
        self.assertEqual(evaluate(rows)['outcome'],'insufficient_data')
        self.assertIsNone(metric(evaluate(rows),'recorded_battery_voltage_time_weighted_mean')['value'])

    def test_strict_references_source_order_overlap_and_resource_hazards(self):
        for change in ({'record_index':True},{'entry_id':0},{'entry_generation':0},{'record_timestamp_ns':'1.0'}):
            with self.subTest(change=change):
                rows=Rows().snapshot(LO,initial=True,overrides={BATTERY:change}).snapshot(HI)
                self.assertEqual(evaluate(rows)['outcome'],'insufficient_data')
        rows=self.clear();rows.rows.insert(-1,copy.deepcopy(rows.rows[0]))
        self.assertEqual(evaluate(rows)['outcome'],'insufficient_data')
        rows=self.clear().snapshot(LO+50000000,initial=True,source=B)
        self.assertEqual(evaluate(rows)['outcome'],'insufficient_data')
        self.assertEqual(evaluate(self.clear(),source_nonadvancing_cycles=[{}])['outcome'],'insufficient_data')
        self.assertEqual(evaluate(self.clear(),row_limit_reached=True)['outcome'],'insufficient_data')
        rows=self.clear();rows.rows=rows.rows*(MAX_ROWS//len(rows.rows)+1)
        self.assertEqual(evaluate(rows)['outcome'],'insufficient_data')

    def test_late_flags_excluded_unrelated_lifecycle_preserves_voltage_interval(self):
        rows=self.clear().snapshot(HI+100000000,{FLAG:True})
        self.assertEqual(evaluate(rows)['outcome'],'evaluated_no_finding')
        rows=Rows().snapshot(LO,initial=True).control('/Other/Field','start',entry_id=999).snapshot(HI)
        self.assertAlmostEqual(metric(evaluate(rows),'recorded_battery_voltage_time_weighted_mean')['eligible_seconds'],.2)

    def test_fault_references_bounded_and_nonfinite_voltage_never_silently_zero(self):
        rows=Rows().snapshot(LO,{FLAG:True},initial=True)
        for index in range(1,REFERENCE_LIMIT+10):rows.snapshot(LO+index*1000000,{FLAG:True})
        end=LO+(REFERENCE_LIMIT+9)*1000000
        check=evaluate(rows,end_monotonic_ns=str(end));warning=next(finding for finding in check['findings'] if finding['kind']=='recorded_system_fault_true')
        self.assertEqual(len(warning['source_references']),REFERENCE_LIMIT);self.assertTrue(warning['supporting_references_limited'])
        rows=Rows().snapshot(LO,{BATTERY:1.7976931348623157e308},initial=True).snapshot(HI)
        self.assertEqual(metric(evaluate(rows),'recorded_battery_voltage_time_weighted_mean')['value'],1.7976931348623157e308)


if __name__=='__main__':unittest.main()
