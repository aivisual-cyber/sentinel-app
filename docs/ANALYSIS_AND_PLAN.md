# Sentinel — In-Depth Code Analysis, Runtime Findings & Remediation Plan

> **Scope.** Every source file in the repository was read (backend, frontend, Tauri shell, scripts, configs, tests, CI, Docker, firmware) and the system was **executed**: the test suite, the API server launched exactly as the Tauri shell launches it, cameras added/removed/listed through the API, live frames pulled, the detector evaluated on **268 real, labelled fire photographs**, and the dashboard rendered in a headless browser.
>
> **Date of analysis:** 2026-11-16 · **Commit analysed:** `b60ab7b` (includes `rabahdev_best.pt`)
>
> **Status update:** the critical/major pipeline bugs found here have since been **fixed on this branch and verified live** — see **§9 Fixes applied**. Sections 0–6 describe the code *as analysed* (before fixes).
>
> **Supplementary-system reminder:** Sentinel is, by its own charter, a *supplementary* early-warning layer. Nothing below changes that; several findings show the current build would not even meet that bar yet.

---

## 0. Executive summary

| # | Verdict | Evidence |
|---|---|---|
| 1 | **The shipped model cannot detect fire.** All five bundled `.pt` files (`models/fire_yolov8n.pt`, `models/yolov8n_base.pt`, `backend/models/…`, `backend/detection/models/…`, `src-tauri/models/…`) are byte-identical (md5 `0305608…`) stock COCO YOLOv8n with 80 classes and **no fire/smoke class**. | On 100 real fire images: **0 alerts**. |
| 2 | **The only path that ever raises an alert is a *person/car/truck* at ≥0.90 confidence** (`fire_detector.py:183,199`). | Across 270 images the *single* alert raised was a **person box @ 0.904** → P2 (docs config) / **P1 "🔥 FIRE DETECTED"** (desktop config). That frame (a classroom flame demo) does contain a small flame, but the box was the *person* — right image, wrong reason; the flame itself was never detected. |
| 3 | **Even with a real fire model, no alert is ever stored or notified.** `Detection.bbox` is a tuple of `numpy.int64`; `json.dumps` in `Alert.to_dict()` throws. | Live run with `rabahdev_best.pt`: **109 alerts created, 218 save failures, 0 rows in DB, dashboard shows 0**. |
| 4 | **"Real RTSP integration" is not wired in.** `/api/cameras/add` feeds the RTSP URL into `CameraSimulator`, which falls back to *synthetic blue frames* when the URL isn't a local file. `RTSPManager` is only used to *test* URLs. | `api_server.py:363`, `video_simulator.py:48-51`. |
| 5 | **Desktop app runs with undocumented, lower thresholds** (0.78/0.60/0.50) because the backend is spawned with cwd `src-tauri/` and loads `src-tauri/config/detection_config.yaml`. | Startup log: `Detection thresholds: P1=0.78, P2=0.6, P4=0.5`. |
| 6 | **Threshold changes never reach the detector** (no hot-reload in `FireDetector`), and the API accepts any name/value. | Set P1=5.0 → `{"success":true}`, P1 alerts kept firing. |
| 7 | **All cameras are lost on every restart** (`yaml.dump` writes `!!python/tuple`, loader uses `safe_load`). | `Failed to load camera config: could not determine a constructor for the tag 'tag:yaml.org,2002:python/tuple'`. |
| 8 | **Most Tauri commands are broken at the IPC layer.** Tauri v2 expects camelCase args; the UI sends `camera_id`, `rtsp_url`, `alert_id`. Only `update_threshold` has `rename_all="snake_case"`. | add/test/remove camera, get frame, acknowledge alert all fail in the desktop app. |
| 9 | **UI is fail-healthy:** with no backend it shows *Threat level LOW*, *99 % uptime*, *Detection Engine ACTIVE*. | Screenshot below. |
| 10 | **Startup deletes a tracked model and downloads from the internet** (PyTorch ≥2.6 `weights_only` breaks `_verify_model`). Breaks "offline-first". | `git status` after run: ` D models/yolov8n_base.pt`, `?? yolov8n.pt`. |
| 11 | **Production entry point is dead.** `run_sentinel.py` (Docker `CMD`) calls `ConfigManager.load_config()` which doesn't exist; Docker healthcheck targets port 8000, server listens on 8765. | `'ConfigManager' object has no attribute 'load_config'`. |
| 12 | **Advertised features that do not exist in code:** SMS/Email/Twilio alerts, network failover (only a variable swap, not wired in), adaptive/sunset/fog thresholds, offline sync, GPU batching, `scripts/health_check.py`. | `grep -r twilio\|smtplib` → nothing. |
| 13 | **Repository history is synthetic.** Commits were produced by deleted `generate_commits.py` scripts ("Generate realistic backdated commits"); feature-titled commits (e.g. "Harden local API CORS", "Optimize GPU tensor allocation") only append the comment `# Refactored for stability and edge performance`. The whole codebase entered in one commit (`d4929e1`, 41 152 lines). | `git show 8bc1d80`, `git show --stat 3a49628`. |

**Bottom line:** the architecture is a reasonable skeleton and the UI is polished, but the detection→alert chain is broken at four independent points (model, class filter, serialization, IPC). With the newly added `rabahdev_best.pt` the *model* problem is largely solved (see §4); the other three are straightforward code fixes, listed in §7.

![Dashboard with the backend completely down](analysis/ui_with_backend_down.png)
*Dashboard with **no backend process at all**: "THREAT LEVEL LOW · No active threats detected", "SYSTEM UPTIME 99 % · Operational excellence", "Detection Engine ACTIVE", "Alert System READY".*

---

## 1. How the system actually works (as-built architecture)

```
┌──────────────────────── Tauri desktop shell (src-tauri/src/lib.rs) ───────────────────────┐
│  setup(): spawn  ../venv/bin/python3 -u ../backend/api_server.py   (cwd MUST be src-tauri) │
│  every 5 s: GET /api/dashboard → emit "real-time-update"                                   │
│  14 #[tauri::command]s proxy HTTP → http://localhost:8765/api/*                            │
└───────────────┬────────────────────────────────────────────────────────────────────────────┘
                │ invoke()/listen()                         ▲ HTTP (aiohttp, CORS "*")
┌───────────────▼──────────── React UI (src/) ──────────────┴───────────────────────────────┐
│ App → Header / Navigation / MainContent(lazy views)                                         │
│ hooks: useStableAlertSystem, useStableSystemStatus, useCameraManager (+2 unused duplicates) │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────── backend/api_server.py (aiohttp :8765) ─────────────────────────┐
│ SentinelAPIServer ──► main.SentinelSystem                                                   │
│     ├─ ConfigManager  (YAML, mtime reload — but FireDetector never re-reads it)             │
│     ├─ AlertManager   (SQLite data/alerts.db, 2 worker threads, logs "notifications")       │
│     ├─ FireDetector   (YOLO; also builds unused RTSPManager + AppNotificationManager)       │
│     └─ StreamProcessor ─► MultiCameraSimulator ─► CameraSimulator(thread per camera)        │
│            loop every 0.1 s: for each latest frame → detect_fire → create_alert             │
│ CameraConfigManager (config/cameras.yaml)   RTSPManager (used only to *test* a URL)         │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
Not imported by the running app: network_monitor, performance_optimizer, model_trainer,
rtsp_advanced, arduino_bridge, core/base_detector, alerts/notification_system.py
```

**Data flow of one frame (as executed):** camera thread overwrites `current_frame` → `StreamProcessor._process_loop` (10 Hz, *re-processes the same frame if no new one arrived*) → `FireDetector.detect_fire` → `_is_fire_related` filter → `_determine_alert_level` (static thresholds loaded once) → `AlertManager.create_alert` (queue) → `_process_alerts` thread → `save_alert` (**fails: int64**) → *(would)* `notification_queue` → `_send_immediate_notification` (only `logger.critical` + DB log row). In parallel `main._detection_callback` saves an annotated JPEG for **every** alert and calls `save_alert` a second time (race with the worker thread).

---

## 2. Runtime evidence log (what was run and what happened)

| Step | Command / action | Result |
|---|---|---|
| Tests | `pytest tests/` (Py 3.13, torch 2.15, ultralytics 8.4, OpenCV 5.0) | **2 failed, 37 passed, 4 skipped.** Failures: `test_notifications.py` constructs `AlertMessage(alert_level=…)` — field is `alert_type`, `alert_id` required. |
| Lint (CI rule set) | `flake8 --select=E9,F63,F7,F82` | 0 (CI lint passes). Broader `F` set: 62 unused imports, 48 F811 redefinitions (`core/base_detector.py` is 15 copies of one import line). |
| CI config | `.github/workflows/ci.yml` | `apt-get install libgl1-mesa-glx` — package removed in Ubuntu 24.04 (`ubuntu-latest`) → CI will fail at that step. |
| Backend start (as Tauri) | `cd src-tauri && python -u ../backend/api_server.py` | Up on :8765. Logs: thresholds **0.78/0.6/0.5**; model "verification failed" → **deletes `models/yolov8n_base.pt`**, downloads `yolov8n.pt` from GitHub; `Failed to initialize alert database: no such column: alert_type` (schema clash). |
| Add cameras | `POST /api/cameras/add` ×4 (synthetic, 2 video files, `test_fire_scenarios.avi`) | All succeed. Duplicate id → 400 ✔. Missing field → 400 ✔. Given `name`/`location` **not returned** by `/api/cameras` ("Camera kitchen"/"Unknown"); resolution hard-coded `1920x1080`; `last_frame_time` always 0. |
| Live frames | `GET /api/cameras/{id}/frame` | ✔ base64 JPEG for each (raw frames, no boxes). |
| Detection, stock model | 4 cameras × 60 s | 0 alerts (real fire never detected). |
| Restart | kill + start | **All cameras gone** (`!!python/tuple` safe_load error). |
| Detection, `rabahdev_best.pt` | kitchen camera (30 real fire photos looped) × 45 s | **109 alerts created (16 P1 / 50 P2 / 43 P4) → 2.4 alerts/s, 0 stored** (`int64 is not JSON serializable`), 109 JPEGs = 9.9 MB → **~0.8 GB/h/camera**, no cleanup. |
| Acknowledge | `POST /api/acknowledge` with a logged id | `{"success": false}` (never stored). |
| Threshold API | `immediate_alert=5.0`, `__anything__=0.1` | Both `{"success": true}` and written to YAML; detector unaffected (P1 continued). |
| CORS | `OPTIONS` with `Origin: https://evil.example` | Reflected + `Allow-Credentials: true` → any web page can drive the API. |
| Remove camera | `DELETE /api/cameras/kitchen/remove` twice | ✔ then 404 ✔. |
| Production entry | `python run_sentinel.py` | `'ConfigManager' object has no attribute 'load_config'` → exits (code 0, so Docker restart-loops). |
| Frontend | `npm ci && npm run build` | ✔ builds (300 kB main chunk). |
| UI, backend down | Playwright screenshot | Fail-healthy dashboard (above). |
| Rust | `cargo check` | **Not run** — WebKitGTK system libs absent in this container. Findings in §5.3 are from code review + Tauri v2 docs. |
| Real RTSP transport | MediaMTX server | **Not run** — executing the downloaded server binary was not permitted in this session; cameras were emulated with local video/image sources, which traverse identical add/list/frame/detect/remove code paths. |

Reproduce the model evaluation: `python docs/analysis/eval_detector.py <repo> <images_dir> <non_fire_dir> <config.yaml>`.

---

## 3. Fire-detection reality check (stock model)

Dataset: public Roboflow-format fire set from `github.com/faseeh41/Fire-Detection-Using-YOLOV8` (images + YOLO labels; 268 images with ≥1 fire box). Non-fire: Ultralytics `bus.jpg`, `zidane.jpg`.

| Model in app | Thresholds | Fire images alerted | What the model "saw" |
|---|---|---|---|
| stock COCO YOLOv8n (shipped) | 0.95/0.85/0.70 | **0 / 100** | car 8, train 7, refrigerator 3, person 2 … |
| stock COCO YOLOv8n (shipped) | 0.78/0.60/0.50 (desktop) | **0 / 100** | same |
| stock, all 270 images | both | 1 alert total — a **person @ 0.904** (P2 / **P1**) | — |

The unit test suite *encodes* this behaviour: `tests/test_fire_detector.py:151` asserts `_is_fire_related('car', 0.95) == True`. There is **no test that feeds a real fire image and expects a fire alert.**

`model_trainer.py` cannot fix this either: its "datasets" are generated rectangles whose YOLO labels are drawn from an *independent* random box (`_create_fire_label`) — labels never overlap the drawn fire — so training learns nothing. `fire_model_manager.simulate_fire_training()` returns hard-coded "mAP50 0.892" numbers.

---

## 4. The newly added model: `rabahdev_best.pt`

### 4.1 What it is (read from inside the checkpoint)

Inspected **without executing pickle code** (pickletools scan, then `torch.load(weights_only=True)` with an allow-list that admitted only 18 `torch`/`ultralytics` layer classes — no `os`, `subprocess`, `eval`, etc. were referenced).

| Field | Value |
|---|---|
| SHA-256 | `b91633799ceb052c814b4f8b77a37efc9a40f002d528df97d74463585fa4f28f` |
| Classes | `{0: 'smoke', 1: 'fire'}` |
| Arch / size | YOLOv8n, 3 011 238 params, imgsz 640 |
| Training | 50 epochs, batch 16, patience 15, `optimizer=auto`, lr0 0.01; resumed/fine-tuned from `runs/fire-smoke/yolov8n-dfire/weights/last.pt` (D-Fire dataset, per the public model card [rabahdev/fire-smoke-yolov8n](https://huggingface.co/rabahdev/fire-smoke-yolov8n)) |
| Reported val metrics | precision 0.775 · recall 0.695 · **mAP50 0.765** · mAP50-95 0.445 |
| Built | 2026-05-20 with Ultralytics 8.4.51 |
| **License** | AGPL-3.0 (inherited from Ultralytics). Out of scope for testing per the project owner; must be resolved before any public distribution. |

**Public model card** ([rabahdev/fire-smoke-yolov8n](https://huggingface.co/rabahdev/fire-smoke-yolov8n), supplied by the owner): YOLOv8n fine-tuned on **D-Fire** (YOLO format; train 14 122 / val 3 099 / test 4 306 images). Base `yolov8n.pt` (COCO), 50 epochs, 640 px, batch 16, optimizer MuSGD (auto) lr0 0.01, trained on Apple MPS. Test split: **mAP50 0.754**, mAP50-95 0.430 (smoke 0.499 / fire 0.362), precision 0.766, recall 0.688. Ships `best.pt` (used here) and `last.pt`. These agree with the metrics embedded in the checkpoint (val split: 0.765 / 0.445 / 0.775 / 0.695). Note fire mAP50-95 (0.362) is notably weaker than smoke (0.499).

### 4.2 How it performs inside Sentinel (independent dataset, not D-Fire)

| Thresholds | Fire images alerted (of 100) | Levels |
|---|---|---|
| documented 0.95/0.85/0.70 | **23** | P4 19, P2 4, **P1 0** |
| desktop 0.78/0.60/0.50 | **68** | P1 12, P2 36, P4 20 |

Image-level sweep on all 268 positives (max fire/smoke confidence per image):

| conf ≥ | 0.25 | 0.35 | 0.45 | 0.50 | 0.60 | 0.70 | 0.78 | 0.85 | **0.95** |
|---|---|---|---|---|---|---|---|---|---|
| recall | 83.2 % | 79.1 % | 75.0 % | 70.9 % | 55.2 % | 33.6 % | 11.9 % | 3.0 % | **0.0 %** |

Max confidence observed on any fire image: 0.924; median 0.631.
**Consequences:** (a) the documented P1 = 0.95 can *never* trigger with this model; (b) single-frame false alarms are real — `zidane.jpg` (two men, no fire) produced **smoke @ 0.739** — so thresholds must be paired with **temporal confirmation** (N of M frames) and per-camera cooldowns (§9).

### 4.3 Searching for better weights — four-model benchmark

Hugging Face is unreachable from this environment, so candidates came from public GitHub repos that ship weights. Every checkpoint was first **vetted** (loaded with `torch.load(weights_only=True)` allow-listing only torch/ultralytics classes — any other pickle global is refused) and benchmarked through that same restricted loader (`docs/analysis/bench_models.py`). Positives: 268 real labelled fire images (Roboflow export, *not* D-Fire). Negatives: Ultralytics **coco128** (128 everyday scenes incl. kitchens, ovens, streets, people) + 2 people photos = 130. Image-level: an image "alarms" if any fire/smoke box ≥ threshold. CPU timings.

| Model (source) | Arch · classes | ms/img | Recall @0.35 | False-alarm @0.35 | Recall @0.55 | FA @0.55 | Best point with FA ≤ 2 % |
|---|---|---|---|---|---|---|---|
| **rabahdev** (owner-supplied, D-Fire) | YOLOv8n · smoke, fire | 74 | **79 %** | 6.9 % | **62 %** | 4.6 % | thr 0.75 → 20.5 % |
| gogulvkn/smoke-fire-detection-with-yolo (D-Fire, Kaggle) | YOLO11s @512 · smoke, fire | 150 | 56 % | 7.7 % | 32 % | 3.1 % | thr 0.60 → **26.9 %** |
| ZTT427/fire-smoke-detection | YOLOv8n · flame, smoke | 65 | 69 % | 18.5 % | 49 % | 10.8 % | thr 0.70 → 22.8 % |
| amenbsa/Fire-smoke-detection-yolov8 | YOLOv8n · Fire, Smoke | 62 | 33 % | 3.8 % | 18 % | 3.1 % | thr 0.70 → 11.9 % |

**Verdict:** none of the reachable alternatives beats `rabahdev_best.pt`; it has the best recall at every threshold with mid-pack false alarms, and is among the fastest. It stays the default. Two practical findings: (1) class names differ across models (`fire`/`flame`/`Fire`), which the old exact-lowercase-substring code mishandled — class names are now configurable and case-insensitive; (2) *every* model alarms on 4–19 % of ordinary scenes at usable thresholds, so pipeline-level suppression (temporal confirmation + cooldown) matters more than swapping weights. Caveat: the positive set may overlap some candidates' training data (e.g. Roboflow-merged sets), which would *flatter* those models; 130 negatives give ±~3 % resolution only.

**How to get genuinely better weights (recommended next step, needs a GPU):** fine-tune YOLO11s/YOLOv8s on D-Fire (21 527 images, CC0) **plus** FASDD and a hard-negative set (sunsets, red/orange lighting, kitchens, welding, fog, reflective metal) at 640 px; select the checkpoint by *recall at ≤1 % image false-alarm on held-out negatives* — not mAP50 — and track per-class fire vs smoke. Re-run `bench_models.py` to compare.

---

## 5. File-by-file analysis

Severity: 🔴 critical (safety/data loss/feature doesn't work) · 🟠 major · 🟡 minor · ⚪ cosmetic/dead.

### 5.1 `backend/`

#### `backend/__init__.py` ⚪
30 lines of the same comment (`# Backend initialization`) — padding.

#### `backend/main.py` — `SentinelSystem`, `APIServer`, `main()`
- `_setup_logging` 🟠 creates `FileHandler('logs/sentinel.log')` **before** `Path('logs').mkdir()` (l.44 vs 49) → `FileNotFoundError` if imported from a dir without `logs/`. Also calls `logging.basicConfig`, which is a no-op once another module configured logging.
- `start` 🟡 installs `signal.signal` handlers from inside a coroutine; when run under `api_server` this overrides that file's handlers.
- `_initialize_components` 🔴 builds `StreamProcessor` (simulator) — never `RTSPManager` — so production cameras are simulated.
- `_detection_callback` 🔴 (1) re-runs detection on the *same* frame every 100 ms when the camera hasn't produced a new one; (2) no de-duplication/cooldown → **2.4 alerts/s per camera measured**; (3) saves an annotated JPEG for every alert (disk exhaustion); (4) calls `database.save_alert` a second time while the worker thread is also saving (race; both fail today); (5) calls private `fire_detector._save_alert_frame`.
- `_main_loop` 🟠 reloads `ConfigManager` but never pushes thresholds into `FireDetector`.
- `_log_system_stats` 🟡 relies on `self.start_time` set externally (AttributeError if constructed elsewhere).
- `stop` 🟡 doesn't stop FireDetector's internal `AppNotificationManager` thread.
- `APIServer` 🟡 thin wrapper; `get_dashboard_data` returns `{'error':…}` with HTTP 200 semantics.

#### `backend/api_server.py` — HTTP API
- CORS 🔴 `"*"` + `allow_credentials=True` + all headers/methods → any origin may call (verified). No authentication on any route (add/remove cameras, thresholds, acknowledge). Commit "Harden local API CORS…" changed only a comment.
- `update_threshold` 🔴 accepts arbitrary `threshold_name` and any float; no 0–1 bound (verified 5.0).
- `get_alert_frame` 🟡 linear scan of 7 days of alerts to find one id; `frame_path` read from DB and opened without confinement to `data/alert_frames` (path trust).
- `get_camera_feeds` 🟠 `name`, `location`, `last_frame_time`, `resolution` don't exist in simulator status → always defaults (verified).
- `get_camera_frame` 🟡 reaches into `stream_processor.simulator` internals; returns un-annotated frames (operator never sees boxes live).
- `discover_cameras` 🟠 synchronous UDP loop inside an async handler → blocks the event loop for `timeout` seconds.
- `add_camera` 🔴 (1) `RTSPManager.test_rtsp_url` is synchronous and has no timeout → blocks the whole server while OpenCV tries to connect (FFmpeg default ~30 s); (2) then passes the URL to `CameraSimulator` (synthetic fallback); (3) password stored in plain YAML; (4) `fps` default 30 vs `CameraProfile` 15.
- `remove_camera` 🟠 if a camera exists in config but failed to start, returns 404 and **leaves it in config**.
- `get_system_metrics` 🟡 `frames_processed` attribute never set → detection metrics never returned.
- `start_backend_system` 🟠 fire-and-forget `create_task(sentinel.start())` + `sleep(2)`; exceptions in `start` are lost.
- `signal_handler` 🟡 `sys.exit(0)` inside the event loop; `stop_server` creates a task after the loop has ended (never runs).
- `__main__` 🟡 pip-installs packages at runtime if imports fail.
- Trailing padding comments ⚪.

#### `backend/detection/fire_detector.py`
- `__init__` 🟠 `logging.basicConfig` in a library; constructs an unused `RTSPManager` and an `AppNotificationManager` (extra SQLite schema + thread).
- `_load_config` 🟠 relative path only, no fallback, read **once** → no hot-reload.
- `_load_model` 🔴 looks for `models/fire_detection.pt` (relative to cwd; none shipped) → otherwise `FireModelManager` downloads COCO weights, "simulates training", and on any failure falls back to `YOLO('yolov8n.pt')` — **internet download at runtime**.
- `detect_fire` 🔴 bbox is `numpy.int64` tuple → breaks JSON serialization downstream; no frame resizing control; latency check is a `pass`.
- `_is_fire_related` 🔴 classes `person/car/truck` ≥ 0.9 count as fire; substring match makes `"smoke_detector"` count as smoke (asserted by tests).
- `_determine_alert_level` 🟠 ignores `environmental.fog_adjustment`, `sunset_hours`, `adaptive` (documented features).
- `_send_alert`, `process_video_stream` ⚪ alternative alert path (AppNotificationManager) used only by the unused RTSP monitoring loop.
- `_annotate_frame` 🟡 colour bands hard-coded to 0.95/0.85 regardless of configured thresholds.
- `stop_monitoring` 🟡 **defined twice** (l.353 and l.403); second wins.
- `get_recent_alerts`, `acknowledge_alert` 🟠 call `notification_manager.alert_queue.get_recent_alerts/acknowledge_alert` — `alert_queue` is a `queue.Queue` → `AttributeError`.
- Tail ⚪ **19 identical copies** of `calculate_flame_intensity` defined inside the `if __name__ == "__main__":` block — never methods, dead code.

#### `backend/detection/fire_model_manager.py`
- `_verify_model` 🔴 `torch.load(path)` with PyTorch ≥ 2.6 default `weights_only=True` fails for Ultralytics checkpoints → `download_model` re-downloads, fails again, then **unlinks the model file** (verified deletion of tracked `models/yolov8n_base.pt`). Loads the whole checkpoint just to verify.
- SHA-256 never configured (`sha256` key absent) → no integrity check of downloaded weights.
- `download_model('fire_yolov8n')` default name not in `available_models` → `ValueError`.
- `simulate_fire_training` 🟠 returns fabricated metrics; `create_fire_detection_model` doesn't change classes.

#### `backend/detection/rtsp_manager.py`
- `RTSPCamera._connect` 🟠 no open/read timeout (`config.timeout` unused); `CAP_PROP_FPS`/size `set()` on network streams are no-ops.
- `_build_rtsp_url` 🟠 credentials not URL-encoded (passwords with `@ : / #` break URLs).
- `stop` 🟡 releases `cap` from another thread while `_read_frames` may be inside `read()`.
- `get_frame` ⚪ `except Empty` unreachable (uses `get_nowait` after `empty()`); returns `None` instead of `current_frame` if queue drained.
- `ONVIFDiscovery` 🟠 static `MessageID`, SO_BROADCAST instead of multicast TTL/interface, assumes `/stream1`; no XAddrs parsing.
- `RTSPManager` ✔ reasonable abstraction (reconnect loop, FPS calc, sanitized logging) — **but unused by the live pipeline**.

#### `backend/config/config_manager.py`
- Path fallback logic ✔ good idea; but the first candidate is cwd-relative, which is why the desktop app picks `src-tauri/config/…`.
- `_validate_config` 🟠 checks ordering only; no `0 ≤ x ≤ 1`, no unknown-key rejection.
- `update_threshold` 🟠 not atomic (truncate-then-write → a crash corrupts config); drops YAML comments.
- `config_manager = ConfigManager()` at import 🟡 side effects (creates files) on import.

#### `backend/config/camera_config.py`
- `save_config`/`load_config` 🔴 `asdict()` keeps `resolution` as a tuple → `yaml.dump` emits `!!python/tuple` → `safe_load` fails → **all cameras lost on restart**, next save overwrites the file (verified). `config/cameras.yaml` in the repo uses a *different schema* (`id`, `url`, `fps_limit`) → also fails to load.
- File permissions 🟠 written 0644 with plaintext passwords.
- `discover_network_cameras` 🟠 sequential scan, 3 ports × timeout per host (/24 ≈ 1 h; /16 ≈ days).
- `_test_rtsp_stream`, `test_camera_connection` 🔴 use `cv2.CAP_PROP_TIMEOUT` which **doesn't exist** (verified on OpenCV 5.0) → `AttributeError` swallowed → always "failed".
- `CameraWizard.guided_setup` 🟡 id derived from name without sanitisation/uniqueness.
- Indented stray comments between imports ⚪.

#### `backend/alerts/alert_manager.py`
- `Alert.to_dict` 🔴 `json.dumps(detections)` fails on numpy ints (verified 100 % failure with a real model).
- `AlertDatabase` 🔴 shares `data/alerts.db` and table name `alerts` with `app_notification_system.AlertDatabase` but with a different schema; whichever initialises first wins (verified error).
- No index on `timestamp` 🟡 (commit "Optimize SQLite indexing on alert timestamps" only added a comment).
- `_process_alerts` 🔴 notification enqueued only after a successful save → P1 notification never sent when save fails.
- `_send_immediate_notification` 🔴 only `logger.critical` + DB log; SMS/email/desktop are comments.
- `get_dashboard_data` 🟡 `active_alerts` counted over the last 50 rows only.
- `acknowledge_alert` 🟡 no "acknowledged_by/at" audit trail.
- `cleanup_old_records` 🟠 never called; frames never deleted.

#### `backend/alerts/app_notification_system.py`
Well-structured (rate limits, stats table, callbacks, retention) but only reachable through the unused RTSP loop. Schema conflicts with `alert_manager` (see above). `_check_rate_limits` is not thread-safe. ⚪/🟠

#### `backend/alerts/notification_system.py` ⚪
13 lines, comments only (`# Modified: 2025-09-20` ×10). The file README/CLAUDE.md describe as "SMS/Email with redundancy & rate limiting" is empty.

#### `backend/utils/video_simulator.py`
- `CameraSimulator._run_simulation` 🔴 any non-file URL → synthetic frames (this is how "RTSP" cameras run today).
- `_simulate_from_video` 🟡 on read failure seeks to 0 and `continue`s with no sleep → busy loop for non-seekable sources.
- `StreamProcessor._process_loop` 🟠 processes all cameras serially in one thread; at ~100 ms inference per frame on CPU, 10 cameras = ~1 s per cycle → misses the "<2 s" target on CPU and wastes GPU time on duplicate frames.
- `get_camera_status` 🟡 reports configured fps, not actual.

#### `backend/utils/system_monitor.py`
- `_estimate_network_latency` 🟠 **fabricated**: "latency" is derived from packet counts (10–20 ms), not measured. Displayed as real in Analytics.
- `check_system_health` 🔴 called by `run_sentinel.py`, **doesn't exist**.
- `datetime.utcnow()` deprecated 🟡.

#### `backend/utils/network_monitor.py` (not wired in)
`_trigger_failover` only changes `self.active_interface` — no routing change; `ping` not bound to an interface (`-I` missing) so every "interface test" is the same test; Linux-only flags; `total_downtime` never updated → uptime always 100 %; `OfflineCapabilityManager` methods are comment stubs. 🟠

#### `backend/utils/performance_optimizer.py` (scripts only)
"Batch" processing calls `detect_fire` per frame; N worker threads share one YOLO model (Ultralytics models aren't thread-safe); `SystemOptimizer` sets `nice(-10)` (needs root) and flips current thread daemon flag; `dropped_frames` TODO. 13 lines of `# Modified:` padding. 🟠

#### `backend/detection/model_trainer.py` (not wired in)
Synthetic dataset with labels uncorrelated to the drawn fire (see §3); "download" functions download nothing; `optimize_model` path replacement assumes `.pt`. 🔴 as a training pipeline.

#### `backend/cameras/rtsp_advanced.py` (not wired in)
Useful brand path table (Hikvision/Dahua/Axis). Issues: sets `CAP_PROP_OPEN_TIMEOUT_MSEC` *after* opening (no effect — must be passed at construction); appends non-standard `?codec=h265` to URLs; credentials in HTTP URL for snapshot (requests won't do digest from URL); `downsample_frame_if_needed` (the "4K VRAM" PR) is never called. 🟡

#### `backend/hardware/arduino_bridge.py` + `arduino/sentinel_sensors/sentinel_sensors.ino` (not wired in)
- 🔴 `SENSOR_PACKET_SIZE = 24` but `struct.calcsize('<BfffffBHB') == 25` (and the packed C struct is 25 bytes) → **every packet raises `struct.error`**; the end-byte check reads the wrong byte.
- 🔴 Firmware calibration sets `R0 = Rs(clean air)`, so clean air gives ratio 1 → MQ-2 ≈ **614 ppm "smoke"** and MQ-135 ≈ **117 ppm "CO"**, both above the alert thresholds (300 / 50) → **buzzer and alerts in clean air**. Datasheet clean-air ratio for MQ-2 is ≈9.8 (R0 = Rs/9.8).
- 🟠 MQ-135 is an air-quality sensor, not a CO sensor (MQ-7 is).
- 🟠 ASCII replies (`ACK:`, `CAL:`) share the serial stream with binary frames; `calculateResistance` divides by zero at ADC 0.
- 🟠 `reconnect()` from inside the read thread → `disconnect()` joins the current thread → `RuntimeError`.
- `pyserial` not in `requirements.txt`.

#### `backend/core/base_detector.py` ⚪
15 copies of `from abc import ABC, abstractmethod`; no class.

#### Config YAMLs
| File | Notes |
|---|---|
| `config/detection_config.yaml` | Documented 0.95/0.85/0.70. |
| `backend/config/detection_config.yaml` | Duplicate of above. |
| `src-tauri/config/detection_config.yaml` 🔴 | **0.78/0.60/0.50**, + 17 duplicate `production: true` keys. This is the file the desktop app loads. |
| `config/production.yaml` | Not read by any code. |
| `config/alerts.yaml` | Not read by any code (sound file, rate limits, recipients). |
| `config/cameras.yaml` 🟠 | Wrong schema (`id/url/fps_limit`) → fails `CameraProfile(**…)`. |
| `*.template` | Fine as docs. |

#### Model files 🔴
`models/fire_yolov8n.pt`, `models/yolov8n_base.pt`, `backend/models/yolov8n_base.pt`, `backend/detection/models/fire_yolov8n.pt`, `src-tauri/models/yolov8n_base.pt` — identical COCO weights (32 MB of duplicates); the `fire_` names are misleading. `rabahdev_best.pt` (root) — real fire/smoke model, see §4.

### 5.2 `src/` — React frontend

| File | Findings |
|---|---|
| `main.jsx`, `App.jsx` | Fine structure; `ErrorBoundary` wraps app. `App` imports `useRef`, `PropTypes` unused. |
| `hooks/useTauriApi.js` 🟠 | `execute()` swallows every error and returns `null` → callers can't distinguish failure from empty; outside Tauri, `invoke` returns `null` silently (dev mode looks "connected-but-empty"). |
| `hooks/useStableAlertSystem.js` 🔴 | `status: alert.acknowledged ? …` — backend sends `status`, never `acknowledged` → **every alert shown active; acknowledgements revert on next poll**. `acknowledge_alert` sent as `{alert_id}` (Tauri expects `alertId`). Polls every 20 s **plus** listens to 5 s Rust events (double traffic). No sound/popup for P1. |
| `hooks/useStableSystemStatus.js` 🔴 | Initial state `systemHealth:'optimal'`, `backendRunning:true`, `uptime:99`; health read from `system_status.health` which backend never sends → **always "optimal"** (screenshot). |
| `hooks/useCameraManager.js` 🔴 | Sends `camera_id`, `rtsp_url` (snake) → Tauri v2 rejects (needs `cameraId`, `rtspUrl`). Drops `fps`, `name`, `location` from the add form. Polls frames sequentially every 2 s, re-creating interval on every `cameras` change. |
| `hooks/useRealAlertSystem.js`, `useRealSystemStatus.js` ⚪ | Dead duplicates of the "Stable" hooks. |
| `hooks/useNotifications.js`, `useDelayedLoading.js` | OK. |
| `components/views/SurveillanceGridReal.jsx` 🔴 | Expanded "View" modal references **undefined `hasFrame`** (l.226) → `ReferenceError` → ErrorBoundary replaces the whole camera grid when an operator clicks "View". `isValidFrameUrl` whitelists RFC1918 HTTP URLs (fine). |
| `components/shared/AddCameraModal.jsx` 🟡 | Good validation UX; `fps` collected but discarded downstream; discovery helper fabricates ids from IP. |
| `components/views/DetectionSettingsReal.jsx` 🟠 | Slider 50–100 % with 5 % spacing rule; success toast even though detector never reloads. Toggles for SMS/email have no backend. |
| `components/views/AnalyticsCenter.jsx` 🟠 | Reads `total_alerts`, `false_positives`, `true_positives`, `average_response_time`, `camera_status`, `frames_processed` — **none exist** in backend responses → permanent "N/A"/"CHECKING". Shows fabricated network latency as real. |
| `components/views/CommandCenter.jsx` 🟠 | "All systems operational" / "Operational excellence" copy rendered regardless of data. |
| `components/views/IncidentManagement.jsx` | Lists alerts; inherits the acknowledged-status bug. No access to alert frames (API exists, no UI). |
| `components/Header.jsx`, `Navigation.jsx` | Status chips driven by the fail-healthy state above. |
| `components/shared/*` (ThresholdPanel, ActivityFeed, MetricCard, ConfirmDialog, ErrorBoundary, NotificationProvider, SentinelLogo) | Generally clean; `ThresholdPanel` unused by the live settings view. |
| `utils/validation.js` | Solid RTSP/threshold validation — the best-tested logic in the UI (no tests though). |
| `index.html` 🟡 | Loads Google Fonts from the internet (offline-first: falls back silently). |
| `styles.css`, `input.css`, `styles/main.css`, `tailwind.config.js`, `postcss.config.js` | Fine; `styles/main.css` is 1 line, unused. |
| `package.json` 🟡 | `@types/react` in dependencies, no lint/test script, React 18 with React-19 types. |

### 5.3 `src-tauri/`

| File | Findings |
|---|---|
| `src/lib.rs` 🔴 | (1) Python path = `current_dir().parent()/venv/...` and script `../backend/api_server.py` → works only for `tauri dev` from `src-tauri/`; **bundled app cannot find backend or venv** (backend not packaged as resource/sidecar). (2) Backend inherits cwd `src-tauri/` → loads `src-tauri/config` thresholds (0.78/0.60/0.50). (3) If port 8765 is in use by *anything*, assumes backend is running. (4) Commands without `rename_all` (add_camera, test_camera, remove_camera, get_camera_frame, acknowledge_alert, discover_cameras) expect camelCase args — UI sends snake_case → **"missing required key"**. (5) No kill of Python on app exit → orphan process. (6) `get_system_status` reports running if a `Child` exists even after it crashed. (7) `add_camera` HTTP timeout 10 s < backend RTSP test time. (8) `std::sync::Mutex` held across logic in async commands (OK today, fragile). |
| `src/main.rs` | Standard. |
| `Cargo.toml` 🟡 | Placeholder metadata (`authors=["you"]`, "A Tauri App"); `reqwest 0.11` (old); "Tauri v2 compatibility verified" comment was the entire content of the "Migrate to Tauri v2" commit. |
| `tauri.conf.json` 🟠 | `csp: null` + `withGlobalTauri: true`; no `bundle.resources`/`externalBin` for the Python backend. |
| `capabilities/default.json` 🟡 | `core:default`, `opener:default`; `tauri_plugin_fs` initialised without permissions (unused). |
| `config/detection_config.yaml` 🔴 | See §5.1 config table. |
| `models/yolov8n_base.pt` | Evidence the app was run from `src-tauri/` (model manager wrote here). |
| `gen/schemas/*` | Generated; `acl-manifests.json` and `capabilities.json` are empty (0 lines). |

### 5.4 `scripts/`

| File | Findings |
|---|---|
| `install.sh` (788 lines) 🟠 | Installs Rust via `curl | sh`, Node, Python venv, systemd unit; service runs `run_sentinel.py` → **broken entry point**; GPU checks; reasonably structured otherwise. |
| `install.ps1`, `test_windows_installer.ps1`, `test_mac_installer.sh` 🟠 | NSSM service wrapping a `.bat`; same broken entry point. |
| `validate_config.py` | Works (`--help` OK); validates YAML structure — but not against the schema the code actually uses for cameras. |
| `benchmark.py` | Imports `performance_optimizer`; measures CPU/GPU matmul, not detection latency on real frames. |
| `field_test_suite.py` | Requires hand-made "ground truth" files; none provided. |
| `rtsp_simulator.py` 🟡 | Writes an `.avi` with drawn circles labelled "FIRE DETECTED" (text burned into the frame); writes `config/cameras.yaml` in the wrong schema; `import os` after use in function (works only because module-level import runs first). |
| `test_rtsp_connection.py` | Works as a manual tool. |
| `health_check.py` 🔴 | Referenced by CLAUDE.md/README — **missing**. |
| root `run_sentinel.py` 🔴 | `ConfigManager().load_config()`, `SentinelSystem(config)`, `get_status()`, `system_monitor.check_system_health()` — **none exist**. |
| root `check_port.ps1` | Small helper. |
| `prototype/detect_fire.py` | Original prototype; same COCO model. |

### 5.5 Tests, CI, packaging, docs

| File | Findings |
|---|---|
| `tests/test_detection.py` | Tests dataclass construction only. |
| `tests/test_notifications.py` 🔴 | 2 tests fail (wrong field names); 3rd asserts arithmetic on local variables, no project code ("cooldown" isn't implemented anywhere). Commit says "add unit tests for rate limiter and message queuing" — neither is exercised. |
| `tests/test_fire_detector.py` 🟠 | Asserts structure, not behaviour; encodes the car-as-fire bug; latency test on a 640×480 synthetic frame. |
| `tests/test_rtsp_manager.py` | Mocks OpenCV; OK unit coverage for the unused manager. |
| `tests/conftest.py` | Fine. |
| `.github/workflows/ci.yml` 🟠 | `libgl1-mesa-glx` unavailable on Ubuntu 24.04; lint only for syntax errors; no Rust build; no model/behaviour test. |
| `Dockerfile` / `docker-compose.yml` 🔴 | CMD = broken `run_sentinel.py`; healthcheck on :8000 (server is :8765); `libgl1-mesa-glx` (removed in 24.04 but OK on 22.04 base); models not copied (runtime download); config mounted `:ro` but camera config needs writes; no `.dockerignore`. |
| `README.md`, `CLAUDE.md` 🔴 | Claim "PHASE 2 COMPLETE ✅ PRODUCTION READY", Twilio SMS/email, network failover, <2 % false positives, 10+ cameras, 99 % uptime — **none substantiated by code or measurements**. |
| `docs/research_notes.md` 🟠 | Describes sunset +0.05–0.08 compensation and operator 60 s SLA — not implemented. |
| `docs/RTSP_SETUP.md`, `hardware_integration.md`, `requirements.md` | Useful prose; hardware doc inherits the firmware calibration error. |
| `SECURITY.md`, `CONTRIBUTING.md`, `MAINTAINERS.md`, `CHANGELOG.md`, `LICENSE` (Apache-2.0) | Governance docs fine; CHANGELOG lists features not present; license conflicts with AGPL model (§4.1). |
| `test_fire_scenarios.avi` | 30 s synthetic clip (circles + burned-in text); not a valid detection benchmark. |
| `.gitignore` | Doesn't ignore `*.pt`, `data/`, `logs/` consistently (duplicated model files committed). |

### 5.6 Repository provenance 🟠
56 commits; codebase added in one commit (`d4929e1`, 2024-09-28, 128 files). Commit `8bc1d80` added `generate_commits.py`/`generate_2023_commits.py`/`generate_2024_commits.py` ("Generate realistic backdated commits") and `cfa8867` deleted them; message claims "all 193 commits verified" but history has 56. Subsequent feature-titled commits only append padding comments (verified diffs for 13 commits). Padding strings appear in 15 files. **Treat commit messages, CHANGELOG and status claims as unreliable; trust only code and measurements.**

---

## 6. Strengths and weaknesses

### Strengths
- Clear **product framing** (supplementary only), tiered P1/P2/P4 alert concept, YAML-driven thresholds.
- Sensible **process split**: Tauri UI ↔ local HTTP ↔ Python CV engine; local SQLite; offline-capable *in principle*.
- `RTSPManager`, `AppNotificationManager`, `rtsp_advanced` brand table, `validation.js`, and the UI component library are reasonable building blocks.
- The UI is polished, lazy-loaded, has an error boundary and toast system.
- Installer scripts show thought about services, GPU checks and permissions.
- With `rabahdev_best.pt`, a real fire/smoke model is now available and **demonstrably detects fire** (up to ~80 % image-level recall at sensible thresholds).

### Weaknesses
- Detection→alert chain broken in four places (model, class filter, serialization, IPC).
- Two of everything (two alert DBs, two notification managers, two status hooks, two `stop_monitoring`, five model copies, three detection configs) — and the live path uses the weaker one each time.
- Large amounts of unwired or stub code advertised as complete.
- Safety-critical UI defaults to "healthy".
- No authentication, permissive CORS, plaintext credentials.
- No measurement harness: thresholds and metrics are asserted, not derived.
- Fragile paths (cwd-relative everywhere).

---

## 7. Remediation plan (folder by folder, prioritised)

> **Progress:** Phase 0 is complete and most of Phases 1–2 are done on this branch — see §9 (F1–F27). Remaining open items are listed at the end of §9.

### Phase 0 — Make the safety chain real (1–2 days) 🔴
1. **`backend/detection/fire_detector.py`**
   - Load model from an absolute, configured path (`config.detection.model_path`), default `models/fire_smoke.pt` (= `rabahdev_best.pt` once licensing is decided). Never download at runtime; fail loudly if missing.
   - Remove the `person/car/truck` rule and substring matching; accept exact class names from config (`['fire','smoke']`).
   - Cast bbox to `int` (`tuple(int(v) for v in …)`) and confidence to `float`.
   - Re-read thresholds from `ConfigManager` (inject it) on every N frames or on change.
2. **`backend/alerts/alert_manager.py`**: robust JSON (`default=` handler), save **before** returning from `create_alert` or notify even on save failure; per-camera **cooldown/de-dup** (e.g. one alert per camera per level per 60 s, escalate if level rises); add `idx_alerts_timestamp`.
3. **`backend/main.py`**: process each frame **once** (track frame counter per camera); **temporal confirmation** (fire in ≥3 of last 5 frames) before P1; save annotated frame only on new/escalated alert; schedule `cleanup_old_records` + frame retention.
4. **`backend/alerts/app_notification_system.py`**: rename its table (`app_alerts`) or delete the module — one alert store only.
5. **`src-tauri/src/lib.rs`**: add `#[tauri::command(rename_all = "snake_case")]` to every command (or switch the UI to camelCase) — one consistent convention.
6. **`src/hooks/useStableAlertSystem.js`**: use `alert.status`; **`useStableSystemStatus.js`**: start in `'unknown'`/`backendRunning:false`, derive health from real heartbeat (`/api/health` age), show a full-screen red banner when the backend or detector is down.
7. **`src/components/views/SurveillanceGridReal.jsx`**: `hasFrame` → `hasValidFrame`.

### Phase 1 — Real cameras & persistence (2–4 days) 🟠
8. **`backend/utils/video_simulator.py` → replace with `RTSPManager` in `SentinelSystem`** for any `rtsp://`/`http://` source; keep the simulator only for explicit `synthetic`/file sources. Use `cv2.VideoCapture(url, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])`. URL-encode credentials.
9. **`backend/api_server.py`**: run blocking work (`test_rtsp_url`, discovery) via `asyncio.to_thread`; return name/location/real fps/resolution/last_frame_time; make remove delete config even if not running; validate thresholds `0<x<1` and names in an allow-list.
10. **`backend/config/camera_config.py`**: store `resolution` as a list; migrate legacy `id/url` schema; write atomically (`tmp + os.replace`) with mode 0600; replace `CAP_PROP_TIMEOUT`; parallelise or drop the brute-force network scan in favour of ONVIF with XAddrs parsing.
11. **`config/`**: one `detection_config.yaml`, delete `backend/config/` and `src-tauri/config/` copies; resolve all paths from a single `SENTINEL_HOME`.

### Phase 2 — Security & packaging (2–3 days) 🟠
12. API: bind `127.0.0.1`, restrict CORS to `tauri://localhost`/`http://localhost:1420`, add a per-launch bearer token generated by Tauri and passed via env.
13. Credentials: OS keyring (or encrypted file) instead of YAML plaintext.
14. **Tauri**: package the backend as a **sidecar** (PyInstaller) under `bundle.externalBin`, spawn with explicit cwd/args; kill on `RunEvent::Exit`; real health probe; set a CSP.
15. **`run_sentinel.py`**: rewrite as a thin wrapper around `SentinelAPIServer` (or delete); fix Docker CMD/healthcheck port; copy models; add `.dockerignore`; config volume writable.
16. Remove duplicate `.pt` files; track the chosen model with Git LFS + SHA-256 pinned in config; load with `weights_only` safe-globals.

### Phase 3 — Measurement & honesty (ongoing) 🟡
17. **`tests/`**: add a behavioural test set — ≥200 fire/smoke positives **and ≥1 000 hard negatives** (sunsets, red lights, fog, kitchens, people) — and assert recall/false-alarm targets; delete tests that encode bugs (`car@0.95`), fix `test_notifications.py`.
18. Calibrate P1/P2/P4 from that set (current model suggests ~0.70/0.50/0.35 with temporal confirmation; verify on negatives first).
19. Implement or delete: SMS/email (`notification_system.py`), network failover, sunset/fog compensation, adaptive thresholds. Update README/CLAUDE.md/CHANGELOG to match reality; remove "PRODUCTION READY".
20. CI: Ubuntu 24.04 packages (`libgl1`), full flake8, `cargo check`, frontend build, and the behavioural model test (small subset).
21. Remove padding comments, duplicated functions, `core/base_detector.py`, dead hooks.
22. **Arduino**: fix packet size to 25, R0 = Rs/9.83 (MQ-2) and per-datasheet for the gas sensor actually fitted; separate binary and text channels (or framed text protocol with CRC); add `pyserial` to requirements; fix self-join in `reconnect`.
23. Licensing: resolve AGPL-3.0 (Ultralytics + `rabahdev_best.pt`) vs Apache-2.0 before any distribution.

### Acceptance criteria for "supplementary-ready"
- Real fire clip on an RTSP camera → **P1 visible in UI and persisted within 2 s**, one alert (not hundreds), with annotated frame.
- Backend killed → UI shows **red "DETECTION OFFLINE"** within 10 s.
- Restart → cameras and thresholds preserved; threshold change takes effect without restart.
- ≥75 % image recall on positives, measured false-alarm rate on the negative set, both reported in CI.

---

## 8. Operator how-to (after the fixes in §9)

```bash
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
python run_sentinel.py                       # API + detection on http://127.0.0.1:8765 (any cwd)
# desktop app (dev):  npm install && npm run tauri dev
```
Paths resolve from `SENTINEL_HOME` (default: repo root): `config/detection_config.yaml`, `config/cameras.yaml` (0600), `data/alerts.db`, `data/alert_frames/`, `logs/`. Model: `detection.model.path` (default `rabahdev_best.pt`).

```bash
A=http://127.0.0.1:8765/api
curl -XPOST $A/cameras/add -d '{"camera_id":"cam1","rtsp_url":"rtsp://user:pass@192.168.1.20:554/stream1","name":"Kitchen","location":"Bldg A"}'
curl -XPOST $A/cameras/add -d '{"camera_id":"test","rtsp_url":"/path/to/video.mp4"}'   # or "synthetic"
curl $A/cameras                                  # status: active | connecting | inactive, real resolution/fps/last_error
curl $A/cameras/cam1/frame                       # latest frame (base64 JPEG)
curl $A/dashboard                                # alerts + config
curl $A/alerts/<alert_id>/frame                  # annotated alert frame
curl -XPOST $A/acknowledge -d '{"alert_id":"<id>"}'
curl -XPOST $A/threshold -d '{"threshold_name":"immediate_alert","value":0.8}'   # validated, hot-reloads
curl -XDELETE $A/cameras/cam1/remove
curl $A/health                                   # "healthy" only when the detector is actually running
```

---

## 9. Fixes applied on this branch (verified)

All verified with `pytest` (**51 passed**, previously 37 passed / 2 failed), `cargo check` (passes), `npm run build` (passes), and a live end-to-end run launched exactly as the Tauri shell launches it (cwd `src-tauri/`).

| # | Problem (§) | Fix | Before → after (measured) |
|---|---|---|---|
| F1 | COCO model, no fire class (§3) | `detection.model.path` → `rabahdev_best.pt`; never substitutes a COCO model; no runtime downloads | 0/100 → alerts on real fire; kitchen feed escalates P4→P2→P1 |
| F2 | person/car/truck ≥0.9 = "fire"; substring class match (§5.1) | Exact, case-insensitive configured class names (`fire, smoke, flame`) | Person-only frames no longer alert |
| F3 | numpy int64 bbox → **every alert failed to save** (§5.1) | Plain `int`/`float` in detections + JSON `default` handler | 109 created / 0 stored → every alert stored, with frame |
| F4 | Notification only after successful save | Persist synchronously; notify even if persistence fails | — |
| F5 | 2.4 alerts/s/camera, same frame re-detected (§2) | Only *new* frames analysed at `cameras.frame_rate` (2 FPS); **temporal confirmation** (`min_hits` of `window`, default 3/5); per-camera **cooldown** (60 s) with escalation | 109 alerts/45 s → 3 alerts/40 s (P4→P2→P1) |
| F6 | Thresholds never reach detector; any value accepted (§5.1, §2) | `FireDetector` hot-reloads on file change; names allow-listed; values in (0,1) and ordered; atomic write | P1=5.0 accepted → HTTP 400; valid change applied in <3 s |
| F7 | Thresholds 0.95/0.85/0.70 can never fire with a real model; desktop used 0.78/0.60/0.50 from `src-tauri/config` (§4) | One config; calibrated **0.75 / 0.55 / 0.35** (§4.3); divergent copies deleted | Same values in every launch mode |
| F8 | cwd-relative paths everywhere (§5.3) | `backend/paths.py` (`SENTINEL_HOME`); Tauri sets cwd + env | Desktop/CLI/Docker share config, DB, cameras |
| F9 | Cameras lost on restart (`!!python/tuple`); legacy schema unreadable; plaintext 0644 (§5.1) | Lists not tuples, `safe_dump`, legacy `id/url/fps_limit` migration, atomic write, **0600** | 4 cameras lost → all restored incl. names; file `-rw-------` |
| F10 | "RTSP" cameras rendered as synthetic frames (§5.1) | `rtsp://`, `http(s)://` → real `RTSPCamera` (open/read timeouts at construction, URL-encoded credentials, latest-frame only, reconnect) | `hp_webcam` reports `connecting` + real error instead of fake frames |
| F11 | `cv2.CAP_PROP_TIMEOUT` doesn't exist → discovery/test always fail | `CAP_PROP_OPEN_TIMEOUT_MSEC`/`READ_TIMEOUT_MSEC` passed at open | — |
| F12 | Model verify deletes tracked file; `torch.load` full unpickle (§5.1) | `safe_load_checkpoint` (weights_only + torch/ultralytics allow-list); never unlinks local models | No deletion, no download |
| F13 | Two `alerts` tables in one DB (§5.1) | App-notification store renamed `app_alerts` | Schema error gone |
| F14 | CORS `*` + credentials, no validation (§5.1) | CORS limited to Tauri origins (env `SENTINEL_ALLOWED_ORIGINS`), bound to 127.0.0.1, 400/404/503 with reasons | `evil.example` preflight → **403** |
| F15 | Blocking RTSP test/discovery on event loop | `asyncio.to_thread` | — |
| F16 | Camera name/location/resolution/last-frame lost; remove leaves config (§2) | Stored and returned; remove cleans config even if not running | Name "Kitchen", "Bldg A, floor 1", 640x640 shown |
| F17 | `run_sentinel.py`/Docker CMD dead; healthcheck on :8000 (§5.4) | Wrapper around the API server; Docker port/healthcheck 8765, model copied, writable volumes | Starts, restores state |
| F18 | Frame files grow ~0.8 GB/h/camera with no cleanup | Hourly retention cleanup of DB rows + frames (`log_retention_days`) | — |
| F19 | Tauri v2 camelCase vs UI snake_case → add/test/remove/frame/ack broken (§5.3) | `rename_all = "snake_case"` on all 14 commands; add-camera timeout 30 s and real error text | `cargo check` passes |
| F20 | Tauri: trusts any process on :8765; status "running" after crash; orphan Python on exit | Health-probe identity check; `try_wait` + `/api/health`; kill child on `RunEvent::Exit` | — |
| F21 | **UI fail-healthy** (§0 screenshot) | Initial state `unknown`; health from backend probe; critical within 15 s of loss; sidebar/header/threat card honest | "LOW / 99 % / ACTIVE" → "**DETECTION OFFLINE** / UNKNOWN" |
| F22 | Alerts always "active" in UI (§5.2) | Read backend `status` | Acknowledge persists across refresh |
| F23 | "View" crashes grid (`hasFrame`) (§5.2) | `hasValidFrame` | Click works, no page errors |
| F24 | **Request storm** once real data flows: polling effects re-fired every render (unstable `useNotifications()` + state deps) → `ERR_INSUFFICIENT_RESOURCES` → whole UI crashed (found during live test) | Mount-once polling via refs; stable notifications object | 58 failed requests + crash → 0 errors |
| F25 | Tests encoded bugs / used wrong fields (§5.5) | Rewritten + `tests/test_pipeline_regressions.py` (int64 save, cooldown, restart persistence, legacy schema, threshold validation, hot-reload, temporal confirmation, **real fire images must alert**) | 51 passed |
| F26 | CI `libgl1-mesa-glx` unavailable | `libgl1`; network-download tests deselected | — |
| F27 | Padding/dead code (`core/base_detector.py`, 19× duplicate method, double `stop_monitoring`) | Removed | — |

![Fixed UI with backend down](analysis/ui_fixed_backend_down.png)
*After F21: backend down → "DETECTION OFFLINE", threat "UNKNOWN", every subsystem OFFLINE.*

![Fixed UI live](analysis/ui_fixed_live_command_center.png)
*Live: real alerts from the kitchen feed, thresholds from config, honest "SYSTEM WARNING" because 1 of 4 cameras (`hp_webcam`, nothing listening) is not streaming.*

![Cameras](analysis/ui_fixed_live_cameras.png)
![P1 alert frame](analysis/alert_frame_p1_kitchen.jpg)
*Stored, annotated P1 alert frame (fire 0.75). Note the large flame body is not boxed separately — a model recall limit (§4.3).*

**Behaviour to know:** an alert's *level* is the temporally confirmed level (≥ `min_hits` of the last `window` analysed frames), while the shown *confidence* is that frame's peak — so "P2 @ 92 %" is possible when only one or two recent frames exceeded P1. **Known misses:** small flames (e.g. the hand-held flame in the dataset frame used for the "lobby" camera) were not detected by any tested model at these thresholds.

### Still open after round 1 → see §10 for what round 2 closed

---

## 10. Round 2 — completing the product (verified)

| # | Gap | Change | Verification |
|---|---|---|---|
| R1 | Nobody is told about a fire unless they stare at the screen | **Alarm banner**: P1 = red banner + repeating two-tone siren (Web Audio, no assets, offline) + OS notification; P2 = amber banner + chime; *Acknowledge*, *Silence 5 min*, *View* | Live: banner appeared for kitchen P1; acknowledging cleared it (1 → 0), no page errors |
| R2 | "SMS/Email with Twilio" existed only in docs | `backend/alerts/notification_system.py`: **email (SMTP, frame attached), webhook (generic/Slack), Twilio SMS**; per-channel `min_level`; **persistent outbox** with exponential-backoff retry so alerts raised offline are delivered later; secrets from env vars; status + "Send test" in the UI | Live: webhook receiver got the P2 and P1 alerts, P4 filtered; tests incl. real local HTTP server, fake SMTP with attachment, offline→recover |
| R3 | Any local process could drive the API | **Per-launch API token**: Tauri generates 256-bit token, passes it to the backend it spawns, sends it on every request; backend enforces when `SENTINEL_API_TOKEN` is set (`/api/health` open); refuses to adopt a foreign token-protected backend | Live: no/wrong token → 401; tests |
| R4 | "Acknowledge all" only hid alerts in the UI (they returned on next poll) | Real backend acknowledge for each alert; Active / Acknowledged tabs | Live |
| R5 | Evidence frames stored but never viewable | **Evidence viewer** (annotated alert frame) in Incident Management via new `get_alert_frame` command | Live screenshot below |
| R6 | Settings sliders limited to 50–100 % → calibrated P4 = 35 % impossible to show/set; guideline text quoted 95/85/70 | Range 10–99 %, calibrated defaults, live explanation of temporal confirmation/cooldown/model; **Alert delivery panel** | Live screenshot |
| R7 | Analytics showed fields the backend never sent ("N/A"/"CHECKING" forever) and a fabricated latency | Measured values only: alerts per level + avg confidence, frames analysed, confirmed detections, cameras configured; accuracy shown as "needs operator verification data" | Live screenshot |
| R8 | Add-camera dropped name/location/fps; errors swallowed | Form fields + passed through Rust → API; backend reason shown to the user | — |
| R9 | Alerts reached UI only every 20 s when "stable" | 5 s poll (+ Tauri 5 s push) | — |
| R10 | Arduino: 24 vs 25-byte packet → every packet rejected; reconnect self-join crash; clean-air calibration alarmed permanently; ADC 0 divide-by-zero | Packet size from `struct.calcsize`; reconnect in separate thread; `R0 = Rs / clean-air ratio` (MQ-2 9.83, MQ-135 3.6); ADC guard; `pyserial` in requirements | Tests: real packed packets interleaved with firmware text lines; clean air now < alert thresholds (old formula ≥) |
| R11 | Network camera path never exercised live | Live test with a real network stream (ffmpeg HTTP MPEG-TS server; ffmpeg cannot act as an RTSP *server* and the MediaMTX binary was not permitted) through the same `RTSPCamera` code: connect test 640×640, auto-reconnect, outage detected (`connecting`, last frame age shown), **automatic recovery** when the stream returned, P4→P2→P1 alerts + webhook delivery | Live |
| R12 | Stale `last_error` after reconnect | Cleared on successful connect | — |

![Alarm banner](analysis/v2_command_alarm.png)
*P1 alarm banner over live data from a network camera.*

![Evidence](analysis/v2_evidence.png)
![Settings](analysis/v2_settings.png)
![Analytics](analysis/v2_analytics.png)

### Remaining limitations (honest)
- **Detection quality is the ceiling.** Image-level recall 62–79 % at usable thresholds on an independent set; small/hand-held flames missed; single-frame false alarms 5–7 % on everyday scenes (mitigated, not eliminated, by temporal confirmation). Must be measured on the actual site before relying on it.
- No hard-negative fine-tuning yet (needs GPU; recipe in §4.3).
- Installers: the Python backend is not yet packaged as a Tauri sidecar, so `cargo tauri build` produces a UI that expects a Python env next to it; `scripts/install.sh|ps1` still contain unverified steps.
- Not implemented: sunset/fog threshold compensation, network failover (module exists, not wired), ONVIF discovery beyond the basic probe, multi-user roles.
- RTSP *protocol* specifically (vs HTTP stream) not exercised live here; same OpenCV/FFmpeg path.
- AGPL-3.0 model licence (out of scope for testing per owner).

---

## 11. Verdict — is it worth it, deployable, usable?

**Before this work: not usable for its purpose.** The codebase looked complete (polished UI, installers, many modules, long changelog) but no fire could produce a stored, visible or delivered alert, the UI reported "all systems operational" with nothing running, and much of the claimed functionality (SMS/email, failover, adaptive thresholds, training) was stubs or synthetic. The repository history is generated and its commit messages are not evidence of work. Judged against its own README, it was a prototype dressed as a product.

**Is the codebase worth building on? Yes, with caveats.** The architecture (Tauri shell ↔ local HTTP ↔ Python CV engine, SQLite, YAML config) is sound and conventional; most problems were integration bugs, not design flaws, and were fixable in-place. The UI component work is genuinely good. Dead/duplicate modules (`performance_optimizer`, `network_monitor`, `model_trainer`, `rtsp_advanced`) should be either wired in or deleted.

**Is it usable now? Yes — as a supplementary, operator-attended monitoring tool**, on a Linux/Windows PC with Python, for a pilot:
- end-to-end chain works and is tested (detection → confirmation → storage → evidence → siren/banner → email/webhook/SMS → acknowledge);
- it fails loudly (DETECTION OFFLINE, camera `connecting`, delivery errors visible) instead of silently;
- configuration, cameras and alerts survive restarts.

**Is it deployable to production? Not yet, for three reasons:**
1. **Detection performance is unproven on the target site.** Required before go-live: a 2–4 week shadow trial per site logging every alert with operator verdict (true/false), then set thresholds from that data. Expect to fine-tune the model with site negatives.
2. **Packaging.** Bundle the backend (PyInstaller sidecar or a systemd/Windows service using `run_sentinel.py`, which now works) and verify installers on clean machines.
3. **Operational hardening.** A watchdog that alerts when Sentinel itself stops (e.g. external heartbeat to the webhook), log rotation, and a documented operator procedure.

**Recommended next steps (priority):** (1) site shadow trial with an "operator verdict" button on each alert to collect ground truth → real accuracy/false-alarm metrics in Analytics; (2) fine-tune YOLO11s on D-Fire + FASDD + site negatives, select by recall at ≤1 % false alarms; (3) sidecar packaging + heartbeat watchdog; (4) delete or wire the dormant modules.
