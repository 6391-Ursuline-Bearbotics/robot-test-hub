"""Actual generated capture must outlive its retained progress limit, without hardware."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from robot_test_hub.recorder import FFmpegAdapter, FFmpegConfig, Recorder


FFMPEG = os.environ.get('ROBOT_HUB_FFMPEG')
FFPROBE = os.environ.get('ROBOT_HUB_FFPROBE')
VERSION = os.environ.get('ROBOT_HUB_FFMPEG_VERSION', '9.0.2-essentials_build-www.gyan.dev')


@unittest.skipUnless(FFMPEG and FFPROBE, 'Explicit local FFmpeg/FFprobe paths required')
class NativeBoundedCaptureTests(unittest.TestCase):
    def test_generated_capture_continues_past_lifetime_output_cap_with_bounded_retention(self):
        with tempfile.TemporaryDirectory(prefix='robot-hub-bounded-native-') as directory:
            config = FFmpegConfig(FFMPEG, FFPROBE, VERSION, 'generated-bounded', 'lavfi',
                'testsrc2=size=64x64:rate=10', 'SYNTHETIC', segment_seconds=1,
                stalled_seconds=10, minimum_free_bytes=1024*1024, operation_timeout=15,
                maximum_progress_bytes=512, maximum_error_log_bytes=1024)
            adapter = FFmpegAdapter(config)
            recorder = Recorder(Path(directory).resolve() / 'video', config, adapter=adapter)
            self.addCleanup(recorder.stop)
            recorder.start('generated-bounded-session')
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                health = recorder.tick()
                self.assertNotEqual(health['state'], 'error', health)
                stats = adapter.capture_diagnostics()
                for name, bound in (('progress.txt', config.maximum_progress_bytes),
                                    ('native-errors.log', config.maximum_error_log_bytes)):
                    path = recorder.folder / name
                    if path.exists():
                        self.assertLessEqual(path.stat().st_size, bound, name)
                if (health['frames'] or 0) >= 80 and stats['progress_bytes_drained'] > 2*config.maximum_progress_bytes:
                    break
                time.sleep(.05)
            else:
                self.fail('Capture did not advance beyond lifetime progress bound: ' + str(recorder.health()))
            self.assertEqual(health['state'], 'recording')
            self.assertGreaterEqual(len(recorder.list_segments()), 6)
            before = {item['relative_path']: (recorder.folder / item['relative_path']).read_bytes()
                      for item in recorder.list_segments()}
            self.assertEqual(recorder.stop()['state'], 'stopped')
            self.assertIsNone(recorder.owner)
            self.assertFalse(recorder.process_started)
            self.assertIsNotNone(adapter.process.poll())
            self.assertEqual(adapter.capture_diagnostics()['readers_alive'], 0)
            self.assertTrue(adapter.process.stdout.closed)
            self.assertTrue(adapter.process.stderr.closed)
            self.assertTrue(adapter.process.stdin.closed)
            segments = recorder.list_segments()
            self.assertGreater(len(segments), len(before), 'Finalization must preserve the open tail')
            for item in segments:
                path = recorder.folder / item['relative_path']
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item['sha256'])
                actual = adapter.probe(path)['frames']
                self.assertEqual(actual, [{'pts_ns': int(frame['pts_ns']), 'duration_ns': int(frame['duration_ns'])}
                                          for frame in item['frames']])
                if item['relative_path'] in before:
                    self.assertEqual(path.read_bytes(), before[item['relative_path']])
            for name, bound in (('progress.txt', config.maximum_progress_bytes),
                                ('native-errors.log', config.maximum_error_log_bytes)):
                self.assertLessEqual((recorder.folder / name).stat().st_size, bound)
            stats = adapter.capture_diagnostics()
            self.assertGreater(stats['progress_bytes_drained'], 2*config.maximum_progress_bytes)
            self.assertFalse(recorder.health()['live_camera_qualified'])
            print('GENERATED_BOUNDED_CAPTURE_QUALIFICATION ' + json.dumps(dict(
                profile='lavfi-10hz-512-byte-progress-retention-v1',
                progress_bytes_drained=stats['progress_bytes_drained'],
                retained_progress_bytes=(recorder.folder / 'progress.txt').stat().st_size,
                retained_error_bytes=(recorder.folder / 'native-errors.log').stat().st_size,
                frame_observation=health['frames'], closed_segments=len(segments),
                readers_alive=stats['readers_alive'], provenance=recorder.provenance,
                generated_media_only=True, live_camera_qualified=False), sort_keys=True))


if __name__ == '__main__':
    unittest.main()
