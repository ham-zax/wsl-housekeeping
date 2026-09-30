[CmdletBinding(SupportsShouldProcess = $true)]
param()
$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot 'Cleanup-WSLDumps-OnStartOrWake-v2.ps1'
& $path -Mode Uninstall @PSBoundParameters
