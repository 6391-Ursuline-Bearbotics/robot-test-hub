"""Video worker/service tests; protocol bytes are explicitly not encoded video."""
from dataclasses import asdict,replace
import http.client
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import patch

from robot_test_hub.collector import RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.recorder import FFmpegConfig, Recorder, RecordingError
from robot_test_hub.recorder_service import RecorderWorker,load_video_config
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from test_recorder import ProtocolAdapter


class AutomaticProtocolAdapter(ProtocolAdapter):
    def start(self,folder):
        super().start(folder)
        self.segment(0,[(0,100_000_000),(100_000_000,100_000_000)])
        (folder/'progress.txt').write_text('frame=2\ndrop_frames=0\nprogress=continue\n')


class RecorderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.config=FFmpegConfig(str(self.root/'ffmpeg.exe'),str(self.root/'ffprobe.exe'),
            'explicit-pin','overview','rtsp','rtsp://private-user:private-password@private-camera/live',
            'REAL',segment_seconds=1,minimum_free_bytes=0)

    def factory(self,adapter=None,**options):
        adapter=adapter or AutomaticProtocolAdapter()
        def construct(root,config):
            return Recorder(root,config,adapter=adapter,**options)
        return construct

    def wait_for(self,predicate,timeout=3):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail('Video worker condition not reached')

    def service(self,**changes):
        config=changes.pop('hub_config',Config(data_dir=str(self.root/'hub')))
        service=HubService(config,changes.pop('source',DemoSource()),**changes)
        self.addCleanup(service.close)
        return service

    def test_default_disables_capture_and_never_calls_factory(self):
        def forbidden(*args):
            self.fail('No opt-in video config, capture must not start')
        service=self.service(recorder_factory=forbidden)
        service.start()
        self.wait_for(lambda:service.video.snapshot()['recovery']['state']=='complete')
        self.assertFalse(service.video.snapshot()['enabled'])
        self.assertEqual(service.snapshot()['video']['state'],'disabled')
        self.assertFalse((service.root/'video').exists())
        self.assertTrue(service.close())

    def test_camera_continues_despite_enabled_robot_status_failure_and_collection_pause(self):
        class Source:
            broken=False
            def status(inner):
                if inner.broken:
                    raise OSError('private camera credential must never appear')
                return RobotStatus(True,time.monotonic(),'boot',1,False)
            def list_closed_files(inner):
                raise AssertionError('Enabled robot cannot bulk-read')
        source=Source()
        adapter=AutomaticProtocolAdapter()
        service=self.service(source=source,video_config=self.config,recorder_factory=self.factory(adapter))
        service.start()
        self.wait_for(lambda:service.video.snapshot()['state']=='recording')
        source.broken=True
        service.set_paused(True)
        self.wait_for(lambda:service.snapshot()['workers']['status']['state']=='failed')
        (adapter.folder/'progress.txt').write_text('frame=3\nprogress=continue\n')
        self.wait_for(lambda:service.video.snapshot()['frames']==3)
        self.assertEqual(service.video.snapshot()['state'],'recording')
        self.assertEqual(service.video.snapshot()['closed_segments'],1)
        self.assertTrue(service.close())
        self.assertEqual(service.video.snapshot()['state'],'stopped')
        self.assertTrue((adapter.folder/'capture-lifecycle.json').exists())

    def test_failure_and_low_space_health_are_redacted_and_do_not_stop_hub(self):
        def broken(*args):
            raise OSError(self.config.camera_input)
        service=self.service(video_config=self.config,recorder_factory=broken)
        service.start()
        self.wait_for(lambda:service.video.snapshot()['state']=='error')
        self.assertEqual(service.video.snapshot()['error_code'],'video_worker_failed')
        self.assertFalse(service.stop.is_set())
        serialized=json.dumps(service.snapshot())+json.dumps(service.diagnostics.snapshot())
        self.assertNotIn('private-password',serialized)
        self.assertNotIn('private-camera',serialized)
        self.assertNotIn('camera_input',json.dumps(service.config.as_dict()))
        self.assertTrue(service.close())
        stopping=threading.Event()
        worker=RecorderWorker(self.root/'space',self.config,recorder_factory=self.factory(
            AutomaticProtocolAdapter(),free_bytes=lambda _:0))
        worker.config=replace(self.config,minimum_free_bytes=1)
        worker.run(stopping)
        self.assertEqual(worker.snapshot()['state'],'error')
        self.assertFalse(worker.recorder.process_started)

    def test_shutdown_retains_hub_ownership_until_camera_cleanup_succeeds(self):
        adapter=AutomaticProtocolAdapter()
        allow_stop=threading.Event()
        original_stop=adapter.stop
        def held_stop():
            if not allow_stop.is_set():
                raise RecordingError('Synthetic shutdown blocked')
            original_stop()
        adapter.stop=held_stop
        service=self.service(hub_config=Config(data_dir=str(self.root/'hub'),shutdown_timeout=.15),
            video_config=self.config,recorder_factory=self.factory(adapter))
        self.addCleanup(allow_stop.set)
        service.start()
        self.wait_for(lambda:service.video.snapshot()['state']=='recording')
        self.assertFalse(service.close())
        self.assertFalse(service.closed)
        self.assertIsNotNone(service.video.recorder.owner)
        allow_stop.set()
        self.wait_for(lambda:not any(thread.is_alive() for thread in service.threads))
        self.assertTrue(service.close())

    def test_restart_recovers_closed_history_and_preserves_unfinished_and_corrupt_evidence(self):
        adapter=AutomaticProtocolAdapter()
        service=self.service(video_config=self.config,recorder_factory=self.factory(adapter))
        service.start()
        self.wait_for(lambda:service.video.snapshot()['closed_segments']==1)
        self.assertTrue(service.close())
        original=(adapter.folder/'segment-000000.mkv').read_bytes()
        orphan=adapter.folder/'segment-000001.mkv'
        orphan.write_bytes(b'UNFINISHED PROTOCOL ORIGINAL')
        worker=RecorderWorker(service.root)
        worker.recover()
        page=worker.segment_page()
        self.assertEqual(len(page['items']),1)
        self.assertEqual(page['items'][0]['capture_state'],'unfinished_preserved')
        self.assertEqual(page['recovery']['unverified_files'],1)
        self.assertEqual(page['recovery']['unfinished_captures'],1)
        self.assertEqual((adapter.folder/'segment-000000.mkv').read_bytes(),original)
        self.assertEqual(orphan.read_bytes(),b'UNFINISHED PROTOCOL ORIGINAL')
        (adapter.folder/'segment-000000.mkv').write_bytes(b'CORRUPTED ORIGINAL PRESERVED')
        worker.recover()
        self.assertEqual(worker.segment_page()['items'],[])
        self.assertGreater(worker.snapshot()['recovery']['errors'],0)
        self.assertTrue(orphan.exists())

    def test_recovered_graceful_lifecycle_and_strict_duration_metadata(self):
        service=self.service(video_config=self.config,recorder_factory=self.factory())
        service.start()
        self.wait_for(lambda:service.video.snapshot()['closed_segments']==1)
        service.close()
        worker=RecorderWorker(service.root)
        worker.recover()
        self.assertEqual(worker.segment_page()['items'][0]['capture_state'],'stopped')
        segment=service.video.recorder.list_segments()[0]
        for change in ({'schema_version':True},{'end_pts_ns':'999999999'},
                       {'start_pts_ns':'00'},{'end_pts_ns':str(1<<63)}):
            with self.assertRaises(RecordingError):
                worker._summary({**segment,**change})
        for duration in (None,'0','-1','01'):
            bad=json.loads(json.dumps(segment));bad['frames'][-1]['duration_ns']=duration
            with self.assertRaises(RecordingError):
                worker._summary(bad)
        overlap=json.loads(json.dumps(segment));overlap['frames'][0]['duration_ns']='150000000'
        with self.assertRaisesRegex(RecordingError,'overlap'):
            worker._summary(overlap)
        for interval in (0,-1,float('nan'),True):
            with self.assertRaises(RecordingError):
                RecorderWorker(service.root,tick_interval=interval)

    def test_missing_lifecycle_receipt_or_empty_interrupted_capture_is_explicit(self):
        service=self.service(video_config=self.config,recorder_factory=self.factory())
        service.start()
        self.wait_for(lambda:service.video.snapshot()['closed_segments']==1)
        self.assertTrue(service.close())
        capture=service.video.recorder.folder
        (capture/'capture-lifecycle.json').unlink()
        empty=capture.parent/'interrupted-empty'
        empty.mkdir();(empty/'session.json').write_text('{}')
        worker=RecorderWorker(service.root)
        worker.recover()
        self.assertEqual(worker.snapshot()['recovery']['state'],'partial')
        self.assertEqual(worker.snapshot()['recovery']['unfinished_captures'],2)
        self.assertEqual(worker.segment_page()['items'][0]['capture_state'],'historical_lifecycle_unknown')

    def test_readonly_http_health_segments_bounded_queries_and_no_private_config(self):
        service=self.service(video_config=self.config,recorder_factory=self.factory())
        service.start()
        self.wait_for(lambda:service.video.snapshot()['closed_segments']==1)
        httpd=create_http_server(service,service.source,0)
        thread=threading.Thread(target=httpd.serve_forever,kwargs={'poll_interval':.01});thread.start()
        def get(path):
            connection=http.client.HTTPConnection('127.0.0.1',httpd.server_address[1],timeout=2)
            try:
                connection.request('GET',path)
                response=connection.getresponse()
                return response.status,json.loads(response.read())
            finally:
                connection.close()
        try:
            status,health=get('/api/v1/video')
            self.assertEqual(status,200)
            self.assertEqual(health['backup_state'],'not_backed_up')
            status,page=get('/api/v1/video/segments?limit=1')
            self.assertEqual(status,200)
            self.assertEqual(len(page['items']),1)
            self.assertNotIn('frames',page['items'][0])
            self.assertNotIn('relative_path',page['items'][0])
            serialized=json.dumps(health)+json.dumps(page)
            for private in ('private-password','private-camera','rtsp://','camera_input',str(self.root)):
                self.assertNotIn(private,serialized)
            for query in ('?limit=0','?limit=101','?limit=1&limit=2','?cursor=../../private','?path=private'):
                self.assertEqual(get('/api/v1/video/segments'+query)[0],400)
            self.assertEqual(get('/api/v1/video?input=private')[0],400)
        finally:
            httpd.shutdown();thread.join();httpd.server_close()

    def test_explicit_private_config_strict_schema_bounds_and_redacted_failures(self):
        path=self.root/'private-video.json'
        path.write_text(json.dumps({'schema_version':1,**asdict(self.config)}))
        self.assertEqual(load_video_config(path),self.config)
        for value in ({'schema_version':1,'camera_input':'private-password'},
                      {'schema_version':True,**asdict(self.config)},
                      {'schema_version':1,**asdict(self.config),'unknown':'private-password'}):
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(RecordingError,'Invalid private video configuration') as failure:
                load_video_config(path)
            self.assertNotIn('private-password',str(failure.exception))
        from robot_test_hub.server import main
        output=io.StringIO()
        with patch.object(sys,'argv',['hub','--video-config',str(path)]),contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as failure:
                main()
        self.assertEqual(failure.exception.code,2)
        self.assertIn('Invalid private video configuration',output.getvalue())
        self.assertNotIn('private-password',output.getvalue())

    def test_stall_and_probe_failure_visible_without_waiting_for_camera_time(self):
        for code in ('camera_frames_stalled','recording_artifact_failure'):
            clock=[0.]
            adapter=ProtocolAdapter() if code=='camera_frames_stalled' else AutomaticProtocolAdapter()
            if code=='recording_artifact_failure':
                def failing_probe(path):
                    raise RecordingError('private-password')
                adapter.probe=failing_probe
            def factory(root,config):
                recorder=Recorder(root,config,adapter=adapter,monotonic=lambda:clock[0])
                original_tick=recorder.tick
                def tick():
                    clock[0]=16.
                    return original_tick()
                recorder.tick=tick
                return recorder
            worker=RecorderWorker(self.root/code,self.config,recorder_factory=factory)
            worker.run(threading.Event())
            self.assertEqual(worker.snapshot()['state'],'error')
            self.assertEqual(worker.snapshot()['error_code'],code)
            self.assertFalse(worker.recorder.process_started)
            self.assertNotIn('private-password',json.dumps(worker.snapshot()))

    def test_segment_cursor_projection_is_bounded_and_stable(self):
        service=self.service(video_config=self.config,recorder_factory=self.factory())
        service.start()
        self.wait_for(lambda:service.video.snapshot()['closed_segments']==1)
        service.close()
        worker=service.video
        first=next(iter(worker.records.values()))
        worker.records[first['segment_id']+'x']={**first,'segment_id':first['segment_id']+'x'}
        one=worker.segment_page(limit=1)
        self.assertEqual(len(one['items']),1)
        self.assertIsNotNone(one['next_cursor'])
        two=worker.segment_page(limit=1,cursor=one['next_cursor'])
        self.assertEqual(two['items'][0]['segment_id'],first['segment_id']+'x')
        self.assertIsNone(two['next_cursor'])

    def test_uncertainty_is_canonical_nonnegative_signed64(self):
        service=self.service(video_config=self.config,recorder_factory=self.factory())
        service.start()
        self.wait_for(lambda:service.video.snapshot()['closed_segments']==1)
        self.assertTrue(service.close())
        segment=service.video.recorder.list_segments()[0]
        for value in ('01','-1',str(1<<63),True):
            with self.subTest(value=value),self.assertRaises(RecordingError):
                service.video._summary({**segment,'utc_uncertainty_ns':value})
        self.assertEqual(service.video._summary({**segment,'utc_uncertainty_ns':'0'})['utc_uncertainty_ns'],'0')

    def test_cached_frame_age_advances_while_worker_cannot_publish(self):
        now=[100.]
        worker=RecorderWorker(self.root,clock=lambda:now[0])
        worker._publish({'state':'recording','error_code':None,'frames':2,'dropped_frames':0,
                         'last_frame_age_seconds':1.,'closed_segments':0})
        now[0]=120.
        value=worker.snapshot()
        self.assertEqual(value['health_age_seconds'],20.)
        self.assertEqual(value['last_frame_age_seconds'],21.)
        self.assertEqual(worker.health['last_frame_age_seconds'],1.)

    def test_format_projection_rejects_paths_and_invalid_time_base(self):
        service=self.service(video_config=self.config,recorder_factory=self.factory())
        service.start()
        self.wait_for(lambda:service.video.snapshot()['closed_segments']==1)
        self.assertTrue(service.close())
        segment=service.video.recorder.list_segments()[0]
        changes=({'container':'private/path'},{'codec':'private/path'},
                 {'utc_basis':'private/path'},{'time_base':'private/path'},
                 {'time_base':'1/0'},{'time_base':'01/1000'},{'time_base':f'1/{1<<63}'})
        for change in changes:
            with self.subTest(change=change),self.assertRaises(RecordingError):
                service.video._summary({**segment,**change})


if __name__=='__main__':
    unittest.main()
