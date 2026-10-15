"""Offline exercise uses real synthetic data flow and cleans its own storage."""
import builtins
from contextlib import redirect_stdout,redirect_stderr
import importlib
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from robot_test_hub import selftest


class SelftestTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='selftest tests ')
        self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name).resolve()
        self.allocations=[]
        self.original_mkdtemp=tempfile.mkdtemp

    def allocate(self,*args,**kwargs):
        if kwargs.get('prefix')=='robot-test-hub-selftest-':
            kwargs['dir']=self.base
            result=self.original_mkdtemp(*args,**kwargs)
            self.allocations.append(Path(result))
            return result
        return self.original_mkdtemp(*args,**kwargs)

    def run_exercise(self,**kwargs):
        before={thread.ident for thread in threading.enumerate()}
        with patch('robot_test_hub.selftest.tempfile.mkdtemp',side_effect=self.allocate):
            result=selftest.run_selftest(**kwargs)
        self.assertEqual(result['cleanup'],{'state':'passed'})
        self.assertTrue(all(not path.exists() for path in self.allocations))
        self.assertEqual({thread.ident for thread in threading.enumerate()},before)
        encoded=json.dumps(result)
        self.assertLess(len(encoded),6000)
        self.assertNotIn(str(self.base),encoded)
        self.assertEqual(tuple(row['id'] for row in result['stages']),selftest.STAGES)
        self.assertIs(result['hardware_qualified'],False)
        self.assertIs(result['network_contacted'],False)
        self.assertIs(result['active_archive_opened'],False)
        self.assertEqual(result['overall_health'],'not_assessed')
        return result

    def test_bundled_original_and_literal_pin_match_official_fixture(self):
        official=Path(__file__).parent/'fixtures/synthetic/alpha7-main.wpilog'
        self.assertEqual(selftest._sample(),official.read_bytes())
        expected=json.loads((official.parent/'expected.json').read_text(encoding='utf-8'))['files'][0]
        self.assertEqual((selftest.SAMPLE_SHA256,selftest.SAMPLE_SIZE),(expected['sha256'],expected['size_bytes']))
        self.assertEqual(list(selftest.CYCLE_TIMESTAMPS),list(map(str,expected['timestamp_ns'])))

    def test_actual_core_twice_without_sockets_subprocess_or_duckdb(self):
        original_import=builtins.__import__
        def importing(name,*args,**kwargs):
            if name=='duckdb' or name.startswith('tests.'):raise AssertionError('optional/runtime test import forbidden')
            return original_import(name,*args,**kwargs)
        with patch.object(socket.socket,'connect',side_effect=AssertionError('no network')),patch('socket.create_connection',side_effect=AssertionError('no network')),patch('subprocess.Popen',side_effect=AssertionError('no processes')),patch('builtins.__import__',side_effect=importing):
            for _ in range(2):
                result=self.run_exercise()
                self.assertTrue(result['success'],result)
                self.assertEqual(result['stages'][-1]['code'],'analytics_not_requested')
        self.assertEqual(len(self.allocations),2)

    def test_import_is_quiet_and_api_has_no_archive_parameters(self):
        with patch('tempfile.mkdtemp',side_effect=AssertionError('no allocation')),patch('socket.socket',side_effect=AssertionError('no network')),patch('subprocess.Popen',side_effect=AssertionError('no process')):
            importlib.reload(selftest)
        for kwargs in ({'root':self.base},{'config':self.base},{'source_path':self.base}):
            with self.assertRaises(TypeError):selftest.run_selftest(**kwargs)
        with self.assertRaises(ValueError):selftest.run_selftest(analytics=1)

    def test_gating_restart_uses_preserved_partial_offset(self):
        from robot_test_hub.collector import Collector
        from robot_test_hub.storage import DataRootOwner
        reads=[];closes=[]
        original_read=selftest._SyntheticSource.read
        original_close=Collector.close
        def reading(source,identity,offset,length):
            self.assertFalse(source.enabled or source.stale)
            reads.append((offset,length))
            return original_read(source,identity,offset,length)
        def closing(collector):
            closes.append((collector.root,collector.db.execute('SELECT offset FROM files').fetchone()[0]))
            return original_close(collector)
        with patch.object(selftest._SyntheticSource,'read',reading),patch.object(Collector,'close',closing):
            result=self.run_exercise()
        self.assertTrue(result['success'])
        self.assertEqual(reads[0],(0,512))
        self.assertEqual(reads[1],(512,512))
        self.assertEqual(closes[0][1],512)
        self.assertEqual(closes[1][1],selftest.SAMPLE_SIZE)
        self.assertEqual(len(closes),2)
        # A fully finished exercise releases its data-root lease too.
        for root,_ in closes:
            root.mkdir(parents=True,exist_ok=True)
            owner=DataRootOwner(root);owner.close();shutil.rmtree(root.parent)

    def test_missing_tampered_sample_and_changed_manifest_fail_closed(self):
        package=self.base/'package';sample=package/'sample_data';sample.mkdir(parents=True)
        source=Path(selftest.__file__).parent/'sample_data'
        for condition in ('missing','bytes','manifest'):
            with self.subTest(condition=condition):
                shutil.copyfile(source/'manifest.json',sample/'manifest.json')
                if condition=='missing':
                    (sample/'alpha7-main.wpilog').unlink(missing_ok=True)
                else:(sample/'alpha7-main.wpilog').write_bytes(selftest._sample()+ (b'x' if condition=='bytes' else b''))
                if condition=='manifest':(sample/'manifest.json').write_bytes(b'{"schema_version":999}')
                with patch('robot_test_hub.selftest.resources.files',return_value=package):result=self.run_exercise()
                self.assertFalse(result['success'])
                self.assertEqual(result['stages'][0]['state'],'failed')
        self.assertEqual(self.allocations,[])

    def test_import_and_backup_failures_stop_dependents_cleanup_and_redact(self):
        from robot_test_hub.backup import Backup
        from robot_test_hub.pipeline import Pipeline
        for target,stage in ((Pipeline,'import_index'),(Backup,'backup_restore')):
            with self.subTest(stage=stage):
                method='tick' if target is Pipeline else 'create'
                with patch.object(target,method,side_effect=OSError('C:/private/password=secret')):
                    result=self.run_exercise()
                self.assertFalse(result['success'])
                stages={row['id']:row for row in result['stages']}
                self.assertEqual(stages[stage]['state'],'failed')
                self.assertEqual(stages['analytics']['state'],'skipped')
                self.assertNotIn('secret',json.dumps(result))
                self.assertNotIn('private',json.dumps(result))

    def test_analytics_missing_and_wrong_pin_are_requested_failures(self):
        from robot_test_hub.columnar import DUCKDB_VERSION
        for value in (None,type('WrongDuck',(),{'__version__':'0.0.0'})()):
            with self.subTest(value=value):
                with patch.dict(sys.modules,{'duckdb':value}):result=self.run_exercise(analytics=True)
                self.assertFalse(result['success'])
                self.assertTrue(result['analytics_requested'])
                self.assertEqual(result['stages'][-1]['state'],'failed')
                self.assertEqual(result['stages'][-1]['code'],'selftest_analytics_failed')

    def test_requested_real_analytics_converts_queries_and_summarizes(self):
        from robot_test_hub.columnar import dependency
        try:dependency()
        except ValueError:self.skipTest('Pinned analytics extra is unavailable')
        result=self.run_exercise(analytics=True)
        self.assertTrue(result['success'],result)
        self.assertTrue(all(row['state']=='passed' for row in result['stages']))

    def test_cleanup_failure_is_explicit_and_not_a_pass(self):
        original=shutil.rmtree
        def removal(path,*args,**kwargs):
            if Path(path) in self.allocations:raise OSError('private cleanup detail')
            return original(path,*args,**kwargs)
        with patch('robot_test_hub.selftest.tempfile.mkdtemp',side_effect=self.allocate),patch('robot_test_hub.selftest.shutil.rmtree',side_effect=removal):
            result=selftest.run_selftest()
        self.assertFalse(result['success'])
        self.assertEqual(result['cleanup'],{'state':'failed'})
        self.assertTrue(self.allocations[0].exists())
        self.assertNotIn('private',json.dumps(result))
        original(self.allocations[0])

    def test_allocation_failure_has_explicit_failed_stage(self):
        with patch('robot_test_hub.selftest.tempfile.mkdtemp',side_effect=OSError('private temp path')):
            result=selftest.run_selftest()
        self.assertFalse(result['success'])
        self.assertEqual(result['stages'][0]['state'],'passed')
        self.assertEqual(result['stages'][1]['state'],'failed')
        self.assertEqual(result['stages'][1]['id'],'transfer_gating')
        self.assertEqual(result['cleanup'],{'state':'passed'})
        self.assertNotIn('private',json.dumps(result))

    def test_failed_close_retains_owner_and_does_not_delete_workspace(self):
        from robot_test_hub.collector import Collector
        from robot_test_hub.storage import DataRootOwner,OwnershipError
        with patch('robot_test_hub.selftest.tempfile.mkdtemp',side_effect=self.allocate),patch.object(Collector,'close',side_effect=OSError('private close detail')):
            result=selftest.run_selftest()
        self.assertFalse(result['success'])
        self.assertEqual(result['cleanup'],{'state':'failed'})
        workspace,collector,db,owner=selftest._RETAINED_CLEANUP.pop()
        self.assertTrue(workspace.exists())
        with self.assertRaises(OwnershipError):DataRootOwner(collector.root)
        collector.close()
        shutil.rmtree(workspace)

    def test_main_json_exit_status_and_rejects_external_paths(self):
        output=io.StringIO()
        with redirect_stdout(output):status=selftest.main(['--json'])
        self.assertEqual(status,0)
        self.assertTrue(json.loads(output.getvalue())['success'])
        with patch('robot_test_hub.selftest._sample',side_effect=OSError('secret')),redirect_stdout(io.StringIO()) as failed:
            self.assertEqual(selftest.main(['--json']),1)
        self.assertNotIn('secret',failed.getvalue())
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):selftest.main(['--data-dir',str(self.base)])


if __name__=='__main__':unittest.main()
