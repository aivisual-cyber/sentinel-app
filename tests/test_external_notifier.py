"""ExternalNotifier: channel filtering, real HTTP webhook delivery, email composition, offline retry."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from alerts.notification_system import ExternalNotifier


def _alert(level="P1", aid="cam1_1"):
    return {"id": aid, "alert_level": level, "camera_id": "cam1", "confidence": 0.91, "timestamp": time.time()}


@pytest.fixture
def webhook_server():
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/hook", received
    srv.shutdown()


def _notifier(tmp_path, channels):
    cfg = tmp_path / "notifications.yaml"
    cfg.write_text(yaml.safe_dump({"site_name": "Test site", "channels": channels}))
    return ExternalNotifier(config_path=str(cfg), db_path=str(tmp_path / "a.db"))


def test_webhook_delivered_and_min_level_respected(tmp_path, webhook_server):
    url, received = webhook_server
    n = _notifier(tmp_path, {"webhook": {"enabled": True, "url": url, "min_level": "P2"}})
    assert n.enqueue(_alert("P4", "a")) == 0          # below min_level
    assert n.enqueue(_alert("P1", "b")) == 1
    assert n.enqueue(_alert("P1", "b")) == 0          # idempotent per alert/channel
    assert n.process_once() == 1
    assert received[0]["alert"]["id"] == "b" and "FIRE ALERT" in received[0]["subject"]
    assert n.get_status()["webhook"]["sent"] == 1


def test_offline_alert_is_retried_until_network_returns(tmp_path, webhook_server):
    url, received = webhook_server
    n = _notifier(tmp_path, {"webhook": {"enabled": True, "url": "http://127.0.0.1:9/down", "min_level": "P1"}})
    n.enqueue(_alert("P1", "c"))
    assert n.process_once() == 0
    assert n.get_status()["webhook"]["pending"] == 1 and n.get_status()["webhook"]["last_error"]
    # network/endpoint restored: fix config and make the row due now
    n.config["channels"]["webhook"]["url"] = url
    import sqlite3
    with sqlite3.connect(n.db_path) as c:
        c.execute("UPDATE notification_outbox SET next_attempt = 0")
    assert n.process_once() == 1 and received[0]["alert"]["id"] == "c"


def test_email_composed_with_frame(tmp_path, monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, user, pwd):
            sent["login"] = (user, pwd)

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setenv("SENTINEL_SMTP_PASSWORD", "s3cret")
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8jpeg")
    n = _notifier(tmp_path, {"email": {"enabled": True, "smtp_host": "smtp.test", "username": "u", "from": "a@x",
                                       "to": ["b@x"], "min_level": "P2"}})
    a = _alert("P2", "d")
    a["frame_path"] = str(frame)
    n.enqueue(a)
    assert n.process_once() == 1
    msg = sent["msg"]
    assert sent["login"] == ("u", "s3cret") and sent["tls"]
    assert "[P2]" in msg["Subject"] and any(p.get_filename() == "f.jpg" for p in msg.iter_attachments())


def test_disabled_by_default(tmp_path):
    n = ExternalNotifier(config_path=str(tmp_path / "missing.yaml"), db_path=str(tmp_path / "a.db"))
    assert n.enabled_channels() == [] and n.enqueue(_alert()) == 0
