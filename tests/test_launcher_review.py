"""Independent adversarial launcher checks; no archive or real endpoint access."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub import launcher


class LauncherIndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name).resolve()
        self.profile = self.base / 'launch.json'
        self.record = dict(schema_version=1, port=6391, pid=1234,
                           token='a' * 64, instance_id='b' * 32)

    def runtime(self, value=None):
        target = launcher.runtime_path(self.profile)
        target.write_text(json.dumps(value or self.record), encoding='utf-8')
        return target

    def test_no_runtime_never_contacts_a_port(self):
        with patch.object(launcher, '_control') as control:
            self.assertFalse(launcher.stop_profile(self.profile))
        control.assert_not_called()

    def test_identity_mismatch_never_posts_or_deletes_record(self):
        target = self.runtime()
        original = target.read_bytes()
        for changed in ({'pid': 4321}, {'instance_id': 'c' * 32}, {'pid': True}):
            response = dict(self.record, **changed)
            with self.subTest(changed=changed), patch.object(launcher, '_control', return_value=response) as control:
                with self.assertRaises(launcher.LaunchError):
                    launcher.stop_profile(self.profile)
                self.assertEqual([call.args[1] for call in control.call_args_list], ['GET'])
                self.assertEqual(target.read_bytes(), original)

    def test_valid_stop_keeps_runtime_until_server_cleans_up(self):
        target = self.runtime()
        with patch.object(launcher, '_control', return_value=self.record) as control:
            self.assertTrue(launcher.stop_profile(self.profile))
        self.assertEqual([call.args[1] for call in control.call_args_list], ['GET', 'POST'])
        self.assertTrue(target.is_file())

    def test_malformed_runtime_never_contacts_network(self):
        target = self.runtime()
        cases = [dict(self.record, port=True), dict(self.record, pid=0),
                 dict(self.record, token='secret'), dict(self.record, instance_id='b' * 33),
                 dict(self.record, host='robot.local'), dict(self.record, schema_version=True)]
        for value in cases:
            target.write_text(json.dumps(value), encoding='utf-8')
            with self.subTest(value=value), patch.object(launcher, '_control') as control:
                with self.assertRaises(launcher.LaunchError):
                    launcher.stop_profile(self.profile)
                control.assert_not_called()

    def test_duplicate_and_oversized_runtime_rejected_without_secret_output(self):
        target = self.runtime()
        for raw in ('{"schema_version":1,"schema_version":1}', 'x' * 65537):
            target.write_text(raw, encoding='utf-8')
            with self.assertRaises(launcher.LaunchError) as raised:
                launcher.stop_profile(self.profile)
            self.assertNotIn(str(target), str(raised.exception))
            self.assertNotIn(self.record['token'], str(raised.exception))

    def test_profile_and_hub_paths_independent_of_invocation_directory(self):
        config = self.base / 'private' / 'hub.json'
        config.parent.mkdir()
        config.write_text(json.dumps(dict(schema_version=1, data_dir='../archive')), encoding='utf-8')
        self.profile.write_text(json.dumps(dict(schema_version=1, mode='demo', hub_config='private/hub.json')), encoding='utf-8')
        before = Path.cwd()
        loaded = launcher.load_profile(self.profile)
        self.assertEqual(loaded['hub_config'], config)
        self.assertTrue(launcher.check_profile(self.profile)['ready'])
        self.assertEqual(Path.cwd(), before)
        self.assertFalse((self.base / 'archive').exists())

    def test_demo_rejects_live_configuration_before_network(self):
        config = self.base / 'hub.json'
        config.write_text(json.dumps(dict(schema_version=1, data_dir='archive')), encoding='utf-8')
        for live in ({'nt_host': 'robot.local'}, {'rewind_config': 'secret.json'}, {'source_config': 'source.json'}):
            self.profile.write_text(json.dumps(dict(schema_version=1, mode='demo', hub_config='hub.json', **live)), encoding='utf-8')
            self.assertFalse(launcher.check_profile(self.profile)['ready'])
        self.assertFalse((self.base / 'archive').exists())

    @contextmanager
    def local_control(self, status=200, payload=None, location=None):
        calls = []
        body = json.dumps(payload if payload is not None else self.record).encode()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                calls.append((self.path, self.headers.get('Authorization')))
                self.send_response(status)
                if location:
                    self.send_header('Location', location)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield dict(self.record, port=server.server_port), calls
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_control_ignores_ambient_proxy(self):
        with self.local_control() as (record, calls):
            with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'http_proxy': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
                result = launcher._control(record, 'GET')
        self.assertEqual(result['instance_id'], record['instance_id'])
        self.assertEqual(calls, [('/api/v1/application', 'Bearer ' + record['token'])])

    def test_control_never_follows_redirect_or_exposes_token(self):
        with self.local_control(status=302, location='http://127.0.0.1:1/private') as (record, calls):
            with self.assertRaises(launcher.LaunchError) as raised:
                launcher._control(record, 'GET')
        self.assertEqual(len(calls), 1)
        self.assertNotIn(record['token'], str(raised.exception))
        self.assertNotIn('.runtime', str(raised.exception))

    def test_control_rejects_response_size_limit(self):
        with self.local_control(payload={'unexpected': 'x' * 5000}) as (record, calls):
            with self.assertRaises(launcher.LaunchError):
                launcher._control(record, 'GET')
        self.assertEqual(len(calls), 1)
