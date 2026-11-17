#!/usr/bin/env python3
"""
Sentinel watchdog - runs as a SEPARATE process from the detection service.

It polls /api/health and notifies through the configured channels (email / webhook / SMS,
config/notifications.yaml) when Sentinel is unreachable, critical (not analysing frames) or
degraded (e.g. a camera down) for `--failures` consecutive checks, and again on recovery.
Optionally it restarts a hung service (a stalled process that systemd would not restart).

Run continuously:             python scripts/watchdog.py
Or from a systemd timer/cron: python scripts/watchdog.py --once   (state persists between runs)

It does not import the detection stack (no torch/ultralytics), so it keeps working when that breaks.
For whole-PC failures (power, OS, network) also enable the `heartbeat` dead-man's switch.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# Windows services / redirected output use a legacy code page: never crash on a non-ASCII character
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'backend'))
import paths  # noqa: E402
paths.load_env_file()  # notification secrets from SENTINEL_ENV_FILE
from alerts.notification_system import ExternalNotifier  # noqa: E402

STATE_FILE = paths.DATA_DIR / 'watchdog_state.json'


def probe(url: str, timeout: float = 5.0) -> dict:
    try:
        r = requests.get(url, timeout=timeout)
        data = r.json()
        return {'state': data.get('status', 'critical'), 'issues': data.get('issues') or []}
    except Exception as e:
        return {'state': 'unreachable', 'issues': [f'health endpoint unreachable: {e.__class__.__name__}']}


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {'reported': 'healthy', 'candidate': None, 'count': 0, 'last_restart': 0}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st))


def check(args, notifier: ExternalNotifier, st: dict) -> dict:
    h = probe(args.url)
    observed = h['state'] if h['state'] != 'degraded' or not args.ignore_degraded else 'healthy'
    if observed == st['reported']:
        st.update(candidate=None, count=0)
    else:
        st['count'] = st['count'] + 1 if st.get('candidate') == observed else 1
        st['candidate'] = observed
        if st['count'] >= args.failures:  # persistent change -> report it once
            title = {'healthy': 'RECOVERED - monitoring restored', 'degraded': 'DEGRADED - partial monitoring',
                     'critical': 'DOWN - detection not running', 'unreachable': 'DOWN - backend unreachable'}[observed]
            msg = f"Sentinel status: {observed.upper()}." + ('' if observed == 'healthy' else
                                                              '\nIssues:\n- ' + '\n- '.join(h['issues']))
            n = notifier.enqueue({'id': f"WATCHDOG_{observed}_{int(time.time())}", 'kind': 'system',
                                  'alert_level': 'P1', 'camera_id': 'system', 'confidence': 1.0,
                                  'timestamp': time.time(), 'title': title, 'message': msg})
            print(f"[watchdog] {title} (queued to {n} channel(s)); issues={h['issues']}", flush=True)
            st.update(reported=observed, candidate=None, count=0)
    bad = observed in ('critical', 'unreachable')
    if bad and args.restart_cmd and st['reported'] == observed and time.time() - st.get('last_restart', 0) > args.restart_backoff:
        print(f"[watchdog] running restart command: {args.restart_cmd}", flush=True)
        subprocess.run(args.restart_cmd, shell=True, timeout=120)
        st['last_restart'] = time.time()
    notifier.process_once()  # deliver now; failures stay in the outbox for the next run
    return st


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default='http://127.0.0.1:8765/api/health')
    ap.add_argument('--interval', type=float, default=30, help='seconds between checks (continuous mode)')
    ap.add_argument('--failures', type=int, default=3, help='consecutive observations before notifying')
    ap.add_argument('--once', action='store_true', help='single check (for systemd timers / cron)')
    ap.add_argument('--ignore-degraded', action='store_true', help='only alert on down, not degraded')
    ap.add_argument('--restart-cmd', default='', help="e.g. 'systemctl restart sentinel'")
    ap.add_argument('--restart-backoff', type=float, default=600, help='min seconds between restarts')
    args = ap.parse_args(argv)

    notifier = ExternalNotifier(db_path=str(paths.DATA_DIR / 'watchdog.db'))
    if not notifier.enabled_channels():
        print("[watchdog] WARNING: no notification channels enabled in config/notifications.yaml - "
              "status changes will only be printed", flush=True)
    st = load_state()
    while True:
        st = check(args, notifier, st)
        save_state(st)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == '__main__':
    sys.exit(main())
