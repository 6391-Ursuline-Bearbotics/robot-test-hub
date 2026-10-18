"""Independent S1 streaming checks using invented WPILOG bytes and exact receipts."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from robot_test_hub.importer import Importer
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
from test_wpilog import log_bytes, start, wire_record

UTC=1800000000000000000
START=1000000000


def write_recording(path,*,prefix_records=0,cycles=8,connections_per_cycle=1,anomaly=True,anomaly_at=None,fraction_ns=0):
    """One exact enabled run; every prefix byte is outside its interval."""
    fields={};initial=[]
    def declare(name,kind,value,metadata='{"source":"AdvantageKit"}'):
        entry=len(fields)+2;fields[name]=entry
        initial.extend((start(entry,name,kind,metadata),wire_record(entry,value)))
    for field,value in (('/Metadata/RobotId','stream-review-robot'),('/Metadata/BootId','stream-review-boot'),
        ('/RealOutputs/TestHub/RobotId','stream-review-robot'),('/RealOutputs/TestHub/BootId','stream-review-boot'),
        ('/RealOutputs/TestHub/RuntimeMode','SIM'),('/RealOutputs/TestHub/Mode','TELEOPERATED'),
        ('/RealOutputs/TestHub/ConfigurationSHA256','c'*64)):
        declare(field,'string',value.encode())
    declare('/RealOutputs/TestHub/StateKnown','boolean',b'\1')
    for field in ('/DriverStation/Enabled','/RealOutputs/TestHub/Enabled'):
        declare(field,'boolean',b'\0')
    declare('/SystemStats/EpochTimeValid','boolean',b'\1')
    declare('/SystemStats/EpochTime','double',struct.pack('<d',UTC//1000),'{"unit":"microseconds"}')
    declare('/Drive/Module0/DriveConnected','boolean',b'\1')
    declare('/Review/IrrelevantPrefix','string',b'initial')
    with path.open('wb') as stream:
        stream.write(log_bytes(*initial))
        prefix=wire_record(fields['/Review/IrrelevantPrefix'],b'prefix')
        for start_index in range(0,prefix_records,4096):stream.write(prefix*min(4096,prefix_records-start_index))
        for index in range(1,cycles+2):
            stamp=START+index*20000000+fraction_ns
            stream.write(wire_record(1,struct.pack('<q',stamp),stamp//1000))
            for field in ('/DriverStation/Enabled','/RealOutputs/TestHub/Enabled'):
                stream.write(wire_record(fields[field],bytes([index<=cycles]),stamp//1000))
            stream.write(wire_record(fields['/SystemStats/EpochTime'],struct.pack('<d',(UTC+stamp-START)//1000),stamp//1000))
            stream.write(wire_record(fields['/Drive/Module0/DriveConnected'],b'\1',stamp//1000)*connections_per_cycle)
            if anomaly and index==(cycles if anomaly_at is None else anomaly_at):
                stream.write(wire_record(fields['/Drive/Module0/DriveConnected'],b'\0',stamp//1000))
    return path


def imported_fixture(root,db,**settings):
    source=write_recording(root/'invented-stream-review.wpilog',**settings)
    imported=Importer(root,db).import_file(source,source_type='SYNTHETIC')
    if imported['state']!='succeeded':raise AssertionError(imported)
    document=rebuild_from_imports(root,db)
    if len(document['runs'])!=1:raise AssertionError(document)
    return imported,document

class StreamReviewTests(unittest.TestCase):
    def fixture(self,**settings):
        temporary=tempfile.TemporaryDirectory(prefix='independent S1 ')
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name).resolve()
        db=open_catalog(root);self.addCleanup(db.close)
        imported,document=imported_fixture(root,db,**settings)
        return root,db,imported,document

    def runner(self,root,db,**limits):
        from robot_test_hub.stream_analysis import StreamRunner
        from robot_test_hub.stream_quality import RecordingQualityReducer
        runner=StreamRunner(root,db,reducer=RecordingQualityReducer(),limits={'min_free_bytes':0,**limits})
        self.addCleanup(runner.close)
        return runner

    def drain(self,root,db,runner,**budgets):
        from robot_test_hub import reports
        for turn in range(40000):
            progress=reports.advance_streams(root,db,runner,**budgets)
            self.assertNotIn(progress['state'],('failed','canceled'),progress)
            if progress.get('report') is not None:return progress
        self.fail('Stream did not finish in bounded test turns')

    def schedule(self,root,db,document,runner):
        from robot_test_hub import reports
        return reports.schedule_stream_reports(root,db,document,runner=runner)[0]

    def test_exact_cycle_nanoseconds_scope_original_observation_and_synthetic_origin(self):
        root,db,_,document=self.fixture(fraction_ns=123,anomaly_at=1)
        runner=self.runner(root,db)
        self.schedule(root,db,document,runner)
        with patch.object(Importer,'read_manifest',side_effect=AssertionError('blocking admission')),patch.object(Importer,'iter_dataset',side_effect=AssertionError('blocking iterator')),patch.object(Importer,'_check_dataset',side_effect=AssertionError('blocking verifier')):
            complete=self.drain(root,db,runner,max_rows=7,max_bytes=997)
        report=complete['report'];quality=report['checks'][0]
        self.assertEqual(report['source_type'],'synthetic')
        self.assertEqual(quality['coverage']['cycle_count'],9)
        finding=next(f for f in quality['findings'] if f['kind']=='disconnected_observations')
        self.assertEqual(finding['count'],1)
        self.assertEqual(finding['source_references'][0]['stream_scope'],'in_run')
        self.assertFalse(quality['coverage']['physical_acquisition_time_qualified'])
        self.assertTrue(quality['provenance']['full_source_verified'])
        self.assertEqual(len(report['checks']),5)
        for check in report['checks'][1:]:self.assertEqual(check['unavailable'],['stream_evaluator_not_converted'])

    def test_fresh_connection_restart_does_not_double_count_committed_contributions(self):
        root,db,_,document=self.fixture(cycles=30,connections_per_cycle=10)
        runner=self.runner(root,db,chunk_bytes=4096)
        scheduled=self.schedule(root,db,document,runner)
        for _ in range(10000):
            partial=runner.step(max_rows=9,max_bytes=4096)
            if partial['chunk_count']>=2:break
        else:self.fail('No committed checkpoint')
        checkpoint=tuple(db.execute('SELECT chunk_ordinal,state_json,state_sha256 FROM analysis_stream_checkpoints').fetchone())
        runner.close();db.close()
        reopened=open_catalog(root);self.addCleanup(reopened.close)
        restarted=self.runner(root,reopened,chunk_bytes=4096)
        complete=self.drain(root,reopened,restarted,max_rows=23,max_bytes=12345)
        quality=complete['report']['checks'][0]
        self.assertEqual(quality['coverage']['cycle_count'],31)
        self.assertEqual(quality['coverage']['disconnected_source_count'],1)
        self.assertGreater(complete['replay_bytes'],0)
        self.assertEqual(reopened.execute('SELECT COUNT(*) FROM analysis_stream_unique').fetchone()[0],1)
        self.assertGreater(reopened.execute('SELECT chunk_ordinal FROM analysis_stream_checkpoints').fetchone()[0],checkpoint[0])
        self.assertEqual(complete['descriptor']['chunk_count'],reopened.execute('SELECT COUNT(*) FROM analysis_stream_chunks').fetchone()[0])

    def test_restart_hashes_unselected_raw_prefix_and_refuses_changed_source(self):
        root,db,_,document=self.fixture(prefix_records=100,cycles=20)
        runner=self.runner(root,db,chunk_bytes=4096)
        scheduled=self.schedule(root,db,document,runner)
        for _ in range(10000):
            partial=runner.step(max_rows=11,max_bytes=4096)
            if partial['chunk_count']>=1:break
        else:self.fail('No committed checkpoint')
        raw_pin=json.loads(db.execute('SELECT pins_json FROM analysis_stream_sources').fetchone()[0])['files']['raw']
        runner.close();db.close()
        path=root/raw_pin['path'];data=path.read_bytes();offset=data.index(b'prefix')
        changed=data[:offset]+b'PREFIX'+data[offset+6:];self.assertEqual(len(changed),len(data));path.write_bytes(changed)
        reopened=open_catalog(root);self.addCleanup(reopened.close)
        restarted=self.runner(root,reopened,chunk_bytes=4096)
        for _ in range(10000):
            progress=restarted.step(scheduled['stream_id'],max_rows=19,max_bytes=1000)
            if progress['state']=='failed':break
        else:self.fail('Changed outside-run prefix was accepted')
        self.assertEqual(progress['error_code'],'stream_source_hash_mismatch')
        self.assertEqual(reopened.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)

    def test_turn_boundaries_do_not_change_chunks_or_result(self):
        from robot_test_hub import reports
        receipts=[]
        for rows,byte_budget in ((1,311),(4096,4*1024*1024)):
            root,db,_,document=self.fixture(cycles=15,connections_per_cycle=5)
            runner=self.runner(root,db,chunk_bytes=4096)
            self.schedule(root,db,document,runner)
            complete=self.drain(root,db,runner,max_rows=rows,max_bytes=byte_budget)
            receipts.append((complete['descriptor']['selection_chain'],complete['descriptor']['result'],complete['report']))
        self.assertEqual(receipts[0],receipts[1])

    def test_context_cannot_override_selected_run_and_step_preserves_caller_transaction(self):
        from robot_test_hub.stream_analysis import StreamError
        root,db,_,document=self.fixture()
        runner=self.runner(root,db)
        run=document['runs'][0]
        for key,value in (('run_id','other'),('robot_id','other'),('boot_id','other'),('start_monotonic_ns','0'),('segment_ids',[])):
            with self.subTest(key=key),self.assertRaises(StreamError):runner.schedule(run,{key:value})
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0],0)
        self.schedule(root,db,document,runner)
        db.execute('CREATE TABLE review_sentinel(value)');db.commit()
        db.execute('INSERT INTO review_sentinel VALUES (1)')
        traced=[];db.set_trace_callback(traced.append)
        try:
            with self.assertRaisesRegex(ValueError,'owned_transaction'):runner.step()
            self.assertEqual(traced,[])
        finally:db.set_trace_callback(None)
        self.assertTrue(db.in_transaction)
        self.assertEqual(db.execute('SELECT value FROM review_sentinel').fetchone()[0],1)
        db.rollback()

    def test_actual_six_second_run_after_250k_prefix_includes_late_50k_observation(self):
        from robot_test_hub import reports
        root,db,_,document=self.fixture(prefix_records=250001,cycles=300,connections_per_cycle=180)
        run=document['runs'][0]
        self.assertEqual(int(run['end_monotonic_ns'])-int(run['start_monotonic_ns']),6000000000)
        legacy=reports.generate(root,db,document,pipeline_version='automatic-reports-7')
        self.assertTrue(all(check['outcome']=='insufficient_data' for check in legacy[0]['checks']))
        self.assertTrue(any('report_resource_limit_reached' in check['unavailable'] for check in legacy[0]['checks']))
        immutable=db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(legacy[0]['report_id'],)).fetchone()[0]
        runner=self.runner(root,db)
        self.schedule(root,db,document,runner)
        complete=self.drain(root,db,runner)
        descriptor=complete['descriptor'];quality=complete['report']['checks'][0]
        self.assertGreater(descriptor['scanned_rows'],300001)
        self.assertEqual(quality['coverage']['cycle_count'],301)
        self.assertEqual(quality['coverage']['span_seconds'],6)
        self.assertEqual(quality['coverage']['fraction'],1)
        self.assertGreater(quality['coverage']['known_connection_observations'],50000)
        self.assertEqual(quality['coverage']['disconnected_source_count'],1)
        self.assertEqual(quality['outcome'],'finding')
        finding=next(f for f in quality['findings'] if f['kind']=='disconnected_observations')
        self.assertGreater(finding['source_references'][0]['record_index'],300000)
        self.assertFalse(finding['physical_health_qualified'])
        self.assertEqual(db.execute('SELECT result_json FROM analysis_reports WHERE report_id=?',(legacy[0]['report_id'],)).fetchone()[0],immutable)
        self.assertTrue(descriptor['full_source_verified'])
        self.assertEqual(descriptor['source_count'],len(descriptor['source_pins']))
        self.assertLessEqual(max(len(json.dumps(row)) for row in runner.inventory(complete['stream_id'])['items']),4096)

    def test_changed_committed_chunk_is_preserved_and_never_published(self):
        root,db,_,document=self.fixture(cycles=20,connections_per_cycle=10)
        runner=self.runner(root,db,chunk_bytes=4096)
        scheduled=self.schedule(root,db,document,runner)
        for _ in range(10000):
            progress=runner.step(max_rows=11,max_bytes=4096)
            if progress['chunk_count']>=1:break
        else:self.fail('No checkpoint')
        chunk=root/db.execute('SELECT path FROM analysis_stream_chunks ORDER BY ordinal LIMIT 1').fetchone()[0]
        runner.close();db.close()
        original=chunk.read_bytes();conflict=b'!'+original[1:];chunk.write_bytes(conflict)
        reopened=open_catalog(root);self.addCleanup(reopened.close)
        restarted=self.runner(root,reopened,chunk_bytes=4096)
        for _ in range(10000):
            progress=restarted.step(scheduled['stream_id'],max_rows=29,max_bytes=8192)
            if progress['state']=='failed':break
        else:self.fail('Conflicting committed chunk accepted')
        self.assertEqual(progress['error_code'],'stream_chunk_conflict')
        self.assertEqual(chunk.read_bytes(),conflict)
        self.assertEqual(reopened.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)

    def test_source_append_after_admission_is_detected_before_publication(self):
        root,db,_,document=self.fixture()
        runner=self.runner(root,db);scheduled=self.schedule(root,db,document,runner)
        progress=runner.step(max_bytes=19)
        pin=json.loads(db.execute('SELECT pins_json FROM analysis_stream_sources').fetchone()[0])['files']['raw']
        with (root/pin['path']).open('ab') as output:output.write(b'unexpected suffix')
        progress=runner.step(scheduled['stream_id'])
        self.assertEqual((progress['state'],progress['error_code']),('failed','stream_source_changed'))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)

    def test_resource_failure_and_owned_workspace_conflict_preserve_originals(self):
        for failure in ('quota','workspace','line'):
            with self.subTest(failure=failure):
                root,db,_,document=self.fixture()
                limits={'max_spool_bytes':1} if failure=='quota' else {'line_bytes':64} if failure=='line' else {}
                runner=self.runner(root,db,**limits);scheduled=self.schedule(root,db,document,runner)
                originals={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in (root/'raw').rglob('*.wpilog')}
                if failure=='workspace':
                    directory=root/'analysis/streams'/scheduled['stream_id']/'g1';directory.mkdir(parents=True)
                    foreign=directory/'foreign.txt';foreign.write_text('preserve foreign evidence')
                for _ in range(10000):
                    progress=runner.step(scheduled['stream_id'],max_bytes=4096,max_rows=7)
                    if progress['state']=='failed':break
                else:self.fail('Resource/conflict did not terminate')
                expected={'quota':'stream_spool_resource_limit','workspace':'stream_chunk_conflict','line':'stream_line_resource_limit'}[failure]
                self.assertEqual(progress['error_code'],expected)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
                self.assertEqual(originals,{str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in (root/'raw').rglob('*.wpilog')})
                if failure=='workspace':self.assertEqual(foreign.read_text(),'preserve foreign evidence')

    def test_reducer_failure_rolls_back_uniqueness_and_checkpoint_together(self):
        from robot_test_hub.stream_quality import RecordingQualityReducer
        root,db,_,document=self.fixture(anomaly_at=1)
        runner=self.runner(root,db);self.schedule(root,db,document,runner)
        original=RecordingQualityReducer.consume
        def fail_after_real_insert(reducer,db,stream_id,generation,state,row):
            updated=original(reducer,db,stream_id,generation,state,row)
            if db.execute('SELECT COUNT(*) FROM analysis_stream_unique').fetchone()[0]:raise RuntimeError('invented crash after disk dedupe insertion')
            return updated
        with patch.object(RecordingQualityReducer,'consume',fail_after_real_insert):
            for _ in range(10000):
                progress=runner.step(max_bytes=4096,max_rows=7)
                if progress['state']=='failed':break
            else:self.fail('Injected reducer error was hidden')
        self.assertEqual(progress['error_code'],'stream_reducer_failed')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_unique').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_stream_chunks').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT chunk_ordinal FROM analysis_stream_checkpoints').fetchone()[0],-1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)

    def test_progress_get_performs_no_source_work_or_large_payload_reads(self):
        from robot_test_hub import stream_api
        from robot_test_hub.stream_analysis import StreamRunner
        root,db,_,document=self.fixture()
        runner=self.runner(root,db);scheduled=self.schedule(root,db,document,runner)
        before=tuple(db.execute('SELECT state,phase,verified_bytes,chunk_count FROM analysis_stream_jobs').fetchone())
        with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True)) as readonly:
            readonly.execute('PRAGMA query_only=ON')
            denied=[]
            def authorize(action,table,column,*unused):
                if action==sqlite3.SQLITE_READ and column in ('document','pins_json','run_json','context_json','descriptor_json','state_json'):
                    denied.append((table,column));return sqlite3.SQLITE_DENY
                if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE):
                    denied.append(('write',table));return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            readonly.set_authorizer(authorize)
            with patch.object(StreamRunner,'step',side_effect=AssertionError('GET advanced worker')),patch.object(StreamRunner,'schedule',side_effect=AssertionError('GET scheduled worker')),patch.object(Importer,'_check_dataset',side_effect=AssertionError('GET hashed sources')):
                page=stream_api.page(readonly,document['runs'][0]['run_id'],{})
            self.assertEqual(page['availability'],'available')
            self.assertEqual(page['items'][0]['stream_id'],scheduled['stream_id'])
            self.assertEqual(page['items'][0]['state'],'pending')
            self.assertFalse(page['items'][0]['source_verification_complete'])
            self.assertEqual(denied,[])
        self.assertEqual(tuple(db.execute('SELECT state,phase,verified_bytes,chunk_count FROM analysis_stream_jobs').fetchone()),before)

    def test_later_after_run_clock_regression_does_not_reenter_run_quality(self):
        # Explicitly constructed pinned legacy-derived fixture: the current raw
        # importer correctly rejects nonadvancing cycles before S1 admission.
        root,db,imported,document=self.fixture(anomaly=False)
        dataset=root/imported['dataset_path'];rows=[json.loads(line) for line in dataset.read_text().splitlines()]
        cycle=next(row for row in reversed(rows) if row['kind']=='cycle')
        observation=next(row for row in reversed(rows) if row['kind']=='observation' and row.get('field')=='/Drive/Module0/DriveConnected')
        for index,stamp in enumerate((START+200000000,START+40000000)):
            rows.extend(({**cycle,'timestamp_ns':str(stamp)},
                {**observation,'record_index':900000+index,'cycle_timestamp_ns':str(stamp),'value':False}))
        encoded=''.join(json.dumps(row,sort_keys=True,separators=(',',':'))+'\n' for row in rows).encode()
        dataset.write_bytes(encoded);dataset_hash=hashlib.sha256(encoded).hexdigest()
        manifest_path=root/imported['manifest_path'];manifest=json.loads(manifest_path.read_bytes());manifest['dataset_sha256']=dataset_hash
        manifest['last_cycle_timestamp_ns']=str(START+200000000)
        manifest_bytes=json.dumps(manifest,sort_keys=True,separators=(',',':')).encode();manifest_path.write_bytes(manifest_bytes)
        with db:db.execute('UPDATE import_jobs SET dataset_sha256=?,manifest_sha256=? WHERE id=?',(dataset_hash,hashlib.sha256(manifest_bytes).hexdigest(),imported['id']))
        runner=self.runner(root,db);self.schedule(root,db,document,runner)
        complete=self.drain(root,db,runner,max_rows=5,max_bytes=1024)
        quality=complete['report']['checks'][0]
        self.assertEqual(quality['coverage']['cycle_count'],9)
        self.assertEqual(quality['coverage']['disconnected_source_count'],0)
        self.assertEqual(quality['coverage']['span_seconds'],.16)
        self.assertEqual(quality['coverage']['fraction'],1)
        self.assertEqual(quality['coverage']['source_order_hazards']['by_scope'],{'after_run':1})
        self.assertEqual(quality['outcome'],'insufficient_data')
        self.assertIn('full_source_clock_order_unqualified',quality['unavailable'])
        self.assertFalse(quality['findings'])

    def test_rejected_worker_call_preserves_existing_caller_sql_guard(self):
        root,db,_,document=self.fixture()
        runner=self.runner(root,db);self.schedule(root,db,document,runner)
        db.execute('BEGIN')
        callbacks=[]
        def guard():callbacks.append(1);return 0
        db.set_progress_handler(guard,1)
        try:
            with self.assertRaisesRegex(ValueError,'owned_transaction'):runner.step()
            self.assertEqual(callbacks,[])
            db.execute('WITH RECURSIVE n(i) AS (VALUES(0) UNION ALL SELECT i+1 FROM n WHERE i<50) SELECT SUM(i) FROM n').fetchone()
            self.assertGreater(len(callbacks),0)
        finally:db.set_progress_handler(None,0);db.rollback()

    def test_actual_sql_budget_failure_is_terminal_but_other_sql_error_visible(self):
        from robot_test_hub import stream_analysis
        root,db,_,document=self.fixture(cycles=30,connections_per_cycle=20)
        runner=self.runner(root,db);scheduled=self.schedule(root,db,document,runner)
        original_read=runner._read
        def expensive_selected_sql(*args,**kwargs):
            db.execute('WITH RECURSIVE n(i) AS (VALUES(0) UNION ALL SELECT i+1 FROM n WHERE i<1000000) SELECT SUM(i) FROM n').fetchone()
            return original_read(*args,**kwargs)
        with patch.object(stream_analysis,'SQL_STEP_LIMIT',10000),patch.object(runner,'_read',expensive_selected_sql):
            progress=runner.step(scheduled['stream_id'])
        self.assertEqual(progress['error_code'],'stream_sql_resource_limit')
        self.assertFalse(db.in_transaction)
        self.assertEqual(runner.step(scheduled['stream_id'])['state'],'failed')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
        other_run={**document['runs'][0],'run_id':'different-sql-error-run'}
        other=runner.schedule(other_run,{})
        with patch.object(runner,'_read',side_effect=sqlite3.OperationalError('invented nonbudget I/O failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError,'nonbudget'):runner.step(other['stream_id'])
        self.assertNotEqual(runner.progress(other['stream_id'])['state'],'failed')
        self.assertFalse(db.in_transaction)
