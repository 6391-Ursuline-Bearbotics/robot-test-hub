"""Independent fake-clock service scheduling and unchanged report contracts."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.collector import RobotStatus
from robot_test_hub.demo import OfflineSource
from robot_test_hub.service import HubService


class Clock:
    def __init__(self):self.now=0.
    def __call__(self):return self.now


class Stop:
    def __init__(self,clock,until,hook=None):self.clock,self.until,self.hook=clock,until,hook;self.flag=False;self.waits=[]
    def is_set(self):return self.flag
    def set(self):self.flag=True
    def wait(self,seconds):
        if seconds<0:raise AssertionError('negative wait')
        self.waits.append((self.clock.now,seconds))
        if len(self.waits)>1000:raise AssertionError('worker spun instead of waiting')
        if not self.flag:self.clock.now+=min(seconds,max(0.,self.until-self.clock.now))
        if self.hook:self.hook(self)
        if self.clock.now>=self.until:self.flag=True
        return self.flag


class SchedulingReviewTests(unittest.TestCase):
    def service(self,clock,source=None):
        service=HubService.__new__(HubService)
        service.source=source if source is not None else OfflineSource()
        service.config=SimpleNamespace(freshness=1.,stream_max_spool_bytes=64*1024**2,stream_min_free_bytes=0)
        service.root=Path('unused-independent-no-io')
        service.paused=threading.Event();service.stop=Stop(clock,10.)
        service.status=RobotStatus(None,0.,'unknown',0)
        service.cached_source=SimpleNamespace(status=lambda:service.status)
        service.health={};service.cache_lock=threading.Lock();service.errors=[]
        service._health=lambda worker,state,code=None:service.health.update({worker:{'state':state,'error_code':code}})
        service.diagnostics=SimpleNamespace(record=lambda *args,**kwargs:service.errors.append(args))
        return service

    def worker(self,*,cost=.01,maintenance_cost=0.,until=1.2,progress='running',transition=None,cleanup_retries=0,source=None):
        from robot_test_hub import service as module
        clock=Clock();service=self.service(clock,source)
        calls={name:[] for name in ('folder','pull','maintenance','stream','close','db_close','sleep')}
        def hook(stop):
            if transition and len(stop.waits)==1:transition(service,clock)
        service.stop=Stop(clock,until,hook)
        service._run_folder_import=lambda db:calls['folder'].append(clock.now)
        service._run_pull=lambda db:calls['pull'].append(clock.now)
        db=SimpleNamespace(close=lambda:calls['db_close'].append(clock.now))
        class Pipeline:
            def __init__(self,root,db,**options):
                self.options=options;self.attempts=0
                if options.get('stream_on_tick') is not False:raise AssertionError('service maintenance advances streams twice')
            def tick(self):calls['maintenance'].append(clock.now);clock.now+=maintenance_cost
            def advance_stream(self):
                calls['stream'].append(clock.now);clock.now+=cost
                return {'state':progress}
            def close(self):
                self.attempts+=1;calls['close'].append(clock.now)
                if self.attempts<=cleanup_retries:
                    if calls['db_close']:raise AssertionError('database closed while stream cleanup pending')
                    return False
                return True
        def sleep(seconds):calls['sleep'].append(seconds);clock.now+=seconds
        with patch('robot_test_hub.pipeline.Pipeline',Pipeline),patch.object(module,'open_catalog',return_value=db),\
             patch.object(module.time,'monotonic',clock),patch.object(module.time,'sleep',side_effect=sleep):
            service._pipeline_worker()
        self.assertEqual(service.errors,[] if not cleanup_retries else [('stream_cleanup_pending','Streaming resources are still closing; archive ownership is retained')])
        return calls,service,clock

    def test_only_explicit_offline_or_fresh_disabled_cached_authority_accelerates(self):
        clock=Clock();clock.now=10.
        offline=self.service(clock)
        self.assertTrue(offline._stream_acceleration_allowed(clock()))
        offline.paused.set();self.assertFalse(offline._stream_acceleration_allowed(clock()))
        spoof=self.service(clock,SimpleNamespace(source_type='offline_import_only'))
        self.assertFalse(spoof._stream_acceleration_allowed(clock()))
        source=SimpleNamespace(source_type='real',status=lambda:self.fail('scheduler made a robot status request'))
        service=self.service(clock,source)
        for enabled,observed,expected in [(False,10.,True),(True,10.,False),(None,10.,False),
                                           (False,8.,False),(False,11.,False),(False,float('nan'),False)]:
            with self.subTest(enabled=enabled,observed=observed):
                service.status=RobotStatus(enabled,observed,'invented-boot',1)
                self.assertEqual(service._stream_acceleration_allowed(clock()),expected)

    def test_stream_duty_cooldown_keeps_maintenance_on_its_own_deadline(self):
        calls,service,clock=self.worker(cost=.2,until=2.)
        self.assertEqual(len(calls['stream']),3)
        for actual,expected in zip(calls['stream'],(0.,.8,1.6)):self.assertAlmostEqual(actual,expected)
        for actual,expected in zip(calls['maintenance'],(0.,.5,1.,1.5)):self.assertAlmostEqual(actual,expected)
        self.assertEqual(calls['folder'],calls['maintenance']);self.assertEqual(calls['pull'],calls['maintenance'])
        for left,right in zip(calls['stream'],calls['stream'][1:]):self.assertGreaterEqual(right-left+.0000001,4*.2)

    def test_fast_turns_do_not_accelerate_import_pull_or_catalog_maintenance(self):
        calls,_,_=self.worker(cost=.001,until=1.1)
        self.assertGreater(len(calls['stream']),30)
        self.assertEqual(len(calls['maintenance']),3)
        self.assertEqual(calls['folder'],calls['maintenance']);self.assertEqual(calls['pull'],calls['maintenance'])
        for left,right in zip(calls['stream'],calls['stream'][1:]):self.assertGreaterEqual(right-left+.0000001,.026)

    def test_enabled_stale_and_operator_pause_revoke_an_already_fast_deadline(self):
        for transition in (lambda s,c:setattr(s,'status',RobotStatus(True,c(),'invented',1)),
                           lambda s,c:setattr(s,'status',RobotStatus(False,c()-2.,'invented',1)),
                           lambda s,c:s.paused.set()):
            with self.subTest(transition=transition):
                # A live source starts fresh-disabled, then changes in first wait.
                source=SimpleNamespace(source_type='real')
                def update(s,c):transition(s,c)
                original=self.service
                def initialized(clock,source=None):
                    value=original(clock,source);value.status=RobotStatus(False,0.,'invented',1);return value
                with patch.object(self,'service',side_effect=initialized):calls,_,_=self.worker(source=source,transition=update,until=.8)
                self.assertEqual(len(calls['stream']),2)
                self.assertAlmostEqual(calls['stream'][1],.51)

    def test_idle_and_terminal_progress_never_fast_spin(self):
        for state in ('idle','failed','canceled'):
            with self.subTest(state=state):
                calls,_,_=self.worker(progress=state,until=1.1)
                self.assertEqual(len(calls['stream']),3)
                for left,right in zip(calls['stream'],calls['stream'][1:]):self.assertGreaterEqual(right-left,.5)

    def test_maintenance_overrun_coalesces_and_shutdown_interrupts_wait(self):
        calls,_,_=self.worker(maintenance_cost=.7,cost=.01,until=2.)
        self.assertEqual(len(calls['maintenance']),2)
        self.assertAlmostEqual(calls['maintenance'][1],1.2)
        calls,service,clock=self.worker(until=.02,cleanup_retries=2)
        self.assertEqual(len(calls['stream']),1)
        self.assertEqual(calls['sleep'],[.05,.05])
        self.assertEqual(len(calls['close']),3);self.assertEqual(len(calls['db_close']),1)
        self.assertEqual(calls['db_close'][0],calls['close'][-1])


class PipelineSchedulingReviewTests(unittest.TestCase):
    def test_plain_tick_keeps_its_default_single_stream_turn(self):
        from robot_test_hub.pipeline import Pipeline
        from robot_test_hub.storage import open_catalog
        with tempfile.TemporaryDirectory(prefix='independent pipeline tick contract ') as folder:
            root=Path(folder)
            with closing(open_catalog(root)) as db:
                default=Pipeline(root,db)
                with patch.object(default,'advance_stream',return_value={'state':'idle'}) as advance:
                    default.tick();advance.assert_called_once_with()
                self.assertTrue(default.close())
                separated=Pipeline(root,db,stream_on_tick=False)
                with patch.object(separated,'advance_stream',return_value={'state':'idle'}) as advance:
                    separated.tick();advance.assert_not_called()
                    separated.advance_stream();advance.assert_called_once_with()
                self.assertTrue(separated.close())

    def test_separate_stream_turns_keep_whole_report_bytes_and_identities(self):
        from robot_test_hub.pipeline import Pipeline
        from robot_test_hub.importer import Importer
        from robot_test_hub.storage import open_catalog
        from robot_test_hub import stream_analysis,reports
        from robot_test_hub.runs import RunCatalog
        original=(Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog').read_bytes();outputs=[]
        for separated in (False,True):
            with self.subTest(separated=separated),tempfile.TemporaryDirectory(prefix='independent scheduled report identity ') as folder:
                root=Path(folder);source=root/'invented.wpilog';source.write_bytes(original)
                with closing(open_catalog(root)) as db:
                    imported=Importer(root,db).import_file(source,source_type='SYNTHETIC');self.assertEqual(imported['state'],'succeeded')
                    pipeline=Pipeline(root,db,stream_on_tick=not separated,stream_limits={'min_free_bytes':0,'chunk_bytes':4096})
                    try:
                        with patch.object(stream_analysis.time,'monotonic',return_value=0):pipeline.tick()
                        pipeline.advance_stream()  # Initialize the lazy runner on both paths.
                        reports.schedule_stream_reports(root,db,RunCatalog(db).current(),runner=pipeline.stream_runner,
                            pipeline_version=reports.DEFAULT_STREAM_VERSION)
                        self.assertGreater(db.execute('SELECT COUNT(*) FROM analysis_stream_jobs').fetchone()[0],0)
                        for turn in range(10000):
                            with patch.object(stream_analysis.time,'monotonic',return_value=0):
                                if separated:
                                    pipeline.advance_stream()
                                    if turn%7==0:pipeline.tick()
                                else:pipeline.tick()
                            pending=db.execute("SELECT COUNT(*) FROM analysis_stream_jobs WHERE report_id IS NULL AND state NOT IN ('failed','canceled')").fetchone()[0]
                            if not pending:break
                        else:self.fail('pipeline did not finish all reports')
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_stream_jobs WHERE state IN ('failed','canceled')").fetchone()[0],0)
                        outputs.append([tuple(row) for row in db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id')])
                    finally:self.assertTrue(pipeline.close())
                self.assertEqual(source.read_bytes(),original)
        self.assertTrue(outputs[0]);self.assertEqual(outputs[0],outputs[1])


if __name__=='__main__':unittest.main()
