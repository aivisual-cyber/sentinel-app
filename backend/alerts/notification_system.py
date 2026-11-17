"""
External alert delivery: email (SMTP), webhook (Slack/Teams/n8n/any HTTP), SMS (Twilio).

Design for a supplementary safety system:
  * Persistent outbox in SQLite - alerts raised while the network is down are delivered
    when it returns (retry with exponential backoff, give up after ``max_age_hours``).
  * Secrets are read from environment variables named in the config, never stored in YAML.
  * Every channel is optional; with none enabled the system still alerts in-app.
  * Delivery status per channel is exposed for the UI.

Config: config/notifications.yaml (see config/notifications.yaml.template).
"""

import json
import logging
import os
import smtplib
import sqlite3
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests
import yaml

try:
    from .. import paths
except ImportError:
    import paths

logger = logging.getLogger(__name__)

LEVEL_RANK = {'P4': 1, 'P2': 2, 'P1': 3}
DEFAULT_CONFIG = {
    'site_name': 'Sentinel site',
    'max_age_hours': 24,
    'channels': {
        'email': {'enabled': False, 'min_level': 'P2', 'smtp_host': '', 'smtp_port': 587, 'starttls': True,
                  'username': '', 'password_env': 'SENTINEL_SMTP_PASSWORD', 'from': '', 'to': [],
                  'attach_frame': True},
        'webhook': {'enabled': False, 'min_level': 'P2', 'url': '', 'url_env': 'SENTINEL_WEBHOOK_URL',
                    'format': 'generic'},  # generic | slack
        'sms': {'enabled': False, 'min_level': 'P1', 'provider': 'twilio', 'account_sid': '',
                'auth_token_env': 'SENTINEL_TWILIO_TOKEN', 'from': '', 'to': []},
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class ExternalNotifier:
    """Delivers alerts to configured external channels through a persistent outbox."""

    def __init__(self, config_path: Optional[str] = None, db_path: Optional[str] = None,
                 alert_lookup: Optional[Callable[[str], Optional[dict]]] = None, poll_seconds: float = 2.0):
        self.config_path = paths.resolve(config_path) if config_path else paths.CONFIG_DIR / 'notifications.yaml'
        self.db_path = paths.resolve(db_path) if db_path else paths.ALERTS_DB
        self.alert_lookup = alert_lookup
        self.poll_seconds = poll_seconds
        self.config = self._load_config()
        self.status: Dict[str, dict] = {c: {'last_success': None, 'last_error': None, 'sent': 0, 'failed': 0}
                                        for c in DEFAULT_CONFIG['channels']}
        self._senders = {'email': self._send_email, 'webhook': self._send_webhook, 'sms': self._send_sms}
        self._wake = threading.Event()
        self._running = False
        self._thread = None
        self._init_db()

    # ------------------------------------------------------------ config/db
    def _load_config(self) -> dict:
        try:
            with open(self.config_path) as f:
                return _merge(DEFAULT_CONFIG, yaml.safe_load(f) or {})
        except FileNotFoundError:
            return _merge(DEFAULT_CONFIG, {})
        except Exception as e:
            logger.error(f"Invalid notifications config {self.config_path}: {e}")
            return _merge(DEFAULT_CONFIG, {})

    def reload(self):
        self.config = self._load_config()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT NOT NULL, channel TEXT NOT NULL,
                payload TEXT NOT NULL, created REAL NOT NULL, attempts INTEGER DEFAULT 0,
                next_attempt REAL NOT NULL, status TEXT DEFAULT 'pending', last_error TEXT,
                UNIQUE(alert_id, channel))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_outbox_pending ON notification_outbox(status, next_attempt)")

    def enabled_channels(self) -> List[str]:
        return [n for n, c in self.config['channels'].items() if c.get('enabled')]

    # ------------------------------------------------------------- public
    def enqueue(self, alert: dict) -> int:
        """Queue an alert for every enabled channel whose min_level it meets. Returns rows queued."""
        rank = LEVEL_RANK.get(alert.get('alert_level'), 0)
        queued = 0
        now = time.time()
        with sqlite3.connect(self.db_path) as c:
            for name, ch in self.config['channels'].items():
                if ch.get('enabled') and rank >= LEVEL_RANK.get(ch.get('min_level', 'P1'), 3):
                    cur = c.execute("INSERT OR IGNORE INTO notification_outbox (alert_id, channel, payload, created, "
                                    "next_attempt) VALUES (?,?,?,?,?)",
                                    (alert['id'], name, json.dumps(alert, default=str), now, now))
                    queued += cur.rowcount
        if queued:
            self._wake.set()
        return queued

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name='notifier', daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_status(self) -> dict:
        with sqlite3.connect(self.db_path) as c:
            pending = dict(c.execute("SELECT channel, COUNT(*) FROM notification_outbox WHERE status='pending' "
                                     "GROUP BY channel").fetchall())
        return {name: {'enabled': bool(self.config['channels'][name].get('enabled')),
                       'min_level': self.config['channels'][name].get('min_level'),
                       'pending': pending.get(name, 0), **self.status[name]}
                for name in self.config['channels']}

    def send_test(self, channel: str) -> tuple:
        """Send a test message immediately (used by the UI 'Send test' button)."""
        if channel not in self._senders:
            return False, f"Unknown channel {channel}"
        alert = {'id': f'TEST_{int(time.time())}', 'alert_level': 'P1', 'camera_id': 'test-camera',
                 'confidence': 0.99, 'timestamp': time.time(), 'test': True}
        try:
            self._senders[channel](self.config['channels'][channel], alert)
            return True, 'Test message sent'
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------- worker
    def process_once(self) -> int:
        """Attempt all due outbox rows once. Returns number delivered."""
        now = time.time()
        max_age = float(self.config.get('max_age_hours', 24)) * 3600
        delivered = 0
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute("SELECT id, alert_id, channel, payload, created, attempts FROM notification_outbox "
                             "WHERE status='pending' AND next_attempt <= ? ORDER BY created LIMIT 50", (now,)).fetchall()
        for row_id, alert_id, channel, payload, created, attempts in rows:
            ch = self.config['channels'].get(channel, {})
            alert = json.loads(payload)
            if self.alert_lookup:  # pick up frame_path saved after the alert was queued
                fresh = self.alert_lookup(alert_id)
                if fresh:
                    alert.update({k: v for k, v in fresh.items() if v is not None})
            try:
                if not ch.get('enabled'):
                    raise RuntimeError('channel disabled')
                self._senders[channel](ch, alert)
                self._finish(row_id, 'sent', None)
                self.status[channel].update(last_success=time.time(), last_error=None)
                self.status[channel]['sent'] += 1
                delivered += 1
            except Exception as e:
                err = str(e)[:500]
                self.status[channel].update(last_error=err)
                self.status[channel]['failed'] += 1
                if now - created > max_age:
                    self._finish(row_id, 'expired', err)
                    logger.error(f"Gave up delivering {alert_id} via {channel}: {err}")
                else:
                    delay = min(30 * 2 ** attempts, 900)  # 30 s, 60 s ... capped at 15 min
                    with sqlite3.connect(self.db_path) as c:
                        c.execute("UPDATE notification_outbox SET attempts=attempts+1, next_attempt=?, last_error=? "
                                  "WHERE id=?", (now + delay, err, row_id))
                    logger.warning(f"Delivery of {alert_id} via {channel} failed (retry in {delay}s): {err}")
        return delivered

    def _finish(self, row_id: int, status: str, err: Optional[str]):
        with sqlite3.connect(self.db_path) as c:
            c.execute("UPDATE notification_outbox SET status=?, last_error=? WHERE id=?", (status, err, row_id))

    def _loop(self):
        while self._running:
            try:
                self.process_once()
            except Exception as e:
                logger.error(f"Notifier loop error: {e}")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    # ------------------------------------------------------------ channels
    def _text(self, alert: dict) -> tuple:
        site = self.config.get('site_name', 'Sentinel')
        if alert.get('kind') == 'system':  # watchdog / health messages, not fire alerts
            subject = f"[SENTINEL SYSTEM] {alert.get('title', 'status change')} - {site}"
            body = (f"{alert.get('message', '')}\nSite: {site}\nTime: "
                    f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(alert.get('timestamp', time.time()))))}\n\n"
                    "While Sentinel is down or degraded it is NOT providing supplementary fire monitoring.")
            return subject, body
        lvl = alert.get('alert_level')
        word = {'P1': 'FIRE ALERT', 'P2': 'Possible fire - verify', 'P4': 'Fire/smoke logged'}.get(lvl, 'Alert')
        ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(alert.get('timestamp', time.time()))))
        test = ' [TEST]' if alert.get('test') else ''
        subject = f"[{lvl}] {word} - {site} - camera {alert.get('camera_id')}{test}"
        body = (f"{word}{test}\nSite: {site}\nCamera: {alert.get('camera_id')}\nLevel: {lvl}\n"
                f"Confidence: {float(alert.get('confidence', 0)):.0%}\nTime: {ts}\nAlert ID: {alert.get('id')}\n\n"
                "Sentinel is a SUPPLEMENTARY early-warning system. Verify visually and follow your site's fire "
                "procedure. Certified fire alarm systems remain primary.")
        return subject, body

    def _send_email(self, ch: dict, alert: dict):
        if not (ch.get('smtp_host') and ch.get('from') and ch.get('to')):
            raise RuntimeError('email channel needs smtp_host, from and to')
        subject, body = self._text(alert)
        msg = EmailMessage()
        msg['Subject'], msg['From'], msg['To'] = subject, ch['from'], ', '.join(ch['to'])
        msg.set_content(body)
        frame = alert.get('frame_path')
        if ch.get('attach_frame', True) and frame and Path(frame).is_file():
            msg.add_attachment(Path(frame).read_bytes(), maintype='image', subtype='jpeg', filename=Path(frame).name)
        with smtplib.SMTP(ch['smtp_host'], int(ch.get('smtp_port', 587)), timeout=15) as s:
            if ch.get('starttls', True):
                s.starttls()
            if ch.get('username'):
                s.login(ch['username'], os.environ.get(ch.get('password_env', ''), ''))
            s.send_message(msg)

    def _send_webhook(self, ch: dict, alert: dict):
        url = ch.get('url') or os.environ.get(ch.get('url_env', ''), '')
        if not url:
            raise RuntimeError('webhook channel needs url or url_env')
        subject, body = self._text(alert)
        if ch.get('format') == 'slack':
            data = {'text': f"*{subject}*\n{body}"}
        else:
            data = {'event': 'sentinel.alert', 'subject': subject, 'message': body,
                    'alert': {k: alert.get(k) for k in ('id', 'alert_level', 'camera_id', 'confidence', 'timestamp',
                                                        'test')}}
        r = requests.post(url, json=data, timeout=10)
        if r.status_code >= 300:
            raise RuntimeError(f"webhook HTTP {r.status_code}: {r.text[:200]}")

    def _send_sms(self, ch: dict, alert: dict):
        sid, token = ch.get('account_sid'), os.environ.get(ch.get('auth_token_env', ''), '')
        if not (sid and token and ch.get('from') and ch.get('to')):
            raise RuntimeError('sms channel needs account_sid, auth_token_env (set), from and to')
        subject, _ = self._text(alert)
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        for number in ch['to']:
            r = requests.post(url, data={'From': ch['from'], 'To': number, 'Body': subject[:300]},
                              auth=(sid, token), timeout=10)
            if r.status_code >= 300:
                raise RuntimeError(f"twilio HTTP {r.status_code}: {r.text[:200]}")
