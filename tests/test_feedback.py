"""Operator verdicts -> trial metrics + YOLO feedback dataset."""

import time

import cv2
import numpy as np
import pytest

from alerts import feedback
from alerts.alert_manager import AlertManager
from detection.fire_detector import Detection, DetectionResult


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", tmp_path / "feedback")
    m = AlertManager(db_path=str(tmp_path / "alerts.db"), cooldown_seconds=0)
    yield m
    m.stop()


def _alert(mgr, tmp_path, cam, level, cls="fire", bbox=(100, 50, 300, 250)):
    r = DetectionResult(1, time.time(), [Detection(0.8, bbox, cls, time.time())], 0.8, level)
    a = mgr.create_alert(cam, r)
    raw = tmp_path / f"{a.id}_raw.jpg"
    cv2.imwrite(str(raw), np.zeros((400, 640, 3), np.uint8))
    mgr.attach_frame(a, str(tmp_path / "annotated.jpg"), str(raw))
    return a


def test_verdicts_drive_report_and_dataset(mgr, tmp_path):
    a1 = _alert(mgr, tmp_path, "kitchen", "P1")
    a2 = _alert(mgr, tmp_path, "kitchen", "P2")
    a3 = _alert(mgr, tmp_path, "lobby", "P1")
    _alert(mgr, tmp_path, "lobby", "P4")  # left unverified

    mgr.set_verdict(a1.id, "fire")
    mgr.set_verdict(a2.id, "false_alarm", "sunlight on steel")
    mgr.set_verdict(a3.id, "false_alarm")

    rep = mgr.trial_report(7)
    assert rep["overall"]["verified_fire"] == 1 and rep["overall"]["false_alarms"] == 2
    assert rep["overall"]["precision"] == pytest.approx(1 / 3, abs=1e-3)
    assert rep["overall"]["unverified"] == 1
    assert rep["cameras"]["kitchen"]["precision"] == 0.5 and rep["cameras"]["lobby"]["precision"] == 0.0
    assert rep["overall"]["median_ack_seconds"] is not None

    # verdict acknowledges the alert and survives frame attachment (no INSERT OR REPLACE reset)
    d = {x["id"]: x for x in mgr.get_dashboard_data(2)["recent_alerts"]}
    assert d[a1.id]["status"] == "acknowledged" and d[a1.id]["verdict"] == "fire"

    labels = tmp_path / "feedback" / "labels"
    fire_lbl = (labels / f"fire_{a1.id}.txt").read_text().split()
    assert fire_lbl[0] == "1"  # class fire
    assert [round(float(v), 4) for v in fire_lbl[1:]] == [0.3125, 0.375, 0.3125, 0.5]
    assert (labels / f"false_alarm_{a2.id}.txt").read_text() == ""  # hard negative
    assert (tmp_path / "feedback" / "images" / f"false_alarm_{a2.id}.jpg").exists()

    csv_text = mgr.trial_csv(7)
    assert "sunlight on steel" in csv_text and csv_text.count("\n") == 5


def test_invalid_verdict_and_unknown_alert(mgr, tmp_path):
    a = _alert(mgr, tmp_path, "c", "P2")
    with pytest.raises(ValueError):
        mgr.set_verdict(a.id, "maybe")
    with pytest.raises(KeyError):
        mgr.set_verdict("nope", "fire")


def test_test_verdict_not_exported(mgr, tmp_path):
    a = _alert(mgr, tmp_path, "c", "P1")
    assert mgr.set_verdict(a.id, "test")["exported"] is None
    assert mgr.trial_report(1)["overall"]["tests"] == 1
