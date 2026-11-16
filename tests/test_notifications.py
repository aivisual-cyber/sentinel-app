"""
Unit tests for the app notification system (AlertMessage, rate limiting, storage).
"""

from datetime import datetime

import numpy as np

from backend.alerts.app_notification_system import AlertMessage, AppAlertConfig, AppNotificationManager


def _msg(alert_id="A1", alert_type="P1", confidence=0.97):
    return AlertMessage(alert_id=alert_id, alert_type=alert_type, camera_id="cam_east_01",
                        message="Flame detected in loading bay", confidence=confidence, timestamp=datetime.now())


class TestNotificationSystem:
    def test_alert_message_roundtrip(self):
        msg = _msg()
        restored = AlertMessage.from_dict(msg.to_dict())
        assert restored.alert_id == "A1" and restored.alert_type == "P1" and restored.confidence == 0.97

    def test_rate_limit_blocks_after_hourly_cap(self, tmp_path):
        cfg = AppAlertConfig(max_alerts_per_hour=3, database_path=str(tmp_path / "a.db"),
                             alert_frames_dir=str(tmp_path / "frames"))
        mgr = AppNotificationManager(cfg)
        try:
            results = [mgr._check_rate_limits("P1") for _ in range(5)]
            assert results == [True, True, True, False, False]
        finally:
            mgr.stop_processing()

    def test_alert_persisted_with_frame(self, tmp_path):
        cfg = AppAlertConfig(database_path=str(tmp_path / "a.db"), alert_frames_dir=str(tmp_path / "frames"))
        mgr = AppNotificationManager(cfg)
        try:
            mgr.send_fire_alert(_msg("A2", "P2", 0.88), np.zeros((32, 32, 3), dtype=np.uint8))
            alerts = mgr.get_recent_alerts(1)
            assert [a.alert_id for a in alerts] == ["A2"]
            assert mgr.get_unacknowledged_count()["P2"] == 1
            assert mgr.acknowledge_alert("A2")
            assert mgr.get_unacknowledged_count()["P2"] == 0
        finally:
            mgr.stop_processing()
