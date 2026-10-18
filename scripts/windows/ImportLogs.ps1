# Import WPILOG files from a folder (USB drive, Drive for desktop, downloads) into the hub.
# Stop the hub first: the import owns the data folder while it runs.
[CmdletBinding()]
param(
    [string]$Folder,
    [string]$Profile,
    [string]$LogProfile
)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
try {
    $checkout = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $venvPython = Join-Path $checkout '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw 'Run SetupHub.cmd first to create this installation''s Python environment.'
    }
    if ([string]::IsNullOrWhiteSpace($Folder)) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = 'Choose the folder or USB drive that holds the robot logs (.wpilog)'
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { throw 'No folder chosen.' }
        $Folder = $dialog.SelectedPath
    }
    if ([string]::IsNullOrWhiteSpace($Profile)) {
        $Profile = Join-Path $env:LOCALAPPDATA 'RobotTestHub\config\launch.json'
    }
    $arguments = @('-m', 'robot_test_hub.import_folder', $Folder, '--launch-profile', $Profile)
    if (-not [string]::IsNullOrWhiteSpace($LogProfile)) { $arguments += @('--profile', $LogProfile) }
    Push-Location -LiteralPath $checkout
    try {
        & $venvPython @arguments
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) { Write-Warning 'Some logs were not imported. See the lines above.' }
    exit $code
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit 1
}
