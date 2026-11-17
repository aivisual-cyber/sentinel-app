#!/usr/bin/env python3
"""
Sentinel accuracy parity check - does THIS machine's inference engine give the same answers as the
original PyTorch model?  Run it on every target machine (and after every driver or model change).

  sentinel parity                      # Windows: bundled Python
  python scripts/parity_check.py [--providers DmlExecutionProvider CPUExecutionProvider] [--json out.json]

The reference (tests/parity/golden.json) holds the detections of the original PyTorch/Ultralytics model
on a fixed set of lossless test images; it was produced on the build machine by make_golden.py.
This script runs the SHIPPED ONNX model with each available engine (DirectML GPU, CUDA, CPU) on the
same pixels and applies these acceptance criteria (all must hold, per engine):

  A1  identical alert decision (None / P4 / P2 / P1 at the configured thresholds) on every image
  A2  every reference box >= 0.10 confidence has a match (same class, IoU >= 0.90) and vice versa
  A3  matched boxes: |confidence difference| <= 0.02 and IoU >= 0.90
  A4  batch consistency: the same image in a batch of 4 gives the same result as alone

Exit code 0 = every tested engine PASSES; 1 = at least one FAILS (do not use that engine: set
detection.model.providers to one that passes, e.g. [CPUExecutionProvider]); 2 = cannot run.
This verifies equivalence to the reference model on this hardware. It does not measure how well
the model detects real fires at your site - that is the site trial.
"""

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tooling import ROOT, find_model, where  # noqa: E402  (sets up paths, UTF-8 output)

MATCH_MIN_CONF = 0.10
MAX_CONF_DIFF = 0.02
MIN_IOU = 0.90


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def level(dets, thresholds, fire_ids):
    m = max((d['conf'] for d in dets if d['cls'] in fire_ids), default=0.0)
    for name in ('immediate_alert', 'review_queue', 'log_only'):
        if m >= thresholds[name]:
            return {'immediate_alert': 'P1', 'review_queue': 'P2', 'log_only': 'P4'}[name], m
    return 'None', m


def to_dets(result, names):
    out = []
    for box in result.boxes or []:
        c = int(box.cls[0])
        out.append({'cls': c, 'name': names[c], 'conf': float(box.conf[0]),
                    'xyxy': [float(v) for v in box.xyxy[0]]})
    return out


def compare(ref, got):
    """A2/A3 for one image -> (problems, max_conf_diff, min_iou)."""
    problems, used, worst_diff, worst_iou = [], set(), 0.0, 1.0
    for r in [d for d in ref if d['conf'] >= MATCH_MIN_CONF]:
        best, best_iou = None, 0.0
        for k, g in enumerate(got):
            if k in used or g['cls'] != r['cls']:
                continue
            v = iou(r['xyxy'], g['xyxy'])
            if v > best_iou:
                best, best_iou = k, v
        if best is None or best_iou < MIN_IOU:
            problems.append(f"reference {r['name']} {r['conf']:.3f} not found (best IoU {best_iou:.2f})")
            continue
        used.add(best)
        diff = abs(got[best]['conf'] - r['conf'])
        worst_diff, worst_iou = max(worst_diff, diff), min(worst_iou, best_iou)
        if diff > MAX_CONF_DIFF:
            problems.append(f"{r['name']} confidence {r['conf']:.3f} -> {got[best]['conf']:.3f}")
    for k, g in enumerate(got):
        if k not in used and g['conf'] >= MATCH_MIN_CONF + MAX_CONF_DIFF:
            if not any(g['cls'] == r['cls'] and iou(r['xyxy'], g['xyxy']) >= MIN_IOU for r in ref):
                problems.append(f"extra {g['name']} {g['conf']:.3f} not in reference")
    return problems, worst_diff, worst_iou


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--golden', default=str(ROOT / 'tests' / 'parity' / 'golden.json'))
    ap.add_argument('--model', default=None, help='.onnx model (default: the one the service uses)')
    ap.add_argument('--providers', nargs='+', default=None, help='engines to test (default: all available)')
    ap.add_argument('--json', help='write the full report to this file')
    args = ap.parse_args(argv)

    import cv2
    import numpy as np
    import yaml
    try:
        import onnxruntime as ort
    except ImportError:
        print("onnxruntime is not installed in this Python. Run 'sentinel parity' (Sentinel's own Python).")
        return 2
    from detection.onnx_backend import OnnxYolo, gpu_names
    import paths

    gpath = Path(args.golden)
    if not gpath.is_file():
        print(f"Reference set not found: {gpath}")
        return 2
    golden = json.loads(gpath.read_text())
    model = find_model(args.model, onnx_only=True)
    model_sha = hashlib.sha256(model.read_bytes()).hexdigest()
    if model_sha != golden['onnx_sha256']:
        print(f"The reference set was made for a different model ({golden['onnx_sha256'][:12]}..., this one is "
              f"{model_sha[:12]}...). Re-create it with make_golden.py for this model.")
        return 2
    try:
        cfg = yaml.safe_load(paths.DETECTION_CONFIG.read_text(encoding='utf-8-sig')) or {}
        det_cfg = cfg.get('detection', {})
        thresholds = {**{'immediate_alert': 0.75, 'review_queue': 0.55, 'log_only': 0.35},
                      **det_cfg.get('thresholds', {})}
        wanted = {c.lower() for c in det_cfg.get('model', {}).get('classes', ['fire', 'smoke', 'flame'])}
    except Exception:
        thresholds = {'immediate_alert': 0.75, 'review_queue': 0.55, 'log_only': 0.35}
        wanted = {'fire', 'smoke', 'flame'}
    names = {int(k): v for k, v in golden['names'].items()}
    fire_ids = {k for k, v in names.items() if v.lower() in wanted}

    providers = args.providers or [p for p in ('CUDAExecutionProvider', 'DmlExecutionProvider',
                                               'CPUExecutionProvider') if p in ort.get_available_providers()]
    images = {f: cv2.imread(str(gpath.parent / f)) for f in golden['images']}
    missing = [f for f, im in images.items() if im is None]
    if missing:
        print(f"Missing reference images: {missing[:5]}")
        return 2

    print(where())
    print(f"Model      {model}  (sha256 {model_sha[:16]}...)")
    print(f"Reference  {golden['reference']}")
    print(f"Platform   {platform.platform()}  onnxruntime {ort.__version__}  GPUs: {', '.join(gpu_names()) or 'none'}")
    print(f"Thresholds P1 {thresholds['immediate_alert']}  P2 {thresholds['review_queue']}  "
          f"P4 {thresholds['log_only']};  {len(images)} images\n")

    report = {'model': str(model), 'model_sha256': model_sha, 'reference': golden['reference'],
              'platform': platform.platform(), 'onnxruntime': ort.__version__, 'gpus': gpu_names(),
              'thresholds': thresholds, 'criteria': {'match_min_conf': MATCH_MIN_CONF, 'max_conf_diff': MAX_CONF_DIFF,
                                                     'min_iou': MIN_IOU}, 'engines': []}
    all_pass = True
    for prov in providers:
        eng = {'provider': prov, 'images': {}, 'decision_mismatches': 0, 'box_problems': 0}
        try:
            m = OnnxYolo(model, providers=[prov], imgsz=int(golden['imgsz']))
            if m.provider != prov:
                raise RuntimeError(f"engine not usable here (fell back to {m.provider})")
        except Exception as e:
            eng.update(status='UNAVAILABLE', error=str(e)[:300])
            print(f"{prov:24} UNAVAILABLE  {str(e)[:120]}")
            report['engines'].append(eng)
            continue
        worst_diff, worst_iou, t0 = 0.0, 1.0, time.time()
        for fname, img in images.items():
            ref = golden['images'][fname]['detections']
            got = to_dets(m([img], conf=golden['ref_conf'], iou=golden['iou'])[0], names)
            ref_level, ref_max = level(ref, thresholds, fire_ids)
            got_level, got_max = level(got, thresholds, fire_ids)
            problems, d, i = compare(ref, got)
            worst_diff, worst_iou = max(worst_diff, d), min(worst_iou, i)
            if ref_level != got_level:
                problems.insert(0, f"DECISION {ref_level} -> {got_level} (max conf {ref_max:.3f} -> {got_max:.3f})")
                eng['decision_mismatches'] += 1
            eng['box_problems'] += len(problems)
            eng['images'][fname] = {'reference_level': ref_level, 'level': got_level,
                                    'reference_max_conf': round(ref_max, 4), 'max_conf': round(got_max, 4),
                                    'problems': problems}
        # A4: batch consistency (the service analyses several cameras in one call)
        first = next(iter(images))
        alone = to_dets(m([images[first]], conf=golden['ref_conf'], iou=golden['iou'])[0], names)
        batch = m([images[first]] * 4, conf=golden['ref_conf'], iou=golden['iou'])
        batch_ok = all(not compare(alone, to_dets(r, names))[0] for r in batch)
        eng.update(max_conf_diff=round(worst_diff, 5), min_iou=round(worst_iou, 4), batch_consistent=batch_ok,
                   seconds=round(time.time() - t0, 1))
        passed = eng['decision_mismatches'] == 0 and eng['box_problems'] == 0 and batch_ok
        eng['status'] = 'PASS' if passed else 'FAIL'
        all_pass &= passed
        report['engines'].append(eng)
        print(f"{prov:24} {eng['status']:4}  decisions {len(images) - eng['decision_mismatches']}/{len(images)} same  "
              f"max conf diff {worst_diff:.4f}  min IoU {worst_iou:.3f}  batch {'ok' if batch_ok else 'DIFFERENT'}")
        for fname, r in eng['images'].items():
            for p in r['problems']:
                print(f"    {fname}: {p}")
    tested = [e for e in report['engines'] if e.get('status') in ('PASS', 'FAIL')]
    report['result'] = 'PASS' if tested and all_pass else 'FAIL'
    print(f"\nResult: {report['result']}" + ("" if report['result'] == 'PASS' else
          " - use only engines marked PASS (detection.model.providers in detection_config.yaml)"))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1))
        print(f"Report written to {args.json}")
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
