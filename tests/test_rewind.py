"""Local HTTP integration only; no robot/camera connection is made."""
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from robot_test_hub.rewind import (RewindArchive, RewindCancelled, RewindConfig,
                                  RewindError, RewindHttpTransport)


class RewindTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = RewindConfig(camera_id='ll4', origin='http://127.0.0.1:5807',
                                   robot_id='6391', source_type='SYNTHETIC')
        self.files = {'clip.avi':b'RIFF-test-video', 'clip_manifest.jsonl':b'{"fidx":1,"ts_us":123}\n'}
        self.entry = {'video':'clip.avi', 'manifest':'clip_manifest.jsonl', 'bootlog':None,
                      'size':sum(map(len, self.files.values()))}
        self.results = {'rewind':{'flushing':0, 'storedSeconds':180}, 'ts_us':1000}
        self.requests = []
        self.mode = None
        outer = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):
                pass
            def do_GET(self):
                outer.requests.append(('GET', self.path))
                if outer.mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:1/private')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path == '/results':
                    raw = json.dumps(outer.results).encode()
                elif self.path == '/recording-list':
                    raw = json.dumps([outer.entry]).encode()
                elif self.path.startswith('/recording/'):
                    raw = outer.files[self.path[11:]]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw) + (10 if outer.mode == 'truncated' else 0)))
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    if outer.mode == 'slow-stream' and self.path.startswith('/recording/'):
                        for offset in range(0, len(raw), 3):
                            time.sleep(.06)
                            self.wfile.write(raw[offset:offset+3])
                            self.wfile.flush()
                    else:
                        self.wfile.write(raw)
                except OSError:
                    pass
                self.close_connection = True
            def do_POST(self):
                outer.requests.append(('POST', self.path))
                raw = b'{"success":false,"message":"busy"}' if outer.mode == 'flush-rejected' else b'{"success":true,"message":"queued"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        import http.client
        connection_class = http.client.HTTPConnection
        def local_connection(host, port, **kwargs):
            self.assertEqual(host, '127.0.0.1')
            self.assertEqual(port, 5807)
            return connection_class('127.0.0.1', self.server.server_port, **kwargs)
        self.mock = patch('robot_test_hub.rewind.http.client.HTTPConnection', side_effect=local_connection)
        self.mock.start()
        self.db = sqlite3.connect(':memory:')
        self.transport = RewindHttpTransport(self.config)
        self.archive = RewindArchive(self.root, self.db, self.config, self.transport)

    def tearDown(self):
        self.transport.abort()
        self.mock.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.db.close()
        self.temp.cleanup()

    def test_absence_disabled_and_config_validated(self):
        self.assertIsNone(RewindConfig.from_mapping(None))
        for change in ({'origin':'https://127.0.0.1:5807'}, {'origin':'http://127.0.0.1:5808'},
                       {'origin':'http://user:pw@127.0.0.1:5807'}, {'origin':'http://127.0.0.1:5807/path'},
                       {'origin':'http://127.0.0.1:5807?x=y'}, {'source_type':'SIM'},
                       {'duration_seconds':True}, {'timeout_seconds':float('nan')},
                       {'poll_interval_seconds':0}, {'max_recording_bytes':0}):
            with self.subTest(change=change), self.assertRaises(RewindError):
                replace(self.config, **change)
        self.assertEqual([], self.requests)

    def test_http_contract_and_flush(self):
        self.assertEqual(self.results, self.transport.state())
        self.assertEqual([self.entry], self.transport.list_recordings())
        self.assertTrue(self.transport.flush()['success'])
        self.assertIn(('POST', '/recording-flush?duration=180'), self.requests)
        target = self.root / 'download.avi'
        info = self.transport.download('clip.avi', target)
        self.assertEqual(self.files['clip.avi'], target.read_bytes())
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), info['sha256'])

    def test_flush_known_rejection_retains_explicit_outcome(self):
        self.mode = 'flush-rejected'
        self.assertFalse(self.transport.flush()['success'])

    def test_gate_prevents_all_requests(self):
        with self.assertRaises(RewindCancelled):
            self.archive.collect(lambda:False)
        self.assertEqual([], self.requests)

    def test_busy_and_unknown_status_do_not_download(self):
        self.results['rewind']['flushing'] = 1
        self.assertEqual([], self.archive.collect(lambda:True))
        self.assertFalse(any(path.startswith('/recording/') for _, path in self.requests))
        del self.results['rewind']['flushing']
        with self.assertRaises(RewindError):
            self.archive.collect(lambda:True)

    def test_group_hashes_cache_and_local_publication(self):
        records = self.archive.collect(lambda:True)
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual(1, record['manifest_frame_count'])
        self.assertIsNone(record['decoded_video_frame_count'])
        self.assertFalse(record['physical_camera_qualified'])
        self.assertFalse(record['remote_content_identity_qualified'])
        self.assertEqual(self.files['clip.avi'], self.archive.file_path(record['recording_id'], 'video').read_bytes())
        count = len([path for _, path in self.requests if path.startswith('/recording/')])
        self.assertEqual([], self.archive.collect(lambda:True))
        self.assertEqual(count, len([path for _, path in self.requests if path.startswith('/recording/')]))
        self.assertEqual([record], self.archive.list_records())

    def test_reused_name_same_size_preserves_version_on_revalidation(self):
        original = self.archive.collect(lambda:True)[0]
        self.files['clip.avi'] = b'X' * len(self.files['clip.avi'])
        self.db.execute('UPDATE rewind_observations SET checked_ns=1')
        self.db.commit()
        newer = self.archive.collect(lambda:True)[0]
        self.assertNotEqual(original['recording_id'], newer['recording_id'])
        self.assertEqual(2, len(self.archive.list_records()))
        self.assertEqual(b'RIFF-test-video', self.archive.file_path(original['recording_id'], 'video').read_bytes())

    def test_application_clock_rollback_invalidates_discovery_cache(self):
        self.archive.collect(lambda:True)
        self.results['ts_us'] = 0
        count = len(self.requests)
        self.assertEqual([], self.archive.collect(lambda:True))
        self.assertTrue(any(path.startswith('/recording/') for _, path in self.requests[count:]))

    def test_truncation_retry_starts_fresh_and_does_not_publish(self):
        self.mode = 'truncated'
        with self.assertRaises(RewindError):
            self.archive.collect(lambda:True)
        self.assertEqual([], self.archive.list_records())
        self.assertEqual([], list(self.archive.groups.iterdir()))
        self.mode = None
        self.assertEqual(1, len(self.archive.collect(lambda:True)))

    def test_redirect_is_not_followed(self):
        self.mode = 'redirect'
        with self.assertRaises(RewindError):
            self.transport.state()
        self.assertEqual([('GET','/results')], self.requests)

    def test_proxy_environment_is_ignored(self):
        with patch.dict('os.environ', {'http_proxy':'http://127.0.0.1:1', 'HTTP_PROXY':'http://127.0.0.1:1'}):
            self.assertEqual(self.results, self.transport.state())

    def test_stream_can_exceed_gap_timeout_while_total_deadline_remains(self):
        self.mode = 'slow-stream'
        transport = RewindHttpTransport(replace(self.config, timeout_seconds=.1, download_deadline_seconds=2))
        info = transport.download('clip.avi', self.root / 'stream.avi')
        self.assertEqual(len(self.files['clip.avi']), info['size_bytes'])

    def test_response_and_group_bounds(self):
        small = RewindHttpTransport(replace(self.config, max_recording_bytes=5))
        with self.assertRaises(RewindError):
            small.download('clip.avi', self.root / 'small.avi')
        self.entry['size'] -= 1
        with self.assertRaises(RewindError):
            self.archive.collect(lambda:True)
        self.assertEqual([], self.archive.list_records())

    def test_filenames_confined_and_cross_group_mismatch_rejected(self):
        for name in ('../clip.avi', '%2e%2e.avi', 'CON.avi', 'x/clip.avi', 'x\\clip.avi', 'x.txt'):
            with self.subTest(name=name), self.assertRaises(RewindError):
                self.transport.download(name, self.root / 'bad')
        self.assertEqual([], self.requests)
        self.entry['manifest'] = 'other_manifest.jsonl'
        with self.assertRaises(RewindError):
            self.transport.list_recordings()

    def test_cancel_mid_transfer_never_publishes(self):
        counter = [0]
        def cancel():
            counter[0] += 1
            return counter[0] > 12
        with self.assertRaises(RewindCancelled):
            self.archive.collect(lambda:True, cancel)
        self.assertEqual([], self.archive.list_records())
        self.assertEqual([], list(self.archive.groups.iterdir()))

    def test_crash_after_publication_before_registration_recovers(self):
        with patch.object(self.archive, '_register', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.archive.collect(lambda:True)
        self.assertEqual([], self.archive.list_records())
        self.assertEqual(1, self.archive.recover())
        self.assertEqual(0, self.archive.recover())
        self.assertEqual(1, len(self.archive.list_records()))

    def test_corrupted_published_bytes_never_return_as_verified(self):
        record = self.archive.collect(lambda:True)[0]
        path = self.archive.file_path(record['recording_id'], 'video')
        path.write_bytes(b'corrupt')
        with self.assertRaises(RewindError):
            self.archive.file_path(record['recording_id'], 'video')
        with self.assertRaises(RewindError):
            self.archive.recover()

    def test_manifest_invalid_utf8_or_incomplete_refuses_publication(self):
        for raw in (b'{}', b'\xff\n', b'[]\n', b'{"a":1,"a":2}\n', b'{"a":NaN}\n'):
            self.files['clip_manifest.jsonl'] = raw
            self.entry['size'] = sum(map(len, self.files.values()))
            with self.subTest(raw=raw), self.assertRaises(RewindError):
                self.archive.collect(lambda:True)
        self.assertEqual([], self.archive.list_records())


if __name__ == '__main__':
    unittest.main()
