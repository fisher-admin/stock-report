#!/usr/bin/env python3
"""Retry Stage 4 until AI coverage is complete or a wait budget is exhausted."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from orchestrator_common import (
    HEALTH_DIR,
    TARGET_STRATEGY,
    WORKING_STRATEGY_JSON,
    WORKSPACE,
    find_strategy,
    load_json,
    normalize_code,
    now_str,
    write_json,
)
import stage4_ai_publish


LOOP_HEALTH_PATH = HEALTH_DIR / "ai_backfill_loop.json"
GIVEUP_PATH = HEALTH_DIR / "ai_backfill_giveup.json"
FISHER_BRIDGE_PATH = WORKSPACE / "daily_stock_analysis/bridge_selection_to_analysis.py"
FISHER_BRIDGE_PYTHON = WORKSPACE / "daily_stock_analysis/venv/bin/python3"
# 20260708: prebreakout传统策略改用直连litellm生成器(与O2C同款,替换易卡死的daily_stock_analysis
# 老桥接)。老桥接慢性失效致prebreakout AI连日全放弃→base_only降级; 新生成器实测稳定, 直接写
# ai_analysis/{date}.json(无需DB→JSON二次同步)。
PREBREAKOUT_AI_PATH = WORKSPACE / "skills/stock-system-orchestrator/scripts/generate_prebreakout_ai.py"
# 20260716: O2C 改走专用 generate_o2c_ai.py（ai_score 恒 null + 写 {date}_o2c_greenfield.json），
# 不再用 write_o2c_ai_analysis 内联路径（易写出空壳/非 null score，合同校验 0/20 整仓锁死）。
O2C_AI_PATH = WORKSPACE / "skills/stock-system-orchestrator/scripts/generate_o2c_ai.py"
# 20260726: O2C 完成判定与 generate_o2c_ai.py(SOURCE_KIND='o2c_ai_analysis')、发布层 _row_ai_counted、
# 合同 13 号检查对齐。0715 起判定只认旧值 'o2c_factor_llm_analysis'（仅老内联路径会写），生成器明明
# success=20 仍被整批再列 pending，回填循环每晚空转烧满 2700s 预算并产生 ~30 个无效 publish push。
# 旧值保留在集合里仅为兼容历史店内数据；'o2c_ai_failed' 行不在集合内，仍视为未完成。
O2C_LLM_SOURCE_KINDS = {"o2c_ai_analysis", "o2c_factor_llm_analysis"}
DAILY_ANALYSIS_ROOT = WORKSPACE / "daily_stock_analysis"
BRIDGE_BLOCKED_CODES = {"000001"}
# 单只股票在同一交易日连续失败多少轮后「放弃」，不再拖垮整批/不再让回填循环卡死超时（2026-06-28 硬化）。
# 用 os.environ 直读（env_int 在本文件较后定义，模块级此处尚不可用）。
def _env_int_early(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or default).strip())
    except (TypeError, ValueError):
        return default


MAX_CODE_ATTEMPTS = _env_int_early("OPENCLAW_AI_BACKFILL_MAX_CODE_ATTEMPTS", 3)
# 单批 fisher_bridge 子进程超时（秒）：防止某只股票把 LLM 调用挂死导致整轮无限阻塞。
BRIDGE_SUBPROCESS_TIMEOUT = _env_int_early("OPENCLAW_AI_BACKFILL_BRIDGE_TIMEOUT_SECONDS", 1500)
# O2C 20 股并行生成往往 10–25 分钟；单独放宽，避免默认 1500s 与主线 aiBackfill 抢时被杀。
O2C_AI_SUBPROCESS_TIMEOUT = _env_int_early("OPENCLAW_AI_BACKFILL_O2C_TIMEOUT_SECONDS", 2400)


def _load_giveup(trade_date: str) -> dict[str, int]:
    """读当日「放弃」计数 {code: fail_count}。"""
    try:
        data = json.loads(GIVEUP_PATH.read_text(encoding="utf-8"))
        return dict((data or {}).get(str(trade_date)) or {})
    except Exception:
        return {}


def _save_giveup(trade_date: str, counts: dict[str, int]) -> None:
    try:
        data = {}
        if GIVEUP_PATH.exists():
            data = json.loads(GIVEUP_PATH.read_text(encoding="utf-8")) or {}
        data[str(trade_date)] = counts
        GIVEUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        GIVEUP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def reset_giveup(trade_date: str) -> bool:
    """Clear one explicit backfill date without disturbing historical retry state."""
    try:
        data = json.loads(GIVEUP_PATH.read_text(encoding="utf-8")) if GIVEUP_PATH.exists() else {}
        if str(trade_date) not in data:
            return False
        data.pop(str(trade_date), None)
        GIVEUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        GIVEUP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def giveup_codes(trade_date: str) -> set[str]:
    """已放弃（失败次数达上限）的 code 集合——pending 时排除，避免 1 只卡死整轮。"""
    return {c for c, n in _load_giveup(trade_date).items() if int(n or 0) >= MAX_CODE_ATTEMPTS}


def _has_real_summary(item: dict[str, Any]) -> bool:
    """判定一条 AI 记录是否含真实摘要（非失败壳）。失败标记串/空摘要均判为假。"""
    if not isinstance(item, dict):
        return False
    if item.get("success") is False:
        return False
    text = " ".join(str(item.get(k) or "") for k in ("analysis_summary", "ai_summary"))
    dash = (item.get("dashboard") or {}).get("core_conclusion") or {}
    text += " " + str(dash.get("one_sentence") or "")
    text = text.strip()
    if not text:
        return False
    for marker in ("分析过程出错", "All LLM models failed", "APIConnectionError", "模型调用失败", "LLM models failed"):
        if marker in text:
            return False
    return True
O2C_FACTOR_PROMPT_TEMPLATE = """你是一个 A 股日内量化分析师。以下股票被 O2C（Open-to-Close）多因子模型选中。

## 股票信息
- 代码: {code}
- 名称: {name}
- 行业: {industry}
- 综合评分: {score}

## 因子得分详情
| 因子 | 值 | 权重 | IC |
|------|-----|------|-----|
| VWAP偏离度 | {vwap_deviation} | 30.7% | 0.025 |
| 量价背离度 | {volume_price_divergence} | 1.6% | 0.018 |
| 筹码回调支撑 | {chip_pullback_support} | 4.3% | 0.008 |
| 成本集中度 | {long_cost_concentration} | 1.8% | 0.008 |
| 收盘强度比 | {close_strength_ratio} | 26.1% | 0.018 |
| 日内振幅扩张 | {intraday_range_expansion} | 35.6% | 0.025 |

## 模型验证结果
- Walk-forward: {walkforward}
- O2C Sharpe: {o2c_sharpe}
- OOS Sharpe: {oos_sharpe}
- 门控: FDR + Newey-West + 噪声测试全部通过

## 请从以下角度分析（限300字）
1. 日内机会判断
2. 驱动因子分析
3. 风险提示
4. 操作建议
"""


def env_int(name: str, default: int) -> int:
    import os

    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def load_readiness() -> dict:
    path = HEALTH_DIR / "ai_publish_readiness.json"
    if not path.exists():
        return {}
    try:
        return load_json(path)
    except Exception:
        return {}


def pending_codes(readiness: dict | None = None) -> list[str]:
    checks = (readiness or {}).get("checks") or {}
    # 当日已「放弃」（连续失败达上限）的 code 一律排除，避免 1 只卡死整轮回填。
    trade_date = str(checks.get("strategy_latest_trade_date") or "")
    skip = set(BRIDGE_BLOCKED_CODES) | (giveup_codes(trade_date) if trade_date else set())
    explicit = []
    for code in checks.get("unexpected_ai_provenance") or []:
        normalized = normalize_code(code)
        if normalized and normalized not in skip and normalized not in explicit:
            explicit.append(normalized)
    if explicit:
        return explicit

    if not WORKING_STRATEGY_JSON.exists():
        return []
    try:
        strategy_doc = load_json(WORKING_STRATEGY_JSON)
    except Exception:
        return []
    target = find_strategy(strategy_doc, TARGET_STRATEGY) or {}
    codes = []
    for item in target.get("top20", []):
        status = item.get("ai_status")
        provenance = item.get("ai_provenance")
        if status == "ready" and provenance not in {"stale_historical", "failed"}:
            continue
        # Late evidence is terminal for this target date; retrying cannot move
        # its generation timestamp back into the source trading day.
        if provenance == "delayed_analysis" or item.get("ai_temporal_class") == "delayed_analysis":
            continue
        code = normalize_code(item.get("code"))
        if code and code not in skip:
            codes.append(code)
    return codes


def delayed_ai_terminal(readiness: dict, pending: list[str], pending_o2c: list[str]) -> bool:
    """Allow base selection to finish when only late AI remains."""
    checks = readiness.get("checks") or {}
    classes = checks.get("ai_temporal_classes") or {}
    return bool(
        readiness.get("base_ok")
        and readiness.get("local_materialized")
        and not readiness.get("ai_complete")
        and not pending
        and not pending_o2c
        and int(checks.get("ai_temporal_eligible_count") or 0) < 20
        and int(classes.get("delayed_analysis") or 0) > 0
    )


def first_greenfield_payload() -> tuple[dict[str, Any], Path | None]:
    for path in stage4_ai_publish.GREENFIELD_TOP20_CANDIDATES:
        if path.exists():
            try:
                return load_json(path), path
            except Exception:
                continue
    return {}, None


def factor_value(stock: dict[str, Any], key: str) -> Any:
    details = stock.get("factor_details") or {}
    value = details.get(key) if isinstance(details, dict) else None
    if isinstance(value, dict):
        return value.get("value")
    return value


def format_factor(value: Any) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "N/A"


def top_o2c_factor_names(stock: dict[str, Any], limit: int = 3) -> list[str]:
    details = stock.get("factor_details") or {}
    ranked: list[tuple[float, str]] = []
    if not isinstance(details, dict):
        return []
    for key, value in details.items():
        detail = value if isinstance(value, dict) else {"value": value}
        try:
            weight = abs(float(detail.get("weight") or detail.get("weight_pct") or 0))
        except (TypeError, ValueError):
            weight = 0.0
        ranked.append((weight, stage4_ai_publish.O2C_FACTOR_LABELS.get(str(key), str(key))))
    return [label for _, label in sorted(ranked, reverse=True)[:limit]]


def o2c_ai_score(raw_score: Any) -> int:
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return 68
    return max(45, min(90, round(60 + score * 7)))


def o2c_advice(ai_score: int) -> tuple[str, str, str]:
    if ai_score >= 78:
        return "买入", "主攻", "高"
    if ai_score >= 68:
        return "观望", "观察", "中"
    return "回避", "回避", "中"


def clean_ai_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    lines = [line.strip() for line in value.replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def advice_from_llm(text: str, fallback_score: int) -> tuple[str, str, str]:
    for marker in ("操作建议", "建议", "结论"):
        idx = text.find(marker)
        if idx >= 0:
            tail = text[idx : idx + 40]
            for advice in ("买入", "观望", "回避"):
                if advice in tail:
                    decision = "主攻" if advice == "买入" else advice
                    confidence = "高" if fallback_score >= 78 else "中"
                    return advice, decision, confidence
    return o2c_advice(fallback_score)


def call_o2c_factor_llm(prompt: str, code: str) -> dict[str, Any]:
    """20260710 Phase-1修(升级方案v3): 从daily_stock_analysis老库LLMToolAdapter切到
    同目录直连三件套(generate_o2c_ai.load_env/model_chain/llm_complete)。
    老adapter内含复杂fallback/thinking逻辑, 间歇性litellm'Provider List'错误
    (7/8与7/10两晚各致9-20只AI失败; 7/8曾误判为磁盘连带)。直连路径已在
    prebreakout(7/7修复)与O2C主生成器上验证稳定。"""
    if not env_bool("OPENCLAW_O2C_AI_USE_LLM", True):
        return {"ok": False, "skipped": True, "reason": "disabled_by_env"}
    try:
        from generate_o2c_ai import ENV_PATH as _ENV_PATH
        from generate_o2c_ai import llm_complete as _llm_complete
        from generate_o2c_ai import load_env as _load_env
        from generate_o2c_ai import model_chain as _model_chain
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "import_failed", "error": str(exc)}

    try:
        env = _load_env(_ENV_PATH)
        if not env.get("OPENAI_API_KEY"):
            return {"ok": False, "reason": "missing_key"}
        models = _model_chain(env)
        sys_prompt = "你只输出面向A股日内交易决策的简洁中文分析，不要复述任务说明。"
        # max_tokens默认4500(对齐generate_o2c_ai头注): mimo-v2.5-pro是推理模型, content前
        # 先消耗大量reasoning_tokens——旧默认700被推理吃光→empty_content(finish=length)。
        # 实测20260710夜: 700下5只失败4只, 全为此因; 7/8同症。
        res = _llm_complete(
            f"{sys_prompt}\n\n{prompt}",
            env,
            models,
            max_tokens=env_int("OPENCLAW_O2C_AI_MAX_TOKENS", 4500),
            timeout=env_int("OPENCLAW_O2C_AI_TIMEOUT_SECONDS", 180),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "llm_call_exception", "error": str(exc)}

    if not res.get("ok"):
        return {"ok": False, "reason": "llm_call_failed", "error": str(res.get("error") or "")[:200]}

    class _Resp:  # 兼容下游 getattr(response,"content") 的旧读法
        content = res.get("content") or ""

    response = _Resp()
    content = clean_ai_text(getattr(response, "content", None))
    if not content or "All LLM models failed" in content:
        return {
            "ok": False,
            "reason": "llm_call_failed",
            "content": content,
            "model": getattr(response, "model", ""),
            "provider": getattr(response, "provider", ""),
        }
    return {
        "ok": True,
        "content": content,
        "model": getattr(response, "model", ""),
        "provider": getattr(response, "provider", ""),
        "usage": getattr(response, "usage", {}),
        "code": code,
    }


def call_o2c_factor_llm_subprocess(prompt: str, code: str) -> dict[str, Any]:
    helper = r'''
import json
import sys

payload = json.loads(sys.stdin.read())
root = payload["root"]
if root not in sys.path:
    sys.path.insert(0, root)

from src.agent.llm_adapter import LLMToolAdapter

adapter = LLMToolAdapter()
response = adapter.call_text(
    [
        {
            "role": "system",
            "content": "你只输出面向A股日内交易决策的简洁中文分析，不要复述任务说明。",
        },
        {"role": "user", "content": payload["prompt"]},
    ],
    temperature=0.2,
    max_tokens=payload["max_tokens"],
    timeout=payload["timeout"],
)
content = (getattr(response, "content", None) or "").strip()
result = {
    "ok": bool(content) and "All LLM models failed" not in content,
    "content": content,
    "model": getattr(response, "model", ""),
    "provider": getattr(response, "provider", ""),
    "usage": getattr(response, "usage", {}),
    "code": payload["code"],
}
print("OPENCLAW_O2C_AI_JSON:" + json.dumps(result, ensure_ascii=False))
'''
    payload = {
        "root": str(DAILY_ANALYSIS_ROOT),
        "prompt": prompt,
        "code": code,
        "max_tokens": env_int("OPENCLAW_O2C_AI_MAX_TOKENS", 700),
        "timeout": env_int("OPENCLAW_O2C_AI_TIMEOUT_SECONDS", 180),
    }
    try:
        proc = subprocess.run(
            [str(FISHER_BRIDGE_PYTHON), "-c", helper],
            cwd=DAILY_ANALYSIS_ROOT,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=payload["timeout"] + 30,
        )
    except Exception as exc:
        return {"ok": False, "reason": "llm_subprocess_exception", "error": str(exc)}

    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("OPENCLAW_O2C_AI_JSON:"):
            try:
                result = json.loads(line.split(":", 1)[1])
            except Exception as exc:
                return {"ok": False, "reason": "llm_subprocess_parse_failed", "error": str(exc)}
            if not result.get("ok"):
                result.setdefault("reason", "llm_call_failed")
            return result
    return {
        "ok": False,
        "reason": "llm_subprocess_failed",
        "returncode": proc.returncode,
        "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-5:]),
        "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-5:]),
    }


def build_o2c_prompt(stock: dict[str, Any], factor_pool: dict[str, Any]) -> str:
    return O2C_FACTOR_PROMPT_TEMPLATE.format(
        code=normalize_code(stock.get("code") or stock.get("ts_code")),
        name=stock.get("name") or stock.get("stock_name") or "",
        industry=stock.get("industry_name") or stock.get("industry") or stock.get("sector_name") or "未标注",
        score=format_factor(stock.get("score")),
        vwap_deviation=format_factor(factor_value(stock, "g_intraday_vwap_deviation")),
        volume_price_divergence=format_factor(factor_value(stock, "g_volume_price_divergence")),
        chip_pullback_support=format_factor(factor_value(stock, "g_chip_pullback_support")),
        long_cost_concentration=format_factor(factor_value(stock, "g_long_cost_concentration")),
        close_strength_ratio=format_factor(factor_value(stock, "g_close_strength_ratio")),
        intraday_range_expansion=format_factor(factor_value(stock, "g_intraday_range_expansion")),
        walkforward=factor_pool.get("walkforward") or factor_pool.get("walkforward_pass_rate") or "样本外验证进行中，暂未提供",
        o2c_sharpe=factor_pool.get("o2c_sharpe") or factor_pool.get("oos_sharpe") or "暂未提供",
        oos_sharpe=factor_pool.get("oos_sharpe") or "暂未提供",
    )


def build_o2c_ai_record(
    stock: dict[str, Any],
    trade_date: str,
    factor_pool: dict[str, Any],
    llm_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = normalize_code(stock.get("code") or stock.get("ts_code"))
    name = stock.get("name") or stock.get("stock_name") or ""
    industry = stock.get("industry_name") or stock.get("industry") or stock.get("sector_name") or "未标注"
    llm_text = clean_ai_text((llm_result or {}).get("content"))
    has_llm = bool(llm_text)
    top_factors = top_o2c_factor_names(stock)
    top_factor_text = "、".join(top_factors) if top_factors else "核心日内因子"
    risk_text = "若开盘后量能不能延续、反转因子方向走弱，或同一行业拥挤度上升，应降级为观察。"
    # 因子客观描述：始终可给，但明确标「非 AI 分析」，只作展示备注，绝不冒充 AI 结论。
    factor_note = (
        f"【因子客观描述·非AI分析】{name} 被 O2C 日内因子选中，综合评分 "
        f"{format_factor(stock.get('score'))}，主要贡献来自 {top_factor_text}。"
    )
    # 公共/溯源字段（不含任何业务结论）
    record: dict[str, Any] = {
        "code": code,
        "ts_code": stock.get("ts_code") or stock.get("code"),
        "name": name,
        "industry_name": industry,
        "strategy_source": "o2c_factor",
        "strategy_name": "O2C日内因子",
        "ai_analysis_date": trade_date,
        "ai_source_date": trade_date,
        "ai_source_stale": False,
        "ai_o2c_factor_score": stock.get("score"),
        "ai_o2c_top_factors": top_factors,
        "ai_o2c_factor_note": factor_note,
        "ai_o2c_prompt": build_o2c_prompt(stock, factor_pool),
    }
    if has_llm:
        # 真·LLM 分析：分数由 O2C 因子分确定性映射，结论/建议来自对 LLM 文本的解析。
        score = o2c_ai_score(stock.get("score"))
        advice, decision, confidence = advice_from_llm(llm_text, score)
        ai_points = "\n".join(
            [
                f"板块定位：{industry}，按 O2C 日内强度和行业集中度跟踪。",
                f"筹码判断：筹码回调支撑 {format_factor(factor_value(stock, 'g_chip_pullback_support'))}，成本集中度 {format_factor(factor_value(stock, 'g_long_cost_concentration'))}。",
                f"事件催化：本轮信号主要由 {top_factor_text} 驱动，不依赖单一新闻。",
                "触发条件：开盘后价格不破 VWAP，量能维持，且收盘强度因子不反向。",
                "失效条件：跌破 VWAP 后无法收回，或行业同组股票同步转弱。",
            ]
        )
        record.update(
            {
                "ai_source_kind": "o2c_factor_llm_analysis",
                "ai_source_name": "o2c_factor_llm_analysis",
                "ai_status": "ready",
                "ai_score": score,
                "sentiment_score": score,
                "ai_advice": advice,
                "operation_advice": advice,
                "ai_decision": decision,
                "decision_type": decision,
                "ai_confidence": confidence,
                "confidence_level": confidence,
                "ai_summary": llm_text,
                "analysis_summary": llm_text,
                "ai_conclusion": llm_text,
                "ai_points": ai_points,
                "ai_trend": f"O2C 趋势侧重点在日内延续，当前由 {top_factor_text} 支撑。",
                "ai_ma": "均线不作为 O2C 主因子，只作为开盘后承接确认条件。",
                "ai_volume": f"重点观察量价背离度 {format_factor(factor_value(stock, 'g_volume_price_divergence'))} 和收盘强度比 {format_factor(factor_value(stock, 'g_close_strength_ratio'))}。",
                "ai_risk_warning": risk_text,
                "risk_warning": risk_text,
            }
        )
    else:
        # LLM 失败：绝不编造 AI 分数/买卖建议/AI 叙述。标 failed 让回填循环按 require_llm 重试；
        # 业务字段一律置空，下游 has_value/真值判断会自动「不计入已分析、如实显示 AI 未完成」。
        record.update(
            {
                "ai_source_kind": "o2c_factor_llm_failed",
                "ai_source_name": "o2c_factor_llm_failed",
                "ai_status": "failed",
                "ai_score": None,
                "sentiment_score": None,
                "ai_advice": None,
                "operation_advice": None,
                "ai_decision": None,
                "decision_type": None,
                "ai_confidence": None,
                "confidence_level": None,
                "ai_summary": None,
                "analysis_summary": None,
                "ai_conclusion": None,
                "ai_points": None,
                "ai_trend": None,
                "ai_ma": None,
                "ai_volume": None,
                "ai_risk_warning": risk_text,
                "risk_warning": risk_text,
            }
        )
    if llm_result:
        record["ai_o2c_llm_ok"] = bool(llm_result.get("ok"))
        record["ai_o2c_llm_model"] = llm_result.get("model")
        record["ai_o2c_llm_provider"] = llm_result.get("provider")
        if not llm_result.get("ok"):
            record["ai_o2c_llm_error"] = llm_result.get("reason") or llm_result.get("error")
            record["ai_o2c_llm_error_detail"] = (
                llm_result.get("error")
                or llm_result.get("stderr_tail")
                or llm_result.get("stdout_tail")
            )
    return record


def write_o2c_ai_analysis(stocks: list[dict[str, Any]], trade_date: str, factor_pool: dict[str, Any]) -> dict:
    if not stocks:
        return {"attempted": False, "reason": "no_o2c_stocks"}
    out = stage4_ai_publish.AI_ANALYSIS_DIR / f"{trade_date}.json"
    existing: list[dict[str, Any]] = []
    if out.exists():
        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                existing = [row for row in payload if isinstance(row, dict)]
        except Exception:
            existing = []
    records = []
    llm_ok = 0
    llm_failed = 0
    for stock in stocks:
        code = normalize_code(stock.get("code") or stock.get("ts_code"))
        llm_result = call_o2c_factor_llm(build_o2c_prompt(stock, factor_pool), code)
        if llm_result.get("ok"):
            llm_ok += 1
        else:
            llm_failed += 1
        records.append(build_o2c_ai_record(stock, trade_date, factor_pool, llm_result))
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing:
        code = normalize_code(row.get("code") or row.get("ts_code"))
        source = row.get("strategy_source") or "traditional"
        merged[(code, source)] = row
    for row in records:
        merged[(normalize_code(row.get("code")), "o2c_factor")] = row
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "attempted": True,
        "strategy_type": "o2c_factor",
        "trade_date": trade_date,
        "codes": [normalize_code(row.get("code")) for row in records],
        "llm_ok": llm_ok,
        "llm_failed": llm_failed,
        "written_path": str(out),
        "ok": True,
    }


def pending_o2c_codes(
    target_trade_date: str | None = None,
) -> tuple[str | None, list[str], list[dict[str, Any]], dict[str, Any]]:
    if target_trade_date:
        return str(target_trade_date), [], [], {"skipped_reason": "strategy_archived"}
    return None, [], [], {"skipped_reason": "strategy_archived"}


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


def _force_daily_analysis_llm_env(env: dict[str, str]) -> dict[str, str]:
    """Make bridge subprocess use daily_stock_analysis/.env, not inherited Gemini env."""
    env_file = DAILY_ANALYSIS_ROOT / ".env"
    env["ENV_FILE"] = str(env_file)
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
            env[key] = file_values[key]

    effective_model = str(env.get("LITELLM_MODEL") or env.get("OPENAI_MODEL") or "").strip()
    if effective_model and not effective_model.startswith("gemini/"):
        # Gateway/cron processes may carry Gemini keys globally.  Do not let those
        # keys reselect Gemini when this stock bridge is explicitly pinned to the
        # OpenClaw OpenAI-compatible route.
        for key in ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_MODEL_FALLBACK"):
            env.pop(key, None)
    return env


def sync_ai_history_to_analysis_dir(
    target_codes: list[str],
    target_trade_date: str,
    since_ts: float,
) -> dict[str, Any]:
    """把 daily_stock_analysis 本次刚生成的 AI 结果从 analysis_history DB 同步到 orchestrator 的
    ai_analysis/{date}.json（A 方案根治 selection↔AI 链路断点，2026-06）。

    背景：bridge_selection_to_analysis 的 run_analysis 只把 prebreakout/traditional 的 AI 写进
    daily_stock_analysis 的 stock_analysis.db（analysis_history.raw_result = AnalysisResult.to_dict），
    不写 orchestrator 读取的 ai_analysis/{date}.json，导致 stage4_ai_publish source_hits=0、当日因缺
    AI 字段无法发布（O2C 路径不受影响，因为 write_o2c_ai_analysis 直接写该文件）。
    这里复用「已生成、已落库」的结果（不重复调用 LLM），按 normalize_code 合并进 {date}.json，
    只取本次运行(since_ts 之后)新生成的记录、避免把本轮失败股的旧记录误同步，且不覆盖同日其它来源(O2C)条目。
    与 fisher_bridge.save_report 的合并口径一致（按 code）。
    """
    db_path = DAILY_ANALYSIS_ROOT / "data" / "stock_analysis.db"
    if not target_trade_date or not db_path.exists():
        return {"synced": 0, "reason": "no_db_or_trade_date"}
    wanted = {normalize_code(c) for c in target_codes if c}
    if not wanted:
        return {"synced": 0, "reason": "no_codes"}
    # 留 2s 余量防时钟粒度漏记录；created_at 形如 "2026-06-17 22:37:08.625783"，字符串可比。
    since_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(max(0.0, since_ts - 2.0)))
    latest_by_code: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT code, raw_result, created_at FROM analysis_history "
                "WHERE created_at >= ? ORDER BY created_at DESC",
                (since_str,),
            )
            for row in cur:
                code = normalize_code(row["code"])
                if code not in wanted or code in latest_by_code:
                    continue
                raw = row["raw_result"]
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(item, dict) and item.get("code"):
                    annotated = dict(item)
                    annotated["ai_source_date"] = target_trade_date
                    annotated["ai_analysis_date"] = target_trade_date
                    annotated["ai_evidence_time"] = str(row["created_at"] or "")
                    latest_by_code[code] = annotated
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"synced": 0, "error": f"sqlite: {exc}"}
    if not latest_by_code:
        return {"synced": 0, "reason": "no_new_history_records", "wanted": len(wanted)}
    out = stage4_ai_publish.AI_ANALYSIS_DIR / f"{target_trade_date}.json"
    merged: dict[str, dict[str, Any]] = {}
    if out.exists():
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                for it in existing:
                    code = normalize_code((it or {}).get("code"))
                    if code:
                        merged[code] = it
        except Exception:
            merged = {}
    for code, item in latest_by_code.items():
        merged[code] = item  # 同 code 以新生成结果覆盖；不同 code（含 O2C）保留
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    # 写后校验（2026-06-28 硬化）：统计本次同步里「真实摘要」的数量，区分真覆盖 vs 失败壳。
    real = sum(1 for it in latest_by_code.values() if _has_real_summary(it))
    return {
        "synced": len(latest_by_code), "real_summaries": real,
        "failed_shells": len(latest_by_code) - real,
        "path": str(out), "codes": sorted(latest_by_code),
    }


def run_o2c_ai_generator(target_trade_date: str, target_codes: list[str] | None = None) -> dict:
    """调用稳定的 generate_o2c_ai.py，写入 {date}_o2c_greenfield.json + 策略库 ai_analysis。"""
    if not O2C_AI_PATH.exists():
        return {"attempted": False, "reason": "missing_o2c_generator", "strategy_type": "o2c_factor"}
    if not target_trade_date:
        return {"attempted": False, "reason": "no_trade_date", "strategy_type": "o2c_factor"}
    python_exec = str(FISHER_BRIDGE_PYTHON if FISHER_BRIDGE_PYTHON.exists() else Path(sys.executable))
    cmd = [python_exec, str(O2C_AI_PATH), "--trade-date", str(target_trade_date)]
    started_at = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(WORKSPACE),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=O2C_AI_SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "attempted": True,
            "codes": target_codes or [],
            "strategy_type": "o2c_factor",
            "returncode": -1,
            "ok": False,
            "timed_out": True,
            "timeout_seconds": O2C_AI_SUBPROCESS_TIMEOUT,
            "stderr_tail": f"o2c generator timed out after {O2C_AI_SUBPROCESS_TIMEOUT}s: {exc}",
            "generator": "generate_o2c_ai.py",
        }
    return {
        "attempted": True,
        "codes": target_codes or [],
        "strategy_type": "o2c_factor",
        "python_exec": python_exec,
        "returncode": proc.returncode,
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-20:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-20:]),
        "ok": proc.returncode == 0,
        "generator": "generate_o2c_ai.py",
        "elapsed_seconds": round(time.time() - started_at, 1),
    }


def run_fisher_bridge(
    target_codes: list[str],
    target_trade_date: str | None = None,
    strategy_type: str = "traditional",
    o2c_stocks: list[dict[str, Any]] | None = None,
    o2c_factor_pool: dict[str, Any] | None = None,
) -> dict:
    if strategy_type == "o2c_factor":
        # 优先专用生成器（合同字段齐全）；仅当脚本缺失时回退旧内联路径。
        if O2C_AI_PATH.exists():
            return run_o2c_ai_generator(str(target_trade_date or ""), target_codes)
        return write_o2c_ai_analysis(o2c_stocks or [], str(target_trade_date or ""), o2c_factor_pool or {})
    if not target_codes:
        return {"attempted": False, "reason": "no_pending_codes"}
    # 20260708: 传统(prebreakout)策略改用直连litellm生成器, 替换易卡死的daily_stock_analysis老桥接。
    # 新生成器自行 load top-20 picks，并保留同交易日已成功的结果；重试只补缺口，
    # 避免每轮 --force 清空成功项后令覆盖率来回波动、无法收敛。
    # 结果直接写 ai_analysis/{date}.json，无需老桥接的 DB→JSON 二次同步。
    if not PREBREAKOUT_AI_PATH.exists():
        return {"attempted": False, "reason": "missing_prebreakout_generator"}
    if not target_trade_date:
        return {"attempted": False, "reason": "no_trade_date"}

    python_exec = str(FISHER_BRIDGE_PYTHON if FISHER_BRIDGE_PYTHON.exists() else Path(sys.executable))
    cmd = [python_exec, str(PREBREAKOUT_AI_PATH), "--trade-date", str(target_trade_date)]
    started_at = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(WORKSPACE),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=BRIDGE_SUBPROCESS_TIMEOUT,  # 防某只股票挂死整轮
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "attempted": True, "codes": target_codes, "strategy_type": strategy_type,
            "returncode": -1, "ok": False, "timed_out": True,
            "timeout_seconds": BRIDGE_SUBPROCESS_TIMEOUT,
            "stderr_tail": f"prebreakout generator timed out after {BRIDGE_SUBPROCESS_TIMEOUT}s: {exc}",
            "generator": "generate_prebreakout_ai.py",
        }
    return {
        "attempted": True,
        "codes": target_codes,
        "strategy_type": strategy_type,
        "python_exec": python_exec,
        "returncode": proc.returncode,
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-20:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-20:]),
        "ok": proc.returncode == 0,
        "generator": "generate_prebreakout_ai.py",
        "elapsed_seconds": round(time.time() - started_at, 1),
    }


def loop_state(
    attempt: int,
    start_ts: float,
    readiness: dict,
    status: str,
    interval_seconds: int,
    generator: dict | None = None,
) -> dict:
    elapsed_seconds = int(max(0, time.time() - start_ts))
    next_retry_at = None
    if status == "retrying":
        next_retry_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + interval_seconds))

    checks = readiness.get("checks") or {}
    ai_status_counts = checks.get("ai_status_counts") or {}
    pending = pending_codes(readiness)
    target_trade_date = str(checks.get("strategy_latest_trade_date") or "")
    o2c_trade_date, o2c_pending, _, _ = pending_o2c_codes(target_trade_date)
    return {
        "generated_at": now_str(),
        "status": status,
        "attempt": attempt,
        "elapsed_seconds": elapsed_seconds,
        "next_retry_at": next_retry_at,
        "base_ok": bool(readiness.get("base_ok")),
        "local_materialized": bool(readiness.get("local_materialized")),
        "published": bool(readiness.get("published")),
        "ai_complete": bool(readiness.get("ai_complete")),
        "publish_mode": readiness.get("publish_mode"),
        "latest_trade_date": checks.get("strategy_latest_trade_date"),
        "ai_status_counts": ai_status_counts,
        "pending_codes": pending,
        "pending_o2c_trade_date": o2c_trade_date,
        "pending_o2c_codes": o2c_pending,
        "bridge_blocked_codes": sorted(BRIDGE_BLOCKED_CODES),
        "generator": generator or {"attempted": False},
    }


def readiness_complete(
    readiness: dict,
    pending: list[str],
    pending_o2c: list[str],
) -> bool:
    """AI backfill owns local readiness; remote deployment happens later."""
    return bool(
        readiness.get("base_ok")
        and readiness.get("local_materialized")
        and readiness.get("ai_complete")
        and not pending
        and not pending_o2c
    )


def main() -> int:
    interval_seconds = max(60, env_int("OPENCLAW_AI_BACKFILL_RETRY_INTERVAL_SECONDS", 1200))
    max_wait_seconds = max(interval_seconds, env_int("OPENCLAW_AI_BACKFILL_MAX_WAIT_SECONDS", 14400))
    # 停滞护栏（20260726）：连续 N 轮两轨 pending 集合完全不变 → 重试已无法推进（典型根因=完成判定
    # 合同不匹配或生成器恒失败），提前退出并写独立状态，不再空转烧满整个 max_wait 预算。
    stall_round_limit = max(2, env_int("OPENCLAW_AI_BACKFILL_STALL_ROUNDS", 3))
    deadline = time.time() + max_wait_seconds
    start_ts = time.time()
    attempt = 0
    stall_rounds = 0
    prev_pending_snapshot: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    explicit_target = str(os.environ.get("OPENCLAW_TARGET_TRADE_DATE") or "").strip()
    if explicit_target and env_bool("OPENCLAW_AI_BACKFILL_RESET_GIVEUP", False):
        if reset_giveup(explicit_target):
            print(f"[ai-backfill] reset prior giveup state for {explicit_target}")

    while True:
        attempt += 1
        stage4_ai_publish.main()
        readiness = load_readiness()
        refresh_codes = pending_codes(readiness)
        target_trade_date = explicit_target or str((readiness.get("checks") or {}).get("strategy_latest_trade_date") or "")
        o2c_trade_date, o2c_codes, o2c_stocks, o2c_factor_pool = pending_o2c_codes(target_trade_date)

        if readiness_complete(readiness, refresh_codes, o2c_codes):
            write_json(LOOP_HEALTH_PATH, loop_state(attempt, start_ts, readiness, "completed", interval_seconds))
            print("[ai-backfill] completed")
            return 0
        if delayed_ai_terminal(readiness, refresh_codes, o2c_codes):
            write_json(
                LOOP_HEALTH_PATH,
                loop_state(attempt, start_ts, readiness, "completed_with_delayed_ai", interval_seconds),
            )
            print("[ai-backfill] completed_with_delayed_ai; late AI is excluded from coverage")
            return 0

        generator = {"attempted": False, "reason": "no_refresh_needed"}
        if refresh_codes:
            generator = run_fisher_bridge(refresh_codes, target_trade_date=target_trade_date)
            if generator.get("attempted"):
                stage4_ai_publish.main()
                readiness = load_readiness()
                # 单只硬化：本轮跑完仍 pending 的 code 计一次失败；达上限→放弃（下轮 pending_codes 自动排除）。
                still = set(pending_codes(readiness))
                if still and target_trade_date:
                    counts = _load_giveup(str(target_trade_date))
                    for c in refresh_codes:
                        if c in still:
                            counts[c] = int(counts.get(c, 0) or 0) + 1
                    _save_giveup(str(target_trade_date), counts)
                if readiness_complete(readiness, pending_codes(readiness), o2c_codes):
                    write_json(
                        LOOP_HEALTH_PATH,
                        loop_state(attempt, start_ts, readiness, "completed", interval_seconds, generator),
                    )
                    print("[ai-backfill] completed after fisher_bridge")
                    return 0

        if o2c_codes:
            generator = run_fisher_bridge(
                o2c_codes,
                target_trade_date=o2c_trade_date,
                strategy_type="o2c_factor",
                o2c_stocks=o2c_stocks,
                o2c_factor_pool=o2c_factor_pool,
            )
            if generator.get("attempted"):
                stage4_ai_publish.main()
                readiness = load_readiness()
                _, o2c_codes, _, _ = pending_o2c_codes(target_trade_date)
                if readiness_complete(readiness, pending_codes(readiness), o2c_codes):
                    write_json(
                        LOOP_HEALTH_PATH,
                        loop_state(attempt, start_ts, readiness, "completed", interval_seconds, generator),
                    )
                    print("[ai-backfill] completed after o2c_factor_analysis")
                    return 0

        # 带跳过完成（2026-06-28 硬化）：剩余 pending 全部已「放弃」、O2C 无 pending、
        # 本地结果已物化，则不再死等那 1~2 只卡死股拖到超时。
        gv = giveup_codes(str(target_trade_date or ""))
        if (
            gv
            and readiness.get("base_ok")
            and readiness.get("local_materialized")
            and not pending_codes(readiness)
            and not o2c_codes
        ):
            write_json(LOOP_HEALTH_PATH, loop_state(attempt, start_ts, readiness, "completed_with_skips", interval_seconds, generator))
            print(f"[ai-backfill] completed_with_skips, gave up on {sorted(gv)}")
            return 0

        pending_snapshot = (
            tuple(sorted(pending_codes(readiness))),
            tuple(sorted(pending_o2c_codes(target_trade_date)[1])),
        )
        if any(pending_snapshot) and pending_snapshot == prev_pending_snapshot:
            stall_rounds += 1
        else:
            stall_rounds = 0
        prev_pending_snapshot = pending_snapshot
        if stall_rounds >= stall_round_limit:
            write_json(LOOP_HEALTH_PATH, loop_state(attempt, start_ts, readiness, "stalled_no_progress", interval_seconds, generator))
            print(
                "[ai-backfill] stopped with status=stalled_no_progress"
                f" rounds={stall_rounds} pending={pending_snapshot[0]} pending_o2c={pending_snapshot[1]}"
            )
            return 1

        if time.time() >= deadline:
            status = "timed_out"
            if not readiness.get("base_ok"):
                status = "base_not_ready"
            elif not readiness.get("local_materialized"):
                status = "local_materialization_not_ready"
            write_json(LOOP_HEALTH_PATH, loop_state(attempt, start_ts, readiness, status, interval_seconds, generator))
            print(f"[ai-backfill] stopped with status={status}")
            return 1

        write_json(LOOP_HEALTH_PATH, loop_state(attempt, start_ts, readiness, "retrying", interval_seconds, generator))
        print(
            "[ai-backfill] retrying"
            f" attempt={attempt}"
            f" publish_mode={readiness.get('publish_mode')}"
            f" ai_complete={readiness.get('ai_complete')}"
        )
        time.sleep(interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
