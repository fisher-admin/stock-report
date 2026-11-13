#!/usr/bin/env python3
"""Unified Chinese message formatting for stock pipeline user-facing outputs."""

from __future__ import annotations

from typing import Any


def format_morning_brief(brief: dict[str, Any], verdict: dict[str, Any]) -> str:
    trade_date = str(brief.get("trade_date") or "")
    regime = str(brief.get("regime") or "未知")
    risk_score = brief.get("risk_score", "—")
    focus_sectors = "、".join(brief.get("focus_sectors", [])[:3]) or "—"
    final_label = str(
        ((verdict.get("final_action") or {}).get("label"))
        or ((verdict.get("final_action") or {}).get("action"))
        or "只观察"
    )
    authority = str(
        ((verdict.get("pipeline_status") or {}).get("execution_authority"))
        or "observe_only_no_auto_order"
    )
    summary = str(brief.get("ai_summary") or "").strip()
    if len(summary) > 80:
        summary = summary[:80] + "..."

    lines = [
        f"🌅 【A股市场晨判】 交易日: {trade_date}",
        f"📊 市场状态: {regime} | 风险分: {risk_score}",
        f"🔥 重点关注行业: {focus_sectors}",
        f"⚖️ 系统裁决: {final_label} ({authority})",
    ]
    if summary:
        lines.append(f"📝 晨判摘要: {summary}")
    lines.append("🚀 晨间数据与行业热力已发布至 GitHub Pages")
    return "\n".join(lines)


def format_midday_analysis(analysis: dict[str, Any], verdict: dict[str, Any]) -> str:
    trade_date = str(analysis.get("trade_date") or "")
    market_view = analysis.get("market_view_midday") or {}
    regime = str(market_view.get("regime") or "未知")
    risk_score = market_view.get("risk_score", "—")
    final_label = str(
        ((verdict.get("final_action") or {}).get("label"))
        or ((verdict.get("final_action") or {}).get("action"))
        or "只观察"
    )
    authority = str(
        ((verdict.get("pipeline_status") or {}).get("execution_authority"))
        or "observe_only_no_auto_order"
    )
    summary = str(analysis.get("ai_summary") or "").strip()
    if len(summary) > 80:
        summary = summary[:80] + "..."

    lines = [
        f"☀️ 【A股午盘分析】 交易日: {trade_date}",
        f"📊 午盘状态: {regime} | 风险分: {risk_score}",
        f"⚖️ 系统裁决: {final_label} ({authority})",
    ]
    if summary:
        lines.append(f"📝 午盘摘要: {summary}")
    lines.append("🚀 午盘数据已同步更新至 GitHub Pages")
    return "\n".join(lines)


def format_watchdog_message(report: dict[str, Any]) -> str:
    if report.get("healthy"):
        return ""
    expected = report.get("expected_trading_day") or "未知"
    lines = [
        "⚠️ 【选股数据可能停更】",
        f"今天应有交易日：{expected}",
    ]
    for finding in report.get("stale_findings") or []:
        lines.append(f"- {finding}")
    last = report.get("last_run") or {}
    if not last.get("ok"):
        if last.get("selection_ok") is False:
            lines.append("- 最近一次选股主线没有跑完，当天结果还不能用。")
        elif last.get("publish_ok") is False:
            lines.append("- 最近一次选股已经算出，但当天报告没有发出去。")
        else:
            lines.append("- 最近一次管线执行异常，未形成有效闭环。")
    return "\n".join(lines)


def format_evening_summary(
    trade_date: str,
    expected_seats: int,
    ready_count: int,
    failed_count: int,
    watch_count: int,
    avoid_count: int,
    buy_count: int,
    unknown_count: int,
    avg_score: float,
    model_summary: str,
    top_candidates: list[dict[str, Any]],
    commit_sha: str = "",
) -> str:
    lines = [
        "=" * 50,
        f"📊 【A股选股 AI深度分析报告】 交易日: {trade_date}",
        f"🎯 生产席位: {ready_count + failed_count}/{expected_seats} | 有效就绪: {ready_count} | 失败/未就绪: {failed_count}",
    ]
    dist_str = f"观察/持仓 {watch_count} | 回避 {avoid_count} | 主攻/买入 {buy_count}"
    if unknown_count:
        dist_str += f" | 无法识别建议标签 {unknown_count}"
    lines.extend([
        f"📈 观点分布: {dist_str}",
        f"⭐ AI 综合均分: {avg_score} 分 (模型: {model_summary})",
        "🛡️ 执行权限: 观察性质 (observe_only_no_auto_order) — 严禁自动下单",
        "=" * 50,
    ])
    if top_candidates:
        lines.append("📌 重点观察标的 (Top 3):")
        for i, item in enumerate(top_candidates[:3], 1):
            name = item.get("name", "")
            code = item.get("code", item.get("ts_code", ""))
            sector = item.get("sector_name", "")
            advice = item.get("ai_advice", "观察")
            score = item.get("ai_score", "--")
            summary = item.get("ai_summary", "")
            if len(summary) > 60:
                summary = summary[:60] + "..."
            lines.append(f"  {i}. {name}({code}) [{sector}] | 建议:{advice} | 评分:{score}")
            if summary:
                lines.append(f"     └ {summary}")
    if commit_sha:
        lines.append(f"\n✅ GitHub Pages 发布已由 19:30 主链确认 (commit: {commit_sha[:7]})")
    return "\n".join(lines)
