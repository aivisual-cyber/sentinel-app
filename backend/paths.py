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

SENTINEL_HOME = Path(os.environ.get("SENTINEL_HOME", Path(__file__).resolve().parent.parent)).resolve()

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
