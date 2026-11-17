# Sentinel for Windows — offline installation

> **Supplementary early warning only.** Never a replacement for certified fire alarm systems.

This folder is a complete, **offline** installation: Python, all libraries, the Microsoft VC++ runtime DLLs,
the fire/smoke model, the operator console and the service wrapper are included. The target machine needs
**no internet, no Python, no CUDA, no Visual C++ Redistributable**.

## Supported systems

| | |
|---|---|
| Windows | 10 (1607+) / 11, x64 · Windows Server 2016 / 2019 / 2022 / 2025 (Desktop Experience) |
| Server Core | engine and services work; use a browser on another PC with `-ListenLan` for the console |
| RAM / disk | 8 GB+ / 5 GB free (+ evidence frames) |

## GPU support (DirectML)

Inference runs on **ONNX Runtime with DirectML**, which works on any **DirectX 12–capable GPU** through the
normal display driver (no CUDA toolkit or cuDNN). The installer runs `gpu_check.py` and prints which device
is used and its speed; the console shows it under *System status → Inference*.

| NVIDIA generation | Examples | Expected |
|---|---|---|
| Blackwell | RTX 50xx, RTX PRO 6000 Blackwell | GPU (DirectML) |
| Ada / Hopper | RTX 40xx, RTX 4000/6000 Ada, L4, L40 | GPU (DirectML) |
| Ampere | RTX 30xx, A2000–A6000, A10, A16 | GPU (DirectML) |
| Turing | GTX 16xx, RTX 20xx, T400–T1000, Quadro RTX, T4 | GPU (DirectML) |
| Volta / Pascal | Titan V, GTX 10xx, Quadro P-series, Tesla P4 | GPU (DirectML) |
| Maxwell | GTX 750/750 Ti, GTX 9xx, Quadro M-series | GPU (DirectML) |
| Kepler | GTX 600/700 (except 750/750 Ti), Quadro K | DirectML usually works on the last Kepler driver (R470, DX12 feature level 11_0); if not → CPU fallback |
| Fermi and older | GTX 400/500 | CPU fallback |

Also works on AMD Radeon and Intel graphics. If no usable GPU is found, detection runs on the CPU automatically
(the YOLOv8n model is small: ~40 ms/frame on a modern 4-core CPU ≈ 10+ cameras at 2 analysed FPS).

Tips: install the newest NVIDIA driver available for the card *before* going offline (copy the driver
installer onto the USB too). Datacenter GPUs in TCC mode (some Tesla cards) have no DirectX: switch them to WDDM
mode (`nvidia-smi -dm 0`, reboot) or use CPU.

## Install

1. Copy this whole folder (or the `.zip`, then *Extract All*) to the target machine, e.g. `C:\Install\Sentinel`.
   Optionally verify integrity: `Get-FileHash .\models\fire_smoke.onnx` against `MANIFEST.txt`.
2. If the zip came from another computer, Windows may block it: right-click the zip → *Properties* → *Unblock*
   before extracting (or run `Get-ChildItem -Recurse | Unblock-File` in the folder).
3. Open **PowerShell as Administrator** in the folder and run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   Options: `-InstallDir`, `-DataDir`, `-Port 8765`, `-ListenLan` (console reachable from the LAN, token-protected,
   opens the firewall port), `-NoShortcut`, `-ShortcutCurrentUserOnly`.
4. Open **Sentinel Fire Monitor** from the desktop / Start menu (Edge app window). Add cameras in
   *Surveillance Grid → Add Camera* (use the camera's RTSP sub-stream).

What the installer does: copies the program to `C:\Program Files\Sentinel`, creates `C:\ProgramData\Sentinel`
(config, alerts, evidence, logs, model), generates the access key, restricts config/secrets to Administrators +
SYSTEM, checks the GPU, installs and starts two Windows services:

* **SentinelEngine** – detection, alerts, console (`http://127.0.0.1:8765/`); restarts automatically on failure.
* **SentinelWatchdog** – independent health check; notifies when monitoring is down/degraded and restarts a hung engine.

Re-running `install.ps1` from a newer bundle **upgrades** in place and keeps configuration, cameras, alerts and
the access key. Uninstall: `C:\Program Files\Sentinel\uninstall.ps1` (`-RemoveData` also deletes alerts/config).

## Alerts by email / webhook / SMS

Edit `C:\ProgramData\Sentinel\config\notifications.yaml`, put secrets (SMTP password, webhook URL, Twilio token)
in `C:\ProgramData\Sentinel\sentinel.env`, then `Restart-Service SentinelEngine, SentinelWatchdog` and use
*Detection Settings → Alert delivery → Send test*. On an offline network use an internal SMTP relay or webhook
receiver; alerts raised while the network is down are retried.

## Troubleshooting

| Symptom | Check |
|---|---|
| Console shows DETECTION OFFLINE | `Get-Service SentinelEngine`; logs in `C:\ProgramData\Sentinel\logs` (`SentinelEngine.err.log`, `sentinel.log`) |
| Inference shows CPU on a GPU machine | `C:\Program Files\Sentinel\python\python.exe "C:\Program Files\Sentinel\app\scripts\gpu_check.py"` – update the display driver; check `dxdiag` shows DirectX 12 |
| Camera `connecting` | test the RTSP URL in VLC from the same machine; firewall between PC and camera |
| Forgot access key | `C:\ProgramData\Sentinel\api_token` (Administrators only) |
| Capacity | `...\python.exe "...\app\scripts\benchmark.py"` – cameras at 2 analysed FPS on this machine |

Operating procedures (site trial, verdicts, retraining): `app\docs\OPERATIONS.md`.
