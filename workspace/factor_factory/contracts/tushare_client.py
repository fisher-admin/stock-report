"""Compatibility entry point for the stock system's configured data client."""
from __future__ import annotations

import os
from pathlib import Path


def get_pro():
    import sys
    workspace = Path(os.environ.get("OPENCLAW_WORKSPACE_DIR") or
                     os.environ.get("STOCK_SYSTEM_WORKSPACE") or Path(__file__).resolve().parents[2])
    analyzer = str(workspace / "skills" / "stock-analyzer")
    if analyzer not in sys.path:
        sys.path.insert(0, analyzer)
    from pipeline import init_tushare
    return init_tushare()


def tushare_version() -> str:
    try:
        import tushare as ts

        return getattr(ts, "__version__", "unknown")
    except Exception:
        return "unavailable"
