#!/usr/bin/env python3
"""
Sentinel capacity benchmark - how many cameras can THIS machine analyse, and how fast?

Runs the real FireDetector (configured model, thresholds, imgsz) on real images, simulating N cameras
each delivering a frame per cycle, and compares per-frame inference with the batched inference the
service uses. Reports the per-camera analysed FPS achievable, the cycle latency (time for one frame
from every camera), and the largest camera count that meets the configured analysis rate.

  python scripts/benchmark.py                          # uses tests/fixtures images, 1..16 cameras
  python scripts/benchmark.py --images /path/to/frames --cameras 1 4 8 16 --seconds 10
"""

import argparse
import glob
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))
import paths  # noqa: E402
from config.config_manager import ConfigManager  # noqa: E402
from detection.fire_detector import FireDetector  # noqa: E402


def run(det, frames, n, seconds, batched):
    cycle_ms, start = [], time.time()
    i = 0
    while time.time() - start < seconds or len(cycle_ms) < 3:
        batch = [frames[(i + k) % len(frames)] for k in range(n)]
        t = time.perf_counter()
        if batched:
            det.detect_batch(batch)
        else:
            for f in batch:
                det.detect_fire(f)
        cycle_ms.append((time.perf_counter() - t) * 1000)
        i += n
    p50 = statistics.median(cycle_ms)
    p95 = sorted(cycle_ms)[int(0.95 * (len(cycle_ms) - 1))]
    return {'cameras': n, 'mode': 'batched' if batched else 'sequential', 'cycles': len(cycle_ms),
            'cycle_p50_ms': round(p50, 1), 'cycle_p95_ms': round(p95, 1),
            'per_camera_fps': round(1000 / p95, 2)}  # each camera gets one frame per cycle (conservative: p95)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--images', default=str(ROOT / 'tests' / 'fixtures'))
    ap.add_argument('--cameras', type=int, nargs='+', default=[1, 2, 4, 8, 16])
    ap.add_argument('--seconds', type=float, default=8)
    ap.add_argument('--json', help='write results to this file')
    args = ap.parse_args(argv)

    files = sorted(glob.glob(os.path.join(args.images, '*.jp*g')) + glob.glob(os.path.join(args.images, '*.png')))
    if not files:
        sys.exit(f"No images in {args.images}")
    frames = [cv2.imread(f) for f in files]
    cfg = ConfigManager()
    target_fps = cfg.get_system_config().frame_rate
    det = FireDetector(str(cfg.config_path))
    device = det.inference_info['device']
    print(f"Model {det.config['detection']['model']['path']}  imgsz {det.config['detection']['model']['imgsz']}  "
          f"device {device}  target {target_fps} analysed FPS/camera")
    det.detect_batch(frames[:1])  # warm-up

    rows = []
    print(f"{'cams':>4} {'mode':>10} {'cycle p50':>10} {'cycle p95':>10} {'FPS/camera':>11}  meets target")
    for n in args.cameras:
        for batched in (False, True):
            r = run(det, frames, n, args.seconds, batched)
            r['meets_target'] = r['per_camera_fps'] >= target_fps
            rows.append(r)
            print(f"{n:>4} {r['mode']:>10} {r['cycle_p50_ms']:>8.0f}ms {r['cycle_p95_ms']:>8.0f}ms "
                  f"{r['per_camera_fps']:>11.2f}  {'yes' if r['meets_target'] else 'NO'}")
    ok = [r['cameras'] for r in rows if r['mode'] == 'batched' and r['meets_target']]
    cap = max(ok) if ok else 0
    print(f"\nCapacity: up to {cap} camera(s) at {target_fps} analysed FPS each on this machine "
          f"(worst-case added latency per frame ~= one cycle).")
    if not str(device).startswith('GPU'):
        print("Running on CPU: for more cameras use a GPU (Windows: DirectML, any DX12 GPU; Linux: CUDA).")
    if args.json:
        Path(args.json).write_text(json.dumps({'device': device, 'platform': platform.platform(),
                                               'target_fps': target_fps, 'capacity': cap, 'rows': rows}, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
