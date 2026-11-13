#!/usr/bin/env python3
"""Deterministic AI risk / action-cap rules (plan v1.4 Epic D).

AI may explain; only these rules may hard-cap actions. No model-estimated prices.
"""
from __future__ import annotations

from typing import Any


ACTION_RANK = {"回避": 0, "观察": 1, "条件试探": 2, "主攻": 3}
FORBIDDEN_MODEL_PRICE_KEYS = (
    "trigger_price",
    "stop_loss_price",
    "support_price",
    "resistance_price",
    "invalidation_price",
    "buy_price",
    "stop_loss",
    "support",
    "resistance",
)


def _norm_advice(value: Any) -> str:
    text = str(value or "").strip()
    aliases = {
        "观望": "观察",
        "watch": "观察",
        "observe": "观察",
        "avoid": "回避",
        "buy": "主攻",
        "main": "主攻",
        "conditional": "条件试探",
        "试探": "条件试探",
    }
    if text in ACTION_RANK:
        return text
    return aliases.get(text.lower(), text if text in ACTION_RANK else "观察")


def min_action(a: str, b: str) -> str:
    ra = ACTION_RANK.get(_norm_advice(a), 1)
    rb = ACTION_RANK.get(_norm_advice(b), 1)
    rank = min(ra, rb)
    for name, value in ACTION_RANK.items():
        if value == rank:
            return name
    return "观察"


def strip_model_estimate_prices(item: dict[str, Any]) -> dict[str, Any]:
    """Remove model-estimated price fields; keep rule_computed only."""
    cleaned = dict(item)
    for key in list(cleaned.keys()):
        if key in FORBIDDEN_MODEL_PRICE_KEYS:
            val = cleaned.get(key)
            if isinstance(val, dict):
                source = str(val.get("source") or "")
                if source == "model_estimate" or source == "":
                    cleaned.pop(key, None)
                elif source != "rule_computed":
                    cleaned.pop(key, None)
            else:
                # Bare numeric/string prices from AI are treated as model estimates.
                cleaned.pop(key, None)
        if key.endswith("_source") and str(cleaned.get(key) or "") == "model_estimate":
            base = key[: -len("_source")]
            cleaned.pop(key, None)
            cleaned.pop(base, None)
        if key == "display_score":
            cleaned.pop(key, None)
    return cleaned


def compute_ai_depth(item: dict[str, Any]) -> str:
    """shallow | standard | deep based on explanation richness (not ranking)."""
    summary = str(item.get("ai_summary") or item.get("ai_conclusion") or "").strip()
    points = item.get("ai_points") or item.get("points") or []
    risks = item.get("ai_risks") or item.get("risk_tags") or item.get("rule_risk_tags") or []
    if not summary:
        return "shallow"
    point_n = len(points) if isinstance(points, list) else 0
    risk_n = len(risks) if isinstance(risks, list) else (1 if risks else 0)
    has_conflict = bool(item.get("ai_conflicts") or item.get("data_completeness_note"))
    if len(summary) >= 80 and point_n >= 3 and (risk_n >= 1 or has_conflict):
        return "deep"
    if len(summary) >= 20 and point_n >= 1:
        return "standard"
    return "shallow"


def apply_risk_rules(
    item: dict[str, Any],
    *,
    system_observe_only: bool = True,
    used_proxy: bool = False,
    chip_mode: str = "unknown",
) -> dict[str, Any]:
    """Return rule fields to merge into a candidate/AI row."""
    tags: list[str] = []
    action_cap = "主攻"
    reasons: list[str] = []

    if system_observe_only:
        action_cap = min_action(action_cap, "观察")
        tags.append("system_observe_only")
        reasons.append("系统处于只观察，动作上限=观察")

    if used_proxy or bool(item.get("used_proxy")):
        action_cap = min_action(action_cap, "回避")
        tags.append("proxy_data_forbidden")
        reasons.append("代理数据禁止进入可执行叙事")

    if chip_mode in {"disabled", "proxy", "partial"} and any(
        str(k).startswith("chip") for k in (item.get("factor_scores") or {})
    ):
        action_cap = min_action(action_cap, "观察")
        tags.append("chip_degraded")
        reasons.append("筹码路径降级，禁止条件试探叙事")

    # Liquidity heuristic from factor_scores if present (0-100 scale).
    scores = item.get("factor_scores") or {}
    for key in ("liquidity", "liquidity_risk_control"):
        if key in scores:
            try:
                liq = float(scores[key])
            except (TypeError, ValueError):
                liq = None
            if liq is not None and liq < 20:
                action_cap = min_action(action_cap, "观察")
                tags.append("low_liquidity")
                reasons.append(f"{key}={liq:.1f}<20")

    ai_advice = _norm_advice(item.get("ai_advice") or item.get("final_action") or "观察")
    capped = min_action(ai_advice, action_cap)

    out = strip_model_estimate_prices(item)
    out["rule_action_cap"] = action_cap
    out["rule_risk_tags"] = tags
    out["rule_risk_reasons"] = reasons
    out["ai_advice"] = capped
    if "final_action" in out:
        out["final_action"] = min_action(str(out.get("final_action") or "观察"), action_cap)
    out["rank_change"] = 0
    out["ai_depth"] = compute_ai_depth(out)
    out["ai_may_change_rank"] = False
    out["display_score"] = None
    return out


def all_observe_is_allowed(*, system_observe_only: bool) -> bool:
    """Plan: 20/20 观察 is valid when system is observe_only."""
    return bool(system_observe_only)
