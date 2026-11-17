<#
.SYNOPSIS
  Sentinel supplementary fire detection - OFFLINE installer for Windows 10/11 and Windows Server 2016+.

.DESCRIPTION
  Run from the unpacked USB bundle as Administrator:
      powershell -ExecutionPolicy Bypass -File .\install.ps1
  Everything needed is inside the bundle (Python, packages, VC++ runtime DLLs, model, UI, service wrapper).
  Nothing is downloaded. GPU acceleration uses DirectML (any DirectX 12 GPU: NVIDIA Kepler/GTX 600 series
  through Blackwell/RTX 50, AMD, Intel) through the normal display driver - no CUDA install needed.

  Re-running upgrades the program and keeps configuration, cameras, alerts and the access key.

  SUPPLEMENTARY EARLY WARNING ONLY - never a replacement for certified fire alarm systems.

.PARAMETER InstallDir   Program files (default C:\Program Files\Sentinel)
.PARAMETER DataDir      Configuration, alerts, logs, models (default C:\ProgramData\Sentinel)
.PARAMETER Port         Local API/UI port (default 8765)
.PARAMETER ListenLan    Also listen on the network (token-protected) and open the Windows firewall port
.PARAMETER NoShortcut   Do not create desktop / Start menu shortcuts
.PARAMETER ShortcutCurrentUserOnly  Put the shortcut (which contains the access key) only on this admin's desktop
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "$env:ProgramFiles\Sentinel",
    [string]$DataDir = "$env:ProgramData\Sentinel",
    [int]$Port = 8765,
    [switch]$ListenLan,
    [switch]$NoShortcut,
    [switch]$ShortcutCurrentUserOnly
)

$ErrorActionPreference = 'Stop'
$Bundle = $PSScriptRoot
$LogFile = Join-Path $env:TEMP "sentinel-install.log"
Start-Transcript -Path $LogFile -Force | Out-Null

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Yellow }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    WARNING: $msg" -ForegroundColor Magenta }
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; Stop-Transcript | Out-Null; exit 1 }

Write-Host "Sentinel installer - SUPPLEMENTARY fire detection only. Never a replacement for certified fire alarms." -ForegroundColor Cyan

# ------------------------------------------------------------------ checks
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Fail "Run this script as Administrator." }
if (-not [Environment]::Is64BitOperatingSystem) { Fail "64-bit Windows is required." }
$os = Get-CimInstance Win32_OperatingSystem
$build = [int]$os.BuildNumber
Ok "$($os.Caption) (build $build)"
if ($build -lt 14393) { Fail "Windows 10 1607 / Windows Server 2016 (build 14393) or newer is required." }
foreach ($p in @('python\python.exe', 'app\run_sentinel.py', 'ui\index.html', 'models\fire_smoke.onnx', 'tools\WinSW-x64.exe')) {
    if (-not (Test-Path (Join-Path $Bundle $p))) { Fail "Bundle is incomplete: missing $p" }
}

# ------------------------------------------------------------------ install locations must be safe
function Full($p) { [IO.Path]::GetFullPath($p).TrimEnd('\') }
function Inside($child, $parent) { ($child + '\').StartsWith($parent + '\', [StringComparison]::OrdinalIgnoreCase) }
$InstallDir = Full $InstallDir; $DataDir = Full $DataDir; $BundleDir = Full $Bundle
foreach ($pair in @(@('InstallDir', $InstallDir), @('DataDir', $DataDir))) {
    if ($pair[1] -match '^[A-Za-z]:$') { Fail "$($pair[0]) cannot be a drive root ($($pair[1])\). Use a folder, e.g. $($pair[1])\Sentinel" }
}
if ((Inside $InstallDir $BundleDir) -or (Inside $BundleDir $InstallDir)) {
    Fail "InstallDir ($InstallDir) must be separate from the unpacked bundle ($BundleDir). Example: -InstallDir E:\Sentinel -DataDir E:\SentinelData"
}
if ((Inside $DataDir $InstallDir) -or (Inside $InstallDir $DataDir) -or (Inside $DataDir $BundleDir)) {
    Fail "DataDir ($DataDir) must be outside InstallDir and the bundle: upgrades and uninstall replace the program folder. Example: -InstallDir E:\Sentinel -DataDir E:\SentinelData"
}
$Marker = Join-Path $InstallDir 'sentinel-install.json'
$isSentinel = (Test-Path $Marker) -or (Test-Path (Join-Path $InstallDir 'app\run_sentinel.py'))
if ((Test-Path $InstallDir) -and -not $isSentinel -and (Get-ChildItem -Force $InstallDir | Select-Object -First 1)) {
    Fail "InstallDir $InstallDir already contains other files. Choose a new or empty folder, e.g. $InstallDir\Sentinel"
}
foreach ($d in @($InstallDir, $DataDir)) {
    try { $vol = Get-Volume -DriveLetter ((Split-Path -Qualifier $d).TrimEnd(':')) -ErrorAction Stop } catch { $vol = $null }
    if ($vol -and $vol.FileSystem -and $vol.FileSystem -notin @('NTFS', 'ReFS')) {
        Fail "$d is on a $($vol.FileSystem) drive. Use an NTFS drive (needed to protect the access key and camera passwords), e.g. C:"
    }
    if ($vol -and $vol.DriveType -eq 'Removable') { Warn "$d is on a removable drive: detection stops if it is unplugged. Prefer an internal disk." }
}

# ------------------------------------------------------------------ stop running services (upgrade)
foreach ($svc in @('SentinelWatchdog', 'SentinelEngine')) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s -and $s.Status -ne 'Stopped') { Say "Stopping $svc (upgrade)"; Stop-Service $svc -Force; Start-Sleep -Seconds 2 }
}

# ------------------------------------------------------------------ program files
Say "Installing program to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($d in @('python', 'app', 'ui', 'tools')) {
    $src = Join-Path $Bundle $d; $dst = Join-Path $InstallDir $d
    # /MIR keeps the program folder identical to the bundle (removes stale files from older versions)
    & robocopy $src $dst /MIR /NFL /NDL /NJH /NJS /NP /R:2 /W:2 | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Copying $d failed (robocopy exit $LASTEXITCODE)" }
}
Copy-Item (Join-Path $Bundle 'uninstall.ps1') $InstallDir -Force
Copy-Item (Join-Path $Bundle 'README-WINDOWS.md') $InstallDir -Force -ErrorAction SilentlyContinue
# Marker: identifies this folder as a Sentinel install and tells uninstall.ps1 where the data lives
$markerJson = @{ InstallDir = $InstallDir; DataDir = $DataDir; Port = $Port; InstalledAt = (Get-Date).ToString('s') } | ConvertTo-Json
[IO.File]::WriteAllText($Marker, $markerJson, (New-Object Text.UTF8Encoding($false)))
$Python = Join-Path $InstallDir 'python\python.exe'

# ------------------------------------------------------------------ data dir (SENTINEL_HOME)
Say "Preparing data directory $DataDir"
foreach ($d in @('config', 'data', 'logs', 'models')) { New-Item -ItemType Directory -Force -Path (Join-Path $DataDir $d) | Out-Null }
$model = Join-Path $DataDir 'models\fire_smoke.onnx'
if (-not (Test-Path $model)) { Copy-Item (Join-Path $Bundle 'models\fire_smoke.onnx') $model }
$detCfg = Join-Path $DataDir 'config\detection_config.yaml'
if (-not (Test-Path $detCfg)) {
    $tpl = Get-Content (Join-Path $Bundle 'app\config\detection_config.yaml.template') -Raw
    $tpl = $tpl -replace '(?m)^(\s+path:\s*)rabahdev_best\.pt', '${1}models/fire_smoke.onnx'
    [IO.File]::WriteAllText($detCfg, $tpl, (New-Object Text.UTF8Encoding($false)))
}
$notif = Join-Path $DataDir 'config\notifications.yaml'
if (-not (Test-Path $notif)) { Copy-Item (Join-Path $Bundle 'app\config\notifications.yaml.template') $notif }
$cams = Join-Path $DataDir 'config\cameras.yaml'
if (-not (Test-Path $cams)) { [IO.File]::WriteAllText($cams, "cameras: []`n", (New-Object Text.UTF8Encoding($false))) }

# ------------------------------------------------------------------ access key + environment file
$envFile = Join-Path $DataDir 'sentinel.env'
$tokenFile = Join-Path $DataDir 'api_token'
if (-not (Test-Path $tokenFile)) {
    Say "Generating access key"
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    [IO.File]::WriteAllText($tokenFile, $token, (New-Object Text.UTF8Encoding($false)))
}
$token = (Get-Content $tokenFile -Raw).Trim()
$bindHost = '127.0.0.1'
if ($ListenLan) { $bindHost = '0.0.0.0' }
$envLines = @(
    "# Sentinel service environment (protected: Administrators + SYSTEM only).",
    "# Add notification secrets here, then: Restart-Service SentinelEngine, SentinelWatchdog",
    "SENTINEL_API_TOKEN=$token",
    "SENTINEL_PORT=$Port",
    "SENTINEL_HOST=$bindHost"
)
if (Test-Path $envFile) {
    # keep user-added secrets, refresh the managed keys
    $keep = Get-Content $envFile | Where-Object { $_ -notmatch '^(SENTINEL_API_TOKEN|SENTINEL_PORT|SENTINEL_HOST)=' -and $_ -notmatch '^# Sentinel service environment|^# Add notification secrets' }
    $envLines = $envLines + $keep
} else {
    $envLines += @("#SENTINEL_SMTP_PASSWORD=", "#SENTINEL_WEBHOOK_URL=", "#SENTINEL_TWILIO_TOKEN=", "#SENTINEL_HEARTBEAT_URL=")
}
[IO.File]::WriteAllLines($envFile, $envLines, (New-Object Text.UTF8Encoding($false)))

# Secrets and configuration: Administrators + SYSTEM only (cameras.yaml contains camera passwords)
foreach ($p in @($envFile, $tokenFile)) {          # files: no inheritance flags
    & icacls $p /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Warn "Could not restrict permissions on $p" }
}
# Directory: Administrators + SYSTEM only, inherited by everything inside. Then make existing files
# inherit (a recursive /grant with (OI)(CI) on files leaves them with an unusable ACL).
$cfgDir = Join-Path $DataDir 'config'
& icacls $cfgDir /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "Could not restrict permissions on $cfgDir" }
& icacls (Join-Path $cfgDir '*') /reset /T /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "Could not reset permissions inside $cfgDir" }

# ------------------------------------------------------------------ GPU check (offline)
Say "Checking inference device (GPU via DirectML / CUDA, else CPU)"
$env:SENTINEL_HOME = $DataDir
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8:replace'
& $Python -s (Join-Path $InstallDir 'app\scripts\gpu_check.py')
if ($LASTEXITCODE -ne 0) { Fail "The detection runtime could not run on this machine (see output above and $LogFile)." }

# ------------------------------------------------------------------ services (WinSW)
Say "Installing Windows services"
$svcDir = Join-Path $InstallDir 'service'
New-Item -ItemType Directory -Force -Path $svcDir | Out-Null
foreach ($svc in @('SentinelEngine', 'SentinelWatchdog')) {
    $exe = Join-Path $svcDir "$svc.exe"
    $xml = Join-Path $svcDir "$svc.xml"
    Copy-Item (Join-Path $InstallDir 'tools\WinSW-x64.exe') $exe -Force
    $t = Get-Content (Join-Path $Bundle "service\$svc.xml.template") -Raw
    $t = $t.Replace('@INSTALL@', $InstallDir).Replace('@DATA@', $DataDir).Replace('@PORT@', "$Port")
    [IO.File]::WriteAllText($xml, $t, (New-Object Text.UTF8Encoding($false)))
    if (-not (Get-Service -Name $svc -ErrorAction SilentlyContinue)) {
        & $exe install | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "Could not install service $svc" }
    } else {
        & $exe refresh | Out-Null
    }
    & sc.exe failure $svc reset= 3600 actions= restart/5000/restart/10000/restart/30000 | Out-Null
}
Start-Service SentinelEngine

Say "Waiting for the detection engine"
$health = $null
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3; if ($health.detector_loaded) { break } } catch { }
}
if (-not $health -or -not $health.detector_loaded) {
    Fail "Engine did not start. See $DataDir\logs\SentinelEngine.err.log and $DataDir\logs\sentinel.log"
}
Ok "Engine status: $($health.status) - inference: $($health.inference.device)"
Start-Service SentinelWatchdog

# ------------------------------------------------------------------ firewall (only for LAN access)
if ($ListenLan) {
    Say "Opening firewall port $Port (Domain/Private networks)"
    Get-NetFirewallRule -DisplayName 'Sentinel UI/API' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'Sentinel UI/API' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Domain, Private | Out-Null
}

# ------------------------------------------------------------------ shortcuts
$url = "http://127.0.0.1:$Port/#token=$token"
if (-not $NoShortcut) {
    Say "Creating shortcuts"
    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    $chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    $browser = $edge
    if (-not $browser) { $browser = $chrome }
    if ($ShortcutCurrentUserOnly) { $targets = @([Environment]::GetFolderPath('Desktop')) }
    else { $targets = @("$env:PUBLIC\Desktop", "$env:ProgramData\Microsoft\Windows\Start Menu\Programs") }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($dir in $targets) {
        $lnk = $shell.CreateShortcut((Join-Path $dir 'Sentinel Fire Monitor.lnk'))
        if ($browser) { $lnk.TargetPath = $browser; $lnk.Arguments = "--app=$url" }
        else { $lnk.TargetPath = "$env:SystemRoot\explorer.exe"; $lnk.Arguments = $url; Warn "No Edge/Chrome found: shortcut opens the default browser (Windows Server 2019: install Edge from the bundle's extras or open $url)" }
        $lnk.Description = 'Sentinel supplementary fire detection - operator console'
        $lnk.Save()
    }
    Ok "Shortcut 'Sentinel Fire Monitor' created ($($targets -join ', '))"
}

Stop-Transcript | Out-Null
Write-Host ""
Write-Host "Installed." -ForegroundColor Green
Write-Host "  Console:      $url"
Write-Host "  Access key:   $token   (also in $tokenFile)"
Write-Host "  Services:     Get-Service SentinelEngine, SentinelWatchdog"
Write-Host "  Logs:         $DataDir\logs"
Write-Host "  Alerts setup: edit $notif, put secrets in $envFile, then Restart-Service SentinelEngine, SentinelWatchdog"
Write-Host "  Uninstall:    powershell -ExecutionPolicy Bypass -File `"$InstallDir\uninstall.ps1`""
Write-Host "Before relying on it: run a 2-4 week site trial and record a verdict for every alert (Analytics > Site trial)."
