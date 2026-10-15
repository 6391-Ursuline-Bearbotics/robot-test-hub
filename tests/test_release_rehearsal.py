"""Actual historical source execution and bounded rehearsal failure handling."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from robot_test_hub import release_rehearsal as rehearsal

ROOT = Path(__file__).resolve().parents[1]
OLD = 'c1b3261e3d48ad5cb029cca939ebeaf88cc4f4d0'
NEW = 'f9bc2fca3b082b5f997af75709444ef6bbf509c1'


class ReleaseRehearsalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        git = shutil.which('git')
        if not git:
            raise unittest.SkipTest('Historical release rehearsal requires local Git objects; child boundary cases still run.')
        env = dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
        for commit in (OLD, NEW):
            available = subprocess.run([git, 'cat-file', '-e', commit + '^{commit}'], cwd=ROOT, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if available.returncode:
                raise unittest.SkipTest('Pinned historical source objects are absent; real schema7-to8 rehearsal requires a full local history.')
        cls.temporary = tempfile.TemporaryDirectory(prefix='rehearsal-test-releases-')
        cls.base = Path(cls.temporary.name).resolve()
        try:
            from robot_test_hub import release
            cls.releases = []
            for commit in (OLD, NEW):
                release.build(ROOT, cls.base / 'releases', commit=commit)
                cls.releases.append(cls.base / 'releases' / ('robot-test-hub-' + commit))
        except BaseException:
            cls.temporary.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_actual_schema_seven_to_eight_upgrade_and_clean_restore_rollback(self):
        report = rehearsal.run_rehearsal(*self.releases, python=Path(sys.executable))
        self.assertTrue(report['success'], report)
        self.assertEqual((report['schema_before'], report['schema_after'], report['schema_rollback']), (7, 8, 7))
        self.assertTrue(report['migration_exercised'])
        self.assertEqual((report['older_commit'], report['newer_commit']), (OLD, NEW))
        self.assertEqual(report['cleanup']['state'], 'passed')
        self.assertEqual({stage['state'] for stage in report['stages']}, {'passed'})
        self.assertFalse(report['network_contacted'])
        self.assertFalse(report['active_archive_opened'])
        self.assertFalse(report['hardware_qualified'])
        self.assertEqual(rehearsal._ACTIVE_CHILDREN, [])

    def test_same_schema_does_not_claim_migration_and_old_selftest_is_optional(self):
        report = rehearsal.run_rehearsal(self.releases[0], self.releases[0], python=Path(sys.executable))
        self.assertTrue(report['success'], report)
        self.assertEqual((report['schema_before'], report['schema_after'], report['schema_rollback']), (7, 7, 7))
        self.assertFalse(report['migration_exercised'])
        stage = next(item for item in report['stages'] if item['id'] == 'newer_exercise')
        self.assertEqual(stage['code'], 'release_selftest_unavailable')

    def test_explicit_python_and_no_external_archive_cli(self):
        report = rehearsal.run_rehearsal(*self.releases, python=Path('python'))
        self.assertFalse(report['success'])
        self.assertEqual(report['stages'][0]['code'], 'rehearsal_verify_extract_failed')
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rehearsal.main(['--older', str(self.releases[0]), '--newer', str(self.releases[1]), '--python', sys.executable, '--data-dir', 'PRIVATE_ARCHIVE'])

    def test_tampered_release_never_executes_any_child(self):
        with tempfile.TemporaryDirectory() as name:
            release = Path(name) / 'tampered'
            shutil.copytree(self.releases[0], release)
            with (release / 'source.zip').open('ab') as stream:
                stream.write(b'tampered')
            with patch.object(rehearsal, '_run', side_effect=AssertionError('must not execute')) as execute:
                report = rehearsal.run_rehearsal(release, self.releases[1], python=Path(sys.executable))
            self.assertFalse(report['success'])
            execute.assert_not_called()

    def test_upgrade_failure_stops_followups_redacts_and_cleans_owned_workspace(self):
        calls = []
        actual = rehearsal._run
        def run(python, source, workspace, mode, analytics=False):
            calls.append((workspace, mode))
            if mode == 'upgrade':
                raise ValueError('PRIVATE_PATH SECRET_VALUE')
            return actual(python, source, workspace, mode, analytics)
        with patch.object(rehearsal, '_run', side_effect=run):
            report = rehearsal.run_rehearsal(*self.releases, python=Path(sys.executable))
        self.assertFalse(report['success'])
        self.assertEqual([mode for _, mode in calls], ['prepare', 'upgrade'])
        self.assertTrue(all(not workspace.exists() for workspace, _ in calls))
        self.assertNotIn('PRIVATE_PATH', json.dumps(report))
        self.assertNotIn('SECRET_VALUE', json.dumps(report))
        self.assertEqual(report['cleanup']['state'], 'passed')

    def test_cleanup_failure_cannot_be_reported_as_success(self):
        retained = []
        original = shutil.rmtree
        def fail(path, *args, **kwargs):
            retained.append(Path(path))
            raise PermissionError('PRIVATE_CLEANUP_PATH')
        try:
            with patch.object(rehearsal.shutil, 'rmtree', side_effect=fail):
                report = rehearsal.run_rehearsal(*self.releases, python=Path(sys.executable))
            self.assertFalse(report['success'])
            self.assertEqual(report['cleanup']['state'], 'failed')
            self.assertNotIn('PRIVATE_CLEANUP_PATH', json.dumps(report))
            self.assertTrue(retained and all(path.exists() for path in retained))
        finally:
            for path in retained:
                if path.exists():
                    original(path)

    def test_requested_analytics_cannot_be_skipped_by_old_release(self):
        report = rehearsal.run_rehearsal(self.releases[0], self.releases[0], python=Path(sys.executable), analytics=True)
        self.assertFalse(report['success'])
        self.assertEqual(report['stages'][-1]['state'], 'failed')
        self.assertEqual(report['stages'][-1]['id'], 'newer_exercise')


class ReleaseChildBoundaryTests(unittest.TestCase):
    def run_driver(self, body):
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name).resolve()
            source = workspace / 'extracted source'
            source.mkdir()
            with patch.object(rehearsal, 'DRIVER', body):
                return rehearsal._run(Path(sys.executable), source, workspace, 'restore')

    def test_actual_isolated_interpreter_and_temp_environment(self):
        body = """
import json,os,sys,tempfile
from pathlib import Path
assert sys.flags.isolated == 1
assert 'PYTHONPATH' not in os.environ and 'PYTHONHOME' not in os.environ
assert Path(tempfile.gettempdir()).resolve()==Path(sys.argv[2]).resolve()/'t'
assert Path.cwd()==Path(sys.argv[2]).resolve()
print(json.dumps({'schema_version':1,'success':True}))
"""
        with patch.dict('os.environ', {'PYTHONPATH':'DO_NOT_IMPORT', 'PYTHONHOME':'DO_NOT_LOAD'}):
            self.assertTrue(self.run_driver(body)['success'])

    def test_actual_timeout_and_output_limit_reap_child(self):
        for body in ("import time;time.sleep(20)", "import sys;sys.stdout.write('X'*100000);sys.stdout.flush();import time;time.sleep(20)"):
            with self.subTest(body=body), patch.object(rehearsal, 'CHILD_TIMEOUT', .2):
                with self.assertRaises(rehearsal.RehearsalError):
                    self.run_driver(body)
                self.assertEqual(rehearsal._ACTIVE_CHILDREN, [])

    def test_malformed_and_unbounded_receipts_are_rejected(self):
        for receipt in ({'schema_version':True,'success':True}, {'schema_version':1,'success':True,'private':'SECRET'}, {'schema_version':1,'success':False}):
            with self.subTest(receipt=receipt):
                with self.assertRaises(rehearsal.RehearsalError):
                    self.run_driver('print('+repr(json.dumps(receipt))+')')


if __name__ == '__main__':
    unittest.main()
