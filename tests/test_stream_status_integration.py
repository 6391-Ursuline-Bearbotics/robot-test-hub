"""Versioned stream status publication, worker dispatch and durable recovery."""
from contextlib import closing
import copy,json,struct,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from robot_test_hub import reports
from robot_test_hub.backup import Backup,restore
from robot_test_hub.importer import Importer
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.runs import rebuild_from_imports
from robot_test_hub.storage import open_catalog
from robot_test_hub.stream_analysis import StreamRunner,StreamError
from robot_test_hub.stream_quality import RecordingQualityReducer
from test_auto_swerve import protocol_recording
from test_phoenix_status import values
from test_system_status_integration import with_status
from test_wpilog import start,wire_record
from robot_test_hub.phoenix_status import COMMON,SIGNALS,SIGNAL_FIELDS


def status_recording():
    raw=with_status(protocol_recording())
    header=12+int.from_bytes(raw[8:12],'little');events=[];entry=20000
    for module in range(4):
        for name,value in values(1,1100000000).items():
            spec=COMMON.get(name)
            if spec is None:
                signal=next(signal for signal in SIGNALS if name.startswith(signal));spec=SIGNAL_FIELDS[name[len(signal):]]
            kind=spec[0]
            if module==0 and name=='DriveVelocityStatusOk':value=False
            payload=(bytes([value]) if kind=='boolean' else struct.pack('<q',value) if kind=='int64' else
                     struct.pack('<d',value) if kind=='double' else value.encode())
            events.extend((start(entry,f'/Drive/Module{module}/Phoenix'+name,kind,'{}'),wire_record(entry,payload)));entry+=1
    return raw[:header]+b''.join(events)+raw[header:]


class StreamStatusIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='stream status integration ');self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name);self.root=self.base/'archive';self.root.mkdir()
        self.db=open_catalog(self.root);self.addCleanup(lambda:self.db.close())
        source=self.base/'invented-status.wpilog';source.write_bytes(status_recording())
        imported=Importer(self.root,self.db).import_file(source,source_type='SYNTHETIC');self.assertEqual(imported['state'],'succeeded')
        full=rebuild_from_imports(self.root,self.db);self.document={**full,'runs':full['runs'][:1]}
    def runner(self,root=None,db=None,**limits):
        runner=reports.StreamReportRunner(root or self.root,db or self.db,limits={'min_free_bytes':0,**limits})
        self.addCleanup(runner.close);return runner
    def drain(self,runner,stream_id=None,root=None,db=None,**budgets):
        for _ in range(20000):
            progress=(reports.advance_streams(root or self.root,db or self.db,runner,**budgets) if stream_id is None else runner.step(stream_id,**budgets))
            self.assertNotEqual(progress.get('state'),'failed',progress)
            if progress.get('report') is not None:return progress
            if stream_id is not None and progress['state']=='ready':return progress
        self.fail('No complete verified report')
    def test_explicit_nine_reports_source_reviewed_status_with_no_tuple_admission(self):
        runner=self.runner()
        with patch.object(Importer,'read_manifest',side_effect=AssertionError('legacy admission')),patch.object(Importer,'iter_dataset',side_effect=AssertionError('legacy admission')),patch.object(Importer,'_check_dataset',side_effect=AssertionError('legacy admission')):
            scheduled=reports.generate(self.root,self.db,self.document,pipeline_version='automatic-reports-9')
            self.assertEqual(len(scheduled),1);self.assertIsNone(scheduled[0]['report_id'])
            complete=self.drain(runner,max_rows=9,max_bytes=4096)
        report=complete['report'];self.assertEqual(report['overall_health'],'not_assessed');self.assertEqual(report['source_type'],'synthetic')
        self.assertEqual([check['analyzer_id'] for check in report['checks']],['recording-quality','swerve-tracking','phoenix-status-observation','system-status-observation','recorded-steering-observation'])
        for index in (1,4):self.assertEqual(report['checks'][index]['unavailable'],['stream_evaluator_not_converted'])
        for index,version in ((2,'stream-phoenix-status-1'),(3,'stream-system-status-1')):
            check=report['checks'][index];self.assertEqual(check['outcome'],'finding',check)
            self.assertEqual(check['analyzer_version'],version);self.assertFalse(check['coverage']['physical_acquisition_time_qualified'])
            self.assertTrue(check['provenance']['full_source_verified'])
            self.assertEqual(check['provenance']['context']['stream_reducer_id'],'recorded-status@stream-recorded-status-1')
            self.assertEqual(check['provenance']['context']['stream_mapper_version'],'stream-cycle-mapper-1')
        self.assertEqual(complete['descriptor']['result']['stream_result_format'],'stream-status-results-1')
    def test_pending_eight_and_nine_resume_through_same_worker_without_reinterpreting_eight(self):
        old=reports.generate(self.root,self.db,self.document,pipeline_version='automatic-reports-8')[0]
        current=reports.generate(self.root,self.db,self.document,pipeline_version='automatic-reports-9')[0]
        self.assertNotEqual(old['stream_id'],current['stream_id'])
        checkpoint=self.db.execute('SELECT reducer_id,state_json,state_sha256 FROM analysis_stream_checkpoints WHERE stream_id=?',(old['stream_id'],)).fetchone()
        self.assertEqual(checkpoint[0],'recording-quality@stream-recording-quality-1')
        self.db.close();self.db=open_catalog(self.root)
        pipeline=Pipeline(self.root,self.db,stream_limits={'min_free_bytes':0});self.addCleanup(pipeline.close)
        for _ in range(2000):
            pipeline.tick()
            if self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs WHERE report_id IS NOT NULL').fetchone()[0]==2:break
        else:self.fail('Worker did not dispatch both saved policies')
        saved=[]
        for stream in (old,current):
            raw=self.db.execute('SELECT result_json FROM analysis_reports WHERE report_id=(SELECT report_id FROM analysis_stream_jobs WHERE stream_id=?)',(stream['stream_id'],)).fetchone()[0]
            saved.append(json.loads(raw))
        self.assertEqual(saved[0]['checks'][0]['provenance']['context']['pipeline_version'],'automatic-reports-8')
        self.assertEqual(saved[1]['checks'][0]['provenance']['context']['pipeline_version'],'automatic-reports-9')
        for check in saved[0]['checks'][1:]:self.assertEqual(check['analyzer_version'],'stream-unconverted-1')
        eight_ready=self.db.execute('SELECT descriptor_json FROM analysis_stream_jobs WHERE stream_id=?',(old['stream_id'],)).fetchone()[0]
        self.assertEqual(json.loads(eight_ready)['result']['outcome'],saved[0]['checks'][0]['outcome'])
        self.assertNotIn('stream_result_format',json.loads(eight_ready)['result'])
        self.assertEqual(json.loads(json.dumps(reports.publish_stream_report(self.db,old['stream_id'],json.loads(eight_ready)))),saved[0])
    def test_nine_admission_cannot_silently_schedule_through_a_fixed_eight_reducer(self):
        from robot_test_hub.stream_status import RecordedStatusReducer
        for reducer,policy in ((RecordingQualityReducer(),'automatic-reports-9'),(RecordedStatusReducer(),'automatic-reports-8')):
            runner=StreamRunner(self.root,self.db,reducer=reducer,limits={'min_free_bytes':0});self.addCleanup(runner.close)
            with self.subTest(policy=policy),self.assertRaises(ValueError):reports.schedule_stream_reports(self.root,self.db,self.document,runner=runner,pipeline_version=policy)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0],0)

    def test_status_version_mismatch_is_refused_at_admission_and_dispatch(self):
        from robot_test_hub.stream_status import RecordedStatusReducer
        runner=StreamRunner(self.root,self.db,reducer=RecordedStatusReducer(),limits={'min_free_bytes':0});self.addCleanup(runner.close)
        for key in RecordedStatusReducer().context_versions:
            with self.subTest(key=key),self.assertRaises(StreamError):runner.schedule(self.document['runs'][0],{key:'unsupported-version'})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0],0)
        scheduled=runner.schedule(self.document['runs'][0],{})
        context=json.loads(self.db.execute('SELECT context_json FROM analysis_stream_jobs WHERE stream_id=?',(scheduled['stream_id'],)).fetchone()[0])
        for key,value in RecordedStatusReducer().context_versions.items():self.assertEqual(context[key],value)
        context['stream_mapper_version']='unsupported-version'
        with self.db:self.db.execute('UPDATE analysis_stream_jobs SET context_json=? WHERE stream_id=?',(json.dumps(context),scheduled['stream_id']))
        with self.assertRaises(StreamError):self.runner().step(scheduled['stream_id'])
        self.assertEqual(self.db.execute('SELECT chunk_count FROM analysis_stream_jobs WHERE stream_id=?',(scheduled['stream_id'],)).fetchone()[0],0)

    def test_changed_component_version_refuses_saved_work_and_pins_new_identity(self):
        from robot_test_hub import stream_status
        runner=self.runner()
        scheduled=reports.generate(self.root,self.db,self.document,pipeline_version='automatic-reports-9')[0]
        ready=self.drain(runner,scheduled['stream_id'])
        with patch.object(stream_status,'PHOENIX_VERSION','stream-phoenix-status-next'):
            with self.assertRaises(StreamError):runner.step(scheduled['stream_id'])
            with self.assertRaises(ValueError):reports.publish_stream_report(self.db,scheduled['stream_id'],ready['descriptor'])
            fresh=reports.generate(self.root,self.db,self.document,pipeline_version='automatic-reports-9')[0]
            self.assertNotEqual(fresh['stream_id'],scheduled['stream_id'])
            context=json.loads(self.db.execute('SELECT context_json FROM analysis_stream_jobs WHERE stream_id=?',(fresh['stream_id'],)).fetchone()[0])
            self.assertEqual(context['stream_phoenix_status_version'],'stream-phoenix-status-next')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)

    def test_dispatch_rejects_malformed_policy_reducer_pair_without_advancing(self):
        scheduled=reports.generate(self.root,self.db,self.document,pipeline_version='automatic-reports-8')[0]
        row=self.db.execute('SELECT context_json FROM analysis_stream_jobs WHERE stream_id=?',(scheduled['stream_id'],)).fetchone();context=json.loads(row[0]);context['pipeline_version']='automatic-reports-9'
        with self.db:self.db.execute('UPDATE analysis_stream_jobs SET context_json=? WHERE stream_id=?',(json.dumps(context),scheduled['stream_id']))
        before=tuple(self.db.execute('SELECT state,chunk_count,selected_rows FROM analysis_stream_jobs WHERE stream_id=?',(scheduled['stream_id'],)).fetchone())
        with self.assertRaises(StreamError):self.runner().step(scheduled['stream_id'])
        self.assertEqual(tuple(self.db.execute('SELECT state,chunk_count,selected_rows FROM analysis_stream_jobs WHERE stream_id=?',(scheduled['stream_id'],)).fetchone()),before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
    def test_partial_status_backup_fresh_root_replays_to_identical_report(self):
        runner=self.runner(chunk_bytes=4096)
        scheduled=reports.schedule_stream_reports(self.root,self.db,self.document,runner=runner,pipeline_version='automatic-reports-9')[0]
        for _ in range(20000):
            progress=runner.step(scheduled['stream_id'],max_rows=1,max_bytes=4096)
            if progress['chunk_count']>0:break
        else:self.fail('No partial status checkpoint')
        self.assertNotEqual(progress['state'],'ready')
        destination=self.base/'backup';Backup(self.root,destination).create('status-partial')
        recovered=self.base/'recovered';restore(destination/'status-partial',recovered)
        original=self.drain(runner,max_rows=13,max_bytes=8192)
        with closing(open_catalog(recovered)) as db:
            other=self.runner(recovered,db,chunk_bytes=4096)
            resumed=self.drain(other,root=recovered,db=db,max_rows=80,max_bytes=12000)
            self.assertEqual(resumed['descriptor'],original['descriptor']);self.assertEqual(resumed['report'],original['report'])
            other.close()
    def test_new_status_descriptor_cannot_borrow_a_ready_identity_with_changed_results(self):
        runner=self.runner();scheduled=reports.schedule_stream_reports(self.root,self.db,self.document,runner=runner,pipeline_version='automatic-reports-9')[0]
        ready=self.drain(runner,scheduled['stream_id'])
        forged=copy.deepcopy(ready['descriptor']);forged['result']['checks']['system-status-observation']['findings']=[]
        with self.assertRaises(ValueError):reports.publish_stream_report(self.db,scheduled['stream_id'],forged)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM analysis_reports').fetchone()[0],0)
        self.assertIsNotNone(reports.publish_stream_report(self.db,scheduled['stream_id'],ready['descriptor'])['report_id'])


if __name__=='__main__':unittest.main()
