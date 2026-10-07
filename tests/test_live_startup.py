"""Exercise the real live CLI ordering without launching readers or opening archives."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.server import main
from robot_test_hub.storage import OwnershipError

try:
    import paramiko
except ImportError:
    paramiko = None


@unittest.skipUnless(paramiko is not None and paramiko.__version__ == '4.0.0', 'Pinned SFTP extra required')
class LiveStartupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / 'known').write_text('synthetic local reference', encoding='utf-8')
        (self.root / 'key').write_text('synthetic local reference', encoding='utf-8')
        self.path = self.root / 'source.json'
        self.path.write_text(json.dumps(dict(schema_version=1, host='private-source.invalid',
            username='private-user', known_hosts='known', private_key='key', log_root='/private-root',
            robot_id='robot-6391')), encoding='utf-8')
        self.archive = self.root / 'unopened-archive'
        self.args = ['robot_test_hub.server', '--source-config', str(self.path),
                     '--nt-host', 'private-nt.invalid', '--nt-port', '5810',
                     '--data-dir', str(self.archive)]
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        for name in ('socket.getaddrinfo', 'socket.create_connection',
                     'robot_test_hub.status_bridge.StatusBridge.start',
                     'robot_test_hub.sftp_source.SFTPSource._connect'):
            forbidden = self.contexts.enter_context(patch(name, side_effect=AssertionError('Unexpected network/reader launch')))
            self.addCleanup(forbidden.assert_not_called)
        self.output = io.StringIO()
        self.contexts.enter_context(redirect_stderr(self.output))
        self.contexts.enter_context(redirect_stdout(self.output))
        self.contexts.enter_context(patch('sys.argv', self.args))

    def assert_private_values_omitted(self):
        for value in ('private-source.invalid', 'private-nt.invalid', 'private-user', '/private-root',
                      str(self.root), 'private-native-error'):
            self.assertNotIn(value, self.output.getvalue())

    def test_native_failure_precedes_hub_creation_and_exposes_only_guidance(self):
        errors = (RuntimeError('private-native-error'), FileNotFoundError('private-native-error'),
                  subprocess.TimeoutExpired('private-native-error', 1))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.output.seek(0)
                self.output.truncate(0)
                with patch('tools.status_bridge.run.prepare', side_effect=error) as prepare, \
                        patch('robot_test_hub.server.HubService') as service:
                    self.assertEqual(main(), 2)
                prepare.assert_called_once()
                service.assert_not_called()
                self.assertFalse(self.archive.exists())
                self.assert_private_values_omitted()
                self.assertIn('Hub source startup failed', self.output.getvalue())

    def test_missing_explicit_nt_endpoint_precedes_native_prep_and_hub(self):
        self.args.remove('--nt-host')
        self.args.remove('private-nt.invalid')
        with patch('tools.status_bridge.run.prepare') as prepare, patch('robot_test_hub.server.HubService') as service:
            self.assertEqual(main(), 2)
        prepare.assert_not_called()
        service.assert_not_called()
        self.assertFalse(self.archive.exists())
        self.assert_private_values_omitted()

    def test_prepared_reader_is_passed_to_hub_before_any_reader_start(self):
        command, env = ['private-prepared-command'], {'private-native-env': 'synthetic'}
        install = self.root / 'explicit-install'
        self.args.extend(['--wpilib-install', str(install)])
        with patch('tools.status_bridge.run.prepare', return_value=(command, env)) as prepare, \
                patch('robot_test_hub.server.HubService', side_effect=OwnershipError('Synthetic archive already owned')) as service:
            self.assertEqual(main(), 2)
        prepare.assert_called_once_with(install)
        service.assert_called_once()
        config, source = service.call_args.args
        self.assertEqual(config.idle_delay, 10)
        self.assertEqual(source.status_provider.command, command)
        self.assertEqual(source.status_provider.env, env)
        self.assertFalse(self.archive.exists())
        self.assert_private_values_omitted()


if __name__ == '__main__':
    unittest.main()
