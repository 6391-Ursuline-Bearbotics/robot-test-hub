"""No-network live pull admission and continuous idle token regressions."""
from dataclasses import replace
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.collector import RobotStatus
from robot_test_hub.config import Config
from robot_test_hub.service import HubService, PullGate, PullPermissionError, CachedSource
from robot_test_hub.sftp_source import SFTPConfig, SFTPSource
from robot_test_hub.source_status import StatusInbox
from robot_test_hub.status_bridge import StatusBridge


class PullServiceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.settings = SFTPConfig('robot', 'reader', 'hosts', 'key', '/logs', 'robot-6391')
        self.inbox = StatusInbox('robot-6391')
        self.service = HubService.__new__(HubService)
        s = self.service
        s.config = Config(pull_source_config='explicit.json', idle_delay=.2, freshness=.5)
        s.root = 'unused-no-io'
        s.source = SFTPSource(self.settings, self.inbox)
        s.cached_source = CachedSource(s.source)
        s.stop, s.paused = threading.Event(), threading.Event()
        s.preference_generation = 0
        s._pull_gate = PullGate(s, clock=lambda: self.now)
        s.cache_lock = threading.Lock()
        s.closed = False
        s.pull_request = None
        s.pull_status = {'state': 'idle'}
        self.loader = patch.object(SFTPConfig, 'load', return_value=self.settings)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def observe(self, *, enabled=False, generation=1, boot='boot', advance=.1, permission=True):
        self.now += advance
        old = self.service.cached_source.status()
        status = RobotStatus(enabled, self.now, boot, generation, permission,
                             (old.sequence or 0)+1)
        self.service.cached_source.update(status)
        self.service._pull_gate.observe(status)

    def ready(self):
        for _ in range(4):
            self.observe()
        return self.service._pull_gate.permission()

    def test_requires_continuous_idle_and_revokes_on_enable_then_disable(self):
        self.observe()
        with self.assertRaises(PullPermissionError):
            self.service._pull_gate.permission()
        token = self.ready()
        token.check()
        self.observe(enabled=True, generation=2)
        self.observe(generation=3)
        with self.assertRaises(PullPermissionError):
            token.check()
        self.ready().check()

    def test_stale_gap_reboot_generation_and_operator_pause_reset_wait(self):
        for change in ('stale', 'boot', 'generation', 'pause', 'permission', 'unknown'):
            with self.subTest(change=change):
                token = self.ready()
                if change == 'stale':
                    self.now += 1
                    self.observe()
                elif change == 'pause':
                    self.service.preference_generation += 2
                    self.observe()
                elif change == 'permission':
                    self.observe(permission=False)
                elif change == 'unknown':
                    self.observe(enabled=None)
                else:
                    self.observe(**({'boot': 'other'} if change == 'boot' else {'generation': 2}))
                with self.assertRaises(PullPermissionError):
                    token.check()

    def test_demo_offline_and_sim_status_never_admit_real_pull(self):
        for source in (SimpleNamespace(), SFTPSource(self.settings, StatusInbox('robot-6391',runtime_mode='SIM'))):
            self.service.source = source
            with self.assertRaisesRegex(ValueError, 'pull_requires_live_status'):
                self.service.request_pull()

    def test_source_and_status_binding_and_post_admission_edits_fail_closed(self):
        for field, value in (('host','other'), ('log_root','/other'), ('robot_id','other'), ('private_key','other')):
            with self.subTest(field=field), patch.object(SFTPConfig,'load',return_value=replace(self.settings,**{field:value})):
                with self.assertRaisesRegex(ValueError,'pull_source_mismatch'):
                    self.service.request_pull()
        self.service.source.status_provider = StatusBridge('other',5810,'robot-6391')
        with self.assertRaisesRegex(ValueError,'pull_source_mismatch'):
            self.service.request_pull()
        self.service.source.status_provider = self.inbox
        token = self.ready()
        with patch.object(SFTPConfig,'load',return_value=replace(self.settings,host='other')):
            with self.assertRaises(ValueError):
                token.check()

    def test_queued_wait_has_no_connection_and_interruption_is_not_complete(self):
        self.service.request_pull()
        with patch('robot_test_hub.pull.collect') as collect:
            self.service._run_pull(None)
            collect.assert_not_called()
            self.assertEqual(self.service.pull_status['state'],'waiting')
            self.ready()
            collect.return_value = {'state':'interrupted'}
            self.service._run_pull(None)
            self.assertEqual(self.service.pull_status['state'],'paused')
            self.assertIsNone(self.service.pull_request)

    def test_bulk_slot_wait_is_cancelable_and_never_bypasses_camera(self):
        self.service.bulk_lock = threading.Lock()
        self.service.camera_network = threading.Event()
        class Permission:
            calls = 0
            def check(token):
                token.calls += 1
                if token.calls >= 2:
                    raise PullPermissionError('revoked while waiting')
        self.service.bulk_lock.acquire()
        with self.assertRaises(PullPermissionError):
            with self.service._pull_bulk_slot(Permission()):
                self.fail('must not enter a busy network slot')
        self.service.bulk_lock.release()
        self.service.camera_network.set()
        with self.assertRaises(PullPermissionError):
            with self.service._pull_bulk_slot(Permission()):
                self.fail('must not enter during camera network work')
        self.assertTrue(self.service.bulk_lock.acquire(blocking=False))
        self.service.bulk_lock.release()

    def test_revocation_at_collect_entry_reports_paused_and_stop_reports_canceled(self):
        from robot_test_hub.pull import PermissionLost
        self.ready()
        self.service.request_pull()
        with patch('robot_test_hub.pull.collect',side_effect=PermissionLost('revoked')):
            self.service._run_pull(None)
        self.assertEqual(self.service.pull_status['state'],'paused')
        self.service.stop.set()
        with self.assertRaises(PullPermissionError):
            self.service._pull_gate.permission()


if __name__ == '__main__':
    unittest.main()
