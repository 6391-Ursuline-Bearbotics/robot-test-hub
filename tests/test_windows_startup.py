"""Startup controller tests: scheduler cmdlets are intercepted; no OS task writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET

from tests import test_windows_setup


ROOT = Path(__file__).resolve().parents[1]
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@unittest.skipUnless(os.name == "nt" and test_windows_setup.POWERSHELL, "Requires Windows PowerShell")
class WindowsStartupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_windows_setup.WindowsSetupTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setup_environment()
        self.fixture.record.unlink()
        self.fixture.profile.parent.mkdir()
        self.fixture.profile.write_text("{}", encoding="utf-8")
        for name in ("ConfigureStartup.ps1", "StartupHub.cmd"):
            shutil.copy2(ROOT / "scripts/windows" / name, self.fixture.checkout / "scripts/windows" / name)
        self.script = self.fixture.checkout / "scripts/windows/ConfigureStartup.ps1"
        self.existing = self.fixture.root / "existing.xml"
        self.registered = self.fixture.root / "registered.xml"
        self.mutations = self.fixture.root / "scheduler-events.jsonl"
        self.harness = self.fixture.root / "startup-harness.ps1"
        self.harness.write_text(r"""
param([string]$Controller, [string]$Action, [string]$Profile, [switch]$Replace)
$ErrorActionPreference = 'Stop'
function Get-ScheduledTask {
    param($TaskPath, $ErrorAction)
    if (Test-Path -LiteralPath $env:HUB_STARTUP_EXISTING) {
        [pscustomobject]@{TaskName=('RobotTestHub-Startup-' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value); State=$(if ($env:HUB_STARTUP_STATE) { $env:HUB_STARTUP_STATE } else { 'Ready' })}
    }
}
function Export-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    [IO.File]::ReadAllText($env:HUB_STARTUP_EXISTING)
}
function Get-ScheduledTaskInfo {
    param($TaskName, $TaskPath, $ErrorAction)
    [pscustomobject]@{LastRunTime=[datetime]'2026-10-14T12:34:56'; LastTaskResult=2}
}
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $Xml, [switch]$Force, $ErrorAction)
    [IO.File]::WriteAllText($env:HUB_STARTUP_REGISTERED, $Xml)
    @{operation='register'; force=[bool]$Force; name=$TaskName; path=$TaskPath} | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:HUB_STARTUP_EVENTS
}
function Unregister-ScheduledTask {
    param($TaskName, $TaskPath, $Confirm, $ErrorAction)
    @{operation='remove'; name=$TaskName; path=$TaskPath} | ConvertTo-Json -Compress | Add-Content -LiteralPath $env:HUB_STARTUP_EVENTS
}
function Start-ScheduledTask { throw 'Forbidden: task start' }
function Stop-ScheduledTask { throw 'Forbidden: task stop' }
function Stop-Process { throw 'Forbidden: process kill' }
function Start-Process { throw 'Forbidden: process start' }
$argsToUse = @{Action=$Action; Profile=$Profile; Replace=$Replace}
& $Controller @argsToUse
exit $LASTEXITCODE
""", encoding="utf-8")
        self.env = dict(self.fixture.env, HUB_STARTUP_EXISTING=str(self.existing),
                        HUB_STARTUP_REGISTERED=str(self.registered), HUB_STARTUP_EVENTS=str(self.mutations))

    def call(self, action="Plan", *, profile=None, replace=False, **env):
        args = [test_windows_setup.POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(self.harness), "-Controller", str(self.script), "-Action", action,
                "-Profile", str(profile or self.fixture.profile)]
        if replace:
            args.append("-Replace")
        return subprocess.run(args, cwd=self.fixture.root, env=dict(self.env, **env),
                              capture_output=True, encoding="utf-8", timeout=40)

    def plan(self):
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_default_plan_has_safe_exact_task_policy_without_mutations(self):
        document = ET.fromstring(self.plan())
        self.assertEqual(document.findtext("t:Principals/t:Principal/t:LogonType", namespaces=NS), "InteractiveToken")
        self.assertEqual(document.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=NS), "LeastPrivilege")
        sid = document.findtext("t:Principals/t:Principal/t:UserId", namespaces=NS)
        self.assertEqual(document.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=NS), sid)
        self.assertEqual(document.findtext("t:Triggers/t:LogonTrigger/t:Delay", namespaces=NS), "PT30S")
        self.assertEqual(len(document.find("t:Triggers", NS)), 1)
        settings = document.find("t:Settings", NS)
        for name, value in {"MultipleInstancesPolicy": "IgnoreNew", "AllowHardTerminate": "false",
                            "DisallowStartIfOnBatteries": "false", "StopIfGoingOnBatteries": "false",
                            "ExecutionTimeLimit": "PT0S"}.items():
            self.assertEqual(settings.findtext("t:" + name, namespaces=NS), value)
        self.assertIsNone(settings.find("t:RestartOnFailure", NS))
        self.assertFalse(self.mutations.exists())
        self.assertFalse(self.fixture.record.exists())

    def test_install_runs_only_offline_check_then_registers_without_force(self):
        result = self.call("Install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.calls()[0]["args"], ["-m", "robot_test_hub.launcher", "check", "--profile", str(self.fixture.profile)])
        self.assertEqual(Path(self.fixture.calls()[0]["cwd"]), self.fixture.checkout)
        event = json.loads(self.mutations.read_text().strip())
        self.assertEqual(event["operation"], "register")
        self.assertFalse(event["force"])
        self.assertIn("nothing was started now", result.stdout)

    def test_failed_readiness_prevents_registration(self):
        result = self.call("Install", HUB_TEST_EXIT="7")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertFalse(self.mutations.exists())

    def test_managed_update_requires_replace_and_unrelated_task_is_preserved(self):
        xml = self.plan()
        self.existing.write_text(xml, encoding="utf-8")
        result = self.call("Install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("-Replace", result.stderr)
        self.assertFalse(self.mutations.exists())
        result = self.call("Install", replace=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(self.mutations.read_text().strip())["force"])
        self.mutations.unlink()
        self.existing.write_text(xml.replace("RobotTestHub.WindowsStartup/v1", "Some unrelated task"), encoding="utf-8")
        result = self.call("Install", replace=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.mutations.exists())

    def test_remove_needs_no_current_profile_and_does_not_stop_hub(self):
        self.existing.write_text(self.plan(), encoding="utf-8")
        self.fixture.profile.unlink()
        result = self.call("Remove")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.mutations.read_text().strip())["operation"], "remove")
        self.assertFalse(self.fixture.record.exists())
        self.assertIn("No stop or kill command", result.stdout)

    def test_running_or_queued_registration_cannot_be_removed_or_replaced(self):
        self.existing.write_text(self.plan(), encoding="utf-8")
        for state in ("Running", "Queued"):
            for action in ("Remove", "Install"):
                with self.subTest(state=state, action=action):
                    result = self.call(action, replace=True, HUB_STARTUP_STATE=state)
                    self.assertNotEqual(result.returncode, 0)
                    # Windows PowerShell formats errors to the host width.
                    # Match human guidance across line wrapping while retaining
                    # exact comparisons for machine argv and path fields.
                    guidance = " ".join(result.stderr.split())
                    self.assertIn("StopHub.cmd", guidance)
                    self.assertIn("wait for the task to exit", guidance)
                    self.assertFalse(self.mutations.exists())
                    self.assertFalse(self.fixture.record.exists())

    def test_status_exposes_last_failure_and_fixed_readiness_guidance(self):
        self.existing.write_text(self.plan(), encoding="utf-8")
        self.fixture.profile.unlink()
        result = self.call("Status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["state"], "Ready")
        self.assertEqual(value["last_task_result"], 2)
        self.assertIn("2026-10-14", value["last_run_time"])
        self.assertIn("CheckHub.cmd", value["guidance"])
        self.assertFalse(self.mutations.exists())

    def test_profile_must_exist_be_private_and_have_no_environment_expansion(self):
        for profile in (self.fixture.root / "missing.json", self.fixture.checkout / "private.json",
                        self.fixture.root / "%TEMP% literal.json", Path(r"\\localhost\never-connect\launch.json")):
            with self.subTest(profile=profile):
                result = self.call(profile=profile)
                self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.mutations.exists())
        self.assertFalse(self.fixture.record.exists())

    def test_xml_action_passes_unicode_and_shell_syntax_as_native_literal_arguments(self):
        profile = self.fixture.root / "caf\u00e9 & $(touch SHOULD_NOT_EXIST).json"
        profile.write_text("{}", encoding="utf-8")
        planned = self.call(profile=profile)
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
        document = ET.fromstring(planned.stdout)
        command = document.findtext("t:Actions/t:Exec/t:Command", namespaces=NS)
        arguments = document.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS)
        self.assertIn(str(profile), arguments)
        result = subprocess.run('"' + command + '" ' + arguments, cwd=self.fixture.root, env=self.env,
                                capture_output=True, encoding="utf-8", timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.calls()[-1]["args"], ["-m", "robot_test_hub.launcher", "run", "--profile", str(profile)])
        self.assertFalse((self.fixture.root / "SHOULD_NOT_EXIST").exists())

    def test_startup_cmd_default_is_preview(self):
        wrapper = self.fixture.checkout / "scripts/windows/StartupHub.cmd"
        command = '""' + str(wrapper) + '" -Profile "' + str(self.fixture.profile) + '""'
        result = subprocess.run('"' + os.environ.get("COMSPEC", "cmd.exe") + '" /d /s /c ' + command,
                                cwd=self.fixture.root, env=self.env, capture_output=True, encoding="utf-8", timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ET.fromstring(result.stdout)
        self.assertFalse(self.mutations.exists())
        self.assertFalse(self.fixture.record.exists())
