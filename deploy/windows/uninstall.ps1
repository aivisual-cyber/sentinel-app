<#
.SYNOPSIS
  Remove Sentinel services, program files and shortcuts. Configuration, alerts and evidence in the
  data directory are KEPT unless -RemoveData is given.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "$env:ProgramFiles\Sentinel",
    [string]$DataDir = "$env:ProgramData\Sentinel",
    [switch]$RemoveData
)
$ErrorActionPreference = 'Continue'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Write-Host "Run as Administrator." -ForegroundColor Red; exit 1 }

foreach ($svc in @('SentinelWatchdog', 'SentinelEngine')) {
    $exe = Join-Path $InstallDir "service\$svc.exe"
    if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
        Stop-Service $svc -Force -ErrorAction SilentlyContinue
        if (Test-Path $exe) { & $exe uninstall | Out-Null } else { & sc.exe delete $svc | Out-Null }
        Write-Host "Removed service $svc"
    }
}
Get-NetFirewallRule -DisplayName 'Sentinel UI/API' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
foreach ($lnk in @("$env:PUBLIC\Desktop\Sentinel Fire Monitor.lnk",
                   "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Sentinel Fire Monitor.lnk",
                   (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Sentinel Fire Monitor.lnk'))) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
}
# uninstall.ps1 itself lives in InstallDir: remove the folder after this script exits
Start-Process -FilePath cmd.exe -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$InstallDir`"" -WindowStyle Hidden
if ($RemoveData) {
    Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
    Write-Host "Removed data directory $DataDir"
} else {
    Write-Host "Kept configuration, alerts and evidence in $DataDir (use -RemoveData to delete)."
}
Write-Host "Sentinel removed."
