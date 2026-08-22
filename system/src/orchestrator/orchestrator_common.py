#!/usr/bin/env python3
"""Shared helpers for the stock-system orchestrator entrypoints."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from immutable_strategy_registry import (
    PREBREAKOUT_CONTROL_ID,
    PREBREAKOUT_CONTROL_STRATEGY_VERSION,
    PREBREAKOUT_LEGACY_ALIAS,
)

_STOCK_ROOT = Path(os.environ.get("STOCK_SYSTEM_ROOT", "/Users/fisher/.openclaw"))
WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", str(_STOCK_ROOT / "workspace")))
VENV_DIR = Path(os.environ.get("STOCK_SYSTEM_VENV", str(_STOCK_ROOT / "venv")))
PREFERRED_PYTHON = Path(os.environ.get("STOCK_SYSTEM_PYTHON", str(VENV_DIR / "bin/python")))
CACHE_DIR = Path(os.environ.get("OPENCLAW_CACHE_DIR", str(WORKSPACE / "stock_data/03-working/backtest_cache")))
HEALTH_DIR = Path(os.environ.get("OPENCLAW_HEALTH_DIR", str(WORKSPACE / "stock_data/03-working/health")))
HEALTH_DIR.mkdir(parents=True, exist_ok=True)
CANONICAL_DIR = HEALTH_DIR / "canonical"
CANONICAL_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR = HEALTH_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
WORKING_REPO = Path(
    os.environ.get("OPENCLAW_WORKING_REPO", str(WORKSPACE / "stock_data/03-working/stock-report-repo"))
)
PUBLISHED_REPO = Path(os.environ.get("OPENCLAW_PUBLISHED_REPO", str(WORKSPACE / "stock-report")))
WORKING_STRATEGY_JSON = WORKING_REPO / "data/strategy_backtests.json"
WORKING_DATA_JSON = WORKING_REPO / "data.json"
PUBLISHED_STRATEGY_JSON = PUBLISHED_REPO / "data/strategy_backtests.json"
PUBLISHED_DATA_JSON = PUBLISHED_REPO / "data.json"
TARGET_STRATEGY = PREBREAKOUT_LEGACY_ALIAS
TARGET_STRATEGY_CANONICAL = PREBREAKOUT_CONTROL_ID
TARGET_STRATEGY_VERSION = PREBREAKOUT_CONTROL_STRATEGY_VERSION
SHORT_TERM_STRATEGY = "alpha_short_v1"
LONG_TERM_STRATEGY = "alpha_long_v1"
RETAINED_STRATEGIES = [TARGET_STRATEGY]
ARCHIVED_STRATEGIES = {
    "greenfield_o2c_v1": {
        "status": "archived_historical_only",
        "reason": "O2C net evidence remained negative; daily expansion stopped",
    },
    "t1_factor_v1": {
        "status": "archived_historical_only",
        "reason": "T1 out-of-sample signal gate failed; daily expansion stopped",
    },
    "S3_intraday_shape_daily": {
        "status": "archived_historical_only",
        "reason": "S3 failed frozen validation; forward signal generation stopped",
    },
    "wts_auction_v1": {
        "status": "archived_historical_only",
        "reason": "WTS crossed its frozen kill threshold; tracking stopped",
    },
    "auction_chase": {
        "status": "archived_historical_only",
        "reason": "auction chase and auction confirmation variants failed",
    },
    "simple_pead": {
        "status": "archived_historical_only",
        "reason": "simple earnings-growth sorting replaced by event_quality_drift_v1",
    },
    "intraday_reversal": {
        "status": "archived_historical_only",
        "reason": "current same-day reversal strategy has no validated net edge",
    },
}
# 20260708对齐: 原10字段含6个daily_stock_analysis老桥接遗留(conclusion/trend/ma/volume/
# confidence/risk_warning), 直连litellm新生成器(generate_prebreakout_ai.py, 与O2C同款)只产
# summary/advice/score/points/risks且v2前端对缺失字段有兜底(ai_summary||ai_conclusion)。
# 老桥接退役后这6字段永0/20→prebreakout item_ai_status永判pending→永久base_only降级。
# 收缩到新生成器保证产出的4字段(gen_one成功即含: summary非空+score非None+points>=3+advice恒有值),
# 与O2C的ready口径一致。收缩是更宽松判定, 对已过的T1/O2C无回归。
REQUIRED_AI_FIELDS = [
    "ai_score",
    "ai_advice",
    "ai_points",
    "ai_summary",
]
EXTENDED_FIELDS = [
    "ai_score",
    "ai_advice",
    "ai_decision",
    "ai_confidence",
    "ai_summary",
    "ai_conclusion",
    "ai_signal",
    "ai_points",
    "ai_checklist",
    "ai_risks",
    "ai_catalysts",
    "ai_news",
    "ai_trend",
    "ai_ma",
    "ai_volume",
    "ai_fundamental",
    "ai_risk_warning",
    "ai_balance_sheet",
    "ai_catalyst_calendar",
    "ai_fund_quality",
    "ai_industry_moat",
    "ai_red_flags",
    "ai_valuation",
    "industry_name",
    "industry_heat_score",
    "major_events_30d",
    "market_cap_float",
    "market_cap_total",
    "news_sentiment_score",
    "news_summary_7d",
    "event_impact_score",
]
# 20260710对齐: 原5标签(触发条件/失效条件/板块定位/筹码判断/事件催化)是已退役的
# daily_stock_analysis老桥接输出规范——现役直连生成器generate_prebreakout_ai(prompt_version
# prebreakout_ai_v1)的ai_points固定前缀是下面5个。老标签永匹配不上→item_ai_status永判
# partial→永久非full(与REQUIRED_AI_FIELDS同源的第二块遗留)。取子串不带冒号, 降低LLM变体敏感。
EXPLICIT_LABELS = [
    "技术形态",
    "核心因子贡献",
    "量价与筹码",
    "风险点",
    "执行参考",
]
ALLOWED_REPO_OUTPUTS = {
    "data/strategy_backtests.json",
    "data.json",
    "data/recommendation_analytics/industry_heatmap.json",
    "data/recommendation_analytics/market_morning_brief_latest.json",
    "data/recommendation_analytics/overnight_fx_history.json",
    "data/recommendation_analytics/market_industry_heatmap.json",
    "data/recommendation_analytics/unified_decision_payload.json",
    "data/recommendation_analytics/latest.json",
    "data/recommendation_analytics/midday_analysis_latest.json",
    "data/recommendation_analytics/o2c_factor_recommendations.json",
    "data/recommendation_analytics/o2c_factor_summary.json",
    "data/recommendation_analytics/o2c_ai_analysis.json",
    "data/recommendation_analytics/o2c_greenfield_ai_analysis.json",
    "data/recommendation_analytics/prebreakout_recommendations.csv",
    "data/recommendation_analytics/prebreakout_recommendations.json",
    "data/recommendation_analytics/prebreakout_summary.json",
    "data/recommendation_analytics/research_lab_latest.json",
    "data/recommendation_analytics/t1_factor_recommendations.json",
    "data/recommendation_analytics/t1_ai_analysis.json",
    "data/recommendation_analytics/t1_alpha191_ai_analysis.json",
    "data/latest/adjustment_log.json",
    "data/latest/candidate_state.json",
    "data/latest/combined_recommendation.json",
    "data/latest/decision_state.json",
    "data/latest/execution_state.json",
    "data/latest/greenfield_top20.json",
    "data/latest/market_context.json",
    "data/latest/market_state.json",
    "data/latest/recommendation_state.json",
    "data/latest/research_state.json",
    "data/latest/research_state_t1.json",
    "data/latest/review_state.json",
    "data/latest/review_state_o2c.json",
    "data/latest/review_state_unified.json",
    "data/latest/run_manifest.json",
    "data/latest/strategy_consensus_state.json",
    "data/latest/strategy_registry.json",
    "data/latest/strategy_run_state.json",
    "data/latest/strategy_state.json",
    "data/latest/system_health.json",
    "data/latest/system_verdict.json",
    "data/latest/t1_factor_research_state.json",
    "data/latest/prebreakout_shadow_watch.json",
    "decision-candidates.html",
    "prebreakout-shadow.html",
}

# 输出目录前缀：这两个目录下的全部文件都是流水线/边车产物（含前端摘要 *_latest.json、
# sentiment_state.json 等）。预检以「前缀」判定是否纯输出变动，避免每新增一个产物就要手动补
# ALLOWED_REPO_OUTPUTS、漏一次就让整条主管线在 git 预检被一票否决（2026-06 停更事故根因）。
OUTPUT_DIR_PREFIXES = ("data/latest/", "data/recommendation_analytics/")


def is_allowed_output_path(path: str) -> bool:
    """该路径是否属于「允许的流水线输出」（显式白名单 或 输出目录前缀下的任意文件）。"""
    return path in ALLOWED_REPO_OUTPUTS or path.startswith(OUTPUT_DIR_PREFIXES)


AI_REQUIRED_PREFIXES = ("ai_",)
CACHE_PATTERN = re.compile(r"^(stk_factor|daily|cyq_perf)_(\d{8})\.parquet$")
GIT_TRACK_RE = re.compile(r"\[(?:ahead (?P<ahead>\d+))?(?:, )?(?:behind (?P<behind>\d+))?\]")
LOCAL_HTTP_PROXY = "http://127.0.0.1:7897"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_run_id() -> str:
    return os.environ.get("OPENCLAW_RUN_ID") or f"manual-{datetime.now().strftime('%Y%m%dT%H%M%S')}"


def current_run_started_at() -> str:
    return os.environ.get("OPENCLAW_RUN_STARTED_AT") or now_utc_iso()


def attach_run_metadata(payload: dict[str, Any], trade_date: str | None = None) -> dict[str, Any]:
    enriched = dict(payload)
    enriched.setdefault("generated_at", now_str())
    run = dict(enriched.get("run") or {})
    run.setdefault("run_id", current_run_id())
    run.setdefault("started_at", current_run_started_at())
    if trade_date:
        run["trade_date"] = trade_date
    elif os.environ.get("OPENCLAW_TARGET_TRADE_DATE"):
        run["trade_date"] = os.environ["OPENCLAW_TARGET_TRADE_DATE"]
    enriched["run"] = run
    return enriched


def normalize_code(code: Any) -> str:
    text = str(code or "").strip()
    return text.split(".")[0] if "." in text else text


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_if_exists(path: Path) -> dict[str, Any]:
    return load_json(path) if path.exists() else {}


def sanitize_json_value(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize_json_value(item) for key, item in value.items()}
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def write_health_snapshot(
    primary_path: Path,
    payload: dict[str, Any],
    trade_date: str | None = None,
    latest_name: str | None = None,
) -> Path:
    enriched = attach_run_metadata(payload, trade_date=trade_date)
    write_json(primary_path, enriched)
    if latest_name:
        latest_path = HEALTH_DIR / latest_name
        if latest_path != primary_path:
            write_json(latest_path, enriched)
    return primary_path


def write_canonical_object(kind: str, trade_date: str, payload: dict[str, Any]) -> Path:
    canonical = attach_run_metadata(payload, trade_date=trade_date)
    canonical["object"] = kind
    out = CANONICAL_DIR / f"{kind}_{trade_date}.json"
    write_json(out, canonical)
    write_json(CANONICAL_DIR / f"{kind}_latest.json", canonical)
    return out


def latest_common_trade_date() -> tuple[str | None, dict[str, str | None]]:
    by: dict[str, set[str]] = {"stk_factor": set(), "daily": set(), "cyq_perf": set()}
    for file_path in CACHE_DIR.glob("*.parquet"):
        match = CACHE_PATTERN.match(file_path.name)
        if match:
            by[match.group(1)].add(match.group(2))

    common = sorted(by["stk_factor"] & by["daily"] & by["cyq_perf"])
    requested = str(os.environ.get("OPENCLAW_TARGET_TRADE_DATE") or "").strip()
    if re.fullmatch(r"\d{8}", requested) and requested in common:
        latest = requested
    else:
        latest = common[-1] if common else None
    latest_by_type = {key: (sorted(values)[-1] if values else None) for key, values in by.items()}
    return latest, latest_by_type


def find_strategy(doc: dict[str, Any], strategy_id: str = TARGET_STRATEGY) -> dict[str, Any] | None:
    for strategy in doc.get("strategies", []):
        if strategy.get("id") == strategy_id:
            return strategy
    return None


def has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def ai_field_counts(items: list[dict[str, Any]], fields: list[str]) -> dict[str, int]:
    counts = {field: 0 for field in fields}
    for item in items:
        for field in fields:
            if has_value(item.get(field)):
                counts[field] += 1
    return counts


def has_explicit_labels(item: dict[str, Any]) -> bool:
    # 20260710修: ai_points是list(生成器规范), 原isinstance(...,str)把list整个丢弃→
    # 标签永拼不进text→检查永False。list时join各条目。
    text = ""
    for key in ("ai_summary", "ai_points"):
        value = item.get(key)
        if isinstance(value, str):
            text += value
        elif isinstance(value, (list, tuple)):
            text += "\n".join(str(p) for p in value)
    return all(label in text for label in EXPLICIT_LABELS)


def parse_percentish(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def in_range(value: Any, lower: float, upper: float) -> bool:
    number = parse_percentish(value)
    return number is not None and lower <= number <= upper


def data_json_trade_date(doc: dict[str, Any]) -> str | None:
    latest = doc.get("latest_trade_date")
    if latest:
        return str(latest)
    update_time = str(doc.get("update_time") or "")
    digits = "".join(ch for ch in update_time if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else None


def extract_yyyymmdd(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return ""


def resolve_effective_trade_date(*candidates: Any, fallback_now: bool = False) -> str:
    for candidate in candidates:
        normalized = extract_yyyymmdd(candidate)
        if normalized:
            return normalized
    if fallback_now:
        return datetime.now().strftime("%Y%m%d")
    return ""


def merge_item_fields(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    merged = dict(target)
    for key, value in source.items():
        if key in EXTENDED_FIELDS or key.startswith(AI_REQUIRED_PREFIXES):
            if has_value(value):
                merged[key] = value
    return merged


def merge_ai_map(target_map: dict[str, dict[str, Any]], items: list[dict[str, Any]]) -> None:
    for item in items:
        code = normalize_code(item.get("code"))
        if not code:
            continue
        current = target_map.get(code, {})
        target_map[code] = merge_item_fields(current, item)


def top_industries(items: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for item in items:
        industry = str(
            item.get("industry_name")
            or item.get("industry")
            or item.get("sector")
            or "未标注"
        ).strip()
        entry = buckets.setdefault(industry, {"industry": industry, "count": 0, "codes": []})
        entry["count"] += 1
        code = normalize_code(item.get("code"))
        if code:
            entry["codes"].append(code)

    ordered = sorted(
        (
            {
                "industry": payload["industry"],
                "count": payload["count"],
                "sample_codes": payload["codes"][:3],
            }
            for payload in buckets.values()
        ),
        key=lambda item: (-item["count"], item["industry"]),
    )
    return ordered[:limit]


def git_status(repo_dir: Path) -> dict[str, Any]:
    proc = subprocess.run(
        ["git", "status", "--short", "--branch"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    lines = proc.stdout.splitlines()
    branch_line = lines[0] if lines else ""
    ahead = 0
    behind = 0
    match = GIT_TRACK_RE.search(branch_line)
    if match:
        ahead = int(match.group("ahead") or 0)
        behind = int(match.group("behind") or 0)

    modified: list[str] = []
    untracked: list[str] = []
    other: list[str] = []
    for line in lines[1:]:
        if len(line) < 3:
            continue
        status = line[:2]
        path = line[3:].strip()
        if status == "??":
            untracked.append(path)
        elif status.strip():
            modified.append(path)
        else:
            other.append(path)

    return {
        "branch": branch_line,
        "ahead": ahead,
        "behind": behind,
        "modified": modified,
        "untracked": untracked,
        "other": other,
        "clean": not modified and not untracked and not other,
        "allowed_output_changes_only": (
            # 纯输出变动即放行：已改/新增（未跟踪）文件全部落在输出目录前缀或白名单内，
            # 且没有 rename/delete/conflict 等异常状态（other）。新增产物不再需要手动登记白名单。
            not other
            and all(is_allowed_output_path(path) for path in modified)
            and all(is_allowed_output_path(path) for path in untracked)
        ),
        "returncode": proc.returncode,
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-20:]),
    }


def detect_local_http_proxy(timeout_seconds: float = 0.2) -> str | None:
    for key in ("STOCK_SYSTEM_HTTP_PROXY", "OPENCLAW_HTTP_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value

    host = "127.0.0.1"
    port = 7897
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return LOCAL_HTTP_PROXY
    except OSError:
        return None


def git_network_env() -> dict[str, str]:
    env = os.environ.copy()
    proxy_url = detect_local_http_proxy()
    if proxy_url:
        env.setdefault("HTTP_PROXY", proxy_url)
        env.setdefault("HTTPS_PROXY", proxy_url)
        env.setdefault("http_proxy", proxy_url)
        env.setdefault("https_proxy", proxy_url)
        env.setdefault("NO_PROXY", "localhost,127.0.0.1")
        env.setdefault("no_proxy", "localhost,127.0.0.1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def run_git(
    args: list[str],
    *,
    cwd: Path,
    use_network: bool = False,
    timeout_seconds: int = 120,
) -> subprocess.CompletedProcess[str]:
    env = git_network_env() if use_network else None
    # errors="replace": push经代理时输出偶含非UTF-8字节(实测20260710夜 0xbc GBK),
    # 严格解码会让subprocess.run直接抛UnicodeDecodeError——push实际成功却被记为
    # 失败且丢返回码,发布仓ahead静默累积。git输出tail只作日志,允许有损解码。
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout_seconds,
    )


def sync_git_tracking_branch(
    repo_dir: Path,
    *,
    retries: int = 3,
    sleep_seconds: int = 3,
) -> dict[str, Any]:
    state = git_status(repo_dir)
    payload: dict[str, Any] = {
        "pre_sync_repo_state": state,
        "attempted": False,
        "skipped": False,
        "ok": False,
        "proxy_used": bool(detect_local_http_proxy()),
    }
    branch_line = str(state.get("branch") or "")
    has_tracking = "...origin/" in branch_line or "...origin" in branch_line

    if not has_tracking:
        payload["post_fetch_repo_state"] = state
        payload["skipped"] = True
        payload["ok"] = True
        payload["reason"] = "no origin tracking branch detected"
        return payload

    fetch_proc = run_git(["fetch", "--prune", "origin"], cwd=repo_dir, use_network=True, timeout_seconds=180)
    payload["git_fetch_returncode"] = fetch_proc.returncode
    payload["git_fetch_stdout_tail"] = "\n".join((fetch_proc.stdout or "").splitlines()[-20:])
    payload["git_fetch_stderr_tail"] = "\n".join((fetch_proc.stderr or "").splitlines()[-20:])
    if fetch_proc.returncode != 0:
        # Flaky GitHub/proxy SSL should not hard-block publish when we are not behind.
        # Local commit can still proceed; push will re-attempt network sync later.
        if int(state.get("behind") or 0) == 0:
            payload["skipped"] = True
            payload["ok"] = True
            payload["degraded"] = True
            payload["reason"] = "git fetch failed but local branch not behind; continue without network pull"
            payload["post_fetch_repo_state"] = state
            return payload
        payload["error"] = "git fetch origin failed"
        return payload

    state = git_status(repo_dir)
    payload["post_fetch_repo_state"] = state
    if state.get("behind", 0) == 0:
        payload["skipped"] = True
        payload["ok"] = True
        payload["reason"] = "tracking branch up to date after fetch; skipped network pull"
        return payload

    stderr_tail = ""
    stdout_tail = ""
    returncode: int | None = None
    payload["attempted"] = True
    for attempt in range(1, retries + 1):
        proc = run_git(["pull", "--rebase", "--autostash"], cwd=repo_dir, use_network=True, timeout_seconds=180)
        returncode = proc.returncode
        stdout_tail = "\n".join((proc.stdout or "").splitlines()[-20:])
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        payload["attempt_count"] = attempt
        if returncode == 0:
            payload["ok"] = True
            break
        if attempt < retries:
            time.sleep(sleep_seconds)

    payload["git_pull_returncode"] = returncode
    payload["git_pull_stdout_tail"] = stdout_tail
    payload["git_pull_stderr_tail"] = stderr_tail
    if not payload["ok"]:
        payload["error"] = f"git pull --rebase --autostash failed after {retries} attempts"
    return payload


def push_git_branch(
    repo_dir: Path,
    branch: str = "main",
    *,
    retries: int = 3,
    sleep_seconds: int = 3,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "attempted": True,
        "ok": False,
        "branch": branch,
        "proxy_used": bool(detect_local_http_proxy()),
    }
    stderr_tail = ""
    stdout_tail = ""
    returncode: int | None = None
    recovery_syncs: list[dict[str, Any]] = []
    for attempt in range(1, retries + 1):
        proc = run_git(["push", "origin", branch], cwd=repo_dir, use_network=True, timeout_seconds=180)
        returncode = proc.returncode
        stdout_tail = "\n".join((proc.stdout or "").splitlines()[-20:])
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        payload["attempt_count"] = attempt
        if returncode == 0:
            payload["ok"] = True
            break

        rejection_text = f"{proc.stdout or ''}\n{proc.stderr or ''}".lower()
        if attempt < retries and (
            "fetch first" in rejection_text
            or "non-fast-forward" in rejection_text
            or "[rejected]" in rejection_text
        ):
            recovery = sync_git_tracking_branch(repo_dir, retries=1, sleep_seconds=sleep_seconds)
            recovery["trigger"] = "push_rejected_requires_sync"
            recovery_syncs.append(recovery)
            if not recovery.get("ok"):
                break

        if attempt < retries:
            time.sleep(sleep_seconds)

    if recovery_syncs:
        payload["recovery_syncs"] = recovery_syncs
    payload["git_push_returncode"] = returncode
    payload["git_push_stdout_tail"] = stdout_tail
    payload["git_push_stderr_tail"] = stderr_tail
    if not payload["ok"]:
        payload["error"] = f"git push origin {branch} failed after {retries} attempts"
    return payload
