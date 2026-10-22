"""Independent adversarial manual-pull checks using synthetic data and loopback only."""
from contextlib import closing, contextmanager
import io
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub import pull
from robot_test_hub.importer import Importer
from robot_test_hub.sftp_source import SFTPConfig
from robot_test_hub.storage import open_catalog
from robot_test_hub.transfer import TransferCancelled

try:
    import paramiko
except ImportError:
    paramiko = None

PAYLOAD = Path(__file__).parent / 'fixtures/synthetic/alpha7-main.wpilog'


class LivePermission:
    def __init__(self):
        self.revoked = threading.Event()

    def check(self):
        if self.revoked.is_set():
            raise TransferCancelled('Synthetic permission revoked')


@contextmanager
def free_slot():
    yield


class MemorySFTP:
    def __init__(self, data):
        self.data = data
        self.closed = threading.Event()

    def attribute(self):
        from types import SimpleNamespace
        return SimpleNamespace(filename='old.wpilog', st_size=len(self.data),
                               st_mode=stat.S_IFREG | 0o444, st_mtime=1)

    def listdir_attr(self, root):
        return [self.attribute()]

    def stat(self, path):
        return self.attribute()

    def open(self, path, mode):
        return io.BytesIO(self.data)

    def close(self):
        self.closed.set()


class Client:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class PullIndependentReviewTests(unittest.TestCase):
    def config(self):
        return SFTPConfig('synthetic-host', 'reader', __file__, __file__, '/logs', 'robot-6391')

    def test_legacy_name_size_receipt_cannot_suppress_capture_or_be_rewritten(self):
        data = PAYLOAD.read_bytes()
        legacy = ('old.wpilog', len(data), 1, '0' * 64, 17)
        with tempfile.TemporaryDirectory() as folder, closing(open_catalog(Path(folder))) as db:
            with db:
                pull.install_schema(db)
                db.execute('INSERT INTO pulled_files VALUES (?,?,?,?,?)', legacy)
            result = pull.collect(folder, db, self.config(), permission=LivePermission(),
                                  bulk_slot=free_slot, include_newest=True,
                                  connect_fn=lambda config: (Client(), MemorySFTP(data)))
            self.assertEqual(result['state'], 'complete', result)
            self.assertEqual(result['downloaded'], 1)
            self.assertEqual(tuple(db.execute('SELECT * FROM pulled_files').fetchone()), legacy)
            jobs = Importer(Path(folder), db).list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(db.execute('SELECT source_type FROM import_artifacts').fetchone()[0], 'MANUAL_LOCAL')
            snapshots = list((Path(folder) / 'pull-staging').rglob('*.wpilog'))
            self.assertEqual([path.read_bytes() for path in snapshots], [data])
            receipts = [json.loads((Path(folder) / path).read_text()) for path in result['receipts']]
            self.assertTrue(all(row['closed_segment_verified'] is False for row in receipts))
            self.assertTrue(all(row['source']['robot_id'] == 'robot-6391' for row in receipts))

    def test_waiting_second_collection_cannot_connect_until_first_owned_client_closes(self):
        data = PAYLOAD.read_bytes()
        first_permission, second_permission = LivePermission(), LivePermission()
        first_read, release_read = threading.Event(), threading.Event()
        second_waiting, second_connected = threading.Event(), threading.Event()
        lock = threading.Lock()
        slot_calls = []
        first_client, second_client = Client(), Client()
        first_sftp, second_sftp = MemorySFTP(data), MemorySFTP(data)
        original_close = first_client.close
        close_attempts = []

        def close_first():
            close_attempts.append(1)
            if len(close_attempts) == 1:
                raise OSError('Synthetic transient close failure')
            original_close()
            release_read.set()
        first_client.close = close_first

        class BlockingRead(io.BytesIO):
            def read(self, length=-1):
                first_read.set()
                if not release_read.wait(2):
                    raise AssertionError('Permission watcher did not close the owned client')
                return super().read(length)
        first_sftp.open = lambda path, mode: BlockingRead(data)

        @contextmanager
        def slot():
            slot_calls.append(threading.current_thread().name)
            if len(slot_calls) == 2:
                second_waiting.set()
            with lock:
                yield

        def connect_second(config):
            self.assertTrue(first_client.closed.is_set())
            self.assertTrue(first_sftp.closed.is_set())
            second_connected.set()
            return second_client, second_sftp

        results, errors = {}, []
        with tempfile.TemporaryDirectory() as folder:
            def run(name, permission, connect_fn):
                try:
                    root = Path(folder) / name
                    root.mkdir()
                    with closing(open_catalog(root)) as db:
                        results[name] = pull.collect(root, db, self.config(), permission=permission,
                                                     bulk_slot=slot, include_newest=True, connect_fn=connect_fn)
                except BaseException as error:
                    errors.append(error)
            first = threading.Thread(target=run, name='review-first',
                                     args=('first', first_permission, lambda config: (first_client, first_sftp)))
            second = threading.Thread(target=run, name='review-second',
                                      args=('second', second_permission, connect_second))
            first.start()
            try:
                self.assertTrue(first_read.wait(2), (results, errors))
                second.start()
                self.assertTrue(second_waiting.wait(2))
                self.assertFalse(second_connected.is_set())
                first_permission.revoked.set()
                first.join(2)
                second.join(2)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results['first']['state'], 'interrupted')
                self.assertEqual(results['second']['state'], 'complete')
                parts = list((Path(folder) / 'first/pull-staging').rglob('*.part'))
                self.assertEqual(len(parts), 1)
                self.assertEqual(parts[0].read_bytes(), b'')
                self.assertEqual(list((Path(folder) / 'first/pull-staging').rglob('*.wpilog')), [])
                self.assertTrue(second_client.closed.is_set())
                self.assertGreaterEqual(len(close_attempts), 2)
            finally:
                first_permission.revoked.set()
                second_permission.revoked.set()
                release_read.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)

    @unittest.skipIf(paramiko is None, 'Install optional pinned sftp extra for synthetic loopback qualification')
    def test_actual_blocked_sftp_read_revocation_discards_bytes_and_joins_owned_watcher(self):
        from test_sftp_network import LoopbackSFTP
        data = PAYLOAD.read_bytes()
        permission = LivePermission()
        clients, results, errors = [], [], []
        original_client = paramiko.SSHClient

        class ObservedClient(original_client):
            def __init__(self):
                super().__init__()
                self.review_closed = threading.Event()
                clients.append(self)
            def connect(self, *args, **kwargs):
                super().connect(*args, **kwargs)
                transport = self.get_transport()
                original_open = transport.open_session
                def observed_open(*args, **kwargs):
                    self.review_channel = original_open(*args, **kwargs)
                    return self.review_channel
                transport.open_session = observed_open
            def close(self):
                super().close()
                self.review_closed.set()

        with tempfile.TemporaryDirectory() as folder:
            server = LoopbackSFTP(folder, {'old.wpilog': data})
            base = server.files_type
            class Listing(base):
                def list_folder(self, path):
                    item = paramiko.SFTPAttributes()
                    item.filename, item.st_size = 'old.wpilog', len(data)
                    item.st_mode, item.st_mtime, item.st_atime = stat.S_IFREG | 0o444, 1, 1
                    return [item]
                def stat(self, path):
                    item = super().stat(path)
                    if isinstance(item, paramiko.SFTPAttributes):
                        item.st_mtime, item.st_atime = 1, 1
                    return item
            server.files_type = Listing
            server.block = True
            config = SFTPConfig('127.0.0.1', 'reader', str(server.known_hosts), str(server.key_path),
                                '/logs', 'robot-6391', port=server.port, timeout=1, max_read=128)
            root = Path(folder) / 'archive'
            root.mkdir()
            def run():
                try:
                    with closing(open_catalog(root)) as db:
                        results.append(pull.collect(root, db, config, permission=permission,
                                                    bulk_slot=free_slot, include_newest=True))
                except BaseException as error:
                    errors.append(error)
            worker = threading.Thread(target=run, name='review-loopback-pull')
            try:
                with patch.object(paramiko, 'SSHClient', ObservedClient):
                    worker.start()
                    self.assertTrue(server.read_started.wait(3), (results, errors))
                    self.assertEqual(clients[0].review_channel.gettimeout(), config.timeout)
                    permission.revoked.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results[0]['state'], 'interrupted', results)
                self.assertEqual(results[0]['imports'], {})
                self.assertTrue(clients[0].review_closed.is_set())
                self.assertTrue(all(length <= 128 for _, _, length in server.requests))
                self.assertEqual([p.read_bytes() for p in root.rglob('*.part')], [b''])
                self.assertEqual(list(root.rglob('*.wpilog')), [])
                self.assertFalse(any(t.name == 'hub-pull-permission' and t.is_alive()
                                     for t in threading.enumerate()))
            finally:
                permission.revoked.set()
                server.close()
                if worker.ident is not None:
                    worker.join(3)


if __name__ == '__main__':
    unittest.main()
