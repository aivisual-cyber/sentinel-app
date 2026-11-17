"""
Camera stream layer.

Historically every camera - including rtsp:// URLs - ran through a *simulator* that
silently produced synthetic blue frames when the source wasn't a local file, and the
processing loop re-detected the same frame every 100 ms. Now:

  * rtsp:// / rtsps:// / http(s):// sources use the real ``RTSPCamera`` (timeouts,
    reconnect with retry, credential handling).
  * local video/image files and ``synthetic`` remain available for testing.
  * ``StreamProcessor`` only hands *new* frames to the detector, rate-limited per camera.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import cv2
import numpy as np

try:
    from ..detection.rtsp_manager import RTSPCamera, CameraConfig
except ImportError:
    from detection.rtsp_manager import RTSPCamera, CameraConfig

NETWORK_SCHEMES = ('rtsp://', 'rtsps://', 'http://', 'https://')


class CameraSimulator:
    """Local video/image file or synthetic test pattern."""

    def __init__(self, camera_id: str, video_source: str, fps: int = 30, name: str = None, location: str = None):
        self.camera_id = camera_id
        self.video_source = video_source
        self.fps = max(1, int(fps))
        self.frame_interval = 1.0 / self.fps
        self.name = name or f"Camera {camera_id}"
        self.location = location or "Unknown"
        self.is_running = False
        self.current_frame = None
        self.frame_count = 0
        self.last_frame_time = 0.0
        self.last_error: Optional[str] = None
        self.kind = 'file' if Path(str(video_source)).exists() else 'synthetic'
        self.logger = logging.getLogger(f"Camera-{camera_id}")
        self.thread = None

    @property
    def connected(self) -> bool:
        return self.is_running and self.current_frame is not None

    @property
    def resolution(self) -> Optional[str]:
        if self.current_frame is None:
            return None
        h, w = self.current_frame.shape[:2]
        return f"{w}x{h}"

    def start(self):
        self.is_running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=2)

    def get_frame(self) -> Optional[np.ndarray]:
        return self.current_frame

    def _publish(self, frame):
        self.current_frame = frame
        self.frame_count += 1
        self.last_frame_time = time.time()

    def _run(self):
        if self.kind == 'file':
            self._from_file()
        else:
            self._synthetic()

    def _from_file(self):
        cap = cv2.VideoCapture(str(self.video_source))
        if not cap.isOpened():
            self.last_error = f"Could not open {self.video_source}"
            self.logger.error(self.last_error)
            return
        still = None
        while self.is_running:
            ret, frame = cap.read()
            if ret:
                self._publish(frame)
                still = frame
            elif still is not None and cap.get(cv2.CAP_PROP_FRAME_COUNT) <= 1:
                self._publish(still)  # single image: keep serving it
            else:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop the video
                if not cap.read()[0]:
                    cap.release()
                    cap = cv2.VideoCapture(str(self.video_source))
                else:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            time.sleep(self.frame_interval)  # also prevents a busy loop on read failure
        cap.release()

    def _synthetic(self):
        width, height = 640, 480
        while self.is_running:
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            frame[:, :, 0] = self.frame_count % 255
            frame[:, :, 1] = 50
            frame[:, :, 2] = 100
            cv2.putText(frame, f"Camera {self.camera_id} (synthetic)", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            cv2.putText(frame, time.strftime("%H:%M:%S"), (10, height - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1)
            self._publish(frame)
            time.sleep(self.frame_interval)


class NetworkCamera:
    """Adapter giving ``RTSPCamera`` the same interface as ``CameraSimulator``."""

    kind = 'network'

    def __init__(self, camera_id: str, url: str, fps: int = 15, username: str = None, password: str = None,
                 name: str = None, location: str = None, timeout: int = 10, retry_interval: int = 10):
        self.camera_id = camera_id
        self.video_source = url
        self.fps = fps
        self.name = name or f"Camera {camera_id}"
        self.location = location or "Unknown"
        self._cam = RTSPCamera(CameraConfig(camera_id=camera_id, rtsp_url=url, username=username,
                                            password=password, fps=fps, timeout=timeout,
                                            retry_interval=retry_interval))

    is_running = property(lambda self: self._cam.is_running)
    connected = property(lambda self: self._cam.status.connected)
    frame_count = property(lambda self: self._cam.status.frame_count)
    last_frame_time = property(lambda self: self._cam.status.last_frame_time)
    last_error = property(lambda self: self._cam.status.last_error)

    @property
    def resolution(self) -> Optional[str]:
        r = self._cam.status.resolution
        return f"{r[0]}x{r[1]}" if r else None

    @property
    def actual_fps(self) -> float:
        return round(self._cam.status.actual_fps, 1)

    def start(self):
        self._cam.start()  # returns after ~2 s; reconnects in background if it failed

    def stop(self):
        self._cam.stop()

    def get_frame(self) -> Optional[np.ndarray]:
        return self._cam.get_frame()


class MultiCameraSimulator:
    """Camera registry (name kept for backward compatibility)."""

    def __init__(self):
        self.cameras: List = []
        self._lock = threading.Lock()
        self.logger = logging.getLogger(__name__)

    def add_camera(self, camera_id: str, video_source: str, fps: int = 30, username: str = None,
                   password: str = None, name: str = None, location: str = None):
        if str(video_source).lower().startswith(NETWORK_SCHEMES):
            camera = NetworkCamera(camera_id, video_source, fps=fps, username=username, password=password,
                                   name=name, location=location)
        else:
            camera = CameraSimulator(camera_id, video_source, fps, name=name, location=location)
        with self._lock:
            self.cameras.append(camera)
        return camera

    def remove_camera(self, camera_id: str) -> bool:
        with self._lock:
            found = [c for c in self.cameras if c.camera_id == camera_id]
            for c in found:
                self.cameras.remove(c)
        for c in found:
            c.stop()
        return bool(found)

    def get(self, camera_id: str):
        return next((c for c in list(self.cameras) if c.camera_id == camera_id), None)

    def start_all(self):
        for camera in list(self.cameras):
            camera.start()

    def stop_all(self):
        for camera in list(self.cameras):
            camera.stop()

    def get_camera_frames(self) -> dict:
        frames = {}
        for camera in list(self.cameras):
            frame = camera.get_frame()
            if frame is not None:
                frames[camera.camera_id] = frame
        return frames

    def create_default_setup(self) -> None:
        """No default cameras - add via configuration or the API."""


class StreamProcessor:
    """Feeds NEW frames from every camera to the detection callback, rate-limited per camera."""

    def __init__(self, detection_callback: Optional[Callable] = None, process_fps: float = 2.0):
        self.simulator = MultiCameraSimulator()
        self.detection_callback = detection_callback
        self.process_interval = 1.0 / max(0.1, float(process_fps))
        self.is_processing = False
        self.process_thread = None
        self._last_seen: Dict[str, int] = {}
        self._last_processed: Dict[str, float] = {}
        self.frames_processed = 0
        self.last_loop_time = time.time()       # heartbeat of the processing thread
        self.last_detection_time = 0.0          # when a frame was last analysed
        self.cycle_seconds = 0.0                # EMA of one detection cycle (all cameras)
        self.logger = logging.getLogger(__name__)

    def setup_test_cameras(self):
        self.simulator.create_default_setup()

    def start_processing(self):
        self.simulator.start_all()
        self.is_processing = True
        self.process_thread = threading.Thread(target=self._process_loop, daemon=True)
        self.process_thread.start()

    def stop_processing(self):
        self.is_processing = False
        self.simulator.stop_all()
        if self.process_thread:
            self.process_thread.join(timeout=3)

    def _new_frames(self) -> dict:
        now = time.time()
        frames = {}
        for cam in list(self.simulator.cameras):
            cid = cam.camera_id
            if cam.frame_count == self._last_seen.get(cid):
                continue  # no new frame since last detection
            if now - self._last_processed.get(cid, 0) < self.process_interval:
                continue
            frame = cam.get_frame()
            if frame is None:
                continue
            frames[cid] = frame
            self._last_seen[cid] = cam.frame_count
            self._last_processed[cid] = now
        return frames

    def _process_loop(self):
        while self.is_processing:
            frames = self._new_frames()
            if self.detection_callback and frames:
                try:
                    t0 = time.time()
                    self.detection_callback(frames)
                    dt = time.time() - t0
                    self._cycles = getattr(self, '_cycles', 0) + 1
                    if self._cycles > 3:  # ignore model warm-up cycles
                        self.cycle_seconds = dt if not self.cycle_seconds else 0.8 * self.cycle_seconds + 0.2 * dt
                    self.frames_processed += len(frames)
                    self.last_detection_time = time.time()
                except Exception as e:
                    self.logger.error(f"Detection callback error: {e}")
            self.last_loop_time = time.time()
            time.sleep(0.05)

    def get_camera_status(self) -> dict:
        status = {}
        for camera in list(self.simulator.cameras):
            status[camera.camera_id] = {
                'running': camera.is_running,
                'connected': camera.connected,
                'name': camera.name,
                'location': camera.location,
                'kind': camera.kind,
                'frame_count': camera.frame_count,
                'last_frame_time': camera.last_frame_time,
                'fps': getattr(camera, 'actual_fps', camera.fps),
                'resolution': camera.resolution,
                'last_error': camera.last_error,
                'source': camera.video_source if camera.kind != 'network' else _redact(camera.video_source),
            }
        return status


def _redact(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.username or p.password:
        host = p.hostname + (f":{p.port}" if p.port else "")
        return f"{p.scheme}://***@{host}{p.path}"
    return url
