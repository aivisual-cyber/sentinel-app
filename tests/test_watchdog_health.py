"""Watchdog state machine and SentinelSystem.get_health() stall detection."""

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from alerts.notification_system import ExternalNotifier

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("watchdog", ROOT / "scripts" / "watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


@pytest.fixture
def servers():
    state = {"health": {"status": "healthy", "issues": []}, "hooks": []}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(state["health"]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            state["hooks"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", state
    srv.shutdown()


def test_watchdog_reports_down_once_then_recovery(tmp_path, servers):
    base, state = servers
    cfg = tmp_path / "n.yaml"
    cfg.write_text(yaml.safe_dump({"channels": {"webhook": {"enabled": True, "url": base + "/hook", "min_level": "P1"}}}))
    n = ExternalNotifier(config_path=str(cfg), db_path=str(tmp_path / "w.db"))
    args = watchdog.main.__globals__["argparse"].Namespace(
        url=base + "/api/health", failures=2, ignore_degraded=False, restart_cmd="", restart_backoff=600)
    st = {"reported": "healthy", "candidate": None, "count": 0, "last_restart": 0}

    st = watchdog.check(args, n, st)                      # healthy: nothing
    state["health"] = {"status": "critical", "issues": ["detection loop stalled for 40s"]}
    st = watchdog.check(args, n, st)                      # 1st bad observation: debounced
    assert state["hooks"] == []
    st = watchdog.check(args, n, st)                      # 2nd: report DOWN
    st = watchdog.check(args, n, st)                      # still down: no repeat
    assert len(state["hooks"]) == 1 and "DOWN" in state["hooks"][0]["subject"]
    assert "stalled" in state["hooks"][0]["message"] and "FIRE" not in state["hooks"][0]["subject"]

    state["health"] = {"status": "healthy", "issues": []}
    st = watchdog.check(args, n, st)
    st = watchdog.check(args, n, st)
    assert len(state["hooks"]) == 2 and "RECOVERED" in state["hooks"][1]["subject"]

    args.url = "http://127.0.0.1:9/none"                  # backend gone entirely
    for _ in range(2):
        st = watchdog.check(args, n, st)
    assert "unreachable" in state["hooks"][2]["subject"]


def _system(last_loop_age, last_det_age, cams, cycle=0.1):
    from main import SentinelSystem
    now = time.time()
    sp = SimpleNamespace(is_processing=True, last_loop_time=now - last_loop_age,
                         last_detection_time=now - last_det_age, process_interval=0.5,
                         get_camera_status=lambda: cams, cycle_seconds=cycle)
    notifier = SimpleNamespace(get_status=lambda: {"webhook": {"enabled": True, "last_error": None, "pending": 0}})
    fake = SimpleNamespace(is_running=True, fire_detector=object(), stream_processor=sp,
                           alert_manager=SimpleNamespace(notifier=notifier), STALL_SECONDS=15.0)
    return SentinelSystem.get_health(fake)


def _cam(connected=True, age=0.5, err=None):
    return {"connected": connected, "last_frame_time": time.time() - age, "last_error": err}


def test_health_states():
    assert _system(0.1, 0.5, {"a": _cam()})["state"] == "healthy"
    h = _system(0.1, 0.5, {"a": _cam(), "b": _cam(False, err="timeout")})
    assert h["state"] == "degraded" and "camera b not streaming (timeout)" in h["issues"]
    assert _system(0.1, 0.5, {"a": _cam(age=60)})["state"] == "critical"     # only camera frozen
    h = _system(40, 0.5, {"a": _cam()})
    assert h["state"] == "critical" and any("stalled" in i for i in h["issues"])
    assert _system(0.1, 60, {"a": _cam()})["state"] == "critical"            # frames not analysed
    h = _system(0.1, 0.5, {"a": _cam()}, cycle=1.2)                           # 0.8 FPS vs target 2
    assert h["state"] == "degraded" and "overloaded" in h["issues"][0]
