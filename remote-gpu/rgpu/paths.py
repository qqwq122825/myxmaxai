from __future__ import annotations

import os
from pathlib import Path
import sys


def reports_directory() -> Path:
    """Frozen GUI apps must not depend on the launcher's working directory."""
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    fallback = local / "FaceLiveRemoteGpu" / "reports"
    preferred = fallback if getattr(sys, "frozen", False) else Path.cwd() / "reports"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
