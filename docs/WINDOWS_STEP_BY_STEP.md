# Sentinel on an offline Windows machine — step by step

> Supplementary early warning only. Never a replacement for certified fire alarm systems.

You need: one **internet-connected PC** (to download), one **USB stick ≥ 1 GB**, and the **target Windows machine**
(Windows 10/11 or Windows Server 2016+, 64-bit, administrator account). The target machine needs no internet.

---

## Part A — On the internet-connected PC

### Step 1. Download the ready-made bundle (easiest)

1. Sign in to GitHub and open the workflow runs:
   **https://github.com/aivisual-cyber/sentinel-app/actions/workflows/windows-bundle.yml**
2. Click the newest run with a **green tick** (branch `claude/jolly-ritchie-hif2il`).
3. At the bottom of the run page, under **Artifacts**, click **`Sentinel-Windows-x64-offline`** to download it
   (about 126 MB). GitHub wraps it: the download is a zip that contains `Sentinel-Windows-x64.zip`.
   Artifacts are kept for 30 days; to get a fresh one, open the workflow page → **Run workflow**.

*Alternative — build it yourself* (any PC with Python 3.10+, Node.js 18+, Git):
```powershell
git clone -b claude/jolly-ritchie-hif2il https://github.com/aivisual-cyber/sentinel-app.git
cd sentinel-app
python scripts/build_windows_bundle.py
```
Result: `build\Sentinel-Windows-x64.zip` (and the unpacked folder `build\Sentinel-Windows-x64\`).

### Step 2. Download the GPU driver (recommended, still on the internet PC)

1. Find the GPU model of the target machine (on the target: *Device Manager → Display adapters*).
2. Download the newest driver for it from **https://www.nvidia.com/Download/index.aspx**
   (AMD: amd.com/support, Intel: intel.com/support). Older GTX cards: pick the newest driver offered for that card.

### Step 3. Copy to the USB stick

Copy onto the USB stick:
* `Sentinel-Windows-x64.zip` (take it out of the GitHub wrapper zip first)
* the GPU driver installer (`.exe`)
* optionally: a short **fire test video** (`.mp4`) to test detection without a real fire

---

## Part B — On the target (offline) Windows machine

### Step 4. Install the GPU driver (skip if already current)

Run the driver `.exe` from the USB stick, reboot if asked.
Check: press `Win+R`, type `dxdiag`, Enter → *Display* tab should show **Feature Levels 12_x or 11_x** and DirectX 12.

### Step 5. Copy and unpack the bundle

1. Copy `Sentinel-Windows-x64.zip` from the USB stick to e.g. `C:\Install\`.
2. Right-click the zip → **Properties** → tick **Unblock** (if shown) → OK.
3. Right-click the zip → **Extract All…** → `C:\Install\` → you get `C:\Install\Sentinel-Windows-x64\`.

### Step 6. Run the installer

1. Start menu → type **PowerShell** → right-click **Windows PowerShell** → **Run as administrator**.
2. Run:
   ```powershell
   cd C:\Install\Sentinel-Windows-x64
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
3. Watch the output. You should see:
   * the Windows version check,
   * the **GPU check**, e.g. `DmlExecutionProvider  OK  12.3 ms/frame` (GPU) — or `CPU only` if no usable GPU,
   * `Engine status: degraded - inference: GPU via DirectML: NVIDIA ...` (`degraded` = no cameras yet, normal),
   * **Installed.** with the **Console** address and the **Access key** — write the key down.

   Options: `-ListenLan` (open the console from other PCs on the network), `-Port 8765`,
   `-ShortcutCurrentUserOnly` (shortcut with the key only on your desktop).

### Step 7. Open the console

Double-click **Sentinel Fire Monitor** on the desktop (opens in an Edge app window).
Left panel *System status* should show **Detection Engine ACTIVE** and **Inference GPU · DirectML** (or CPU).
The header says **SYSTEM WARNING – no cameras configured** until you add one.

### Step 8. Add a camera

*Surveillance Grid → Add Camera*:
* **Camera ID**: e.g. `warehouse_1`; **Display name / Location** as you like
* **RTSP URL**: the camera's stream, e.g. `rtsp://192.168.1.64:554/Streaming/Channels/102` (Hikvision sub-stream),
  `rtsp://IP:554/cam/realmonitor?channel=1&subtype=1` (Dahua), plus username/password
* Click **Test**, then **Add**. The tile should show **LIVE** with the picture.

No camera at hand? Add a **video file** as a camera: copy the test video to
`C:\ProgramData\Sentinel\data\test_fire.mp4` and use that path as the URL.

### Step 9. Test detection

* Show the camera a fire **video on a screen/phone** (or use the video-file camera from step 8).
* Within a few seconds: red **FIRE ALERT (P1)** banner + siren (click once anywhere in the console to allow sound),
  alert in *Incident Management* with the **EVIDENCE** image.
* Mark it **TEST** (drills) — real ones **REAL FIRE** / **FALSE ALARM**.

### Step 10. Check speed and capacity on this machine

In the Administrator PowerShell:
```powershell
& "C:\Program Files\Sentinel\python\python.exe" "C:\Program Files\Sentinel\app\scripts\gpu_check.py"
$env:SENTINEL_HOME="C:\ProgramData\Sentinel"
& "C:\Program Files\Sentinel\python\python.exe" "C:\Program Files\Sentinel\app\scripts\benchmark.py"
```
`benchmark.py` prints how many cameras this machine handles at 2 analysed frames/second.

### Step 11. (Optional) Email / webhook / SMS alerts

Edit `C:\ProgramData\Sentinel\config\notifications.yaml` (enable a channel), put the password/URL in
`C:\ProgramData\Sentinel\sentinel.env`, then `Restart-Service SentinelEngine, SentinelWatchdog`, and in the console
*Detection Settings → Alert delivery → Send test*. On an offline network use an internal mail relay.

### Step 12. Run the site trial

Leave it running ≥ 14 days, give a verdict for **every** alert, and review *Analytics → Site trial*
(precision, false alarms per day, per camera). Procedure and go/no-go guidance: `app\docs\OPERATIONS.md`.

---

## Everyday commands (Administrator PowerShell)

| Task | Command |
|---|---|
| Status | `Get-Service SentinelEngine, SentinelWatchdog` |
| Health | `Invoke-RestMethod http://127.0.0.1:8765/api/health` |
| Restart | `Restart-Service SentinelEngine` |
| Logs | `C:\ProgramData\Sentinel\logs\` (`sentinel.log`, `SentinelEngine.err.log`) |
| Access key | `Get-Content C:\ProgramData\Sentinel\api_token` |
| Upgrade | unpack a newer bundle, run its `install.ps1` (keeps cameras, alerts, key) |
| Uninstall | `powershell -ExecutionPolicy Bypass -File "C:\Program Files\Sentinel\uninstall.ps1"` (`-RemoveData` deletes data) |

## If something goes wrong

| Problem | What to do |
|---|---|
| "running scripts is disabled" | use the exact command in step 6 (`-ExecutionPolicy Bypass`) |
| Installer says *Bundle is incomplete* | the zip was not fully extracted — extract again (step 5) |
| Inference shows **CPU** on a GPU machine | install/update the GPU driver (step 4), run `gpu_check.py`; datacenter cards in TCC mode need WDDM mode |
| Console shows **DETECTION OFFLINE** | `Get-Service SentinelEngine`; read `C:\ProgramData\Sentinel\logs\SentinelEngine.err.log` |
| Camera stays **connecting** | open the same RTSP URL in VLC on that machine; check user/password and network |
| No siren | click once inside the console window (browsers need one click before playing sound) |
