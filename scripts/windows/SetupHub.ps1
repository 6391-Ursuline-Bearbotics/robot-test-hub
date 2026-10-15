# Native Windows setup and profile-based launch. No scheduled task is installed.
[CmdletBinding()]
param(
    [ValidateSet('Setup', 'Run', 'Check', 'Stop', 'Open')]
    [string]$Action = 'Setup',
    [string]$PythonPath,
    [string]$Profile
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$script:hubExitCode = 1

function Invoke-HubPython {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        $script:hubExitCode = $LASTEXITCODE
        throw "Python command failed (exit $LASTEXITCODE). Review the preceding output."
    }
}

try {
    $checkout = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    if (-not (Test-Path -LiteralPath (Join-Path $checkout 'pyproject.toml') -PathType Leaf)) {
        throw 'Extract the complete source release before running this script.'
    }
    if ([string]::IsNullOrWhiteSpace($Profile)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw 'LOCALAPPDATA is unavailable; supply -Profile with a private path outside the checkout.'
        }
        $Profile = Join-Path $env:LOCALAPPDATA 'RobotTestHub\config\launch.json'
    }
    $Profile = [System.IO.Path]::GetFullPath($Profile)
    $checkoutPrefix = $checkout.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if ($Profile.Equals($checkout, [System.StringComparison]::OrdinalIgnoreCase) -or
        $Profile.StartsWith($checkoutPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'The private launch profile must be outside the source checkout.'
    }
    $venvPython = Join-Path $checkout '.venv\Scripts\python.exe'
    $versionCheck = 'import sys; ok = sys.version_info >= (3, 10); print(''Python 3.10 or newer is required.'', file=sys.stderr) if not ok else None; sys.exit(0 if ok else 3)'
    if ($Action -eq 'Setup') {
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
                $python = [System.IO.Path]::GetFullPath($PythonPath)
                if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
                    throw 'PythonPath must identify an installed Python executable (Python 3.10 or newer).'
                }
                Invoke-HubPython -Executable $python -Arguments @('-c', $versionCheck)
                Invoke-HubPython -Executable $python -Arguments @('-m', 'venv', (Join-Path $checkout '.venv'))
            } else {
                $launcher = Get-Command 'py.exe' -CommandType Application -ErrorAction SilentlyContinue
                if ($null -eq $launcher) {
                    throw 'Install Python 3.10 or newer with the py launcher, or supply -PythonPath.'
                }
                Invoke-HubPython -Executable $launcher.Source -Arguments @('-3', '-c', $versionCheck)
                Invoke-HubPython -Executable $launcher.Source -Arguments @('-3', '-m', 'venv', (Join-Path $checkout '.venv'))
            }
        }
        Invoke-HubPython -Executable $venvPython -Arguments @('-c', $versionCheck)
        Push-Location -LiteralPath $checkout
        try {
            Invoke-HubPython -Executable $venvPython -Arguments @('-m', 'pip', 'install', '.[sftp,analytics]')
        } finally {
            Pop-Location
        }
        Write-Host 'Setup complete. No hub, robot connection, or automatic startup was started.'
        Write-Host "Private profile location: $Profile"
        Write-Host 'Create the profile once with launcher configure; see docs/WINDOWS_SETUP.md. Then use CheckHub.cmd and StartHub.cmd.'
    } else {
        if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
            throw 'Run SetupHub.cmd first to create this installation''s Python environment.'
        }
        # Live support tooling belongs to the complete source release, not the wheel.
        Push-Location -LiteralPath $checkout
        try {
            Invoke-HubPython -Executable $venvPython -Arguments @('-m', 'robot_test_hub.launcher', $Action.ToLowerInvariant(), '--profile', $Profile)
        } finally {
            Pop-Location
        }
    }
    exit 0
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit $script:hubExitCode
}
