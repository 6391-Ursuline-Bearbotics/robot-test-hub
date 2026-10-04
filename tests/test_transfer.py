import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest

from robot_test_hub.transfer import BoundedIO, Cancellation, ManifestPage, Throughput, TransferCancelled, TransferTimeout


class TransferPrimitiveTests(unittest.TestCase):
    def test_time_window_rate_and_paused_estimate(self):
        rate = Throughput(window=10, minimum=2)
        rate.add(1, 100, 10)
        self.assertIsNone(rate.rate)
        rate.add(1, 100, 11)
        self.assertEqual(rate.rate, 100)
        result = rate.estimate(300, 200, active=False, complete=True, blocked=False)
        self.assertEqual(result['historical_eta_seconds'], 3)
        self.assertEqual(result['eta_basis'], 'paused_historical')
        self.assertIsNone(result['eta_seconds'])
        rate.add(10, 2000, 201)
        self.assertEqual(rate.rate, 200)

    def test_discovery_lower_bound_and_blocked_never_finish(self):
        rate = Throughput()
        rate.add(2, 200, 10)
        partial = rate.estimate(100, 10, active=True, complete=False, blocked=False)
        self.assertTrue(partial['eta_is_lower_bound'])
        self.assertEqual(partial['eta_basis'], 'lower_bound')
        blocked = rate.estimate(100, 10, active=True, complete=True, blocked=True)
        self.assertIsNone(blocked['eta_seconds'])
        self.assertIsNone(blocked['historical_eta_seconds'])

    def test_long_reconnection_invalidates_current_rate(self):
        rate = Throughput(historical_max_age=30)
        rate.add(2, 200, 10)
        result = rate.estimate(100, 50, active=True, complete=True, blocked=False)
        self.assertIsNone(result['bytes_per_second'])
        self.assertIsNone(result['historical_bytes_per_second'])

    def test_stalled_transport_retains_single_outstanding_slot(self):
        io = BoundedIO()
        release = threading.Event()
        token = Cancellation(('boot', 1), time.monotonic() + 10)
        try:
            with self.assertRaises(TransferTimeout):
                io.call(lambda: release.wait(5), token, 0.03, length=4)
            self.assertTrue(io.busy)
            self.assertEqual(io.maximum_bytes, 4)
            with self.assertRaises(TransferTimeout):
                io.call(lambda: b'next', token, 0.03, length=4)
        finally:
            release.set()
            io.thread.join(1)
        self.assertFalse(io.busy)
        self.assertEqual(io.outstanding_bytes, 0)

    def test_permission_revocation_ends_client_wait(self):
        io = BoundedIO()
        token = Cancellation(('boot', 1), time.monotonic() + 10)
        try:
            with self.assertRaises(TransferCancelled):
                io.call(lambda: token.event.wait(5), token, 1, permitted=lambda: False)
        finally:
            token.cancel()
            if io.thread is not None:
                io.thread.join(1)

from robot_test_hub.collector import Collector, LogFile, RobotStatus
from robot_test_hub.service import HubService, CachedSource
from robot_test_hub.config import Config, ConfigError
from robot_test_hub.transfer import AuthenticationError, IdentityError, SourceMissing, TransferError
from test_foundation import wait_for


class ControlledTransport:
    def __init__(self, count=1):
        self.now = 100.0
        self.enabled = False
        self.generation = 0
        self.allowed = True
        self.boot = 'boot'
        self.profile = 'wifi'
        self.data = {f'file-{i}': bytes([65 + i]) * 12 for i in range(count)}
        self.files = tuple(LogFile(identity, identity + '.demo', len(data), hashlib.sha256(data).hexdigest(), float(i), relative_path=identity + '.demo') for i, (identity, data) in enumerate(self.data.items()))
        self.reads = []
        self.pages = []
        self.revision = 'revision-1'
        self.faults = {}
        self.open_bytes = 0
        self.pending_bytes = 0
        self.fail_discovery = False
        self.after_read = None

    def status(self):
        return RobotStatus(self.enabled, self.now, self.boot, self.generation, self.allowed, link_profile=self.profile)

    def discover_closed(self, cursor, limit, cancellation):
        cancellation.check()
        if self.fail_discovery:
            raise TransferError('Network loss')
        self.pages.append(cursor)
        offset = int(cursor or 0)
        values = self.files[offset:offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(self.files) else None
        return ManifestPage(self.revision, values, next_cursor, next_cursor is None, self.open_bytes, self.pending_bytes)

    def read(self, identity, offset, length, *, permission_generation, cancellation):
        cancellation.check()
        self.reads.append((identity, offset, length, permission_generation))
        fault = self.faults.get(identity)
        if fault:
            raise fault
        self.now += 1
        if self.after_read:
            self.after_read()
        return self.data[identity][offset:offset + length]


class ProductionTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = ControlledTransport()
        self.collector = self.make()

    def make(self, **kwargs):
        return Collector(self.root, self.source, idle_delay=0, chunk_size=4, clock=lambda: self.source.now,
                         jitter=lambda: 1, eta_minimum=2, **kwargs)

    def tearDown(self):
        self.collector.close()
        self.tmp.cleanup()

    def rows(self):
        return {r['id']: r for r in self.collector.snapshot()['files']}

    def test_paged_manifest_persisted_and_incomplete_never_caught_up(self):
        self.collector.close()
        self.source = ControlledTransport(3)
        self.collector = self.make(discovery_page_size=1)
        self.source.open_bytes = 50
        self.collector.tick()
        snap = self.collector.snapshot()
        self.assertFalse(snap['discovery_complete'])
        self.assertEqual(snap['pending_files'], 1)
        self.assertEqual(snap['open_bytes'], 50)
        self.assertIsNone(snap['eta_seconds'])
        self.assertEqual(self.source.reads, [])
        self.collector.tick()
        self.collector.tick()
        self.assertEqual(self.source.pages, [None, '1', '2'])
        self.assertTrue(self.collector.snapshot()['discovery_complete'])
        self.assertEqual(self.collector.db.execute('SELECT COUNT(*) FROM manifest_items').fetchone()[0], 3)
        self.assertEqual(self.collector.db.execute('SELECT complete FROM manifest_snapshots').fetchone()[0], 1)

    def test_source_permission_gate_and_future_receipt(self):
        self.source.allowed = False
        self.collector.tick()
        self.assertEqual(self.source.pages, [])
        self.source.allowed = True
        self.source.status = lambda: RobotStatus(False, self.source.now + 1, 'boot', 0)
        self.collector.tick()
        self.assertEqual(self.source.pages, [])

    def test_generation_discards_modern_inflight_block(self):
        self.collector.tick()
        self.source.after_read = lambda: setattr(self.source, 'generation', 2)
        self.collector.tick()
        self.assertEqual(self.rows()['file-0']['offset'], 4)
        self.assertEqual(self.collector.db.execute("SELECT COUNT(*) FROM transfer_events WHERE code='permission_revoked'").fetchone()[0], 1)

    def test_partial_corruption_quarantined_at_startup(self):
        self.collector.tick()
        (self.root / 'partial/file-0.part').write_bytes(b'bad!')
        self.collector.close()
        self.collector = self.make()
        self.assertEqual(self.rows()['file-0']['offset'], 0)
        self.assertTrue((self.root / 'partial/file-0.invalid').exists())
        self.collector.tick()
        self.assertEqual(self.source.reads[-1][1], 0)

    def test_backoff_does_not_starve_other_files_and_is_persisted(self):
        self.collector.close()
        self.source = ControlledTransport(3)
        self.source.faults['file-2'] = TransferError('Network loss')
        self.collector = self.make(retry_initial=10)
        self.collector.tick()
        self.assertEqual(self.rows()['file-2']['attempts'], 1)
        self.collector.tick()
        self.assertEqual(self.source.reads[-1][0], 'file-1')
        self.assertGreater(self.rows()['file-2']['next_retry'], time.time())
        self.source.now += 11
        self.source.faults.clear()
        for _ in range(15):
            self.collector.tick()
        self.assertTrue(all(r['state'] == 'complete' for r in self.rows().values()))

    def test_nontransient_auth_error_requires_explicit_retry(self):
        self.source.faults['file-0'] = AuthenticationError('Host identity mismatch')
        self.collector.tick()
        self.assertEqual(self.rows()['file-0']['state'], 'error')
        self.source.now += 100
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 1)
        self.assertIsNone(self.collector.snapshot()['eta_seconds'])
        self.source.faults.clear()
        self.collector.retry_errors('file-0')
        for _ in range(4):
            self.collector.tick()
        self.assertEqual(self.rows()['file-0']['state'], 'complete')

    def test_missing_file_preserves_partial_and_blocks_finish(self):
        self.collector.tick()
        self.source.files = ()
        self.source.now += 5
        self.collector.tick()
        snap = self.collector.snapshot()
        self.assertEqual(snap['blocked_bytes'], 8)
        self.assertEqual(snap['blocked_files'], 1)
        self.assertIsNone(snap['eta_seconds'])
        self.assertEqual((self.root / 'partial/file-0.part').read_bytes(), b'AAAA')

    def test_oldest_selected_after_two_recent_files(self):
        self.collector.close()
        self.source = ControlledTransport(5)
        self.collector = self.make()
        for _ in range(9):
            self.collector.tick()
        selected = [r[0] for r in self.source.reads][::3]
        self.assertEqual(selected, ['file-4', 'file-3', 'file-0'])

    def test_urgent_priority_preempts_at_checkpoint(self):
        self.collector.close()
        self.source = ControlledTransport(2)
        self.collector = self.make()
        self.collector.tick()
        self.collector.set_priority('file-0', 100, urgent=True)
        self.collector.tick()
        self.assertEqual([r[0] for r in self.source.reads], ['file-1', 'file-0'])
        self.assertEqual(self.rows()['file-1']['offset'], 4)

    def test_discovery_revision_switch_is_visible_and_persisted(self):
        self.collector.close()
        self.source = ControlledTransport(3)
        self.collector = self.make(discovery_page_size=1)
        self.collector.tick()
        self.source.revision = 'revision-2'
        self.collector.tick()
        self.assertEqual(self.collector.snapshot()['state'], 'attention')
        self.assertFalse(self.collector.snapshot()['discovery_complete'])
        self.assertEqual(self.source.reads, [])

    def test_checksum_failure_has_no_automatic_redownload(self):
        self.source.data['file-0'] = b'bad!' * 3
        for _ in range(10):
            self.collector.tick()
            self.source.now += 5
        self.assertEqual(len(self.source.reads), 3)
        self.assertEqual(self.rows()['file-0']['state'], 'error')
        self.assertFalse((self.root / 'archive/file-0.logdata').exists())

    def test_path_traversal_rejected_before_read(self):
        old = self.source.files[0]
        self.source.files = (LogFile(old.id, old.name, old.size, old.sha256, old.created_at, '../secret'),)
        self.collector.tick()
        self.assertEqual(self.source.reads, [])
        self.assertEqual(self.collector.snapshot()['state'], 'attention')

    def test_paused_estimate_is_retained_and_labeled(self):
        self.collector.tick()
        self.collector.tick()
        self.source.enabled = True
        self.source.now += 120
        self.collector.tick()
        snap = self.collector.snapshot()
        self.assertIsNone(snap['eta_seconds'])
        self.assertEqual(snap['historical_eta_seconds'], 1)
        self.assertEqual(snap['eta_basis'], 'paused_historical')

    def test_archive_verification_job_records_format_as_unchecked(self):
        for _ in range(4):
            self.collector.tick()
        job = self.collector.db.execute('SELECT * FROM verification_jobs').fetchone()
        self.assertEqual(job['state'], 'complete')
        self.assertEqual(job['format_status'], 'not_checked')
        self.assertEqual(job['digest'], self.source.files[0].sha256)
        self.assertEqual(len(list((self.root / 'archive').iterdir())), 1)


class IndependentTransferTests(unittest.TestCase):
    def test_cached_heartbeat_replay_never_refreshes_receipt(self):
        cached = CachedSource(None)
        cached.update(RobotStatus(False, 10, 'boot', 1, sequence=2))
        cached.update(RobotStatus(False, 20, 'boot', 1, sequence=2))
        self.assertEqual(cached.status().observed_at, 10)
        cached.update(RobotStatus(False, 21, 'boot', 1, sequence=3))
        self.assertEqual(cached.status().observed_at, 21)

    def test_local_verification_does_not_block_next_download_or_enable(self):
        with tempfile.TemporaryDirectory() as folder:
            source = ControlledTransport(2)
            source.status = lambda: RobotStatus(source.enabled, time.monotonic(), 'boot', source.generation)
            entered, release = threading.Event(), threading.Event()
            class SlowVerifier(Collector):
                def _digest(self, path):
                    entered.set()
                    release.wait(5)
                    return super()._digest(path)
            service = HubService(Config(data_dir=folder, idle_delay=0, chunk_size=12), source, collector_factory=SlowVerifier)
            service.start()
            try:
                self.assertTrue(entered.wait(2))
                # The transport records a read before fsync/catalog publication.
                # Wait for the published checkpoint, not the earlier I/O entry.
                wait_for(lambda: service.snapshot().get('verification_pending_files') == 2)
                self.assertEqual(len(source.reads), 2)
                source.enabled = True
                wait_for(lambda: service.snapshot()['source_status']['enabled'])
                release.set()
                wait_for(lambda: service.snapshot()['completed_files'] == 2)
                self.assertEqual(service.snapshot()['state'], 'paused')
            finally:
                release.set()
                self.assertTrue(service.close())

    def test_transport_token_revoked_promptly_and_no_next_request(self):
        with tempfile.TemporaryDirectory() as folder:
            source = ControlledTransport()
            source.status = lambda: RobotStatus(source.enabled, time.monotonic(), 'boot', source.generation)
            entered, ended = threading.Event(), threading.Event()
            def stall(identity, offset, length, *, permission_generation, cancellation):
                source.reads.append((identity, offset, length))
                entered.set()
                cancellation.event.wait(2)
                ended.set()
                cancellation.check()
            source.read = stall
            service = HubService(Config(data_dir=folder, idle_delay=0, status_interval=0.01, chunk_size=4), source)
            service.start()
            try:
                self.assertTrue(entered.wait(2))
                source.enabled = True
                self.assertTrue(ended.wait(1))
                wait_for(lambda: service.snapshot()['source_status']['enabled'])
                self.assertEqual(len(source.reads), 1)
                self.assertEqual(service.snapshot()['files'][0]['offset'], 0)
                self.assertLessEqual(service.snapshot()['max_outstanding_bytes'], 4)
            finally:
                self.assertTrue(service.close())

    def test_new_limits_reject_invalid_values(self):
        for key, value in [('io_timeout', 0), ('discovery_page_size', True), ('retry_limit', 0), ('eta_minimum', 20), ('retry_max', .5)]:
            with self.subTest(key=key), self.assertRaises(ConfigError):
                Config(**{key: value})

from unittest.mock import patch
import sqlite3


class TransferFaultTests(unittest.TestCase):
    setUp = ProductionTransferTests.setUp
    tearDown = ProductionTransferTests.tearDown
    make = ProductionTransferTests.make
    rows = ProductionTransferTests.rows
    def test_disk_full_keeps_checkpoint_and_truncates_tail_on_restart(self):
        self.collector.tick()
        with patch('robot_test_hub.collector.os.fsync', side_effect=OSError(28, 'No space left')):
            self.collector.tick()
        self.assertEqual(self.rows()['file-0']['offset'], 4)
        self.collector.close()
        self.collector = self.make()
        self.assertEqual((self.root / 'partial/file-0.part').read_bytes(), b'AAAA')
        self.assertEqual(self.rows()['file-0']['offset'], 4)

    def test_retry_limit_stops_repeated_truncation_errors(self):
        self.source.data['file-0'] = b''
        self.collector.retry_limit = 2
        self.collector.tick()
        self.source.now += 2
        self.collector.tick()
        self.assertEqual(self.rows()['file-0']['attempts'], 2)
        self.assertEqual(self.rows()['file-0']['state'], 'error')
        self.source.now += 100
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 2)

    def test_pending_source_digest_is_blocked_backlog(self):
        self.source.pending_bytes = 999
        self.collector.tick()
        self.collector.tick()
        snapshot = self.collector.snapshot()
        self.assertEqual(snapshot['blocked_bytes'], 999)
        self.assertIsNone(snapshot['eta_seconds'])
        self.collector.tick()
        self.collector.tick()
        self.assertEqual(self.collector.snapshot()['state'], 'attention')

    def test_malformed_replayed_heartbeat_revokes_permission(self):
        cached = CachedSource(None)
        cached.update(RobotStatus(False, 10, 'boot', 1, sequence=2))
        cached.update(RobotStatus(True, 20, 'boot', 1, sequence=2))
        self.assertIsNone(cached.status().enabled)

    def test_missing_source_recovers_by_identity_when_manifest_returns(self):
        original = self.source.files
        self.collector.tick()
        self.source.files = ()
        self.source.now += 5
        self.collector.tick()
        self.assertEqual(self.rows()['file-0']['state'], 'missing')
        self.source.files = original
        self.source.now += 5
        self.collector.tick()
        self.assertEqual(self.source.reads[-1][1], 4)

    def test_identity_conflict_cannot_be_automatically_or_explicitly_retried(self):
        self.collector.tick()
        old = self.source.files[0]
        self.source.files = (LogFile(old.id, old.name, old.size + 1, old.sha256, old.created_at),)
        self.source.now += 5
        self.collector.tick()
        self.collector.retry_errors(old.id)
        self.assertEqual(self.rows()[old.id]['state'], 'error')
        self.assertEqual(self.rows()[old.id]['error_code'], 'identity_conflict')

    def test_persisted_retry_backoff_survives_restart(self):
        self.source.faults['file-0'] = TransferError('network loss')
        self.collector.tick()
        next_retry = self.rows()['file-0']['next_retry']
        self.collector.close()
        self.collector = self.make()
        self.assertEqual(self.rows()['file-0']['next_retry'], next_retry)
        self.collector.tick()
        self.assertEqual(len(self.source.reads), 1)

class TransferAdditionalTests(unittest.TestCase):
    setUp = ProductionTransferTests.setUp
    tearDown = ProductionTransferTests.tearDown
    make = ProductionTransferTests.make
    rows = ProductionTransferTests.rows

    def test_downloaded_checkpoint_recovers_local_verification_while_enabled(self):
        for _ in range(2):
            self.collector.tick()
        # Model the crash after final checkpoint before creating verification job.
        part = self.root / 'partial/file-0.part'
        part.write_bytes(self.source.data['file-0'])
        with self.collector.db:
            self.collector.db.execute("UPDATE files SET offset=size WHERE id='file-0'")
            self.collector.db.execute("UPDATE transfer_meta SET checkpoint_offset=12,checkpoint_digest=? WHERE file_id='file-0'", (self.source.files[0].sha256,))
        self.collector.close()
        self.collector = self.make()
        self.source.enabled = True
        before = len(self.source.reads)
        self.collector.tick()
        self.assertEqual(self.rows()['file-0']['state'], 'complete')
        self.assertEqual(len(self.source.reads), before)

    def test_deadline_timeout_gets_attempt_and_backoff(self):
        self.collector.io_timeout = .03
        def stall(identity, offset, length, *, permission_generation, cancellation):
            self.source.reads.append((identity, offset, length))
            cancellation.event.wait(1)
            cancellation.check()
        self.source.read = stall
        self.collector.tick()
        row = self.rows()['file-0']
        self.assertEqual(row['error_code'], 'io_timeout')
        self.assertEqual(row['attempts'], 1)
        self.assertGreater(row['next_retry'], time.time())
        self.assertEqual(row['offset'], 0)

    def test_interface_change_invalidates_historical_estimate(self):
        self.collector.tick()
        self.collector.tick()
        self.source.profile = 'ethernet'
        self.source.enabled = True
        self.collector.tick()
        snap = self.collector.snapshot()
        self.assertIsNone(snap['historical_bytes_per_second'])
        self.assertEqual(snap['rate_link_profile'], 'ethernet')

    def test_expired_historical_estimate_is_unknown(self):
        self.collector.tick()
        self.collector.tick()
        self.source.enabled = True
        self.source.now += 301
        self.collector.tick()
        self.assertIsNone(self.collector.snapshot()['historical_eta_seconds'])

    def test_service_priority_and_retry_requests_validate_before_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            source = ControlledTransport()
            source.status = lambda: RobotStatus(False, time.monotonic(), 'boot', 0)
            service = HubService(Config(data_dir=folder, idle_delay=0), source)
            service.start()
            try:
                wait_for(lambda: service.snapshot()['completed_files'] == 1)
                with self.assertRaises(ValueError):
                    service.set_priority('unknown', 1)
                with self.assertRaises(ValueError):
                    service.set_priority('file-0', True)
                service.set_priority('file-0', 50, True)
                wait_for(lambda: service.snapshot()['files'][0]['priority'] == 50)
                service.retry_errors('file-0')
            finally:
                self.assertTrue(service.close())

    def test_identity_conflict_survives_restart_with_original_archive_preserved(self):
        for _ in range(4):
            self.collector.tick()
        old = self.source.files[0]
        self.source.files = (LogFile(old.id, old.name, old.size + 1, old.sha256, old.created_at),)
        self.source.now += 5
        self.collector.tick()
        self.collector.close()
        self.collector = self.make()
        self.assertEqual(self.rows()[old.id]['state'], 'error')
        self.assertEqual((self.root / 'archive/file-0.logdata').read_bytes(), self.source.data['file-0'])
        self.assertEqual(self.collector.snapshot()['blocked_files'], 1)

    def test_conflict_latch_survives_disappearance_return_retry_and_restart(self):
        self.collector.tick()
        original = self.source.files
        old = original[0]
        self.source.files = (LogFile(old.id, old.name, old.size + 1, old.sha256, old.created_at),)
        self.source.now += 5
        self.collector.tick()
        self.source.files = ()
        self.collector.retry_errors()
        self.collector.tick()
        self.assertEqual(self.rows()[old.id]['error_code'], 'identity_conflict')
        self.collector.retry_errors(old.id)
        self.source.files = original
        self.source.now += 5
        self.collector.tick()
        self.assertEqual(self.rows()[old.id]['state'], 'error')
        self.assertEqual(self.rows()[old.id]['error_code'], 'identity_conflict')
        self.collector.close()
        self.collector = self.make()
        self.assertEqual(self.rows()[old.id]['state'], 'error')
        self.assertEqual(len(self.source.reads), 1)

    def test_duplicate_within_single_page_is_nonretryable_malformed_manifest(self):
        self.source.files = self.source.files * 2
        self.collector.tick()
        self.assertEqual(self.source.reads, [])
        self.assertEqual(self.collector.snapshot()['state'], 'attention')
        self.assertEqual(self.collector.discovery_retry, float('inf'))
        self.assertEqual(self.collector.db.execute('SELECT error_code FROM manifest_snapshots').fetchone()[0], 'malformed_manifest')
        self.source.now += 100
        self.collector.tick()
        self.assertEqual(len(self.source.pages), 1)

    def test_async_rename_failure_is_explicit_local_retry_without_redownload(self):
        self.collector.close()
        self.collector = self.make(independent_verification=True)
        for _ in range(3):
            self.collector.tick()
        self.collector.verifier.thread.join(1)
        before = len(self.source.reads)
        with patch('pathlib.Path.replace', side_effect=PermissionError('Archive locked')):
            self.collector.tick()
        self.assertEqual(self.rows()['file-0']['state'], 'error')
        self.assertEqual(self.rows()['file-0']['error_code'], 'local_verification')
        self.assertEqual(self.rows()['file-0']['offset'], 12)
        self.collector.retry_errors('file-0')
        self.source.enabled = True
        self.collector.tick()
        self.collector.verifier.thread.join(1)
        self.collector.tick()
        self.assertEqual(self.rows()['file-0']['state'], 'complete')
        self.assertEqual(len(self.source.reads), before)

    def test_commit_failure_after_rename_reconciles_locally_on_explicit_retry(self):
        self.collector.close()
        self.collector = self.make(independent_verification=True)
        for _ in range(3):
            self.collector.tick()
        self.collector.verifier.thread.join(1)
        before = len(self.source.reads)
        self.collector.db.execute("CREATE TRIGGER fail_complete BEFORE UPDATE ON files WHEN NEW.state='complete' BEGIN SELECT RAISE(ABORT,'catalog commit fault'); END")
        self.collector.tick()
        self.assertTrue((self.root / 'archive/file-0.logdata').exists())
        self.assertEqual(self.rows()['file-0']['state'], 'error')
        self.collector.db.execute('DROP TRIGGER fail_complete')
        self.collector.retry_errors('file-0')
        self.source.enabled = True
        self.collector.tick()
        self.collector.verifier.thread.join(1)
        self.collector.tick()
        self.assertEqual(self.rows()['file-0']['state'], 'complete')
        self.assertEqual(len(self.source.reads), before)
        self.assertEqual(len(list((self.root / 'archive').iterdir())), 1)
