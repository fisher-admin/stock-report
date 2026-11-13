#!/usr/bin/env python3
"""启动前夕加工厂 · 正式体检（替代 runbook 内嵌易误判脚本）

exit:
  0 = 通过
  2 = 警告（如弱市空仓、真生产对照 partial）
  1 = 失败
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

WS = Path.home() / ".openclaw" / "workspace"
HEALTH = WS / "stock_data" / "03-working" / "health"
CACHE = WS / "skills" / "stock-analyzer" / "backtest_cache"
if not CACHE.exists():
    # 兼容 shadow 模块实际 cache 位置
    try:
        sys.path.insert(0, str(WS / "skills" / "stock-system-orchestrator" / "scripts"))
        import prebreakout_shadow_research as shadow

        CACHE = shadow.CACHE
    except Exception:
        pass


def load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_parse_error": str(exc)}


def list_trade_dates() -> list[str]:
    if not CACHE.exists():
        return []
    return sorted(
        {
            p.stem.replace("daily_", "")
            for p in CACHE.glob("daily_*.parquet")
            if p.stem.replace("daily_", "").isdigit()
        }
    )


def expected_trade_date(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    dates = list_trade_dates()
    if not dates:
        return None
    # 最新有 stk_factor 的日
    for d in reversed(dates):
        if (CACHE / f"stk_factor_{d}.parquet").exists():
            return d
    return dates[-1]


def check_freshness(generated_at: str | None, trade_date: str | None) -> tuple[bool, str]:
    if not generated_at:
        return False, "missing generated_at/finished_at"
    try:
        # "YYYY-MM-DD HH:MM:SS"
        dt = datetime.strptime(generated_at[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False, f"bad timestamp {generated_at!r}"
    age = datetime.now() - dt
    if age > timedelta(hours=36):
        return False, f"stale age={age}"
    return True, f"age={age}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade-date", default="")
    ap.add_argument("--require-daily", action="store_true", help="要求日淘也必须今日 ok")
    args = ap.parse_args()

    exp = expected_trade_date(args.trade_date or None)
    fails: list[str] = []
    warns: list[str] = []
    infos: list[str] = []

    daily = load(HEALTH / "prebreakout_daily_pan_status.json")
    factory = load(HEALTH / "prebreakout_factory_status.json")
    watch = load(WS / "stock-report" / "data" / "latest" / "prebreakout_shadow_watch.json")
    report = load(
        WS
        / "stock_data"
        / "03-working"
        / "strategy_research"
        / "prebreakout_shadow"
        / "factory"
        / "search"
        / "search_report_daily_latest.json"
    )

    print("=" * 50, "期望 trade_date=", exp or "UNKNOWN")

    def chk(name: str, doc: dict | None, need_ok: bool = True) -> None:
        print("=" * 50, name)
        if doc is None:
            fails.append(f"{name}: file missing")
            print("  ❌ missing")
            return
        if doc.get("_parse_error"):
            fails.append(f"{name}: parse error {doc['_parse_error']}")
            print("  ❌ parse error", doc["_parse_error"])
            return
        ok = doc.get("ok")
        td = doc.get("trade_date") or doc.get("latest_signal_date")
        ga = doc.get("finished_at") or doc.get("generated_at")
        ch = (
            doc.get("observation_champion_id")
            or doc.get("observation_champion")
            or doc.get("champion")
            or doc.get("champion_experiment_id")
        )
        if isinstance(ch, dict):
            ch = ch.get("id")
        print(f"  ok={ok} trade_date={td} at={ga}")
        print(f"  champion={ch}")
        if need_ok and ok is not True:
            fails.append(f"{name}: ok!={True!r}")
            print("  ❌ ok != true")
        if exp and td and str(td) != str(exp):
            fails.append(f"{name}: trade_date {td} != expected {exp}")
            print(f"  ❌ trade_date mismatch (expected {exp})")
        if not td:
            fails.append(f"{name}: missing trade_date")
            print("  ❌ missing trade_date")
        fresh_ok, fresh_msg = check_freshness(ga, td)
        print(f"  freshness: {fresh_msg}")
        if not fresh_ok and need_ok:
            # 非交易日可能 stale——仅 warn
            if exp and td == exp:
                warns.append(f"{name}: {fresh_msg}")
            else:
                fails.append(f"{name}: {fresh_msg}")

    # 工厂必查；日淘默认必查（可用 --no 关闭，这里默认 require）
    chk("日淘", daily, need_ok=True)
    if daily and daily.get("ok") is True:
        files = daily.get("experiment_files") or []
        if len(files) < 1:
            warns.append("日淘 experiment_files 为空")
        if not daily.get("holdout_sealed") and report and not report.get("holdout_sealed"):
            warns.append("日淘未标记 holdout_sealed（旧产物？）")

    chk("工厂", factory, need_ok=True)
    if factory and factory.get("ok") is True:
        if not factory.get("production_config_fingerprint"):
            warns.append("工厂缺少 production_config_fingerprint")
        if factory.get("state") not in (None, "completed"):
            infos.append(f"factory state={factory.get('state')}")

    print("=" * 50, "观察页")
    if watch is None:
        fails.append("观察页 missing")
        print("  ❌ missing")
    elif watch.get("_parse_error"):
        fails.append("观察页 parse error")
        print("  ❌", watch["_parse_error"])
    else:
        td = watch.get("trade_date") or watch.get("latest_signal_date")
        ga = watch.get("generated_at")
        print(f"  trade_date={td} generated_at={ga}")
        print(f"  production_frozen={watch.get('production_frozen')}")
        print(f"  real_control={watch.get('real_production_control_status')}")
        print(f"  market_note={watch.get('market_note')}")
        picks = (watch.get("champion") or {}).get("today_picks") or watch.get("latest_picks") or []
        print(f"  today_picks_n={len(picks)}")
        if exp and td and str(td) != str(exp):
            fails.append(f"观察页 trade_date {td} != {exp}")
        if watch.get("production_frozen") is not True:
            fails.append("production_frozen != true")
        if not picks:
            note = str(watch.get("market_note") or "")
            if "risk_off" in note or "cap=0" in note:
                warns.append("空仓：弱市门控（正常）")
                print("  ⚠️ empty book with risk_off (OK as warning)")
            else:
                warns.append("空仓且无弱市说明")
        if watch.get("real_production_control_status") not in ("available",):
            warns.append(f"真生产对照 {watch.get('real_production_control_status')}")
        if not watch.get("research_only", True):
            warns.append("research_only 未标记")

    print("=" * 50, "日淘报告")
    if report is None:
        warns.append("日淘报告缺失")
        print("  ⚠️ missing")
    elif report.get("_parse_error"):
        fails.append("日淘报告 parse error")
    else:
        print(f"  mode={report.get('mode')} holdout_sealed={report.get('holdout_sealed')}")
        print(f"  observation_champion_id={report.get('observation_champion_id')}")
        print(f"  trade_date={report.get('trade_date')}")
        if exp and report.get("trade_date") and str(report.get("trade_date")) != str(exp):
            fails.append("日淘报告 trade_date mismatch")

    # 日期一致性
    tds = []
    for doc in (daily, factory, watch):
        if doc and not doc.get("_parse_error"):
            t = doc.get("trade_date") or doc.get("latest_signal_date")
            if t:
                tds.append(str(t))
    if tds and len(set(tds)) > 1:
        fails.append(f"trade_date 不一致: {set(tds)}")

    print("=" * 50, "汇总")
    for w in warns:
        print("  WARN:", w)
    for f in fails:
        print("  FAIL:", f)
    if fails:
        print("RESULT: FAIL")
        return 1
    if warns:
        print("RESULT: WARN")
        return 2
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
