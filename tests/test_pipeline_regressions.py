"""
Regression tests for the detection -> alert pipeline bugs documented in docs/ANALYSIS_AND_PLAN.md.

Each test fails on the pre-fix code.
"""

import time
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from alerts.alert_manager import AlertManager
from config.camera_config import CameraConfigManager, CameraProfile
from config.config_manager import ConfigManager
from detection.fire_detector import Detection, DetectionResult, FireDetector
from main import TemporalConfirmer

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).parent.parent


def _result(level, conf=0.9):
    # bbox deliberately numpy.int64 - this used to make every alert fail to save
    bbox = tuple(np.array([10, 20, 110, 220], dtype=np.int64))
    return DetectionResult(1, time.time(), [Detection(conf, bbox, "fire", time.time())], conf, level)


@pytest.fixture
def alert_manager(tmp_path):
    mgr = AlertManager(db_path=str(tmp_path / "alerts.db"), cooldown_seconds=60)
    yield mgr
    mgr.stop()


class TestAlertPersistence:
    def test_numpy_bbox_alert_is_stored_and_visible(self, alert_manager):
        alert = alert_manager.create_alert("cam1", _result("P1"))
        assert alert is not None
        dash = alert_manager.get_dashboard_data(1)
        assert [a["id"] for a in dash["recent_alerts"]] == [alert.id]
        assert dash["system_status"]["active_alerts"] == 1

    def test_acknowledge_persists(self, alert_manager):
        alert = alert_manager.create_alert("cam1", _result("P2"))
        assert alert_manager.acknowledge_alert(alert.id)
        assert alert_manager.get_dashboard_data(1)["recent_alerts"][0]["status"] == "acknowledged"

    def test_cooldown_suppresses_duplicates_but_allows_escalation(self, alert_manager):
        assert alert_manager.create_alert("cam1", _result("P4", 0.4)) is not None
        assert alert_manager.create_alert("cam1", _result("P4", 0.4)) is None   # duplicate
        assert alert_manager.create_alert("cam1", _result("P1", 0.8)) is not None  # escalation
        assert alert_manager.create_alert("cam1", _result("P2", 0.6)) is None   # lower level
        assert alert_manager.create_alert("cam2", _result("P2", 0.6)) is not None  # other camera
        assert alert_manager.get_dashboard_data(2)["statistics"]["total"] == 3


class TestCameraPersistence:
    def test_cameras_survive_restart(self, tmp_path):
        path = tmp_path / "cameras.yaml"
        mgr = CameraConfigManager(str(path))
        mgr.add_camera(CameraProfile(camera_id="c1", name="Kitchen", rtsp_url="rtsp://h/s",
                                     resolution=(1920, 1080), password="p@ss:w/rd"))
        assert "!!python" not in path.read_text()
        assert (path.stat().st_mode & 0o777) == 0o600
        reloaded = CameraConfigManager(str(path))
        assert reloaded.cameras["c1"].name == "Kitchen"
        assert reloaded.cameras["c1"].password == "p@ss:w/rd"

    def test_legacy_schema_loads(self, tmp_path):
        path = tmp_path / "cameras.yaml"
        path.write_text(yaml.safe_dump({"cameras": [{"id": "hp", "name": "HP", "url": "rtsp://x/y",
                                                     "username": "", "fps_limit": 10, "detection_enabled": True}]}))
        cam = CameraConfigManager(str(path)).cameras["hp"]
        assert cam.rtsp_url == "rtsp://x/y" and cam.fps == 10 and cam.username is None


class TestThresholdConfig:
    def _cfg(self, tmp_path):
        p = tmp_path / "detection_config.yaml"
        p.write_text((REPO / "config" / "detection_config.yaml.template").read_text())
        return p

    def test_rejects_out_of_range_and_unknown_names(self, tmp_path):
        cm = ConfigManager(str(self._cfg(tmp_path)))
        assert not cm.update_threshold("immediate_alert", 5.0)
        assert not cm.update_threshold("__anything__", 0.1)
        assert not cm.update_threshold("log_only", 0.9)  # breaks ordering
        assert cm.update_threshold("immediate_alert", 0.8)

    def test_detector_hot_reloads_thresholds(self, tmp_path):
        p = self._cfg(tmp_path)
        det = FireDetector(str(p))
        assert det._determine_alert_level(0.7) == "P2"
        time.sleep(0.01)
        cm = ConfigManager(str(p))
        assert cm.update_threshold("immediate_alert", 0.65)
        det.maybe_reload_config()
        assert det._determine_alert_level(0.7) == "P1"


class TestTemporalConfirmation:
    def test_single_frame_spike_is_not_confirmed(self):
        t = TemporalConfirmer(window=5, min_hits=3)
        assert [t.update("c", lvl) for lvl in ("P1", "None", "None", "None")] == ["None"] * 4

    def test_persistent_fire_is_confirmed_at_highest_sustained_level(self):
        t = TemporalConfirmer(window=5, min_hits=3)
        levels = [t.update("c", lvl) for lvl in ("P2", "P1", "P2", "P1", "P1")]
        assert levels[2] == "P2" and levels[-1] == "P1"


class TestRealFireDetection:
    """The shipped COCO model produced 0 alerts on 100 real fire photos; this guards against that."""

    @pytest.fixture(scope="class")
    def detector(self):
        return FireDetector(str(REPO / "config" / "detection_config.yaml.template"))

    @pytest.mark.parametrize("name", ["fire_1.jpg", "fire_2.jpg"])
    def test_real_fire_image_raises_alert(self, detector, name):
        r = detector.detect_fire(cv2.imread(str(FIXTURES / name)))
        assert r.alert_level in ("P1", "P2"), (r.alert_level, r.max_confidence)
        assert all(isinstance(v, int) for d in r.detections for v in d.bbox)

    def test_blank_frame_raises_nothing(self, detector):
        assert detector.detect_fire(np.zeros((480, 640, 3), dtype=np.uint8)).alert_level == "None"
