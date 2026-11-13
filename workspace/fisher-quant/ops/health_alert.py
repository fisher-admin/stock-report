#!/usr/bin/env python3
"""Active alerting for the stock system (overhaul 2026-06-12).

Health files used to be write-only — failures were discovered passively.
This script aggregates the contracts and PUSHES a Discord message when
something needs attention. Quiet (exit 0, no message) when all green
unless --always is passed.

Checks:
1. ai_publish_readiness: publish_mode != full / pending > 0
2. run_manifest: trade_date stale (> max allowed lag vs today), validation failed
3. feature coverage (selection_meta in working strategy_backtests.json) < 80%
4. industry concentration: prebreakout top20 single industry > cap
5. governance evaluation flags
6. verify_history_v2 freshness (no new file in 3 trade days)

Webhook resolution: env DISCORD_WEBHOOK_URL, else .secrets/discord_webhook.
Without a webhook it prints the report to stdout (cron logs still catch it).

Suggested cron: daily 20:45  ~/.openclaw/venv/bin/python ~/.openclaw/workspace/fisher-quant/ops/health_alert.py
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

WS = Path(os.environ.get("OPENCLAW_WORKSPACE_DIR", str(Path.home() / ".openclaw/workspace")))
HEALTH = WS / "stock_data/03-working/health"
WORKING_REPO = WS / "stock_data/03-working/stock-report-repo"


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def collect_issues() -> list[str]:
    issues: list[str] = []

    ai = load(HEALTH / "ai_publish_readiness.json")
    if ai:
        mode = str(ai.get("publish_mode", ""))
        pending = ai.get("pending")
        if mode and mode != "full":
            issues.append(f"AI发布降级: publish_mode={mode}" + (f", pending={pending}" if pending else ""))

    manifest = load(WS / "stock-report/data/latest/run_manifest.json")
    if manifest:
        if manifest.get("validation_ok") is False:
            issues.append("validation_ok=false（发布前校验失败）")
        trade_date = str(manifest.get("trade_date", ""))
        if trade_date:
            try:
                lag = (datetime.now() - datetime.strptime(trade_date, "%Y%m%d")).days
                if lag > 4:
                    issues.append(f"数据陈旧: 最新交易日 {trade_date}（滞后 {lag} 天）")
            except ValueError:
                pass

    doc = load(WORKING_REPO / "data/strategy_backtests.json")
    for strategy in doc.get("strategies", []):
        if strategy.get("id") != "prebreakout_v41":
            continue
        meta = strategy.get("selection_meta") or {}
        coverage = (meta.get("feature_coverage") or {}).get("coverage_pct")
        if coverage is not None and coverage < 80:
            issues.append(f"历史特征覆盖率仅 {coverage}%（应≥80%，检查 backtest_cache 缺口）")
        top20 = strategy.get("top20") or []
        industries = {}
        for item in top20:
            industries[item.get("industry") or "未知"] = industries.get(item.get("industry") or "未知", 0) + 1
        if industries:
            top_industry, count = max(industries.items(), key=lambda kv: kv[1])
            if count > 6 and top_industry != "未知":
                issues.append(f"行业集中度异常: {top_industry} 占 {count}/20（上限机制可能失效）")

    gov = load(WS / "stock_data/03-working/strategy_research/governance_evaluation_latest.json")
    # The v2 governance file is an explicit tombstone: it says the old
    # promotion evaluator is deprecated and points to the authoritative
    # dual-track evaluation. Its flags are documentation, not live alerts.
    if not gov.get("deprecated"):
        for sid, verdict in (gov.get("verdicts") or {}).items():
            for flag in verdict.get("flags") or []:
                issues.append(f"[{sid}] {flag}")

    # 收盘一致性(20260703两起"实时冒充收盘"事故的主动告警):定调/指数不得与涨跌面自相矛盾。
    ctx = load(WS / "stock-report/data/latest/market_context.json")
    mstate = load(WS / "stock-report/data/latest/market_state.json")
    if ctx and mstate:
        ca = ctx.get("close_actuals") or {}
        breadth = ca.get("breadth")
        if not isinstance(breadth, (int, float)):
            breadth = (mstate.get("market_summary") or {}).get("positive_sector_ratio")
        gate = str(ctx.get("gate_signal") or "")
        if isinstance(breadth, (int, float)):
            if gate == "risk_off" and breadth > 0.55:
                issues.append(f"收盘定调矛盾: 判risk_off空仓但当日涨跌面{breadth:.0%}(普涨)——疑盘前情绪冒充收盘")
            ss = mstate.get("session_snapshot") or {}
            idx = {k: (ss.get(k) or {}).get("change_pct") for k in ("shanghai", "shenzhen", "chinext")}
            idx = {k: round(v, 2) for k, v in idx.items() if isinstance(v, (int, float))}
            if breadth > 0.55 and any(v < -0.3 for v in idx.values()):
                issues.append(f"指数与涨跌面矛盾: 涨跌面{breadth:.0%}普涨却有指数收跌 {idx}——疑指数取盘中/陈旧值")
            if breadth < 0.45 and any(v > 0.3 for v in idx.values()):
                issues.append(f"指数与涨跌面矛盾: 涨跌面{breadth:.0%}普跌却有指数收涨 {idx}——疑指数取盘中/陈旧值")
            spot = [k for k in ("shanghai", "shenzhen", "chinext") if str((ss.get(k) or {}).get("source_kind") or "") == "intraday_spot"]
            if spot:
                issues.append(f"指数为盘中现价而非结算收盘: {spot}——收盘应走tushare exact_close")

    verify_dir = WS / "stock_data/03-working/verify_history_v2"
    if verify_dir.exists():
        files = sorted(verify_dir.glob("2*.json"))
        if files:
            newest = files[-1].stem
            try:
                lag = (datetime.now() - datetime.strptime(newest, "%Y%m%d")).days
                if lag > 5:
                    issues.append(f"T+1验证闭环停更: 最新 {newest}（{lag} 天前）")
            except ValueError:
                pass
    else:
        issues.append("verify_history_v2 目录缺失（验证闭环未运行）")

    # S3 is archived historical-only and no longer emits daily signals.
    # Keep the old check available only for an explicit forensic replay.
    if os.environ.get("STOCK_MONITOR_ARCHIVED_S3") == "1":
        s3_status = load(HEALTH / "s3_tracking_status.json")
        if s3_status:
            if s3_status.get("generation_ok") is False:
                err = str(s3_status.get("generation_err", ""))[:80]
                issues.append(f"S3新信号生成失败(结算已解耦保全): {err}——分钟拉取中断,重跑续拉")
            wl_at = s3_status.get("watchlist_generated_at")
            if wl_at:
                try:
                    wl_day = datetime.strptime(str(wl_at)[:10], "%Y-%m-%d")
                    lag = (datetime.now() - wl_day).days
                    if lag > 2:
                        issues.append(f"S3观察看板停更: 最新 {wl_at}（{lag} 天前）")
                except ValueError:
                    pass

    return issues


def send_discord(text: str) -> bool:
    url = (os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        secret = WS / ".secrets/discord_webhook"
        if secret.exists():
            url = secret.read_text(encoding="utf-8").strip()
    if not url:
        return False
    body = json.dumps({"content": text[:1900]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return 200 <= resp.status < 300


def alerts_disabled(
    *,
    disable_flag: Path | None = None,
    runtime_owner: str | None = None,
) -> bool:
    owner = str(runtime_owner or os.environ.get("STOCK_HEALTH_RUNTIME") or "openclaw").strip().lower()
    flag = disable_flag or (Path.home() / ".openclaw" / "control" / "stock_alerts.disabled")
    return owner != "hermes" and flag.exists()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--always", action="store_true", help="即使全绿也发送日报")
    args = parser.parse_args()

    # OpenClaw-side stock alerts kill-switch (Hermes owns runtime now).
    if alerts_disabled():
        print("[health_alert disabled] ~/.openclaw/control/stock_alerts.disabled present")
        return

    issues = collect_issues()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if issues:
        text = f"🚨 **股票系统健康告警** ({stamp})\n" + "\n".join(f"- {i}" for i in issues)
    else:
        text = f"✅ 股票系统健康检查全绿 ({stamp})"

    if issues or args.always:
        print(text)
        if str(os.environ.get("STOCK_HEALTH_RUNTIME") or "").strip().lower() == "hermes":
            print("[delivery] delegated-to-hermes")
        else:
            delivered = send_discord(text)
            print(f"[delivery] discord={'ok' if delivered else 'not-configured/failed'}")
    else:
        print(text)


if __name__ == "__main__":
    main()
