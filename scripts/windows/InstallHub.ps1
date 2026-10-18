# One-step install for a mentor laptop: Python check, setup, self-test, private folder,
# offline (import-only) profile and a readiness check. Safe to run again: existing
# configuration and profiles are kept. Nothing connects to a robot.
[CmdletBinding()]
param(
    [string]$DataRoot = 'C:\RobotHubPrivate',
    [string]$Profile,
    [switch]$SkipSelfTest
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'

function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }

try {
    $here = $PSScriptRoot
    $checkout = [System.IO.Path]::GetFullPath((Join-Path $here '..\..'))
    if ([string]::IsNullOrWhiteSpace($Profile)) {
        $Profile = Join-Path $env:LOCALAPPDATA 'RobotTestHub\config\launch.json'
    }
    $DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
    if ($DataRoot.StartsWith($checkout.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Choose a private folder outside the app folder for -DataRoot.'
    }

    Step '1/5 Python'
    $launcher = Get-Command 'py.exe' -CommandType Application -ErrorAction SilentlyContinue
    $ok = $false
    if ($null -ne $launcher) {
        & $launcher.Source -3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 3)' 2>$null
        $ok = ($LASTEXITCODE -eq 0)
    }
    if (-not $ok) {
        $winget = Get-Command 'winget.exe' -CommandType Application -ErrorAction SilentlyContinue
        if ($null -eq $winget) {
            throw 'Python 3.10+ with the py launcher is needed. Install it from python.org (tick "py launcher"), then run this again.'
        }
        $answer = Read-Host 'Python 3.10+ was not found. Install Python 3.13 for this user with winget now? (y/n)'
        if ($answer -notmatch '^[Yy]') { throw 'Install Python 3.10+ from python.org, then run this again.' }
        & $winget.Source install --exact --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw 'winget could not install Python. Install it from python.org, then run this again.' }
        Write-Host 'Python installed. If the next step cannot find it, close this window and run InstallHub.cmd again.'
    } else {
        Write-Host 'Python 3.10+ found.'
    }

    Step '2/5 Install the hub into .venv'
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'SetupHub.ps1') -Action Setup -Profile $Profile
    if ($LASTEXITCODE -ne 0) { throw 'Setup failed; see the messages above.' }
    $python = Join-Path $checkout '.venv\Scripts\python.exe'

    if (-not $SkipSelfTest) {
        Step '3/5 Self-test with built-in sample data'
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'SetupHub.ps1') -Action Test
        if ($LASTEXITCODE -ne 0) { throw 'The self-test failed; see the messages above.' }
    }

    Step '4/5 Private data folder and offline profile'
    New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
    $hubConfig = Join-Path $DataRoot 'hub.json'
    if (-not (Test-Path -LiteralPath $hubConfig)) {
        $json = @{ schema_version = 1; data_dir = (Join-Path $DataRoot 'data') } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($hubConfig, $json, (New-Object System.Text.UTF8Encoding $false))
        Write-Host "Created $hubConfig"
    } else {
        Write-Host "Kept existing $hubConfig"
    }
    if (-not (Test-Path -LiteralPath $Profile)) {
        Push-Location -LiteralPath $checkout
        try {
            & $python -m robot_test_hub.launcher configure --profile $Profile --mode offline --hub-config $hubConfig
            if ($LASTEXITCODE -ne 0) { throw 'Could not save the launch profile.' }
        } finally { Pop-Location }
    } else {
        Write-Host "Kept existing profile $Profile"
    }

    Step '5/5 Readiness check'
    Push-Location -LiteralPath $checkout
    try {
        & $python -m robot_test_hub.launcher check --profile $Profile
        if ($LASTEXITCODE -ne 0) { throw 'The readiness check needs attention; see the messages above.' }
    } finally { Pop-Location }

    Write-Host ''
    Write-Host 'Installed. Start the hub with scripts\windows\StartHub.cmd, then open http://127.0.0.1:6391' -ForegroundColor Green
    Write-Host 'Next: docs\FIRST_PRACTICE.md steps 7-10 (add students, import logs, read results).'
    exit 0
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
