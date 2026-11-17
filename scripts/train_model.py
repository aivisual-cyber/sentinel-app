#!/usr/bin/env python3
"""
Sentinel model improvement pipeline: build dataset -> fine-tune -> evaluate -> promote (only if better).

Data sources (all YOLO format; class names are remapped to smoke=0, fire=1):
  --public DIR       public datasets (D-Fire, FASDD, Roboflow exports, ...). Class names are read from a
                     data.yaml in DIR or up to two parent directories, or given with --names.
  --feedback DIR     this site's operator verdicts (default: $SENTINEL_HOME/data/feedback):
                     confirmed fires (pseudo-labelled) + false alarms (hard negatives, empty labels)
  --negatives DIR    extra images known to contain no fire (site footage, sunsets, kitchens ...)

Evaluation uses what matters operationally: image-level RECALL at a fixed FALSE-ALARM budget on held-out
negatives (default 2 %), plus recall/false alarms at the configured P4 threshold - not mAP alone.
A candidate is promoted only if it beats the current model; the config is updated atomically and the
running service hot-swaps the model. History: models/MODEL_HISTORY.jsonl (roll back by editing model.path).

Examples
  python scripts/train_model.py build --public /data/D-Fire --negatives /data/site_negatives
  python scripts/train_model.py all --public /data/D-Fire --epochs 50 --device 0
  python scripts/train_model.py evaluate --model models/candidate.pt --data datasets/sentinel_xxx/data.yaml
GPU strongly recommended for training (CPU works for smoke tests: --epochs 1 --imgsz 320).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))
import paths  # noqa: E402

TARGET = {'smoke': 0, 'fire': 1, 'flame': 1, 'flames': 1}
NAMES = {0: 'smoke', 1: 'fire'}
IMG_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}


# ----------------------------------------------------------------------------- dataset
def _class_names(root: Path, override: str = None) -> list:
    if override:
        return [n.strip() for n in override.split(',')]
    for d in [root, *list(root.parents)[:2]]:
        for name in ('data.yaml', 'dataset.yaml'):
            f = d / name
            if f.is_file():
                names = (yaml.safe_load(f.read_text()) or {}).get('names')
                if isinstance(names, dict):
                    names = [names[k] for k in sorted(names)]
                if names:
                    return list(names)
    raise SystemExit(f"No data.yaml with 'names' found for {root}; pass --names fire,smoke,...")


def _pairs(root: Path):
    for img in sorted(root.rglob('*')):
        if img.suffix.lower() in IMG_EXT and img.parent.name == 'images':
            yield img, img.parent.parent / 'labels' / (img.stem + '.txt')


def _remap(label: Path, src_names: list) -> list:
    if not label.is_file():
        return []
    out = []
    for line in label.read_text().splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue  # skip segmentation/malformed rows
        name = src_names[int(parts[0])].lower() if int(parts[0]) < len(src_names) else ''
        if name in TARGET:
            out.append(' '.join([str(TARGET[name])] + parts[1:]))
    return out


def _split(key: str, val_pct: int) -> str:
    return 'val' if int(hashlib.md5(key.encode()).hexdigest(), 16) % 100 < val_pct else 'train'


def _place(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def build_dataset(args) -> Path:
    out = Path(args.out or ROOT / 'datasets' / f"sentinel_{time.strftime('%Y%m%d_%H%M%S')}")
    if out.exists():
        shutil.rmtree(out)
    stats = {}

    def add(src_tag, img, lines):
        split = _split(f"{src_tag}/{img}", args.val_pct)
        name = f"{src_tag}_{hashlib.md5(str(img).encode()).hexdigest()[:10]}{img.suffix.lower()}"
        _place(img, out / 'images' / split / name)
        lbl = out / 'labels' / split / (Path(name).stem + '.txt')
        lbl.parent.mkdir(parents=True, exist_ok=True)
        lbl.write_text('\n'.join(lines))
        s = stats.setdefault(src_tag, {'train_pos': 0, 'train_neg': 0, 'val_pos': 0, 'val_neg': 0})
        s[f"{split}_{'pos' if lines else 'neg'}"] += 1

    for i, d in enumerate(args.public or []):
        root = Path(d)
        names = _class_names(root, args.names)
        for img, lbl in _pairs(root):
            add(f"pub{i}", img, _remap(lbl, names))
    fb = Path(args.feedback) if args.feedback else paths.DATA_DIR / 'feedback'
    if (fb / 'images').is_dir():
        for img in sorted((fb / 'images').iterdir()):
            lbl = fb / 'labels' / (img.stem + '.txt')
            add('site', img, [l for l in lbl.read_text().splitlines() if l.strip()] if lbl.is_file() else [])
    for i, d in enumerate(args.negatives or []):
        for img in sorted(Path(d).rglob('*')):
            if img.suffix.lower() in IMG_EXT:
                add(f"neg{i}", img, [])

    if not stats:
        raise SystemExit("No images found - give --public / --feedback / --negatives")
    (out / 'data.yaml').write_text(yaml.safe_dump({'path': str(out), 'train': 'images/train', 'val': 'images/val',
                                                   'names': NAMES}))
    (out / 'stats.json').write_text(json.dumps(stats, indent=1))
    print(f"Dataset: {out}")
    for k, v in stats.items():
        print(f"  {k:6} {v}")
    val_neg = sum(v['val_neg'] for v in stats.values())
    if val_neg < 100:
        print(f"  WARNING: only {val_neg} validation negatives - false-alarm estimates will be coarse. "
              "Add site footage with --negatives.")
    return out / 'data.yaml'


# ----------------------------------------------------------------------------- evaluation
def evaluate(model_path: str, data_yaml: str, budget: float, p4: float) -> dict:
    import cv2
    from detection.fire_model_manager import safe_load_yolo
    m = safe_load_yolo(model_path)
    keep = {i for i, n in m.names.items() if n.lower() in TARGET}
    root = Path(yaml.safe_load(Path(data_yaml).read_text())['path'])
    scores, labels = [], []
    for img in sorted((root / 'images' / 'val').iterdir()):
        lbl = root / 'labels' / 'val' / (img.stem + '.txt')
        labels.append(bool(lbl.is_file() and lbl.read_text().strip()))
        r = m(cv2.imread(str(img)), verbose=False, conf=0.05)[0]
        scores.append(max([float(b.conf[0]) for b in r.boxes if int(b.cls[0]) in keep] or [0.0]))
    pos = [s for s, l in zip(scores, labels) if l]
    neg = [s for s, l in zip(scores, labels) if not l]
    if not pos or not neg:
        raise SystemExit("Validation split needs both positives and negatives")

    def at(t):
        return sum(s >= t for s in pos) / len(pos), sum(s >= t for s in neg) / len(neg)

    op = next(((t / 100, *at(t / 100)) for t in range(5, 100) if at(t / 100)[1] <= budget), (1.0, 0.0, 0.0))
    rec_p4, fa_p4 = at(p4)
    return {'model': str(model_path), 'val_pos': len(pos), 'val_neg': len(neg),
            'budget': budget, 'threshold_at_budget': op[0], 'recall_at_budget': round(op[1], 4),
            'fa_at_budget': round(op[2], 4), 'p4': p4, 'recall_at_p4': round(rec_p4, 4), 'fa_at_p4': round(fa_p4, 4)}


# ----------------------------------------------------------------------------- train / promote
def current_model_and_p4():
    cfg = yaml.safe_load(paths.DETECTION_CONFIG.read_text()) or {}
    det = cfg.get('detection', {})
    return paths.resolve(det.get('model', {}).get('path', 'rabahdev_best.pt')), \
        float(det.get('thresholds', {}).get('log_only', 0.35))


def train(args, data_yaml: Path) -> Path:
    from ultralytics import YOLO
    base = args.base or str(current_model_and_p4()[0])
    print(f"Fine-tuning {base} on {data_yaml} ({args.epochs} epochs, imgsz {args.imgsz}, device {args.device})")
    run = YOLO(base).train(data=str(data_yaml), epochs=args.epochs, imgsz=args.imgsz, batch=args.batch,
                           device=args.device, workers=args.workers, project=str(paths.DATA_DIR / 'training'),
                           name=Path(data_yaml).parent.name, exist_ok=True, patience=max(10, args.epochs // 3),
                           fraction=args.fraction, plots=False, verbose=False)
    best = Path(run.save_dir) / 'weights' / 'best.pt'
    if not best.is_file():
        raise SystemExit(f"Training finished but {best} is missing")
    return best


def promote(candidate: Path, cand: dict, cur: dict, args) -> bool:
    better = (cand['recall_at_budget'] >= cur['recall_at_budget'] + args.min_gain
              and cand['fa_at_p4'] <= cur['fa_at_p4'] + 0.01)
    print(f"Current : recall@{args.budget:.0%}FA={cur['recall_at_budget']:.1%}  P4 recall={cur['recall_at_p4']:.1%} "
          f"FA={cur['fa_at_p4']:.1%}")
    print(f"Candidate: recall@{args.budget:.0%}FA={cand['recall_at_budget']:.1%}  P4 recall={cand['recall_at_p4']:.1%} "
          f"FA={cand['fa_at_p4']:.1%}")
    decision = 'promoted' if (better or args.force) and not args.no_promote else 'rejected'
    entry = {'time': time.time(), 'candidate': str(candidate), 'decision': decision, 'candidate_metrics': cand,
             'current_metrics': cur}
    if decision == 'promoted':
        dst = paths.MODELS_DIR / f"fire_smoke_{time.strftime('%Y%m%d_%H%M%S')}.pt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, dst)
        rel = os.path.relpath(dst, paths.SENTINEL_HOME)
        text = paths.DETECTION_CONFIG.read_text()
        new, n = re.subn(r'(?m)^(\s+path:\s*)\S+', lambda m: m.group(1) + rel, text, count=1)
        if n != 1:
            raise SystemExit("Could not find detection.model.path in config")
        tmp = paths.DETECTION_CONFIG.with_suffix('.yaml.tmp')
        tmp.write_text(new)
        os.replace(tmp, paths.DETECTION_CONFIG)
        entry['installed_as'] = rel
        print(f"PROMOTED -> {rel} (running service hot-swaps within seconds; previous model kept for rollback)")
    else:
        print("NOT promoted: candidate is not better than the current model" + (" (--no-promote)" if args.no_promote else ""))
    paths.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with open(paths.MODELS_DIR / 'MODEL_HISTORY.jsonl', 'a') as f:
        f.write(json.dumps(entry) + '\n')
    return decision == 'promoted'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('build', 'all'):
        p = sub.add_parser(name)
        p.add_argument('--public', action='append')
        p.add_argument('--names', help='class names of the public set(s), comma-separated, if no data.yaml')
        p.add_argument('--feedback')
        p.add_argument('--negatives', action='append')
        p.add_argument('--val-pct', type=int, default=20)
        p.add_argument('--out')
        if name == 'all':
            p.add_argument('--base', help='weights to fine-tune (default: current model)')
            p.add_argument('--epochs', type=int, default=50)
            p.add_argument('--imgsz', type=int, default=640)
            p.add_argument('--batch', type=int, default=16)
            p.add_argument('--device', default='0', help="'0' for first GPU, 'cpu'")
            p.add_argument('--workers', type=int, default=4)
            p.add_argument('--fraction', type=float, default=1.0)
            p.add_argument('--budget', type=float, default=0.02, help='false-alarm budget for evaluation')
            p.add_argument('--min-gain', type=float, default=0.02)
            p.add_argument('--no-promote', action='store_true')
            p.add_argument('--force', action='store_true', help='promote even if not better (not recommended)')
    p = sub.add_parser('evaluate')
    p.add_argument('--model', required=True)
    p.add_argument('--data', required=True)
    p.add_argument('--budget', type=float, default=0.02)
    args = ap.parse_args(argv)

    if args.cmd == 'build':
        build_dataset(args)
    elif args.cmd == 'evaluate':
        print(json.dumps(evaluate(args.model, args.data, args.budget, current_model_and_p4()[1]), indent=1))
    else:
        data = build_dataset(args)
        cand_path = train(args, data)
        cur_path, p4 = current_model_and_p4()
        cur = evaluate(str(cur_path), str(data), args.budget, p4)
        cand = evaluate(str(cand_path), str(data), args.budget, p4)
        promote(cand_path, cand, cur, args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
