"""Bounded local stream scheduling without accelerating periodic maintenance."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from robot_test_hub.collector import RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.demo import OfflineSource
from robot_test_hub.service import HubService
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.storage import open_catalog
from robot_test_hub.importer import Importer
from robot_test_hub.selftest import _sample

class Clock:
    now=0.
    def __call__(self):return self.now

class Stop:
    def __init__(self,clock,end):self.clock=clock;self.end=end;self.flag=False;self.waits=[]
    def is_set(self):return self.flag or self.clock.now>=self.end
    def set(self):self.flag=True
    def wait(self,delay):
        self.waits.append(delay);self.clock.now+=delay
        return self.is_set()

class StreamSchedulingTests(unittest.TestCase):
    def service(self,source=None):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        service=HubService(Config(data_dir=temp.name,stream_min_free_bytes=0),source or OfflineSource())
        self.addCleanup(service.close);return service

    def test_pacing_active_cooperative_duty_and_terminal_baseline(self):
        for state in ('pending','verifying','running','ready'):
            self.assertAlmostEqual(HubService._stream_wait({'state':state},.1,True),.3)
            self.assertEqual(HubService._stream_wait({'state':state},0.,True),.025)
            self.assertAlmostEqual(HubService._stream_wait({'state':state},.314,True),.942)
            self.assertEqual(HubService._stream_wait({'state':state},.1,False),.5)
        for state in ('idle','failed','canceled','stopped'):
            self.assertEqual(HubService._stream_wait({'state':state},.001,True),.5)

    def test_status_accessor_never_dispatches_remote_and_explicit_offline_only(self):
        class UntrustedLabel:
            source_type='offline_import_only'
            def status(self):raise AssertionError('remote status called by scheduler')
        service=self.service(UntrustedLabel())
        for value,allowed in ((RobotStatus(False,10.,'boot',1),True),(RobotStatus(True,10.,'boot',1),False),
            (RobotStatus(None,10.,'boot',1),False),(RobotStatus(False,8.,'boot',1),False),(RobotStatus(False,11.,'boot',1),False)):
            service.cached_source.latest=value
            self.assertEqual(service._stream_acceleration_allowed(10.),allowed)
        service.paused.set();self.assertFalse(service._stream_acceleration_allowed(10.))
        service.paused.clear();service.source=OfflineSource();self.assertTrue(service._stream_acceleration_allowed(10.))
        service.stop.set();self.assertFalse(service._stream_acceleration_allowed(10.))

    def run_loop(self,*,maintenance_seconds=0.,progress='running',end=1.55):
        service=self.service();clock=Clock();service.stop=Stop(clock,end);maintained=[];advanced=[]
        class LocalPipeline:
            def __init__(self,*args,**kwargs):
                self_options.update(kwargs)
            def tick(self):maintained.append(clock.now);clock.now+=maintenance_seconds
            def advance_stream(self):advanced.append(clock.now);clock.now+=.01;return {'state':progress}
            def close(self):return True
        self_options={}
        with patch('robot_test_hub.pipeline.Pipeline',LocalPipeline),patch('robot_test_hub.service.time.monotonic',clock):service._pipeline_worker()
        return maintained,advanced,service.stop.waits,self_options

    def test_active_work_advances_without_repeating_maintenance(self):
        maintained,advanced,waits,options=self.run_loop()
        self.assertFalse(options['stream_on_tick'])
        self.assertEqual(len(maintained),4)
        self.assertGreater(len(advanced),25)
        self.assertTrue(all(b-a>=.5-1e-10 for a,b in zip(maintained,maintained[1:])))
        self.assertTrue(all(delay>0 for delay in waits))

    def test_delayed_maintenance_is_coalesced_and_idle_does_not_spin(self):
        maintained,advanced,waits,_=self.run_loop(maintenance_seconds=1.3,end=2.)
        self.assertEqual(len(maintained),2);self.assertGreaterEqual(maintained[1]-maintained[0],1.8-1e-10)
        maintained,advanced,waits,_=self.run_loop(progress='idle')
        self.assertLessEqual(len(advanced),4)
        self.assertTrue(all(b-a>=.5 for a,b in zip(advanced,advanced[1:])))

    def test_split_pipeline_preserves_default_saved_reports(self):
        reports=[]
        for separate in (False,True):
            temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
            db=open_catalog(root)
            try:
                source=root/'invented.wpilog';source.write_bytes(_sample())
                self.assertEqual(Importer(root,db).import_file(source,source_type='SYNTHETIC')['state'],'succeeded')
                pipeline=Pipeline(root,db,stream_on_tick=not separate)
                try:
                    pipeline.tick()
                    if separate:self.assertEqual(pipeline.advance_stream()['state'],'idle')
                    reports.append([tuple(row) for row in db.execute('SELECT report_id,result_json FROM analysis_reports ORDER BY report_id')])
                finally:pipeline.close()
            finally:db.close()
        self.assertEqual(reports[0],reports[1])

if __name__=='__main__':unittest.main()
