"""Central credential loading for the stock system.

No plaintext tokens may appear in code or docs. Resolution order:
1. Environment variable
2. Secret file under ~/.openclaw/workspace/.secrets/ (chmod 600)

Secret files:
- tushare_token    : Tushare Pro token
- tushare_api_url  : Optional custom API endpoint (e.g. private proxy)
"""
from __future__ import annotations

import os
from pathlib import Path

_WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE_DIR")
    or os.environ.get("STOCK_SYSTEM_WORKSPACE")
    or (Path.home() / ".openclaw" / "workspace")
)
SECRETS_DIR = Path(os.environ.get("OPENCLAW_SECRETS_DIR", str(_WORKSPACE / ".secrets")))

DEFAULT_TUSHARE_URL = "http://api.tushare.pro"


def _read_secret_file(name: str) -> str | None:
    path = SECRETS_DIR / name
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except OSError:
        return None
    return None


def get_tushare_token(required: bool = True) -> str:
    token = (os.environ.get("TUSHARE_TOKEN") or "").strip() or _read_secret_file("tushare_token")
    if not token and required:
        raise RuntimeError(
            "TUSHARE_TOKEN is not configured. Set the env var or write it to "
            f"{SECRETS_DIR / 'tushare_token'} (chmod 600)."
        )
    return token or ""


def get_tushare_http_url() -> str:
    """Custom endpoint wins (env > secret file); falls back to the official API."""
    url = (
        (os.environ.get("TUSHARE_API_URL") or "").strip()
        or (os.environ.get("TUSHARE_HTTP_URL") or "").strip()
        or _read_secret_file("tushare_api_url")
        or DEFAULT_TUSHARE_URL
    )
    return url
