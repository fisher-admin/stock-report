#!/usr/bin/env python3
"""Dual-channel Stock AI Model Gateway (Copilot Primary -> xAI Fallback)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
def read_hermes_copilot_tokens() -> tuple[str, str]:
    token = os.environ.get("COPILOT_API_TOKEN") or ""
    base_url = os.environ.get("COPILOT_API_BASE") or "https://api.githubcopilot.com"
    if token:
        return token, base_url

    hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    hermes_agent = hermes_home / "hermes-agent"
    if hermes_agent.is_dir() and str(hermes_agent) not in sys.path:
        sys.path.insert(0, str(hermes_agent))
    try:
        from hermes_cli.copilot_auth import get_copilot_api_token, resolve_copilot_token
        raw_token, _ = resolve_copilot_token()
        if raw_token:
            api_token, base = get_copilot_api_token(raw_token)
            if api_token:
                return api_token, base or "https://api.githubcopilot.com"
    except Exception:
        pass
    return "", ""


def read_hermes_xai_token() -> str:
    token = os.environ.get("XAI_API_KEY") or ""
    if token:
        return token
    hermes_auth = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser() / "auth.json"
    if hermes_auth.is_file():
        try:
            payload = json.loads(hermes_auth.read_text(encoding="utf-8"))
            return str(((payload.get("providers") or {}).get("xai-oauth") or {}).get("tokens", {}).get("access_token") or "").strip()
        except Exception:
            pass
    return ""


def call_model_gateway(
    prompt: str,
    models: list[str],
    *,
    max_tokens: int = 512,
    timeout: int = 60,
    llm_func: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    last_error = ""
    copilot_token, copilot_base = read_hermes_copilot_tokens()
    xai_token = read_hermes_xai_token()

    for model in models:
        try:
            low = model.lower()
            if low.startswith(("copilot/", "github_copilot/")):
                if not copilot_token:
                    last_error = f"{model}: missing copilot token"
                    continue
            elif low.startswith("xai/"):
                if not xai_token:
                    last_error = f"{model}: missing xAI token"
                    continue

            if llm_func is not None:
                content = llm_func(prompt=prompt, model=model, max_tokens=max_tokens, timeout=timeout)
                return {"ok": True, "content": content, "model": model, "error": None}

            import litellm
            messages = [{"role": "user", "content": prompt}]
            kwargs: dict[str, Any] = {
                "messages": messages,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
            if low.startswith(("copilot/", "github_copilot/")):
                model_clean = model.split("/", 1)[1]
                kwargs["model"] = f"openai/{model_clean}"
                kwargs["api_key"] = copilot_token
                kwargs["api_base"] = copilot_base
                kwargs["extra_headers"] = {
                    "Editor-Version": "vscode/1.96.2",
                    "Copilot-Integration-Id": "vscode-chat",
                }
            elif low.startswith("xai/"):
                kwargs["model"] = model
                kwargs["api_key"] = xai_token
            else:
                kwargs["model"] = model

            resp = litellm.completion(**kwargs)
            choice = resp.choices[0]
            content = getattr(choice.message, "content", "") or ""
            return {"ok": True, "content": content, "model": model, "error": None}
        except Exception as exc:
            last_error = f"{model}: {type(exc).__name__}: {exc}"
            continue

    return {"ok": False, "content": "", "model": None, "error": last_error or "All models in chain failed"}
