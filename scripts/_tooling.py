"""
Shared helpers for the command-line tools (benchmark, gpu_check, parity_check).

They must work the same way from every place an operator may start them:
  * the installed program   <InstallDir>\\app\\scripts   (data dir from the install marker / SENTINEL_HOME)
  * the unpacked USB bundle <Bundle>\\app\\scripts       (model in <Bundle>\\models, before or after install)
  * the repository          scripts/                     (development)
Use the bundled Python (sentinel.cmd does that): a different Python lacks onnxruntime.
"""

import glob
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent          # repo root or <InstallDir|Bundle>\app
sys.path.insert(0, str(ROOT / 'backend'))
import paths  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # never crash on a legacy console code page
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.bmp', '.webp')
VIDEO_EXT = ('.mp4', '.avi', '.mkv', '.mov', '.m4v', '.ts')


def configured_model() -> Path:
    try:
        cfg = yaml.safe_load(paths.DETECTION_CONFIG.read_text(encoding='utf-8-sig')) or {}
        return paths.resolve(cfg['detection']['model']['path'])
    except Exception:
        return paths.resolve('rabahdev_best.pt')


def find_model(explicit=None, onnx_only=False) -> Path:
    """The model the service uses; else the one shipped with the bundle/repo. Never a COCO model."""
    candidates = [Path(explicit)] if explicit else []
    candidates += [configured_model(), paths.MODELS_DIR / 'fire_smoke.onnx',
                   ROOT.parent / 'models' / 'fire_smoke.onnx', ROOT / 'models' / 'fire_smoke.onnx',
                   ROOT / 'rabahdev_best.onnx']
    for c in candidates:
        if c and c.is_file() and (not onnx_only or c.suffix.lower() == '.onnx'):
            return c.resolve()
    sys.exit("No fire/smoke model found. Looked in:\n  " + "\n  ".join(str(c) for c in candidates)
             + "\nPass --model <path to .onnx>.")


def load_frames(source=None, limit=64):
    """Frames from an image folder (recursive), one image, or a video file (evenly sampled)."""
    import cv2
    source = Path(source) if source else ROOT / 'tests' / 'fixtures'
    if source.is_file() and source.suffix.lower() in VIDEO_EXT:
        cap = cv2.VideoCapture(str(source))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or limit
        step = max(1, total // limit)
        frames, i = [], 0
        while len(frames) < limit:
            ok, f = cap.read()
            if not ok:
                break
            if i % step == 0:
                frames.append(f)
            i += 1
        cap.release()
        return frames, [f"{source.name}#{k}" for k in range(len(frames))]
    files = [source] if source.is_file() else sorted(
        p for p in source.rglob('*') if p.suffix.lower() in IMAGE_EXT)
    frames, names = [], []
    for p in files[:limit]:
        img = cv2.imdecode(__import__('numpy').fromfile(str(p), dtype='uint8'), cv2.IMREAD_COLOR)  # unicode paths
        if img is not None:
            frames.append(img)
            names.append(p.name)
    return frames, names


def where() -> str:
    return f"data dir {paths.SENTINEL_HOME}  (set SENTINEL_HOME to override)"
