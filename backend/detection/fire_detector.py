"""
Sentinel Fire Detection Engine
YOLO-based fire and smoke detection.

SUPPLEMENTARY early-warning only - never a replacement for certified fire alarm systems.

Key behaviours (see docs/ANALYSIS_AND_PLAN.md for why each exists):
  * The model path and the class names that count as fire/smoke come from config.
    No COCO-class heuristics (a person/car used to be reported as fire).
  * Checkpoints are loaded with torch ``weights_only=True`` (no arbitrary pickle code)
    and never downloaded at runtime (offline-first).
  * Thresholds hot-reload when detection_config.yaml changes.
  * Detection values are plain Python types so alerts serialise to JSON
    (numpy int64 bboxes previously made every alert fail to save).
"""

import copy
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
import yaml

try:
    from .. import paths
    from .rtsp_manager import RTSPManager, CameraConfig
except ImportError:  # backend/ on sys.path (api_server, tests)
    import paths
    from detection.rtsp_manager import RTSPManager, CameraConfig

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    'detection': {
        'model': {
            'path': 'rabahdev_best.pt',          # relative to SENTINEL_HOME
            'classes': ['fire', 'smoke', 'flame'],  # case-insensitive model class names
            'imgsz': 640,
            'iou': 0.7,      # NMS overlap. Always passed explicitly: library defaults change between
                             # versions (Ultralytics 8.4 moved its default 0.7 -> 0.5), which would make
                             # the .pt and .onnx paths - and two installs - behave differently.
            # .onnx models: ONNX Runtime execution providers in priority order, or [auto]
            # (auto = CUDA -> DirectML -> CPU, whichever is installed and works)
            'providers': ['auto'],
        },
        'thresholds': {
            'immediate_alert': 0.75,
            'review_queue': 0.55,
            'log_only': 0.35,
        },
        'temporal': {'window': 5, 'min_hits': 3},
        'environmental': {'fog_adjustment': -0.05, 'sunset_hours': [17, 19]},
    },
    'system': {'detection_latency_target': 2.0},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validate_thresholds(t: dict) -> None:
    for name in ('immediate_alert', 'review_queue', 'log_only'):
        v = t.get(name)
        if not isinstance(v, (int, float)) or not 0.0 < float(v) < 1.0:
            raise ValueError(f"threshold '{name}' must be a number in (0, 1), got {v!r}")
    if not t['log_only'] <= t['review_queue'] <= t['immediate_alert']:
        raise ValueError("thresholds must satisfy log_only <= review_queue <= immediate_alert")


@dataclass
class Detection:
    """Fire detection result"""
    confidence: float
    bbox: Tuple[int, int, int, int]  # x1, y1, x2, y2 (plain ints)
    class_name: str
    timestamp: float


@dataclass
class DetectionResult:
    """Complete detection result for a frame"""
    frame_id: int
    timestamp: float
    detections: List[Detection]
    max_confidence: float
    alert_level: str  # 'P1', 'P2', 'P4' or 'None'
    latency_ms: float = 0.0


class FireDetector:
    """Main fire detection engine"""

    def __init__(self, config_path: Optional[str] = None, model_path: Optional[str] = None):
        self.config_path = paths.resolve(config_path) if config_path else paths.DETECTION_CONFIG
        self._model_override = str(model_path) if model_path else None  # tools: benchmark/parity
        self._config_mtime = 0.0
        self.config = self._load_config()
        self._infer_lock = threading.Lock()
        self.model = self._load_model()
        self.fire_class_ids = self._resolve_fire_classes()
        self.frame_count = 0
        self.rtsp_manager = RTSPManager()
        self._notification_manager = None
        self.detection_callback: Optional[Callable] = None
        self.is_running = False
        self.detection_thread = None

    # ------------------------------------------------------------------ config
    def _load_config(self) -> dict:
        try:
            with open(self.config_path, 'r') as f:
                raw = yaml.safe_load(f) or {}
            self._config_mtime = Path(self.config_path).stat().st_mtime
        except FileNotFoundError:
            logger.warning(f"Config file not found: {self.config_path}, using defaults")
            raw = {}
        config = _deep_merge(DEFAULT_CONFIG, raw)
        if getattr(self, '_model_override', None):
            config['detection']['model']['path'] = self._model_override
        validate_thresholds(config['detection']['thresholds'])
        return config

    def _default_config(self) -> dict:
        return copy.deepcopy(DEFAULT_CONFIG)

    def maybe_reload_config(self) -> bool:
        """Hot-reload thresholds when the YAML file changes. Invalid edits are ignored."""
        try:
            mtime = Path(self.config_path).stat().st_mtime
        except OSError:
            return False
        if mtime <= self._config_mtime:
            return False
        try:
            new = self._load_config()
        except Exception as e:  # keep running with the last good config
            logger.error(f"Ignoring invalid detection config update: {e}")
            self._config_mtime = mtime
            return False
        self.config['detection']['thresholds'] = new['detection']['thresholds']
        self.config['detection']['temporal'] = new['detection']['temporal']
        logger.info(f"Detection thresholds reloaded: {self.config['detection']['thresholds']}")
        if new['detection']['model'] != self.config['detection']['model']:
            self._swap_model(new['detection']['model'])
        return True

    def _swap_model(self, model_cfg: dict) -> bool:
        """Hot-swap the model after a promotion (scripts/train_model.py). A model that fails to load
        or has no fire/smoke classes is rejected and the current one keeps running."""
        old_cfg = self.config['detection']['model']
        try:
            self.config['detection']['model'] = model_cfg
            new_model = self._load_model()
            wanted = {c.lower() for c in model_cfg['classes']}
            ids = {i for i, n in new_model.names.items() if n.lower() in wanted}
            if not ids:
                raise ValueError(f"model classes {new_model.names} contain none of {sorted(wanted)}")
        except Exception as e:
            self.config['detection']['model'] = old_cfg
            logger.error(f"Model swap rejected, keeping {old_cfg['path']}: {e}")
            return False
        with self._infer_lock:
            self.model, self.fire_class_ids = new_model, ids
        logger.warning(f"Detection model hot-swapped to {model_cfg['path']}")
        return True

    @property
    def thresholds(self) -> dict:
        return self.config['detection']['thresholds']

    # ------------------------------------------------------------------- model
    def _load_model(self):
        model_cfg = self.config['detection']['model']
        model_path = paths.resolve(model_cfg['path'])
        if not model_path.exists():
            raise FileNotFoundError(
                f"Fire/smoke model not found at {model_path}. Set detection.model.path in "
                f"{self.config_path}. A COCO model will NOT be substituted: it cannot detect fire.")
        if model_path.suffix.lower() == '.onnx':
            # ONNX Runtime: no PyTorch needed; GPU via DirectML (Windows, any DX12 GPU) or CUDA
            try:
                from .onnx_backend import OnnxYolo
            except ImportError:
                from detection.onnx_backend import OnnxYolo
            model = OnnxYolo(model_path, providers=model_cfg.get('providers') or ['auto'],
                             imgsz=int(model_cfg.get('imgsz', 640)))
        else:
            try:  # PyTorch is imported only when a .pt model is used
                from .fire_model_manager import safe_load_yolo
            except ImportError:
                from detection.fire_model_manager import safe_load_yolo
            model = safe_load_yolo(model_path)
            try:
                import torch
                model.device_label = (f"GPU via CUDA: {torch.cuda.get_device_name(0)}" if torch.cuda.is_available()
                                      else 'CPU (PyTorch)')
            except Exception:
                model.device_label = 'PyTorch'
        logger.info(f"Loaded fire model {model_path.name} classes={model.names} on {self.device_of(model)}")
        return model

    @staticmethod
    def device_of(model) -> str:
        return getattr(model, 'device', None) if isinstance(getattr(model, 'device', None), str) \
            else getattr(model, 'device_label', 'unknown')

    @property
    def inference_info(self) -> dict:
        return {'backend': getattr(self.model, 'backend', 'pytorch'), 'device': self.device_of(self.model),
                'model': self.config['detection']['model']['path']}

    def _resolve_fire_classes(self) -> set:
        wanted = {c.lower() for c in self.config['detection']['model']['classes']}
        ids = {i for i, n in self.model.names.items() if n.lower() in wanted}
        if not ids:
            raise ValueError(f"Model classes {self.model.names} contain none of {sorted(wanted)}")
        return ids

    # --------------------------------------------------------------- inference
    def detect_fire(self, frame: np.ndarray) -> DetectionResult:
        """Detect fire/smoke in a single BGR frame."""
        return self.detect_batch([frame])[0]

    def detect_batch(self, frames: List[np.ndarray]) -> List[DetectionResult]:
        """Detect fire/smoke in several frames with ONE inference call (one frame per camera per cycle).
        On a GPU this is much faster than per-frame calls; results are in input order."""
        if not frames:
            return []
        start = time.time()
        self.maybe_reload_config()
        min_conf = float(self.thresholds['log_only'])
        with self._infer_lock:  # Ultralytics models are not thread-safe
            results = self.model(list(frames), verbose=False, conf=min_conf,
                                 imgsz=int(self.config['detection']['model'].get('imgsz', 640)),
                                 iou=float(self.config['detection']['model'].get('iou', 0.7)))
        latency_ms = (time.time() - start) * 1000
        if latency_ms > self.config['system']['detection_latency_target'] * 1000:
            logger.warning(f"Detection latency {latency_ms:.0f} ms for {len(frames)} frame(s) exceeds target")

        out = []
        for result in results:
            self.frame_count += 1
            detections: List[Detection] = []
            if result.boxes is not None:
                for box in result.boxes:
                    class_id = int(box.cls[0])
                    confidence = float(box.conf[0])
                    class_name = str(self.model.names[class_id])
                    if class_id in self.fire_class_ids and self._is_fire_related(class_name, confidence):
                        x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                        detections.append(Detection(confidence, (x1, y1, x2, y2), class_name, time.time()))
            max_confidence = max((d.confidence for d in detections), default=0.0)
            out.append(DetectionResult(self.frame_count, time.time(), detections, max_confidence,
                                       self._determine_alert_level(max_confidence), latency_ms))
        return out

    def _is_fire_related(self, class_name: str, confidence: float) -> bool:
        """Exact (case-insensitive) match against configured fire/smoke class names."""
        wanted = {c.lower() for c in self.config['detection']['model']['classes']}
        return class_name.lower() in wanted and confidence >= self.thresholds['log_only']

    def _determine_alert_level(self, max_confidence: float) -> str:
        t = self.thresholds
        if max_confidence >= t['immediate_alert']:
            return 'P1'
        if max_confidence >= t['review_queue']:
            return 'P2'
        if max_confidence >= t['log_only']:
            return 'P4'
        return 'None'

    # ------------------------------------------------------------ annotation
    def _annotate_frame(self, frame: np.ndarray, detections: List[Detection]) -> np.ndarray:
        annotated = frame.copy()
        t = self.thresholds
        for d in detections:
            x1, y1, x2, y2 = d.bbox
            if d.confidence >= t['immediate_alert']:
                color = (0, 0, 255)
            elif d.confidence >= t['review_queue']:
                color = (0, 165, 255)
            else:
                color = (0, 255, 255)
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(annotated, f"{d.class_name}: {d.confidence:.2f}", (x1, max(12, y1 - 8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        return annotated

    def _save_alert_frame(self, frame: np.ndarray, detections: List[Detection], alert_id: str) -> Optional[str]:
        """Save an annotated frame for operator review."""
        try:
            paths.ALERT_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
            filepath = paths.ALERT_FRAMES_DIR / f"{alert_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            cv2.imwrite(str(filepath), self._annotate_frame(frame, detections))
            return str(filepath)
        except Exception as e:
            logger.error(f"Failed to save alert frame: {e}")
            return None

    save_alert_frame = _save_alert_frame

    def save_raw_frame(self, frame: np.ndarray, alert_id: str) -> Optional[str]:
        """Save the unannotated frame (used for the operator-feedback training dataset)."""
        try:
            paths.ALERT_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
            filepath = paths.ALERT_FRAMES_DIR / f"{alert_id}_raw.jpg"
            cv2.imwrite(str(filepath), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            return str(filepath)
        except Exception as e:
            logger.error(f"Failed to save raw frame: {e}")
            return None

    # ------------------------------------------------- legacy standalone mode
    @property
    def notification_manager(self):
        if self._notification_manager is None:
            try:
                from ..alerts.app_notification_system import AppNotificationManager
            except ImportError:
                from alerts.app_notification_system import AppNotificationManager
            self._notification_manager = AppNotificationManager()
        return self._notification_manager

    def _send_alert(self, result: DetectionResult, frame: np.ndarray, camera_source: str) -> None:
        try:
            from alerts.app_notification_system import AlertMessage
        except ImportError:
            from ..alerts.app_notification_system import AlertMessage
        try:
            alert = AlertMessage(
                alert_id=f"FIRE_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{result.frame_id}",
                alert_type=result.alert_level, camera_id=str(camera_source),
                message=f"Fire detected with {result.max_confidence:.1%} confidence",
                confidence=result.max_confidence, timestamp=datetime.now(),
                location=f"Camera: {camera_source}")
            self.notification_manager.send_fire_alert(alert, frame)
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    def process_video_stream(self, video_source) -> None:
        """Process a video file/stream standalone (debug tool)."""
        cap = cv2.VideoCapture(video_source)
        if not cap.isOpened():
            raise ValueError(f"Could not open video source: {video_source}")
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                result = self.detect_fire(frame)
                if result.alert_level != 'None':
                    self._send_alert(result, frame, video_source)
        finally:
            cap.release()

    def add_rtsp_camera(self, camera_id: str, rtsp_url: str, username: str = None,
                        password: str = None, fps: int = 15) -> bool:
        return self.rtsp_manager.add_camera(CameraConfig(camera_id=camera_id, rtsp_url=rtsp_url,
                                                         username=username, password=password, fps=fps))

    def remove_rtsp_camera(self, camera_id: str) -> bool:
        return self.rtsp_manager.remove_camera(camera_id)

    def start_monitoring(self) -> int:
        if self.is_running:
            return 0
        started = self.rtsp_manager.start_all()
        if started > 0:
            self.is_running = True
            self.detection_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
            self.detection_thread.start()
        return started

    def stop_monitoring(self) -> None:
        self.is_running = False
        self.rtsp_manager.stop_all()
        if self.detection_thread and self.detection_thread.is_alive():
            self.detection_thread.join(timeout=5)
        if self._notification_manager is not None:
            self._notification_manager.stop_processing()

    def set_detection_callback(self, callback: Callable[[str, DetectionResult], None]):
        self.detection_callback = callback

    def _monitoring_loop(self):
        while self.is_running:
            try:
                for camera_id, frame in self.rtsp_manager.get_frames().items():
                    result = self.detect_fire(frame)
                    if self.detection_callback and result.alert_level != 'None':
                        self.detection_callback(camera_id, result)
                time.sleep(0.1)
            except Exception as e:
                logger.error(f"Monitoring loop error: {e}")
                time.sleep(1)

    def get_camera_status(self) -> Dict:
        return self.rtsp_manager.get_all_status()

    def discover_cameras(self, timeout: int = 5) -> List[Dict]:
        return self.rtsp_manager.discover_cameras(timeout)

    def test_rtsp_connection(self, rtsp_url: str, username: str = None, password: str = None) -> Tuple[bool, str]:
        return self.rtsp_manager.test_rtsp_url(rtsp_url, username, password)

    def get_alert_stats(self) -> Dict:
        return self.notification_manager.get_alert_stats()

    def get_recent_alerts(self, hours: int = 24) -> List:
        return self.notification_manager.get_recent_alerts(hours)

    def acknowledge_alert(self, alert_id: str, acknowledged_by: str = "operator", notes: str = "") -> bool:
        return self.notification_manager.acknowledge_alert(alert_id, acknowledged_by)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    det = FireDetector()
    for p in sys.argv[1:]:
        r = det.detect_fire(cv2.imread(p))
        print(p, r.alert_level, [(d.class_name, round(d.confidence, 3)) for d in r.detections])
