import React, { useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { Image as ImageIcon, X } from 'lucide-react';
import { useNotifications } from '../../hooks/useNotifications';
import { useTauriCommand } from '../../hooks/useTauriApi';
import { ClearAlertsConfirmDialog } from '../shared/ConfirmDialog';

const LEVELS = {
  P1: { bg: 'emergency', text: 'CRITICAL – IMMEDIATE ACTION' },
  P2: { bg: 'warning', text: 'REVIEW REQUIRED' },
  P4: { bg: 'safety', text: 'LOGGED EVENT' },
};

const VERDICTS = {
  fire: { label: 'REAL FIRE', cls: 'bg-red-600 text-white' },
  false_alarm: { label: 'FALSE ALARM', cls: 'bg-gray-500 text-white' },
  test: { label: 'TEST', cls: 'bg-blue-600 text-white' },
};

const VerdictButtons = ({ alert, onVerdict, busy }) => {
  if (alert.verdict) {
    const v = VERDICTS[alert.verdict] || { label: alert.verdict, cls: 'bg-gray-600 text-white' };
    return <span className={`px-3 py-1 rounded-full text-xs font-bold ${v.cls}`}>{v.label}</span>;
  }
  return (
    <div className="flex items-center gap-1" title="Your verdict builds the site trial report and the training set">
      <button disabled={busy} onClick={() => onVerdict(alert.id, 'fire')}
        className="px-2 py-2 rounded-lg text-xs font-bold bg-red-700 hover:bg-red-600 text-white">REAL FIRE</button>
      <button disabled={busy} onClick={() => onVerdict(alert.id, 'false_alarm')}
        className="px-2 py-2 rounded-lg text-xs font-bold bg-gray-600 hover:bg-gray-500 text-white">FALSE ALARM</button>
      <button disabled={busy} onClick={() => onVerdict(alert.id, 'test')}
        className="px-2 py-2 rounded-lg text-xs font-bold bg-gray-800 hover:bg-gray-700 text-gray-200">TEST</button>
    </div>
  );
};

VerdictButtons.propTypes = { alert: PropTypes.object.isRequired, onVerdict: PropTypes.func.isRequired, busy: PropTypes.bool };

const AlertItem = ({ alert, onAcknowledge, onEvidence, onVerdict, isProcessing }) => {
  const config = LEVELS[alert.alert_level] || { bg: 'gray', text: 'UNKNOWN' };
  const when = new Date(alert.timestamp * 1000);
  const active = alert.status === 'active';
  return (
    <div className={`bg-command-700 rounded-lg p-4 border-l-4 border-${config.bg}-500`}>
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center space-x-4 min-w-0">
          <div className={`bg-${config.bg}-500 text-white px-3 py-1 rounded-full text-xs font-semibold font-command`}>
            {alert.alert_level}
          </div>
          <div className="min-w-0">
            <div className="font-semibold text-white truncate">{alert.camera_id}</div>
            <div className="text-sm text-gray-400">{config.text}</div>
          </div>
        </div>
        <div className="text-right">
          <div className={`text-lg font-command text-${config.bg}-400`}>{(alert.confidence * 100).toFixed(1)}%</div>
          <div className="text-xs text-gray-500">{when.toLocaleDateString()} {when.toLocaleTimeString()}</div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onEvidence(alert)}
            disabled={!alert.has_frame}
            title={alert.has_frame ? 'View annotated alert frame' : 'No frame stored for this alert'}
            className={`px-3 py-2 rounded-lg text-sm font-semibold flex items-center gap-1 ${alert.has_frame
              ? 'bg-gray-700 hover:bg-gray-600 text-white' : 'bg-gray-800 text-gray-500 cursor-not-allowed'}`}
          >
            <ImageIcon className="w-4 h-4" /> EVIDENCE
          </button>
          <VerdictButtons alert={alert} onVerdict={onVerdict} busy={isProcessing} />
          {active && <button
            onClick={() => onAcknowledge(alert.id)}
            disabled={!active || isProcessing}
            className={`px-4 py-2 rounded-lg text-sm font-semibold transition-colors ${active && !isProcessing
              ? 'bg-fire-500 hover:bg-fire-600 text-white' : 'bg-gray-600 text-gray-400 cursor-not-allowed'}`}
          >
            {isProcessing ? 'PROCESSING...' : 'ACKNOWLEDGE'}
          </button>}
        </div>
      </div>
    </div>
  );
};

AlertItem.propTypes = {
  alert: PropTypes.object.isRequired,
  onAcknowledge: PropTypes.func.isRequired,
  onEvidence: PropTypes.func.isRequired,
  onVerdict: PropTypes.func.isRequired,
  isProcessing: PropTypes.bool,
};

const AllClearState = () => (
  <div className="text-center text-gray-500 py-12">
    <div className="w-16 h-16 bg-emerald-500 rounded-full mx-auto mb-4 flex items-center justify-center">
      <div className="w-8 h-8 bg-white rounded"></div>
    </div>
    <p className="text-lg font-semibold text-emerald-400">NO ACTIVE INCIDENTS</p>
    <p className="text-sm">Detection must be ONLINE for this to mean anything – check the header status.</p>
  </div>
);

const EvidenceModal = ({ evidence, onClose }) => {
  if (!evidence) return null;
  return (
    <div className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-gray-900 rounded-xl overflow-hidden max-w-4xl w-full" onClick={e => e.stopPropagation()}>
        <div className="p-4 border-b border-gray-800 flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold text-white">Alert evidence · {evidence.camera_id}</h3>
            <p className="text-sm text-gray-400">
              {new Date(evidence.timestamp * 1000).toLocaleString()} · confidence {(evidence.confidence * 100).toFixed(1)}%
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-white" aria-label="Close"><X className="w-5 h-5" /></button>
        </div>
        {evidence.loading && <div className="p-12 text-center text-gray-400">Loading frame…</div>}
        {evidence.error && <div className="p-12 text-center text-red-400">{evidence.error}</div>}
        {evidence.frame && (
          <img src={`data:image/jpeg;base64,${evidence.frame}`} alt="Annotated alert frame"
            className="w-full h-auto max-h-[75vh] object-contain bg-black" />
        )}
      </div>
    </div>
  );
};

EvidenceModal.propTypes = { evidence: PropTypes.object, onClose: PropTypes.func.isRequired };

const IncidentManagement = ({ alerts, onAcknowledgeAlert, connectionStatus }) => {
  const [tab, setTab] = useState('active');
  const [showAckAll, setShowAckAll] = useState(false);
  const [processing, setProcessing] = useState(new Set());
  const [evidence, setEvidence] = useState(null);
  const notifications = useNotifications();
  const { execute: getAlertFrame } = useTauriCommand('get_alert_frame');
  const { execute: setAlertVerdict } = useTauriCommand('set_alert_verdict');
  const [verdicts, setVerdicts] = useState({}); // optimistic until the next poll

  const merged = useMemo(() => alerts.map(a => (verdicts[a.id]
    ? { ...a, verdict: verdicts[a.id], status: a.status === 'active' ? 'acknowledged' : a.status } : a)), [alerts, verdicts]);
  const active = useMemo(() => merged.filter(a => a.status === 'active'), [merged]);
  const history = useMemo(() => merged.filter(a => a.status !== 'active'), [merged]);

  const giveVerdict = async (alertId, verdict) => {
    setProcessing(prev => new Set([...prev, alertId]));
    const r = await setAlertVerdict({ alert_id: alertId, verdict });
    setProcessing(prev => { const s = new Set(prev); s.delete(alertId); return s; });
    if (r && r.success) {
      setVerdicts(v => ({ ...v, [alertId]: verdict }));
      notifications.showSuccess(verdict === 'false_alarm'
        ? 'Marked false alarm – saved as a training example' : `Marked ${verdict.replace('_', ' ')}`);
    } else {
      notifications.showError('Could not save verdict (backend unreachable?)');
    }
  };
  const shown = tab === 'active' ? active : history;

  const acknowledge = async (alertId) => {
    if (processing.has(alertId)) return;
    setProcessing(prev => new Set([...prev, alertId]));
    try {
      await onAcknowledgeAlert(alertId);
    } finally {
      setProcessing(prev => { const s = new Set(prev); s.delete(alertId); return s; });
    }
  };

  // Acknowledge every active alert IN THE BACKEND (previously this only hid them in the UI)
  const acknowledgeAll = async () => {
    await Promise.all(active.map(a => acknowledge(a.id)));
    notifications.operations.alertsCleared(active.length);
  };

  const openEvidence = async (alert) => {
    setEvidence({ ...alert, loading: true });
    const data = await getAlertFrame({ alert_id: alert.id });
    setEvidence(data && data.frame
      ? { ...alert, frame: data.frame }
      : { ...alert, error: 'Frame not available (it may have been removed by retention cleanup).' });
  };

  return (
    <div className="p-6">
      <div className="mb-6">
        <h2 className="text-2xl font-bold text-white mb-2">INCIDENT MANAGEMENT</h2>
        <p className="text-gray-400">Verify each alert on camera, then record what it was. Verdicts drive the site trial report and model retraining. Last 24 hours.</p>
      </div>

      <div className="command-panel rounded-xl p-6">
        {!connectionStatus?.isConnected && (
          <div className="bg-emergency-500/20 border border-emergency-500 rounded-lg p-3 mb-4">
            <div className="flex items-center space-x-2">
              <div className="w-2 h-2 bg-emergency-500 rounded-full animate-pulse"></div>
              <span className="text-emergency-400 text-sm font-semibold">CONNECTION ISSUE – list may be stale</span>
            </div>
            <p className="text-emergency-300 text-xs mt-1">{connectionStatus?.lastError || 'Unable to connect to backend'}</p>
          </div>
        )}
        <div className="flex items-center justify-between mb-4">
          <div className="flex gap-2">
            {[['active', `ACTIVE (${active.length})`], ['history', `ACKNOWLEDGED (${history.length})`]].map(([key, label]) => (
              <button key={key} onClick={() => setTab(key)}
                className={`px-4 py-2 rounded-lg text-sm font-semibold ${tab === key ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-white'}`}>
                {label}
              </button>
            ))}
          </div>
          {tab === 'active' && active.length > 0 && (
            <button onClick={() => setShowAckAll(true)}
              className="bg-fire-500 hover:bg-fire-600 text-white px-4 py-2 rounded-lg text-sm font-semibold transition-colors">
              ACKNOWLEDGE ALL
            </button>
          )}
        </div>
        <div className="space-y-3">
          {shown.length === 0 ? (tab === 'active' ? <AllClearState /> :
            <p className="text-center text-gray-500 py-12">No acknowledged alerts in the last 24 hours.</p>) :
            shown.map(alert => (
              <AlertItem key={alert.id} alert={alert} onAcknowledge={acknowledge} onEvidence={openEvidence}
                onVerdict={giveVerdict}
                isProcessing={processing.has(alert.id)} />
            ))}
        </div>
      </div>

      <ClearAlertsConfirmDialog isOpen={showAckAll} onClose={() => setShowAckAll(false)}
        onConfirm={acknowledgeAll} alertCount={active.length} />
      <EvidenceModal evidence={evidence} onClose={() => setEvidence(null)} />
    </div>
  );
};

IncidentManagement.propTypes = {
  alerts: PropTypes.array.isRequired,
  onAcknowledgeAlert: PropTypes.func.isRequired,
  connectionStatus: PropTypes.object,
};

export default React.memo(IncidentManagement);
