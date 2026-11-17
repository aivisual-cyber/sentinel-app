"""
ONNX Runtime inference backend for YOLOv8/YOLO11 detection models - no PyTorch required.

Why: one small runtime that uses the GPU on *any* generation of NVIDIA card (and AMD / Intel):
  * Windows: DirectML execution provider - any DirectX 12 GPU through the normal display driver
    (NVIDIA Kepler/GTX 600 series through Blackwell/RTX 50), no CUDA toolkit or cuDNN.
  * Linux/Windows with onnxruntime-gpu: CUDA execution provider.
  * Anywhere: CPU fallback.

The class mimics the small part of the Ultralytics results API that FireDetector uses
(``model.names`` and ``model(frames, conf=, imgsz=)[i].boxes`` with ``cls``/``conf``/``xyxy``),
so detection, alerting and evidence code are identical for both backends.

Pre/post-processing matches Ultralytics: letterbox to a square (pad colour 114), RGB, /255,
class-aware NMS (IoU 0.7), boxes scaled back to the original frame.
"""

import ast
import logging
import platform
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import cv2
import numpy as np

logger = logging.getLogger(__name__)

PREFERRED_PROVIDERS = ('TensorrtExecutionProvider', 'CUDAExecutionProvider', 'DmlExecutionProvider',
                       'CPUExecutionProvider')
PROVIDER_ALIASES = {'tensorrt': 'TensorrtExecutionProvider', 'cuda': 'CUDAExecutionProvider',
                    'directml': 'DmlExecutionProvider', 'dml': 'DmlExecutionProvider',
                    'cpu': 'CPUExecutionProvider'}


class _Box:
    __slots__ = ('cls', 'conf', 'xyxy')

    def __init__(self, cls: int, conf: float, xyxy: np.ndarray):
        self.cls = np.array([cls])
        self.conf = np.array([conf], dtype=np.float32)
        self.xyxy = xyxy.reshape(1, 4)


class _Result:
    __slots__ = ('boxes',)

    def __init__(self, boxes: List[_Box]):
        self.boxes = boxes


def _letterbox(img: np.ndarray, size: int, rect: bool = False, stride: int = 32):
    """Ultralytics LetterBox: square pad, or (rect=True, used when all frames share a shape)
    minimal padding to a multiple of the stride - matching it keeps results identical to PyTorch."""
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    dw, dh = size - nw, size - nh
    if rect:
        dw, dh = dw % stride, dh % stride
    dw, dh = dw / 2, dh / 2
    if (w, h) != (nw, nh):
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return img, r, left, top


def _nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> List[int]:
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][ovr <= iou]
    return keep


def available_providers() -> List[str]:
    import onnxruntime as ort
    return list(ort.get_available_providers())


def gpu_names() -> List[str]:
    """Best-effort list of GPU adapter names (for status display only)."""
    import subprocess
    try:
        r = subprocess.run(['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return [f"{l.split(',')[0].strip()} (driver {l.split(',')[1].strip()})" for l in r.stdout.strip().splitlines()]
    except Exception:
        pass
    if platform.system() == 'Windows':
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-Command',
                                '(Get-CimInstance Win32_VideoController).Name'],
                               capture_output=True, text=True, timeout=10)
            return [l.strip() for l in r.stdout.splitlines() if l.strip()]
        except Exception:
            pass
    return []


class OnnxYolo:
    """YOLO detection model running on ONNX Runtime."""

    backend = 'onnxruntime'

    def __init__(self, model_path, providers: Optional[Sequence[str]] = None, imgsz: int = 640,
                 iou: float = 0.7, max_det: int = 300):
        import onnxruntime as ort
        self.ckpt_path = str(model_path)
        self.imgsz, self.iou, self.max_det = int(imgsz), float(iou), int(max_det)
        avail = ort.get_available_providers()
        wanted = [PROVIDER_ALIASES.get(str(p).lower(), p) for p in (providers or ['auto'])]
        if wanted == ['auto'] or 'auto' in [str(p).lower() for p in wanted]:
            wanted = [p for p in PREFERRED_PROVIDERS if p != 'TensorrtExecutionProvider']
        chosen = [p for p in wanted if p in avail] or ['CPUExecutionProvider']
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if 'DmlExecutionProvider' in chosen:
            # DirectML requirements (onnxruntime docs): no memory pattern, sequential execution
            so.enable_mem_pattern = False
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = self._create_session(ort, model_path, so, chosen)
        self.provider = self.session.get_providers()[0]
        meta = self.session.get_modelmeta().custom_metadata_map
        names = ast.literal_eval(meta.get('names', '{}')) if meta.get('names') else {}
        out_ch = self.session.get_outputs()[0].shape[1]
        nc = (out_ch - 4) if isinstance(out_ch, int) else len(names)
        self.names: Dict[int, str] = {int(k): str(v) for k, v in names.items()} or {i: str(i) for i in range(nc)}
        self.input_name = self.session.get_inputs()[0].name
        self.stride = int(meta.get('stride', 32))
        self.device = self._describe()
        logger.info(f"ONNX model {Path(model_path).name} on {self.device} (available providers: {avail})")

    @staticmethod
    def _create_session(ort, model_path, so, chosen):
        try:
            return ort.InferenceSession(str(model_path), sess_options=so, providers=chosen)
        except Exception as e:  # e.g. GPU provider present but driver/GPU unusable -> CPU
            if chosen != ['CPUExecutionProvider']:
                logger.error(f"GPU execution provider {chosen[0]} failed ({e}); falling back to CPU")
                return ort.InferenceSession(str(model_path), sess_options=so, providers=['CPUExecutionProvider'])
            raise

    def _describe(self) -> str:
        if self.provider == 'CPUExecutionProvider':
            return 'CPU (ONNX Runtime)'
        names = gpu_names()
        label = {'DmlExecutionProvider': 'DirectML', 'CUDAExecutionProvider': 'CUDA',
                 'TensorrtExecutionProvider': 'TensorRT'}.get(self.provider, self.provider)
        return f"GPU via {label}" + (f": {', '.join(names)}" if names else '')

    def __call__(self, frames, verbose: bool = False, conf: float = 0.25, imgsz: Optional[int] = None, **_):
        if isinstance(frames, np.ndarray):
            frames = [frames]
        size = int(imgsz or self.imgsz)
        rect = len({f.shape for f in frames}) == 1  # same as Ultralytics: rect only for uniform batches
        batch, metas = [], []
        for f in frames:
            img, r, left, top = _letterbox(f, size, rect=rect, stride=self.stride)
            batch.append(img[:, :, ::-1].transpose(2, 0, 1))  # BGR->RGB, HWC->CHW
            metas.append((r, left, top, f.shape[1], f.shape[0]))
        x = np.ascontiguousarray(np.stack(batch), dtype=np.float32) / 255.0
        out = self.session.run(None, {self.input_name: x})[0]  # (B, 4+nc, anchors)
        return [self._post(out[i], metas[i], conf) for i in range(len(frames))]

    def _post(self, pred: np.ndarray, meta, conf_thr: float) -> _Result:
        pred = pred.T  # (anchors, 4+nc)
        scores = pred[:, 4:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(scores)), cls]
        m = conf >= conf_thr
        if not m.any():
            return _Result([])
        b, cls, conf = pred[m, :4], cls[m], conf[m]
        xyxy = np.concatenate([b[:, :2] - b[:, 2:] / 2, b[:, :2] + b[:, 2:] / 2], axis=1)
        offset = cls[:, None] * 7680.0  # class-aware NMS
        keep = _nms(xyxy + offset, conf, self.iou)[: self.max_det]
        r, left, top, w, h = meta
        boxes = []
        for i in keep:
            x1, y1, x2, y2 = xyxy[i]
            bb = np.array([(x1 - left) / r, (y1 - top) / r, (x2 - left) / r, (y2 - top) / r], dtype=np.float32)
            bb[[0, 2]] = bb[[0, 2]].clip(0, w)
            bb[[1, 3]] = bb[[1, 3]].clip(0, h)
            boxes.append(_Box(int(cls[i]), float(conf[i]), bb))
        return _Result(boxes)
