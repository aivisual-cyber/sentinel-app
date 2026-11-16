"""Image-level benchmark of fire/smoke detectors.

Usage: bench_models.py <pos_root> <neg_dir> <neg_dir2> name=path.pt [name=path.pt ...]
Checkpoints are loaded with torch weights_only=True, allow-listing only torch/ultralytics
classes, so no arbitrary pickle code can execute.
"""
import builtins
import glob
import importlib
import json
import os
import pickle
import re
import sys
import time

import cv2
import torch
from ultralytics import YOLO

FIRE = {'fire', 'smoke', 'flame', 'flames'}


def safe_yolo(path):
    allow = [builtins.set, builtins.getattr]
    ck = None
    for _ in range(80):
        try:
            with torch.serialization.safe_globals(allow):
                ck = torch.load(path, map_location='cpu', weights_only=True)
            break
        except pickle.UnpicklingError as e:
            g = re.search(r'Unsupported global: GLOBAL ([\w\.]+)', str(e)).group(1)
            if g.split('.')[0] not in ('torch', 'ultralytics', 'collections'):
                raise SystemExit(f"refusing {path}: needs global {g}")
            mod, name = g.rsplit('.', 1)
            allow.append(getattr(importlib.import_module(mod), name))
    net = (ck.get('ema') or ck['model']).float().eval()
    y = YOLO('yolov8n.yaml', task='detect')
    y.model = net
    return y


def main():
    pos = [p for p in glob.glob(sys.argv[1] + '/*/images/*.jpg')
           if os.path.getsize(p.replace('/images/', '/labels/').rsplit('.', 1)[0] + '.txt') > 0]
    neg = glob.glob(sys.argv[2] + '/*.jpg') + glob.glob(sys.argv[3] + '/*.jpg')
    thrs = (0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85)
    out = {}
    for name, path in (a.split('=', 1) for a in sys.argv[4:]):
        m = safe_yolo(path)
        keep = {i for i, n in m.names.items() if n.lower() in FIRE}

        def score(p):
            r = m(cv2.imread(p), verbose=False, conf=0.05)[0]
            return max([float(b.conf[0]) for b in r.boxes if int(b.cls[0]) in keep] or [0.0])

        t = time.time()
        P = [score(p) for p in pos]
        ms = (time.time() - t) / len(pos) * 1000
        N = [score(p) for p in neg]
        rows = {round(t / 100, 2): (sum(x >= t / 100 for x in P) / len(P), sum(x >= t / 100 for x in N) / len(N))
                for t in range(10, 96, 5)}
        ok = sorted((thr, r, f) for thr, (r, f) in rows.items() if f <= 0.02)
        op = ok[0] if ok else None
        out[name] = {'ms': round(ms), 'classes': m.names, 'op': op, 'rows': rows}
        print(f"\n## {name}  classes={m.names}  {ms:.0f} ms/img (CPU)  pos={len(P)} neg={len(N)}")
        print("thr  " + " ".join(f"{t:>6.2f}" for t in thrs))
        print("rec  " + " ".join(f"{rows[t][0]:6.0%}" for t in thrs))
        print("FA   " + " ".join(f"{rows[t][1]:6.1%}" for t in thrs))
        print("operating point (lowest thr with image FA <= 2%):",
              f"thr={op[0]:.2f} recall={op[1]:.1%} FA={op[2]:.1%}" if op else "none")
    with open(os.path.splitext(sys.argv[0])[0] + '_results.json', 'w') as f:
        json.dump(out, f, default=str, indent=1)


if __name__ == '__main__':
    main()
