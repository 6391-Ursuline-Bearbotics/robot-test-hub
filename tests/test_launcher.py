"""Launch profile tests use temporary private settings; no hardware links."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from robot_test_hub import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hub private ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.hub = self.root / 'hub settings.json'
        self.hub.write_text(json.dumps({'schema_version': 1, 'data_dir': 'archive', 'port': 6391}), encoding='utf-8')
        self.path = self.root / 'practice profile.json'
        self.values = {'schema_version': 1, 'mode': 'demo', 'hub_config': self.hub.name}
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.values), encoding='utf-8')

    def live(self):
        (self.root / 'wpilib').mkdir()
        (self.root / 'known_hosts').write_text('local fixture')
        (self.root / 'key').write_text('local fixture')
        source = dict(schema_version=1, host='robot.invalid', username='fixture', known_hosts='known_hosts',
                      private_key='key', log_root='/logs', robot_id='robot')
        (self.root / 'source.json').write_text(json.dumps(source))
        self.values.update(mode='live', source_config='source.json', nt_host='robot.invalid', nt_port=5810,
                           wpilib_install='wpilib')
        self.write()

    def record(self):
        value = dict(schema_version=1, port=6391, pid=123, token='a'*64, instance_id='b'*32)
        launcher.runtime_path(self.path).write_text(json.dumps(value))
        return value

    def test_relative_paths_and_spaces_are_not_shell_fragments(self):
        profile = launcher.load_profile(self.path)
        args = launcher.server_arguments(self.path, profile)
        self.assertEqual(profile['hub_config'], self.hub)
        self.assertEqual(args[args.index('--data-dir')+1], str(self.root / 'archive'))
        self.assertIn(str(self.hub), args)
        self.assertNotIn('--idle-delay', args)
        self.assertEqual(args[-1], str(self.path.with_suffix('.runtime.json')))

    @unittest.skipUnless(os.name == 'nt', 'Windows short-path aliases only')
    def test_actual_windows_short_path_alias_resolves_to_canonical_fixture(self):
        import ctypes
        from ctypes import wintypes
        get_short_path = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
        get_short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_short_path(str(self.root), buffer, len(buffer))
        if not length or length >= len(buffer):
            self.skipTest('Temporary filesystem does not expose a Windows short-path alias')
        alias = Path(buffer.value)
        if str(alias).casefold() == str(self.root).casefold():
            self.skipTest('Short-path alias creation is disabled on this filesystem')
        self.assertEqual(alias.resolve(), self.root)
        alias_profile = alias / self.path.name
        profile = launcher.load_profile(alias_profile)
        arguments = launcher.server_arguments(alias_profile, profile)
        self.assertEqual(profile['hub_config'], self.hub)
        self.assertIn(str(self.hub), arguments)
        self.assertEqual(arguments[arguments.index('--data-dir') + 1], str(self.root / 'archive'))
        self.assertEqual(arguments[-1], str(self.path.with_suffix('.runtime.json')))

    def test_launcher_never_changes_process_directory(self):
        with patch('os.chdir', side_effect=AssertionError('process-global chdir')):
            self.assertTrue(launcher.check_profile(self.path)['ready'])
            profile = launcher.load_profile(self.path)
            self.assertIn('--resolve-config-paths', launcher.server_arguments(self.path, profile))
            with patch('robot_test_hub.server.main', return_value=0):
                self.assertEqual(launcher.main(['run', '--profile', str(self.path)]), 0)

    def test_all_hub_storage_paths_anchor_to_hub_json(self):
        nested = self.root / 'settings'
        nested.mkdir()
        config = nested / 'hub.json'
        config.write_text(json.dumps(dict(schema_version=1, data_dir='../archive',
                                         backup_destination='../backup',
                                         log_export_destination='../exports')))
        self.values['hub_config'] = 'settings/hub.json'
        self.write()
        effective, explicit = launcher._inspect(launcher.load_profile(self.path))
        self.assertEqual(effective.data_dir, str(self.root / 'archive'))
        self.assertEqual(effective.backup_destination, str(self.root / 'backup'))
        self.assertEqual(effective.log_export_destination, str(self.root / 'exports'))
        self.assertFalse(explicit)

    def test_optional_marker_install_must_be_absolute(self):
        marker = dict(schema_version=1, enabled=True, nt_host='localhost', nt_port=5810,
                      robot_id='robot', runtime_mode='SIM', install='relative/wpilib')
        (self.root / 'marker.json').write_text(json.dumps(marker))
        self.values['marker_config'] = 'marker.json'
        self.write()
        self.assertFalse(launcher.check_profile(self.path)['ready'])

    def test_sim_marker_native_readiness_uses_shared_compiler_without_connections(self):
        marker = dict(schema_version=1, enabled=True, nt_host='localhost', nt_port=5810,
                      robot_id='robot', runtime_mode='SIM')
        (self.root / 'marker.json').write_text(json.dumps(marker))
        self.values['marker_config'] = 'marker.json'
        self.write()
        with patch('tools.status_bridge.run.prepare', return_value=([], {})) as prepare, \
             patch('socket.create_connection', side_effect=AssertionError('network')):
            report = launcher.check_profile(self.path)
        self.assertTrue(report['ready'])
        prepare.assert_called_once()
        self.assertTrue(any(item['name'] == 'marker_native_tools' for item in report['checks']))

    def test_live_rewind_identity_and_real_provenance_checked_before_links(self):
        self.live()
        rewind = dict(schema_version=1, enabled=True, camera_id='ll4',
                      origin='http://camera.invalid:5807', robot_id='robot', source_type='REAL')
        self.values['rewind_config'] = 'rewind.json'
        self.write()
        for changed in ({'robot_id':'different'}, {'source_type':'SYNTHETIC'}):
            (self.root / 'rewind.json').write_text(json.dumps(dict(rewind, **changed)))
            with self.subTest(changed=changed), \
                 patch('robot_test_hub.live_check.check_live') as check, \
                 patch('socket.getaddrinfo', side_effect=AssertionError('DNS')), \
                 patch('socket.create_connection', side_effect=AssertionError('network')), \
                 patch('sqlite3.connect', side_effect=AssertionError('archive')):
                self.assertFalse(launcher.check_profile(self.path)['ready'])
                check.assert_not_called()
        (self.root / 'rewind.json').write_text(json.dumps(rewind))
        with patch('robot_test_hub.live_check.check_live', return_value=dict(ready=True, checks=[])) as check:
            self.assertTrue(launcher.check_profile(self.path)['ready'])
            check.assert_called_once()
        self.assertFalse((self.root / 'archive').exists())

    def test_relative_recording_executables_rejected_even_if_invocation_file_exists(self):
        video = dict(schema_version=1, executable='relative-ffmpeg.exe',
                     probe_executable='relative-ffprobe.exe', version_pin='1.2.3', camera_id='fixture',
                     input_format='lavfi', camera_input='testsrc', source_type='SYNTHETIC')
        (self.root / 'video.json').write_text(json.dumps(video))
        self.values['video_config'] = 'video.json'
        self.write()
        with patch('pathlib.Path.is_file', return_value=True), \
             patch('socket.create_connection', side_effect=AssertionError('network')), \
             patch('sqlite3.connect', side_effect=AssertionError('archive')):
            self.assertFalse(launcher.check_profile(self.path)['ready'])

    def test_unknown_fields_and_explicit_mode_required(self):
        for extra in ({'mode': 'LIVE'}, {'mode': None}, {'command': 'powershell'}, {'token': 'secret'}):
            with self.subTest(extra=extra):
                self.values.update(extra)
                self.write()
                with self.assertRaises(launcher.LaunchError):
                    launcher.load_profile(self.path)
                self.values = {'schema_version': 1, 'mode': 'demo', 'hub_config': self.hub.name}

    def test_demo_rejects_live_and_rewind_options(self):
        for key in ('source_config', 'nt_host', 'nt_port', 'wpilib_install', 'rewind_config'):
            self.values[key] = 'anything'
            self.write()
            with self.assertRaises(launcher.LaunchError):
                launcher.load_profile(self.path)
            del self.values[key]

    def test_live_requires_every_explicit_field_and_valid_endpoint(self):
        self.live()
        valid = self.values.copy()
        for field in launcher.LIVE_FIELDS:
            self.values = {k: v for k, v in valid.items() if k != field}
            self.write()
            with self.assertRaises(launcher.LaunchError):
                launcher.load_profile(self.path)
        for host, port in [('user@host', 5810), ('http://host', 5810), ('host', True), ('host', 65536)]:
            self.values = dict(valid, nt_host=host, nt_port=port)
            self.write()
            with self.assertRaises(launcher.LaunchError):
                launcher.load_profile(self.path)

    def test_duplicate_oversized_and_future_schema_rejected(self):
        for raw in ['{"schema_version":1,"schema_version":1}', ' ' * 65537,
                    json.dumps(dict(self.values, schema_version=2))]:
            self.path.write_text(raw)
            with self.assertRaises(launcher.LaunchError):
                launcher.load_profile(self.path)

    def test_network_paths_and_private_release_paths_rejected(self):
        for path in ('//server/private/hub.json', '\\\\server\\private\\hub.json'):
            self.values['hub_config'] = path
            self.write()
            with self.assertRaises(launcher.LaunchError):
                launcher.load_profile(self.path)
        with self.assertRaises(launcher.LaunchError):
            launcher.load_profile(launcher.RELEASE_ROOT / 'profile.json')

    def test_demo_check_never_opens_network_or_archive(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError('DNS')), \
             patch('socket.create_connection', side_effect=AssertionError('network')), \
             patch('sqlite3.connect', side_effect=AssertionError('archive')):
            report = launcher.check_profile(self.path)
        self.assertTrue(report['ready'])
        self.assertFalse(report['network_contacted'])
        self.assertFalse(report['archive_opened'])
        self.assertFalse(report['hardware_qualified'])
        self.assertFalse((self.root / 'archive').exists())

    def test_live_check_preserves_exact_idle_delay_presence(self):
        self.live()
        good = dict(schema_version=1, ready=True, checks=[])
        with patch('robot_test_hub.live_check.check_live', return_value=good) as check:
            report = launcher.check_profile(self.path)
            self.assertTrue(report['ready'])
            self.assertFalse(check.call_args.kwargs['idle_delay_explicit'])
            raw = json.loads(self.hub.read_text())
            raw['idle_delay'] = 3
            self.hub.write_text(json.dumps(raw))
            launcher.check_profile(self.path)
            self.assertTrue(check.call_args.kwargs['idle_delay_explicit'])
            args = launcher.server_arguments(self.path, launcher.load_profile(self.path))
            self.assertNotIn('--idle-delay', args)
            self.assertIn('--source-config', args)

    def test_each_invalid_optional_config_fails_closed(self):
        for field in ('marker_config', 'video_config', 'video_media_config'):
            with self.subTest(field=field):
                (self.root / 'optional.json').write_text('{"schema_version":1}')
                self.values[field] = 'optional.json'
                self.write()
                self.assertFalse(launcher.check_profile(self.path)['ready'])
                del self.values[field]

    def test_missing_data_root_and_data_inside_release_fail(self):
        for raw in ({'schema_version': 1}, {'schema_version': 1, 'data_dir': str(launcher.RELEASE_ROOT / 'data')}):
            self.hub.write_text(json.dumps(raw))
            self.assertFalse(launcher.check_profile(self.path)['ready'])

    def test_analytics_pin_required_locally(self):
        self.hub.write_text(json.dumps({'schema_version':1, 'data_dir':'archive', 'analytics_enabled':True}))
        with patch('robot_test_hub.launcher.importlib.metadata.version', return_value='0.0'):
            report = launcher.check_profile(self.path)
        self.assertFalse(report['ready'])
        self.assertEqual(report['checks'][-1]['name'], 'analytics_dependency')

    def test_configure_no_overwrite_and_revision_retained(self):
        original = self.path.read_bytes()
        with self.assertRaises(launcher.LaunchError):
            launcher.configure_profile(self.path, self.values)
        self.assertEqual(self.path.read_bytes(), original)
        launcher.configure_profile(self.path, self.values, force=True)
        self.assertEqual(self.path.with_name(self.path.name + '.previous').read_bytes(), original)
        with self.assertRaises(FileExistsError):
            launcher.configure_profile(self.path, self.values, force=True)

    def test_configure_validation_failure_preserves_previous_profile(self):
        original = self.path.read_bytes()
        with self.assertRaises(launcher.LaunchError):
            launcher.configure_profile(self.path, dict(self.values, source_config='missing'), force=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.root.glob('.profile-*')))

    def test_stop_missing_record_does_not_connect(self):
        with patch.object(launcher, '_control') as control:
            self.assertFalse(launcher.stop_profile(self.path))
        control.assert_not_called()

    def test_runtime_strict_keys_types_and_duplicate_detection(self):
        valid = self.record()
        for change in ({'port':True}, {'port':65536}, {'pid':True}, {'token':'secret'},
                       {'instance_id':'bad'}, {'host':'example.com'}, {'schema_version':True}):
            launcher.runtime_path(self.path).write_text(json.dumps(dict(valid, **change)))
            with patch.object(launcher, '_control') as control:
                with self.assertRaises(launcher.LaunchError):
                    launcher.stop_profile(self.path)
                control.assert_not_called()
        launcher.runtime_path(self.path).write_text('{"pid":1,"pid":2}')
        with self.assertRaises(launcher.LaunchError):
            launcher.stop_profile(self.path)

    def test_stop_mismatched_instance_and_pid_never_posts_or_deletes(self):
        record = self.record()
        for identity in (dict(record, instance_id='c'*32), dict(record, pid=999), dict(record, pid=True)):
            with patch.object(launcher, '_control', return_value=identity) as control:
                with self.assertRaises(launcher.LaunchError):
                    launcher.stop_profile(self.path)
                self.assertEqual(control.call_count, 1)
                self.assertTrue(launcher.runtime_path(self.path).exists())

    def test_stop_valid_identity_requests_post_and_retains_runtime(self):
        record = self.record()
        with patch.object(launcher, '_control', side_effect=[dict(pid=record['pid'], instance_id=record['instance_id']), {'stopping':True}]) as control:
            self.assertTrue(launcher.stop_profile(self.path))
            self.assertEqual([call.args[1] for call in control.call_args_list], ['GET', 'POST'])
        self.assertTrue(launcher.runtime_path(self.path).exists())

    def test_control_ignores_proxy_and_rejects_redirect(self):
        record = self.record()
        conn = Mock()
        conn.getresponse.return_value.status = 302
        conn.getresponse.return_value.read.return_value = b'{}'
        with patch.dict(os.environ, {'HTTP_PROXY':'http://evil.invalid:80'}), \
             patch.object(launcher.http.client, 'HTTPConnection', return_value=conn) as connection:
            with self.assertRaises(launcher.LaunchError):
                launcher._control(record, 'POST')
        connection.assert_called_once_with('127.0.0.1',6391,timeout=2)
        request = conn.request.call_args
        self.assertEqual(request.args, ('POST','/api/v1/application/stop'))
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer '+record['token'])
        conn.close.assert_called_once()

    def test_open_pins_loopback_without_runtime_endpoint(self):
        with patch.object(launcher.webbrowser, 'open', return_value=True) as browser:
            self.assertEqual(launcher.main(['open','--profile',str(self.path)]),0)
        browser.assert_called_once_with('http://127.0.0.1:6391')

    def test_run_calls_foreground_server_with_list_and_no_delay_override(self):
        before = Path.cwd()
        with patch('robot_test_hub.server.main', return_value=0) as server:
            self.assertEqual(launcher.main(['run','--profile',str(self.path)]),0)
        self.assertEqual(Path.cwd(), before)
        self.assertIsInstance(server.call_args.kwargs['argv'],list)
        self.assertNotIn('--idle-delay',server.call_args.kwargs['argv'])

    def test_cli_errors_and_report_do_not_export_private_content(self):
        self.hub.write_text('secret-user:password@camera.invalid')
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(launcher.main(['check','--profile',str(self.path),'--json']),2)
        self.assertNotIn('secret-user',output.getvalue()+errors.getvalue())
        self.assertNotIn(str(self.root),output.getvalue()+errors.getvalue())


if __name__ == '__main__':
    unittest.main()
