#!/usr/bin/env python3
"""Export the public DOUBLE-PASS monitor contract: data/latest/double_pass_monitor.json.

Source: factor_factory/outputs/forward_shadow/tier_monitor.json (written by the 23:30 forward-shadow job,
so the public file lags one trading day, like the arena ledger).  Research display only: no execution
authority, no effect on the v4.3 list.  Missing/invalid input writes an explicit "unavailable" contract.
When maintenance mode is on, per-stock member lists are dropped (aggregates stay).

Exit 0 on missing input (non-blocking for the publisher); 2 on a write error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

WS = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", Path.home() / ".openclaw" / "workspace"))
DEFAULT_REPO = Path(os.environ.get("OPENCLAW_PUBLISHED_REPO", WS / "stock-report"))
SRC = WS / "factor_factory" / "outputs" / "forward_shadow" / "tier_monitor.json"
MAINT = WS / "config" / "stock_maintenance_mode.json"
OUT_REL = "data/latest/double_pass_monitor.json"
CONTRACT = "double_pass_monitor_v1"
HONESTY = ("前瞻监控 · 仅研究观察 · 非买入建议。双料王牌 = 生产 v4.3 当日 Top-20 中同时通过绿地因子 comp_rank_ew6 的股票；"
           "只统计信号在次日开盘前入账的样本，不回补；样本量达预注册门槛前不构成结论。")
KEEP_KEYS = ("liquidity", "impact_stress", "status", "message", "spec_hash", "as_of", "first_signal", "last_signal", "ledger_days", "official_days",
             "late_days_excluded", "matured", "pending_days", "stats", "spreads", "verdict", "curve", "cost",
             "double_empty_days", "groups")


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def maintenance_on() -> bool:
    cfg = load(MAINT)
    return bool(cfg and cfg.get("enabled"))


def build(src: dict | None, now: str, hide_members: bool = False) -> dict:
    base = {"contract_version": CONTRACT, "generated_at": now, "title": "双料王牌 · 前瞻周期监控",
            "honesty_banner": HONESTY, "research_only": True, "execution_authority": "observe_only_no_auto_order"}
    if not src or src.get("contract_version") != "tier_monitor_v1":
        return {**base, "status": "unavailable", "reason": "tier_monitor.json 缺失或版本不符"}
    doc = {**base, **{k: src[k] for k in KEEP_KEYS if k in src}}
    spec = src.get("spec") or {}
    doc["spec"] = {k: spec.get(k) for k in ("definition", "cohort", "execution", "cost", "benchmark", "forward_only",
                                            "evaluation") if k in spec}
    days = []
    for d in src.get("days") or []:
        row = {k: d.get(k) for k in ("date", "entry", "counts", "t1", "t5", "tradable_t5")}
        row["double_members"] = [] if hide_members else d.get("double_members") or []
        days.append(row)
    doc["days"] = days
    doc["members_hidden"] = hide_members
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    ap.add_argument("--src", type=Path, default=SRC)
    args = ap.parse_args(argv)
    doc = build(load(args.src), datetime.now().astimezone().isoformat(timespec="seconds"), maintenance_on())
    out = args.repo / OUT_REL
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(out)
    except OSError as exc:
        print(f"FATAL: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    print(f"double_pass_monitor: status={doc['status']} as_of={doc.get('as_of')} matured={doc.get('matured')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
