"""Independent startup-controller review; all task operations are local mocks."""
import json
import os
from pathlib import Path
import subprocess
import unittest
import xml.etree.ElementTree as ET

from tests import test_windows_setup as setup_tests


@unittest.skipUnless(os.name == 'nt' and setup_tests.POWERSHELL, 'Requires Windows PowerShell')
class WindowsStartupIndependentReviewTest(unittest.TestCase):
    def setUp(self):
        self.fixture = setup_tests.WindowsSetupTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setup_environment()
        self.fixture.profile.parent.mkdir()
        self.fixture.profile.write_text('{}', encoding='utf-8')
        self.task_calls = self.fixture.root / 'task-calls.jsonl'
        self.existing_xml = self.fixture.root / 'existing-task.xml'
        self.shim = self.fixture.root / 'startup-review-shim.ps1'
        self.shim.write_text(r'''
param([string]$Action='Plan',[string]$Profile,[switch]$Replace)
$ErrorActionPreference='Stop'
function Record-TaskCall { param($Name,$Arguments)
 @{name=$Name; arguments=$Arguments} | ConvertTo-Json -Depth 8 -Compress | Add-Content -LiteralPath $env:HUB_REVIEW_TASK_CALLS -Encoding UTF8
}
function Get-ScheduledTask { [CmdletBinding()]param($TaskPath)
 Record-TaskCall 'get' $PSBoundParameters
 if ($env:HUB_REVIEW_DENY -eq 'get') { throw 'Synthetic school policy lookup denied' }
 if (Test-Path -LiteralPath $env:HUB_REVIEW_EXISTING) {
  [pscustomobject]@{TaskName=('RobotTestHub-Startup-'+[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value);State=$env:HUB_REVIEW_TASK_STATE}
 }
}
function Export-ScheduledTask { [CmdletBinding()]param($TaskName,$TaskPath)
 Record-TaskCall 'export' $PSBoundParameters
 Get-Content -LiteralPath $env:HUB_REVIEW_EXISTING -Raw
}
function Register-ScheduledTask { [CmdletBinding()]param($TaskName,$TaskPath,$Xml,[switch]$Force)
 Record-TaskCall 'register' $PSBoundParameters
 if ($env:HUB_REVIEW_DENY -eq 'register') { throw 'Synthetic school policy registration denied' }
}
function Unregister-ScheduledTask { [CmdletBinding(SupportsShouldProcess=$true)]param($TaskName,$TaskPath)
 Record-TaskCall 'unregister' $PSBoundParameters
 if ($env:HUB_REVIEW_DENY -eq 'unregister') { throw 'Synthetic school policy removal denied' }
}
function Get-ScheduledTaskInfo { [CmdletBinding()]param($TaskName,$TaskPath)
 Record-TaskCall 'info' $PSBoundParameters
 [pscustomobject]@{LastRunTime=[datetime]'2026-01-01';LastTaskResult=0}
}
function Start-ScheduledTask { Record-TaskCall 'forbidden-start' @{}; throw 'Forbidden real-time task start' }
function Stop-ScheduledTask { Record-TaskCall 'forbidden-stop' @{}; throw 'Forbidden running task stop' }
function Stop-Process { Record-TaskCall 'forbidden-kill' @{}; throw 'Forbidden process kill' }
& $env:HUB_REVIEW_CONTROLLER -Action $Action -Profile $Profile -Replace:$Replace
exit $LASTEXITCODE
'''.lstrip(), encoding='utf-8')

    def run_controller(self, action='Plan', profile=None, replace=False, readiness=0, state='Ready', deny=''):
        fixture = self.fixture
        args = [setup_tests.POWERSHELL, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                str(self.shim), '-Action', action, '-Profile', str(profile or fixture.profile)]
        if replace:
            args.append('-Replace')
        env = dict(fixture.env, HUB_REVIEW_CONTROLLER=str(fixture.checkout / 'scripts/windows/ConfigureStartup.ps1'),
                   HUB_REVIEW_TASK_CALLS=str(self.task_calls), HUB_REVIEW_EXISTING=str(self.existing_xml),
                   HUB_TEST_EXIT=str(readiness), HUB_REVIEW_TASK_STATE=state, HUB_REVIEW_DENY=deny)
        return subprocess.run(args, cwd=fixture.root, env=env, capture_output=True, encoding='utf-8', timeout=40)

    def calls(self):
        if not self.task_calls.exists():
            return []
        return [json.loads(line) for line in self.task_calls.read_text(encoding='utf-8-sig').splitlines()]

    def plan(self):
        result = self.run_controller()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_plan_is_pure_and_xml_keeps_literal_hostile_path(self):
        before = self.fixture.record.read_bytes()
        xml = self.plan()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.fixture.record.read_bytes(), before)
        root = ET.fromstring(xml)
        ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        argument = root.find('t:Actions/t:Exec/t:Arguments', ns).text
        self.assertIn('-File "', argument)
        self.assertIn('-Profile "' + str(self.fixture.profile) + '"', argument)
        self.assertNotIn('-Command', argument)
        self.assertEqual(root.find('t:Settings/t:AllowHardTerminate', ns).text, 'false')
        self.assertEqual(root.find('t:Settings/t:ExecutionTimeLimit', ns).text, 'PT0S')
        self.assertEqual(root.find('t:Settings/t:MultipleInstancesPolicy', ns).text, 'IgnoreNew')
        self.assertIsNone(root.find('t:Settings/t:RestartOnFailure', ns))

    def test_install_requires_successful_offline_readiness_before_register(self):
        result = self.run_controller('Install', readiness=23)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertEqual([call['name'] for call in self.calls()], ['get'])
        self.assertEqual(self.fixture.calls()[-1]['args'][1:3], ['robot_test_hub.launcher', 'check'])
        self.assertEqual(Path(self.fixture.calls()[-1]['cwd']), self.fixture.checkout)

    def test_new_install_registers_once_without_force_or_immediate_start(self):
        result = self.run_controller('Install')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call['name'] for call in self.calls()], ['get', 'register'])
        self.assertNotIn('Force', self.calls()[-1]['arguments'])

    def test_managed_replace_requires_explicit_switch_and_remove_does_not_check_profile(self):
        self.existing_xml.write_text(self.plan(), encoding='utf-8')
        before = self.fixture.record.read_bytes()
        result = self.run_controller('Install')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fixture.record.read_bytes(), before)
        self.assertNotIn('register', [call['name'] for call in self.calls()])
        result = self.run_controller('Install', replace=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.calls()[-1]['arguments']['Force'])
        self.fixture.profile.unlink()
        before = self.fixture.record.read_bytes()
        result = self.run_controller('Remove')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls()[-1]['name'], 'unregister')
        self.assertEqual(self.fixture.record.read_bytes(), before)

    def test_foreign_modified_or_dtd_task_never_unregisters_or_replaces(self):
        xml = self.plan()
        cases = [xml.replace('RobotTestHub.WindowsStartup/v1', 'Unrelated.Application/v1'),
                 xml.replace('<AllowHardTerminate>false</AllowHardTerminate>', '<AllowHardTerminate>true</AllowHardTerminate>'),
                 '<!DOCTYPE Task [<!ENTITY x SYSTEM "file:///C:/missing">]>' + xml]
        for value in cases:
            for action in ('Install', 'Remove'):
                with self.subTest(action=action, value=value[:70]):
                    self.existing_xml.write_text(value, encoding='utf-8')
                    if self.task_calls.exists():
                        self.task_calls.unlink()
                    before = self.fixture.record.read_bytes()
                    result = self.run_controller(action, replace=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual([call['name'] for call in self.calls()], ['get', 'export'])
                    self.assertEqual(self.fixture.record.read_bytes(), before)

    def test_percent_profile_refused_before_readiness_or_task_lookup(self):
        profile = self.fixture.root / '%LOCALAPPDATA%' / 'launch.json'
        profile.parent.mkdir()
        profile.write_text('{}', encoding='utf-8')
        before = self.fixture.record.read_bytes()
        result = self.run_controller('Install', profile=profile)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.fixture.record.read_bytes(), before)

    def test_junction_ancestor_profile_refused_before_task_lookup(self):
        alias = self.fixture.root / 'private junction'
        target = self.fixture.profile.parent
        command = 'mklink /J "' + str(alias) + '" "' + str(target) + '"'
        made = subprocess.run(chr(34) + os.environ.get('COMSPEC','cmd.exe') + chr(34) + ' /d /s /c ' + command, capture_output=True, timeout=10)
        self.assertEqual(made.returncode, 0, made.stderr.decode(errors='replace'))
        self.addCleanup(alias.rmdir)
        result = self.run_controller('Install', profile=alias / self.fixture.profile.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
    def test_generated_action_executes_actual_setup_entrypoint_with_literal_profile(self):
        root = ET.fromstring(self.plan())
        ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        command = root.find('t:Actions/t:Exec/t:Command', ns).text
        arguments = root.find('t:Actions/t:Exec/t:Arguments', ns).text
        # CreateProcess receives the scheduled action directly, without a shell.
        # The existing startup hook intercepts launcher execution before imports.
        result = subprocess.run('"' + command + '" ' + arguments,
            cwd=self.fixture.root, env=self.fixture.env, capture_output=True,
            encoding='utf-8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        call = self.fixture.calls()[-1]
        self.assertEqual(call['args'], ['-m', 'robot_test_hub.launcher', 'run', '--profile', str(self.fixture.profile)])
        self.assertEqual(Path(call['cwd']), self.fixture.checkout)
        self.assertTrue(call['bridge'])
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.fixture.root / 'SHOULD_NOT_EXIST').exists())
    def test_native_normalized_managed_definition_is_still_removable_via_mock(self):
        from tests import test_windows_startup_native as native_tests
        self.existing_xml.write_text(self.plan(), encoding='utf-8')
        validator = self.fixture.root / 'normalize-review.ps1'
        validator.write_text(native_tests.VALIDATE, encoding='utf-8')
        result = subprocess.run([setup_tests.POWERSHELL, '-NoLogo', '-NoProfile', '-ExecutionPolicy',
            'Bypass', '-File', str(validator), '-XmlFile', str(self.existing_xml)],
            cwd=self.fixture.root, capture_output=True, encoding='utf-8', timeout=20)
        if result.returncode == 43:
            self.skipTest(result.stdout.strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        normalized = json.loads(result.stdout)['normalized_xml']
        self.existing_xml.write_text(normalized, encoding='utf-8')
        result = self.run_controller('Remove')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([call['name'] for call in self.calls()], ['get', 'export', 'unregister'])
    def test_running_or_queued_task_blocks_remove_and_replace_without_mutation(self):
        self.existing_xml.write_text(self.plan(), encoding='utf-8')
        for state in ('Running', 'Queued'):
            for action in ('Remove', 'Install'):
                with self.subTest(state=state, action=action):
                    if self.task_calls.exists():
                        self.task_calls.unlink()
                    before = self.fixture.record.read_bytes()
                    result = self.run_controller(action, replace=True, state=state)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual([call['name'] for call in self.calls()], ['get', 'export'])
                    self.assertEqual(self.fixture.record.read_bytes(), before)
    def test_task_permission_denials_fail_visibly_without_success_or_followup_actions(self):
        managed = self.plan()
        cases = [
            ('get', 'Install', False, ['get'], False),
            ('register', 'Install', False, ['get', 'register'], True),
            ('unregister', 'Remove', True, ['get', 'export', 'unregister'], False),
        ]
        for deny, action, existing, expected_calls, checks_ready in cases:
            with self.subTest(deny=deny):
                if self.task_calls.exists():
                    self.task_calls.unlink()
                if existing:
                    self.existing_xml.write_text(managed, encoding='utf-8')
                elif self.existing_xml.exists():
                    self.existing_xml.unlink()
                before = self.fixture.record.read_bytes()
                result = self.run_controller(action, deny=deny)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Synthetic school policy', result.stderr)
                self.assertNotIn('Sign-in startup installed', result.stdout)
                self.assertNotIn('Sign-in startup registration removed', result.stdout)
                self.assertNotIn('already absent', result.stdout)
                self.assertEqual([call['name'] for call in self.calls()], expected_calls)
                if checks_ready:
                    self.assertNotEqual(self.fixture.record.read_bytes(), before)
                    self.assertEqual(self.fixture.calls()[-1]['args'][1:3], ['robot_test_hub.launcher', 'check'])
                else:
                    self.assertEqual(self.fixture.record.read_bytes(), before)

    def test_builtin_service_identities_refused_before_plan_or_scheduler_lookup(self):
        controller = self.fixture.checkout / 'scripts/windows/ConfigureStartup.ps1'
        original = controller.read_text(encoding='utf-8-sig')
        identity = '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'
        self.assertEqual(original.count(identity), 1)
        before = self.fixture.record.read_bytes()
        try:
            for sid in ('S-1-5-18', 'S-1-5-19', 'S-1-5-20'):
                # Substitute the read-only identity expression in the copied
                # controller only; the actual Windows identity never changes.
                controller.write_text(original.replace(identity, "'" + sid + "'"), encoding='utf-8')
                for action in ('Plan', 'Install', 'Status', 'Remove'):
                    with self.subTest(sid=sid, action=action):
                        result = self.run_controller(action)
                        self.assertNotEqual(result.returncode, 0)
                        guidance = ' '.join(result.stderr.lower().split())
                        self.assertIn('intended windows user account', guidance)
                        self.assertIn('built-in service accounts are unsupported', guidance)
                        self.assertNotIn('<Task', result.stdout)
                        self.assertEqual(self.calls(), [])
                        self.assertEqual(self.fixture.record.read_bytes(), before)
        finally:
            controller.write_text(original, encoding='utf-8')
