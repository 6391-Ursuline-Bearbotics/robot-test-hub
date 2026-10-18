"""Independent S2 status regressions from invented, imported WPILOG bytes."""
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
from robot_test_hub.phoenix_status import COMMON,SIGNALS,SIGNAL_FIELDS
from robot_test_hub.system_status import SPECIFICATIONS,BATTERY
from test_phoenix_status import values as sdk_values
from test_wpilog import log_bytes,start,wire_record,text

START=1000000000
UTC=1800000000000000000
FLAG='/SystemStats/Faults/Brownout'
COUNT='/SystemStats/FaultCounts/Brownout'
SDK_FLAG='/Drive/Module0/PhoenixDriveVelocityStatusOk'
SDK_CODE='/Drive/Module0/PhoenixDriveVelocityStatusCode'


def encode(kind,value):
    if kind=='boolean':return bytes([value])
    if kind=='int64':return struct.pack('<q',value)
    if kind=='double':return struct.pack('<d',value)
    if kind=='string':return value.encode()
    raise AssertionError(kind)


def status_recording(path,*,cycles=8,prefix_records=0,full_updates=True,updates=None,times=None,fraction_ns=0,lifecycle=None):
    """Invented fixed identities and reviewed field units; no hardware evidence."""
    fields={};specs={};initial=[]
    def declare(field,kind,value,unit=None):
        entry=len(fields)+2;fields[field]=entry;specs[field]=(kind,unit)
        metadata=json.dumps({'unit':unit} if unit is not None else {'source':'AdvantageKit'})
        initial.extend((start(entry,field,kind,metadata),wire_record(entry,encode(kind,value))))
    for field,value in (('/Metadata/RobotId','status-review-robot'),('/Metadata/BootId','status-review-boot'),
        ('/RealOutputs/TestHub/RobotId','status-review-robot'),('/RealOutputs/TestHub/BootId','status-review-boot'),
        ('/RealOutputs/TestHub/RuntimeMode','SIM'),('/RealOutputs/TestHub/Mode','TELEOPERATED'),
        ('/RealOutputs/TestHub/ConfigurationSHA256','e'*64)):
        declare(field,'string',value)
    for field,value in (('/RealOutputs/TestHub/StateKnown',True),('/DriverStation/Enabled',False),
        ('/RealOutputs/TestHub/Enabled',False),('/SystemStats/EpochTimeValid',True)):
        declare(field,'boolean',value)
    declare('/SystemStats/EpochTime','double',UTC//1000,'microseconds')
    declare('/Review/Prefix','string','prefix')
    for module in range(4):
        for name,value in sdk_values(1,START).items():
            specification=COMMON.get(name)
            if specification is None:
                signal=next(signal for signal in SIGNALS if name.startswith(signal))
                suffix=name[len(signal):]
                specification=SIGNAL_FIELDS[suffix]
                if suffix=='RawValue':specification=(specification[0],SIGNALS[signal])
            declare(f'/Drive/Module{module}/Phoenix'+name,specification[0],value,specification[1])
    defaults={field:12.0 if field==BATTERY else False if kind=='boolean' else 0 for field,(kind,unit) in SPECIFICATIONS.items()}
    for field,value in defaults.items():declare(field,*SPECIFICATIONS[field][:1],value,SPECIFICATIONS[field][1])
    with path.open('wb') as output:
        output.write(log_bytes(*initial))
        prefix=wire_record(fields['/Review/Prefix'],b'prefix')
        for offset in range(0,prefix_records,4096):output.write(prefix*min(4096,prefix_records-offset))
        stamps=times or [START+index*20000000+fraction_ns for index in range(1,cycles+2)]
        for index,stamp in enumerate(stamps,1):
            output.write(wire_record(1,struct.pack('<q',stamp),stamp//1000))
            for field in ('/DriverStation/Enabled','/RealOutputs/TestHub/Enabled'):
                output.write(wire_record(fields[field],bytes([index<=cycles]),stamp//1000))
            output.write(wire_record(fields['/SystemStats/EpochTime'],struct.pack('<d',(UTC+stamp-START)//1000),stamp//1000))
            if lifecycle is not None:lifecycle(index,output,fields,specs,stamp)
            current={}
            if full_updates or index==1:
                current.update(defaults)
                for module in range(4):current.update({f'/Drive/Module{module}/Phoenix'+name:value for name,value in sdk_values(index,stamp).items()})
            current.update((updates or {}).get(index,{}))
            for field,value in current.items():
                if value is None:continue
                output.write(wire_record(fields[field],encode(specs[field][0],value),stamp//1000))
    return path


class StreamStatusReviewTests(unittest.TestCase):
    def fixture(self,**settings):
        allow_multiple=settings.pop('allow_multiple_runs',False)
        temporary=tempfile.TemporaryDirectory(prefix='independent S2 ');self.addCleanup(temporary.cleanup)
        root=Path(temporary.name).resolve();db=open_catalog(root);self.addCleanup(db.close)
        source=status_recording(root/'invented-status.wpilog',**settings)
        imported=Importer(root,db).import_file(source,source_type='SYNTHETIC')
        self.assertEqual(imported['state'],'succeeded',imported)
        document=rebuild_from_imports(root,db)
        if not allow_multiple:self.assertEqual(len(document['runs']),1)
        return root,db,imported,document

    def runner(self,root,db,**limits):
        from robot_test_hub.reports import StreamReportRunner
        runner=StreamReportRunner(root,db,limits={'min_free_bytes':0,**limits});self.addCleanup(runner.close)
        return runner

    def evaluate(self,root,db,document,runner=None,**budgets):
        from robot_test_hub import reports
        runner=runner or self.runner(root,db)
        scheduled=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version='automatic-reports-9')[0]
        for _ in range(60000):
            progress=reports.advance_streams(root,db,runner,**budgets)
            self.assertNotIn(progress['state'],('failed','canceled'),progress)
            if progress.get('report') is not None:return progress
        self.fail('No completed S2 receipt in bounded test turns')

    def check(self,complete,analyzer_id):
        return next(check for check in complete['report']['checks'] if check['analyzer_id']==analyzer_id)

    def metric(self,check,name):return next(metric for metric in check['metrics'] if metric['name']==name)

    def test_partial_held_sdk_and_exact_ns_counter_baseline_with_unequal_voltage_weights(self):
        stamps=[START+20000123,START+70000123,START+220000123]
        changes={1:{COUNT:9,SDK_FLAG:False,SDK_CODE:-7},2:{COUNT:12,BATTERY:10.0},3:{COUNT:4,BATTERY:1.0}}
        root,db,_,document=self.fixture(cycles=2,times=stamps,full_updates=False,updates=changes)
        complete=self.evaluate(root,db,document,max_rows=7,max_bytes=997)
        phoenix=self.check(complete,'phoenix-status-observation');system=self.check(complete,'system-status-observation')
        self.assertEqual(phoenix['coverage']['expected_cycles'],3)
        for module in phoenix['coverage']['module_observations']:
            self.assertEqual(module['status_cycles'],3)
            self.assertEqual(module['timing_cycles'],3)
            self.assertEqual(module['distinct_sdk_observations'],1)
            self.assertEqual(module['held_snapshot_cycles'],2)
        warning=next(finding for finding in phoenix['findings'] if finding['signal']=='DriveVelocity')
        self.assertEqual(warning['observed_false_status_records'],1)
        self.assertEqual(warning['source_references'][0]['recorded_status_code'],-7)
        self.assertFalse(warning['physical_health_qualified'])
        self.assertEqual(system['coverage']['expected_cycles'],3)
        battery=next(field for field in system['coverage']['fields'] if field['field']==BATTERY)
        self.assertEqual((battery['covered_cycles'],battery['distinct_records']),(3,3))
        self.assertAlmostEqual(self.metric(system,'recorded_battery_voltage_time_weighted_mean')['value'],10.5)
        self.assertAlmostEqual(system['coverage']['eligible_seconds'],.2)
        increase=next(f for f in system['findings'] if f['kind']=='recorded_system_fault_counter_increased')
        decrease=next(f for f in system['findings'] if f['kind']=='recorded_system_fault_counter_decreased')
        self.assertEqual(increase['observations'],1);self.assertEqual(decrease['observations'],1)
        self.assertEqual(increase['source_references'][0]['recorded_change'],'3')
        self.assertEqual(decrease['source_references'][0]['recorded_change'],'-8')
        self.assertLess(int(increase['source_references'][0]['previous']['record_timestamp_ns']),int(document['runs'][0]['start_monotonic_ns']))
        for check in (phoenix,system):self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])
        self.assertEqual(complete['report']['overall_health'],'not_assessed')

    def test_partial_sdk_update_does_not_reorder_held_original_records(self):
        root,db,_,document=self.fixture(cycles=3,full_updates=False,updates={2:{SDK_FLAG:False,SDK_CODE:-7}})
        complete=self.evaluate(root,db,document,max_rows=11,max_bytes=4096)
        phoenix=self.check(complete,'phoenix-status-observation')
        self.assertEqual(phoenix['coverage']['expected_cycles'],4)
        self.assertTrue(all(module['timing_cycles']==4 for module in phoenix['coverage']['module_observations']))
        self.assertNotIn('source_record_order_unqualified',phoenix['unavailable'])
        self.assertEqual(len(phoenix['findings']),1)
        self.assertEqual(phoenix['findings'][0]['observed_false_status_records'],1)

    def test_finite_extreme_voltage_mean_does_not_overflow_or_use_final_endpoint_weight(self):
        stamps=[START+20000000,START+70000000,START+220000000]
        root,db,_,document=self.fixture(cycles=2,times=stamps,full_updates=False,
            updates={1:{BATTERY:1e308},2:{BATTERY:-1e308},3:{BATTERY:1e307}})
        complete=self.evaluate(root,db,document)
        system=self.check(complete,'system-status-observation')
        self.assertAlmostEqual(self.metric(system,'recorded_battery_voltage_time_weighted_mean')['value']/1e308,-.5)
        self.assertEqual(self.metric(system,'recorded_battery_voltage_min')['value'],-1e308)
        self.assertEqual(self.metric(system,'recorded_battery_voltage_max')['value'],1e308)
        self.assertEqual(system['coverage']['eligible_seconds'],.2)
        json.dumps(complete,allow_nan=False)

    def test_late_sdk_and_system_faults_beyond_both_old_boundaries_remain_observed(self):
        from robot_test_hub import reports
        clock='/Drive/Module0/PhoenixObservationClockValid'
        root,db,_,document=self.fixture(cycles=300,prefix_records=250001,
            updates={300:{FLAG:True,COUNT:3,SDK_FLAG:False,SDK_CODE:-7,clock:False},301:{COUNT:3}})
        with patch.object(Importer,'read_manifest',side_effect=AssertionError('legacy admission')),patch.object(Importer,'iter_dataset',side_effect=AssertionError('legacy iterator')),patch.object(Importer,'_check_dataset',side_effect=AssertionError('legacy verification')):
            complete=self.evaluate(root,db,document)
        phoenix=self.check(complete,'phoenix-status-observation');system=self.check(complete,'system-status-observation')
        self.assertEqual(complete['descriptor']['context']['pipeline_version'],'automatic-reports-9')
        self.assertTrue(complete['descriptor']['full_source_verified'])
        self.assertGreater(complete['descriptor']['scanned_rows'],300001)
        self.assertEqual((phoenix['coverage']['expected_cycles'],system['coverage']['expected_cycles']),(301,301))
        sdk_warning=next(f for f in phoenix['findings'] if f['signal']=='DriveVelocity')
        self.assertEqual(sdk_warning['observed_false_status_records'],1)
        self.assertGreater(sdk_warning['source_references'][0]['status_ok']['record_index'],300000)
        self.assertEqual(sdk_warning['source_references'][0]['recorded_status_code'],-7)
        self.assertEqual(phoenix['coverage']['module_observations'][0]['timing_cycles'],300)
        self.assertEqual(phoenix['coverage']['module_observations'][0]['status_cycles'],301)
        flag=next(f for f in system['findings'] if f['kind']=='recorded_system_fault_true')
        counter=next(f for f in system['findings'] if f['kind']=='recorded_system_fault_counter_increased')
        self.assertEqual(flag['observations'],1);self.assertEqual(counter['observations'],1)
        self.assertEqual(counter['source_references'][0]['recorded_change'],'3')
        self.assertGreater(flag['source_references'][0]['observation']['record_index'],300000)
        self.assertAlmostEqual(system['coverage']['eligible_seconds'],6)
        for check in (phoenix,system):
            self.assertEqual(check['outcome'],'finding')
            self.assertNotIn('report_resource_limit_reached',check['unavailable'])
            self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])
        for index in (1,4):self.assertEqual(complete['report']['checks'][index]['unavailable'],['stream_evaluator_not_converted'])
        self.assertEqual(complete['report']['overall_health'],'not_assessed')

    def test_sequence_entry_finish_start_breaks_regression_and_distinct_identity(self):
        sequence='/Drive/Module0/PhoenixObservationSequence'
        def lifecycle(index,output,fields,specs,stamp):
            if index!=2:return
            entry=fields[sequence]
            output.write(wire_record(0,b'\1'+struct.pack('<I',entry),stamp//1000))
            output.write(start(entry,sequence,'int64'))
        root,db,_,document=self.fixture(cycles=2,lifecycle=lifecycle,
            updates={2:{sequence:1},3:{sequence:2}})
        complete=self.evaluate(root,db,document,max_rows=5,max_bytes=997)
        phoenix=self.check(complete,'phoenix-status-observation');module=phoenix['coverage']['module_observations'][0]
        self.assertEqual(module['timing_cycles'],3)
        self.assertEqual(module['sequence_regressions'],0)
        self.assertEqual(module['distinct_sdk_observations'],3)
        self.assertEqual(module['held_snapshot_cycles'],0)
        self.assertNotIn('phoenix_observation_sequence_regressed:front-left',phoenix['unavailable'])
        self.assertEqual(phoenix['outcome'],'evaluated_no_finding')

    def test_batches_and_fresh_connection_restart_have_identical_status_receipt(self):
        receipts=[]
        for restart in (False,True):
            root,db,_,document=self.fixture(cycles=15,full_updates=False,
                updates={2:{SDK_FLAG:False,SDK_CODE:-7},4:{FLAG:True,COUNT:3,BATTERY:10.0},8:{COUNT:1,BATTERY:11.0}})
            runner=self.runner(root,db,chunk_bytes=4096)
            if restart:
                from robot_test_hub import reports
                scheduled=reports.schedule_stream_reports(root,db,document,runner=runner,pipeline_version='automatic-reports-9')[0]
                for _ in range(10000):
                    progress=runner.step(scheduled['stream_id'],max_rows=7,max_bytes=997)
                    if progress['chunk_count']>5:break
                else:self.fail('No durable S2 checkpoint')
                runner.close();db.close();db=open_catalog(root);self.addCleanup(db.close)
                runner=self.runner(root,db,chunk_bytes=4096)
            complete=self.evaluate(root,db,document,runner,max_rows=11 if restart else 4096,max_bytes=997 if restart else 4*1024*1024)
            receipts.append((complete['descriptor']['selection_chain'],complete['descriptor']['input_digest'],complete['report']))
            if restart:self.assertGreater(complete['replay_bytes'],0)
        self.assertEqual(receipts[0],receipts[1])

    def test_invalid_sdk_timing_breaks_sequence_continuity_but_not_false_status_warning(self):
        sequence='/Drive/Module0/PhoenixObservationSequence'
        timing='/Drive/Module0/PhoenixVendorObservationStartSeconds'
        root,db,_,document=self.fixture(cycles=2,updates={2:{timing:float('nan'),SDK_FLAG:False,SDK_CODE:-7},3:{sequence:1}})
        complete=self.evaluate(root,db,document,max_rows=5,max_bytes=997)
        phoenix=self.check(complete,'phoenix-status-observation');module=phoenix['coverage']['module_observations'][0]
        self.assertEqual(module['status_cycles'],3)
        self.assertEqual(module['timing_cycles'],2)
        self.assertEqual(module['sequence_regressions'],0)
        # Restoring the same qualified sequence under the same entry generation
        # breaks continuity but supplies no new declared SDK observation identity.
        self.assertEqual(module['distinct_sdk_observations'],1)
        self.assertEqual(phoenix['findings'][0]['observed_false_status_records'],1)
        self.assertFalse(phoenix['coverage']['physical_acquisition_time_qualified'])

    def test_after_run_source_order_hazard_disqualifies_status_coverage(self):
        # Constructed pinned derived legacy fixture. The current raw importer
        # rejects regressed cycle payloads, which is a separate admission rule.
        root,db,imported,document=self.fixture()
        dataset=root/imported['dataset_path'];rows=[json.loads(line) for line in dataset.read_text().splitlines()]
        cycle=next(row for row in reversed(rows) if row['kind']=='cycle')
        rows.extend(({**cycle,'timestamp_ns':str(START+200000000)},
                     {**cycle,'timestamp_ns':str(START+40000000)}))
        data=''.join(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n' for row in rows).encode();dataset.write_bytes(data)
        manifest_path=root/imported['manifest_path'];manifest=json.loads(manifest_path.read_bytes())
        manifest.update(dataset_sha256=hashlib.sha256(data).hexdigest(),last_cycle_timestamp_ns=str(START+200000000))
        encoded=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode();manifest_path.write_bytes(encoded)
        with db:db.execute('UPDATE import_jobs SET dataset_sha256=?,manifest_sha256=? WHERE id=?',(manifest['dataset_sha256'],hashlib.sha256(encoded).hexdigest(),imported['id']))
        complete=self.evaluate(root,db,document,max_rows=7,max_bytes=997)
        self.assertEqual(complete['descriptor']['source_hazards']['total'],1)
        for name in ('phoenix-status-observation','system-status-observation'):
            check=self.check(complete,name)
            self.assertEqual(check['coverage']['expected_cycles'],9)
            self.assertEqual(check['outcome'],'insufficient_data')
            self.assertIn('full_source_order_or_reference_unqualified',check['unavailable'])
            self.assertFalse(check['findings'])
        self.assertFalse(self.check(complete,'system-status-observation')['coverage']['complete_recorded_profile'])

    def test_gap_and_entry_generation_reset_counter_and_voltage_comparisons(self):
        def lifecycle(index,output,fields,specs,stamp):
            if index!=2:return
            for field in (COUNT,BATTERY):
                entry=fields[field]
                output.write(wire_record(0,b'\1'+struct.pack('<I',entry),stamp//1000))
                output.write(start(entry,field,specs[field][0],json.dumps({'unit':specs[field][1]})))
        for boundary in ('gap','entry'):
            with self.subTest(boundary=boundary):
                stamps=([START+20000000,START+320000001,START+420000001] if boundary=='gap'
                        else [START+20000000,START+120000000,START+220000000])
                root,db,_,document=self.fixture(cycles=2,times=stamps,allow_multiple_runs=boundary=='gap',lifecycle=lifecycle if boundary=='entry' else None,
                    updates={1:{COUNT:9,BATTERY:12.0},2:{COUNT:12,BATTERY:10.0},3:{COUNT:13,BATTERY:8.0}})
                if boundary=='gap':
                    # Run derivation correctly splits an unknown 300ms gap.
                    # Exercise status adjacency over an explicitly constructed
                    # observation window; this is not a derived run claim.
                    merged={**document['runs'][0],'run_id':'constructed-gap-window',
                        'end_monotonic_ns':document['runs'][-1]['end_monotonic_ns'],'completeness':'complete'}
                    document={**document,'runs':[merged]}
                complete=self.evaluate(root,db,document,max_rows=5,max_bytes=997)
                system=self.check(complete,'system-status-observation')
                counters=[finding for finding in system['findings'] if 'counter_' in finding['kind']]
                self.assertEqual(len(counters),1)
                self.assertEqual(counters[0]['observations'],1)
                self.assertEqual(counters[0]['source_references'][0]['recorded_change'],'1')
                self.assertAlmostEqual(system['coverage']['eligible_seconds'],.1)
                self.assertEqual(self.metric(system,'recorded_battery_voltage_time_weighted_mean')['value'],10.0)
                self.assertFalse(system['coverage']['counter_semantics_qualified'])

    def test_restored_clock_with_same_held_sequence_is_not_new_sdk_observation(self):
        clock='/Drive/Module0/PhoenixObservationClockValid'
        root,db,_,document=self.fixture(cycles=2,full_updates=False,updates={2:{clock:False},3:{clock:True}})
        complete=self.evaluate(root,db,document,max_rows=5,max_bytes=997)
        module=self.check(complete,'phoenix-status-observation')['coverage']['module_observations'][0]
        self.assertEqual(module['timing_cycles'],2)
        self.assertEqual(module['status_cycles'],3)
        self.assertEqual(module['distinct_sdk_observations'],1)
        self.assertEqual(module['sequence_regressions'],0)

    def test_invalid_units_clear_only_affected_comparisons_until_fresh_valid_records(self):
        def lifecycle(index,output,fields,specs,stamp):
            if index not in (2,3):return
            for field in (COUNT,BATTERY):
                metadata={'unit':'seconds'} if index==2 else ({'unit':'volts'} if field==BATTERY else {})
                output.write(wire_record(0,b'\2'+struct.pack('<I',fields[field])+text(json.dumps(metadata)),stamp//1000))
        root,db,_,document=self.fixture(cycles=3,lifecycle=lifecycle,
            times=[START+20000000,START+120000000,START+220000000,START+320000000],
            updates={1:{COUNT:9,BATTERY:12.0},2:{COUNT:12,BATTERY:10.0},3:{COUNT:13,BATTERY:8.0},4:{COUNT:15,BATTERY:6.0}})
        complete=self.evaluate(root,db,document,max_rows=5,max_bytes=997)
        system=self.check(complete,'system-status-observation')
        counter=next(f for f in system['findings'] if f['kind']=='recorded_system_fault_counter_increased')
        self.assertEqual(counter['observations'],1)
        self.assertEqual(counter['source_references'][0]['recorded_change'],'2')
        self.assertEqual(system['coverage']['complete_cycles'],3)
        self.assertFalse(system['coverage']['complete_recorded_profile'])
        self.assertAlmostEqual(system['coverage']['eligible_seconds'],.1)
        self.assertEqual(self.metric(system,'recorded_battery_voltage_time_weighted_mean')['value'],8.0)

    def test_many_entry_lifecycles_use_disk_history_and_bounded_checkpoint_maps(self):
        sizes=[]
        for replacements in (10,1000):
            def lifecycle(index,output,fields,specs,stamp):
                if index!=2:return
                for offset in range(replacements):
                    output.write(wire_record(0,b'\1'+struct.pack('<I',fields[COUNT]),stamp//1000))
                    fields[COUNT]=40000+offset
                    output.write(start(fields[COUNT],COUNT,'int64'))
                    output.write(wire_record(fields[COUNT],struct.pack('<q',offset),stamp//1000))
            root,db,_,document=self.fixture(cycles=2,lifecycle=lifecycle)
            complete=self.evaluate(root,db,document,max_rows=19,max_bytes=4096)
            raw=db.execute('SELECT state_json FROM analysis_stream_checkpoints WHERE stream_id=?',(complete['stream_id'],)).fetchone()[0]
            state=json.loads(raw);sizes.append(len(raw.encode()))
            from robot_test_hub.stream_mapper import FIELDS
            self.assertLessEqual(len(state['mapper']['held']),len(FIELDS))
            self.assertLessEqual(len(state['mapper']['epochs']),len(FIELDS))
            self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM analysis_stream_unique WHERE namespace='mapper-retired'").fetchone()[0],replacements)
            self.assertLess(sizes[-1],512*1024)
        self.assertLess(abs(sizes[1]-sizes[0]),4096)
