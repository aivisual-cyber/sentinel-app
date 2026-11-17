"""Live preview stream, adaptive (burst) sampling, time-aware confirmation, tool path discovery, parity."""

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'scripts'))

import api_server  # noqa: E402
from main import TemporalConfirmer  # noqa: E402
from utils.video_simulator import StreamProcessor  # noqa: E402


class _FakeCam:
    camera_id, kind = 'cam1', 'file'

    def __init__(self):
        self.frame_count, self._frame = 0, np.zeros((360, 640, 3), np.uint8)
        self.last_frame_time = time.time()

    def tick(self):
        self.frame_count += 1
        self._frame[:] = self.frame_count % 255

    def get_frame(self):
        return self._frame.copy()


class TestBurstSampling:
    def _run(self, candidate: bool, seconds=1.5):
        calls = []

        def cb(frames):
            calls.append(time.time())
            return set(frames) if candidate else set()

        sp = StreamProcessor(cb, process_fps=2, burst_fps=10, burst_hold=5)
        cam = _FakeCam()
        sp.simulator.cameras.append(cam)
        sp.is_processing = True
        import threading
        t = threading.Thread(target=sp._process_loop, daemon=True)
        t.start()
        end = time.time() + seconds
        while time.time() < end:
            cam.tick()
            time.sleep(0.01)
        sp.is_processing = False
        t.join(2)
        return len(calls)

    def test_candidate_switches_camera_to_burst_rate(self):
        normal, burst = self._run(False), self._run(True)
        assert normal <= 4                  # ~2 FPS for 1.5 s
        assert burst >= 2.5 * normal        # ~10 FPS once a candidate was seen

    def test_burst_never_slower_than_normal(self):
        sp = StreamProcessor(None, process_fps=5, burst_fps=1)
        assert sp.burst_interval == sp.process_interval


class TestTimeAwareConfirmation:
    def test_hits_far_apart_in_time_do_not_combine(self):
        c = TemporalConfirmer(window=5, min_hits=3)
        assert c.update('c', 'P1', now=0) == 'None'
        assert c.update('c', 'P1', now=1) == 'None'
        # camera froze / reconnected: next hit 60 s later must not complete the earlier two
        assert c.update('c', 'P1', now=61) == 'None'
        assert c.update('c', 'P1', now=61.2) == 'None'
        assert c.update('c', 'P1', now=61.4) == 'P1'

    def test_close_hits_confirm(self):
        c = TemporalConfirmer(window=5, min_hits=3)
        assert [c.update('c', 'P2', now=t) for t in (0, 0.2, 0.4)][-1] == 'P2'


class _Processor:
    def __init__(self, cam):
        self.simulator = type('S', (), {'get': lambda _self, cid: cam if cid == cam.camera_id else None})()
        self.is_processing = True


@pytest.fixture
def stream_server(monkeypatch):
    monkeypatch.setattr(api_server, 'API_TOKEN', 'tok')
    srv = api_server.SentinelAPIServer.__new__(api_server.SentinelAPIServer)
    srv.is_running, srv._preview_cache = True, {}
    cam = _FakeCam()
    cam.tick()
    srv._detection_running = lambda: True
    srv.sentinel_system = type('Sys', (), {'stream_processor': _Processor(cam)})()
    app = web.Application(middlewares=[api_server.auth_middleware])
    app.router.add_get('/api/cameras/{camera_id}/stream.mjpg', srv.stream_camera)
    return app, cam, srv


@pytest.mark.asyncio
async def test_mjpeg_stream_sends_new_frames(aiohttp_client, stream_server):
    app, cam, srv = stream_server
    client = await aiohttp_client(app)
    assert (await client.get('/api/cameras/cam1/stream.mjpg')).status == 401          # key required
    resp = await client.get('/api/cameras/cam1/stream.mjpg?token=tok&fps=20&w=320')
    assert resp.status == 200 and resp.headers['Content-Type'].startswith('multipart/x-mixed-replace')
    data, end = b'', time.time() + 1.0
    while time.time() < end:
        cam.tick()
        try:
            data += await asyncio.wait_for(resp.content.read(65536), 0.2)
        except asyncio.TimeoutError:
            pass
    srv.is_running = False
    frames = data.count(b'--frame')
    assert frames >= 8                                # ~20 FPS requested, >= 8 in 1 s
    assert data.count(b'\xff\xd8') >= frames - 1      # JPEG payloads
    assert (await client.get('/api/cameras/nope/stream.mjpg?token=tok')).status == 404


def test_access_log_never_contains_query(caplog):
    class Req:
        remote, method, path = '127.0.0.1', 'GET', '/api/cameras/c/stream.mjpg'

    import logging
    lg = api_server._AccessLogger(logging.getLogger('acc'), '')
    with caplog.at_level(logging.INFO, logger='acc'):
        lg.log(Req(), type('R', (), {'status': 200})(), 0.1)
    assert 'token' not in caplog.text and '/stream.mjpg' in caplog.text


def test_tools_find_data_dir_from_install_marker(tmp_path, monkeypatch):
    install, data = tmp_path / 'Sentinel', tmp_path / 'SentinelData'
    (install / 'app' / 'backend').mkdir(parents=True)
    data.mkdir()
    (install / 'sentinel-install.json').write_text(json.dumps({'DataDir': str(data)}))
    shutil_copy = __import__('shutil').copy
    shutil_copy(ROOT / 'backend' / 'paths.py', install / 'app' / 'backend' / 'paths.py')
    monkeypatch.delenv('SENTINEL_HOME', raising=False)
    monkeypatch.syspath_prepend(str(install / 'app' / 'backend'))
    sys.modules.pop('paths', None)
    try:
        mod = importlib.import_module('paths')
        assert mod.SENTINEL_HOME == data.resolve()
    finally:
        sys.modules.pop('paths', None)
        importlib.import_module('paths')


@pytest.mark.skipif(not (ROOT / 'tests' / 'parity' / 'golden.json').exists(), reason='no reference set')
def test_parity_check_passes_on_cpu():
    pytest.importorskip('onnxruntime')
    import parity_check
    assert parity_check.main(['--providers', 'CPUExecutionProvider']) == 0
