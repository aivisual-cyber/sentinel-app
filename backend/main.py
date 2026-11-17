#!/usr/bin/env python3
"""
Sentinel Fire Detection System - Main Application
Coordinates detection engine, camera streams, and alert management.

SUPPLEMENTARY early-warning only - never a replacement for certified fire alarm systems.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402
from detection.fire_detector import FireDetector, DetectionResult  # noqa: E402
from config.config_manager import config_manager  # noqa: E402
from alerts.alert_manager import get_alert_manager, LEVEL_RANK  # noqa: E402
from utils.video_simulator import StreamProcessor  # noqa: E402

RANK_LEVEL = {v: k for k, v in LEVEL_RANK.items()}


class TemporalConfirmer:
    """A level is confirmed only if >= min_hits of the last `window` analysed frames reached it.

    Single-frame detectors produce sporadic false positives (measured 5-12 % of everyday
    images at usable thresholds); requiring persistence across frames suppresses most of
    them at a cost of (min_hits / process_fps) seconds of latency.
    """

    def __init__(self, window: int = 5, min_hits: int = 3):
        self.window, self.min_hits = max(1, window), max(1, min(min_hits, window))
        self._hist: Dict[str, deque] = {}

    def update(self, camera_id: str, level: str) -> str:
        h = self._hist.setdefault(camera_id, deque(maxlen=self.window))
        if h.maxlen != self.window:
            h = self._hist[camera_id] = deque(h, maxlen=self.window)
        h.append(LEVEL_RANK.get(level, 0))
        for rank in (3, 2, 1):
            if sum(r >= rank for r in h) >= self.min_hits:
                return RANK_LEVEL[rank]
        return 'None'

    def forget(self, camera_id: str) -> None:
        self._hist.pop(camera_id, None)


def setup_logging() -> logging.Logger:
    from logging.handlers import RotatingFileHandler
    paths.LOG_DIR.mkdir(parents=True, exist_ok=True)  # must exist before the FileHandler
    root = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            # 24/7 service: bounded log size (10 MB x 5)
            handlers=[logging.StreamHandler(),
                      RotatingFileHandler(paths.LOG_DIR / 'sentinel.log', maxBytes=10 * 1024 * 1024, backupCount=5)],
            force=True,
        )
    return logging.getLogger(__name__)


class SentinelSystem:
    """Main Sentinel Fire Detection System"""

    def __init__(self):
        paths.ensure_dirs()
        self.logger = setup_logging()
        self.config_manager = config_manager
        alert_cfg = self.config_manager.get_alert_config()
        self.alert_manager = get_alert_manager()
        self.alert_manager.cooldown_seconds = alert_cfg.cooldown_seconds
        self.fire_detector = None
        self.stream_processor = None
        self.confirmer = None
        self.is_running = False
        self.start_time = time.time()
        self.frames_processed = 0
        self.detections_made = 0
        self.last_detection_error = None
        self.last_heartbeat_ok = None
        self.logger.info("Sentinel System initialized")

    async def start(self, install_signals: bool = True):
        try:
            self.logger.info("Starting Sentinel Fire Detection System")
            self._initialize_components()
            if install_signals:
                try:
                    signal.signal(signal.SIGINT, self._signal_handler)
                    signal.signal(signal.SIGTERM, self._signal_handler)
                except ValueError:
                    pass  # not in main thread
            self.is_running = True
            await self._main_loop()
        except Exception as e:
            self.logger.critical(f"Failed to start Sentinel system: {e}")
            self.is_running = False
            raise

    def _initialize_components(self):
        det = self.config_manager.get_detection_config()
        system = self.config_manager.get_system_config()
        self.logger.info(f"Detection thresholds: P1={det.immediate_alert}, P2={det.review_queue}, "
                         f"P4={det.log_only}; temporal {det.temporal_min_hits}/{det.temporal_window}")

        self.fire_detector = FireDetector(str(self.config_manager.config_path))
        self.confirmer = TemporalConfirmer(det.temporal_window, det.temporal_min_hits)
        self.stream_processor = StreamProcessor(self._detection_callback, process_fps=system.frame_rate)
        self.stream_processor.start_processing()
        self.logger.info(f"Camera stream processing started at {system.frame_rate} FPS per camera")

    def _detection_callback(self, frames: Dict[str, Any]):
        ids = list(frames)
        try:
            # One batched inference for all cameras with a new frame this cycle
            results = self.fire_detector.detect_batch([frames[c] for c in ids])
        except Exception as e:
            self.last_detection_error = str(e)
            self.logger.error(f"Batch detection failed: {e}")
            return
        for camera_id, result in zip(ids, results):
            frame = frames[camera_id]
            try:
                self.frames_processed += 1
                det = self.config_manager.get_detection_config()
                self.confirmer.window, self.confirmer.min_hits = det.temporal_window, det.temporal_min_hits
                confirmed = self.confirmer.update(camera_id, result.alert_level)
                if confirmed == 'None':
                    continue
                self.detections_made += 1
                result.alert_level = confirmed
                alert = self.alert_manager.create_alert(camera_id, result)
                if alert:  # None when suppressed by the per-camera cooldown
                    frame_path = self.fire_detector.save_alert_frame(frame, result.detections, alert.id)
                    raw_path = self.fire_detector.save_raw_frame(frame, alert.id)
                    self.alert_manager.attach_frame(alert, frame_path, raw_path)
                    self.logger.warning(f"Alert {alert.id} ({alert.alert_level.value}) camera={camera_id} "
                                        f"confidence={alert.confidence:.2f}")
                self.last_detection_error = None
            except Exception as e:
                self.last_detection_error = str(e)
                self.logger.error(f"Detection error for camera {camera_id}: {e}")

    async def _main_loop(self):
        self.logger.info("Sentinel system running - monitoring cameras for fire detection")
        last_stats = time.time()
        last_cleanup = 0.0
        last_heartbeat = 0.0
        while self.is_running:
            try:
                now = time.time()
                if now - last_stats >= 60:
                    self._log_system_stats()
                    last_stats = now
                if now - last_cleanup >= 3600:
                    retention = self.config_manager.get_system_config().log_retention_days
                    self.alert_manager.database.cleanup_old_records(retention, frames_dir=paths.ALERT_FRAMES_DIR)
                    last_cleanup = now
                hb_every = float((self.alert_manager.notifier.config.get('heartbeat') or {}).get('interval_seconds', 60))
                if now - last_heartbeat >= hb_every:
                    await self._send_heartbeat()
                    last_heartbeat = now
                if self.config_manager.reload_config():
                    self.logger.info("Configuration reloaded")
                await asyncio.sleep(1)
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(5)

    def _log_system_stats(self):
        try:
            status = self.stream_processor.get_camera_status()
            connected = sum(1 for c in status.values() if c['connected'])
            dashboard = self.alert_manager.get_dashboard_data(len(status))
            self.logger.info(f"System Stats - Cameras: {connected}/{len(status)}, "
                             f"Active Alerts: {dashboard['system_status']['active_alerts']}, "
                             f"Frames analysed: {self.frames_processed}")
            self.alert_manager.database.log_system_event("INFO", "System health check", "SentinelSystem", {
                'connected_cameras': connected, 'total_cameras': len(status),
                'frames_processed': self.frames_processed, 'uptime_seconds': time.time() - self.start_time})
        except Exception as e:
            self.logger.error(f"Failed to log system stats: {e}")

    STALL_SECONDS = 15.0

    def get_health(self) -> Dict[str, Any]:
        """Self-assessment for /api/health, the UI and the external watchdog.

        critical: detection is not protecting anything (not running / loop stalled / frames not analysed)
        degraded: partially protecting (a camera down or frozen, alert delivery failing, no cameras)
        """
        now = time.time()
        critical, issues = [], []
        sp = self.stream_processor
        if not (self.is_running and self.fire_detector and sp and sp.is_processing):
            critical.append('detection engine not running')
            return {'state': 'critical', 'issues': critical, 'checked_at': now}
        if now - sp.last_loop_time > self.STALL_SECONDS:
            critical.append(f'detection loop stalled for {now - sp.last_loop_time:.0f}s')
        cams = sp.get_camera_status()
        live = [c for c in cams.values() if c['connected'] and now - (c['last_frame_time'] or 0) < self.STALL_SECONDS]
        if not cams:
            issues.append('no cameras configured')
        for cid, c in cams.items():
            if not c['connected']:
                issues.append(f"camera {cid} not streaming" + (f" ({c['last_error']})" if c['last_error'] else ''))
            elif now - (c['last_frame_time'] or 0) > self.STALL_SECONDS:
                issues.append(f"camera {cid} frozen (no new frame for {now - c['last_frame_time']:.0f}s)")
        stall = max(self.STALL_SECONDS, 5 * sp.process_interval)
        if live and sp.last_detection_time and now - sp.last_detection_time > stall:
            critical.append(f'no frames analysed for {now - sp.last_detection_time:.0f}s')
        if cams and not live:
            critical.append('no camera is streaming')
        cycle = getattr(sp, 'cycle_seconds', 0.0)
        if live and cycle > 1.2 * sp.process_interval:
            issues.append(f"detection overloaded: ~{1 / cycle:.1f} analysed FPS per camera "
                          f"(target {1 / sp.process_interval:.1f}); add a GPU or reduce cameras")
        for name, ch in self.alert_manager.notifier.get_status().items():
            if ch['enabled'] and ch['last_error'] and ch['pending']:
                issues.append(f"{name} delivery failing ({ch['pending']} pending): {ch['last_error'][:80]}")
        state = 'critical' if critical else ('degraded' if issues else 'healthy')
        return {'state': state, 'issues': critical + issues, 'checked_at': now,
                'inference': getattr(self.fire_detector, 'inference_info', None),
                'last_detection_age': round(now - sp.last_detection_time, 1) if sp.last_detection_time else None,
                'cameras_streaming': len(live), 'cameras_total': len(cams)}

    async def _send_heartbeat(self):
        """Dead-man's switch: ping an external monitor (healthchecks.io, Uptime Kuma push, ...) only while
        detection is actually working. If Sentinel or the whole PC dies, the monitor alerts someone."""
        hb = self.alert_manager.notifier.config.get('heartbeat') or {}
        url = hb.get('url') or os.environ.get(hb.get('url_env', ''), '')
        if not (hb.get('enabled') and url):
            return
        if self.get_health()['state'] == 'critical':
            return  # stay silent so the external monitor raises the alarm
        try:
            import requests
            await asyncio.to_thread(requests.get, url, timeout=10)
            self.last_heartbeat_ok = time.time()
        except Exception as e:
            self.logger.warning(f"Heartbeat ping failed: {e}")

    def get_status(self) -> Dict[str, Any]:
        cams = self.stream_processor.get_camera_status() if self.stream_processor else {}
        return {
            'running': self.is_running,
            'detector_loaded': self.fire_detector is not None,
            'inference': self.fire_detector.inference_info if self.fire_detector else None,
            'model': getattr(getattr(self.fire_detector, 'model', None), 'ckpt_path', None),
            'active_cameras': sum(1 for c in cams.values() if c['connected']),
            'total_cameras': len(cams),
            'uptime': (time.time() - self.start_time) / 3600,
            'frames_processed': self.frames_processed,
            'detections_made': self.detections_made,
            'last_detection_error': self.last_detection_error,
        }

    def _signal_handler(self, signum, frame):
        self.logger.info(f"Received signal {signum}, initiating shutdown...")
        self.is_running = False

    async def stop(self):
        self.logger.info("Stopping Sentinel Fire Detection System")
        self.is_running = False
        if self.stream_processor:
            self.stream_processor.stop_processing()
        if self.alert_manager:
            self.alert_manager.stop()
        self.logger.info("Sentinel system stopped")


class APIServer:
    """Thin adapter used by api_server.py"""

    def __init__(self, sentinel_system: SentinelSystem):
        self.sentinel = sentinel_system
        self.logger = logging.getLogger(__name__)

    def get_dashboard_data(self) -> Dict[str, Any]:
        camera_count = len(self.sentinel.stream_processor.get_camera_status()) if self.sentinel.stream_processor else 0
        return {
            'alerts': self.sentinel.alert_manager.get_dashboard_data(camera_count),
            'config': self.sentinel.config_manager.get_config_summary(),
            'timestamp': time.time(),
        }

    def update_threshold(self, threshold_name: str, value: float) -> bool:
        return self.sentinel.config_manager.update_threshold(threshold_name, value)

    def acknowledge_alert(self, alert_id: str) -> bool:
        return self.sentinel.alert_manager.acknowledge_alert(alert_id)


async def main():
    print("🔥 Starting Sentinel Fire Detection System (headless)")
    print("⚠️  SUPPLEMENTARY FIRE DETECTION ONLY - NOT A REPLACEMENT FOR CERTIFIED SYSTEMS")
    sentinel = SentinelSystem()
    try:
        await sentinel.start()
    except KeyboardInterrupt:
        pass
    finally:
        await sentinel.stop()


if __name__ == "__main__":
    asyncio.run(main())
