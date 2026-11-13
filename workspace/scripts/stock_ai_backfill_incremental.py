#!/usr/bin/env python3
"""Incrementally backfill missing stock AI analysis into OpenClaw working artifacts."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


_STOCK_ROOT = Path(os.environ.get("STOCK_SYSTEM_ROOT", "/Users/fisher/.openclaw"))
_WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", str(_STOCK_ROOT / "workspace")))

REPO_ROOT = Path(os.environ.get("STOCK_SYSTEM_DAILY_ANALYSIS", str(_WORKSPACE / "daily_stock_analysis")))
ORCHESTRATOR_SCRIPTS = _WORKSPACE / "skills/stock-system-orchestrator/scripts"
WORKING_STRATEGY = _WORKSPACE / "stock_data/03-working/stock-report-repo/data/strategy_backtests.json"
PUBLISHED_STRATEGY = _WORKSPACE / "stock-report/data/strategy_backtests.json"


def _read_env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def force_daily_analysis_llm_env() -> None:
    """Pin backfill to daily_stock_analysis/.env and scrub inherited Gemini env."""
    env_file = REPO_ROOT / ".env"
    os.environ["ENV_FILE"] = str(env_file)
    file_values = _read_env_file_values(env_file)
    for key in (
        "LITELLM_MODEL",
        "AGENT_LITELLM_MODEL",
        "LITELLM_FALLBACK_MODELS",
        "OPENAI_API_KEY",
        "OPENAI_API_KEYS",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "AIHUBMIX_KEY",
        "LLM_CHANNELS",
        "LITELLM_CONFIG",
    ):
        if key in file_values:
            os.environ[key] = file_values[key]

    effective_model = (os.environ.get("LITELLM_MODEL") or os.environ.get("OPENAI_MODEL") or "").strip()
    if effective_model and not effective_model.startswith("gemini/"):
        for key in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_MODEL_FALLBACK"):
            os.environ.pop(key, None)


def configure_light_mode(timeout_seconds: float) -> None:
    # Backfill runs after the close for a target trade_date; disable fragile realtime quote dependencies.
    force_daily_analysis_llm_env()
    os.environ["ENABLE_REALTIME_QUOTE"] = "false"
    os.environ["ENABLE_CHIP_DISTRIBUTION"] = "false"
    os.environ["BOCHA_API_KEYS"] = ""
    os.environ["TAVILY_API_KEYS"] = ""
    os.environ["BRAVE_API_KEYS"] = ""
    os.environ["SERPAPI_KEYS"] = ""
    os.environ["MAX_WORKERS"] = "1"
    os.environ["ANALYSIS_DELAY"] = "0"
    os.environ["LITELLM_TIMEOUT_SECONDS"] = str(timeout_seconds)


def add_repo_to_path() -> None:
    repo = str(REPO_ROOT)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    orchestrator = str(ORCHESTRATOR_SCRIPTS)
    if orchestrator not in sys.path:
        sys.path.insert(0, orchestrator)


def load_pending_codes(path: Path) -> list[str]:
    obj = json.loads(path.read_text())
    failure_markers = (
        "分析过程出错",
        "All LLM models failed",
        "APIConnectionError",
        "GeminiException",
        "模型调用失败",
        "LLM models failed",
    )
    for strategy in obj.get("strategies", []):
        strategy_id = strategy.get("id") or strategy.get("strategy_id")
        if strategy_id != "prebreakout_v41":
            continue
        pending = []
        for item in strategy.get("top20", []):
            status = (item.get("ai_status") or item.get("ai_analysis_status") or "").lower()
            code = item.get("code") or item.get("stock_code") or item.get("ts_code")
            text = "\n".join(str(item.get(key) or "") for key in ["ai_summary", "ai_conclusion", "ai_points", "ai_advice"])
            failed_text = any(marker in text for marker in failure_markers)
            if code and (status in {"pending", "failed"} or failed_text):
                pending.append(code)
        return pending
    return []


def detect_trade_date(path: Path) -> str:
    obj = json.loads(path.read_text())
    return str(obj.get("latest_trade_date") or "")


def count_statuses(path: Path) -> tuple[int, int]:
    obj = json.loads(path.read_text())
    for strategy in obj.get("strategies", []):
        strategy_id = strategy.get("id") or strategy.get("strategy_id")
        if strategy_id != "prebreakout_v41":
            continue
        ready = 0
        pending = 0
        for item in strategy.get("top20", []):
            status = (item.get("ai_status") or item.get("ai_analysis_status") or "").lower()
            if status == "ready":
                ready += 1
            elif status == "pending":
                pending += 1
        return ready, pending
    return 0, 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Incrementally backfill pending prebreakout AI analysis.")
    parser.add_argument("--codes", default="", help="Comma separated stock codes. Defaults to pending codes.")
    parser.add_argument("--timeout", type=float, default=90.0, help="Per-model timeout in seconds.")
    parser.add_argument("--sleep", type=float, default=2.0, help="Sleep between stocks in seconds.")
    args = parser.parse_args()

    configure_light_mode(args.timeout)
    trade_date = detect_trade_date(WORKING_STRATEGY)
    if trade_date:
        os.environ.setdefault("OPENCLAW_TARGET_TRADE_DATE", trade_date)
    add_repo_to_path()

    from fisher_bridge import backfill_prebreakout_strategy, format_summary, merge_to_github_data, save_report
    from analyzer_service import analyze_stock
    from src.config import get_config, setup_env
    import stage4_ai_publish

    setup_env()
    config = get_config()

    if args.codes.strip():
        target_codes = [part.strip() for part in args.codes.split(",") if part.strip()]
    else:
        target_codes = load_pending_codes(WORKING_STRATEGY)

    if not target_codes:
        print("No pending codes found.")
        return 0

    print(f"Pending codes: {', '.join(target_codes)}")
    successes = []
    failures: list[str] = []

    for code in target_codes:
        print(f"\n=== Backfilling {code} ===")
        try:
            result = analyze_stock(code, config=config, full_report=True)
        except Exception as exc:
            print(f"[FAIL] {code}: {exc}")
            failures.append(code)
            continue

        if not result:
            print(f"[FAIL] {code}: empty result")
            failures.append(code)
            continue

        payload = result.to_dict()
        successes.append(result)
        merge_to_github_data([payload])
        backfill_prebreakout_strategy([payload])
        stage4_ai_publish.main()
        ready, pending = count_statuses(WORKING_STRATEGY)
        print(f"[OK] {code} merged. working ready={ready} pending={pending}")
        if args.sleep > 0:
            time.sleep(args.sleep)

    if successes:
        summary = format_summary(successes)
        save_report(summary, successes)

    ready, pending = count_statuses(WORKING_STRATEGY)
    print(f"\nFinal working status: ready={ready}, pending={pending}")
    if failures:
        print(f"Failures: {', '.join(failures)}")
    print(f"Published status snapshot: ready/pending from {PUBLISHED_STRATEGY}")
    return 0 if pending == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
