"""Phone notes listener: PIN, cookie session, wrong-PIN limit, notebook-only surface."""
import http.client
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from robot_test_hub.lan_notes import LanNotes, FAIL_LIMIT
from robot_test_hub.notebook import Notebook


def note():
    now = str(time.time_ns())
    return {'schema_version': 1, 'event_id': 'phone-' + uuid.uuid4().hex[:12], 'revision': 1, 'submitted_utc_ns': now,
            'client_monotonic_ns': None, 'event_utc_start_ns': now, 'event_utc_end_ns': now, 'when': {'kind': 'now'},
            'uncertainty_ms': 1000, 'clock_domain': 'phone', 'clock_quality': 'unverified_client', 'text': 'intake jammed',
            'tags': ['intake'], 'source': 'phone', 'author': 'Ava'}


class LanNotesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.book = Notebook(Path(folder.name) / 'catalog.sqlite3')
        self.clock = [0.0]
        self.lan = LanNotes(self.book, host='127.0.0.1', port=0, clock=lambda: self.clock[0])
        self.lan.start(); self.addCleanup(self.lan.close)

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.lan.port, timeout=5)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def login(self, pin):
        return self.call('POST', '/pin', ('pin=' + pin).encode(), {'Content-Type': 'application/x-www-form-urlencoded'})

    def test_pin_cookie_then_save_and_list_notes(self):
        self.assertEqual(self.call('GET', '/notebook')[0], 303)
        self.assertEqual(self.call('GET', '/api/v1/annotations')[0], 401)
        status, headers, _ = self.login(self.lan.pin)
        self.assertEqual(status, 303)
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertEqual(self.call('GET', '/notebook', headers={'Cookie': cookie})[0], 200)
        payload = json.dumps(note()).encode()
        status, _, _ = self.call('POST', '/api/v1/annotations', payload,
                                 {'Cookie': cookie, 'Content-Type': 'application/json', 'X-Hub-Request': '1'})
        self.assertEqual(status, 201)
        self.assertEqual(self.call('POST', '/api/v1/annotations', payload, {'Cookie': cookie, 'Content-Type': 'application/json'})[0], 403)
        status, _, body = self.call('GET', '/api/v1/annotations', headers={'Cookie': cookie})
        self.assertEqual(json.loads(body)['annotations'][0]['text'], 'intake jammed')

    def test_only_the_notebook_is_reachable(self):
        cookie = self.login(self.lan.pin)[1]['Set-Cookie'].split(';')[0]
        for path in ('/admin', '/api/v1/status', '/api/v1/roster', '/runs', '/api/v1/import-folder'):
            self.assertEqual(self.call('GET', path, headers={'Cookie': cookie})[0], 404, path)
        self.assertEqual(self.call('POST', '/api/v1/import-folder', b'{}', {'Cookie': cookie, 'Content-Type': 'application/json',
                                                                            'X-Hub-Request': '1'})[0], 403)

    def test_wrong_pins_are_limited_per_address(self):
        wrong = '000000' if self.lan.pin != '000000' else '111111'
        for _ in range(FAIL_LIMIT):
            self.assertEqual(self.login(wrong)[0], 403)
        self.assertEqual(self.login(self.lan.pin)[0], 429)
        self.clock[0] += 61
        self.assertEqual(self.login(self.lan.pin)[0], 303)

    def test_robot_network_address_is_listed_first(self):
        from robot_test_hub.lan_notes import address_rank
        addresses = ['172.24.32.1', '192.168.86.47', '10.63.91.5', '10.0.0.4']
        self.assertEqual(sorted(addresses, key=address_rank), ['10.63.91.5', '10.0.0.4', '192.168.86.47', '172.24.32.1'])

    def test_qr_svg_for_url(self):
        from robot_test_hub import qr
        self.assertTrue(qr.svg('http://192.168.1.23:6392/').startswith('<svg'))


if __name__ == '__main__':
    unittest.main()
