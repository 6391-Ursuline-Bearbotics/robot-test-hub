"""Native task-definition validation only; never register or start a task."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
VALIDATE = r"""
param([string]$XmlFile)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $definition = $scheduler.NewTask(0)
} catch {
    Write-Output 'Native Task Scheduler definition unavailable under this account.'
    exit 43
}
try {
    $definition.XmlText = [System.IO.File]::ReadAllText($XmlFile)
    @{
        logon_type = $definition.Principal.LogonType
        run_level = $definition.Principal.RunLevel
        trigger_count = $definition.Triggers.Count
        trigger_type = $definition.Triggers.Item(1).Type
        delay = $definition.Triggers.Item(1).Delay
        action_count = $definition.Actions.Count
        duplicates = $definition.Settings.MultipleInstances
        hard_terminate = $definition.Settings.AllowHardTerminate
        stop_on_battery = $definition.Settings.StopIfGoingOnBatteries
        disallow_on_battery = $definition.Settings.DisallowStartIfOnBatteries
        time_limit = $definition.Settings.ExecutionTimeLimit
        normalized_xml = $definition.XmlText
    } | ConvertTo-Json -Compress
} finally {
    [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($definition)
    [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($scheduler)
}
"""


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Native Windows Task Scheduler definition qualification")
class NativeStartupDefinitionTest(unittest.TestCase):
    def test_plan_loads_in_native_task_definition_without_registration(self):
        with tempfile.TemporaryDirectory(prefix="hub native startup ") as directory:
            root = Path(directory).resolve()
            source = root / "source & caf\u00e9"
            scripts = source / "scripts" / "windows"
            scripts.mkdir(parents=True)
            for name in ("ConfigureStartup.ps1", "SetupHub.ps1"):
                shutil.copy2(ROOT / "scripts" / "windows" / name, scripts / name)
            (source / "pyproject.toml").write_bytes(b"# synthetic Plan prerequisite\n")
            interpreter = source / ".venv" / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            # Plan checks prerequisite placement without executing this file.
            interpreter.write_bytes(b"synthetic prerequisite; not executed")
            profile = root / "private caf\u00e9 & $(literal)" / "launch.json"
            profile.parent.mkdir()
            profile.write_bytes(b"{}")
            plan = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy",
                "Bypass", "-File", str(scripts / "ConfigureStartup.ps1"), "-Profile", str(profile)],
                cwd=root, capture_output=True, encoding="utf-8", timeout=20)
            self.assertEqual(plan.returncode, 0, plan.stderr)
            import xml.etree.ElementTree as ET
            parsed = ET.fromstring(plan.stdout)
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            self.assertEqual(parsed.find("t:Actions/t:Exec/t:WorkingDirectory", ns).text, str(source))
            self.assertIn(str(profile), parsed.find("t:Actions/t:Exec/t:Arguments", ns).text)
            xml_file = root / "task.xml"
            xml_file.write_text(plan.stdout.strip(), encoding="utf-8")
            validator = root / "validate.ps1"
            validator.write_bytes(VALIDATE.encode("utf-8"))
            result = subprocess.run([POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy",
                "Bypass", "-File", str(validator), "-XmlFile", str(xml_file)],
                cwd=root, capture_output=True, encoding="utf-8", timeout=20)
            if result.returncode == 43:
                self.skipTest(result.stdout.strip())
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            self.assertEqual(actual["logon_type"], 3)  # InteractiveToken
            self.assertEqual(actual["run_level"], 0)  # LeastPrivilege
            self.assertEqual(actual["trigger_count"], 1)
            self.assertEqual(actual["trigger_type"], 9)  # Logon
            self.assertEqual(actual["delay"], "PT30S")
            self.assertEqual(actual["action_count"], 1)
            self.assertEqual(actual["duplicates"], 2)  # IgnoreNew
            self.assertFalse(actual["hard_terminate"])
            self.assertFalse(actual["stop_on_battery"])
            self.assertFalse(actual["disallow_on_battery"])
            self.assertEqual(actual["time_limit"], "PT0S")
            normalized = ET.fromstring(actual["normalized_xml"])
            self.assertEqual(normalized.find("t:Actions/t:Exec/t:WorkingDirectory", ns).text, str(source))
            self.assertIn(str(profile), normalized.find("t:Actions/t:Exec/t:Arguments", ns).text)
