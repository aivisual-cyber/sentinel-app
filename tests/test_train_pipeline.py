"""Training pipeline: dataset building (class remap, negatives, feedback) and the promotion gate."""

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("train_model", ROOT / "scripts" / "train_model.py")
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)


def _img(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(p), np.zeros((32, 32, 3), np.uint8))


def test_build_dataset_remaps_classes_and_includes_all_sources(tmp_path):
    pub = tmp_path / "pub"
    (pub / "data.yaml").parent.mkdir(parents=True)
    (pub / "data.yaml").write_text(yaml.safe_dump({"names": ["FIRE", "Smoke", "person"]}))
    for i in range(6):
        _img(pub / "train" / "images" / f"a{i}.jpg")
        (pub / "train" / "labels").mkdir(parents=True, exist_ok=True)
        (pub / "train" / "labels" / f"a{i}.txt").write_text("0 .5 .5 .2 .2\n1 .3 .3 .1 .1\n2 .1 .1 .1 .1\n")
    neg = tmp_path / "neg"
    for i in range(4):
        _img(neg / f"n{i}.jpg")
    fb = tmp_path / "fb"
    _img(fb / "images" / "false_alarm_x.jpg")
    (fb / "labels").mkdir(parents=True)
    (fb / "labels" / "false_alarm_x.txt").write_text("")

    data = tm.build_dataset(Namespace(public=[str(pub)], names=None, feedback=str(fb), negatives=[str(neg)],
                                      val_pct=30, out=str(tmp_path / "ds")))
    ds = Path(yaml.safe_load(data.read_text())["path"])
    labels = [p.read_text() for p in ds.rglob("labels/*/*.txt")]
    pos = [l for l in labels if l.strip()]
    assert len(labels) == 11 and len(pos) == 6
    assert all(l.splitlines() == ["1 .5 .5 .2 .2", "0 .3 .3 .1 .1"] for l in pos)  # FIRE->1, Smoke->0, person dropped
    stats = json.loads((ds / "stats.json").read_text())
    assert set(stats) == {"pub0", "site", "neg0"}
    assert yaml.safe_load(data.read_text())["names"] == {0: "smoke", 1: "fire"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "detection_config.yaml"
    cfg.parent.mkdir()
    cfg.write_text((ROOT / "config" / "detection_config.yaml.template").read_text())
    monkeypatch.setattr(tm.paths, "SENTINEL_HOME", tmp_path)
    monkeypatch.setattr(tm.paths, "DETECTION_CONFIG", cfg)
    monkeypatch.setattr(tm.paths, "MODELS_DIR", tmp_path / "models")
    cand = tmp_path / "best.pt"
    cand.write_bytes(b"weights")
    return tmp_path, cfg, cand


def _m(rec_budget, fa_p4):
    return {"recall_at_budget": rec_budget, "recall_at_p4": 0.7, "fa_at_p4": fa_p4}


def _args(**kw):
    return Namespace(budget=0.02, min_gain=0.02, no_promote=False, force=False, **kw)


def test_promotion_gate(home):
    tmp, cfg, cand = home
    # better recall but more false alarms at P4 -> rejected, config untouched
    assert not tm.promote(cand, _m(0.5, 0.21), _m(0.1, 0.09), _args())
    assert "rabahdev_best.pt" in cfg.read_text()
    # better on both -> promoted, config points at the new model, comments preserved
    assert tm.promote(cand, _m(0.5, 0.08), _m(0.1, 0.09), _args())
    text = cfg.read_text()
    assert "models/fire_smoke_" in text and "# relative to SENTINEL_HOME" in text
    hist = [json.loads(l) for l in (tmp / "models" / "MODEL_HISTORY.jsonl").read_text().splitlines()]
    assert [h["decision"] for h in hist] == ["rejected", "promoted"]
