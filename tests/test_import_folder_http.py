"""Loopback requests for the dashboard's Import logs from a folder action."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from robot_test_hub.config import Config
from robot_test_hub.importer import Importer
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.storage import open_catalog
from test_processing_http import GeneratedRecordingSource

FIXTURE = Path(__file__).parent / 'fixtures' / 'synthetic' / 'alpha7-main.wpilog'


class FolderImportHttpTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.root, self.usb = base / 'data', base / 'usb'
        self.usb.mkdir()
        shutil.copy(FIXTURE, self.usb / 'practice.wpilog')
        self.source = GeneratedRecordingSource()
        self.service = HubService(Config(data_dir=str(self.root)), self.source)
        self.addCleanup(self.service.close)
        self.service._health('indexer', 'running')
        self.server = create_http_server(self.service, self.source, 0)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.thread.join(3), self.server.server_close()))

    def request(self, method, payload=None, headers=None):
        settings = {'Content-Type': 'application/json', 'X-Hub-Request': '1'}
        settings.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, '/api/v1/import-folder',
                               body=json.dumps(payload).encode() if payload is not None else None, headers=settings)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_queued_import_runs_on_indexer_and_reports_progress(self):
        self.assertEqual(self.request('GET'), (200, self.service.folder_import_status()))
        status, body = self.request('POST', {'folder': str(self.usb)})
        self.assertEqual(status, 202)
        self.assertEqual((body['state'], body['total']), ('queued', 1))
        self.assertEqual(self.request('POST', {'folder': str(self.usb)})[0], 409)
        with closing(open_catalog(self.root)) as db:
            self.service._run_folder_import(db)
            self.assertEqual([job['state'] for job in Importer(self.root, db).list_jobs()], ['succeeded'])
        status, body = self.request('GET')
        self.assertEqual((body['state'], body['done'], body['counts']), ('complete', 1, {'imported': 1}))

    def test_rejects_bad_folders_and_cross_site_requests(self):
        for payload, code in (({'folder': ''}, 'folder_required'),
                              ({'folder': 'relative/logs'}, 'folder_must_be_absolute'),
                              ({'folder': str(self.usb / 'missing')}, 'folder_not_found'),
                              ({'folder': str(self.root)}, 'folder_inside_hub_data'),
                              ({'folder': str(self.usb), 'extra': 1}, 'invalid_import_request')):
            self.assertEqual(self.request('POST', payload), (400, {'error_code': code}), payload)
        self.assertEqual(self.request('POST', {'folder': str(self.usb)}, {'X-Hub-Request': '0'})[0], 403)
        self.assertEqual(self.service.folder_import_status()['state'], 'idle')


if __name__ == '__main__':
    unittest.main()
