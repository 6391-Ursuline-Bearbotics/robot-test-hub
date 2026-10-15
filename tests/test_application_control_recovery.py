"""Actual OS lease contention/crash recovery with private synthetic runtime files."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub.application_control import ApplicationControl, ControlError
from robot_test_hub import launcher

CHILD = r'''
import os, sys, threading
from pathlib import Path
from robot_test_hub.application_control import ApplicationControl
path = Path(sys.argv[1])
if sys.argv[2] == 'raw':
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
else:
    control = ApplicationControl(path, threading.Event())
    control.check_available()
    control.publish(6391)
print('READY', flush=True)
sys.stdin.readline()
# Simulate abrupt process exit: no remove(), close(), finally or __del__.
os._exit(0)
'''


class ApplicationControlRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='hub lease private ')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.path = self.root / 'practice.runtime.json'

    def control(self):
        value = ApplicationControl(self.path, threading.Event())
        self.addCleanup(value.close)
        return value

    def record(self, **changes):
        value = dict(schema_version=2, port=6391, pid=12345,
                     token='a'*64, instance_id='b'*32)
        value.update(changes)
        raw = json.dumps(value).encode()
        self.path.write_bytes(raw)
        return raw

    @contextmanager
    def child(self, path=None, mode='control'):
        process = subprocess.Popen([sys.executable, '-c', CHILD, str(path or self.path), mode],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, PYTHONUTF8='1'))
        try:
            self.assertEqual(process.stdout.readline().strip(), 'READY')
            yield process
        finally:
            if process.poll() is None:
                process.communicate('exit\n', timeout=10)
            else:
                process.communicate(timeout=10)

    def test_actual_active_process_lease_blocks_without_changing_runtime(self):
        with self.child():
            original = self.path.read_bytes()
            self.assertEqual(json.loads(original)['schema_version'], 2)
            other = self.control()
            with self.assertRaises(ControlError):
                other.check_available()
            self.assertFalse(other.lease_held)
            self.assertEqual(self.path.read_bytes(), original)

    def test_actual_abrupt_exit_releases_lock_and_schema2_recovers(self):
        with self.child():
            old = self.path.read_bytes()
            lease = self.path.with_name(self.path.name+'.lease')
            identity = lease.stat().st_ino
        self.assertEqual(self.path.read_bytes(), old)
        value = self.control()
        value.check_available()
        self.assertTrue(value.lease_held)
        self.assertFalse(self.path.exists())
        value.publish(6392)
        new = json.loads(self.path.read_bytes())
        self.assertEqual(new['schema_version'], 2)
        self.assertEqual(new['port'], 6392)
        self.assertNotEqual(new['token'], json.loads(old)['token'])
        self.assertEqual(lease.stat().st_ino, identity)
        self.assertTrue(value.remove())
        self.assertTrue(value.lease_held)
        value.close()
        self.assertTrue(lease.is_file())
        self.assertFalse(value.lease_held)
        value.close()

    def test_empty_lease_is_not_initialized_before_lock(self):
        value = self.control()
        value.lease_path.write_bytes(b'')
        with self.child(value.lease_path, mode='raw'):
            with self.assertRaises(ControlError):
                value.check_available()
            self.assertEqual(value.lease_path.stat().st_size, 0)
        value.check_available()
        value.close()
        self.assertEqual(value.lease_path.read_bytes(), b'\x00')

    def test_same_process_second_control_is_also_excluded(self):
        first, second = self.control(), self.control()
        first.check_available()
        with self.assertRaises(ControlError):
            second.check_available()
        first.close()
        second.check_available()
        self.assertTrue(second.lease_held)

    def test_legacy_unknown_invalid_runtime_preserved_and_lease_released(self):
        valid = dict(schema_version=2, port=6391, pid=123, token='a'*64, instance_id='b'*32)
        cases = [dict(valid, schema_version=1), dict(valid, schema_version=3),
                 dict(valid, schema_version=True), dict(valid, pid=True), dict(valid, pid=0),
                 dict(valid, port=True), dict(valid, port=0), dict(valid, token='bad'),
                 dict(valid, instance_id='bad'), dict(valid, extra='unknown')]
        for case in cases:
            with self.subTest(case=case):
                raw = json.dumps(case).encode()
                self.path.write_bytes(raw)
                value = self.control()
                with self.assertRaises(ControlError):
                    value.check_available()
                self.assertFalse(value.lease_held)
                self.assertEqual(self.path.read_bytes(), raw)
        for raw in (b'not JSON', b'{"schema_version":2,"schema_version":2}', b'x'*65537):
            self.path.write_bytes(raw)
            value = self.control()
            with self.assertRaises(ControlError):
                value.check_available()
            self.assertEqual(self.path.read_bytes(), raw)
            self.assertFalse(value.lease_held)

    def test_hardlinked_runtime_is_preserved(self):
        original = self.record()
        alternate = self.root / 'shared.json'
        os.link(self.path, alternate)
        value = self.control()
        with self.assertRaises(ControlError):
            value.check_available()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(alternate.read_bytes(), original)

    def test_hardlinked_lease_is_preserved_without_writes(self):
        value = self.control()
        value.lease_path.write_bytes(b'untouched')
        alternate = self.root / 'shared.lease'
        os.link(value.lease_path, alternate)
        with self.assertRaises(ControlError):
            value.check_available()
        self.assertEqual(alternate.read_bytes(), b'untouched')

    def test_lease_and_runtime_symlinks_preserved_if_platform_allows(self):
        target = self.root / 'target.json'
        target.write_bytes(b'keep target')
        value = self.control()
        try:
            value.lease_path.symlink_to(target)
        except OSError:
            self.skipTest('Platform does not permit symlink creation')
        with self.assertRaises(ControlError):
            value.check_available()
        self.assertTrue(value.lease_path.is_symlink())
        self.assertEqual(target.read_bytes(), b'keep target')
        value.lease_path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(ControlError):
            value.check_available()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), b'keep target')

    def test_publish_failure_keeps_lease_until_explicit_close(self):
        value = self.control()
        value.check_available()
        with patch('robot_test_hub.application_control.os.link', side_effect=OSError('private detail')):
            with self.assertRaises(OSError):
                value.publish(6391)
        self.assertTrue(value.lease_held)
        self.assertEqual(list(self.root.glob('.hub-runtime-*')), [])
        value.close()
        replacement = self.control()
        replacement.check_available()

    def test_remove_does_not_release_and_changed_runtime_is_preserved(self):
        value = self.control()
        value.publish(6391)
        self.path.write_bytes(b'changed')
        self.assertFalse(value.remove())
        self.assertTrue(value.lease_held)
        value.close()
        self.assertEqual(self.path.read_bytes(), b'changed')
        self.assertTrue(value.lease_path.is_file())

    def test_repeated_publish_cannot_replace_its_live_credential(self):
        value = self.control()
        value.publish(6391)
        raw = self.path.read_bytes()
        with self.assertRaises(ControlError):
            value.publish(6392)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_launcher_stops_both_known_runtime_versions_but_rejects_unknown(self):
        profile = self.path.with_name('practice.json')
        for schema in (1, 2):
            self.record(schema_version=schema)
            with patch.object(launcher, '_control', return_value=dict(pid=12345, instance_id='b'*32)) as control:
                self.assertTrue(launcher.stop_profile(profile))
                self.assertEqual([c.args[1] for c in control.call_args_list], ['GET', 'POST'])
        self.record(schema_version=3)
        with patch.object(launcher, '_control') as control:
            with self.assertRaises(launcher.LaunchError):
                launcher.stop_profile(profile)
            control.assert_not_called()


if __name__ == '__main__':
    unittest.main()
