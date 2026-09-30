# Fixture-only regression checks; no real user cache directories are used.
[CmdletBinding()]
param([string]$ScriptPath = (Join-Path $PSScriptRoot 'Cleanup-WSLDumps-OnStartOrWake-v2.ps1'))
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ScriptPath = (Resolve-Path -LiteralPath $ScriptPath).Path
$fixture = Join-Path ([System.IO.Path]::GetTempPath()) ('windows-housekeeping-test-' + [guid]::NewGuid())
$previous = @{}
foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'USERPROFILE')) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name)
}
$checks = 0
function Assert-That {
    param([bool]$Condition, [string]$Description)
    if (-not $Condition) { throw "FAIL: $Description" }
    $script:checks++
    Write-Host "PASS: $Description"
}
function New-FixtureFile {
    param([string]$Relative, [int]$Days = 10)
    $path = Join-Path $fixture $Relative
    New-Item -ItemType Directory -Path (Split-Path $path) -Force | Out-Null
    [System.IO.File]::WriteAllText($path, 'fixture contents')
    [System.IO.File]::SetCreationTime($path, (Get-Date).AddDays(-$Days))
    [System.IO.File]::SetLastWriteTime($path, (Get-Date).AddDays(-$Days))
    return $path
}
# Script-scope mock visible to the invoked script; never starts real applications.
function Get-Process {
    [CmdletBinding()]
    param()
    foreach ($name in $BusyNames) { [pscustomobject]@{ProcessName=$name} }
}
$BusyNames = @('Notion')
$lock = $null
try {
    $env:LOCALAPPDATA = Join-Path $fixture 'local'
    $env:APPDATA = Join-Path $fixture 'roaming'
    $env:USERPROFILE = Join-Path $fixture 'user'
    $old = New-FixtureFile 'local\Temp\old.tmp'
    $fresh = New-FixtureFile 'local\Temp\fresh.tmp' 0
    $newCopy = New-FixtureFile 'local\Temp\new-copy.tmp'
    [System.IO.File]::SetCreationTime($newCopy, (Get-Date))
    $unknown = New-FixtureFile 'local\Temp\document.txt'
    $disk = New-FixtureFile 'local\Temp\swap.vhdx'
    $nested = New-FixtureFile 'local\Temp\old-folder\old.tmp'
    [System.IO.Directory]::SetLastWriteTime((Split-Path $nested), (Get-Date).AddDays(-15))
    $recentNested = New-FixtureFile 'local\Temp\old-folder\fresh.tmp' 0
    $data = New-FixtureFile 'roaming\Notion\Partitions\persist\IndexedDB\data'
    $cache = New-FixtureFile 'roaming\Notion\Cache\old-cache'
    $archive = New-FixtureFile 'user\vscode-remote-wsl\old.tar.gz'
    $stagingData = New-FixtureFile 'user\vscode-remote-wsl\unpacked\user-data.json'
    $locked = New-FixtureFile 'local\Temp\locked.tmp'
    $lock = [System.IO.File]::Open($locked, 'Open', 'ReadWrite', 'None')
    $dumps = @()
    foreach ($days in @(8, 9, 10, 11, 12)) {
        $dumps += New-FixtureFile "local\CrashDumps\dump-$days.dmp" $days
    }
    & $ScriptPath -Mode Run
    Assert-That (Test-Path -LiteralPath $old) 'Default preview retains eligible files'
    & $ScriptPath -Mode Run -Apply -WhatIf
    Assert-That (Test-Path -LiteralPath $old) 'Apply plus WhatIf retains eligible files'
    & $ScriptPath -Mode Install -WhatIf
    Assert-That (-not (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA 'WSLDumpCleanup\Cleanup-WSLDumps-OnStartOrWake-v2.ps1'))) 'Install plus WhatIf does not install a script or register a task'
    & $ScriptPath -Mode Run -Apply
    Assert-That (-not (Test-Path -LiteralPath $old)) 'Apply deletes an old loose temp file'
    Assert-That (Test-Path -LiteralPath $fresh) 'Recent temp file survives'
    Assert-That (Test-Path -LiteralPath $newCopy) 'Recently created file with an old modification time survives'
    Assert-That (Test-Path -LiteralPath $unknown) 'Unknown loose file type survives'
    Assert-That (Test-Path -LiteralPath $disk) 'Swap/VM disk survives'
    Assert-That ((Test-Path -LiteralPath $nested) -and (Test-Path -LiteralPath $recentNested)) 'Temp directory and its contents survive regardless of parent age'
    Assert-That (Test-Path -LiteralPath $data) 'Notion partition data survives'
    Assert-That (Test-Path -LiteralPath $cache) 'Running app cache survives'
    Assert-That (Test-Path -LiteralPath $locked) 'Locked old temp file survives'
    Assert-That (-not (Test-Path -LiteralPath $archive)) 'Old VS Code download archive is deleted'
    Assert-That (Test-Path -LiteralPath $stagingData) 'Non-archive VS Code staging data survives'
    Assert-That (@($dumps | Where-Object { Test-Path -LiteralPath $_ }).Count -eq 3) 'Newest three old crash dumps are retained'
    Assert-That ((Test-Path -LiteralPath $dumps[0]) -and (Test-Path -LiteralPath $dumps[2])) 'Crash dump retention sorts by modification time'
    $BusyNames = @()
    & $ScriptPath -Mode Run -Apply
    Assert-That (-not (Test-Path -LiteralPath $cache)) 'Closed app stale cache is eligible'

    # Junctions are available without administrator/developer-mode privileges.
    $outside = New-FixtureFile 'outside\important.tmp'
    $junction = Join-Path $env:LOCALAPPDATA 'Temp\DiagOutputDir'
    New-Item -ItemType Junction -Path $junction -Target (Split-Path $outside) | Out-Null
    $outsideTrace = New-FixtureFile 'outside\important.etl'
    & $ScriptPath -Mode Run -Apply
    Assert-That (Test-Path -LiteralPath $outsideTrace) 'Linked cleanup root is skipped'
    [System.IO.Directory]::Delete($junction)
    $parserTokens = $null; $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$parserTokens, [ref]$parseErrors)
    Assert-That ($parseErrors.Count -eq 0) 'PowerShell parser accepts script'
    $wslCalls = @($ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -match '^wsl(\.exe)?$'
    }, $true))
    Assert-That ($wslCalls.Count -eq 0) 'Windows cleanup does not launch WSL'
    Write-Host "All $checks fixture checks passed."
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
    # Remove the fixture junction explicitly before any recursive fixture cleanup.
    $junction = Join-Path $fixture 'local\Temp\DiagOutputDir'
    if ([System.IO.Directory]::Exists($junction)) {
        $entry = Get-Item -LiteralPath $junction -Force
        if ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            [System.IO.Directory]::Delete($junction)
        }
    }
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name])
    }
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
