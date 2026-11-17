#!/usr/bin/env bash
# Sentinel supplementary fire detection - Linux installer (systemd).
#
#   sudo ./scripts/install.sh                 # standard install
#   ./scripts/install.sh --prefix /tmp/s --state /tmp/s/state --etc /tmp/s/etc --no-systemd --no-user   # test install
#
# Layout:  PREFIX/app (code)  PREFIX/venv (Python)  STATE (config/ data/ logs/ models/ = SENTINEL_HOME)
#          ETC/sentinel.env   (API token, secrets for notification channels; mode 0640 root:sentinel)
# Idempotent: re-running upgrades code and dependencies but never overwrites existing config or secrets.
set -euo pipefail

PREFIX=/opt/sentinel
STATE=/var/lib/sentinel
ETC=/etc/sentinel
SYSTEMD=1
CREATE_USER=1
VENV=""
SKIP_PIP=0
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --state) STATE="$2"; shift 2 ;;
    --etc) ETC="$2"; shift 2 ;;
    --no-systemd) SYSTEMD=0; shift ;;
    --no-user) CREATE_USER=0; shift ;;
    --venv) VENV="$2"; shift 2 ;;          # reuse an existing virtualenv (skips creation)
    --skip-pip) SKIP_PIP=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown option: $1"; usage 1 ;;
  esac
done

say() { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

echo "Sentinel installer - SUPPLEMENTARY fire detection only. Never a replacement for certified fire alarms."

# ---------------------------------------------------------------- prerequisites
PY=$(command -v python3 || true)
[[ -n "$PY" ]] || die "python3 not found (need 3.10+)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 3.10+ required"
if [[ $SYSTEMD -eq 1 || $CREATE_USER -eq 1 ]]; then
  [[ $EUID -eq 0 ]] || die "run as root (or use --no-systemd --no-user for a user-level test install)"
fi
[[ -f "$REPO/rabahdev_best.pt" ]] || die "model file rabahdev_best.pt missing from $REPO"

# ---------------------------------------------------------------- user
if [[ $CREATE_USER -eq 1 ]] && ! id sentinel &>/dev/null; then
  say "Creating system user 'sentinel'"
  useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin sentinel
  getent group video >/dev/null && usermod -aG video sentinel || true
fi

# ---------------------------------------------------------------- code
say "Installing code to $PREFIX/app"
mkdir -p "$PREFIX/app"
for item in backend scripts config run_sentinel.py requirements.txt LICENSE; do
  rm -rf "${PREFIX:?}/app/$item"
  cp -r "$REPO/$item" "$PREFIX/app/$item"
done
find "$PREFIX/app" -name '__pycache__' -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------- python
if [[ -n "$VENV" ]]; then
  [[ -x "$VENV/bin/python" ]] || die "--venv $VENV has no bin/python"
  ln -sfn "$VENV" "$PREFIX/venv"
elif [[ ! -x "$PREFIX/venv/bin/python" ]]; then
  say "Creating virtualenv $PREFIX/venv"
  "$PY" -m venv "$PREFIX/venv"
fi
if [[ $SKIP_PIP -eq 0 ]]; then
  say "Installing Python dependencies (this can take several minutes: PyTorch)"
  "$PREFIX/venv/bin/pip" install --upgrade pip >/dev/null
  "$PREFIX/venv/bin/pip" install -r "$PREFIX/app/requirements.txt"
fi
"$PREFIX/venv/bin/python" -c "import torch, ultralytics, cv2, aiohttp" || die "Python dependencies incomplete"

# ---------------------------------------------------------------- state (SENTINEL_HOME)
say "Preparing state directory $STATE"
mkdir -p "$STATE"/{config,data,logs,models}
[[ -f "$STATE/models/fire_smoke.pt" ]] || cp "$REPO/rabahdev_best.pt" "$STATE/models/fire_smoke.pt"
if [[ ! -f "$STATE/config/detection_config.yaml" ]]; then
  sed 's#^\(    path: \)rabahdev_best.pt#\1models/fire_smoke.pt#' "$REPO/config/detection_config.yaml.template" \
    > "$STATE/config/detection_config.yaml"
fi
[[ -f "$STATE/config/notifications.yaml" ]] || cp "$REPO/config/notifications.yaml.template" "$STATE/config/notifications.yaml"
[[ -f "$STATE/config/cameras.yaml" ]] || printf 'cameras: []\n' > "$STATE/config/cameras.yaml"
chmod 600 "$STATE/config/cameras.yaml"

# ---------------------------------------------------------------- secrets / env
mkdir -p "$ETC"
if [[ ! -f "$ETC/sentinel.env" ]]; then
  say "Generating API token and $ETC/sentinel.env"
  TOKEN=$("$PY" -c 'import secrets; print(secrets.token_hex(32))')
  cat > "$ETC/sentinel.env" <<EOF
# Sentinel service environment. Keep this file private (0640 root:sentinel).
SENTINEL_HOME=$STATE
SENTINEL_PORT=8765
SENTINEL_API_TOKEN=$TOKEN
# Notification secrets (see $STATE/config/notifications.yaml):
#SENTINEL_SMTP_PASSWORD=
#SENTINEL_WEBHOOK_URL=
#SENTINEL_TWILIO_TOKEN=
#SENTINEL_HEARTBEAT_URL=
EOF
  printf '%s\n' "$TOKEN" > "$ETC/api_token"   # read by the desktop app (operators in group 'sentinel')
fi
chmod 640 "$ETC/sentinel.env" "$ETC/api_token"
if [[ $CREATE_USER -eq 1 ]]; then
  chown root:sentinel "$ETC/sentinel.env" "$ETC/api_token"
  chown -R sentinel:sentinel "$STATE"
fi

# ---------------------------------------------------------------- systemd
UNIT_SRC="$REPO/deploy/linux"
if [[ $SYSTEMD -eq 1 ]]; then
  say "Installing systemd units"
  for u in sentinel.service sentinel-watchdog.service sentinel-watchdog.timer; do
    sed -e "s#@PREFIX@#$PREFIX#g" -e "s#@STATE@#$STATE#g" "$UNIT_SRC/$u" > "/etc/systemd/system/$u"
  done
  # The env file path is fixed in the units; honour a custom --etc
  [[ "$ETC" == /etc/sentinel ]] || sed -i "s#/etc/sentinel/sentinel.env#$ETC/sentinel.env#" /etc/systemd/system/sentinel*.service
  systemctl daemon-reload
  systemctl enable --now sentinel.service sentinel-watchdog.timer
  sleep 3
  systemctl --no-pager --lines=5 status sentinel.service || true
else
  mkdir -p "$PREFIX/systemd"
  for u in sentinel.service sentinel-watchdog.service sentinel-watchdog.timer; do
    sed -e "s#@PREFIX@#$PREFIX#g" -e "s#@STATE@#$STATE#g" -e "s#/etc/sentinel/sentinel.env#$ETC/sentinel.env#" \
      "$UNIT_SRC/$u" > "$PREFIX/systemd/$u"
  done
  say "Skipped systemd. Rendered units are in $PREFIX/systemd/. Start manually with:"
  echo "    set -a; . $ETC/sentinel.env; set +a; $PREFIX/venv/bin/python $PREFIX/app/run_sentinel.py"
fi

cat <<EOF

Installed.
  Health:        curl http://127.0.0.1:8765/api/health
  Cameras:       add in the desktop app (Surveillance Grid > Add Camera) or via the API
  Alerts:        edit $STATE/config/notifications.yaml, put secrets in $ETC/sentinel.env, then
                 systemctl restart sentinel
  Desktop app:   operators must be in group 'sentinel' (usermod -aG sentinel <user>) so the app can
                 read $ETC/api_token and attach to the service
  Logs:          journalctl -u sentinel -f   (and $STATE/logs/sentinel.log, rotated)
Before relying on it: run a 2-4 week site trial and record a verdict for every alert (Analytics > Site trial).
EOF
