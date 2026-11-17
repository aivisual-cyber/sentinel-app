"""
Single source of truth for filesystem locations.

Every module used to resolve "config/...", "data/...", "models/..." relative to the
*current working directory*. The Tauri shell starts the backend from ``src-tauri/``,
so the desktop app silently read a different config (thresholds 0.78/0.60/0.50),
wrote a different database and lost cameras. All paths now resolve from
``SENTINEL_HOME`` (env var) or the repository root.
"""

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent   # repo root, or <InstallDir>\app on Windows


def _find_home() -> Path:
    r"""Where config/data/logs live. Order: SENTINEL_HOME env var (services; set machine-wide by the
    Windows installer) -> the install marker next to app\ (scripts run from <InstallDir>\app) ->
    %ProgramData%\Sentinel if it holds a config -> the repository root (development)."""
    env = os.environ.get("SENTINEL_HOME")
    if env:
        return Path(env)
    marker = APP_ROOT.parent / "sentinel-install.json"
    if marker.is_file():
        try:
            import json
            data_dir = json.loads(marker.read_text(encoding="utf-8-sig")).get("DataDir")
            if data_dir and Path(data_dir).is_dir():
                return Path(data_dir)
        except Exception:
            pass
    program_data = os.environ.get("ProgramData")
    if program_data and (Path(program_data) / "Sentinel" / "config" / "detection_config.yaml").is_file():
        return Path(program_data) / "Sentinel"
    return APP_ROOT


SENTINEL_HOME = _find_home().resolve()

CONFIG_DIR = SENTINEL_HOME / "config"
DATA_DIR = SENTINEL_HOME / "data"
LOG_DIR = SENTINEL_HOME / "logs"
MODELS_DIR = SENTINEL_HOME / "models"

DETECTION_CONFIG = CONFIG_DIR / "detection_config.yaml"
CAMERAS_CONFIG = CONFIG_DIR / "cameras.yaml"
ALERTS_DB = DATA_DIR / "alerts.db"
ALERT_FRAMES_DIR = DATA_DIR / "alert_frames"


def resolve(path) -> Path:
    """Resolve a possibly-relative path against SENTINEL_HOME (absolute paths pass through)."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else (SENTINEL_HOME / p).resolve()


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, LOG_DIR, MODELS_DIR, ALERT_FRAMES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_env_file(path=None) -> int:
    """Load KEY=VALUE lines (secrets, token) from SENTINEL_ENV_FILE without overriding existing env vars.
    Used by Windows services (one ACL-protected file instead of secrets in service definitions)."""
    path = path or os.environ.get("SENTINEL_ENV_FILE")
    if not path or not Path(path).is_file():
        return 0
    n = 0
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            n += 1
    return n
