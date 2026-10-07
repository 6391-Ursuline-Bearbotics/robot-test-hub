from pathlib import Path
import contextlib
import hashlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub.backup import Backup, BackupError, restore, verify, main
from robot_test_hub.config import Config
from robot_test_hub.storage import open_catalog


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root, self.dest = self.base / 'source', self.base / 'second'
        self.root.mkdir()
        self.addCleanup(self.tmp.cleanup)
        self.db = open_catalog(self.root)
        self.addCleanup(self.db.close)
        self.payload = b'immutable original\n'
        self.sha = hashlib.sha256(self.payload).hexdigest()
        (self.root / 'archive').mkdir()
        (self.root / 'archive' / 'segment.logdata').write_bytes(self.payload)
        with self.db:
            self.db.execute("INSERT INTO files(id,name,size,sha256,created_at,offset,state) VALUES ('segment','private-original',?,?,0,?,'complete')", (len(self.payload), self.sha, len(self.payload)))
            self.db.execute("INSERT INTO transfer_meta(file_id,checkpoint_offset,checkpoint_digest) VALUES ('segment',?,?)", (len(self.payload), self.sha))
            self.db.execute("INSERT INTO settings VALUES ('paused','1')")
            self.db.execute("INSERT INTO annotation_revisions(event_id,revision,payload_json,hub_received_utc_ns) VALUES ('note',1,'{}',1)")
        self.backup = Backup(self.root, self.dest)

    def test_roundtrip_catalog_notes_pause_hashes_and_generations(self):
        result = self.backup.create('g1', configuration=Config(data_dir=str(self.root)))
        self.assertEqual(result['state'], 'complete')
        self.assertFalse(result['failure_domain_qualified'])
        folder = self.dest / 'g1'
        self.assertEqual(verify(folder)['file_count'], 3)
        recovered = self.base / 'restored'
        self.assertEqual(restore(folder, recovered)['state'], 'restored')
        self.assertEqual((recovered / 'archive' / 'segment.logdata').read_bytes(), self.payload)
        db = sqlite3.connect(recovered / 'catalog.sqlite3')
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0], '1')
        self.assertEqual(db.execute('SELECT event_id,revision FROM annotation_revisions').fetchone(), ('note', 1))
        self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        with self.db:
            self.db.execute("UPDATE settings SET value='0' WHERE key='paused'")
        first = (folder / 'manifest.json').read_bytes()
        self.backup.create('g1')
        self.assertEqual((folder / 'manifest.json').read_bytes(), first)
        self.backup.create('g2')
        self.assertTrue((self.dest / 'g1').is_dir())
        self.assertEqual(len(self.backup.status()), 2)

    def test_online_snapshot_captures_wal_commits(self):
        self.assertTrue((self.root / 'catalog.sqlite3-wal').exists())
        self.backup.create('wal')
        db = sqlite3.connect(self.dest / 'wal' / 'catalog.sqlite3')
        self.addCleanup(db.close)
        self.assertEqual(db.execute('SELECT count(*) FROM annotation_revisions').fetchone()[0], 1)
        self.assertFalse((self.dest / 'wal' / 'catalog.sqlite3-wal').exists())

    def test_interrupted_replication_reuses_pinned_snapshot_and_staging(self):
        from robot_test_hub import backup as module
        original = module._copy
        attempts = []
        def interrupt(source, target, sha, size):
            if target.is_relative_to(self.dest):
                attempts.append(target.name)
                if len(attempts) == 2:
                    raise OSError('credential=do-not-export')
            return original(source, target, sha, size)
        with patch.object(module, '_copy', side_effect=interrupt):
            with self.assertRaises(OSError):
                self.backup.create('retry')
        status = self.backup.status('retry')
        self.assertEqual(status['error_code'], 'OSError')
        self.assertNotIn('credential', json.dumps(status))
        self.assertFalse((self.dest / 'retry' / 'manifest.json').exists())
        with self.db:
            self.db.execute("UPDATE settings SET value='0' WHERE key='paused'")
        # Replication no longer relies on the original immutable bytes.
        (self.root / 'archive' / 'segment.logdata').write_bytes(b'corrupt later')
        self.assertEqual(self.backup.create('retry')['state'], 'complete')
        self.assertEqual((self.dest / 'retry' / 'archive' / 'segment.logdata').read_bytes(), self.payload)
        db = sqlite3.connect(self.dest / 'retry' / 'catalog.sqlite3')
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0], '1')

    def test_unavailable_destination_is_durable_retry(self):
        self.dest.write_text('not a mounted directory')
        with self.assertRaises(OSError):
            self.backup.create('offline')
        self.assertEqual(self.backup.status('offline')['state'], 'error')
        self.assertTrue((self.root / 'exports/backup-jobs/offline/image/manifest.json').exists())
        # Test fixture changes only; production never deletes failed destinations.
        self.dest.unlink()
        self.assertEqual(self.backup.create('offline')['state'], 'complete')

    def test_partial_checkpoint_excludes_uncommitted_tail_and_is_restorable(self):
        payload = b'committed'
        (self.root / 'partial').mkdir()
        (self.root / 'partial' / 'pending.part').write_bytes(payload + b'uncommitted')
        with self.db:
            self.db.execute("INSERT INTO files(id,name,size,sha256,created_at,offset,state) VALUES ('pending','pending',99,?,0,?,'queued')", ('a' * 64, len(payload)))
            self.db.execute("INSERT INTO transfer_meta(file_id,checkpoint_offset,checkpoint_digest) VALUES ('pending',?,?)", (len(payload), hashlib.sha256(payload).hexdigest()))
        self.backup.create('partial')
        copied = self.dest / 'partial' / 'partial/pending.part'
        self.assertEqual(copied.read_bytes(), payload)
        recovered = self.base / 'recovered'
        restore(self.dest / 'partial', recovered)
        self.assertEqual((recovered / 'partial/pending.part').read_bytes(), payload)
        self.assertEqual((self.root / 'partial/pending.part').read_bytes(), payload + b'uncommitted')

    def test_importer_raw_and_derived_refs_roundtrip(self):
        raw_path = f'raw/{self.sha}.wpilog'
        (self.root / 'raw').mkdir()
        (self.root / raw_path).write_bytes(self.payload)
        (self.root / 'derived/job').mkdir(parents=True)
        dataset, manifest = b'{"kind":"cycle"}\n', b'{"source_sha256":"' + self.sha.encode() + b'"}\n'
        (self.root / 'derived/job/signals.jsonl').write_bytes(dataset)
        (self.root / 'derived/job/manifest.json').write_bytes(manifest)
        with self.db:
            self.db.execute("INSERT INTO import_artifacts(sha256,size_bytes,relative_path,source_type,created_utc_ns) VALUES (?,?,?,'SYNTHETIC',1)", (self.sha, len(self.payload), raw_path))
            self.db.execute("INSERT INTO import_jobs(id,artifact_sha256,extractor_version,profile,mapping_revision,state,dataset_path,dataset_sha256,manifest_path,manifest_sha256,created_utc_ns,updated_utc_ns) VALUES ('job',?,'v','p','m','succeeded','derived/job/signals.jsonl',?,'derived/job/manifest.json',?,1,1)", (self.sha, hashlib.sha256(dataset).hexdigest(), hashlib.sha256(manifest).hexdigest()))
        self.backup.create('derived')
        self.assertEqual(verify(self.dest / 'derived')['file_count'], 5)
        restore(self.dest / 'derived', self.base / 'restore-derived')
        self.assertEqual((self.base / 'restore-derived/derived/job/signals.jsonl').read_bytes(), dataset)

    def test_missing_changed_original_and_checkpoint_digest_fail(self):
        (self.root / 'archive/segment.logdata').write_bytes(b'bad')
        with self.assertRaises(BackupError):
            self.backup.create('bad')
        self.assertFalse((self.dest / 'bad').exists())
        (self.root / 'archive/segment.logdata').write_bytes(self.payload)
        self.assertEqual(self.backup.create('bad')['state'], 'complete')

    def test_corruption_and_catalog_reference_tampering_detected(self):
        self.backup.create('corrupt')
        folder = self.dest / 'corrupt'
        (folder / 'archive/segment.logdata').write_bytes(b'bad')
        with self.assertRaises(BackupError):
            verify(folder)
        with self.assertRaises(BackupError):
            restore(folder, self.base / 'no-restore')
        self.assertFalse((self.base / 'no-restore').exists())
        (folder / 'archive/segment.logdata').write_bytes(self.payload)
        manifest = json.loads((folder / 'manifest.json').read_text())
        manifest['files'] = [item for item in manifest['files'] if item['path'] != 'archive/segment.logdata']
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BackupError, 'references|unexpected'):
            verify(folder)

    def test_path_escape_duplicate_future_schema_and_nonempty_restore_rejected(self):
        for destination in (self.root, self.root / 'backup', self.base):
            with self.assertRaises(BackupError):
                Backup(self.root, destination)
        self.backup.create('safe')
        with self.assertRaises(BackupError):
            self.backup.create('../escape')
        target = self.base / 'occupied'
        target.mkdir()
        (target / 'preserve').write_text('yes')
        with self.assertRaises(BackupError):
            restore(self.dest / 'safe', target)
        self.assertEqual((target / 'preserve').read_text(), 'yes')
        original = (self.dest / 'safe/manifest.json').read_text()
        for change in ('escape', 'duplicate', 'schema'):
            manifest = json.loads(original)
            if change == 'escape':
                manifest['files'][0]['path'] = '../outside'
            elif change == 'duplicate':
                manifest['files'].append(manifest['files'][0])
            else:
                manifest['schema_version'] = 999
            (self.dest / 'safe/manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(BackupError):
                verify(self.dest / 'safe')

    def test_configuration_generation_destination_immutable_and_existing_conflict(self):
        self.backup.create('locked', configuration=Config())
        with self.assertRaises(BackupError):
            self.backup.create('locked', configuration=Config(port=1234))
        with self.assertRaises(BackupError):
            Backup(self.root, self.base / 'other').create('locked')
        (self.dest / 'locked/archive/segment.logdata').write_bytes(b'existing different data')
        with self.assertRaises(BackupError):
            self.backup.create('locked')
        self.assertEqual((self.dest / 'locked/archive/segment.logdata').read_bytes(), b'existing different data')

    def test_identity_conflict_preserves_quarantine_and_terminal_catalog_latch(self):
        (self.root / 'partial').mkdir()
        evidence = b'unsafe identity evidence'
        (self.root / 'partial/segment.invalid').write_bytes(evidence)
        with self.db:
            self.db.execute("UPDATE files SET state='error' WHERE id='segment'")
            self.db.execute("UPDATE transfer_meta SET error_code='identity_conflict' WHERE file_id='segment'")
        self.backup.create('conflict')
        self.assertEqual((self.dest / 'conflict/partial/segment.invalid').read_bytes(), evidence)
        target = self.base / 'restore-conflict'
        restore(self.dest / 'conflict', target)
        with contextlib.closing(sqlite3.connect(target / 'catalog.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT error_code FROM transfer_meta WHERE file_id='segment'").fetchone()[0], 'identity_conflict')
        self.assertEqual((target / 'partial/segment.invalid').read_bytes(), evidence)

    def test_incomplete_catalog_and_unsupported_extra_file_fail_even_with_rehashed_manifest(self):
        self.backup.create('structure')
        folder = self.dest / 'structure'
        manifest = json.loads((folder / 'manifest.json').read_text())
        with contextlib.closing(sqlite3.connect(folder / 'catalog.sqlite3')) as db:
            db.execute('DROP TABLE annotation_revisions')
            db.commit()
        data = (folder / 'catalog.sqlite3').read_bytes()
        item = next(item for item in manifest['files'] if item['path'] == 'catalog.sqlite3')
        item.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BackupError, 'incomplete'):
            verify(folder)
        self.backup.create('extras')
        folder = self.dest / 'extras'
        manifest = json.loads((folder / 'manifest.json').read_text())
        (folder / 'catalog.sqlite3-wal').write_bytes(b'unsafe')
        manifest['files'].append({'path':'catalog.sqlite3-wal','sha256':hashlib.sha256(b'unsafe').hexdigest(),'size':6,'kind':'raw','prefix':False})
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(BackupError, 'unsupported'):
            verify(folder)

    def test_capture_retry_keeps_original_catalog_and_rejects_ads_paths(self):
        from robot_test_hub import backup as module
        original = module._copy
        def interrupted_capture(source, target, sha, size):
            if target.is_relative_to(self.root / 'exports'):
                raise OSError('local staging interrupted')
            return original(source,target,sha,size)
        with patch.object(module, '_copy', side_effect=interrupted_capture):
            with self.assertRaises(OSError):
                self.backup.create('capture-retry', configuration=Config(port=1234))
        with self.db:
            self.db.execute("UPDATE settings SET value='0' WHERE key='paused'")
        self.backup.create('capture-retry')
        self.assertEqual(json.loads((self.dest/'capture-retry/hub-config.json').read_text(encoding='utf-8'))['port'],1234)
        with contextlib.closing(sqlite3.connect(self.dest / 'capture-retry/catalog.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0], '1')
        with self.db:
            self.db.execute("INSERT INTO import_artifacts(sha256,size_bytes,relative_path,source_type,created_utc_ns) VALUES (?,?,?,'SYNTHETIC',1)", (self.sha,len(self.payload),'raw/file:stream'))
        with self.assertRaisesRegex(BackupError, 'path'):
            self.backup.create('ads')

    def test_restore_to_existing_empty_and_cli_redacted_failure(self):
        self.backup.create('empty')
        empty = self.base / 'empty'
        empty.mkdir()
        self.assertEqual(restore(self.dest / 'empty', empty)['state'], 'restored')
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['verify', str(self.base / 'unknown')]), 1)
        self.assertEqual(json.loads(output.getvalue()), {'state': 'error', 'error_code': 'BackupError'})

    def test_unmanifested_sidecar_and_restore_staging_leftovers_are_preserved_and_rejected(self):
        self.backup.create('inventory')
        folder = self.dest / 'inventory'
        sidecar = folder / 'catalog.sqlite3-wal'
        sidecar.write_bytes(b'unverified sidecar')
        with self.assertRaisesRegex(BackupError, 'unexpected'):
            verify(folder)
        self.assertEqual(sidecar.read_bytes(), b'unverified sidecar')
        sidecar.unlink()  # fixture only; production preserves unexpected evidence.
        target = self.base / 'restore-inventory'
        image = self.base / '.restore-restore-inventory-inventory' / 'image'
        image.mkdir(parents=True)
        leftover = image / 'unverified.txt'
        leftover.write_bytes(b'preserve this evidence')
        with self.assertRaisesRegex(BackupError, 'unexpected'):
            restore(folder, target)
        self.assertFalse(target.exists())
        self.assertEqual(leftover.read_bytes(), b'preserve this evidence')

    def test_linked_temporary_files_never_overwrite_external_evidence(self):
        from robot_test_hub import backup as module
        external = self.base / 'external-evidence'
        external.write_bytes(b'original evidence')
        temporary = self.base / 'output.writing'
        try:
            temporary.symlink_to(external)
        except OSError:
            self.skipTest('Local account cannot create symlinks')
        with self.assertRaisesRegex(BackupError, 'links'):
            module._write(self.base / 'output', b'replacement')
        with self.assertRaisesRegex(BackupError, 'links'):
            module._copy(self.root / 'archive/segment.logdata', self.base / 'output', self.sha, len(self.payload))
        self.assertEqual(external.read_bytes(), b'original evidence')


if __name__ == '__main__':
    unittest.main()
