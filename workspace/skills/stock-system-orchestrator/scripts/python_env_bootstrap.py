from __future__ import annotations

import os
import site
import sys
from pathlib import Path


def _discover_venv_site_packages() -> Path | None:
    """Resolve only site-packages compatible with the running interpreter."""
    venv_root = Path(
        os.environ.get("STOCK_SYSTEM_VENV", "/Users/fisher/.openclaw/venv")
    )
    lib_dir = venv_root / "lib"
    if not lib_dir.is_dir():
        return None
    version_dir = lib_dir / f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = version_dir / "site-packages"
    return site_packages if site_packages.is_dir() else None


def bootstrap_openclaw_venv() -> dict:
    sp = _discover_venv_site_packages()
    injected: list[str] = []
    if sp is not None:
        text = str(sp)
        if text not in sys.path:
            sys.path.insert(0, text)
            injected.append(text)
        try:
            site.addsitedir(text)
        except Exception:
            pass
    return {
        'sys_executable': sys.executable,
        'injected_site_packages': injected,
    }
