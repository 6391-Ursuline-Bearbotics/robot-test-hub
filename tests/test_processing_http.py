"""Loopback processing receipts from actual generated WPILOG transfers/imports."""
from contextlib import closing
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from robot_test_hub.collector import Collector, LogFile, RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.pipeline import Pipeline
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog
from robot_test_hub.wpilog import PROFILE

FIXTURES = Path(__file__).parent / 'fixtures' / 'synthetic'


class GeneratedRecordingSource:
    source_type = 'SYNTHETIC'

    def __init__(self):
        self.entries = {
            'valid': ((FIXTURES / 'alpha7-main.wpilog').read_bytes(), 'wpilog', PROFILE),
            'unsupported': ((FIXTURES / 'alpha7-boot-b.wpilog').read_bytes(), 'wpilog', 'unqualified-test-profile'),
            'invalid': (b'INVENTED INVALID WPILOG', 'wpilog', PROFILE),
            'opaque': (b'OPAQUE SYNTHETIC TRANSFER FIXTURE', 'opaque', None),
        }
        self.hashes = {key: hashlib.sha256(value[0]).hexdigest() for key, value in self.entries.items()}
        self.read_calls = 0

    def status(self):
        return RobotStatus(False, time.monotonic(), 'synthetic-boot', 1)

    def list_closed_files(self):
        return [LogFile(key, 'private-fixture-name-' + key, len(data), self.hashes[key], 1,
                        format=kind, format_profile=profile)
                for key, (data, kind, profile) in self.entries.items()]

    def read(self, identity, offset, length):
        self.read_calls += 1
        return self.entries[identity][0][offset:offset+length]


class ProcessingHttpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = GeneratedRecordingSource()
        collector = Collector(self.root, self.source, idle_delay=0)
        try:
            for _ in range(12):
                collector.tick()
                if collector.snapshot()['completed_files'] == 4:
                    break
            self.assertEqual(collector.snapshot()['completed_files'], 4)
        finally:
            collector.close()
        with closing(open_catalog(self.root)) as db:
            pipeline = Pipeline(self.root, db)
            for _ in range(4):
                pipeline.tick()
        self.service = HubService(Config(data_dir=str(self.root)), self.source)
        self.addCleanup(self.service.close)
        self.service._health('indexer', 'running')
        self.server = create_http_server(self.service, self.source, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.thread.join(3), self.server.server_close()))

    def request(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def get(self, suffix='', **query):
        path = '/api/v1/processing' + suffix
        if query:
            path += '?' + urlencode(query)
        status, _, raw = self.request(path)
        return status, json.loads(raw)

    def test_genuine_import_to_searchable_run_and_exact_report_receipts_without_source_io(self):
        before = hashlib.sha256((self.root/'catalog.sqlite3').read_bytes()).hexdigest()
        read_calls = self.source.read_calls
        with patch.object(self.source, 'status', side_effect=AssertionError('Source poll in local processing request')), \
             patch.object(self.source, 'read', side_effect=AssertionError('Source read in local processing request')):
            status, page = self.get(limit='20')
            self.assertEqual(status, 200)
            self.assertEqual(page['total'], 4)
            self.assertEqual(len(page['items']), 4)
            valid = next(item for item in page['items'] if item['sha256'] == self.source.hashes['valid'])
            self.assertEqual(valid['original']['state'], 'verified')
            self.assertEqual(valid['indexing']['run_count'], 2)
            self.assertGreater(valid['reports']['count'], 0)
            status, runs = self.get('/' + valid['sha256'], section='runs')
            self.assertEqual(status, 200)
            self.assertEqual(runs['total'], 2)
            status, reports = self.get('/' + valid['sha256'], section='reports')
            self.assertEqual(status, 200)
            self.assertGreater(len(reports['items']), 0)
            for item in reports['items']:
                path = '/api/v1/runs/' + item['run_id'] + '/reports/' + item['report_id']
                response_status, _, raw = self.request(path)
                self.assertEqual(response_status, 200)
                exact = json.loads(raw)
                self.assertEqual(item['sha256'], exact['result_sha256'])
        self.assertEqual(self.source.read_calls, read_calls)
        self.assertEqual(hashlib.sha256((self.root/'catalog.sqlite3').read_bytes()).hexdigest(), before)
        serialized = json.dumps(page)
        self.assertNotIn('private-fixture-name', serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_attention_contains_invalid_and_unsupported_but_not_opaque_demo(self):
        status, page = self.get(view='attention')
        self.assertEqual(status, 200)
        hashes = {item['sha256'] for item in page['items']}
        self.assertIn(self.source.hashes['invalid'], hashes)
        self.assertIn(self.source.hashes['unsupported'], hashes)
        self.assertNotIn(self.source.hashes['opaque'], hashes)
        for item in page['items']:
            self.assertEqual(item['indexing']['run_count'], 0)
        status, opaque = self.get('/' + self.source.hashes['opaque'])
        self.assertEqual(status, 200)
        self.assertEqual(opaque['recording']['imports']['state'], 'not_applicable')

    def test_worker_failure_remains_visible_without_invalidating_old_catalog_evidence(self):
        with patch('robot_test_hub.pipeline.Pipeline.tick', side_effect=RuntimeError('private-source-token')):
            self.service._pipeline_worker()
        status, page = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(page['indexer']['state'], 'failed')
        self.assertNotIn('private-source-token', json.dumps(page))
        self.assertTrue(any(item['indexing']['run_count'] for item in page['items']))

    def test_strict_query_routes_shutdown_and_static_script(self):
        for query in ('limit=0', 'limit=51', 'limit=01', 'limit=2&limit=3',
                      'view=secret', 'cursor=private-path', 'unknown=private-secret',
                      'section=imports', 'view=all&limit=2&cursor=x&unknown=x&extra=x'):
            with self.subTest(query=query):
                status, _, raw = self.request('/api/v1/processing?' + query)
                self.assertEqual(status, 400)
                self.assertNotIn('private-', raw.decode())
        status, _ = self.get('/' + '0'*64)
        self.assertEqual(status, 404)
        status, headers, raw = self.request('/processing.js')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/javascript; charset=utf-8')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn(b'processing', raw)
        self.service.stop.set()
        self.assertEqual(self.get()[0], 503)


if __name__ == '__main__':
    unittest.main()
