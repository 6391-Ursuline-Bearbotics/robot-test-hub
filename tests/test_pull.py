"""Collect now: pull finished logs over real loopback SSH/SFTP (synthetic logs only)."""
from contextlib import closing
from pathlib import Path
import stat
import tempfile
import unittest

try:
    import paramiko
except ImportError:
    paramiko = None

from robot_test_hub import pull
from robot_test_hub.importer import Importer
from robot_test_hub.sftp_source import SFTPConfig
from robot_test_hub.storage import open_catalog

FIXTURES = Path(__file__).parent / 'fixtures' / 'synthetic'


class Attr:
    def __init__(self, name, size, mtime):
        self.filename, self.st_size, self.st_mtime, self.st_mode = name, size, mtime, stat.S_IFREG | 0o444


class FakeSFTP:
    """In-memory folder; a name in `growing` gains bytes once it has been read."""
    def __init__(self, files, growing=()):
        self.files, self.growing, self.closed = dict(files), set(growing), False

    def listdir_attr(self, root):
        return [Attr(n, len(d), i) for i, (n, d) in enumerate(self.files.items())] + [Attr('notes.txt', 3, 99)]

    def open(self, path, mode):
        import io
        name = path.rsplit('/', 1)[-1]
        data = self.files[name]
        if name in self.growing:
            self.files[name] = data + b'more'
        return io.BytesIO(data)

    def stat(self, path):
        name = path.rsplit('/', 1)[-1]
        return Attr(name, len(self.files[name]), 0)

    def close(self):
        self.closed = True


class Client:
    def close(self):
        pass


class PullPlanTests(unittest.TestCase):
    def config(self):
        return SFTPConfig('robot', 'reader', __file__, __file__, '/logs', 'robot-6391')

    def test_newest_held_back_growing_skipped_and_already_pulled_not_repeated(self):
        a, b = (FIXTURES / 'alpha7-main.wpilog').read_bytes(), (FIXTURES / 'alpha7-boot-b.wpilog').read_bytes()
        sftp = FakeSFTP({'akit_1.wpilog': a, 'akit_2.wpilog': b, 'akit_3.wpilog': a + b}, growing={'akit_2.wpilog'})
        with tempfile.TemporaryDirectory() as folder, closing(open_catalog(Path(folder))) as db:
            from robot_test_hub.importer import install_schema
            with db:
                install_schema(db)
            first = pull.collect(folder, db, self.config(), connect_fn=lambda c: (Client(), sftp))
            self.assertEqual(first['held_back_newest'], 'akit_3.wpilog')
            self.assertEqual(first['downloaded'], 1)
            self.assertEqual(first['problems'], [{'file': 'akit_2.wpilog', 'reason': 'still_being_written'}])
            self.assertEqual(first['imports'], {'imported': 1})
            self.assertEqual(list((Path(folder) / 'pull-staging').iterdir()), [])
            sftp.growing.clear()
            second = pull.collect(folder, db, self.config(), include_newest=True, connect_fn=lambda c: (Client(), sftp))
            self.assertEqual((second['downloaded'], second['already_pulled'], second['held_back_newest']), (2, 1, None))
            self.assertEqual(len(Importer(Path(folder), db).list_jobs()), 3)

    def test_unreachable_robot_is_a_plain_error(self):
        def refuse(config):
            raise OSError('no route')
        with tempfile.TemporaryDirectory() as folder, closing(open_catalog(Path(folder))) as db:
            with self.assertRaises(pull.PullError):
                pull.collect(folder, db, self.config(), connect_fn=refuse)


@unittest.skipIf(paramiko is None, 'Install the optional pinned sftp extra for loopback SSH qualification')
class PullNetworkTests(unittest.TestCase):
    def test_actual_ssh_listing_download_and_import(self):
        from test_sftp_network import LoopbackSFTP
        data = (FIXTURES / 'alpha7-main.wpilog').read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            server = LoopbackSFTP(folder, {'old.wpilog': data, 'new.wpilog': data + b'x'})
            self.addCleanup(server.close)
            base = server.files_type

            class Listing(base):
                def list_folder(self, path):
                    out = []
                    for index, (name, content) in enumerate(server.files.items()):
                        attributes = paramiko.SFTPAttributes()
                        attributes.filename, attributes.st_size = name, len(content)
                        attributes.st_mode, attributes.st_mtime = stat.S_IFREG | 0o444, 1000 + index
                        attributes.st_atime = attributes.st_mtime  # SFTP sends times only as a pair
                        out.append(attributes)
                    return out
            server.files_type = Listing
            config = SFTPConfig('127.0.0.1', 'reader', str(server.known_hosts), str(server.key_path), '/logs',
                                'robot-6391', port=server.port, timeout=.5)
            data_dir = Path(folder) / 'data'
            data_dir.mkdir()
            with closing(open_catalog(data_dir)) as db:
                from robot_test_hub.importer import install_schema
                with db:
                    install_schema(db)
                result = pull.collect(data_dir, db, config)
                self.assertEqual((result['listed'], result['downloaded'], result['held_back_newest']), (2, 1, 'new.wpilog'))
                self.assertEqual(result['imports'], {'imported': 1})


if __name__ == '__main__':
    unittest.main()
