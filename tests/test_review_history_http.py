"""Actual loopback routes with append-only review data; no robot/source traffic."""
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.review import ReviewStore
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService


class ReviewHistoryHttpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = DemoSource()
        self.service = HubService(Config(data_dir=str(self.root)), self.source)
        self.addCleanup(self.service.close)
        with sqlite3.connect(self.root / 'catalog.sqlite3') as db:
            store = ReviewStore(db)
            for number in range(105):
                store.record_maintenance(self.record('repair-' + str(number)))
        self.server = create_http_server(self.service, self.source, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.thread.join(3), self.server.server_close()))

    def record(self, identity, **extra):
        return dict(id=identity, robot_id='invented-robot', kind='inspection',
                    reviewer='Synthetic reviewer', rationale='Invented lab fixture',
                    effective_utc_ns='1700000000000000000', **extra)

    def request(self, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def get(self, route='history', **query):
        status, headers, body = self.request('/api/v1/review/' + route + '?' + urlencode(query))
        return status, json.loads(body)

    def test_all_identities_and_exact_revisions_stable_across_concurrent_correction(self):
        status, first = self.get(kind='maintenance', limit='20')
        self.assertEqual(status, 200)
        records = list(first['records'])
        with sqlite3.connect(self.root / 'catalog.sqlite3') as db:
            ReviewStore(db).record_maintenance(self.record('repair-0', revision=2, expected_revision=1))
        page = first
        while page['next_cursor']:
            status, page = self.get(kind='maintenance', limit='20', cursor=page['next_cursor'])
            self.assertEqual(status, 200)
            self.assertEqual(page['snapshot'], first['snapshot'])
            records.extend(page['records'])
        self.assertEqual(len(records), 105)
        self.assertEqual(len({r['id'] for r in records}), 105)
        self.assertEqual(next(r['revision'] for r in records if r['id'] == 'repair-0'), 1)
        status, revisions = self.get(kind='maintenance', record_id='repair-0')
        self.assertEqual([r['revision'] for r in revisions['records']], [2, 1])
        status, exact = self.get('record', kind='maintenance', id='repair-0', revision='1')
        self.assertEqual((status, exact['revision']), (200, 1))
        self.assertEqual(exact['record']['reviewer'], 'Synthetic reviewer')

    def test_strict_queries_fixed_errors_missing_record_and_unavailable_shutdown(self):
        for query in ('kind=maintenance&kind=baseline', 'kind=maintenance&limit=0',
                      'kind=maintenance&limit=51', 'kind=maintenance&cursor=private-path',
                      'kind=maintenance&unknown=secret', 'kind=maintenance&limit=01',
                      'kind=maintenance&record_id=private%2Fpath',
                      'kind=maintenance&limit=1&cursor=x&record_id=x&extra=x'):
            with self.subTest(query=query):
                status, _, raw = self.request('/api/v1/review/history?' + query)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(raw)['error_code'], 'invalid_review_history_request')
                self.assertNotIn('secret', raw.decode())
                self.assertNotIn('private-path', raw.decode())
        status, error = self.get('record', kind='maintenance', id='absent', revision='1')
        self.assertEqual((status, error['error_code']), (404, 'review_record_not_found'))
        self.service.stop.set()
        self.assertEqual(self.get(kind='maintenance')[0], 503)

    def test_schema_change_invalidates_cursor_and_reads_leave_originals_unchanged(self):
        status, first = self.get(kind='maintenance', limit='2')
        with sqlite3.connect(self.root / 'catalog.sqlite3') as db:
            db.execute('CREATE TABLE schema_change_fixture(id INTEGER)')
        status, error = self.get(kind='maintenance', cursor=first['next_cursor'])
        self.assertEqual((status, error['error_code']), (409, 'review_history_changed'))
        database = self.root / 'catalog.sqlite3'
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        for route, query in [('history', dict(kind='maintenance')),
                             ('record', dict(kind='maintenance', id='repair-0', revision='1'))]:
            self.assertEqual(self.get(route, **query)[0], 200)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
        self.assertFalse(list(self.root.glob('rewind/groups/*')))

    def test_history_script_is_explicitly_served_with_no_store_and_safe_mime(self):
        status, headers, body = self.request('/review-history.js')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Content-Type'], 'application/javascript; charset=utf-8')
        self.assertIn(b'history-identities', body)
        status, _, _ = self.request('/review-history.js/other')
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
