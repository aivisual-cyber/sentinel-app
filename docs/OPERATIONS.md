# Sentinel – Deployment & Operations Guide

> **Supplementary early warning only.** Sentinel never replaces certified fire alarm systems (NFPA 72 / EN 54 / UL 268). Keep them installed, maintained and primary.

This guide takes a site from install to a **go / no-go decision** based on measured performance.

---

## 1. Architecture in production

```
 cameras (RTSP / HTTP) ──► sentinel.service (always on, systemd, user 'sentinel')
                              detection (YOLO, batched, 2 analysed FPS/camera)
                              3-of-5-frame confirmation + per-camera cooldown
                              SQLite alerts + evidence frames + feedback dataset
                              API 127.0.0.1:8765 (token-protected)
                              ├─► email / webhook / SMS (persistent outbox, retries)
                              └─► heartbeat ping (dead-man's switch, optional)
 sentinel-watchdog.timer (every 30 s, separate process) ──► notifies DOWN / DEGRADED / RECOVERED,
                                                            restarts a hung service
 Desktop app (Tauri) ── client only: reads /etc/sentinel/api_token, attaches to the service.
                        Closing the app never stops monitoring.
```

## 2. Hardware sizing

Run the capacity benchmark **on the target machine** before choosing camera counts:

```bash
sudo -u sentinel /opt/sentinel/venv/bin/python /opt/sentinel/app/scripts/benchmark.py
```

Reference (4-thread CPU, no GPU, YOLOv8n @640): **4 cameras at 2 analysed FPS**, ~60 ms per frame.
A CUDA GPU is recommended for more cameras. If the service cannot keep up, `/api/health` reports
`detection overloaded` and the header shows SYSTEM WARNING.

## 3. Install (Linux, systemd)

```bash
git clone <repo> sentinel && cd sentinel
sudo ./scripts/install.sh
sudo usermod -aG sentinel <operator-user>      # lets the desktop app read the API token
```

| Path | Contents |
|---|---|
| `/opt/sentinel/app`, `/opt/sentinel/venv` | code, Python environment |
| `/var/lib/sentinel` (`SENTINEL_HOME`) | `config/`, `data/` (alerts.db, alert_frames/, feedback/), `logs/`, `models/` |
| `/etc/sentinel/sentinel.env` | API token + notification secrets (0640 root:sentinel) |
| `/etc/sentinel/api_token` | token read by the desktop app |

Check: `systemctl status sentinel` · `curl -s 127.0.0.1:8765/api/health` · `journalctl -u sentinel -f`

Upgrade: `git pull && sudo ./scripts/install.sh` (keeps config, cameras, token, alerts) then `sudo systemctl restart sentinel`.

Docker alternative: see `docker-compose.yml` (engine + watchdog containers, NVIDIA runtime).

## 4. Cameras

Desktop app → **Surveillance Grid → Add Camera** (or `POST /api/cameras/add`). Use the camera's **sub-stream**
(e.g. 640–1280 px) – detection runs at 640 px anyway. Credentials are URL-encoded and stored in
`config/cameras.yaml` (mode 0600). Status per camera: `active`, `connecting` (with the last error), `inactive`.
Cameras reconnect automatically; an outage is reported in the header, `/api/health` and by the watchdog.

## 5. Alert delivery

1. Edit `/var/lib/sentinel/config/notifications.yaml` (template documents every field): email (SMTP, frame attached),
   webhook (generic JSON or Slack), Twilio SMS. Each channel has a `min_level` (P1, P2 or P4).
2. Put secrets in `/etc/sentinel/sentinel.env` (`SENTINEL_SMTP_PASSWORD`, `SENTINEL_WEBHOOK_URL`, `SENTINEL_TWILIO_TOKEN`).
3. `sudo systemctl restart sentinel`, then **Detection Settings → Alert delivery → Send test** for each channel.

Alerts raised while the network is down stay in the outbox and are retried with backoff for `max_age_hours`.

**Heartbeat (strongly recommended):** create a check at healthchecks.io / Uptime Kuma (push) / Cronitor, set
`heartbeat.enabled: true` and `SENTINEL_HEARTBEAT_URL`. Sentinel pings only while detection is actually
working; the external service alerts you if pings stop (dead PC, power, network) – failures Sentinel cannot report itself.

## 6. Site trial (required before relying on Sentinel)

1. Run for **at least 14 days** with all production cameras, existing alarms unchanged.
2. Operators record a verdict for **every** alert in **Incident Management**: **REAL FIRE**, **FALSE ALARM** or **TEST**
   (use TEST for drills with a controlled flame / smoke source).
3. Watch **Analytics → Site trial**: verified fraction (aim ≥ 80 %), precision, false alarms/day and **P1 false alarms/day**
   per camera, median time to acknowledge. Export CSV for the safety file.
4. Run controlled tests (e.g. smoke pellet / small tray fire where permitted, or a flame video on a screen in view of
   each camera) to confirm each camera *can* alert, and record them as TEST.

**Go/no-go guidance** (agree the numbers with the site's fire-safety lead): P1 false alarms ≤ 1 per week per site,
every camera detected its controlled test, ≥ 80 % of alerts verified. Tune `config/detection_config.yaml`
(thresholds, `temporal.min_hits/window`, `alerts.cooldown_seconds`) – changes apply within seconds.

## 7. Improving the model with site data

Every FALSE ALARM verdict saves the frame as a hard negative; every REAL FIRE saves a pseudo-labelled positive
(`/var/lib/sentinel/data/feedback`). Periodically (GPU machine recommended):

```bash
sudo -u sentinel env SENTINEL_HOME=/var/lib/sentinel /opt/sentinel/venv/bin/python /opt/sentinel/app/scripts/train_model.py all \
  --public /data/D-Fire --negatives /data/site_negative_footage --epochs 50 --device 0
```

The candidate is evaluated against the current model on the same held-out data (**recall at a 2 % false-alarm
budget** and false alarms at the P4 threshold) and promoted **only if better**. The running service hot-swaps it;
a broken model is rejected and the old one keeps running. History: `models/MODEL_HISTORY.jsonl`.
**Rollback:** set `detection.model.path` back to the previous file.

## 8. Watchdog & failure handling

| Failure | Detected by | Result |
|---|---|---|
| Service crash | systemd `Restart=always` | restarted in 5 s |
| Hung/stalled detection | `/api/health` → critical; watchdog (3 × 30 s) | notification + `systemctl restart sentinel` |
| Camera offline / frozen | health → degraded | header warning; watchdog notification |
| Overload | health → degraded | header warning ("add a GPU or reduce cameras") |
| Notification channel failing | health → degraded | header warning; outbox retries |
| PC / power / network down | external heartbeat monitor | alert from the monitoring service |

## 9. Daily / weekly checks

* Daily: header shows **SYSTEM OPTIMAL**; no unverified alerts older than a shift.
* Weekly: Analytics → Site trial; disk use of `data/alert_frames` (retention: `system.log_retention_days`).
* Monthly: send a test notification on every channel; run a controlled detection test per camera.
