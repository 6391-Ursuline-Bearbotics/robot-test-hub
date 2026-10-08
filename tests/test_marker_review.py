"""Independent marker pin, authoritative status and cleanup lifetime regressions."""
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from robot_test_hub.config import Config
from robot_test_hub.demo import DemoSource
from robot_test_hub.marker_bridge import MarkerBridge
from robot_test_hub.marker_delivery import MarkerError, strict_json, validate_ack
from robot_test_hub.marker_service import MarkerConfig, MarkerWorker
from robot_test_hub.notebook import Notebook
from robot_test_hub.server import create_http_server
from robot_test_hub.service import HubService
from robot_test_hub.source_status import StatusInbox
from robot_test_hub.storage import DataRootOwner, OwnershipError
from tests.test_notebook import annotation
from tests.test_marker_delivery import accepted_ack


def wait(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Bounded controlled marker fixture did not complete')


class Transport:
    def __init__(self):
        self.sent = []
    def send(self, wire):
        self.sent.append(wire)
        return True
    def poll(self):
        return None


class MarkerReviewTests(unittest.TestCase):
    def test_large_configuration_numbers_fail_with_fixed_error(self):
        for field in ('freshness', 'ack_timeout', 'retry_initial', 'retry_max'):
            with self.subTest(field=field), self.assertRaises(MarkerError) as error:
                MarkerConfig('localhost', 5810, 'robot-a', 'SIM', **{field: 10 ** 1000})
            self.assertEqual(error.exception.code, 'invalid_marker_configuration')

    def test_actual_status_pair_held_publication_and_new_boot_cannot_retarget(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [10.]
            class Service:
                root = Path(directory)
                settings_lock = threading.Lock()
                stop = threading.Event()
                closed = False
            worker = MarkerWorker(Service(), MarkerConfig('localhost', 5810, 'robot-a', 'SIM'),
                                  clock=lambda: now[0])
            inbox = StatusInbox('robot-a', runtime_mode='SIM', clock=lambda: now[0])
            worker.status_reader = inbox
            worker.transport = Transport()
            book = Notebook(worker.store.path)
            original = annotation()
            book.save(original)
            request = dict(delivery_id='delivery-a', event_id='event-a', note_revision=1,
                annotation_sha256=book.exact_revision('event-a', 1)['sha256'],
                destination_robot_id='robot-a', destination_boot_id='boot-a')
            worker.schedule(request)
            def publish(boot, sequence):
                inbox.receive(json.dumps(dict(schema_version=1, robot_id='robot-a', runtime_mode='SIM',
                    boot_id=boot, sequence=sequence, mode_generation=1, enabled=True,
                    transfer_allowed=False, mode='teleoperated', operating_mode='teleoperated',
                    robot_monotonic_ns=str(sequence * 100000000))))
            publish('boot-a', 1)
            worker.tick()
            self.assertEqual(worker.transport.sent, [], 'first complete publication is not advancing proof')
            publish('boot-a', 2)
            worker.tick()
            self.assertEqual(len(worker.transport.sent), 1, 'enabled passive notes are allowed after proof')
            wire = worker.transport.sent[0]
            self.assertEqual(strict_json(strict_json(wire)['payload_json'])['event_utc_start_ns'],
                             original['event_utc_start_ns'])
            now[0] += 2
            publish('boot-a', 2)  # Repeated cached publication must not renew observation age.
            worker.tick()
            self.assertEqual(len(worker.transport.sent), 1)
            self.assertFalse(worker.snapshot()['status_fresh'])
            inbox.disconnect()
            publish('boot-b', 1)
            worker.tick()
            self.assertEqual(worker.store.get('delivery-a')['delivery']['state'], 'waiting_fresh_status')
            publish('boot-b', 2)
            worker.tick()
            saved = worker.store.get('delivery-a', private=True)
            self.assertEqual(saved['state'], 'historical_boot_ended')
            self.assertEqual(saved['destination_boot_id'], 'boot-a')
            self.assertEqual(saved['wire_json'], wire)
            self.assertEqual(worker.transport.sent, [wire])
            with self.assertRaises(MarkerError):
                worker.retry('delivery-a')

    def test_rejected_ack_cannot_claim_duplicate_admission(self):
        # A rejected message has never admitted this request; contradictory duplicate=true fails.
        job = dict(delivery_id='d', event_id='e', destination_robot_id='r', destination_boot_id='b',
                   annotation_sha256='a' * 64, payload_sha256='b' * 64, note_revision=1,
                   receipt_robot_ns=None)
        ack = dict(accepted_ack(job), state='rejected', reason='capacity', receipt_robot_ns=None,
                   duplicate=True)
        with self.assertRaises(MarkerError):
            validate_ack(ack, job, 'SIM')

    def test_native_blocked_writer_retains_archive_owner_until_complete_cleanup(self):
        entered = threading.Event()
        release = threading.Event()
        exited = threading.Event()
        class Stdin:
            closed = False
            def write(self, data):
                entered.set()
                release.wait()
                return len(data)
            def flush(self): pass
            def close(self): self.closed = True
        class Stdout:
            closed = False
            def __init__(self):
                self.lines = io.BytesIO((json.dumps(dict(type='ready', protocol=1,
                    profile='wpilib-2027.0.0-alpha-7-windowsx86-64',
                    topic='/Telemetry/TestHub/MarkerAck', request_topic='/TestHub/Notebook/MarkerRequest'))
                    + '\n' + json.dumps(dict(type='connected', connection_epoch=1)) + '\n').encode())
            def readline(self, maximum):
                line = self.lines.readline(maximum)
                if line: return line
                exited.wait()
                return b''
            def close(self): self.closed = True
        class Process:
            def __init__(self): self.stdin, self.stdout = Stdin(), Stdout()
            def poll(self): return 0 if exited.is_set() else None
            def terminate(self): exited.set()
            def kill(self): exited.set()
            def wait(self, timeout=None):
                if not exited.wait(timeout): raise TimeoutError()
                return 0
        class Status:
            _threads = []
            def start(self): return self
            def close(self): return True
            def status(self): return StatusInbox('robot-a', runtime_mode='SIM').status()
        process = Process()
        with tempfile.TemporaryDirectory() as directory:
            config = MarkerConfig('localhost', 5810, 'robot-a', 'SIM')
            bridge = MarkerBridge(config, command=['public-controlled-child'])
            service = HubService(Config(data_dir=directory, shutdown_timeout=.1), DemoSource(),
                marker_config=config, marker_status_factory=lambda *a, **k: Status(),
                marker_transport_factory=lambda *a, **k: bridge)
            with patch('robot_test_hub.marker_bridge.subprocess.Popen', return_value=process) as spawn:
                try:
                    service.start()
                    self.assertTrue(entered.wait(3), 'actual writer thread must enter controlled blocking write')
                    self.assertFalse(service.close())
                    self.assertTrue(bridge.thread.is_alive())
                    with self.assertRaises(OwnershipError): DataRootOwner(Path(directory))
                    self.assertEqual(spawn.call_count, 1, 'replacement cannot start while old writer owns pipe')
                    self.assertFalse(process.stdin.closed)
                finally:
                    release.set(); exited.set()
                    wait(lambda: service.close())
                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.stdout.closed)
                self.assertFalse(bridge.thread.is_alive())
            owner = DataRootOwner(Path(directory))
            owner.close()

    def test_closed_root_disk_marker_routes_reject_but_cached_health_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            service = HubService(Config(data_dir=directory), DemoSource())
            book = Notebook(service.root / 'catalog.sqlite3'); book.save(annotation())
            server = create_http_server(service, service.source, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                self.assertTrue(service.close())
                def get(path):
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=2)
                    try:
                        connection.request('GET', path)
                        response = connection.getresponse(); return response.status, response.read()
                    finally: connection.close()
                self.assertEqual(get('/api/v1/markers')[0], 200)
                for path in ('/api/v1/markers/deliveries',
                             '/api/v1/markers/annotations/event-a?revision=1',
                             '/api/v1/markers/deliveries/missing'):
                    self.assertEqual(get(path)[0], 503)
            finally:
                server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__':
    unittest.main()
