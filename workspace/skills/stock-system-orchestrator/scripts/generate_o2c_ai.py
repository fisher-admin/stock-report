#!/usr/bin/env python3
"""O2C Top20 独立 AI 分析生成器（结构化输出 + 断点续跑 + 失败隔离 + 写策略库）。

链路：
  greenfield_top20.json（含 factor_values / factor_zscores / composite_score）
    → 逐股构造「O2C 日内」结构化中文 prompt（只喂真实因子数值，禁止编造）
    → Copilot SDK 直调（2026-11-11 起：Node 子进程调 @github/copilot-sdk，主模型
      claude-haiku-5.5，失败回退 gpt-6-luna；STOCK_AI_TRANSPORT=litellm 可回退旧链）
    → 解析 + 校验结构化 JSON：ai_summary / ai_advice / ai_points[] / ai_risks[]
    → 写 O2C 策略库 greenfield_o2c_v1.db 的 ai_analysis 表（系统记录，upsert）
    → 写 ai_analysis/{date}_o2c_greenfield.json（发布聚合层 merge_strategy_ai 的 o2c_factor 入口）

诚实化铁律：
  - ai_score 恒为 null（O2C 暂不建可靠评分体系，绝不造假分）。
  - 策略处于研究观察 / 门槛未达标时，ai_advice 只能是「研究观察」或「观察」，不得「买入」。
  - 只依据 prompt 给出的真实因子数值分析，缺失字段不编造。
  - 单只失败不影响其他；自动重试；重试仍失败记录 status=failed + error（不静默跳过）。

Copilot SDK 直调见 llm_complete / _sdk_complete_once（2026-11-11）；旧的 LiteLLM 直连
路径仅在 STOCK_AI_TRANSPORT=litellm 时经 stock_model_gateway 调用。

用法：
  daily_stock_analysis/venv/bin/python3 generate_o2c_ai.py [--trade-date YYYYMMDD] [--force]
       [--max-workers N] [--attempts N] [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[3]
LATEST_DIR = WORKSPACE / "stock-report" / "data" / "latest"
GREENFIELD_PUBLISHED = LATEST_DIR / "greenfield_top20.json"
GREENFIELD_SNAPSHOT = WORKSPACE / "factor_factory" / "outputs" / "greenfield_multifactor" / "current_selection_snapshot.json"
RECOMMENDATION_STATE = LATEST_DIR / "recommendation_state.json"
O2C_DB = WORKSPACE / "stock_data" / "03-working" / "strategy_research" / "greenfield_o2c_v1" / "greenfield_o2c_v1.db"
AI_ANALYSIS_DIR = WORKSPACE / "stock_data" / "03-working" / "ai_analysis"
ENV_PATH = WORKSPACE / "daily_stock_analysis" / ".env"

STRATEGY_ID = "greenfield_o2c_v1"
PROMPT_VERSION = "o2c_ai_v1"
SOURCE_KIND = "o2c_ai_analysis"
ANALYSIS_TYPE = "o2c_ai"

# 6 个 O2C 因子的白话标签与默认权重（与 factor_factory/scripts/daily_o2c_selection.py 对齐）。
FACTOR_LABELS = {
    "g_intraday_range_expansion": "日内波幅扩展",
    "g_intraday_vwap_deviation": "日内VWAP偏离",
    "g_close_strength_ratio": "收盘强度比",
    "g_chip_pullback_support": "筹码回踩支撑",
    "g_long_cost_concentration": "长线成本集中",
    "g_volume_price_divergence": "量价背离",
}
FACTOR_WEIGHTS = {
    "g_intraday_range_expansion": 0.3557,
    "g_intraday_vwap_deviation": 0.3074,
    "g_close_strength_ratio": 0.2606,
    "g_chip_pullback_support": 0.0431,
    "g_long_cost_concentration": 0.0176,
    "g_volume_price_divergence": 0.0156,
}

ADVICE_ALLOWED = {"研究观察", "观察", "回避", "买入"}


# --------------------------------------------------------------------------- #
# .env + LLM 网关（直连 litellm，避开不稳定的 LLMToolAdapter Router 包装）
# --------------------------------------------------------------------------- #
def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def model_chain(env: dict[str, str]) -> list[str]:
    primary = env.get("LITELLM_MODEL") or "openai/mimo-v2.5-pro"
    fallbacks = [m.strip() for m in (env.get("LITELLM_FALLBACK_MODELS") or "").split(",") if m.strip()]
    chain = [primary] + [m for m in fallbacks if m != primary]
    return chain


# --------------------------------------------------------------------------- #
# Copilot SDK 直调（2026-11-11）：选股 AI 不再走 LiteLLM，改由 Node 子进程
# 调 @github/copilot-sdk（/srv/hermes-stock/tools/copilot-sdk/）。主模型
# claude-haiku-5.5、备用 gpt-6-luna，均为 Copilot 最便宜档（$0.10/$0.50 每百万
# token；Haiku 智能指数 36，高于 Luna 的 28）。回退：STOCK_AI_TRANSPORT=litellm
# 时走旧的 stock_model_gateway / LiteLLM 路径（.env 里 LITELLM_* 保持不动）。
# --------------------------------------------------------------------------- #
COPILOT_SDK_RUNNER = WORKSPACE.parent / "tools" / "copilot-sdk" / "copilot_analyze.mjs"
COPILOT_SDK_TOKEN_FILE = Path.home() / ".copilot" / "config.json"


def copilot_model_chain(env: dict[str, str]) -> list[str]:
    primary = (env.get("COPILOT_SDK_MODEL") or "claude-haiku-5.5").strip()
    fallbacks = [m.strip() for m in (env.get("COPILOT_SDK_FALLBACK_MODELS") or "gpt-6-luna").split(",") if m.strip()]
    return [primary] + [m for m in fallbacks if m != primary]


def copilot_transport_enabled(env: dict[str, str]) -> bool:
    transport = (env.get("STOCK_AI_TRANSPORT") or os.environ.get("STOCK_AI_TRANSPORT") or "copilot_sdk").strip().lower()
    return transport != "litellm"


def effective_model_chain(env: dict[str, str]) -> list[str]:
    return copilot_model_chain(env) if copilot_transport_enabled(env) else model_chain(env)


def _sdk_complete_once(prompt: str, model: str, *, max_tokens: int, timeout: int) -> dict[str, Any]:
    node = shutil.which("node") or "/usr/bin/node"
    try:
        proc = subprocess.run(
            [node, str(COPILOT_SDK_RUNNER), "--model", model,
             "--max-tokens", str(max_tokens), "--timeout", str(timeout)],
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "model": model, "content": "", "error": f"{model}: sdk_timeout"}
    except FileNotFoundError:
        return {"ok": False, "model": model, "content": "", "error": f"{model}: node_not_found"}
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "model": model, "content": "",
                "error": f"{model}: {(err[-1][:200] if err else 'sdk_failed')}"}
    content = proc.stdout.decode("utf-8", "replace")
    if not content.strip():
        return {"ok": False, "model": model, "content": "", "error": f"{model}: empty_response"}
    return {"ok": True, "model": model, "content": content, "error": None}


from stock_model_gateway import (
    call_model_gateway,
    read_hermes_copilot_tokens,
    read_hermes_xai_token,
)


def _copilot_request_headers() -> dict[str, str]:
    hermes_home = Path(
        os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    ).expanduser()
    hermes_agent = hermes_home / "hermes-agent"
    if str(hermes_agent) not in sys.path:
        sys.path.insert(0, str(hermes_agent))
    try:
        from hermes_cli.copilot_auth import copilot_request_headers
        return copilot_request_headers(is_agent_turn=True)
    except Exception:
        return {}


def _adc_available() -> bool:
    return (Path.home() / ".config/gcloud/application_default_credentials.json").exists()


def prepare_runtime_env(env: dict[str, str]) -> dict[str, str]:
    runtime_env = dict(env)
    if runtime_env.get("VERTEXAI_PROJECT") and not runtime_env.get("GOOGLE_CLOUD_PROJECT"):
        runtime_env["GOOGLE_CLOUD_PROJECT"] = runtime_env["VERTEXAI_PROJECT"]

    # 1. Resolve Copilot credentials
    try:
        copilot_token, copilot_base = read_hermes_copilot_tokens()
        if copilot_token:
            runtime_env["COPILOT_API_TOKEN"] = copilot_token
            runtime_env["COPILOT_API_BASE"] = copilot_base
    except Exception as exc:
        runtime_env["COPILOT_AUTH_ERROR"] = str(exc)[:300]

    # 2. Resolve xAI credentials from Hermes auth.json or env (pure fallback, non-blocking)
    try:
        xai_token = read_hermes_xai_token()
        if xai_token:
            runtime_env["XAI_API_KEY"] = xai_token
            runtime_env.setdefault("XAI_REASONING_EFFORT", "medium")
    except Exception as exc:
        runtime_env["XAI_AUTH_ERROR"] = str(exc)[:300]

    return runtime_env


def has_model_credentials(env: dict[str, str], models: list[str]) -> bool:
    if copilot_transport_enabled(env):
        # SDK 直调只依赖 Copilot CLI 的登录凭证（~/.copilot/config.json，2026-11-11 已登录 fisher-admin）。
        return COPILOT_SDK_TOKEN_FILE.is_file() or bool(os.environ.get("COPILOT_GITHUB_TOKEN"))
    for model in models:
        low = model.lower()
        if low.startswith(("copilot/", "github_copilot/")):
            if env.get("COPILOT_API_TOKEN"):
                return True
            try:
                token, _base = read_hermes_copilot_tokens()
                if token:
                    return True
            except Exception:
                pass
            continue
        if low.startswith("xai/") and (env.get("XAI_API_KEY") or read_hermes_xai_token()):
            return True
        if low.startswith(("gemini/", "vertex_ai/")) and _adc_available():
            return True
        if not low.startswith(("xai/", "gemini/", "vertex_ai/")) and env.get("OPENAI_API_KEY"):
            return True
    return False


def extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = cleaned[start : i + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    return None
    return None


def llm_complete(prompt: str, env: dict[str, str], models: list[str], *, max_tokens: int, timeout: int) -> dict[str, Any]:
    """单次（含模型回退）调用。默认走 Copilot SDK 直调（Haiku 5.5 主、Luna 备）；
    STOCK_AI_TRANSPORT=litellm 时回退到旧的 stock_model_gateway / LiteLLM 路径。"""
    if not copilot_transport_enabled(env):
        return call_model_gateway(prompt, models, max_tokens=max_tokens, timeout=timeout)
    last_error = ""
    for model in copilot_model_chain(env):
        res = _sdk_complete_once(prompt, model, max_tokens=max_tokens, timeout=timeout)
        if res.get("ok"):
            return res
        last_error = res.get("error") or "sdk_failed"
    return {"ok": False, "content": "", "model": None, "error": last_error or "All Copilot SDK models failed"}


# --------------------------------------------------------------------------- #
# 候选 + prompt
# --------------------------------------------------------------------------- #
def load_o2c_items(trade_date_hint: str | None) -> tuple[str, list[dict[str, Any]]]:
    payload: dict[str, Any] = {}
    for path in (GREENFIELD_PUBLISHED, GREENFIELD_SNAPSHOT):
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                break
            except json.JSONDecodeError:
                continue
    items = payload.get("top20") or payload.get("items") or []
    trade_date = str(trade_date_hint or payload.get("trade_date") or payload.get("source_date") or "")
    return trade_date, [it for it in items if isinstance(it, dict)][:20]


def load_strategy_state() -> dict[str, Any]:
    """读取 O2C 当前研究观察/门槛状态与每股 final_action，用于约束 advice。"""
    state: dict[str, Any] = {"research_only": True, "gate_status": "warn", "actions": {}}
    try:
        rec = json.loads(RECOMMENDATION_STATE.read_text(encoding="utf-8"))
        s = (rec.get("strategies") or {}).get(STRATEGY_ID) or {}
        state["research_only"] = bool(s.get("research_only", True))
        state["gate_status"] = ((s.get("strategy_gate") or {}).get("status")) or "warn"
        for it in s.get("items") or []:
            code = norm_code(it.get("code") or it.get("stock_code") or it.get("ts_code"))
            if code:
                state["actions"][code] = it.get("final_action") or "watch"
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return state


def norm_code(value: Any) -> str:
    m = re.search(r"(\d{6})", str(value or ""))
    return m.group(1) if m else ""


def fnum(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_prompt(stock: dict[str, Any], state: dict[str, Any]) -> str:
    name = stock.get("name") or stock.get("stock_name") or stock.get("code") or stock.get("ts_code")
    ts = stock.get("ts_code") or stock.get("code")
    industry = stock.get("industry") or stock.get("industry_name") or "未标注"
    rank = stock.get("rank_no") or stock.get("rank") or "?"
    close = fnum(stock.get("close"))
    pct = fnum(stock.get("pct_chg"))
    comp = fnum(stock.get("composite_score"))
    fv = stock.get("factor_values") or {}
    fz = stock.get("factor_zscores") or {}

    lines = []
    for key, label in FACTOR_LABELS.items():
        v = fnum(fv.get(key))
        z = fnum(fz.get(key))
        if v is None and z is None:
            continue
        lines.append(
            f"- {label}（{key}）：原值={v:+.3f} | 截面Z={z:+.2f} | 权重={FACTOR_WEIGHTS.get(key, 0):.0%}"
            if v is not None and z is not None
            else f"- {label}（{key}）：数据缺失"
        )
    factor_block = "\n".join(lines) if lines else "（本期无可用因子读数）"

    code = norm_code(stock.get("code") or ts)
    action = state["actions"].get(code, "watch")
    research_only = state["research_only"]
    if research_only or action != "main":
        advice_rule = "该策略当前为【研究观察】（未通过样本外硬门槛），你的 ai_advice 只能是「研究观察」或「观察」，绝不允许「买入」。"
        advice_options = "研究观察 | 观察"
    else:
        advice_rule = "你的 ai_advice 取值范围：买入 | 观察 | 研究观察 | 回避。"
        advice_options = "买入 | 观察 | 研究观察 | 回避"

    head = (
        f"股票：{name} {ts} | 行业：{industry} | O2C策略排名：第{rank}/20\n"
        f"当前价：{close if close is not None else '—'} | 当日涨跌：{pct:+.2f}% | O2C综合分(截面)：{comp:+.3f}"
        if pct is not None and comp is not None
        else f"股票：{name} {ts} | 行业：{industry} | O2C策略排名：第{rank}/20\n当前价：{close if close is not None else '—'}"
    )

    return f"""你是A股日内量化分析师。下面这只股票来自 O2C（Open-to-Close）日内因子策略，目标是判断它当日从开盘到收盘的相对机会、风险与执行条件。

{head}
策略状态：{advice_rule}

O2C 因子证据（原值 / 截面Z分数 / IC权重）：
{factor_block}

分析要求：
1) 只依据上面给出的真实因子数值与价格涨跌做判断，不要编造未给出的消息面、财报、目标价。
2) 必须解释「为什么最终动作是 {('观察/研究观察' if (research_only or action != 'main') else action)}」。
3) ai_points 必须依次覆盖：日内机会判断 / 核心因子贡献 / 量价结构 / 筹码与成本结构 / 执行条件。
4) ai_risks 给出 2 条最关键风险。

严格只输出如下 JSON（不要任何多余文字、不要 markdown 围栏）：
{{"ai_summary":"一句话总体判断（<=60字）","ai_advice":"{advice_options}","ai_points":["日内机会判断：...","核心因子贡献：...","量价结构：...","筹码/成本结构：...","执行条件：..."],"ai_risks":["...","..."]}}"""


def clamp_advice(advice: str, state: dict[str, Any], code: str) -> str:
    advice = (advice or "").strip()
    research_only = state["research_only"] or state["actions"].get(code, "watch") != "main"
    if advice not in ADVICE_ALLOWED:
        # 容错：从文本里挑一个允许值
        for cand in ("研究观察", "观察", "回避", "买入"):
            if cand in advice:
                advice = cand
                break
        else:
            advice = "观察"
    if research_only and advice == "买入":
        advice = "研究观察"
    return advice


def validate_result(obj: dict[str, Any]) -> str | None:
    if not isinstance(obj, dict):
        return "not_dict"
    if not str(obj.get("ai_summary") or "").strip():
        return "empty_summary"
    pts = obj.get("ai_points")
    if not isinstance(pts, list) or len([p for p in pts if str(p).strip()]) < 3:
        return "points_lt_3"
    rks = obj.get("ai_risks")
    if not isinstance(rks, list) or len([r for r in rks if str(r).strip()]) < 1:
        return "risks_lt_1"
    return None


# --------------------------------------------------------------------------- #
# O2C 策略库 ai_analysis 表
# --------------------------------------------------------------------------- #
def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_analysis (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          strategy_id TEXT NOT NULL,
          trade_date TEXT NOT NULL,
          stock_code TEXT NOT NULL,
          ts_code TEXT,
          stock_name TEXT,
          ai_summary TEXT,
          ai_advice TEXT,
          ai_points_json TEXT,
          ai_risks_json TEXT,
          ai_source_kind TEXT,
          ai_analysis_type TEXT,
          ai_coverage_counted INTEGER,
          ai_score REAL,
          prompt_version TEXT,
          model_name TEXT,
          generated_at TEXT NOT NULL,
          attempts INTEGER,
          status TEXT,
          error TEXT,
          UNIQUE(strategy_id, trade_date, stock_code)
        )
        """
    )
    conn.commit()


def done_codes(conn: sqlite3.Connection, trade_date: str) -> set[str]:
    rows = conn.execute(
        "SELECT stock_code FROM ai_analysis WHERE strategy_id=? AND trade_date=? AND status='success'",
        (STRATEGY_ID, trade_date),
    ).fetchall()
    return {r[0] for r in rows}


def upsert(conn: sqlite3.Connection, rec: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO ai_analysis
          (strategy_id, trade_date, stock_code, ts_code, stock_name, ai_summary, ai_advice,
           ai_points_json, ai_risks_json, ai_source_kind, ai_analysis_type, ai_coverage_counted,
           ai_score, prompt_version, model_name, generated_at, attempts, status, error)
        VALUES
          (:strategy_id, :trade_date, :stock_code, :ts_code, :stock_name, :ai_summary, :ai_advice,
           :ai_points_json, :ai_risks_json, :ai_source_kind, :ai_analysis_type, :ai_coverage_counted,
           :ai_score, :prompt_version, :model_name, :generated_at, :attempts, :status, :error)
        ON CONFLICT(strategy_id, trade_date, stock_code) DO UPDATE SET
          ts_code=excluded.ts_code, stock_name=excluded.stock_name, ai_summary=excluded.ai_summary,
          ai_advice=excluded.ai_advice, ai_points_json=excluded.ai_points_json, ai_risks_json=excluded.ai_risks_json,
          ai_source_kind=excluded.ai_source_kind, ai_analysis_type=excluded.ai_analysis_type,
          ai_coverage_counted=excluded.ai_coverage_counted, ai_score=excluded.ai_score,
          prompt_version=excluded.prompt_version, model_name=excluded.model_name,
          generated_at=excluded.generated_at, attempts=excluded.attempts, status=excluded.status, error=excluded.error
        """,
        rec,
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# 单只生成（含重试）
# --------------------------------------------------------------------------- #
def generate_one(stock: dict[str, Any], state: dict[str, Any], env: dict[str, str], models: list[str],
                 trade_date: str, *, attempts: int, max_tokens: int, timeout: int, now: str) -> dict[str, Any]:
    code = norm_code(stock.get("code") or stock.get("stock_code") or stock.get("ts_code"))
    ts = stock.get("ts_code") or code
    name = stock.get("name") or stock.get("stock_name") or code
    prompt = build_prompt(stock, state)
    last_err = ""
    used_model = ""
    for attempt in range(1, attempts + 1):
        res = llm_complete(prompt, env, models, max_tokens=max_tokens, timeout=timeout)
        used_model = res.get("model") or used_model
        if not res.get("ok"):
            last_err = res.get("error") or "llm_failed"
        else:
            obj = extract_json(res.get("content") or "")
            if obj is None:
                last_err = "json_parse_failed"
            else:
                verr = validate_result(obj)
                if verr:
                    last_err = verr
                else:
                    advice = clamp_advice(str(obj.get("ai_advice") or ""), state, code)
                    points = [str(p).strip() for p in obj.get("ai_points") if str(p).strip()]
                    risks = [str(r).strip() for r in obj.get("ai_risks") if str(r).strip()]
                    return {
                        "status": "success", "code": code, "ts_code": ts, "name": name,
                        "ai_summary": str(obj.get("ai_summary")).strip(), "ai_advice": advice,
                        "ai_points": points, "ai_risks": risks, "model": used_model,
                        "attempts": attempt, "error": None,
                    }
        if attempt < attempts:
            time.sleep(min(8.0, 1.5 * attempt))
    return {
        "status": "failed", "code": code, "ts_code": ts, "name": name,
        "ai_summary": None, "ai_advice": None, "ai_points": [], "ai_risks": [],
        "model": used_model, "attempts": attempts, "error": last_err,
    }


def to_db_record(r: dict[str, Any], trade_date: str, now: str) -> dict[str, Any]:
    ok = r["status"] == "success"
    return {
        "strategy_id": STRATEGY_ID, "trade_date": trade_date, "stock_code": r["code"],
        "ts_code": r["ts_code"], "stock_name": r["name"],
        "ai_summary": r["ai_summary"], "ai_advice": r["ai_advice"],
        "ai_points_json": json.dumps(r["ai_points"], ensure_ascii=False),
        "ai_risks_json": json.dumps(r["ai_risks"], ensure_ascii=False),
        "ai_source_kind": SOURCE_KIND if ok else "o2c_ai_failed",
        "ai_analysis_type": ANALYSIS_TYPE if ok else "none",
        "ai_coverage_counted": 1 if ok else 0,
        "ai_score": None,
        "prompt_version": PROMPT_VERSION, "model_name": r["model"],
        "generated_at": now, "attempts": r["attempts"],
        "status": r["status"], "error": r["error"],
    }


def to_publish_record(r: dict[str, Any], trade_date: str, now: str) -> dict[str, Any]:
    """供 strategy_publication_layer.merge_strategy_ai 的 o2c_factor 入口读取（{date}_o2c_greenfield.json）。"""
    return {
        "code": r["code"], "ts_code": r["ts_code"], "name": r["name"],
        "strategy_source": "o2c_factor", "strategy_name": "O2C日内因子",
        "ai_source_kind": SOURCE_KIND, "ai_analysis_type": ANALYSIS_TYPE,
        "ai_summary": r["ai_summary"], "ai_advice": r["ai_advice"],
        "operation_advice": r["ai_advice"], "ai_points": r["ai_points"], "ai_risks": r["ai_risks"],
        "ai_score": None, "ai_coverage_counted": True,
        "ai_source_date": trade_date, "ai_analysis_date": trade_date, "trade_date": trade_date,
        "model_name": r["model"], "generated_at": now, "ai_evidence_time": now,
        "rank_change": 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade-date", default=os.environ.get("OPENCLAW_TARGET_TRADE_DATE"))
    ap.add_argument("--force", action="store_true", help="忽略续跑，重算全部 20 只")
    ap.add_argument("--max-workers", type=int, default=4)
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=int(os.environ.get("OPENCLAW_O2C_AI_MAX_TOKENS", "4500")))
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("OPENCLAW_O2C_AI_TIMEOUT_SECONDS", "120")))
    ap.add_argument("--limit", type=int, default=0, help=">0 时只跑前 N 只（调试）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将处理的候选，不调用 LLM")
    args = ap.parse_args()

    trade_date, items = load_o2c_items(args.trade_date)
    if not trade_date or not items:
        print(json.dumps({"ok": False, "reason": "no_o2c_items", "trade_date": trade_date}, ensure_ascii=False))
        return 1
    if args.limit > 0:
        items = items[: args.limit]

    try:
        env = prepare_runtime_env(load_env(ENV_PATH))
        # 2026-11-10 免费模型切换：把 .env 里的 key（如 OPENROUTER_API_KEY）注入进程环境，
        # 供 LiteLLM 按 provider 自行取用；已有环境变量优先，不覆盖。
        for _k, _v in env.items():
            if _v and _k not in os.environ:
                os.environ[_k] = _v
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "reason": "xai_auth_unavailable", "error": str(exc)[:300]}, ensure_ascii=False))
        return 1
    models = effective_model_chain(env)
    state = load_strategy_state()
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    O2C_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(O2C_DB), timeout=30)
    ensure_table(conn)
    already = set() if args.force else done_codes(conn, trade_date)

    pending = [it for it in items if norm_code(it.get("code") or it.get("stock_code") or it.get("ts_code")) not in already]
    print(f"[o2c-ai] trade_date={trade_date} total={len(items)} done={len(already)} pending={len(pending)} "
          f"models={models} workers={args.max_workers} attempts={args.attempts}")

    if args.dry_run:
        for it in pending:
            print("  pending:", norm_code(it.get("code") or it.get("ts_code")), it.get("name"))
        conn.close()
        return 0

    if not ENV_PATH.exists() or not has_model_credentials(env, models):
        print(json.dumps({"ok": False, "reason": "missing_env_or_key", "env_path": str(ENV_PATH)}, ensure_ascii=False))
        conn.close()
        return 1

    results: list[dict[str, Any]] = []
    if pending:
        with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            futs = {
                ex.submit(generate_one, it, state, env, models, trade_date,
                          attempts=args.attempts, max_tokens=args.max_tokens, timeout=args.timeout, now=now): it
                for it in pending
            }
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                upsert(conn, to_db_record(r, trade_date, now))
                tag = "OK " if r["status"] == "success" else "FAIL"
                print(f"  [{tag}] {r['code']} {r['name']} attempts={r['attempts']} "
                      f"{'advice=' + str(r['ai_advice']) if r['status'] == 'success' else 'err=' + str(r['error'])}")

    # 汇总 DB 里本交易日全部成功记录 → 写发布层入口 JSON（含本次 + 历史续跑成功）。
    rows = conn.execute(
        "SELECT stock_code, ts_code, stock_name, ai_summary, ai_advice, ai_points_json, ai_risks_json, model_name "
        "FROM ai_analysis WHERE strategy_id=? AND trade_date=? AND status='success'",
        (STRATEGY_ID, trade_date),
    ).fetchall()
    publish_records = [
        to_publish_record(
            {
                "code": r[0], "ts_code": r[1], "name": r[2], "ai_summary": r[3], "ai_advice": r[4],
                "ai_points": json.loads(r[5] or "[]"), "ai_risks": json.loads(r[6] or "[]"), "model": r[7],
            },
            trade_date, now,
        )
        for r in rows
    ]
    AI_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AI_ANALYSIS_DIR / f"{trade_date}_o2c_greenfield.json"
    out_path.write_text(json.dumps(publish_records, ensure_ascii=False, indent=2), encoding="utf-8")

    # 失败状态记录（用户合同：不静默跳过）。
    fail_rows = conn.execute(
        "SELECT stock_code, ts_code, stock_name, attempts, error FROM ai_analysis "
        "WHERE strategy_id=? AND trade_date=? AND status='failed'",
        (STRATEGY_ID, trade_date),
    ).fetchall()
    status_path = AI_ANALYSIS_DIR / f"o2c_ai_status_{trade_date}.json"
    status_payload = {
        "strategy_id": STRATEGY_ID, "trade_date": trade_date, "generated_at": now,
        "total": len(items), "success": len(publish_records), "failed": len(fail_rows),
        "coverage": round(len(publish_records) / len(items), 3) if items else 0.0,
        "failures": [
            {"code": f[0], "ts_code": f[1], "name": f[2], "attempts": f[3], "error": f[4]} for f in fail_rows
        ],
    }
    status_path.write_text(json.dumps(status_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()

    summary = {
        "ok": status_payload["failed"] == 0,
        "trade_date": trade_date,
        "total": status_payload["total"],
        "success": status_payload["success"],
        "failed": status_payload["failed"],
        "coverage": status_payload["coverage"],
        "publish_json": str(out_path),
        "status_json": str(status_path),
        "db": str(O2C_DB),
        "failures": status_payload["failures"],
    }
    print("[o2c-ai] " + json.dumps(summary, ensure_ascii=False))
    return 0 if status_payload["success"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
