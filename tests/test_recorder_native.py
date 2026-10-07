"""Opt-in generated-media qualification for explicitly supplied FFmpeg binaries.

No camera/device/network source is used. Generated artifacts stay in a temporary
directory. This qualifies the executable hashes reported by the test, not live
camera timing, UTC synchronization, or other FFmpeg distributions.
"""
from pathlib import Path
import hashlib
import json
import os
import tempfile
import time
import unittest

from robot_test_hub.recorder import FFmpegAdapter, FFmpegConfig, Recorder


FFMPEG=os.environ.get('ROBOT_HUB_FFMPEG')
FFPROBE=os.environ.get('ROBOT_HUB_FFPROBE')
VERSION=os.environ.get('ROBOT_HUB_FFMPEG_VERSION','9.0.2-essentials_build-www.gyan.dev')


@unittest.skipUnless(FFMPEG and FFPROBE,'Explicit opt-in FFmpeg and FFprobe paths required')
class NativeGeneratedRecordingTests(unittest.TestCase):
    def test_generated_variable_cadence_segments_and_derived_actual_pts(self):
        for executable in (FFMPEG,FFPROBE):
            self.assertTrue(Path(executable).is_absolute())
            self.assertTrue(Path(executable).is_file())
        with tempfile.TemporaryDirectory(prefix='robot-hub-generated-video-') as directory:
            # Source cadence is 10 Hz. Frames 2 and 6 in each ten-frame block
            # are deliberately dropped, without changing the remaining PTS.
            config=FFmpegConfig(FFMPEG,FFPROBE,VERSION,'generated-overview','lavfi',
                "testsrc2=size=64x64:rate=10,select='not(eq(mod(n,10),2)+eq(mod(n,10),6))'",
                'SYNTHETIC',segment_seconds=1,stalled_seconds=10,
                minimum_free_bytes=1024*1024,operation_timeout=15)
            adapter=FFmpegAdapter(config)
            recorder=Recorder(Path(directory)/'video',config,adapter=adapter)
            self.addCleanup(recorder.stop)
            health=recorder.start('generated-cadence')
            self.assertFalse(health['live_camera_qualified'])
            deadline=time.monotonic()+15
            while len(recorder.list_segments())<2 and time.monotonic()<deadline:
                health=recorder.tick()
                self.assertNotEqual(health['state'],'error',health)
                time.sleep(.05)
            self.assertGreaterEqual(len(recorder.list_segments()),2,recorder.health())
            before_shutdown=len(recorder.list_segments())
            self.assertEqual(recorder.stop()['state'],'stopped')
            self.assertIsNone(recorder.owner)
            self.assertIsNotNone(adapter.process.poll())
            segments=recorder.list_segments()
            self.assertGreater(len(segments),before_shutdown,'Shutdown must finalize the open generated tail')
            first=segments[0]
            self.assertEqual(first['source_type'],'SYNTHETIC')
            self.assertEqual(first['utc_basis'],'unmapped_recording_pts')
            self.assertIsNone(first['utc_uncertainty_ns'])
            actual=[int(frame['pts_ns']) for frame in first['frames']]
            expected=[n*100_000_000 for n in (0,1,3,4,5,7,8,9)]
            self.assertEqual(actual,expected)
            self.assertTrue(all(int(frame['duration_ns'])==100_000_000 for frame in first['frames']))
            originals={segment['relative_path']:(recorder.folder/segment['relative_path']).read_bytes()
                for segment in segments}
            for segment in segments:
                self.assertEqual(hashlib.sha256(originals[segment['relative_path']]).hexdigest(),segment['sha256'])
                self.assertEqual(adapter.probe(recorder.folder/segment['relative_path'])['frames'],
                    [{'pts_ns':int(f['pts_ns']),'duration_ns':int(f['duration_ns'])}
                     for f in segment['frames']])
            pin=recorder.preserve('generated-incident',50_000_000,850_000_000,event_id='synthetic-event')
            self.assertEqual(pin['state'],'partial')
            self.assertEqual(pin['unavailable_pts_intervals'],
                [['200000000','300000000'],['600000000','700000000']])
            self.assertEqual(pin['spans'][0]['frame_indexes'],list(range(7)))
            self.assertEqual(recorder.preserve('generated-incident',50_000_000,850_000_000,
                event_id='synthetic-event'),pin)
            manifest=recorder.render_pin('generated-incident')
            self.assertEqual(manifest['coverage'],'partial')
            artifact=manifest['artifacts'][0]
            derived=adapter.probe(recorder.folder/'clips/generated-incident'/artifact['relative_path'])
            self.assertEqual([frame['pts_ns'] for frame in derived['frames']],expected[:-1])
            self.assertEqual(artifact['actual_frame_pts_ns'],[str(value) for value in expected[:-1]])
            self.assertEqual(recorder.render_pin('generated-incident'),manifest)
            for name,content in originals.items():
                self.assertEqual((recorder.folder/name).read_bytes(),content)
            self.assertEqual(manifest['provenance']['version'],VERSION)
            for key in ('executable_sha256','probe_executable_sha256'):
                self.assertEqual(len(manifest['provenance'][key]),64)
            print('GENERATED_MEDIA_QUALIFICATION '+json.dumps({
                'profile':'lavfi-10hz-drop-2-and-6-per-second-v1',
                'version':VERSION,'provenance':manifest['provenance'],
                'segments_before_shutdown':before_shutdown,
                'closed_segments':len(segments),'first_original_pts_ns':actual,
                'derived_pts_ns':[frame['pts_ns'] for frame in derived['frames']],
                'native_generated_media_only':True,'live_camera_qualified':False},sort_keys=True))


if __name__=='__main__':
    unittest.main()
