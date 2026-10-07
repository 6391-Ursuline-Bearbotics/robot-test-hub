"""Deterministic recorded SDK observations, never robot/hardware qualification."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.analysis import Evidence, Runner
from robot_test_hub.importer import Importer
from robot_test_hub.phoenix_status import (COMMON, SIGNAL_FIELDS, SIGNALS, OBSERVATION_PROFILE,
    SNAPSHOT_METHOD, FIELDS, phoenix_status, phoenix_status_analyzer)
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.reports import generate
from robot_test_hub.review import ReviewStore
from robot_test_hub.runs import RunCatalog
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE
from test_auto_swerve import protocol_recording, UTC
from test_wpilog import log_bytes, start, wire_record

A, B = 'a'*64, 'b'*64
LO, HI = 1000000000, 1200000000


def values(sequence, ns):
    common = {'DiagnosticsPresent': True, 'DiagnosticsProfile': OBSERVATION_PROFILE,
        'SnapshotMethod': SNAPSHOT_METHOD, 'ObservationSequence': sequence,
        'RobotObservationStartNs': ns, 'RobotObservationEndNs': ns+1000,
        'VendorObservationStartSeconds': 20.1, 'VendorObservationEndSeconds': 20.10001,
        'ObservationClockValid': True, 'ObservationClockRegressed': False,
        'PhysicalAcquisitionTimeQualified': False, 'NativeTimestampAvailabilityQualified': False,
        'DriveGroupRefreshStatusCode': 0, 'DriveGroupRefreshStatusOk': True,
        'TurnGroupRefreshStatusCode': 0, 'TurnGroupRefreshStatusOk': True}
    signal = {'RawValue': 2.5, 'StatusCode': 0, 'StatusOk': True,
        'BestTimestampSeconds': 20., 'BestTimestampSource': 1, 'BestTimestampValid': True,
        'SystemTimestampSeconds': 20., 'SystemTimestampValid': True,
        'CANivoreTimestampSeconds': 20., 'CANivoreTimestampValid': True,
        'DeviceTimestampSeconds': 20., 'DeviceTimestampValid': True,
        'AgeAtObservationStartSeconds': .1, 'AgeAtObservationEndSeconds': .10001,
        'ReceiptComparison': 'first' if sequence == 1 else 'advanced',
        'RawValueComparison': 'first' if sequence == 1 else 'held',
        'BestTimestampSourceChanged': False, 'TimestampInFuture': False}
    return {**common, **{name+suffix: (value if suffix != 'RawValue' or name == 'DriveVelocity' else .125)
        for name in SIGNALS for suffix, value in signal.items()}}


class Rows:
    def __init__(self): self.rows=[]; self.index={}; self.entries={}
    def snapshot(self, sequence, ns, *, overrides=None, held=(), source=A, before=False):
        self.index.setdefault(source, 0)
        cycle_index=self.index[source]; self.index[source]+=1
        for module in range(4):
            for name,value in values(sequence,ns).items():
                field=f'/Drive/Module{module}/Phoenix'+name
                if field in held: continue
                spec=COMMON.get(name)
                if spec is None:
                    signal=next(s for s in SIGNALS if name.startswith(s)); spec=SIGNAL_FIELDS[name[len(signal):]]
                identity=(source,field); self.entries.setdefault(identity,len(self.entries)+1)
                record=dict(kind='observation',source_sha256=source,field=field,type=spec[0],unit=None,
                    category='input',value=value,validity='valid',record_index=self.index[source],
                    entry_id=self.entries[identity],entry_generation=1,record_timestamp_ns=str(ns),
                    cycle_timestamp_ns=str(ns),before_run=before)
                record.update((overrides or {}).get(field,{})); self.rows.append(record); self.index[source]+=1
        if not before:self.rows.append(dict(kind='cycle',source_sha256=source,timestamp_ns=str(ns),timestamp_record_index=cycle_index,aliases={}))
        return self
    def control(self, field, *, kind='finish', source=A, entry_id=None):
        self.index.setdefault(source,0)
        self.rows.append(dict(kind='control',control=kind,source_sha256=source,field=field,
            entry_id=entry_id or self.entries.get((source,field)),record_index=self.index[source],record_timestamp_ns=str(HI)))
        self.index[source]+=1
        return self
    def evidence(self, **context):
        return Evidence(PROFILE,tuple(sorted({r['source_sha256'] for r in self.rows})),tuple(self.rows),
            dict(run_id='sdk-run',robot_id='recorded-robot',boot_id='recorded-boot',runtime_mode='REAL',
                start_monotonic_ns=str(LO),end_monotonic_ns=str(HI),completeness='complete',**context),
            'alpha7-aliases-2','test-importer','real')


def evaluate(rows, **context): return phoenix_status(rows.evidence(**context),{})


class PhoenixStatusTests(unittest.TestCase):
    def clear(self):return Rows().snapshot(1,LO).snapshot(2,LO+100000000).snapshot(3,HI)

    def test_clear_sdk_status_is_not_physical_freshness_or_native_availability(self):
        check=evaluate(self.clear())
        self.assertEqual(check['outcome'],'evaluated_no_finding'); self.assertFalse(check['findings'])
        self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])
        self.assertFalse(check['coverage']['native_timestamp_availability_qualified'])
        self.assertIn('sdk_age_fault_threshold_not_configured',check['unavailable'])
        module=check['coverage']['module_observations'][0]
        self.assertIsNone(module['physical_component_id']);self.assertEqual(module['distinct_sdk_observations'],3)
        self.assertEqual(module['signal_observations']['DriveVelocity']['receipt_comparisons'],{'first':1,'advanced':2})
        self.assertEqual(module['signal_observations']['DriveVelocity']['raw_value_comparisons'],{'first':1,'held':2})
        latest=module['latest_observation'];self.assertEqual(latest['robot_start_ns'],str(HI));self.assertEqual(latest['vendor_start_seconds'],20.1)
        self.assertEqual(latest['signals']['DriveVelocity']['raw_value_unit'],'rotations per second')
        self.assertEqual(latest['signals']['TurnPosition']['raw_value_unit'],'rotations')
        self.assertEqual(latest['signals']['DriveVelocity']['recorded_values']['BestTimestampSource'],1)
        self.assertTrue(latest['signals']['DriveVelocity']['recorded_values']['CANivoreTimestampValid'])
        self.assertTrue(all(m['unit']=='seconds' for m in check['metrics']))
        self.assertNotIn('modules',check['coverage'])

    def test_exact_individual_false_and_aggregate_false_remain_separate_and_held_deduplicated(self):
        signal='/Drive/Module0/PhoenixDriveVelocityStatusOk';group='/Drive/Module0/PhoenixDriveGroupRefreshStatusOk'
        code='/Drive/Module0/PhoenixDriveVelocityStatusCode'
        rows=Rows().snapshot(1,LO).snapshot(2,LO+100000000,overrides={signal:{'value':False},group:{'value':False},code:{'value':-7}})
        rows.snapshot(3,HI,held=(signal,group,code))
        check=evaluate(rows); self.assertEqual(check['outcome'],'finding');self.assertEqual(len(check['findings']),2)
        finding=next(f for f in check['findings'] if f['signal']=='DriveVelocity')
        self.assertEqual(finding['observed_false_status_records'],1)
        self.assertFalse(finding['physical_health_qualified']);self.assertEqual(finding['criterion'],'exact_recorded_status_ok_false')
        ref=finding['source_references'][0];self.assertEqual(ref['recorded_status_code'],-7)
        self.assertEqual(ref['status_ok']['source_sha256'],A);self.assertEqual(ref['status_ok']['field'],signal)
        self.assertIsInstance(ref['status_ok']['record_index'],int)
        self.assertEqual(ref['status_ok']['current_cycle_timestamp_ns'],str(LO+100000000))
        self.assertFalse(any(f['signal']=='TurnPosition' for f in check['findings']))

    def test_pre_run_held_false_retains_original_reference_and_is_not_new_receipt(self):
        field='/Drive/Module0/PhoenixDriveVelocityStatusOk'
        rows=Rows().snapshot(1,LO-100,overrides={field:{'value':False}},before=True)
        for sequence,ns in enumerate((LO,LO+100000000,HI),2):rows.snapshot(sequence,ns,held=(field,))
        finding=evaluate(rows)['findings'][0];self.assertEqual(finding['observed_false_status_records'],1)
        ref=finding['source_references'][0]['status_ok'];self.assertTrue(ref['before_run']);self.assertFalse(ref['updated_in_cycle'])
        self.assertEqual(ref['record_cycle_timestamp_ns'],str(LO-100));self.assertEqual(ref['current_cycle_timestamp_ns'],str(LO))

    def test_finished_or_reused_entry_cannot_keep_false_held_status(self):
        field='/Drive/Module0/PhoenixDriveVelocityStatusOk'
        for control in ('finish','start','metadata'):
            with self.subTest(control=control):
                rows=Rows().snapshot(1,LO-100,overrides={field:{'value':False}},before=True)
                rows.control('/replacement' if control=='start' else field,kind=control,entry_id=rows.entries[(A,field)])
                for seq,ns in enumerate((LO,LO+100000000,HI),2):rows.snapshot(seq,ns,held=(field,))
                check=evaluate(rows);self.assertFalse(check['findings']);self.assertEqual(check['outcome'],'insufficient_data')

    def test_source_state_and_entry_ids_do_not_bleed_into_another_artifact(self):
        field='/Drive/Module0/PhoenixDriveVelocityStatusOk'
        rows=Rows().snapshot(1,LO-100,overrides={field:{'value':False}},before=True)
        for seq,ns in enumerate((LO,LO+100000000,HI),1):rows.snapshot(seq,ns,source=B,held=(field,))
        check=evaluate(rows);self.assertFalse(check['findings']);self.assertEqual(check['outcome'],'insufficient_data')
        self.assertIn('phoenix_status_ok_unavailable:front-left',check['unavailable'])

    def test_missing_old_profile_and_sim_defaults_cannot_create_false_sdk_warning(self):
        rows=Rows();rows.index[A]=0
        for ns in (LO,HI):rows.rows.append(dict(kind='cycle',source_sha256=A,timestamp_ns=str(ns),timestamp_record_index=0))
        check=evaluate(rows);self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])
        defaults={field:{'value':False if field.endswith('DiagnosticsPresent') else 'unavailable'} for field in FIELDS if field.endswith(('DiagnosticsPresent','DiagnosticsProfile'))}
        check=evaluate(Rows().snapshot(1,LO,overrides=defaults).snapshot(2,HI,overrides=defaults))
        self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])

    def test_wrong_type_unit_nonfinite_and_comparison_metadata_remain_unavailable(self):
        path='/Drive/Module0/PhoenixDriveVelocityAgeAtObservationStartSeconds'
        for damage in ({'type':'float'},{'unit':'milliseconds'},{'value':None,'validity':'nonfinite'}, {'value':True}):
            with self.subTest(damage=damage):
                check=evaluate(Rows().snapshot(1,LO,overrides={path:damage}).snapshot(2,HI,overrides={path:damage}))
                self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])
                self.assertIn('DriveVelocityAgeAtObservationStartSeconds',check['coverage']['module_observations'][0]['unavailable_fields'])
        for suffix,value in (('BestTimestampSource',3),('ReceiptComparison','fresh'),('StatusOk',0)):
            field='/Drive/Module0/PhoenixDriveVelocity'+suffix
            check=evaluate(Rows().snapshot(1,LO,overrides={field:{'value':value}}).snapshot(2,HI,overrides={field:{'value':value}}))
            self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])

    def test_held_receipt_equal_raw_and_negative_sdk_age_are_observations_not_fault_limits(self):
        overrides={f'/Drive/Module0/PhoenixDriveVelocity{suffix}':{'value':value} for suffix,value in
            (('ReceiptComparison','held'),('RawValueComparison','held'),('AgeAtObservationStartSeconds',-.001),('TimestampInFuture',True))}
        check=evaluate(Rows().snapshot(1,LO,overrides=overrides).snapshot(2,HI,overrides=overrides))
        self.assertEqual(check['outcome'],'evaluated_no_finding');self.assertFalse(check['findings'])
        signal=check['coverage']['module_observations'][0]['signal_observations']['DriveVelocity']
        self.assertEqual(signal['receipt_comparisons'],{'held':2});self.assertEqual(signal['sdk_timestamp_in_future_count'],2)
        self.assertEqual(signal['age_at_observation_start_seconds_min'],-.001)

    def test_complete_run_endpoints_cycle_refs_order_and_sequence_are_required(self):
        for damage in ('endpoint','cycle_reference','source_order','sequence','incomplete'):
            with self.subTest(damage=damage):
                rows=self.clear();kwargs={}
                if damage=='endpoint':rows.rows=[r for r in rows.rows if not(r['kind']=='cycle' and r['timestamp_ns']==str(HI))]
                elif damage=='cycle_reference':next(r for r in rows.rows if r['kind']=='cycle')['timestamp_record_index']=None
                elif damage=='source_order':rows.rows[1]['record_index']=rows.rows[0]['record_index']
                elif damage=='sequence':
                    for r in rows.rows:
                        if r.get('field')=='/Drive/Module0/PhoenixObservationSequence' and r['cycle_timestamp_ns']==str(HI):r['value']=1
                else:kwargs={'completeness':'partial'}
                check=phoenix_status(Evidence(PROFILE,(A,),tuple(rows.rows),{**rows.evidence().context,**kwargs},'mapping','importer','real'),{})
                self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['findings'])

    def test_bad_sdk_clock_or_qualification_flag_never_promotes_physical_time(self):
        for suffix,value in (('ObservationClockValid',False),('ObservationClockRegressed',True),('PhysicalAcquisitionTimeQualified',True),('NativeTimestampAvailabilityQualified',True),('RobotObservationEndNs',-1)):
            path='/Drive/Module0/Phoenix'+suffix
            check=evaluate(Rows().snapshot(1,LO,overrides={path:{'value':value}}).snapshot(2,HI,overrides={path:{'value':value}}))
            self.assertEqual(check['outcome'],'insufficient_data');self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])

    def test_unsupported_profile_limits_and_immutable_runner_reuse(self):
        with sqlite3.connect(':memory:') as db:
            runner=Runner(db);evidence=self.clear().evidence();first=runner.run(phoenix_status_analyzer(),evidence)
            self.assertEqual(runner.run(phoenix_status_analyzer(),evidence),first)
            unsupported=Evidence('unknown',(A,),evidence.rows,evidence.context,'mapping','importer','real')
            self.assertEqual(runner.run(phoenix_status_analyzer(),unsupported)['outcome'],'unsupported')
            limited=Evidence(PROFILE,(A,),evidence.rows,{**evidence.context,'row_limit_reached':True},'mapping','importer','real')
            self.assertEqual(runner.run(phoenix_status_analyzer(),limited)['outcome'],'insufficient_data')
            revised=Evidence(PROFILE,(A,),evidence.rows,{**evidence.context,'review_history_revision':'explicit-new-revision'},'mapping','importer','real')
            second=runner.run(phoenix_status_analyzer(),revised);self.assertNotEqual(first['job_id'],second['job_id'])
            self.assertEqual(json.loads(db.execute('SELECT result_json FROM analyzer_jobs WHERE job_id=?',(first['job_id'],)).fetchone()[0]),first)


class PhoenixPipelineTests(unittest.TestCase):
    def test_real_import_preserves_pre_run_sdk_selection_and_old_report_revisions(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);db=open_catalog(root);self.addCleanup(db.close)
        raw=protocol_recording();length=int.from_bytes(raw[8:12],'little');header=raw[:12+length];events=[]
        entry=10000
        for module in range(4):
            for name,value in values(1,1100000000).items():
                spec=COMMON.get(name)
                if spec is None:signal=next(s for s in SIGNALS if name.startswith(s));spec=SIGNAL_FIELDS[name[len(signal):]]
                field=f'/Drive/Module{module}/Phoenix'+name
                if field.endswith('DriveVelocityStatusOk') and module==0:value=False
                payload=value.encode() if spec[0]=='string' else bytes([value]) if spec[0]=='boolean' else struct.pack('<q' if spec[0]=='int64' else '<d',value)
                events.extend([start(entry,field,spec[0]),wire_record(entry,payload)]);entry+=1
        path=root/'generated.wpilog';original=header+b''.join(events)+raw[len(header):];path.write_bytes(original)
        imported=Importer(root,db).import_file(path,source_type='SYNTHETIC');self.assertEqual(imported['state'],'succeeded',imported)
        pipeline=Pipeline(root,db);pipeline.tick()
        before=db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id').fetchall()
        for row in before:
            report=json.loads(row['result_json']);check=next(c for c in report['checks'] if c['analyzer_id']=='phoenix-status-observation')
            self.assertEqual(check['outcome'],'finding');self.assertEqual(check['provenance']['context']['pipeline_version'],'automatic-reports-5')
            self.assertEqual(check['findings'][0]['source_references'][0]['status_ok']['source_sha256'],hashlib.sha256(original).hexdigest())
            self.assertTrue(check['findings'][0]['source_references'][0]['status_ok']['before_run'])
        pipeline.tick();self.assertEqual([tuple(r) for r in before],[tuple(r) for r in db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id')])
        ReviewStore(db).record_maintenance(dict(id='new-review',kind='inspection',robot_id='author-robot',effective_utc_ns=str(UTC),reviewer='Synthetic',rationale='New immutable review context'))
        pipeline.tick();self.assertGreater(db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],len(before))
        for row in before:self.assertEqual(db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(row['report_id'],)).fetchone()[0],row['result_json'])
        self.assertEqual(path.read_bytes(),original)
        other=root/'new-import.wpilog';other.write_bytes(protocol_recording(build='c'*64).replace(b'author-boot',b'second-boot'));job=Importer(root,db).import_file(other,source_type='SYNTHETIC')
        self.assertEqual(job['state'],'succeeded');pipeline.tick();checks=[c for row in db.execute('SELECT result_json FROM analysis_reports') for c in json.loads(row[0])['checks'] if c['analyzer_id']=='phoenix-status-observation']
        self.assertTrue(any(c['outcome']=='insufficient_data' for c in checks))


if __name__=='__main__':unittest.main()