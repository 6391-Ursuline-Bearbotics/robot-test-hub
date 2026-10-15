"""Independent synthetic HTTP/adversarial Rewind qualification."""
import contextlib
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from robot_test_hub.rewind import RewindConfig, RewindHttpTransport, RewindError, RewindCancelled
import robot_test_hub.rewind as rewind


class RawServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def endpoint(response, *, delayed=0, trigger=None, prefix=b''):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            request_bytes = self.request.recv(8192)
            if prefix:
                self.request.sendall(prefix)
            if trigger is not None:
                trigger.set()
            if delayed:
                time.sleep(delayed)
            try:
                self.request.sendall(response(request_bytes) if callable(response) else response)
            except OSError:
                pass
    server = RawServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = rewind.http.client.HTTPConnection
    def connect(host, port, timeout):
        return original('127.0.0.1', server.server_address[1], timeout=timeout)
    try:
        with patch.object(rewind.http.client, 'HTTPConnection', side_effect=connect):
            yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class RewindIndependentHttpTests(unittest.TestCase):
    def transport(self, **kwargs):
        return RewindHttpTransport(RewindConfig('synthetic', 'http://127.0.0.1:5807', 'robot', source_type='SYNTHETIC', **kwargs))

    def download(self, response):
        with tempfile.TemporaryDirectory() as root, endpoint(response):
            return self.transport().download('sample.avi', Path(root) / 'sample.avi')

    def test_duplicate_content_length_rejected(self):
        with self.assertRaises(RewindError):
            self.download(b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 3\r\n\r\nabc')

    def test_unknown_transfer_encoding_rejected(self):
        with self.assertRaises(RewindError):
            self.download(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\nConnection: close\r\n\r\nabc')

    def test_stacked_transfer_encoding_rejected(self):
        with self.assertRaises(RewindError):
            self.download(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n0\r\n\r\n')

    def test_chunked_with_content_length_rejected(self):
        with self.assertRaises(RewindError):
            self.download(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 3\r\n\r\n3\r\nabc\r\n0\r\n\r\n')

    def test_redirect_not_followed(self):
        with self.assertRaises(RewindError):
            self.download(b'HTTP/1.1 302 Found\r\nLocation: http://private.invalid/secret\r\nContent-Length: 0\r\n\r\n')

    def test_partial_content_not_treated_as_full_recording(self):
        with self.assertRaises(RewindError):
            self.download(b'HTTP/1.1 206 Partial Content\r\nContent-Length: 3\r\nContent-Range: bytes 0-2/100\r\n\r\nabc')

    def test_truncated_body_not_accepted(self):
        with self.assertRaises(RewindError):
            self.download(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nabc')

    def test_abort_during_body_wait_without_external_cancel(self):
        request = threading.Event()
        transport = self.transport(timeout_seconds=2)
        errors = []
        with tempfile.TemporaryDirectory() as root, endpoint(b'abc', delayed=.7, trigger=request,
                prefix=b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n'):
            def run():
                try:
                    transport.download('sample.avi', Path(root) / 'sample.avi')
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(request.wait(1))
            transport.abort()
            worker.join(.5)
            self.assertFalse(worker.is_alive(), 'Abort must release body wait')
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RewindCancelled)

    def test_chunked_payload_preserves_decoded_original_bytes(self):
        result = self.download(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n')
        self.assertEqual(result['size_bytes'], 3)
        self.assertEqual(result['sha256'], hashlib.sha256(b'abc').hexdigest())

    def test_cancel_active_http_before_headers(self):
        request = threading.Event()
        cancel = threading.Event()
        transport = self.transport(timeout_seconds=2)
        errors = []
        with tempfile.TemporaryDirectory() as root, endpoint(b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc', delayed=.7, trigger=request):
            def run():
                try:
                    transport.download('sample.avi', Path(root) / 'sample.avi', cancel.is_set)
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(request.wait(1))
            cancel.set()
            transport.abort()
            worker.join(.5)
            self.assertFalse(worker.is_alive(), 'Abort must release header wait')
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RewindCancelled)


import hashlib
import sqlite3
from robot_test_hub.rewind import RewindArchive


class ArchiveTransport:
    def __init__(self):
        self.payload = {'sample.avi': b'video-original', 'sample_manifest.jsonl': b'{"frame":1}\n'}
        self.calls = []
        self.revoke_after_video = False
        self.permitted = True
        self.changed_after_download = False
        self.downloaded = False

    def state(self, cancel):
        if cancel():
            raise RewindCancelled()
        return {'rewind': {'flushing': 0, 'storedSeconds': 10}, 'ts_us': 100}

    @staticmethod
    def is_flushing(state):
        return bool(state['rewind']['flushing'])

    def list_recordings(self, cancel):
        if cancel():
            raise RewindCancelled()
        if self.changed_after_download and self.downloaded:
            return []
        return [{'video':'sample.avi', 'manifest':'sample_manifest.jsonl', 'bootlog':None,
                 'size':sum(map(len, self.payload.values()))}]

    def download(self, name, destination, cancel, max_bytes):
        if cancel():
            raise RewindCancelled()
        self.calls.append(name)
        payload = self.payload[name]
        Path(destination).write_bytes(payload)
        self.downloaded = True
        if self.revoke_after_video and name.endswith('.avi'):
            self.permitted = False
        return {'size_bytes':len(payload), 'sha256':hashlib.sha256(payload).hexdigest()}


class RewindIndependentArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.db = sqlite3.connect(':memory:')
        self.transport = ArchiveTransport()
        self.config = RewindConfig('synthetic', 'http://127.0.0.1:5807', 'robot', source_type='SYNTHETIC', revalidate_seconds=1)
        self.archive = RewindArchive(self.root, self.db, self.config, self.transport)

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def collect(self):
        return self.archive.collect(lambda:self.transport.permitted)

    def test_gate_revoked_after_video_leaves_group_unpublished(self):
        self.transport.revoke_after_video = True
        with self.assertRaises(RewindCancelled):
            self.collect()
        self.assertEqual(self.transport.calls, ['sample.avi'])
        self.assertEqual(self.archive.list_records(), [])
        self.assertEqual(list(self.archive.groups.iterdir()), [])

    def test_entry_disappearing_after_download_leaves_group_unpublished(self):
        self.transport.changed_after_download = True
        with self.assertRaises(RewindError):
            self.collect()
        self.assertEqual(self.archive.list_records(), [])
        self.assertEqual(list(self.archive.groups.iterdir()), [])

    def test_same_name_same_length_new_content_preserves_both_originals(self):
        first = self.collect()[0]
        old_path = self.archive.file_path(first['recording_id'], 'video')
        old = old_path.read_bytes()
        self.transport.payload['sample.avi'] = b'VIDEO-original'
        self.db.execute('DELETE FROM rewind_observations')
        self.db.commit()
        second = self.collect()[0]
        self.assertNotEqual(first['recording_id'], second['recording_id'])
        self.assertEqual(len(self.archive.list_records()), 2)
        self.assertEqual(old_path.read_bytes(), old)
        self.assertFalse(second['remote_content_identity_qualified'])

    def test_failed_manifest_never_registers_video_as_complete(self):
        self.transport.payload['sample_manifest.jsonl'] = b'{"frame":1}'
        with self.assertRaises(RewindError):
            self.collect()
        self.assertEqual(self.archive.list_records(), [])

    def test_published_group_recovers_after_database_registration_loss(self):
        original = self.collect()[0]
        self.db.execute('DELETE FROM rewind_recordings')
        self.db.commit()
        self.assertEqual(self.archive.recover(), 1)
        self.assertEqual(self.archive.get_record(original['recording_id']), original)





from types import SimpleNamespace
from robot_test_hub.rewind_service import RewindWorker
from robot_test_hub.collector import RobotStatus


class RewindIndependentPermissionTests(unittest.TestCase):
    def test_stale_disabled_gap_restarts_complete_idle_delay(self):
        now = [100.]
        with tempfile.TemporaryDirectory() as root, contextlib.closing(sqlite3.connect(':memory:')) as db:
            service = SimpleNamespace(source=object(), settings_db=db, root=Path(root),
                config=SimpleNamespace(freshness=.5, idle_delay=10), stop=threading.Event(),
                paused=threading.Event(), preference_generation=0, camera_network=threading.Event(), _health=lambda *args:None)
            config = RewindConfig('synthetic', 'http://127.0.0.1:5807', 'robot', source_type='SYNTHETIC')
            worker = RewindWorker(service, config, clock=lambda:now[0])
            worker.observe(RobotStatus(False, now[0], 'boot', 1))
            now[0] += 11
            self.assertFalse(worker._permission(idle=True))
            worker.observe(RobotStatus(False, now[0], 'boot', 1))
            self.assertFalse(worker._permission(idle=True), 'Fresh recovery must establish a new full idle window')





class RewindIndependentPauseTests(unittest.TestCase):
    def test_pause_resume_revokes_token_and_restarts_idle_window(self):
        now = [100.]
        with tempfile.TemporaryDirectory() as root, contextlib.closing(sqlite3.connect(':memory:')) as db:
            service = SimpleNamespace(source=object(), settings_db=db, root=Path(root),
                config=SimpleNamespace(freshness=100, idle_delay=10), stop=threading.Event(),
                paused=threading.Event(), preference_generation=0, camera_network=threading.Event(), _health=lambda *args:None)
            config = RewindConfig('synthetic', 'http://127.0.0.1:5807', 'robot', source_type='SYNTHETIC')
            worker = RewindWorker(service, config, clock=lambda:now[0])
            worker.observe(RobotStatus(False, now[0], 'boot', 1))
            now[0] += 11
            self.assertTrue(worker._permission(idle=True))
            token = worker._token()
            service.paused.set()
            service.preference_generation += 1
            service.paused.clear()
            service.preference_generation += 1
            self.assertFalse(worker._permission(idle=True, token=token))
            self.assertFalse(worker._permission(idle=True), 'Pause/resume must require a full new idle window')


class RewindIndependentCaptureTests(unittest.TestCase):
    def setUp(self):
        from robot_test_hub.storage import open_catalog
        self.folder = tempfile.TemporaryDirectory()
        self.now = [100.]
        self.root = Path(self.folder.name)
        self.settings = open_catalog(self.root)
        self.service = SimpleNamespace(source=object(), settings_db=self.settings, root=self.root,
            config=SimpleNamespace(freshness=.5, idle_delay=10), stop=threading.Event(),
            paused=threading.Event(), preference_generation=0, camera_network=threading.Event(), bulk_lock=threading.Lock(), _health=lambda *args:None)
        self.config = RewindConfig('synthetic', 'http://127.0.0.1:5807', 'robot', source_type='SYNTHETIC')
        self.worker = RewindWorker(self.service, self.config, clock=lambda:self.now[0])
        self.worker._open()

    def tearDown(self):
        self.worker.db.close()
        self.settings.close()
        self.folder.cleanup()

    def event(self):
        self.worker.observe(RobotStatus(True, self.now[0], 'boot', 0))
        self.now[0] += .1
        self.worker.observe(RobotStatus(False, self.now[0], 'boot', 1))
        self.worker._events_to_db()

    def state(self):
        return self.worker.db.execute('SELECT state FROM rewind_capture_requests').fetchone()[0]

    def test_crashed_issuing_post_becomes_unknown_without_retry(self):
        self.event()
        self.worker.db.execute("UPDATE rewind_capture_requests SET state='issuing'")
        self.worker.db.commit()
        self.worker.db.close()
        self.worker._open()
        self.assertEqual(self.state(), 'unknown')
        self.assertFalse(self.worker._capture())

    def test_boot_change_never_retargets_old_capture(self):
        self.event()
        self.worker.observe(RobotStatus(False, self.now[0], 'next-boot', 1))
        self.assertTrue(self.worker._capture())
        self.assertEqual(self.state(), 'missed')

    def test_capture_prompt_does_not_wait_for_bulk_idle_delay(self):
        self.event()
        self.assertFalse(self.worker._permission(idle=True))
        self.worker.transport = SimpleNamespace(flush=lambda *args, **kwargs:{'success':True},
            state=lambda *args, **kwargs:{'rewind':{'flushing':0}}, is_flushing=lambda state:False)
        self.assertTrue(self.worker._capture())
        self.assertEqual(self.state(), 'accepted')
        self.assertFalse(self.worker._capture(), 'Accepted capture must not be issued twice')

    def test_actual_http_rejection_is_rejected_not_unknown(self):
        self.event()
        body = b'{"success":false,"message":"busy"}'
        response = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body
        def route(request):
            if request.startswith(b'GET /results '):
                state = b'{"rewind":{"flushing":0,"storedSeconds":10}}'
                return b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(state)).encode() + b'\r\n\r\n' + state
            return response
        with endpoint(route):
            self.worker._capture()
        self.assertEqual(self.state(), 'rejected')


if __name__ == '__main__':
    unittest.main()
