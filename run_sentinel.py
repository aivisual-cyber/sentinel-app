#!/usr/bin/env python3
"""
Sentinel Fire Detection System - Production Entry Point (systemd service / Docker CMD).

Runs the same API server + detection engine the desktop app uses.
SUPPLEMENTARY FIRE DETECTION ONLY - NOT A REPLACEMENT FOR CERTIFIED SYSTEMS.

Environment:
  SENTINEL_HOME   base directory for config/, data/, logs/, models/ (default: this repo)
  SENTINEL_PORT   API port (default 8765)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'backend'))

if sys.version_info < (3, 10):
    sys.exit("Python 3.10+ required")

import paths  # noqa: E402

paths.load_env_file()  # SENTINEL_ENV_FILE (Windows service) - must run before api_server reads the token

from api_server import main  # noqa: E402

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
