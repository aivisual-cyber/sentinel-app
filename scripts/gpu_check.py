#!/usr/bin/env python3
"""
Sentinel GPU check - which inference device will detection use on THIS machine, and how fast is it?

  python scripts/gpu_check.py              # human-readable report
  python scripts/gpu_check.py --json       # machine-readable (used by the Windows installer)

Tries every available ONNX Runtime execution provider (CUDA, DirectML, CPU) with the configured
model and times real inferences, so problems (old driver, unsupported GPU) show up at install time
instead of during a fire. Never needs internet.
"""

import argparse
import json
import platform
import sys
import time
from pathlib import Path

# Windows services / redirected output use a legacy code page: never crash on a non-ASCII character
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))
import paths  # noqa: E402


def model_path() -> Path:
    import yaml
    try:
        cfg = yaml.safe_load(paths.DETECTION_CONFIG.read_text()) or {}
        p = paths.resolve(cfg['detection']['model']['path'])
        if p.suffix == '.onnx' and p.exists():
            return p
    except Exception:
        pass
    for c in (paths.MODELS_DIR / 'fire_smoke.onnx', ROOT / 'rabahdev_best.onnx'):
        if c.exists():
            return c
    raise SystemExit("No .onnx model found")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--runs', type=int, default=10)
    args = ap.parse_args(argv)

    import onnxruntime as ort
    from detection.onnx_backend import OnnxYolo, gpu_names
    mp = model_path()
    frame = (np.random.default_rng(0).random((720, 1280, 3)) * 255).astype(np.uint8)
    report = {'platform': platform.platform(), 'python': platform.python_version(),
              'onnxruntime': ort.__version__, 'available_providers': ort.get_available_providers(),
              'gpus': gpu_names(), 'model': str(mp), 'results': []}
    for prov in [p for p in ('CUDAExecutionProvider', 'DmlExecutionProvider', 'CPUExecutionProvider')
                 if p in report['available_providers']]:
        entry = {'provider': prov}
        try:
            m = OnnxYolo(mp, providers=[prov])
            entry['active'] = m.provider
            if m.provider != prov:
                raise RuntimeError(f"fell back to {m.provider}")
            m(frame)  # warm-up (DirectML/CUDA compile kernels on first run)
            t = time.perf_counter()
            for _ in range(args.runs):
                m(frame)
            entry['ms_per_frame'] = round((time.perf_counter() - t) / args.runs * 1000, 1)
            entry['ok'] = True
        except Exception as e:
            entry.update(ok=False, error=str(e)[:300])
        report['results'].append(entry)
    gpu_ok = [r for r in report['results'] if r['ok'] and r['provider'] != 'CPUExecutionProvider']
    best = min((r for r in report['results'] if r['ok']), key=lambda r: r['ms_per_frame'], default=None)
    report['gpu_usable'] = bool(gpu_ok)
    report['selected'] = best['provider'] if best else None
    report['est_cameras_at_2fps'] = int(1000 / (best['ms_per_frame'] * 2)) if best else 0

    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(f"Platform     {report['platform']}  (Python {report['python']}, onnxruntime {report['onnxruntime']})")
        print(f"GPUs         {', '.join(report['gpus']) or 'none detected'}")
        print(f"Providers    {', '.join(report['available_providers'])}")
        for r in report['results']:
            print(f"  {r['provider']:24} " + (f"OK  {r['ms_per_frame']:7.1f} ms/frame" if r['ok'] else f"FAILED  {r['error']}"))
        print(f"Selected     {report['selected']}  -> upper bound ~{report['est_cameras_at_2fps']} camera(s) at 2 analysed FPS "
              "(single synthetic frame; run scripts/benchmark.py for real capacity)")
        if not report['gpu_usable']:
            print("NOTE: no usable GPU provider - detection runs on CPU. Update the GPU driver (DirectML needs a "
                  "DirectX 12 capable GPU) or reduce the number of cameras.")
    return 0 if report['selected'] else 1


if __name__ == '__main__':
    sys.exit(main())
