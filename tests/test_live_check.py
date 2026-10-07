"""Local readiness must neither contact configured endpoints nor expose private values."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.config import Config
from robot_test_hub.live_check import check_live, main

try:
    import paramiko
except ImportError:
    paramiko = None


@unittest.skipUnless(paramiko is not None and paramiko.__version__ == '4.0.0', 'Pinned SFTP extra required')
class LiveCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        key = paramiko.RSAKey.generate(1024)
        key.write_private_key_file(str(self.root / 'private-key'))
        self.key = key
        self.host = 'private-endpoint.invalid'
        self.pin(self.host)
        self.source = dict(schema_version=1, host=self.host, username='private-user',
                           known_hosts='known', private_key='private-key', log_root='/private-root',
                           robot_id='robot-6391')
        self.save()
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.build = contexts.enter_context(patch('tools.status_bridge.run.prepare', return_value=([], {})))
        # Even a DNS lookup is a regression. The check is strictly local.
        for name in ('socket.getaddrinfo', 'socket.create_connection',
                     'robot_test_hub.status_bridge.StatusBridge.start',
                     'robot_test_hub.sftp_source.SFTPSource._connect',
                     'robot_test_hub.service.HubService.__init__'):
            forbidden = contexts.enter_context(patch(name, side_effect=AssertionError('Unexpected robot/archive access')))
            self.addCleanup(forbidden.assert_not_called)

    def pin(self, name):
        keys = paramiko.HostKeys()
        keys.add(name, self.key.get_name(), self.key)
        keys.save(str(self.root / 'known'))

    def save(self):
        self.path = self.root / 'source.json'
        self.path.write_text(json.dumps(self.source), encoding='utf-8')

    def check(self, **kwargs):
        report = check_live(kwargs.pop('config', Config()), self.path, 'private-nt.invalid',
                            kwargs.pop('nt_port', 5810), **kwargs)
        encoded = json.dumps(report)
        for secret in (self.host, 'private-user', '/private-root', 'private-nt.invalid', str(self.root),
                       self.key.get_base64(), 'BEGIN RSA PRIVATE KEY'):
            self.assertNotIn(secret, encoded)
        self.assertFalse(report['network_contacted'])
        self.assertFalse(report['archive_opened'])
        self.assertFalse(report['hardware_qualified'])
        return report

    def test_valid_local_setup_uses_live_defaults_without_any_connection(self):
        report = self.check()
        self.assertTrue(report['ready'])
        self.assertEqual(report['settings'], dict(idle_delay_seconds=10, freshness_seconds=.5,
                                                 chunk_bytes=262144, sftp_read_bytes=32768))
        self.build.assert_called_once()

    def test_explicit_idle_override_and_effective_freshness(self):
        report = self.check(config=Config(idle_delay=15, freshness=.2), idle_delay_explicit=True)
        self.assertEqual(report['settings']['idle_delay_seconds'], 15)
        self.assertEqual(report['settings']['freshness_seconds'], .2)

    def test_nonstandard_ssh_port_requires_exact_pinned_lookup(self):
        self.source['port'] = 2222
        self.save()
        report = self.check()
        self.assertFalse(report['ready'])
        self.assertFalse(next(c for c in report['checks'] if c['name'] == 'pinned_host_key')['passed'])
        self.pin(f'[{self.host}]:2222')
        self.assertTrue(self.check()['ready'])

    def test_invalid_private_key_is_a_redacted_actionable_failure(self):
        (self.root / 'private-key').write_text('private-secret-content', encoding='utf-8')
        report = self.check()
        self.assertFalse(report['ready'])
        item = next(c for c in report['checks'] if c['name'] == 'private_key')
        self.assertFalse(item['passed'])
        self.assertNotIn('private-secret-content', json.dumps(report))

    def test_invalid_source_and_native_failure_are_reported_independently(self):
        self.path.write_text('{', encoding='utf-8')
        self.build.side_effect = RuntimeError('private-secret-content')
        report = self.check()
        self.assertFalse(report['ready'])
        failures = {c['name'] for c in report['checks'] if not c['passed']}
        self.assertEqual(failures, {'source_config', 'native_status_reader'})
        self.assertNotIn('private-secret-content', json.dumps(report))

    def test_valid_source_native_failures_keep_json_redacted_and_prepare_once(self):
        errors = (RuntimeError('private-native-error'),
                  subprocess.TimeoutExpired('private-command', 1, output='private-native-error'),
                  subprocess.CalledProcessError(1, 'private-command', stderr='private-native-error'))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.build.reset_mock()
                self.build.side_effect = error
                stream = io.StringIO()
                with redirect_stdout(stream):
                    code = main(['--source-config', str(self.path), '--nt-host', 'private-nt.invalid',
                                 '--nt-port', '5810', '--json'])
                self.assertEqual(code, 2)
                report = json.loads(stream.getvalue())
                self.assertTrue(next(c for c in report['checks'] if c['name'] == 'transfer_settings')['passed'])
                self.assertFalse(next(c for c in report['checks'] if c['name'] == 'native_status_reader')['passed'])
                self.assertNotIn('private-', stream.getvalue())
                self.build.assert_called_once()

    def test_source_replacement_cannot_change_checked_snapshot_mid_check(self):
        original = paramiko.PKey.from_path
        def replace_source(path):
            key = original(path)
            self.source.update(host='replacement.invalid', max_read=1, private_key='missing-key')
            self.save()
            return key
        with patch.object(paramiko.PKey, 'from_path', side_effect=replace_source):
            report = self.check()
        self.assertTrue(report['ready'])
        self.assertEqual(report['settings']['chunk_bytes'], 262144)
        self.assertEqual(report['settings']['sftp_read_bytes'], 32768)

    def test_invalid_nt_and_chunk_bounds_cannot_be_ready(self):
        for options in (dict(nt_port=0), dict(config=Config(chunk_size=524288))):
            with self.subTest(options=options):
                report = self.check(**options)
                self.assertFalse(report['ready'])
                self.assertIsNone(report['settings'])

    def test_wrong_transport_version_does_not_attempt_keys_or_configuration(self):
        with patch.object(paramiko, '__version__', '0.invalid'):
            report = self.check()
        self.assertFalse(report['ready'])
        self.assertEqual({c['name'] for c in report['checks']},
                         {'source_config', 'sftp_dependency', 'native_status_reader'})

    def test_cli_json_exit_and_explicit_hub_setting(self):
        config = self.root / 'hub.json'
        config.write_text(json.dumps(dict(schema_version=1, idle_delay=12)), encoding='utf-8')
        args = ['--source-config', str(self.path), '--nt-host', 'private-nt.invalid', '--nt-port', '5810',
                '--config', str(config), '--json']
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(stream.getvalue())['settings']['idle_delay_seconds'], 12)
        config.write_text('{', encoding='utf-8')
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main(args), 2)
        self.assertEqual(json.loads(stream.getvalue())['checks'][0]['name'], 'hub_config')

    def test_cli_text_explains_limits_without_echoing_values(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(['--source-config', str(self.path), '--nt-host', 'private-nt.invalid',
                         '--nt-port', '5810'])
        self.assertEqual(code, 0)
        self.assertIn('No robot connection or archive opened', stream.getvalue())
        self.assertNotIn('private-', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
