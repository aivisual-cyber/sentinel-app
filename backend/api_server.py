#!/usr/bin/env python3
"""
Sentinel Fire Detection API Server
Local HTTP API used by the Tauri desktop shell (http://127.0.0.1:8765).

SUPPLEMENTARY early-warning only - never a replacement for certified fire alarm systems.
"""

import asyncio
import base64
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp_cors
import cv2
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402
from main import SentinelSystem, APIServer  # noqa: E402
from config.camera_config import CameraConfigManager, CameraProfile  # noqa: E402
from utils.system_monitor import system_monitor  # noqa: E402

# Only the desktop app's own origins may call the API from a browser context.
# (Previously "*" + credentials: any web page could change thresholds or delete cameras.)
DEFAULT_ORIGINS = "tauri://localhost,http://tauri.localhost,https://tauri.localhost,http://localhost:1420"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("SENTINEL_ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",") if o.strip()]


API_TOKEN = os.environ.get("SENTINEL_API_TOKEN", "")


@web.middleware
async def auth_middleware(request, handler):
    """Require 'Authorization: Bearer <SENTINEL_API_TOKEN>' when a token is configured.
    The Tauri shell generates a per-launch token and passes it to the backend it spawns.
    /api/health stays open so the shell can identify a running backend."""
    # Only the API is protected; the static UI (served to the local browser) is public
    if (API_TOKEN and request.method != 'OPTIONS' and request.path.startswith('/api/')
            and request.path != '/api/health'):
        import hmac
        supplied = request.headers.get('Authorization', '')
        if not hmac.compare_digest(supplied, f'Bearer {API_TOKEN}'):
            return web.json_response({'success': False, 'error': 'unauthorized'}, status=401)
    return await handler(request)


def _json_error(message: str, status: int) -> web.Response:
    return web.json_response({'success': False, 'error': message}, status=status)


class SentinelAPIServer:
    """HTTP API Server for Tauri communication"""

    def __init__(self, port: int = 8765, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.app = web.Application(middlewares=[auth_middleware])
        self.sentinel_system: Optional[SentinelSystem] = None
        self.api_handler: Optional[APIServer] = None
        self.backend_task: Optional[asyncio.Task] = None
        self.backend_error: Optional[str] = None
        self.camera_config = CameraConfigManager()
        self.logger = logging.getLogger(__name__)
        self.is_running = False

        self.cors = aiohttp_cors.setup(self.app, defaults={
            origin: aiohttp_cors.ResourceOptions(allow_credentials=False, expose_headers="*",
                                                 allow_headers=("Content-Type", "Authorization"),
                                                 allow_methods=("GET", "POST", "DELETE"))
            for origin in ALLOWED_ORIGINS
        })
        self._setup_routes()

    def _setup_routes(self):
        r = self.app.router
        r.add_get('/api/health', self.health_check)
        r.add_get('/api/dashboard', self.get_dashboard_data)
        r.add_post('/api/threshold', self.update_threshold)
        r.add_post('/api/acknowledge', self.acknowledge_alert)
        r.add_get('/api/alerts/{alert_id}/frame', self.get_alert_frame)
        r.add_post('/api/alerts/{alert_id}/verdict', self.set_alert_verdict)
        r.add_get('/api/report', self.get_trial_report)
        r.add_get('/api/cameras', self.get_camera_feeds)
        r.add_get('/api/cameras/{camera_id}/frame', self.get_camera_frame)
        r.add_post('/api/cameras/discover', self.discover_cameras)
        r.add_post('/api/cameras/add', self.add_camera)
        r.add_post('/api/cameras/{camera_id}/test', self.test_camera)
        r.add_delete('/api/cameras/{camera_id}/remove', self.remove_camera)
        r.add_get('/api/status', self.get_system_status)
        r.add_get('/api/metrics', self.get_system_metrics)
        r.add_get('/api/notifications', self.get_notifications)
        r.add_post('/api/notifications/test', self.test_notification)
        for route in list(self.app.router.routes()):
            self.cors.add(route)
        self._setup_ui()

    def _setup_ui(self):
        """Serve the built web UI (browser mode: Windows deployments, no Tauri/WebView2 needed)."""
        candidates = [os.environ.get('SENTINEL_UI_DIR'), paths.SENTINEL_HOME / 'ui',
                      Path(__file__).resolve().parent.parent / 'ui', Path(__file__).resolve().parent.parent / 'dist']
        ui = next((Path(c) for c in candidates if c and (Path(c) / 'index.html').is_file()), None)
        if not ui:
            self.logger.info("No built UI found (dist/ or ui/); API only")
            return
        index = ui / 'index.html'

        async def index_handler(request):
            return web.FileResponse(index, headers={'Cache-Control': 'no-cache'})

        self.app.router.add_get('/', index_handler)
        if (ui / 'assets').is_dir():
            self.app.router.add_static('/assets', ui / 'assets')
        self.logger.info(f"Web UI served from {ui} at http://{self.host}:{self.port}/")

    # ----------------------------------------------------------- helpers
    @property
    def processor(self):
        s = self.sentinel_system
        return s.stream_processor if s else None

    def _detection_running(self) -> bool:
        s = self.sentinel_system
        return bool(s and s.is_running and s.fire_detector is not None and self.processor
                    and self.processor.is_processing)

    def _require_backend(self) -> Optional[web.Response]:
        if not self._detection_running():
            return _json_error(f"Detection system not running: {self.backend_error or 'starting'}", 503)
        return None

    # ----------------------------------------------------------- routes
    async def health_check(self, request) -> web.Response:
        s = self.sentinel_system
        health = s.get_health() if s else {'state': 'critical', 'issues': ['starting']}
        if self.backend_error:
            health = {'state': 'critical', 'issues': [f'backend failed: {self.backend_error}']}
        return web.json_response({
            'status': health['state'],          # healthy | degraded | critical
            'issues': health['issues'],
            'timestamp': time.time(),
            # True only while frames are actually being analysed (not merely "process alive")
            'backend_running': self._detection_running() and health['state'] != 'critical',
            'detector_loaded': bool(s and s.fire_detector),
            'error': self.backend_error or (s.last_detection_error if s else None),
            'auth_required': bool(API_TOKEN),
            'inference': health.get('inference'),
        })

    async def get_dashboard_data(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        try:
            return web.json_response(await asyncio.to_thread(self.api_handler.get_dashboard_data))
        except Exception as e:
            self.logger.error(f"Error getting dashboard data: {e}")
            return _json_error(str(e), 500)

    async def update_threshold(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        try:
            data = await request.json()
            name, value = data.get('threshold_name'), data.get('value')
            if not name or value is None:
                return _json_error('Missing threshold_name or value', 400)
            ok = self.api_handler.update_threshold(str(name), float(value))
            if not ok:
                return _json_error('Rejected: threshold must be one of immediate_alert/review_queue/log_only, '
                                   'in (0, 1), and ordered log_only <= review_queue <= immediate_alert', 400)
            return web.json_response({'success': True})
        except (ValueError, TypeError) as e:
            return _json_error(f'Invalid value: {e}', 400)

    async def acknowledge_alert(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        data = await request.json()
        alert_id = data.get('alert_id')
        if not alert_id:
            return _json_error('Missing alert_id', 400)
        ok = await asyncio.to_thread(self.api_handler.acknowledge_alert, str(alert_id))
        return web.json_response({'success': ok}, status=200 if ok else 404)

    async def get_alert_frame(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        alert_id = request.match_info['alert_id']
        import sqlite3
        db = self.sentinel_system.alert_manager.database.db_path
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT frame_path, timestamp, camera_id, confidence, detections FROM alerts "
                               "WHERE id = ?", (alert_id,)).fetchone()
        if not row:
            return _json_error(f'Alert {alert_id} not found', 404)
        frame_path = Path(row[0]).resolve() if row[0] else None
        frames_dir = paths.ALERT_FRAMES_DIR.resolve()
        if not frame_path or frames_dir not in frame_path.parents or not frame_path.exists():
            return _json_error('No frame available for this alert', 404)
        return web.json_response({
            'alert_id': alert_id, 'frame': base64.b64encode(frame_path.read_bytes()).decode(),
            'timestamp': row[1], 'camera_id': row[2], 'confidence': row[3], 'detections': row[4], 'format': 'jpeg'})

    async def set_alert_verdict(self, request) -> web.Response:
        """Operator verdict: fire | false_alarm | test (also acknowledges the alert)."""
        if (err := self._require_backend()):
            return err
        data = await request.json()
        try:
            result = await asyncio.to_thread(self.sentinel_system.alert_manager.set_verdict,
                                             request.match_info['alert_id'], str(data.get('verdict', '')),
                                             str(data.get('notes', ''))[:500])
        except KeyError:
            return _json_error('Alert not found', 404)
        except ValueError as e:
            return _json_error(str(e), 400)
        return web.json_response({'success': True, **result})

    async def get_trial_report(self, request) -> web.Response:
        """Site-trial metrics from operator verdicts. ?days=7&format=csv for the raw alert log."""
        if (err := self._require_backend()):
            return err
        try:
            days = min(max(float(request.query.get('days', 7)), 0.04), 365)
        except ValueError:
            return _json_error('days must be a number', 400)
        am = self.sentinel_system.alert_manager
        if request.query.get('format') == 'csv':
            text = await asyncio.to_thread(am.trial_csv, days)
            return web.Response(text=text, content_type='text/csv',
                                headers={'Content-Disposition': f'attachment; filename="sentinel_trial_{int(days)}d.csv"'})
        return web.json_response(await asyncio.to_thread(am.trial_report, days))

    async def get_camera_feeds(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        status = self.processor.get_camera_status()
        cameras = []
        for cid, st in status.items():
            state = 'active' if st['connected'] else ('connecting' if st['running'] else 'inactive')
            cameras.append({'id': cid, 'name': st['name'], 'location': st['location'], 'status': state,
                            'last_frame_time': st['last_frame_time'], 'fps': st['fps'],
                            'resolution': st['resolution'] or 'unknown', 'kind': st['kind'],
                            'last_error': st['last_error']})
        return web.json_response({'cameras': cameras, 'total_cameras': len(cameras),
                                  'active_cameras': sum(c['status'] == 'active' for c in cameras)})

    async def get_system_status(self, request) -> web.Response:
        s = self.sentinel_system
        status = {'backend_running': self._detection_running(), 'api_server_running': True,
                  'timestamp': time.time(), 'error': self.backend_error}
        if s:
            st = s.get_status()
            status.update(st)
            status['uptime'] = time.time() - s.start_time
        return web.json_response(status)

    async def get_camera_frame(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        cam = self.processor.simulator.get(request.match_info['camera_id'])
        if cam is None:
            return _json_error('Camera not found', 404)
        frame = cam.get_frame()
        if frame is None:
            return _json_error('No frame received yet', 404)
        ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return web.json_response({'camera_id': cam.camera_id, 'frame': base64.b64encode(buf).decode(),
                                  'timestamp': cam.last_frame_time, 'format': 'jpeg'})

    async def discover_cameras(self, request) -> web.Response:
        from detection.rtsp_manager import ONVIFDiscovery
        data = await request.json() if request.can_read_body else {}
        timeout = min(max(int(data.get('timeout', 5)), 1), 30)
        cams = await asyncio.to_thread(ONVIFDiscovery.discover_cameras, timeout)
        return web.json_response({'cameras': cams, 'count': len(cams), 'timestamp': time.time()})

    @staticmethod
    def _test_source(url: str, username=None, password=None):
        if url in ('synthetic', 'test'):
            return True, 'Synthetic test source'
        if not url.lower().startswith(('rtsp://', 'rtsps://', 'http://', 'https://')):
            return (True, 'Local file') if Path(url).exists() else (False, f'File not found: {url}')
        from detection.rtsp_manager import RTSPManager
        return RTSPManager().test_rtsp_url(url, username, password)

    async def add_camera(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        data = await request.json()
        for field in ('camera_id', 'rtsp_url'):
            if not data.get(field):
                return _json_error(f'Missing required field: {field}', 400)
        camera_id, url = str(data['camera_id']).strip(), str(data['rtsp_url']).strip()
        if self.processor.simulator.get(camera_id) or camera_id in self.camera_config.cameras:
            return _json_error(f'Camera {camera_id} already exists', 400)

        # Connection test runs in a worker thread: it can take up to the open timeout
        ok, message = await asyncio.to_thread(self._test_source, url, data.get('username'), data.get('password'))
        if not ok:
            return _json_error(f'Camera test failed: {message}', 400)

        profile = CameraProfile(camera_id=camera_id, name=data.get('name') or f'Camera {camera_id}',
                                rtsp_url=url, username=data.get('username') or None,
                                password=data.get('password') or None, fps=int(data.get('fps', 15)),
                                enabled=bool(data.get('enabled', True)), location=data.get('location', 'Unknown'))
        if not self.camera_config.add_camera(profile):
            return _json_error('Failed to save camera configuration', 500)
        await asyncio.to_thread(self._start_camera, profile)
        return web.json_response({'success': True, 'camera_id': camera_id, 'message': f'Camera {camera_id} added ({message})'})

    def _start_camera(self, profile: CameraProfile):
        cam = self.processor.simulator.add_camera(profile.camera_id, profile.rtsp_url, fps=profile.fps,
                                                  username=profile.username, password=profile.password,
                                                  name=profile.name, location=profile.location)
        cam.start()

    async def test_camera(self, request) -> web.Response:
        data = await request.json()
        url = data.get('rtsp_url')
        if not url:
            return _json_error('Missing rtsp_url', 400)
        ok, message = await asyncio.to_thread(self._test_source, url, data.get('username'), data.get('password'))
        return web.json_response({'camera_id': request.match_info['camera_id'], 'success': ok,
                                  'message': message, 'timestamp': time.time()})

    async def remove_camera(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        camera_id = request.match_info['camera_id']
        removed_live = await asyncio.to_thread(self.processor.simulator.remove_camera, camera_id)
        removed_cfg = self.camera_config.remove_camera(camera_id)
        self.sentinel_system.confirmer.forget(camera_id)
        if not (removed_live or removed_cfg):
            return _json_error(f'Camera {camera_id} not found', 404)
        return web.json_response({'success': True, 'camera_id': camera_id,
                                  'message': f'Camera {camera_id} removed', 'timestamp': time.time()})

    async def get_system_metrics(self, request) -> web.Response:
        metrics = await asyncio.to_thread(system_monitor.get_system_metrics)
        if self.sentinel_system:
            metrics['detection_performance'] = system_monitor.get_detection_performance_metrics(
                frames_processed=self.sentinel_system.frames_processed,
                detections_made=self.sentinel_system.detections_made)
        return web.json_response(metrics)

    async def get_notifications(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        notifier = self.sentinel_system.alert_manager.notifier
        notifier.reload()
        return web.json_response({'channels': notifier.get_status()})

    async def test_notification(self, request) -> web.Response:
        if (err := self._require_backend()):
            return err
        data = await request.json()
        notifier = self.sentinel_system.alert_manager.notifier
        notifier.reload()
        ok, message = await asyncio.to_thread(notifier.send_test, str(data.get('channel', '')))
        return web.json_response({'success': ok, 'message': message}, status=200 if ok else 502)

    # ----------------------------------------------------------- lifecycle
    async def start_backend_system(self):
        self.sentinel_system = SentinelSystem()
        self.api_handler = APIServer(self.sentinel_system)
        self.backend_task = asyncio.create_task(self.sentinel_system.start(install_signals=False))

        def _done(task: asyncio.Task):
            if not task.cancelled() and task.exception():
                self.backend_error = str(task.exception())
                self.logger.critical(f"DETECTION BACKEND FAILED: {self.backend_error}")
        self.backend_task.add_done_callback(_done)

        for _ in range(100):  # wait up to ~20 s for the detector/model to load
            if self._detection_running() or self.backend_task.done():
                break
            await asyncio.sleep(0.2)
        if self._detection_running():
            await self._load_cameras_from_config()

    async def _load_cameras_from_config(self):
        for camera_id, profile in self.camera_config.get_enabled_cameras().items():
            try:
                await asyncio.to_thread(self._start_camera, profile)
                self.logger.info(f"Camera {camera_id} loaded from configuration")
            except Exception as e:
                self.logger.error(f"Failed to load camera {camera_id}: {e}")

    async def start_server(self):
        await self.start_backend_system()
        runner = web.AppRunner(self.app)
        await runner.setup()
        await web.TCPSite(runner, self.host, self.port).start()
        self.is_running = True
        print(f"API Server running on http://{self.host}:{self.port}", flush=True)
        try:
            while self.is_running:
                await asyncio.sleep(1)
        finally:
            if self.sentinel_system:
                await self.sentinel_system.stop()
            await runner.cleanup()

    def stop_server(self):
        self.is_running = False


async def main():
    print("🔥 Starting Sentinel API Server")
    print("⚠️  SUPPLEMENTARY FIRE DETECTION ONLY - NOT A REPLACEMENT FOR CERTIFIED SYSTEMS")
    paths.ensure_dirs()
    # Default: localhost only. SENTINEL_HOST=0.0.0.0 exposes the token-protected API/UI to the LAN.
    server = SentinelAPIServer(port=int(os.environ.get("SENTINEL_PORT", 8765)),
                               host=os.environ.get("SENTINEL_HOST", "127.0.0.1"))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, server.stop_server)
        except NotImplementedError:  # Windows
            pass
    await server.start_server()
    print("API Server stopped")


if __name__ == "__main__":
    asyncio.run(main())
