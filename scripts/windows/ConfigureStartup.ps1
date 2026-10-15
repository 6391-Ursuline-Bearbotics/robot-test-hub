# Opt-in sign-in startup; preview by default. No task is ever started here.
[CmdletBinding()]
param(
    [ValidateSet('Plan', 'Install', 'Remove', 'Status')]
    [string]$Action = 'Plan',
    [string]$Profile,
    [switch]$Replace
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$script:startupExitCode = 1
$script:taskNamespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
$script:managedSource = 'RobotTestHub.WindowsStartup/v1'

function Add-HubElement {
    param($Parent, [string]$Name, [string]$Value)
    $element = $Parent.OwnerDocument.CreateElement($Name, $script:taskNamespace)
    if ($PSBoundParameters.ContainsKey('Value')) { $element.InnerText = $Value }
    [void]$Parent.AppendChild($element)
    return $element
}

function Get-HubAbsolutePath {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '[%"\x00-\x1f]') {
        throw 'Paths must be nonempty Windows file paths without percent signs, quotes or control characters (Task Scheduler expands environment variables).'
    }
    $absolute = [System.IO.Path]::GetFullPath($Value)
    if ($absolute.StartsWith('\\')) { throw 'Startup source and profile paths must be local drive paths; UNC paths are unsupported.' }
    return $absolute
}

function Assert-HubPrivatePath {
    param([string]$Path, [string]$SourceRoot)
    $prefix = $SourceRoot.TrimEnd('\', '/') + '\'
    if ($Path.Equals($SourceRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'The private launch profile must be outside the source checkout.'
    }
}

function Get-HubArguments {
    param([string]$SourceRoot, [string]$PrivateProfile)
    # Both interpolated values are validated file paths: no embedded quotes or
    # control characters, and no directory-ending backslash. -File is literal.
    $entry = Join-Path $SourceRoot 'scripts\windows\SetupHub.ps1'
    if ($entry.EndsWith('\') -or $PrivateProfile.EndsWith('\')) { throw 'Expected a file path.' }
    return '-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $entry + '" -Action Run -Profile "' + $PrivateProfile + '"'
}

function New-HubTaskXml {
    param([string]$SourceRoot, [string]$PrivateProfile, [string]$Sid, [string]$TaskName, [string]$PowerShell)
    $document = New-Object System.Xml.XmlDocument
    $root = $document.CreateElement('Task', $script:taskNamespace)
    $root.SetAttribute('version', '1.3')
    [void]$document.AppendChild($root)
    $registration = Add-HubElement $root 'RegistrationInfo'
    [void](Add-HubElement $registration 'Source' $script:managedSource)
    [void](Add-HubElement $registration 'Author' $Sid)
    [void](Add-HubElement $registration 'URI' ('\' + $TaskName))
    [void](Add-HubElement $registration 'Description' 'Opt-in current-user Robot Test Hub sign-in startup. Stop the hub separately with StopHub.cmd.')
    $metadata = @{version=1; sid=$Sid; source_root=$SourceRoot; profile=$PrivateProfile} | ConvertTo-Json -Compress
    [void](Add-HubElement $registration 'Documentation' $metadata)
    $triggers = Add-HubElement $root 'Triggers'
    $trigger = Add-HubElement $triggers 'LogonTrigger'
    [void](Add-HubElement $trigger 'Enabled' 'true')
    [void](Add-HubElement $trigger 'UserId' $Sid)
    [void](Add-HubElement $trigger 'Delay' 'PT30S')
    $principals = Add-HubElement $root 'Principals'
    $principal = Add-HubElement $principals 'Principal'
    $principal.SetAttribute('id', 'HubUser')
    [void](Add-HubElement $principal 'UserId' $Sid)
    [void](Add-HubElement $principal 'LogonType' 'InteractiveToken')
    [void](Add-HubElement $principal 'RunLevel' 'LeastPrivilege')
    $settings = Add-HubElement $root 'Settings'
    foreach ($pair in @(
        @('MultipleInstancesPolicy', 'IgnoreNew'), @('DisallowStartIfOnBatteries', 'false'),
        @('StopIfGoingOnBatteries', 'false'), @('AllowHardTerminate', 'false'),
        @('StartWhenAvailable', 'false'), @('RunOnlyIfNetworkAvailable', 'false'),
        @('Enabled', 'true'), @('Hidden', 'false'), @('ExecutionTimeLimit', 'PT0S'),
        @('WakeToRun', 'false'))) {
        [void](Add-HubElement $settings $pair[0] $pair[1])
    }
    $actions = Add-HubElement $root 'Actions'
    $actions.SetAttribute('Context', 'HubUser')
    $execution = Add-HubElement $actions 'Exec'
    [void](Add-HubElement $execution 'Command' $PowerShell)
    [void](Add-HubElement $execution 'Arguments' (Get-HubArguments $SourceRoot $PrivateProfile))
    [void](Add-HubElement $execution 'WorkingDirectory' $SourceRoot)
    return $document
}

function Assert-HubManagedTask {
    param([string]$Xml, [string]$Sid, [string]$TaskName, [string]$PowerShell)
    # DTDs and external entities are never loaded from a stored task definition.
    $readerSettings = New-Object System.Xml.XmlReaderSettings
    $readerSettings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $readerSettings.XmlResolver = $null
    $reader = [System.Xml.XmlReader]::Create((New-Object System.IO.StringReader($Xml)), $readerSettings)
    try {
        $document = New-Object System.Xml.XmlDocument
        $document.XmlResolver = $null
        $document.Load($reader)
    } finally { $reader.Dispose() }
    $ns = New-Object System.Xml.XmlNamespaceManager($document.NameTable)
    $ns.AddNamespace('t', $script:taskNamespace)
    $root = $document.SelectSingleNode('/t:Task', $ns)
    if ($null -eq $root) { throw 'Existing task is not a managed Robot Test Hub task.' }
    foreach ($pair in @(@('Source', $script:managedSource), @('Author', $Sid), @('URI', ('\' + $TaskName)))) {
        $node = $root.SelectNodes(('t:RegistrationInfo/t:' + $pair[0]), $ns)
        if ($node.Count -ne 1 -or $node[0].InnerText -cne $pair[1]) { throw 'Existing task identity is unrelated or modified; refusing to change it.' }
    }
    $metadataNodes = $root.SelectNodes('t:RegistrationInfo/t:Documentation', $ns)
    if ($metadataNodes.Count -ne 1) { throw 'Existing task provenance is unavailable.' }
    $metadata = $metadataNodes[0].InnerText | ConvertFrom-Json
    $keys = @($metadata.PSObject.Properties.Name | Sort-Object)
    if (($keys -join ',') -cne 'profile,sid,source_root,version' -or
        ($metadata.version -isnot [int] -and $metadata.version -isnot [long]) -or $metadata.version -ne 1 -or
        $metadata.sid -isnot [string] -or $metadata.sid -cne $Sid -or
        $metadata.source_root -isnot [string] -or $metadata.profile -isnot [string]) {
        throw 'Existing task provenance is invalid.'
    }
    $oldRoot = Get-HubAbsolutePath $metadata.source_root
    $oldProfile = Get-HubAbsolutePath $metadata.profile
    if ($oldRoot -cne $metadata.source_root -or $oldProfile -cne $metadata.profile) { throw 'Existing task paths are not absolute.' }
    Assert-HubPrivatePath $oldProfile $oldRoot
    $expected = New-HubTaskXml $oldRoot $oldProfile $Sid $TaskName $PowerShell
    foreach ($query in @('t:Principals/t:Principal', 't:Actions/t:Exec', 't:Triggers/t:LogonTrigger')) {
        $actualNodes = $root.SelectNodes($query, $ns)
        $expectedNodes = $expected.DocumentElement.SelectNodes($query, $ns)
        if ($actualNodes.Count -ne 1) { throw 'Existing task action, principal or trigger was modified.' }
        # Compare element names, values and attributes, independent of XML whitespace/order.
        $actualNode = $actualNodes[0]; $expectedNode = $expectedNodes[0]
        if ($actualNode.Attributes.Count -ne $expectedNode.Attributes.Count -or
            $actualNode.ChildNodes.Count -ne $expectedNode.ChildNodes.Count) { throw 'Existing task execution provenance was modified.' }
        foreach ($attribute in $expectedNode.Attributes) {
            if ($actualNode.GetAttribute($attribute.Name) -cne $attribute.Value) { throw 'Existing task execution identity was modified.' }
        }
        foreach ($child in $expectedNode.ChildNodes) {
            $matches = $actualNode.SelectNodes(('t:' + $child.LocalName), $ns)
            if ($matches.Count -ne 1 -or $matches[0].InnerText -cne $child.InnerText) { throw 'Existing task execution provenance was modified.' }
        }
    }
    foreach ($query in @('t:Principals/*', 't:Actions/*', 't:Triggers/*')) {
        if ($root.SelectNodes($query, $ns).Count -ne 1) { throw 'Existing task contains additional execution behavior.' }
    }
    if ($root.SelectSingleNode('t:Actions', $ns).GetAttribute('Context') -cne 'HubUser' -or
        $root.SelectNodes('t:Settings/t:RestartOnFailure', $ns).Count -ne 0) { throw 'Existing task execution policy was modified.' }
    foreach ($setting in $expected.DocumentElement.SelectSingleNode('t:Settings', $ns).ChildNodes) {
        $nodes = $root.SelectNodes(('t:Settings/t:' + $setting.LocalName), $ns)
        if ($nodes.Count -ne 1 -or $nodes[0].InnerText -cne $setting.InnerText) { throw 'Existing task safety settings were modified.' }
    }
}

try {
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($sid -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')) {
        throw 'Use the intended Windows user account to configure sign-in startup; built-in service accounts are unsupported.'
    }
    $taskName = 'RobotTestHub-Startup-' + $sid
    $powerShell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
    $checkout = Get-HubAbsolutePath (Join-Path $PSScriptRoot '..\..')
    if ($Action -in @('Plan', 'Install')) {
        foreach ($relative in @('pyproject.toml', '.venv\Scripts\python.exe', 'scripts\windows\SetupHub.ps1')) {
            if (-not (Test-Path -LiteralPath (Join-Path $checkout $relative) -PathType Leaf)) {
                throw 'Prepare the complete source release and its .venv with SetupHub.cmd first.'
            }
        }
        if ([string]::IsNullOrWhiteSpace($Profile)) {
            if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'Supply an explicit private -Profile path.' }
            $Profile = Join-Path $env:LOCALAPPDATA 'RobotTestHub\config\launch.json'
        }
        $Profile = Get-HubAbsolutePath $Profile
        Assert-HubPrivatePath $Profile $checkout
        if (-not (Test-Path -LiteralPath $Profile -PathType Leaf)) { throw 'Create the explicit private launch profile first; no endpoint or mode is inferred.' }
        # A private profile must not route back into source via a link/junction.
        $component = Get-Item -LiteralPath $Profile -Force
        while ($null -ne $component) {
            if (($component.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Private profile links and junctions are unsupported.' }
            if ($component -is [System.IO.FileInfo]) { $component = $component.Directory } else { $component = $component.Parent }
            if ($null -eq $component) { break }
        }
        $xml = New-HubTaskXml $checkout $Profile $sid $taskName $powerShell
        if ($Action -eq 'Plan') { Write-Output $xml.OuterXml; exit 0 }
    }
    $existing = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -ieq $taskName })
    if ($existing.Count -gt 1) { throw 'Multiple matching tasks found; no change was made.' }
    if ($existing.Count -eq 1) {
        $existingXml = Export-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
        Assert-HubManagedTask $existingXml $sid $taskName $powerShell
        if ($Action -in @('Install', 'Remove') -and ([string]$existing[0].State) -in @('Running', 'Queued')) {
            throw 'Managed startup is Running or Queued. Use StopHub.cmd, wait for the task to exit, then retry. No registration change or stop command was sent.'
        }
    }
    if ($Action -eq 'Status') {
        $status = @{task_name=$taskName; installed=($existing.Count -eq 1); state='Absent'; last_run_time=$null; last_task_result=$null;
                    guidance='Run CheckHub.cmd for local readiness. A stopped or failed hub requires manual StartHub.cmd or the next sign-in.'}
        if ($existing.Count -eq 1) {
            $information = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\' -ErrorAction Stop
            $status.state = [string]$existing[0].State
            $status.last_run_time = $information.LastRunTime.ToString('o')
            $status.last_task_result = [long]$information.LastTaskResult
        }
        $status | ConvertTo-Json -Compress
    } elseif ($Action -eq 'Remove') {
        if ($existing.Count -eq 1) {
            Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
        }
        Write-Output 'Sign-in startup registration removed or already absent. No stop or kill command was sent; use StopHub.cmd separately.'
    } else {
        if ($existing.Count -eq 1 -and -not $Replace) { throw 'Managed startup already exists. Use -Replace explicitly after reviewing Plan.' }
        Push-Location -LiteralPath $checkout
        try {
            $arguments = @('-m', 'robot_test_hub.launcher', 'check', '--profile', $Profile)
            & (Join-Path $checkout '.venv\Scripts\python.exe') @arguments
            if ($LASTEXITCODE -ne 0) { $script:startupExitCode = $LASTEXITCODE; throw 'Offline readiness failed; no startup task was installed.' }
        } finally { Pop-Location }
        $registrationArguments = @{TaskName=$taskName; TaskPath='\'; Xml=$xml.OuterXml; ErrorAction='Stop'}
        if ($existing.Count -eq 1) { $registrationArguments.Force = $true }
        Register-ScheduledTask @registrationArguments | Out-Null
        Write-Output 'Sign-in startup installed for the current user. It starts only at the next sign-in, after 30 seconds; nothing was started now.'
    }
    exit 0
} catch {
    Write-Error -Message $_.Exception.Message -ErrorAction Continue
    exit $script:startupExitCode
}
