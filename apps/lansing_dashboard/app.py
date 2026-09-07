"""Compatibility launcher for the universal Fluid Reality dashboard.

The original Lansing-only dashboard has been superseded by
``apps.fluidreality_dashboard``. Keeping this launcher preserves existing
commands and shortcuts while ensuring Lansing users receive the same Serial,
TCP, TLS, and Bluetooth connection support as every other compatible board.
"""

from __future__ import annotations

import sys
from pathlib import Path


SDK_ROOT = Path(__file__).resolve().parents[2]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))

from apps.fluidreality_dashboard.app import *  # noqa: F403


if __name__ == "__main__":
    raise SystemExit(main())
