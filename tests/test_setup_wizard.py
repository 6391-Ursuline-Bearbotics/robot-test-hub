"""Setup wizard: host-key fingerprint, confirmation, read-only test and save (loopback SSH)."""
import json
from pathlib import Path
import stat
import tempfile
import unittest

try:
    import paramiko
except ImportError:
    paramiko = None

from robot_test_hub import setup_wizard
from robot_test_hub.config import Config
from robot_test_hub.sftp_source import SFTPConfig


@unittest.skipIf(paramiko is None, 'Install the optional pinned sftp extra for loopback SSH qualification')
class SetupWizardTests(unittest.TestCase):
    def setUp(self):
        from test_sftp_network import LoopbackSFTP
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        server = LoopbackSFTP(self.folder, {'akit_1.wpilog': b'x' * 10, 'akit_2.wpilog': b'y' * 20})
        self.addCleanup(server.close)
        base = server.files_type

        class Listing(base):
            def list_folder(self, path):
                out = []
                for index, (name, content) in enumerate(server.files.items()):
                    a = paramiko.SFTPAttributes()
                    a.filename, a.st_size, a.st_mode = name, len(content), stat.S_IFREG | 0o444
                    a.st_mtime = a.st_atime = 1000 + index
                    out.append(a)
                return out
        server.files_type = Listing
        self.server = server

    def values(self, key):
        return dict(host='127.0.0.1', port=self.server.port, username='reader', private_key=str(self.server.key_path),
                    log_root='/logs', confirmed=True, **{k: key[k] for k in ('key_type', 'key_base64', 'fingerprint')})

    def test_fetch_confirm_test_and_save(self):
        key = setup_wizard.fetch_host_key('127.0.0.1', self.server.port)
        self.assertEqual(key['fingerprint'], setup_wizard.fingerprint(self.server.host_key.asbytes()))
        self.assertEqual(setup_wizard.test_connection(self.values(key), self.folder / 'check'),
                         {'logs': 2, 'bytes': 30, 'newest': 'akit_2.wpilog'})
        hub = self.folder / 'hub.json'
        hub.write_text(json.dumps({'schema_version': 1, 'data_dir': str(self.folder / 'data')}))
        result = setup_wizard.save(self.values(key), hub)
        self.assertTrue(result['restart_required'])
        config = Config.load(hub, resolve_paths=True)
        source = SFTPConfig.load(config.pull_source_config)
        self.assertEqual((source.host, source.port, source.log_root), ('127.0.0.1', self.server.port, '/logs'))
        self.assertTrue((self.folder / 'hub.json.previous').is_file())
        from robot_test_hub import pull
        client, sftp = pull.connect(source)
        sftp.close(); client.close()

    def test_unconfirmed_or_tampered_key_is_refused(self):
        key = setup_wizard.fetch_host_key('127.0.0.1', self.server.port)
        with self.assertRaises(setup_wizard.SetupError):
            setup_wizard.test_connection(dict(self.values(key), confirmed=False), self.folder)
        other = paramiko.RSAKey.generate(2048)
        with self.assertRaises(setup_wizard.SetupError):
            setup_wizard.test_connection(dict(self.values(key), key_base64=other.get_base64()), self.folder)

    def test_unreachable_and_bad_inputs(self):
        with self.assertRaises(setup_wizard.SetupError):
            setup_wizard.fetch_host_key('127.0.0.1', 1, timeout=.5)
        with self.assertRaises(setup_wizard.SetupError):
            setup_wizard.fetch_host_key('-bad', 22)
        self.assertEqual(setup_wizard.default_host(6391), '10.63.91.2')


if __name__ == '__main__':
    unittest.main()
