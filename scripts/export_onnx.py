#!/usr/bin/env python3
"""
Export a YOLO .pt fire/smoke model to ONNX for the PyTorch-free runtime (Windows / DirectML, ONNX Runtime).

  python scripts/export_onnx.py rabahdev_best.pt                 # -> rabahdev_best.onnx
  python scripts/export_onnx.py models/fire_smoke_2026.pt --verify tests/fixtures

Needs PyTorch + ultralytics + onnx (build/training machine only). The checkpoint is loaded with the
restricted (weights_only) loader. --verify compares ONNX vs PyTorch detections on the given images.
"""

import argparse
import glob
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))


def export(pt_path: str, imgsz: int = 640) -> Path:
    from detection.fire_model_manager import safe_load_yolo
    pt = Path(pt_path).resolve()
    out = Path(safe_load_yolo(pt).export(format='onnx', imgsz=imgsz, dynamic=True, opset=17, simplify=True))
    target = pt.with_suffix('.onnx')
    if out.resolve() != target:
        shutil.move(str(out), target)
    return target


def verify(pt_path: str, onnx_path: str, images_dir: str) -> bool:
    import cv2
    import numpy as np
    from detection.fire_model_manager import safe_load_yolo
    from detection.onnx_backend import OnnxYolo
    a, b = safe_load_yolo(pt_path), OnnxYolo(onnx_path, providers=['cpu'])
    files = sorted(glob.glob(os.path.join(images_dir, '*.jp*g')) + glob.glob(os.path.join(images_dir, '*.png')))
    worst = 0.0
    for f in files:
        im = cv2.imread(f)
        ca = max([float(x.conf[0]) for x in a(im, verbose=False, conf=0.05)[0].boxes] or [0.0])
        cb = max([float(x.conf[0]) for x in b(im, conf=0.05)[0].boxes] or [0.0])
        worst = max(worst, abs(ca - cb))
    print(f"verified on {len(files)} images: max confidence difference {worst:.4f}")
    return worst < 0.01


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('model')
    ap.add_argument('--imgsz', type=int, default=640)
    ap.add_argument('--verify', help='directory of images to compare ONNX vs PyTorch')
    args = ap.parse_args(argv)
    out = export(args.model, args.imgsz)
    print(f"ONNX model: {out} ({out.stat().st_size / 1e6:.1f} MB)")
    if args.verify and not verify(args.model, str(out), args.verify):
        print("WARNING: ONNX output differs from PyTorch by more than 0.01")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
