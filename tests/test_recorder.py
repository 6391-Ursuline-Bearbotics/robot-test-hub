"""Deterministic protocol fixtures. These bytes are NOT encoded video or camera qualification."""
from pathlib import Path
import json
import subprocess
import sys
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.recorder import FFmpegAdapter, FFmpegConfig, Recorder, RecordingError, _ns


class ProtocolAdapter:
    qualification = 'synthetic_protocol_only'

    def __init__(self):
        self.folder = None
        self.returncode = None
        self.stops = 0
        self.metadata = {}
        self.rendered = []

    def validate(self):
        return {'adapter': 'deterministic_protocol_fixture', 'qualification': self.qualification}

    def start(self, folder):
        self.folder = folder

    def poll(self):
        return self.returncode

    def stop(self):
        self.stops += 1
        self.returncode = 0

    def probe(self, path):
        return self.metadata[path.name]

    def render(self, source, destination, start_ns, end_ns):
        self.rendered.append((source, start_ns, end_ns))
        frames = [frame for frame in self.metadata[source.name]['frames'] if start_ns <= frame['pts_ns'] < end_ns]
        destination.write_bytes(b'SYNTHETIC PROTOCOL DERIVATIVE; NOT VIDEO')
        self.metadata[destination.name] = {'frames': [dict(frame, pts_ns=frame['pts_ns']-start_ns) for frame in frames],
                                         'codec': 'protocol_fixture', 'time_base': '1/1000000000'}

    def segment(self, number, frames):
        name = f'segment-{number:06d}.mkv'
        (self.folder/name).write_bytes(b'SYNTHETIC PROTOCOL ORIGINAL; NOT VIDEO ' + str(number).encode())
        self.metadata[name] = {'frames': [{'pts_ns': pts, 'duration_ns': duration} for pts,duration in frames],
                               'codec': 'protocol_fixture', 'time_base': '1/1000000000'}
        with (self.folder/'segments.csv').open('a', newline='') as listing:
            listing.write(f'{name},0,1\n')


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = FFmpegConfig(str(self.base/'ffmpeg.exe'), str(self.base/'ffprobe.exe'), 'explicit-pin',
                                   'overview', 'lavfi', 'testsrc=size=32x32:rate=10', 'SYNTHETIC',
                                   segment_seconds=1, stalled_seconds=5, minimum_free_bytes=100)
        self.time = [0.0]
        self.space = [10000]
        self.adapter = ProtocolAdapter()
        self.recorder = Recorder(self.base/'video', self.config, adapter=self.adapter,
                                 monotonic=lambda:self.time[0], clock_ns=lambda:1_790_000_000_000_000_000,
                                 free_bytes=lambda root:self.space[0])
        self.addCleanup(self.recorder.stop)

    def start(self):
        self.recorder.start('practice')

    def progress(self, frame, drop='0'):
        (self.adapter.folder/'progress.txt').write_text(f'frame={frame}\ndrop_frames={drop}\nprogress=continue\n')

    def test_no_implicit_capture_and_provenance_precision(self):
        self.assertIsNone(self.adapter.folder)
        self.assertEqual(self.recorder.health()['state'],'stopped')
        self.start()
        self.assertEqual(self.recorder.health()['state'],'starting')
        session=json.loads((self.adapter.folder/'session.json').read_bytes())
        self.assertEqual(session['started_utc_ns'],'1790000000000000000')
        self.assertEqual(session['source_type'],'SYNTHETIC')
        self.assertIsNone(session['utc_uncertainty_ns'])
        self.assertNotIn('testsrc',json.dumps(session))
        self.progress(3)
        self.assertEqual(self.recorder.tick()['state'],'recording')
        self.assertFalse(self.recorder.health()['live_camera_qualified'])

    def test_real_frame_pts_variable_rate_gaps_and_clip_original_preservation(self):
        self.start()
        frames=[(0,100_000_000),(100_000_000,100_000_000),(400_000_000,150_000_000),(550_000_000,100_000_000)]
        self.adapter.segment(0,frames)
        self.recorder.tick()
        source=self.adapter.folder/'segment-000000.mkv'
        original=source.read_bytes()
        segment=self.recorder.list_segments()[0]
        self.assertEqual(segment['frames'][2]['pts_ns'],'400000000')
        result=self.recorder.preserve('note1',50_000_000,600_000_000,event_id='incident',run_id='run')
        self.assertEqual(result['state'],'partial')
        self.assertEqual(result['unavailable_pts_intervals'],[['200000000','400000000']])
        self.assertEqual(result['spans'][0]['frame_indexes'],[0,1,2,3])
        clipped=self.recorder.render_pin('note1')
        self.assertEqual(clipped['artifacts'][0]['actual_frame_pts_ns'],['0','100000000','400000000','550000000'])
        self.assertEqual(source.read_bytes(),original)
        self.assertEqual(self.recorder.render_pin('note1'),clipped)

    def test_closed_segments_cross_boundaries_and_pin_idempotency(self):
        self.start()
        self.adapter.segment(0,[(0,500_000_000),(500_000_000,500_000_000)])
        self.adapter.segment(1,[(1_000_000_000,500_000_000),(1_500_000_000,500_000_000)])
        self.recorder.tick()
        pin=self.recorder.preserve('run1',750_000_000,1_250_000_000,run_id='run')
        self.assertEqual(pin['state'],'preserved')
        self.assertEqual(len(pin['spans']),2)
        self.assertEqual(self.recorder.preserve('run1',750_000_000,1_250_000_000,run_id='run'),pin)
        with self.assertRaises(RecordingError):
            self.recorder.preserve('run1',0,1_250_000_000,run_id='run')
        self.assertEqual(len(self.recorder.render_pin('run1')['artifacts']),2)

    def test_late_or_open_context_unavailable_and_no_deletion(self):
        self.start()
        self.adapter.segment(0,[(0,1_000_000_000)])
        self.recorder.tick()
        unavailable=self.recorder.preserve('late',2_000_000_000,3_000_000_000)
        self.assertEqual(unavailable['state'],'unavailable')
        with self.assertRaises(RecordingError):
            self.recorder.render_pin('late')
        source=self.adapter.folder/'segment-000000.mkv'
        self.assertTrue(source.is_file())
        source.unlink()  # Explicit fixture loss; recorder never prunes original files.
        self.assertEqual(self.recorder.preserve('lost',0,1_000_000_000)['state'],'unavailable')

    def test_stall_and_stopped_camera_visible_without_sleep(self):
        self.start()
        self.progress(1)
        self.recorder.tick()
        self.time[0]=5
        self.assertEqual(self.recorder.tick()['error_code'],'camera_frames_stalled')
        self.assertEqual(self.adapter.stops,1)
        self.assertTrue(list(self.adapter.folder.glob('failure-*.json')))
        self.adapter.returncode=None
        self.recorder.start('restart')
        self.adapter.returncode=0
        self.assertEqual(self.recorder.tick()['error_code'],'camera_process_stopped')

    def test_storage_failure_and_missing_executable_never_claim_success(self):
        self.start()
        self.space[0]=99
        self.assertEqual(self.recorder.tick()['error_code'],'storage_reserve_exhausted')
        native=Recorder(self.base/'native',self.config,free_bytes=lambda root:10000)
        self.addCleanup(native.stop)
        with self.assertRaises(RecordingError):
            native.start('practice')
        self.assertEqual(native.health()['state'],'error')
        self.assertIsNone(native.owner)

    def test_probe_failure_unknown_duration_and_mutated_original_rejected(self):
        self.start()
        self.adapter.segment(0,[(0,None)])
        self.assertEqual(self.recorder.tick()['error_code'],'recording_artifact_failure')
        self.assertTrue((self.adapter.folder/'segment-000000.mkv').exists())
        self.adapter.returncode=None
        self.recorder.start('next')
        self.adapter.segment(0,[(0,1_000_000_000)])
        self.recorder.tick()
        (self.adapter.folder/'segment-000000.mkv').write_bytes(b'changed')
        with self.assertRaises(RecordingError):
            self.recorder.preserve('bad',0,1_000_000_000)

    def test_list_copy_metadata_immutable_and_traversal_rejected(self):
        self.start()
        self.adapter.segment(0,[(0,1_000_000_000)])
        self.recorder.tick()
        records=self.recorder.list_segments()
        records[0]['frames'][0]['pts_ns']='garbage'
        self.assertEqual(self.recorder.list_segments()[0]['frames'][0]['pts_ns'],'0')
        before=(self.adapter.folder/'segment-000000.mkv.json').read_bytes()
        self.recorder.tick()
        self.assertEqual((self.adapter.folder/'segment-000000.mkv.json').read_bytes(),before)
        for identity in ('../outside','a/b','C:escape'):
            with self.assertRaises(RecordingError):
                self.recorder.preserve(identity,0,1)
        (self.adapter.folder/'segments.csv').write_text('../outside.mkv,0,1\n')
        self.assertEqual(self.recorder.tick()['error_code'],'recording_artifact_failure')

    def test_no_robot_status_dependency_and_stop_finalizes_closed_tail(self):
        self.start()
        self.progress(2)
        self.recorder.tick()
        self.adapter.segment(0,[(0,1_000_000_000)])
        # No network or robot status object is accepted by this independent core.
        self.assertEqual(self.recorder.stop()['state'],'stopped')
        self.assertEqual(len(self.recorder.list_segments()),1)
        self.assertIsNone(self.recorder.owner)

    def test_bounds_types_and_time_units(self):
        self.assertEqual(_ns('0.123456789'),123456789)
        for value in ('NaN','Infinity','invalid'):
            with self.assertRaises(RecordingError):
                _ns(value)
        from dataclasses import replace
        for keyword,value in (('segment_seconds',0),('stalled_seconds',float('nan')),
                              ('minimum_free_bytes',True),('source_type','REAL'),('executable','ffmpeg')):
            with self.assertRaises(RecordingError):
                replace(self.config,**{keyword:value})

    def test_derivative_frame_count_does_not_hide_collapsed_pts_gaps(self):
        self.start()
        self.adapter.segment(0,[(0,100_000_000),(400_000_000,100_000_000)])
        self.recorder.tick()
        self.recorder.preserve('gaps',0,500_000_000)
        original_render=self.adapter.render
        def collapsed(*args):
            original_render(*args)
            self.adapter.metadata[args[1].name]['frames'][1]['pts_ns']=100_000_000
        with patch.object(self.adapter,'render',side_effect=collapsed):
            with self.assertRaisesRegex(RecordingError,'preserve actual frame PTS gaps'):
                self.recorder.render_pin('gaps')
        self.assertFalse((self.adapter.folder/'clips/gaps/manifest.json').exists())
        self.assertTrue((self.adapter.folder/'segment-000000.mkv').exists())

    def test_derivative_records_current_tool_provenance_separately(self):
        self.start()
        capture=self.recorder.provenance
        self.adapter.segment(0,[(0,1_000_000_000)])
        self.recorder.tick()
        self.recorder.preserve('version',0,1_000_000_000)
        current={**capture,'executable_sha256':'a'*64}
        with patch.object(self.adapter,'validate',return_value=current):
            clip=self.recorder.render_pin('version')
        self.assertEqual(clip['provenance'],current)
        self.assertEqual(clip['capture_provenance'],capture)

    def test_full_storage_failure_metadata_does_not_escape_health(self):
        self.start()
        self.space[0]=0
        with patch('robot_test_hub.recorder._publish',side_effect=OSError('fixture disk full')):
            health=self.recorder.tick()
        self.assertEqual(health['state'],'error')
        self.assertEqual(health['error_code'],'storage_reserve_exhausted')
        self.assertIsNone(self.recorder.owner)
        self.assertEqual(self.adapter.stops,1)

    def test_shutdown_timeout_keeps_session_owned_and_error_visible(self):
        self.start()
        with patch.object(self.adapter,'stop',side_effect=subprocess.TimeoutExpired('fixture',1)):
            health=self.recorder.stop()
        self.assertEqual(health['error_code'],'camera_shutdown_failed')
        self.assertTrue(self.recorder.process_started)
        self.assertIsNotNone(self.recorder.owner)
        with self.assertRaises(RecordingError):
            self.recorder.start('overlap')
        self.recorder.stop()
        self.assertIsNone(self.recorder.owner)

    def test_argument_arrays_version_pin_and_native_probe_protocol(self):
        for executable in (self.config.executable,self.config.probe_executable):
            Path(executable).write_bytes(b'protocol executable placeholder')
        adapter=FFmpegAdapter(self.config)
        def version(arguments,**kwargs):
            tool='ffprobe' if 'ffprobe' in arguments[0] else 'ffmpeg'
            self.assertEqual(kwargs['maximum_output_bytes'],self.config.maximum_version_output_bytes)
            return f'{tool} version explicit-pin Copyright\n'.encode()
        with patch.object(adapter,'_run',side_effect=version):
            self.assertEqual(adapter.validate()['version'],'explicit-pin')
        with patch.object(adapter,'_run',return_value=b'ffmpeg version wrong Copyright\n'):
            with self.assertRaises(RecordingError):
                adapter.validate()
        self.start()
        with patch('robot_test_hub.recorder.subprocess.Popen') as popen:
            adapter.start(self.adapter.folder)
            args=popen.call_args.args[0]
            self.assertIsInstance(args,list)
            self.assertIn(self.config.camera_input,args)
            self.assertFalse(popen.call_args.kwargs['shell'])
            self.assertIn('-segment_time',args)
            adapter.stop()
        payload={'streams':[{'codec_name':'h264','time_base':'1/1000'}],
                 'frames':[{'best_effort_timestamp_time':'0.000000001','duration_time':'0.04'}]}
        with patch.object(adapter,'_run',return_value=json.dumps(payload).encode()):
            self.assertEqual(adapter.probe(self.base/'anything')['frames'],[{'pts_ns':1,'duration_ns':40_000_000}])

    def test_native_output_is_bounded_and_overflow_child_is_cancelled(self):
        adapter=FFmpegAdapter(replace(self.config,maximum_probe_output_bytes=1024,operation_timeout=2))
        original_popen=subprocess.Popen
        children=[]
        def capture(*args,**kwargs):
            child=original_popen(*args,**kwargs)
            children.append(child)
            self.assertFalse(kwargs['shell'])
            return child
        script='import sys,time; sys.stdout.buffer.write(b"x"*2000000); sys.stdout.flush(); time.sleep(30)'
        with patch('robot_test_hub.recorder.subprocess.Popen',side_effect=capture):
            with self.assertRaisesRegex(RecordingError,'output limit exceeded'):
                adapter._run([sys.executable,'-c',script])
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertEqual(adapter._run([sys.executable,'-c','print("bounded")']),b'bounded\r\n' if sys.platform=='win32' else b'bounded\n')

    def test_native_timeout_cancels_child_without_unbounded_capture(self):
        adapter=FFmpegAdapter(replace(self.config,operation_timeout=.2))
        original_popen=subprocess.Popen
        children=[]
        def capture(*args,**kwargs):
            child=original_popen(*args,**kwargs);children.append(child);return child
        with patch('robot_test_hub.recorder.subprocess.Popen',side_effect=capture):
            with self.assertRaises(RecordingError):
                adapter._run([sys.executable,'-c','import time; time.sleep(30)'])
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)

    def test_probe_output_overflow_stops_capture_and_preserves_original(self):
        self.start()
        self.adapter.segment(0,[(0,1_000_000_000)])
        native=FFmpegAdapter(replace(self.config,maximum_probe_output_bytes=1024,operation_timeout=2))
        def overflowing_probe(path):
            native._run([sys.executable,'-c',
                'import sys,time; sys.stdout.buffer.write(b"x"*2000000); sys.stdout.flush(); time.sleep(30)'])
        with patch.object(self.adapter,'probe',side_effect=overflowing_probe):
            health=self.recorder.tick()
        self.assertEqual(health['error_code'],'recording_artifact_failure')
        self.assertFalse(self.recorder.process_started)
        self.assertIsNone(self.recorder.owner)
        self.assertTrue((self.adapter.folder/'segment-000000.mkv').exists())
        self.assertFalse((self.adapter.folder/'segment-000000.mkv.json').exists())

    def test_frame_and_log_limits_stop_capture_preserving_originals(self):
        self.start()
        self.adapter.segment(0,[(0,500_000_000),(500_000_000,500_000_000)])
        self.recorder.config=replace(self.config,maximum_frames_per_segment=1)
        self.assertEqual(self.recorder.tick()['error_code'],'recording_artifact_failure')
        self.assertTrue((self.adapter.folder/'segment-000000.mkv').exists())
        for name in ('progress.txt','native-errors.log'):
            self.recorder.config=replace(self.config,maximum_progress_bytes=16,maximum_error_log_bytes=16)
            self.adapter.returncode=None
            self.recorder.start('log-limit')
            self.adapter.segment(0,[(0,1_000_000_000)])
            (self.adapter.folder/name).write_bytes(b'x'*17)
            self.assertEqual(self.recorder.tick()['error_code'],'recording_log_size_limit_exceeded')
            self.assertFalse(self.recorder.process_started)
            self.assertTrue((self.adapter.folder/'segment-000000.mkv').exists())

    def test_probe_frame_limit_and_invalid_resource_limits(self):
        adapter=FFmpegAdapter(replace(self.config,maximum_frames_per_segment=1))
        payload={'frames':[{},{}]}
        with patch.object(adapter,'_run',return_value=json.dumps(payload).encode()):
            with self.assertRaises(RecordingError):
                adapter.probe(self.base/'original')
        for name in ('maximum_probe_output_bytes','maximum_version_output_bytes',
                     'maximum_frames_per_segment','maximum_progress_bytes','maximum_error_log_bytes'):
            for value in (0,-1,True,256*1024*1024+1):
                with self.assertRaises(RecordingError):
                    replace(self.config,**{name:value})

    def test_auxiliary_cleanup_retains_hub_owner_until_child_wait_and_pipe_close_finish(self):
        from robot_test_hub.config import Config
        from robot_test_hub.demo import DemoSource
        from robot_test_hub.service import HubService
        import threading
        import time
        entered=threading.Event()
        release=threading.Event()
        original_popen=subprocess.Popen
        children=[]
        phases=[]
        def capture(*args,**kwargs):
            child=original_popen(*args,**kwargs)
            children.append(child)
            kill,wait,close=child.kill,child.wait,child.stdout.close
            failures={'wait':True,'close':True}
            def delayed_kill():
                entered.set()
                if not release.is_set():
                    raise OSError('Private fixture endpoint must not escape')
                phases.append('kill')
                return kill()
            def retry_wait(*args,**kwargs):
                if release.is_set() and failures['wait']:
                    failures['wait']=False
                    phases.append('wait_retry')
                    raise subprocess.TimeoutExpired('private fixture',.01)
                return wait(*args,**kwargs)
            def retry_close():
                if failures['close']:
                    failures['close']=False
                    phases.append('close_retry')
                    raise OSError('Private fixture pipe-close failure')
                return close()
            child.kill,child.wait,child.stdout.close=delayed_kill,retry_wait,retry_close
            return child
        config=replace(self.config,operation_timeout=.1)
        class AuxiliaryAdapter(FFmpegAdapter):
            def validate(adapter):
                adapter._run([sys.executable,'-c','import time; time.sleep(30)'])
        def factory(root,config):
            return Recorder(root,config,adapter=AuxiliaryAdapter(config))
        with patch('robot_test_hub.recorder.subprocess.Popen',side_effect=capture):
            service=HubService(Config(data_dir=str(self.base/'hub-owner'),shutdown_timeout=.05),
                               DemoSource(),video_config=config,recorder_factory=factory)
            try:
                service.start()
                self.assertTrue(entered.wait(2),'Auxiliary cleanup did not begin')
                service.set_paused(True)
                self.assertTrue(service.snapshot()['paused_by_operator'])
                self.assertFalse(service.close())
                self.assertFalse(service.owner.file.closed)
                self.assertTrue(any(thread.is_alive() and thread.name=='hub-video' for thread in service.threads))
                self.assertEqual(len(children),1)
            finally:
                release.set()
                deadline=time.monotonic()+3
                while any(thread.is_alive() for thread in service.threads) and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(service.close())
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=2)
            self.assertEqual(len(children),1)
            self.assertIsNotNone(children[0].poll())
            self.assertTrue(children[0].stdout.closed)
            self.assertIn('wait_retry',phases)
            self.assertIn('close_retry',phases)
            self.assertNotIn('Private',json.dumps(service.video.snapshot()))


if __name__=='__main__':
    unittest.main()
