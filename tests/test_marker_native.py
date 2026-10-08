"""Optional exact robot producer/native hub proof; all data is public invented SIM data."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.marker_service import MarkerConfig
from robot_test_hub.notebook import Notebook
from robot_test_hub.service import HubService
from robot_test_hub.wpilog import records, _control, decode
from tests.test_notebook import annotation


@unittest.skipUnless(os.environ.get('ROBOT_HUB_MARKER_FIXTURE_CLASSPATH'),
                     'Exact sibling robot SIM producer fixture not configured')
class NativeMarkerTests(unittest.TestCase):
    def test_enabled_paused_collector_actual_robot_admission_preserves_event_time(self):
        java = os.environ['ROBOT_HUB_MARKER_JAVA']
        native = os.environ['ROBOT_HUB_MARKER_NATIVE_DIR']
        classpath = Path(os.environ['ROBOT_HUB_MARKER_FIXTURE_CLASSPATH']).read_text().strip()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as bound:
                bound.bind(('127.0.0.1', 0)); port = bound.getsockname()[1]
            env = dict(os.environ, PATH=native + os.pathsep + os.environ.get('PATH', ''))
            command = [java, '--enable-native-access=ALL-UNNAMED', '-Djava.library.path=' + native,
                       '-cp', classpath, 'org.littletonrobotics.junction.MarkerLoopbackFixture',
                       str(port), str(root), '15']
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            service = None
            try:
                service = HubService(Config(data_dir=str(root/'hub')), DemoSource(),
                                     marker_config=MarkerConfig('127.0.0.1', port, '6391-practice', 'SIM'))
                service.set_paused(True)
                service.start()
                deadline = time.monotonic() + 10
                while not service.markers.snapshot()['ready_for_delivery'] and time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), 'actual SIM producer exited before readiness')
                    time.sleep(.02)
                health = service.markers.snapshot()
                self.assertTrue(health['ready_for_delivery'])
                note = annotation()
                book = Notebook(service.root/'catalog.sqlite3'); book.save(note)
                exact = book.exact_revision(note['event_id'], 1)
                request = dict(delivery_id='native-delivery', event_id=note['event_id'], note_revision=1,
                               annotation_sha256=exact['sha256'], destination_robot_id='6391-practice',
                               destination_boot_id=health['current_confirmed_boot_id'])
                service.markers.schedule(request)
                while time.monotonic() < deadline:
                    result = service.markers.store.get('native-delivery')['delivery']
                    if result['state'] == 'robot_acknowledged': break
                    time.sleep(.02)
                self.assertEqual(result['state'], 'robot_acknowledged')
                self.assertTrue(service.paused.is_set())
                self.assertEqual(result['usb_durability'], 'unavailable')
                self.assertIsNotNone(result['receipt_robot_ns'])
                self.assertEqual(book.exact_revision(note['event_id'], 1)['payload_json'], exact['payload_json'])
                wire = service.markers.store.get('native-delivery', private=True)['wire_json']
                self.assertIn(note['event_utc_start_ns'], wire)
            finally:
                if service is not None: service.close()
                try: process.wait(timeout=18)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertTrue((root/'marker-proof.wpilog').is_file())
            active, generations, saved_markers = {}, {}, []
            with (root/'marker-proof.wpilog').open('rb') as stream:
                for record in records(stream):
                    if record.entry == 0:
                        _control(record, active, generations)
                    elif active[record.entry]['field'] == '/TestHub/Notebook/AcceptedEnvelopes':
                        saved_markers.extend(decode(active[record.entry]['type'], record.payload, None))
            self.assertEqual(len(saved_markers), 1)
            saved = json.loads(saved_markers[0])
            self.assertEqual(saved['request_json'], wire)
            self.assertEqual(saved['receipt_robot_ns'], result['receipt_robot_ns'])
            projected = json.loads(json.loads(saved['request_json'])['payload_json'])
            self.assertEqual(projected['event_utc_start_ns'], note['event_utc_start_ns'])
            self.assertNotIn('author', projected)
            self.assertNotIn('device_id', projected)
