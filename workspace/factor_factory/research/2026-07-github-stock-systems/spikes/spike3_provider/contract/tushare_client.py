"""Tushare proxy client init — MUST go through the proxy (direct official fails)."""
from __future__ import annotations

import os
from pathlib import Path


def get_pro():
    import tushare as ts

    secrets = Path(os.environ.get("OPENCLAW_SECRETS_DIR", str(Path.home() / ".openclaw/workspace/.secrets")))
    token = (os.environ.get("TUSHARE_TOKEN") or "").strip()
    if not token and (secrets / "tushare_token").exists():
        token = (secrets / "tushare_token").read_text().strip()
    api_url = (os.environ.get("TUSHARE_API_URL") or "").strip()
    if not api_url and (secrets / "tushare_api_url").exists():
        api_url = (secrets / "tushare_api_url").read_text().strip()
    if not token or not api_url:
        raise RuntimeError("tushare proxy credentials missing (.secrets/tushare_token|tushare_api_url)")
    pro = ts.pro_api(token)
    pro._DataApi__token = token
    pro._DataApi__http_url = api_url  # proxy, never official
    return pro


def tushare_version() -> str:
    try:
        import tushare as ts

        return getattr(ts, "__version__", "unknown")
    except Exception:
        return "unavailable"
