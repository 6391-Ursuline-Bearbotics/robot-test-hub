"""Independent application-control ownership and hostile-request checks."""
import contextlib
from email.message import Message
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from robot_test_hub.application_control import ApplicationControl, ControlError
from robot_test_hub import server
from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.service import HubService


class ApplicationControlIndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.path = self.base / 'private.runtime.json'
        self.event = threading.Event()
        self.control = ApplicationControl(self.path, self.event)
        self.addCleanup(self.control.close)

    def headers(self, extra=(), *, auth=True):
        headers = Message()
        headers['Host'] = '127.0.0.1:6391'
        if auth:
            headers['Authorization'] = 'Bearer ' + self.control.token
        for key, value in extra:
            headers[key] = value
        return headers

    def test_exclusive_runtime_publication_preserves_existing_bytes(self):
        self.control.publish(6391)
        original = self.path.read_bytes()
        other = ApplicationControl(self.path, threading.Event())
        self.addCleanup(other.close)
        with self.assertRaises(ControlError):
            other.publish(6392)
        self.assertFalse(other.remove())
        self.assertEqual(self.path.read_bytes(), original)
        record = json.loads(original)
        self.assertEqual(record['pid'], os.getpid())
        self.assertEqual(len(record['token']), 64)
        self.assertEqual(len(record['instance_id']), 32)
        self.assertEqual(list(self.base.glob('.hub-runtime-*')), [])

    def test_publish_loses_race_without_overwriting_competitor(self):
        def compete(*args, **kwargs):
            self.path.write_bytes(b'competing instance')
            raise FileExistsError()
        with patch('robot_test_hub.application_control.os.link', side_effect=compete):
            with self.assertRaises(FileExistsError):
                self.control.publish(6391)
        self.assertFalse(self.control.remove())
        self.assertEqual(self.path.read_bytes(), b'competing instance')
        self.assertEqual(list(self.base.glob('.hub-runtime-*')), [])

    def test_remove_preserves_changed_runtime(self):
        self.control.publish(6391)
        self.path.write_bytes(b'replacement runtime')
        self.assertFalse(self.control.remove())
        self.assertEqual(self.path.read_bytes(), b'replacement runtime')

    def test_clean_remove_is_idempotent(self):
        self.control.publish(6391)
        self.assertTrue(self.control.remove())
        self.assertFalse(self.control.remove())
        self.assertFalse(self.path.exists())

    def test_bad_auth_never_sets_event_or_echoes_credentials(self):
        for supplied in ([], [('Authorization', 'Bearer stolen')], [('Authorization', '\u00e9')],
                         [('Authorization', 'Bearer ' + self.control.token)]):
            # Last case is duplicate valid auth, not a successful request.
            headers = self.headers(supplied, auth=bool(supplied and supplied[-1][1] == 'Bearer ' + self.control.token))
            code, result = self.control.request('POST', '/api/v1/application/stop', headers)
            self.assertEqual(code, 403)
            self.assertFalse(self.event.is_set())
            self.assertNotIn(self.control.token, json.dumps(result))

    def test_bad_framing_origin_and_duplicate_headers_never_stop(self):
        ordinary = [('Content-Length', '0'), ('X-Hub-Request', '1')]
        invalid = [[('Content-Length', '1'), ('X-Hub-Request', '1')],
                   ordinary + [('Content-Length', '0')], ordinary + [('Transfer-Encoding', 'chunked')],
                   ordinary + [('Origin', 'http://127.0.0.1:6391')], ordinary + [('Host', 'localhost:6391')],
                   ordinary + [('X-Hub-Request', '1')], [('X-Hub-Request', '1')]]
        for extra in invalid:
            with self.subTest(extra=extra):
                code, _ = self.control.request('POST', '/api/v1/application/stop', self.headers(extra))
                self.assertEqual(code, 400)
                self.assertFalse(self.event.is_set())

    def test_exact_route_required_and_success_is_redacted(self):
        headers = self.headers([('Content-Length', '0'), ('X-Hub-Request', '1')])
        for route in ('/api/v1/application/stop?x=1', '/api/v1/application/stop/', '/api/v1/application'):
            self.assertEqual(self.control.request('POST', route, headers)[0], 404)
            self.assertFalse(self.event.is_set())
        code, result = self.control.request('POST', '/api/v1/application/stop', headers)
        self.assertEqual(code, 202)
        self.assertTrue(self.event.is_set())
        self.assertEqual(result['instance_id'], self.control.instance_id)
        self.assertNotIn('token', result)
        self.assertNotIn(str(self.path), json.dumps(result))

    def lifecycle(self, *, bind_error=False, start_error=False, closes=True, competitor=False):
        service = Mock(root=self.base / 'archive')
        service.close.return_value = closes
        if start_error:
            service.start.side_effect = RuntimeError('private failure')
        httpd = Mock(server_address=('127.0.0.1', 6391))
        def request():
            if competitor:
                self.path.write_bytes(b'other instance')
            # Exit the foreground loop without using signals.
            control = factory.call_args.kwargs['application_control']
            control.shutdown.set()
        httpd.handle_request.side_effect = request
        with patch.object(server, 'HubService', return_value=service), \
             patch.object(server, 'create_http_server', side_effect=OSError() if bind_error else None, return_value=httpd) as factory, \
             patch.object(server.signal, 'signal', return_value=None), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = server.main(['--data-dir', str(self.base / 'archive'), '--launch-control', str(self.path)])
        if getattr(service, 'application_control_owner', None) is not None:
            self.addCleanup(service.application_control_owner.close)
            self.addCleanup(lambda: server._pending_shutdown_services.remove(service) if service in server._pending_shutdown_services else None)
        return result, service, httpd

    def test_bind_failure_closes_service_and_publishes_nothing(self):
        result, service, _ = self.lifecycle(bind_error=True)
        self.assertEqual(result, 2)
        service.close.assert_called_once()
        self.assertFalse(service.application_control_owner.lease_held)
        self.assertFalse(self.path.exists())

    def test_worker_start_failure_closes_bound_server_and_no_runtime(self):
        result, service, httpd = self.lifecycle(start_error=True)
        self.assertEqual(result, 2)
        httpd.server_close.assert_called_once()
        service.close.assert_called_once()
        self.assertFalse(service.application_control_owner.lease_held)
        self.assertFalse(self.path.exists())

    def test_outstanding_worker_retains_runtime(self):
        result, service, httpd = self.lifecycle(closes=False)
        self.assertEqual(result, 0)
        self.assertTrue(self.path.exists())
        self.assertTrue(service.application_control_owner.lease_held)
        contender = ApplicationControl(self.path, threading.Event())
        self.addCleanup(contender.close)
        with self.assertRaises(ControlError):
            contender.check_available()
        httpd.server_close.assert_called_once()

    def test_clean_shutdown_removes_only_own_runtime(self):
        result, _, _ = self.lifecycle()
        self.assertEqual(result, 0)
        self.assertFalse(self.path.exists())
        result, _, _ = self.lifecycle(competitor=True)
        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_bytes(), b'other instance')

    def test_stale_runtime_prevents_service_construction(self):
        self.path.write_bytes(b'stale private credential')
        with patch.object(server, 'HubService') as service, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(server.main(['--launch-control', str(self.path)]), 2)
        service.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'stale private credential')

    def test_actual_loopback_control_rejects_browser_origin_then_accepts_empty_post(self):
        source = DemoSource()
        service = HubService(Config(data_dir=str(self.base / 'archive')), source)
        httpd = server.create_http_server(service, source, 0, application_control=self.control)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=2)
            headers = {'Authorization': 'Bearer ' + self.control.token, 'X-Hub-Request': '1', 'Content-Length': '0'}
            conn.request('POST', '/api/v1/application/stop', body=b'', headers=dict(headers, Origin='http://127.0.0.1:' + str(httpd.server_port)))
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            conn.close()
            self.assertFalse(self.event.is_set())
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=2)
            conn.request('POST', '/api/v1/application/stop', body=b'', headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 202)
            self.assertNotIn(self.control.token.encode(), response.read())
            self.assertTrue(self.event.is_set())
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)
            self.assertTrue(service.close())
