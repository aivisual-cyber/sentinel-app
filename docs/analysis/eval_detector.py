"""Evaluate Sentinel's FireDetector exactly as the app runs it, on real imagery."""
import sys, glob, os, json, collections
run, imgdir, extra, cfg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
os.chdir(run); sys.path.insert(0, os.path.join(run, 'backend'))
import cv2
from detection.fire_detector import FireDetector
det = FireDetector(cfg)
print("model classes:", len(det.model.names), "| has fire/smoke class:",
      any(n in ('fire','smoke','flame') for n in det.model.names.values()))
print("thresholds:", det.config['detection']['thresholds'])
imgs = sorted(glob.glob(imgdir + '/*.jpg'))
labelled = [p for p in imgs if os.path.getsize(p.replace('/images/','/labels/').rsplit('.',1)[0]+'.txt') > 0] if imgs else []
levels = collections.Counter(); classes = collections.Counter(); raw = collections.Counter()
for p in labelled:
    r = det.detect_fire(cv2.imread(p)); levels[r.alert_level] += 1
    for d in r.detections: classes[d.class_name] += 1
    for res in det.model(cv2.imread(p), verbose=False):
        for b in res.boxes: raw[det.model.names[int(b.cls[0])]] += 1
print(f"\nREAL FIRE IMAGES (ground-truth fire boxes present): {len(labelled)}")
print("  Sentinel alert levels:", dict(levels))
print("  detections Sentinel kept:", dict(classes))
print("  what the model actually saw (top):", raw.most_common(8))
for p in sorted(glob.glob(extra + '/*.jpg')):
    r = det.detect_fire(cv2.imread(p))
    print(f"\nNON-FIRE {os.path.basename(p)} -> alert={r.alert_level} max_conf={r.max_confidence:.3f}",
          [(d.class_name, round(d.confidence,3)) for d in r.detections])
