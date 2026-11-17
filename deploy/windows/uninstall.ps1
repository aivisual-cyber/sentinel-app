<#
.SYNOPSIS
  Remove Sentinel services, program files and shortcuts. Configuration, alerts and evidence in the
  data directory are KEPT unless -RemoveData is given.

  Run the copy inside the install folder; it finds the install and data folders by itself:
      powershell -ExecutionPolicy Bypass -File "<InstallDir>\uninstall.ps1" [-RemoveData]
  Only files Sentinel installed are deleted; other files in those folders are left alone.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [string]$DataDir = '',
    [switch]$RemoveData
)
$ErrorActionPreference = 'Continue'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Write-Host "Run as Administrator." -ForegroundColor Red; exit 1 }

if (-not $InstallDir) {
    if (Test-Path (Join-Path $PSScriptRoot 'sentinel-install.json')) { $InstallDir = $PSScriptRoot }
    else { $InstallDir = "$env:ProgramFiles\Sentinel" }
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$info = $null
$marker = Join-Path $InstallDir 'sentinel-install.json'
if (Test-Path $marker) { try { $info = Get-Content $marker -Raw | ConvertFrom-Json } catch { } }
if (-not $DataDir) { if ($info -and $info.DataDir) { $DataDir = $info.DataDir } else { $DataDir = "$env:ProgramData\Sentinel" } }
$DataDir = [IO.Path]::GetFullPath($DataDir).TrimEnd('\')

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

# Program files: only what install.ps1 put there. The folder itself goes only if it is then empty.
# Runs after this script exits because uninstall.ps1 itself lives in InstallDir.
if ((Test-Path $marker) -or (Test-Path (Join-Path $InstallDir 'app\run_sentinel.py'))) {
    $cmds = @('timeout /t 2 >nul')
    foreach ($d in @('python', 'app', 'ui', 'tools', 'service')) { $cmds += "rmdir /s /q `"$InstallDir\$d`"" }
    foreach ($f in @('uninstall.ps1', 'README-WINDOWS.md', 'sentinel-install.json')) { $cmds += "del /q `"$InstallDir\$f`"" }
    $cmds += "rmdir `"$InstallDir`""
    Start-Process -FilePath cmd.exe -ArgumentList ('/c ' + ($cmds -join ' & ')) -WindowStyle Hidden
    Write-Host "Removed program files from $InstallDir"
} else {
    Write-Host "No Sentinel program files found in $InstallDir (nothing deleted there)."
}

if ($RemoveData) {
    $looksLikeData = (Test-Path (Join-Path $DataDir 'api_token')) -or (Test-Path (Join-Path $DataDir 'config\detection_config.yaml'))
    if ($DataDir -match '^[A-Za-z]:$' -or -not $looksLikeData) {
        Write-Host "Not deleting $DataDir: it does not look like a Sentinel data folder." -ForegroundColor Yellow
    } else {
        Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
        Write-Host "Removed data directory $DataDir"
    }
} else {
    Write-Host "Kept configuration, alerts and evidence in $DataDir (use -RemoveData to delete)."
}
Write-Host "Sentinel removed."
