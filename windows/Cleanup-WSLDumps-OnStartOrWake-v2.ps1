<#
.SYNOPSIS
  Conservative Windows housekeeping, with optional logon and wake scheduling.
.DESCRIPTION
  Run previews by default. Use -Mode Run -Apply to delete eligible files.
  -Mode Install installs a per-user task two minutes after logon or wake.
  WSL caches and worktrees are managed separately by the Linux housekeeping script.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('Install', 'Run', 'Status', 'Uninstall')]
    [string]$Mode = 'Run',
    [ValidateRange(1440, 525600)]
    [int]$OlderThanMinutes = 10080,
    [ValidateRange(1, 20)]
    [int]$KeepNewest = 3,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$TaskName = 'Cleanup WSL codebase-memory dumps on logon or wake'
$OldTaskName = 'Cleanup WSL codebase-memory crash dumps'
$InstallDirectory = Join-Path $env:LOCALAPPDATA 'WSLDumpCleanup'
$InstalledScript = Join-Path $InstallDirectory 'Cleanup-WSLDumps-OnStartOrWake-v2.ps1'
$LogFile = Join-Path $InstallDirectory 'cleanup.log'
$TempDirectory = Join-Path $env:LOCALAPPDATA 'Temp'

function Test-SafePath {
    param([string]$Path)
    # Reject a link/junction anywhere along the path, including the configured root.
    $current = [System.IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return $false }
        }
        $parent = [System.IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $current = $parent.FullName
    }
    return $true
}

function Write-Log {
    param([string]$Message)
    if (-not (Test-SafePath $InstallDirectory)) { throw 'Log directory contains a link/junction.' }
    New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
    if (-not (Test-SafePath $LogFile) -or -not (Test-SafePath "$LogFile.1")) {
        throw 'Log file contains a link/junction.'
    }
    if ((Test-Path -LiteralPath $LogFile) -and (Get-Item -LiteralPath $LogFile).Length -gt 1MB) {
        Move-Item -LiteralPath $LogFile -Destination "$LogFile.1" -Force
    }
    $line = '{0:o} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    Write-Host $Message
}

function Get-CleanupFiles {
    param([string]$Root, [string[]]$Patterns, [switch]$Recurse)
    if (-not (Test-Path -LiteralPath $Root)) { return }
    if (-not (Test-SafePath $Root)) { Write-Log "KEEP linked path: $Root"; return }
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        if (-not (Test-SafePath $directory)) { continue }
        try { $items = @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) }
        catch { Write-Log "KEEP unreadable directory: $directory"; continue }
        foreach ($item in $items) {
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
            if ($item.PSIsContainer) {
                if ($Recurse) { $pending.Push($item.FullName) }
            } else {
                # VM disks and swap files are never housekeeping targets.
                if ($item.Extension -match '^\.(vhdx?|avhdx?)$' -or $item.Name -like '*swap*') { continue }
                foreach ($pattern in $Patterns) {
                    if ($item.Name -like $pattern) { $item; break }
                }
            }
        }
    }
}

function Remove-CleanupFile {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([System.IO.FileInfo]$File, [datetime]$Cutoff, [switch]$Apply)
    $path = $File.FullName
    if (-not (Test-SafePath $path)) { return }
    $fresh = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if ($fresh.LastWriteTime -ge $Cutoff -or $fresh.CreationTime -ge $Cutoff) { return }
    if (-not $Apply) { Write-Log "PREVIEW eligible file: $path"; return }
    if (-not $PSCmdlet.ShouldProcess($path, 'Delete stale housekeeping file')) { return }
    $handle = $null
    try {
        # Deny concurrent read/write access. An open file causes this to fail safely.
        # Share Delete lets our own deletion complete while the handle stays open.
        $handle = [System.IO.File]::Open($path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::Delete)
        if ([System.IO.File]::GetLastWriteTime($path) -ge $Cutoff -or
            [System.IO.File]::GetCreationTime($path) -ge $Cutoff) { return }
        $size = $handle.Length
        [System.IO.File]::Delete($path)
        $script:RemovedBytes += $size
        $script:RemovedFiles++
        Write-Log "DELETED: $path ($size bytes)"
    } catch { Write-Log "KEEP locked or inaccessible file: $path ($($_.Exception.Message))" }
    finally { if ($null -ne $handle) { $handle.Dispose() } }
}

function Invoke-FileGroup {
    param([string]$Root, [string[]]$Patterns, [datetime]$Cutoff,
        [switch]$Recurse, [switch]$KeepDiagnostics, [switch]$Apply)
    $files = @(Get-CleanupFiles -Root $Root -Patterns $Patterns -Recurse:$Recurse |
        Sort-Object LastWriteTime -Descending)
    if ($KeepDiagnostics) { $files = @($files | Select-Object -Skip $KeepNewest) }
    foreach ($file in $files) {
        try { Remove-CleanupFile -File $file -Cutoff $Cutoff -Apply:$Apply }
        catch { Write-Log "KEEP changed or inaccessible file: $($file.FullName)" }
    }
}

function Invoke-Cleanup {
    # Prevent overlaps between scheduled and manually launched copies.
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutex = New-Object System.Threading.Mutex($false, "Local\WSLDumpCleanup-$sid")
    $acquired = $false
    try {
        try { $acquired = $mutex.WaitOne(0) }
        catch [System.Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { Write-Host 'Another housekeeping run is active; skipping.'; return }
        $script:RemovedBytes = [long]0
        $script:RemovedFiles = 0
        $cutoff = (Get-Date).AddMinutes(-$OlderThanMinutes)
        # Process inspection failure stops cleanup, rather than assuming apps are closed.
        $processNames = @(Get-Process -ErrorAction Stop | Select-Object -ExpandProperty ProcessName)
        Write-Log "=== Windows housekeeping: Apply=$Apply; cutoff=$($cutoff.ToString('o')) ==="
        Invoke-FileGroup -Root (Join-Path $TempDirectory 'wsl-crashes') -Patterns 'wsl-crash-*.dmp' -Cutoff $cutoff -Recurse -KeepDiagnostics -Apply:$Apply
        Invoke-FileGroup -Root $TempDirectory -Patterns 'wsl-crash-*.dmp' -Cutoff $cutoff -KeepDiagnostics -Apply:$Apply
        Invoke-FileGroup -Root (Join-Path $env:LOCALAPPDATA 'CrashDumps') -Patterns '*.dmp' -Cutoff $cutoff -KeepDiagnostics -Apply:$Apply
        Invoke-FileGroup -Root (Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data\Crashpad\reports') -Patterns '*.dmp' -Cutoff $cutoff -KeepDiagnostics -Apply:$Apply
        Invoke-FileGroup -Root (Join-Path $TempDirectory 'DiagOutputDir') -Patterns '*.etl' -Cutoff $cutoff -Recurse -Apply:$Apply
        # Only loose, old files of known temporary types; never recursively remove Temp directories.
        Invoke-FileGroup -Root $TempDirectory -Patterns '*.tmp', '*.temp', '*.log' -Cutoff $cutoff -Apply:$Apply

        $apps = @(
            @{ Name='VS Code staging'; Busy=@('Code', 'Code - Insiders', 'VSCodium'); Root=(Join-Path $env:USERPROFILE 'vscode-remote-wsl'); Sub=@(''); Patterns=@('*.tar.gz', '*.tgz', '*.zip') },
            @{ Name='Zoom'; Busy=@('Zoom', 'ZoomInstaller', 'ZoomUpdate'); Root=(Join-Path $env:APPDATA 'Zoom'); Sub=@('tmp_bin', 'ZoomDownload'); Patterns=@('*.exe', '*.msi', '*.zip') },
            @{ Name='Stremio'; Busy=@('stremio', 'stremio-server', 'node'); Root=(Join-Path $env:APPDATA 'stremio'); Sub=@('Cache', 'GPUCache', 'Code Cache'); Patterns=@('*') },
            @{ Name='Notion'; Busy=@('Notion'); Root=(Join-Path $env:APPDATA 'Notion'); Sub=@('Cache', 'GPUCache', 'Code Cache', 'DawnCache'); Patterns=@('*') }
        )
        foreach ($app in $apps) {
            if (@($processNames | Where-Object { $app.Busy -contains $_ }).Count -gt 0) {
                Write-Log "KEEP $($app.Name) caches: app is running"; continue
            }
            foreach ($sub in $app.Sub) {
                $root = if ($sub) { Join-Path $app.Root $sub } else { $app.Root }
                Invoke-FileGroup -Root $root -Patterns $app.Patterns -Cutoff $cutoff -Recurse -Apply:$Apply
            }
        }
        Write-Log ("=== Finished: deleted {0} file(s), {1:N2} MiB. ===" -f $script:RemovedFiles, ($script:RemovedBytes / 1MB))
    } finally {
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function ConvertTo-XmlSafeText {
    param([Parameter(Mandatory)][string]$Text)
    return [System.Security.SecurityElement]::Escape($Text)
}

function Install-CleanupTask {
    if (-not $PSCommandPath) {
        throw "Run this script from a saved .ps1 file so it can install itself."
    }

    if (-not $PSCmdlet.ShouldProcess($TaskName, 'Install/update Windows housekeeping task')) { return }
    if (-not (Test-SafePath $InstallDirectory) -or -not (Test-SafePath $InstalledScript)) {
        throw 'Installation path contains a link/junction.'
    }
    New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        $taskBackup = Join-Path $InstallDirectory ("task.bak-$(Get-Date -Format 'yyyyMMdd-HHmmssfff').xml")
        Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $taskBackup -Encoding Unicode
    }

    if ($PSCommandPath -and ($PSCommandPath -ne $InstalledScript)) {
        if (Test-Path -LiteralPath $InstalledScript) {
            $backup = "$InstalledScript.bak-$(Get-Date -Format 'yyyyMMdd-HHmmssfff')"
            Copy-Item -LiteralPath $InstalledScript -Destination $backup -ErrorAction Stop
        }
        Copy-Item -LiteralPath $PSCommandPath -Destination $InstalledScript -Force
    }

    foreach ($name in @($OldTaskName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
        }
    }

    $windowsIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $userSid = $windowsIdentity.User.Value
    $userName = $windowsIdentity.Name

    $powerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $arguments = (
        '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass ' +
        '-File "{0}" -Mode Run -Apply -OlderThanMinutes {1} -KeepNewest {2}' -f `
        $InstalledScript, $OlderThanMinutes, $KeepNewest
    )

    $xmlPowerShellPath = ConvertTo-XmlSafeText $powerShellPath
    $xmlArguments = ConvertTo-XmlSafeText $arguments
    $xmlUserSid = ConvertTo-XmlSafeText $userSid
    $xmlUserName = ConvertTo-XmlSafeText $userName
    $xmlAuthor = ConvertTo-XmlSafeText $userName

    $eventSubscription = @"
&lt;QueryList&gt;
  &lt;Query Id="0" Path="System"&gt;
    &lt;Select Path="System"&gt;
      *[System[
        (Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1)
        or
        (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=107)
      ]]
    &lt;/Select&gt;
  &lt;/Query&gt;
&lt;/QueryList&gt;
"@

    $taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$xmlAuthor</Author>
    <Description>Conservative Windows-only housekeeping of stale files on logon or wake; preserves VM disks, recent crash evidence, and app data.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT2M</Delay>
      <UserId>$xmlUserName</UserId>
    </LogonTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Delay>PT2M</Delay>
      <Subscription>$eventSubscription</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$xmlUserSid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$xmlPowerShellPath</Command>
      <Arguments>$xmlArguments</Arguments>
    </Exec>
  </Actions>
</Task>
"@

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Xml $taskXml `
        -Force | Out-Null

    Write-Log "Installed/updated scheduled cleanup task with logon and wake triggers."
}

function Show-Status {
    Write-Host ""
    Write-Host "=== Windows Housekeeping Status ===" -ForegroundColor Cyan
    Write-Host "Installed script  : $InstalledScript"
    Write-Host "Log file          : $LogFile"

    Write-Host ""
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "Scheduled task    : Installed" -ForegroundColor Green
        Write-Host "Task state        : $($task.State)"
        Write-Host "Last run          : $($info.LastRunTime)"
        Write-Host "Next run          : Event-triggered (Logon / Wake - 2 min delay)"
        Write-Host "Last result       : $($info.LastTaskResult)"
    } else {
        Write-Host "Scheduled task    : Not installed" -ForegroundColor Red
    }
}

function Uninstall-CleanupTask {
    if (-not $PSCmdlet.ShouldProcess($TaskName, 'Remove task and installed files')) { return }
    if (-not (Test-SafePath $InstallDirectory)) { throw 'Installation path contains a link/junction.' }
    foreach ($name in @($TaskName, $OldTaskName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed scheduled task: $name"
        }
    }

    if (Test-Path -LiteralPath $InstallDirectory) {
        try {
            Remove-Item -LiteralPath $InstallDirectory -Recurse -Force
            Write-Host "Removed: $InstallDirectory"
        } catch {
            Write-Warning "Could not remove '$InstallDirectory'."
        }
    }
}

switch ($Mode) {
    "Install"   { Install-CleanupTask }
    "Run"       { Invoke-Cleanup }
    "Status"    { Show-Status }
    "Uninstall" { Uninstall-CleanupTask }
}
