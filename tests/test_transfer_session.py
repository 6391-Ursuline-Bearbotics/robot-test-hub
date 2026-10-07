import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.transfer_session import (MAX_BODY, SnapshotUnavailable, capture,
                                             fetch_status, main, project_status)


def snapshot(**changes):
    value = dict(schema_version=1, state='downloading', source_type='systemcore_sftp_unqualified',
        source_status=dict(enabled=False, transfer_allowed=True, fresh=True, age_seconds=.03),
        paused_by_operator=False,
        remaining_bytes=1000, pending_files=2, bytes_per_second=123, eta_seconds=8,
        connection=dict(status_reader=dict(restart_count=0, consecutive_failures=0,
            restart_pending=False, reader_running=True, retry_in_seconds=None, error_code=None)))
    value.update(changes)
    return value


class Clock:
    def __init__(self): self.ns = 0
    def now(self): return self.ns
    def utc(self): return 1800000000000000000 + self.ns
    def sleep(self, seconds): self.ns += round(seconds * 1e9)


class TransferSessionTests(unittest.TestCase):
    def test_incomplete_response_closed_after_http_socket_ownership_transfer(self):
        with patch('robot_test_hub.transfer_session.http.client.HTTPConnection') as factory:
            connection = factory.return_value
            wire_socket = connection.sock
            response = connection.getresponse.return_value
            response.status = 200
            response.length = None
            response.isclosed.return_value = False
            def take_socket():
                connection.sock = None
                return response
            connection.getresponse.side_effect = take_socket
            response.read1.return_value = b'x'*(MAX_BODY+1)
            with self.assertRaises(SnapshotUnavailable):fetch_status(6391)
            response.close.assert_called_once()
            connection.close.assert_called_once()
            wire_socket.settimeout.assert_called_once()

    def test_last_declared_body_read_closes_socket_without_masking_json_result(self):
        for body, remaining_bytes, expected_error in ((json.dumps(snapshot()).encode(), 0, None),
                                     (b'{"invalid":NaN}', 0, 'invalid_json'),
                                     (json.dumps(snapshot()).encode(), 1, 'local_http_unavailable')):
            with self.subTest(expected_error=expected_error), \
                 patch('robot_test_hub.transfer_session.http.client.HTTPConnection') as factory:
                connection = factory.return_value
                wire_socket = connection.sock
                response = connection.getresponse.return_value
                response.status = 200
                response.length = len(body)
                closed = False
                response.isclosed.side_effect = lambda:closed
                def set_timeout(value):
                    if closed:raise OSError('Bad file descriptor')
                wire_socket.settimeout.side_effect = set_timeout
                def read_last(length):
                    nonlocal closed
                    closed = True
                    response.length = remaining_bytes
                    return body
                response.read1.side_effect = read_last
                if expected_error is None:
                    self.assertEqual(fetch_status(6391), snapshot())
                else:
                    with self.assertRaises(SnapshotUnavailable) as raised:fetch_status(6391)
                    self.assertEqual(str(raised.exception), expected_error)
                response.read1.assert_called_once()
                wire_socket.settimeout.assert_called_once()
                response.close.assert_called_once()
                connection.close.assert_called_once()

    def test_connection_closed_even_when_response_close_fails(self):
        with patch('robot_test_hub.transfer_session.http.client.HTTPConnection') as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            response.status = 302
            response.close.side_effect = OSError('private cleanup detail')
            with self.assertRaises(OSError):fetch_status(6391)
            connection.close.assert_called_once()

    def test_valid_json_prefix_then_empty_read_rejects_declared_truncation(self):
        with patch('robot_test_hub.transfer_session.http.client.HTTPConnection') as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value
            body = json.dumps(snapshot()).encode()
            response.status = 200
            response.length = len(body) + 1
            closed = False
            reads = 0
            response.isclosed.side_effect = lambda:closed
            def read_prefix_then_eof(length):
                nonlocal closed, reads
                reads += 1
                if reads == 1:
                    response.length = 1
                    return body
                closed = True
                return b''
            response.read1.side_effect = read_prefix_then_eof
            with self.assertRaises(SnapshotUnavailable) as raised:fetch_status(6391)
            self.assertEqual(str(raised.exception), 'local_http_unavailable')
            self.assertEqual(response.read1.call_count, 2)
            response.close.assert_called_once()
            connection.close.assert_called_once()

    def run_capture(self, path, values, **kwargs):
        clock = kwargs.pop('clock', Clock())
        iterator = iter(values)
        def fetch(port, timeout):
            self.assertEqual(port, 6391)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 2)
            value = next(iterator)
            if isinstance(value, BaseException): raise value
            return value
        return capture(path, duration=len(values), interval=1, fetch=fetch,
                       monotonic_ns=clock.now, utc_ns=clock.utc, sleep=clock.sleep, **kwargs)

    def test_redacted_samples_summary_and_missing_duration(self):
        private = dict(snapshot(), reason='secret /archive', active_id='secret-id', files=[{'id':'secret'}],
                       endpoint='secret-host', backup={'root':'secret-path'})
        private['connection']['private_key'] = 'secret-key'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session.jsonl'
            summary = self.run_capture(path, [private, snapshot(state='paused'),
                RuntimeError('secret provider detail'), snapshot(state='caught_up', remaining_bytes=50)])
            saved = [json.loads(line) for line in path.read_text().splitlines()]
            records = saved[:-1]
            self.assertEqual(saved[-1], summary)
            self.assertEqual(saved[-1]['completion'], 'complete')
            self.assertEqual([r['elapsed_monotonic_ns'] for r in records], [0,10**9,2*10**9,3*10**9])
            self.assertEqual(records[0]['utc_ns'], 1800000000000000000)
            self.assertEqual(summary['polling_seconds'], dict(active=1., paused=1., unavailable=1., other=1.))
            self.assertEqual(summary['valid_snapshots'], 3)
            self.assertEqual(summary['missing_snapshots'], 1)
            self.assertEqual(summary['partial_snapshots'], 3)
            self.assertEqual(summary['queue_changes'], 1)
            self.assertEqual(summary['last_queue_bytes'], 50)
            self.assertEqual(summary['reported_rate_max_bytes_per_second'], 123)
            combined = path.read_text() + json.dumps(summary)
            self.assertNotIn('secret', combined)
            self.assertFalse(summary['cumulative_durable_bytes_available'])
            self.assertFalse(summary['cancellation_tail_measured'])

    def test_source_transition_and_reader_counter_reset_do_not_infer_bytes(self):
        values = [snapshot(), snapshot(), snapshot(source_type='synthetic_demo', remaining_bytes=0), snapshot()]
        values[1]['connection']['status_reader']['restart_count'] = 2
        values[3]['connection']['status_reader']['restart_count'] = 10
        with tempfile.TemporaryDirectory() as directory:
            summary = self.run_capture(Path(directory)/'session', values)
            self.assertEqual(summary['source_transitions'], 2)
            self.assertEqual(summary['reader_restarts_observed'], 2)
            self.assertFalse(summary['boot_changes_observable'])
            self.assertNotIn('downloaded_bytes', summary)
            values = [snapshot(), snapshot()]
            values[0]['connection']['status_reader']['restart_count'] = 5
            summary = self.run_capture(Path(directory)/'reset', values)
            self.assertEqual(summary['reader_counter_resets'], 1)
            self.assertEqual(summary['reader_restarts_observed'], 0)

    def test_malformed_safe_fields_rejected_and_unrecognized_fields_dropped(self):
        for changes in ({'state':'secret-path'}, {'source_type':'secret-endpoint'},
                        {'remaining_bytes':True}, {'eta_seconds':float('nan')},
                        {'pending_files':-1}, {'schema_version':True}, {'eta_seconds':10**1000}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                project_status(snapshot(**changes))
        with self.assertRaises(ValueError):
            project_status(snapshot(source_status=dict(fresh=True, enabled=None, transfer_allowed=True)))
        value = snapshot();value['connection']['status_reader']['error_code'] = 'secret exception'
        self.assertEqual(project_status(value)['status_reader']['error_code'], 'other_status_error')
        self.assertNotIn('unrecognized', project_status(snapshot(unrecognized={'path':'secret'})))

    def test_bounds_fail_before_output_and_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session'
            for options in ({'port':True}, {'port':65536}, {'duration':float('inf')},
                            {'duration':.5}, {'interval':float('nan')}, {'interval':0},
                            {'duration':86400,'interval':.1}):
                with self.subTest(options=options), self.assertRaises(ValueError):capture(path, **options)
                self.assertFalse(path.exists())
            path.write_bytes(b'original')
            with self.assertRaises(FileExistsError):capture(path)
            self.assertEqual(path.read_bytes(), b'original')

    def test_interrupt_keeps_flushed_records_and_output_private_on_posix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session'
            with self.assertRaises(KeyboardInterrupt):
                self.run_capture(path, [snapshot(), KeyboardInterrupt()])
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]['record_type'], 'sample')
            self.assertEqual(records[1]['completion'], 'interrupted')
            if os.name != 'nt':self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_interrupt_summary_accounts_first_fetch_next_fetch_and_final_sleep(self):
        for stage, expected, samples in (('first', .3, 0), ('next', 1.5, 1), ('sleep', .4, 1)):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                clock = Clock();calls = 0
                def fetch(port, timeout):
                    nonlocal calls
                    calls += 1
                    if stage == 'first' or (stage == 'next' and calls == 2):
                        clock.sleep(.3 if stage == 'first' else .5)
                        raise KeyboardInterrupt()
                    return snapshot()
                def sleep(seconds):
                    if stage == 'sleep':
                        clock.sleep(.4)
                        raise KeyboardInterrupt()
                    clock.sleep(seconds)
                path = Path(directory)/'session'
                with self.assertRaises(KeyboardInterrupt):
                    capture(path, duration=2 if stage == 'next' else 1, interval=1,
                        fetch=fetch,monotonic_ns=clock.now,utc_ns=clock.utc,sleep=sleep)
                saved = [json.loads(line) for line in path.read_text().splitlines()]
                summary = saved[-1]
                self.assertEqual(summary['completion'], 'interrupted')
                self.assertEqual(summary['samples'], samples)
                self.assertAlmostEqual(summary['elapsed_seconds'], expected)
                self.assertAlmostEqual(sum(summary['polling_seconds'].values()), expected)
                self.assertAlmostEqual(summary['polling_seconds']['unavailable' if stage == 'first' else 'active'], expected)

    def test_fetch_latency_is_part_of_elapsed_and_unavailable_until_first_success(self):
        clock = Clock()
        def fetch(port, timeout):
            clock.sleep(.25)
            return snapshot()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session'
            summary = capture(path, duration=2, interval=1, fetch=fetch, monotonic_ns=clock.now,
                              utc_ns=clock.utc, sleep=clock.sleep)
            records = [json.loads(line) for line in path.read_text().splitlines()][:-1]
            self.assertEqual([r['elapsed_monotonic_ns'] for r in records], [250000000,1250000000])
            self.assertEqual(summary['elapsed_seconds'], 2)
            self.assertEqual(summary['polling_seconds']['unavailable'], .25)

    def test_clock_reversal_is_explicit(self):
        clock = Clock();calls = 0
        def fetch(port, timeout):
            nonlocal calls
            calls += 1
            if calls == 2:clock.ns -= 2*10**9
            return snapshot()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session'
            summary = capture(path, duration=2, interval=1, fetch=fetch,
                monotonic_ns=clock.now, utc_ns=clock.utc, sleep=clock.sleep)
            self.assertEqual(summary['monotonic_clock_resets'], 1)
            self.assertEqual(summary['utc_clock_reversals'], 1)
            self.assertTrue(json.loads(path.read_text().splitlines()[1])['monotonic_clock_reset'])

    def test_contradictory_or_incomplete_download_status_is_not_active(self):
        enabled = snapshot(source_status=dict(fresh=True, enabled=True, transfer_allowed=False))
        paused = snapshot(paused_by_operator=True)
        unknown = snapshot();unknown.pop('paused_by_operator')
        with tempfile.TemporaryDirectory() as directory:
            summary = self.run_capture(Path(directory)/'capture', [enabled, paused, unknown])
            self.assertEqual(summary['polling_seconds']['active'], 0)
            self.assertEqual(summary['polling_seconds']['paused'], 2)
            self.assertEqual(summary['polling_seconds']['unavailable'], 1)

    def test_json_cli_and_redacted_failures(self):
        with patch('robot_test_hub.transfer_session.capture', return_value={'samples':1}) as operation:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):self.assertEqual(main(['--output','private','--json']), 0)
            self.assertEqual(json.loads(stdout.getvalue()), {'samples':1})
            operation.assert_called_once_with('private',port=6391,duration=60.,interval=1.)
        with patch('robot_test_hub.transfer_session.capture', side_effect=OSError('secret-path')):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):self.assertEqual(main(['--output','secret-path']), 2)
            self.assertNotIn('secret', stderr.getvalue())

    def test_final_sync_failure_reports_redacted_error_and_preserves_exclusive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'session'
            clock = Clock()
            def operation(output, **options):
                return capture(output, **options, fetch=lambda port, timeout:snapshot(),
                    monotonic_ns=clock.now, utc_ns=clock.utc, sleep=clock.sleep)
            stderr = io.StringIO()
            with patch('robot_test_hub.transfer_session.capture', side_effect=operation), \
                 patch('robot_test_hub.transfer_session.os.fsync', side_effect=OSError('secret disk path')), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(main(['--output',str(path),'--duration','1']), 2)
            self.assertNotIn('secret', stderr.getvalue())
            original = path.read_bytes()
            self.assertTrue(original)
            self.assertEqual(json.loads(original.splitlines()[-1])['completion'], 'complete')
            with self.assertRaises(FileExistsError):operation(path,port=6391,duration=1,interval=1)
            self.assertEqual(path.read_bytes(), original)


class LoopbackHTTPTests(unittest.TestCase):
    def setUp(self):
        owner = self
        self.body = json.dumps(snapshot()).encode();self.status = 200;self.requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                self.send_response(owner.status)
                self.send_header('Content-Length', str(len(owner.body)))
                self.send_header('Location', 'http://secret-remote.invalid/private')
                self.end_headers()
                try:self.wfile.write(owner.body)
                except (ConnectionError, OSError):pass
            def log_message(self, *args):pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1',0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01})
        self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
    def test_actual_loopback_capture_ignores_proxy_and_never_controls_hub(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,
                {'HTTP_PROXY':'http://secret-remote.invalid','http_proxy':'http://secret-remote.invalid'}):
            path = Path(directory)/'session'
            summary = capture(path,port=self.server.server_port,duration=1,interval=.5,
                monotonic_ns=clock.now,utc_ns=clock.utc,sleep=clock.sleep)
            self.assertEqual(summary['valid_snapshots'], 2)
            self.assertEqual(self.requests, ['/api/v1/status']*2)
            self.assertNotIn('secret',path.read_text())
    def test_redirect_oversize_duplicate_and_nonfinite_are_redacted(self):
        for status, body, code in ((302,b'secret', 'http_status'),
                                  (200,b'x'*(MAX_BODY+1),'response_too_large'),
                                  (200,b'{"a":1,"a":2}','invalid_json'),
                                  (200,b'{"a":NaN}','invalid_json'),
                                  (200,b'\xffsecret','invalid_json')):
            self.status,self.body = status,body
            with self.subTest(code=code), self.assertRaises(SnapshotUnavailable) as raised:
                fetch_status(self.server.server_port)
            self.assertEqual(str(raised.exception), code)
        self.assertEqual(self.requests, ['/api/v1/status']*5)

    def test_actual_hub_service_versioned_status_projection(self):
        from robot_test_hub.config import Config
        from robot_test_hub.demo import DemoSource
        from robot_test_hub.server import create_http_server
        from robot_test_hub.service import HubService
        with tempfile.TemporaryDirectory() as directory:
            source = DemoSource()
            service = HubService(Config(data_dir=str(Path(directory)/'archive')), source)
            server = create_http_server(service, source, 0)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01})
            thread.start()
            try:
                clock = Clock()
                path = Path(directory)/'capture.jsonl'
                summary = capture(path, port=server.server_port, duration=1, interval=1,
                    monotonic_ns=clock.now, utc_ns=clock.utc, sleep=clock.sleep)
                self.assertEqual(summary['valid_snapshots'], 1)
                record = json.loads(path.read_text().splitlines()[0])
                self.assertEqual(record['snapshot']['source_type'], 'synthetic_demo')
                self.assertFalse(record['snapshot']['source_status']['fresh'])
                self.assertEqual(summary['polling_seconds']['unavailable'], 1)
                self.assertNotIn(directory, path.read_text())
                self.assertNotIn('demo-boot', path.read_text())
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2)
                self.assertTrue(service.close())


if __name__ == '__main__':unittest.main()
