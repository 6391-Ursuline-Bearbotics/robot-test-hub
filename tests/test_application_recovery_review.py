"""Independent recovery tests: OS ownership, strict admission and failure release."""
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


class ApplicationRecoveryIndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.path = self.base / 'hub.runtime.json'
        self.controls = []
        self.addCleanup(self.close_all)
        self.record = dict(schema_version=2, port=6391, pid=os.getpid(), token='a' * 64, instance_id='b' * 32)

    def close_all(self):
        for control in reversed(self.controls):
            control.close()

    def control(self):
        control = ApplicationControl(self.path, threading.Event())
        self.controls.append(control)
        return control

    def child(self, code):
        result = subprocess.run([sys.executable, '-c', code, str(self.path)], capture_output=True,
                                encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_active_lease_with_no_runtime_blocks_another_process_without_writing(self):
        owner = self.control()
        owner.check_available()
        self.assertFalse(self.path.exists())
        lease_info = owner.lease_path.stat()
        output = self.child('from pathlib import Path\nimport sys,threading\nfrom robot_test_hub.application_control import ApplicationControl,ControlError\nc=ApplicationControl(Path(sys.argv[1]),threading.Event())\ntry:\n c.check_available()\nexcept ControlError:\n print("blocked")\nelse:\n raise SystemExit(9)\n')
        self.assertEqual(output, 'blocked')
        self.assertFalse(self.path.exists())
        self.assertEqual(owner.lease_path.stat().st_size, lease_info.st_size)
        self.assertEqual(owner.lease_path.stat().st_mtime_ns, lease_info.st_mtime_ns)
        self.assertTrue(owner.lease_held)

    def test_crashed_process_releases_os_lease_and_recovers_only_its_schema(self):
        self.child('import os,sys,threading\nfrom robot_test_hub.application_control import ApplicationControl\nc=ApplicationControl(sys.argv[1],threading.Event())\nc.publish(6391)\nos._exit(0)\n')
        old = json.loads(self.path.read_bytes())
        owner = self.control()
        owner.check_available()
        self.assertTrue(owner.lease_held)
        self.assertFalse(self.path.exists())
        owner.publish(6392)
        current = json.loads(self.path.read_bytes())
        self.assertEqual(current['schema_version'], 2)
        self.assertNotEqual(current['instance_id'], old['instance_id'])
        self.assertEqual(current['port'], 6392)
        self.assertNotIn(b'\r', self.path.read_bytes())

    def test_live_pid_is_not_used_as_ownership_or_contact_authority(self):
        self.path.write_text(json.dumps(self.record), encoding='utf-8')
        owner = self.control()
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')), \
             patch('os.kill', side_effect=AssertionError('process signals forbidden')):
            owner.check_available()
        self.assertFalse(self.path.exists())
        self.assertTrue(owner.lease_held)

    def test_legacy_unknown_duplicate_and_invalid_records_are_preserved_and_release_lease(self):
        cases = [dict(self.record, schema_version=1), dict(self.record, schema_version=3),
                 dict(self.record, pid=True), dict(self.record, port=0), dict(self.record, token='private'),
                 dict(self.record, unexpected='private'), dict(self.record, instance_id='B' * 32)]
        raws = [json.dumps(value).encode() for value in cases]
        raws += [b'{"schema_version":2,"schema_version":2}', b'\xff', b'x' * 65537]
        for raw in raws:
            with self.subTest(raw=raw[:60]):
                self.path.write_bytes(raw)
                owner = self.control()
                with self.assertRaises(ControlError) as raised:
                    owner.check_available()
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertFalse(owner.lease_held)
                self.assertNotIn(str(self.path), str(raised.exception))
                self.assertNotIn(self.record['token'], str(raised.exception))
                self.path.unlink()
                follower = self.control()
                follower.check_available()
                self.assertTrue(follower.lease_held)
                follower.close()

    def test_hardlinked_runtime_is_preserved(self):
        target = self.base / 'other-private.json'
        raw = json.dumps(self.record).encode()
        target.write_bytes(raw)
        os.link(target, self.path)
        owner = self.control()
        with self.assertRaises(ControlError):
            owner.check_available()
        self.assertFalse(owner.lease_held)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(target.stat().st_nlink, 2)

    def test_hardlinked_lease_is_preserved_without_initialization(self):
        owner = self.control()
        target = self.base / 'other-lease'
        target.write_bytes(b'preserve private bytes')
        os.link(target, owner.lease_path)
        with self.assertRaises(ControlError):
            owner.check_available()
        self.assertFalse(owner.lease_held)
        self.assertEqual(target.read_bytes(), b'preserve private bytes')
        self.assertEqual(owner.lease_path.read_bytes(), b'preserve private bytes')
        self.assertFalse(self.path.exists())

    def test_remove_retains_lease_until_explicit_close_and_never_unlinks_lease(self):
        owner = self.control()
        owner.publish(6391)
        lease_identity = owner.lease_path.stat().st_ino
        self.assertTrue(owner.remove())
        contender = self.control()
        with self.assertRaises(ControlError):
            contender.check_available()
        self.assertTrue(owner.lease_held)
        owner.close()
        owner.close()
        contender.check_available()
        self.assertTrue(contender.lease_held)
        self.assertEqual(contender.lease_path.stat().st_ino, lease_identity)

    def test_publish_failure_leaves_lease_until_owner_explicitly_closes(self):
        owner = self.control()
        owner.check_available()
        with patch('robot_test_hub.application_control.os.link', side_effect=OSError('private failure')):
            with self.assertRaises(OSError):
                owner.publish(6391)
        self.assertTrue(owner.lease_held)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.base.glob('.hub-runtime-*')), [])
        owner.close()
        follower = self.control()
        follower.publish(6392)
        self.assertTrue(follower.lease_held)

    def test_check_is_idempotent_and_does_not_reclaim_own_published_record(self):
        owner = self.control()
        owner.publish(6391)
        original = self.path.read_bytes()
        owner.check_available()
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaises(ControlError):
            owner.publish(6392)
        self.assertEqual(self.path.read_bytes(), original)
    def test_pending_service_survives_gc_and_retains_both_owners_after_main_returns(self):
        import contextlib
        import gc
        import io
        import weakref
        from robot_test_hub import server
        from robot_test_hub.storage import DataRootOwner, OwnershipError

        archive = self.base / 'archive'
        helper_release = threading.Event()
        helper = threading.Thread(target=helper_release.wait, name='independent-source-only-helper')
        helper.start()
        observed = []
        control_path = self.path

        class PendingService:
            def __init__(self):
                self.root = archive
                self.owner = DataRootOwner(archive)
                self.cycle = self
                self.helper = helper
            def start(self):
                pass
            def close(self):
                return False

        def make_service(*args, **kwargs):
            service = PendingService()
            observed.append(weakref.ref(service))
            return service

        class ForegroundHTTP:
            server_address = ('127.0.0.1', 6391)
            def __init__(self, control):
                self.control = control
            def handle_request(self):
                self.control.shutdown.set()
            def server_close(self):
                pass

        def make_http(service, source, port, *, application_control=None):
            return ForegroundHTTP(application_control)

        def cleanup():
            helper_release.set()
            helper.join(2)
            for reference in observed:
                service = reference()
                if service is not None:
                    if service in server._pending_shutdown_services:
                        server._pending_shutdown_services.remove(service)
                    service.application_control_owner.close()
                    service.owner.close()
                    service.cycle = None
            gc.collect()
        self.addCleanup(cleanup)

        with patch.object(server, 'HubService', new=make_service), \
             patch.object(server, 'create_http_server', new=make_http), \
             patch.object(server.signal, 'signal', new=lambda *args: None), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = server.main(['--data-dir', str(archive), '--launch-control', str(control_path)])
        self.assertEqual(result, 0)
        gc.collect()
        self.assertEqual(len(observed), 1)
        self.assertIsNotNone(observed[0](), 'Pending service must outlive main even when helper owns only source state')
        self.assertIn(observed[0](), server._pending_shutdown_services)
        self.assertTrue(helper.is_alive())
        contender = self.control()
        with self.assertRaises(ControlError):
            contender.check_available()
        with self.assertRaises(OwnershipError):
            DataRootOwner(archive)
        self.assertTrue(self.path.is_file())
