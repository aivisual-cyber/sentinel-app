import React, { useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { AlertTriangle, BellOff, CheckCircle2, Eye } from 'lucide-react';

/**
 * Operator alarm for unacknowledged P1/P2 alerts.
 * - P1: full-width red banner + repeating two-tone siren until acknowledged or silenced.
 * - P2: amber banner + single chime when it first appears.
 * Sound is synthesised with Web Audio (no asset files, works offline).
 */
const RECENT_SECONDS = 15 * 60; // only alarm for alerts raised in the last 15 minutes
const SILENCE_MS = 5 * 60 * 1000;

let audioCtx = null;
const ctx = () => {
  if (!audioCtx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    audioCtx = new AC();
  }
  if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
  return audioCtx;
};

const tone = (freq, start, dur, gain = 0.25) => {
  const c = ctx();
  if (!c) return;
  const o = c.createOscillator();
  const g = c.createGain();
  o.type = 'square';
  o.frequency.value = freq;
  g.gain.setValueAtTime(gain, c.currentTime + start);
  g.gain.exponentialRampToValueAtTime(0.0001, c.currentTime + start + dur);
  o.connect(g).connect(c.destination);
  o.start(c.currentTime + start);
  o.stop(c.currentTime + start + dur + 0.02);
};

const siren = () => { for (let i = 0; i < 4; i++) { tone(960, i * 0.5, 0.24); tone(720, i * 0.5 + 0.25, 0.24); } };
const chime = () => { tone(880, 0, 0.18, 0.15); tone(1320, 0.2, 0.3, 0.15); };

const notifyDesktop = (alert) => {
  try {
    if (!('Notification' in window)) return;
    const show = () => new Notification(`🔥 ${alert.alert_level} fire alert – camera ${alert.camera_id}`, {
      body: `Confidence ${(alert.confidence * 100).toFixed(0)}%. Verify visually and follow site fire procedure.`,
      requireInteraction: alert.alert_level === 'P1',
      tag: alert.id,
    });
    if (Notification.permission === 'granted') show();
    else if (Notification.permission !== 'denied') Notification.requestPermission().then(p => p === 'granted' && show());
  } catch { /* notifications unavailable */ }
};

const AlarmBanner = ({ alerts, onAcknowledge, onView }) => {
  const [silencedUntil, setSilencedUntil] = useState(0);
  const seen = useRef(new Set());

  const now = Date.now() / 1000;
  const urgent = useMemo(() => alerts
    .filter(a => a.status === 'active' && (a.alert_level === 'P1' || a.alert_level === 'P2')
      && now - a.timestamp < RECENT_SECONDS)
    .sort((a, b) => (a.alert_level === b.alert_level ? b.timestamp - a.timestamp : a.alert_level === 'P1' ? -1 : 1)),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [alerts]);

  const top = urgent[0];
  const hasP1 = urgent.some(a => a.alert_level === 'P1');

  // New alerts: desktop notification once, chime for P2
  useEffect(() => {
    urgent.forEach(a => {
      if (seen.current.has(a.id)) return;
      seen.current.add(a.id);
      notifyDesktop(a);
      if (a.alert_level === 'P2' && Date.now() > silencedUntil) chime();
    });
  }, [urgent, silencedUntil]);

  // Repeating siren while any P1 is unacknowledged and not silenced
  useEffect(() => {
    if (!hasP1 || Date.now() < silencedUntil) return undefined;
    siren();
    const id = setInterval(() => { if (Date.now() >= silencedUntil) siren(); }, 4000);
    return () => clearInterval(id);
  }, [hasP1, silencedUntil]);

  // Browsers only allow audio after a user gesture: unlock on first interaction
  useEffect(() => {
    const unlock = () => ctx();
    window.addEventListener('pointerdown', unlock, { once: true });
    return () => window.removeEventListener('pointerdown', unlock);
  }, []);

  if (!top) return null;
  const isP1 = top.alert_level === 'P1';
  const silenced = Date.now() < silencedUntil;

  return (
    <div role="alert" aria-live="assertive"
      className={`w-full px-6 py-3 flex items-center justify-between gap-4 border-b ${isP1
        ? 'bg-red-700 border-red-500 text-white animate-pulse'
        : 'bg-amber-600 border-amber-400 text-gray-950'}`}>
      <div className="flex items-center gap-3 min-w-0">
        <AlertTriangle className="w-6 h-6 shrink-0" />
        <div className="min-w-0">
          <div className="font-bold tracking-wide">
            {isP1 ? 'FIRE ALERT (P1)' : 'POSSIBLE FIRE – VERIFY (P2)'} · camera {top.camera_id}
            {urgent.length > 1 && <span className="ml-2 font-normal">(+{urgent.length - 1} more)</span>}
          </div>
          <div className="text-sm opacity-90 truncate">
            Confidence {(top.confidence * 100).toFixed(0)}% · {new Date(top.timestamp * 1000).toLocaleTimeString()} ·
            {' '}Verify on camera, then follow your site&apos;s fire procedure. Certified alarms remain primary.
          </div>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {onView && (
          <button onClick={() => onView(top)} className="px-3 py-1.5 rounded-md bg-black/25 hover:bg-black/40 flex items-center gap-1 text-sm font-semibold">
            <Eye className="w-4 h-4" /> View
          </button>
        )}
        {isP1 && (
          <button onClick={() => setSilencedUntil(silenced ? 0 : Date.now() + SILENCE_MS)}
            className="px-3 py-1.5 rounded-md bg-black/25 hover:bg-black/40 flex items-center gap-1 text-sm font-semibold">
            <BellOff className="w-4 h-4" /> {silenced ? 'Unsilence' : 'Silence 5 min'}
          </button>
        )}
        <button onClick={() => onAcknowledge(top.id)}
          className="px-3 py-1.5 rounded-md bg-white text-gray-900 hover:bg-gray-100 flex items-center gap-1 text-sm font-bold">
          <CheckCircle2 className="w-4 h-4" /> Acknowledge
        </button>
      </div>
    </div>
  );
};

AlarmBanner.propTypes = {
  alerts: PropTypes.array.isRequired,
  onAcknowledge: PropTypes.func.isRequired,
  onView: PropTypes.func,
};

export default AlarmBanner;
