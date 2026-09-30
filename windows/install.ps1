[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateRange(1440, 525600)][int]$OlderThanMinutes = 10080,
    [ValidateRange(1, 20)][int]$KeepNewest = 3
)
$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot 'Cleanup-WSLDumps-OnStartOrWake-v2.ps1'
& $path -Mode Install @PSBoundParameters
