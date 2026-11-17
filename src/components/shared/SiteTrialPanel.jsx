import React, { useCallback, useEffect, useState } from 'react';
import { Download, ClipboardCheck } from 'lucide-react';
import { useTauriCommand } from '../../hooks/useTauriApi';
import { useNotifications } from '../../hooks/useNotifications';

/**
 * Site-trial metrics computed from operator verdicts (REAL FIRE / FALSE ALARM / TEST).
 * This is the evidence needed before relying on Sentinel at a site.
 */
const pct = (v) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`);
const num = (v) => (v === null || v === undefined ? '—' : v);

const readiness = (o, verifiedFraction, observedDays) => {
  if (!o || o.alerts === 0) return { text: 'No alerts yet in this window. Keep the trial running.', cls: 'text-gray-400' };
  if ((verifiedFraction ?? 0) < 0.8) return { text: `Only ${pct(verifiedFraction)} of alerts verified – mark every alert in Incident Management.`, cls: 'text-warning-400' };
  if (observedDays < 14) return { text: `${observedDays} days observed – run at least 14 days before deciding.`, cls: 'text-warning-400' };
  if (o.p1_false_alarms_per_day > 0.2) return { text: 'P1 false alarms are too frequent: raise P1 or retrain with site false alarms.', cls: 'text-red-400' };
  return { text: 'Trial metrics look acceptable for supplementary use. Review per camera below.', cls: 'text-emerald-400' };
};

const SiteTrialPanel = () => {
  const [days, setDays] = useState(7);
  const [report, setReport] = useState(null);
  const { execute: getTrialReport } = useTauriCommand('get_trial_report');
  const { execute: exportCsv } = useTauriCommand('export_trial_csv');
  const notifications = useNotifications();

  const load = useCallback(async () => {
    const r = await getTrialReport({ days });
    if (r && r.overall) setReport(r);
  }, [getTrialReport, days]);

  useEffect(() => { load(); const id = setInterval(load, 30000); return () => clearInterval(id); }, [load]);

  const download = async () => {
    const text = await exportCsv({ days });
    if (typeof text !== 'string') { notifications.showError('Export failed (backend unreachable?)'); return; }
    const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
    const a = document.createElement('a');
    a.href = url; a.download = `sentinel_trial_${days}d.csv`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const o = report?.overall;
  const r = readiness(o, report?.verified_fraction, report?.observed_days ?? 0);
  return (
    <div className="command-panel rounded-xl p-6 mb-6">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <ClipboardCheck className="w-6 h-6 text-fire-400" />
          <div>
            <h3 className="text-lg font-semibold text-white">SITE TRIAL – OPERATOR-VERIFIED PERFORMANCE</h3>
            <p className="text-xs text-gray-400">From verdicts recorded in Incident Management. Observed {num(report?.observed_days)} days.</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {[1, 7, 30].map(d => (
            <button key={d} onClick={() => setDays(d)}
              className={`px-3 py-1 rounded text-sm ${days === d ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-white'}`}>{d}d</button>
          ))}
          <button onClick={download} className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-white text-sm flex items-center gap-1">
            <Download className="w-4 h-4" /> CSV
          </button>
        </div>
      </div>
      {!o ? <p className="text-gray-500 text-sm">Backend not reachable.</p> : (
        <>
          <div className="grid grid-cols-6 gap-4 mb-3">
            {[
              ['Alerts', o.alerts], ['Verified', pct(report.verified_fraction)], ['Precision', pct(o.precision)],
              ['False alarms / day', num(o.false_alarms_per_day)], ['P1 false alarms / day', num(o.p1_false_alarms_per_day)],
              ['Median time to ack', o.median_ack_seconds === null ? '—' : `${Math.round(o.median_ack_seconds)} s`],
            ].map(([k, v]) => (
              <div key={k} className="bg-gray-800/50 rounded-lg p-3">
                <div className="text-xs text-gray-400">{k}</div>
                <div className="text-xl font-command text-white">{v}</div>
              </div>
            ))}
          </div>
          <p className={`text-sm mb-3 ${r.cls}`}>{r.text}</p>
          {Object.keys(report.cameras).length > 0 && (
            <table className="w-full text-sm">
              <thead><tr className="text-gray-400 text-left">
                <th className="py-1">Camera</th><th>Alerts</th><th>P1/P2/P4</th><th>Real fire</th><th>False alarms</th><th>Unverified</th><th>Precision</th><th>FA/day</th>
              </tr></thead>
              <tbody>
                {Object.entries(report.cameras).map(([cam, c]) => (
                  <tr key={cam} className="border-t border-gray-800 text-gray-200">
                    <td className="py-1 font-mono">{cam}</td><td>{c.alerts}</td>
                    <td>{c.by_level.P1}/{c.by_level.P2}/{c.by_level.P4}</td>
                    <td>{c.verified_fire}</td><td>{c.false_alarms}</td><td>{c.unverified}</td>
                    <td>{pct(c.precision)}</td><td>{c.false_alarms_per_day}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
};

export default SiteTrialPanel;
