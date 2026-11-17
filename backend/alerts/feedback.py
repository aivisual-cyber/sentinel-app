"""
Operator feedback loop: verdicts, site-trial report, and the site training dataset.

Why: whether Sentinel is fit for a site can only be decided from *that site's* data.
Every alert can be marked by the operator as a real fire, a false alarm, or a test.
That gives:
  * real precision and false-alarms-per-day per camera (trial report / Analytics),
  * a YOLO-format dataset under data/feedback/ (false alarms = hard negatives with empty
    label files; confirmed fires = positives pseudo-labelled from the alert's boxes),
    consumed by scripts/train_model.py.
"""

import csv
import io
import json
import shutil
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Dict, Optional

import cv2

try:
    from .. import paths
except ImportError:
    import paths

VERDICTS = ('fire', 'false_alarm', 'test')
# Training dataset class ids (matches the bundled smoke/fire model)
DATASET_CLASSES = {'smoke': 0, 'fire': 1, 'flame': 1}
FEEDBACK_DIR = paths.DATA_DIR / 'feedback'

MIGRATIONS = {
    'acknowledged_at': 'REAL',
    'verdict': 'TEXT',
    'verdict_at': 'REAL',
    'verdict_notes': 'TEXT',
    'raw_frame_path': 'TEXT',
}


def migrate(db_path) -> None:
    """Add feedback columns to an existing alerts table (idempotent)."""
    with sqlite3.connect(db_path) as c:
        have = {r[1] for r in c.execute("PRAGMA table_info(alerts)")}
        for col, typ in MIGRATIONS.items():
            if col not in have:
                c.execute(f"ALTER TABLE alerts ADD COLUMN {col} {typ}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_camera_ts ON alerts(camera_id, timestamp)")


def set_verdict(db_path, alert_id: str, verdict: str, notes: str = '',
                feedback_dir: Optional[Path] = None) -> Dict:
    """Record an operator verdict, acknowledge the alert, and export it to the feedback dataset."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    now = time.time()
    with sqlite3.connect(db_path) as c:
        row = c.execute("SELECT detections, raw_frame_path, status FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if not row:
            raise KeyError(alert_id)
        c.execute("""UPDATE alerts SET verdict = ?, verdict_at = ?, verdict_notes = ?,
                     status = CASE WHEN status = 'active' THEN 'acknowledged' ELSE status END,
                     acknowledged_at = COALESCE(acknowledged_at, ?) WHERE id = ?""",
                  (verdict, now, notes, now, alert_id))
    exported = None
    if verdict != 'test' and row[1] and Path(row[1]).is_file():
        exported = _export_example(alert_id, verdict, json.loads(row[0] or '[]'), Path(row[1]),
                                   feedback_dir or FEEDBACK_DIR)
    return {'alert_id': alert_id, 'verdict': verdict, 'exported': exported}


def _export_example(alert_id: str, verdict: str, detections, raw: Path, root: Path) -> Optional[str]:
    img = cv2.imread(str(raw))
    if img is None:
        return None
    h, w = img.shape[:2]
    (root / 'images').mkdir(parents=True, exist_ok=True)
    (root / 'labels').mkdir(parents=True, exist_ok=True)
    dst = root / 'images' / f"{verdict}_{alert_id}.jpg"
    shutil.copyfile(raw, dst)
    lines = []
    if verdict == 'fire':  # pseudo-labels from the alert's own boxes (review before training if possible)
        for d in detections:
            cls = DATASET_CLASSES.get(str(d.get('class_name', '')).lower())
            if cls is None:
                continue
            x1, y1, x2, y2 = d['bbox']
            lines.append(f"{cls} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}")
    # false_alarm -> empty label file = hard negative
    (root / 'labels' / f"{verdict}_{alert_id}.txt").write_text("\n".join(lines))
    return str(dst)


def trial_report(db_path, days: float = 7.0) -> Dict:
    """Per-camera and overall site-trial metrics from operator verdicts."""
    since = time.time() - days * 86400
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT id, timestamp, camera_id, alert_level, confidence, status, verdict, acknowledged_at "
            "FROM alerts WHERE timestamp > ? ORDER BY timestamp", (since,))]
    span_days = max((time.time() - rows[0]['timestamp']) / 86400, 1 / 24) if rows else days

    def summarise(items):
        fire = sum(r['verdict'] == 'fire' for r in items)
        fa = sum(r['verdict'] == 'false_alarm' for r in items)
        ack = [r['acknowledged_at'] - r['timestamp'] for r in items if r['acknowledged_at']]
        return {
            'alerts': len(items),
            'by_level': {lvl: sum(r['alert_level'] == lvl for r in items) for lvl in ('P1', 'P2', 'P4')},
            'verified_fire': fire,
            'false_alarms': fa,
            'tests': sum(r['verdict'] == 'test' for r in items),
            'unverified': sum(r['verdict'] is None for r in items),
            'precision': round(fire / (fire + fa), 3) if fire + fa else None,
            'false_alarms_per_day': round(fa / span_days, 2),
            'p1_false_alarms_per_day': round(sum(r['verdict'] == 'false_alarm' and r['alert_level'] == 'P1'
                                                 for r in items) / span_days, 2),
            'median_ack_seconds': round(statistics.median(ack), 1) if ack else None,
        }

    cams = sorted({r['camera_id'] for r in rows})
    return {
        'days': days, 'observed_days': round(span_days, 2), 'generated_at': time.time(),
        'overall': summarise(rows),
        'cameras': {cam: summarise([r for r in rows if r['camera_id'] == cam]) for cam in cams},
        'verified_fraction': round(sum(r['verdict'] is not None for r in rows) / len(rows), 3) if rows else None,
    }


def trial_csv(db_path, days: float = 7.0) -> str:
    since = time.time() - days * 86400
    with sqlite3.connect(db_path) as c:
        rows = c.execute("SELECT id, timestamp, camera_id, alert_level, confidence, status, verdict, "
                         "verdict_notes, acknowledged_at FROM alerts WHERE timestamp > ? ORDER BY timestamp",
                         (since,)).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['alert_id', 'time', 'camera_id', 'level', 'confidence', 'status', 'verdict', 'notes',
                'seconds_to_acknowledge'])
    for aid, ts, cam, lvl, conf, status, verdict, notes, ack in rows:
        w.writerow([aid, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)), cam, lvl, f"{conf:.3f}", status,
                    verdict or '', notes or '', f"{ack - ts:.0f}" if ack else ''])
    return out.getvalue()
