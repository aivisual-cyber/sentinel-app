/**
 * HTTP transport: lets the UI run in a normal browser (served by the Sentinel backend at
 * http://127.0.0.1:8765/) with exactly the same command names and results as the Tauri shell
 * (src-tauri/src/lib.rs). Used on Windows deployments, where no Tauri build or WebView2 is needed.
 *
 * Access token: passed once via the desktop shortcut URL (#token=...), kept in localStorage and
 * removed from the address bar. If the API answers 401 the operator is asked for the key.
 */
const TOKEN_KEY = 'sentinel_api_token';

const apiBase = () => {
  // Served by the backend itself -> same origin. Vite dev server (:1420) -> local backend.
  if (window.location.protocol.startsWith('http') && window.location.port !== '1420') return '';
  return 'http://127.0.0.1:8765';
};

const readToken = () => {
  try {
    const m = window.location.hash.match(/token=([A-Za-z0-9_-]+)/);
    if (m) {
      localStorage.setItem(TOKEN_KEY, m[1]);
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }
    return localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
};

let token = readToken();
let prompted = false;

const request = async (method, path, body, { raw = false, allowError = false } = {}) => {
  const doFetch = () => fetch(`${apiBase()}/api${path}`, {
    method,
    headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let res = await doFetch();
  if (res.status === 401 && !prompted) {
    prompted = true;
    // eslint-disable-next-line no-alert
    const key = window.prompt('Sentinel access key (from the installer / desktop shortcut):');
    if (key) {
      token = key.trim();
      try { localStorage.setItem(TOKEN_KEY, token); } catch { /* ignore */ }
      res = await doFetch();
    }
  }
  if (raw) {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok && !allowError) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};

const commands = {
  get_dashboard_data: () => request('GET', '/dashboard'),
  get_camera_feeds: () => request('GET', '/cameras'),
  get_camera_frame: ({ camera_id }) => request('GET', `/cameras/${encodeURIComponent(camera_id)}/frame`),
  get_system_metrics: () => request('GET', '/metrics'),
  get_system_status: async () => {
    try {
      const h = await request('GET', '/health');
      return { backend_running: !!h.backend_running, python_pid: null, last_update: Date.now() / 1000,
        health_state: h.status, issues: h.issues || [], inference: h.inference || null };
    } catch {
      return { backend_running: false, python_pid: null, last_update: Date.now() / 1000,
        health_state: 'unreachable', issues: ['Sentinel backend unreachable'] };
    }
  },
  update_threshold: async (a) => {
    const d = await request('POST', '/threshold', a, { allowError: true });
    return !!d.success;
  },
  acknowledge_alert: async ({ alert_id }) => !!(await request('POST', '/acknowledge', { alert_id })).success,
  discover_cameras: ({ timeout } = {}) => request('POST', '/cameras/discover', { timeout: timeout || 5 }),
  add_camera: (a) => request('POST', '/cameras/add', { enabled: true, fps: 15, ...a }, { allowError: true }),
  test_camera: ({ camera_id, ...rest }) => request('POST', `/cameras/${encodeURIComponent(camera_id || 'test')}/test`, rest, { allowError: true }),
  remove_camera: ({ camera_id }) => request('DELETE', `/cameras/${encodeURIComponent(camera_id)}/remove`),
  get_alert_frame: ({ alert_id }) => request('GET', `/alerts/${encodeURIComponent(alert_id)}/frame`),
  get_notifications: () => request('GET', '/notifications'),
  test_notification: ({ channel }) => request('POST', '/notifications/test', { channel }, { allowError: true }),
  set_alert_verdict: ({ alert_id, verdict, notes }) =>
    request('POST', `/alerts/${encodeURIComponent(alert_id)}/verdict`, { verdict, notes: notes || '' }),
  get_trial_report: ({ days } = {}) => request('GET', `/report?days=${days || 7}`),
  export_trial_csv: ({ days } = {}) => request('GET', `/report?days=${days || 30}&format=csv`, null, { raw: true }),
  // The backend runs as a service in browser mode; nothing to start or stop from the UI.
  start_python_backend: async () => true,
  stop_python_backend: async () => false,
};

export const httpApi = {
  invoke: async (command, args) => {
    const fn = commands[command];
    if (!fn) throw new Error(`Unknown command ${command}`);
    return fn(args || {});
  },
  // No server push in browser mode: the hooks poll (alerts every 5 s).
  listen: async () => () => {},
  isAvailable: true,
  transport: 'http',
};
