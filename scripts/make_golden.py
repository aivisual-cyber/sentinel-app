#!/usr/bin/env python3
"""
Build the accuracy reference ("golden") set used by parity_check.py.  Run on a machine with
PyTorch + Ultralytics (the build/development machine), never on the target.

  python scripts/make_golden.py --pt rabahdev_best.pt --onnx rabahdev_best.onnx

Writes tests/parity/:
  img_*.png      deterministic test images (lossless PNG, so every machine decodes identical pixels):
                 the fire fixtures and variants (scales, crop, flip, brightness, low saturation,
                 JPEG artefacts), frames of test_fire_scenarios.avi, and fire-free hard negatives
  golden.json    the REFERENCE detections of the original PyTorch model (Ultralytics) on each image,
                 plus SHA-256 of the .pt and of the ONNX file the check applies to

parity_check.py then runs the shipped ONNX model on the target with every available engine
(DirectML, CUDA, CPU) and compares against this reference.
"""

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'tests' / 'parity'
REF_CONF = 0.05          # record low-confidence boxes too: near-threshold behaviour is what matters


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fit(img, width=960):
    h, w = img.shape[:2]
    return img if w <= width else cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)


def build_images() -> dict:
    imgs = {}
    for f in sorted((ROOT / 'tests' / 'fixtures').glob('*.jpg')):
        base = cv2.imread(str(f))
        n = f.stem
        h, w = base.shape[:2]
        imgs[f'{n}_orig'] = fit(base)
        imgs[f'{n}_w640'] = cv2.resize(base, (640, int(h * 640 / w)), interpolation=cv2.INTER_AREA)
        imgs[f'{n}_w320'] = cv2.resize(base, (320, int(h * 320 / w)), interpolation=cv2.INTER_AREA)
        imgs[f'{n}_flip'] = fit(cv2.flip(base, 1))
        imgs[f'{n}_crop'] = fit(base[h // 6:h - h // 6, w // 6:w - w // 6])
        imgs[f'{n}_dark'] = fit(cv2.convertScaleAbs(base, alpha=0.6, beta=0))
        imgs[f'{n}_bright'] = fit(cv2.convertScaleAbs(base, alpha=1.3, beta=25))
        hsv = cv2.cvtColor(base, cv2.COLOR_BGR2HSV)
        hsv[..., 1] = (hsv[..., 1] * 0.35).astype(np.uint8)
        imgs[f'{n}_lowsat'] = fit(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR))
        ok, buf = cv2.imencode('.jpg', base, [cv2.IMWRITE_JPEG_QUALITY, 35])
        imgs[f'{n}_jpeg35'] = fit(cv2.imdecode(buf, cv2.IMREAD_COLOR))
    video = ROOT / 'test_fire_scenarios.avi'
    if video.exists():
        cap = cv2.VideoCapture(str(video))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 100
        for k, idx in enumerate(np.linspace(0, total - 1, 10).astype(int)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
            ok, fr = cap.read()
            if ok:
                imgs[f'video_{k:02d}'] = fit(fr)
        cap.release()
    rng = np.random.default_rng(7)
    grad = np.tile(np.linspace(0, 255, 960, dtype=np.uint8), (540, 1))
    imgs['neg_gradient'] = cv2.merge([grad, grad // 2, 255 - grad])
    imgs['neg_noise'] = (rng.random((540, 960, 3)) * 255).astype(np.uint8)
    orange = np.full((540, 960, 3), 40, np.uint8)
    cv2.rectangle(orange, (300, 150), (650, 420), (0, 140, 255), -1)       # orange object, not fire
    imgs['neg_orange_box'] = orange
    sunset = np.zeros((540, 960, 3), np.uint8)
    for y in range(540):
        sunset[y] = (30 + y // 8, 80 + y // 4, min(255, 150 + y // 3))  # warm sky gradient
    imgs['neg_warm_sky'] = sunset
    imgs['neg_black'] = np.zeros((540, 960, 3), np.uint8)
    imgs['neg_grey'] = np.full((540, 960, 3), 128, np.uint8)
    return imgs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pt', default=str(ROOT / 'rabahdev_best.pt'))
    ap.add_argument('--onnx', default=str(ROOT / 'rabahdev_best.onnx'))
    ap.add_argument('--imgsz', type=int, default=640)
    ap.add_argument('--iou', type=float, default=0.7, help='NMS IoU - must equal detection.model.iou')
    args = ap.parse_args(argv)

    sys.path.insert(0, str(ROOT / 'backend'))
    import torch
    import ultralytics
    from detection.fire_model_manager import safe_load_yolo  # restricted (weights-only) loading

    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob('img_*.png'):
        old.unlink()
    model = safe_load_yolo(Path(args.pt))
    images, golden = build_images(), {}
    for name, img in images.items():
        fname = f'img_{name}.png'
        cv2.imwrite(str(OUT / fname), img)
        pix = cv2.imread(str(OUT / fname))                    # exactly the pixels the target will read
        r = model(pix, conf=REF_CONF, imgsz=args.imgsz, iou=args.iou, verbose=False)[0]
        dets = [{'cls': int(c), 'name': str(model.names[int(c)]), 'conf': round(float(s), 5),
                 'xyxy': [round(float(v), 2) for v in b]}
                for c, s, b in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist(), r.boxes.xyxy.tolist())]
        golden[fname] = {'shape': list(pix.shape), 'detections': dets}
        print(f"{fname:32} {len(dets):3} boxes  max {max((d['conf'] for d in dets), default=0):.3f}")
    meta = {'reference': f"PyTorch {torch.__version__} + Ultralytics {ultralytics.__version__}, CPU, "
                         f"{platform.platform()}",
            'pt_sha256': sha256(Path(args.pt)), 'onnx_sha256': sha256(Path(args.onnx)),
            'imgsz': args.imgsz, 'iou': args.iou, 'ref_conf': REF_CONF, 'names': {int(k): v for k, v in model.names.items()},
            'images': golden}
    (OUT / 'golden.json').write_text(json.dumps(meta, indent=1))
    print(f"\n{len(golden)} images -> {OUT / 'golden.json'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
