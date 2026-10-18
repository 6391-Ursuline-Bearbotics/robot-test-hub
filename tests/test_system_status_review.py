"""Independent typed-wire, held-state and legacy regression integration checks."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from robot_test_hub.storage import open_catalog
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from test_wpilog import log_bytes,start,wire_record,text

FAULTS=('Brownout','CANBusDown','CANBusUnavail','Display','IMU','IO','RSL','USB')
FIELDS={'/SystemStats/BatteryVoltage'}|{'/SystemStats/'+prefix+'/'+name for prefix in ('Faults','FaultCounts') for name in FAULTS}
EPOCH=1800000000000000000


def recording(*,initial_fault=False,counters=False,finish=False,reuse=False,late_fault=False,wrong_unit=False,header_regression=False,unrelated_control=False,large_counter=False):
    events=[];ids={};next_id=10
    def field(name,kind,value,metadata='{"source":"AdvantageKit"}'):
        nonlocal next_id
        identity=next_id;next_id+=1;ids[name]=identity
        events.extend((start(identity,name,kind,metadata),wire_record(identity,value)))
    for name,value in (('/RealOutputs/TestHub/RobotId','review-system'),('/RealOutputs/TestHub/BootId','review-boot'),
                       ('/RealOutputs/TestHub/RuntimeMode','SIM'),('/RealOutputs/TestHub/Mode','TELEOPERATED')):
        field(name,'string',value.encode())
    field('/RealOutputs/TestHub/StateKnown','boolean',b'\x01')
    field('/RealOutputs/TestHub/Enabled','boolean',b'\x00')
    field('/DriverStation/Enabled','boolean',b'\x00')
    field('/SystemStats/EpochTimeValid','boolean',b'\x01')
    field('/SystemStats/EpochTime','double',struct.pack('<d',EPOCH//1000),'{"unit":"microseconds"}')
    field('/SystemStats/BatteryVoltage','double',struct.pack('<d',11.25),json.dumps({'unit':'millivolts' if wrong_unit else 'volts'}))
    for name in FAULTS:
        field('/SystemStats/Faults/'+name,'boolean',bytes([initial_fault and name=='Brownout']))
        field('/SystemStats/FaultCounts/'+name,'int64',struct.pack('<q',9 if name=='Brownout' else 0))
    for index in range(1,5):
        ns=1000000000+index*20000000
        if finish and index==2:
            identity=ids['/SystemStats/Faults/Brownout']
            events.append(wire_record(0,b'\x01'+struct.pack('<I',identity),ns//1000))
            if reuse:
                events.extend((wire_record(0,b'\0'+struct.pack('<I',identity)+text('/Unrelated/ReusedEntry')+text('boolean')+text('{"source":"AdvantageKit"}'),ns//1000),wire_record(identity,b'\x01',ns//1000)))
        if header_regression and index==2:
            events.append(wire_record(ids['/SystemStats/Faults/Brownout'],b'\x00',990000))
        if unrelated_control and index==2:
            events.append(wire_record(0,b'\0'+struct.pack('<I',99)+text('/Unrelated/Later')+text('boolean')+text('{}'),ns//1000))
        events.append(wire_record(1,struct.pack('<q',ns),ns//1000))
        for name in ('/RealOutputs/TestHub/Enabled','/DriverStation/Enabled'):
            events.append(wire_record(ids[name],bytes([index<4]),ns//1000))
        events.append(wire_record(ids['/SystemStats/EpochTime'],struct.pack('<d',(EPOCH+ns-1000000000)//1000),ns//1000))
        if initial_fault and index==3 and not finish:
            events.append(wire_record(ids['/SystemStats/Faults/Brownout'],b'\x00',ns//1000))
        if large_counter and index in (1,2,3):
            events.append(wire_record(ids['/SystemStats/FaultCounts/Brownout'],struct.pack('<q',{1:2**53,2:2**63-2,3:2**63-1}[index]),ns//1000))
        if counters and index in (2,3):
            events.append(wire_record(ids['/SystemStats/FaultCounts/Brownout'],struct.pack('<q',12 if index==2 else 4),ns//1000))
    if late_fault:
        ns=1100000000
        events.append(wire_record(1,struct.pack('<q',ns),ns//1000))
        events.append(wire_record(ids['/SystemStats/Faults/USB'],b'\x01',ns//1000))
    return log_bytes(*events)


class SystemStatusReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='system review ');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.db=open_catalog(self.root);self.addCleanup(self.db.close)

    def analyzed(self,**flags):
        source=self.root/'invented.wpilog';source.write_bytes(recording(**flags))
        job=Importer(self.root,self.db).import_file(source,source_type='SYNTHETIC')
        self.assertEqual(job['state'],'succeeded')
        Pipeline(self.root,self.db).tick()
        reports=[json.loads(row[0]) for row in self.db.execute('SELECT result_json FROM analysis_reports')]
        self.assertEqual(len(reports),1)
        return next(check for check in reports[0]['checks'] if check['analyzer_id']=='system-status-observation')

    def test_delta_held_before_run_true_is_one_record_not_new_physical_samples(self):
        check=self.analyzed(initial_fault=True,counters=True)
        self.assertEqual(check['outcome'],'finding')
        flags=[x for x in check['findings'] if x['kind']=='recorded_system_fault_true']
        self.assertEqual([(x['signal'],x['observations']) for x in flags],[('Brownout',1)])
        reference=flags[0]['source_references'][0]['observation']
        self.assertTrue(reference['before_run']);self.assertTrue(reference['held_logger_state'])
        self.assertFalse(reference['physical_acquisition_time_qualified'])
        changes=[x for x in check['findings'] if 'counter_' in x['kind']]
        self.assertEqual(len(changes),1) # pre-run nine is not an in-run comparison baseline
        observation=changes[0]['source_references'][0]
        self.assertEqual((observation['previous_recorded_value'],observation['recorded_value'],observation['recorded_change']),('12','4','-8'))
        self.assertFalse(observation['physical_reset_qualified']);self.assertFalse(observation['physical_event_count_qualified'])
        self.assertEqual([x['value'] for x in check['metrics']],[11.25]*3)

    def test_finish_and_reused_unrelated_true_cannot_fill_missing_fault_coverage(self):
        check=self.analyzed(finish=True,reuse=True)
        self.assertEqual(check['outcome'],'insufficient_data')
        self.assertFalse(check['coverage']['complete_recorded_profile'])
        self.assertFalse(check['findings'])
        brownout=next(x for x in check['coverage']['fields'] if x['field']=='/SystemStats/Faults/Brownout')
        self.assertLess(brownout['covered_cycles'],check['coverage']['expected_cycles'])

    def test_post_run_fault_is_not_attributed_to_prior_run(self):
        check=self.analyzed(late_fault=True)
        self.assertEqual(check['outcome'],'evaluated_no_finding')
        self.assertFalse(check['findings'])
        self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])

    def test_wrong_voltage_units_are_unavailable_not_zero_or_pass(self):
        check=self.analyzed(wrong_unit=True)
        self.assertEqual(check['outcome'],'insufficient_data')
        self.assertTrue(all(x['value'] is None for x in check['metrics']))

    def test_same_entry_older_wire_header_cannot_produce_no_finding(self):
        check=self.analyzed(header_regression=True)
        self.assertNotEqual(check['outcome'],'evaluated_no_finding')
        self.assertFalse(check['coverage']['complete_recorded_profile'])

    def test_unrelated_entry_start_does_not_shorten_valid_voltage_interval(self):
        check=self.analyzed(unrelated_control=True)
        self.assertEqual(check['outcome'],'evaluated_no_finding')
        mean=next(x for x in check['metrics'] if x['name']=='recorded_battery_voltage_time_weighted_mean')
        self.assertAlmostEqual(mean['eligible_seconds'],.06)

    def test_wire_int64_counters_above_json_safe_integer_keep_exact_changes(self):
        check=self.analyzed(large_counter=True)
        self.assertEqual(check['outcome'],'finding')
        changes=[x for x in check['findings'] if x['kind']=='recorded_system_fault_counter_increased']
        self.assertEqual(len(changes),1)
        refs=changes[0]['source_references']
        self.assertEqual([(x['previous_recorded_value'],x['recorded_value'],x['recorded_change']) for x in refs],
                         [(str(2**53),str(2**63-2),str((2**63-2)-2**53)),(str(2**63-2),str(2**63-1),'1')])
        self.assertTrue(check['coverage']['complete_recorded_profile'])

    def test_legacy_selector_case_still_rejects_coherently_resealed_invented_rows(self):
        from test_regression_review import RegressionReviewTests
        from robot_test_hub import regression
        from robot_test_hub.analysis import identity
        fixture=RegressionReviewTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        folder,_=fixture.exported(pipeline_version='automatic-reports-5')
        self.assertTrue(regression.verify_case(folder)['verified'])
        case=json.loads((folder/'case.json').read_bytes())
        evidence=json.loads((folder/'evidence.json').read_bytes())
        job=json.loads((folder/'saved-job.json').read_bytes())
        self.assertEqual(evidence['context']['pipeline_version'],'automatic-reports-5')
        row=next(x for x in evidence['rows'] if x.get('field')=='/Drive/Module0/Connected' and x.get('value') is False)
        row['value']=True
        job['provenance']['rows_digest']=identity(evidence['rows'])
        job['job_id']=identity([case['framework_version'],job['declaration'],job['provenance'],job['configuration']])
        case['job_id']=job['job_id'];case['review']['job_id']=job['job_id']
        case['review']['finding_identity']=identity([job['job_id'],case['review']['finding_index'],job['findings'][case['review']['finding_index']]])
        for name,value in (('case.json',case),('evidence.json',evidence),('saved-job.json',job)):
            (folder/name).write_text(json.dumps(value,sort_keys=True,separators=(',',':'))+'\n',encoding='utf-8')
        fixture.reseal(folder)
        with self.assertRaises(regression.RegressionError):regression.verify_case(folder)

    def test_literal_field_set_matches_pinned_advantagekit_source(self):
        from robot_test_hub import system_status
        self.assertEqual(set(system_status.FIELDS),FIELDS)
        folder=Path('C:/Users/jason/.gradle/caches/modules-2/files-2.1/org.littletonrobotics.akit/akit-java/27.0.0-alpha-6')
        jars=list(folder.glob('*/akit-java-27.0.0-alpha-6-sources.jar'))
        if not jars:self.skipTest('Pinned installed AK source jar is unavailable; portable wire cases still run')
        jar=jars[0]
        self.assertEqual(hashlib.sha256(jar.read_bytes()).hexdigest(),'d822d7625a9553c311526134721ceabac79035a5f296c3e88f6bac7bda3cc1ea')
        with ZipFile(jar) as archive:
            stats=archive.read('org/littletonrobotics/junction/LoggedSystemStats.java').decode()
            conduit=archive.read('org/littletonrobotics/conduit/ConduitApi.java').decode()
            logger=archive.read('org/littletonrobotics/junction/Logger.java').decode()
        self.assertIn('LoggedSystemStats.saveToLog(entry.getSubtable("SystemStats"))',logger)
        self.assertIn('table.put("BatteryVoltage", conduit.getBatteryVoltage(), "volts")',stats)
        for name in FAULTS:
            self.assertIn('table.put("Faults/'+name+'",',stats)
            self.assertIn('table.put("FaultCounts/'+name+'",',stats)
        self.assertIn('public boolean getFaultBrownout()',conduit)
        self.assertIn('public long getFaultCountBrownout()',conduit)
        self.assertIn('public double getBatteryVoltage()',conduit)


if __name__=='__main__':unittest.main()
