#!/usr/bin/env python3
"""Export the public daily-rolling COHORT monitor contract: data/latest/cohort_monitor.json.

Source: factor_factory/outputs/forward_shadow/cohort_monitor.json (forward_shadow_cohort_v1, spec hash 70f0e200,
written by the 23:30 forward-shadow job, so the public file lags one trading day).  Research display only: no
execution authority, no effect on the v4.3 list.  Aggregates only -- the source carries no member lists (those stay
in the local cohort_ledger.jsonl) and this exporter whitelists keys so none can leak in later; when maintenance mode
is on the contract is additionally marked members_hidden.  Missing/invalid input writes an explicit "unavailable".

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
SRC = WS / "factor_factory" / "outputs" / "forward_shadow" / "cohort_monitor.json"
SPEC = WS / "factor_factory" / "contracts" / "forward_shadow_cohort_v1.json"
MAINT = WS / "config" / "stock_maintenance_mode.json"
OUT_REL = "data/latest/cohort_monitor.json"
CONTRACT = "cohort_monitor_v1"
SRC_CONTRACT = "forward_shadow_cohort_v1"
BOOKS = ("cohort_top20", "cohort_top100", "cohort_avoid70", "bench_ew")
HONESTY = ("前瞻监控 · 仅研究观察 · 非买入建议。每个官方信号日 D 按绿地因子 comp_rank_ew6 新开一组，D 之后第 1 个交易日（E1）"
           "开盘入场、持有至 E5 收盘，扣 0.3% 往返成本；只统计信号在 E1 开盘前入账的组，不回补。"
           "已结算组数达预注册 120 组之前不下结论（60 组时出一次只报告不判定的中期快照）。")
KEEP_KEYS = ("status", "message", "spec_hash", "as_of", "first_signal", "last_signal", "ledger_cohorts",
             "official_cohorts", "late_excluded", "matured", "stats", "verdict", "regime_h5", "rolling20_h5",
             "sleeve_curve", "blocked", "impact_stress")
SPEC_KEYS = ("purpose", "execution", "forward_only", "sleeve_portfolio_reported_not_gated")


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def maintenance_on() -> bool:
    cfg = load(MAINT)
    return bool(cfg and cfg.get("enabled"))


def _num(v):
    return round(float(v), 6) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _date(v):
    return v if isinstance(v, str) and len(v) == 8 and v.isdigit() else None


def build(src: dict | None, now: str, maintenance: bool = False, spec: dict | None = None) -> dict:
    base = {"contract_version": CONTRACT, "generated_at": now, "title": "每日滚动分组 · 前瞻监控",
            "honesty_banner": HONESTY, "research_only": True, "execution_authority": "observe_only_no_auto_order",
            "members_hidden": True, "maintenance": maintenance,
            "thresholds": {"verdict_cohorts": 120, "interim_cohorts": 60, "nw_t": 2.0, "hold_days": 5, "sleeves": 5}}
    if not src or src.get("contract_version") != SRC_CONTRACT:
        return {**base, "status": "unavailable", "reason": "cohort_monitor.json 缺失或版本不符"}
    doc = {**base, **{k: src[k] for k in KEEP_KEYS if k in src}}
    ev = (spec or {}).get("evaluation") or {}
    doc["spec"] = {**{k: spec[k] for k in SPEC_KEYS if spec and k in spec},
                   **({"primary": ev["primary"], "when": ev.get("when"), "success": ev.get("success"),
                       "caveat": ev.get("caveat")} if ev.get("primary") else {})}
    # Windows: dates + per-book cohort means only (each book value is a float, never a list of names).
    doc["windows"] = [{"signal_date": _date(w.get("signal_date")), "entry": _date(w.get("entry")),
                       "exit": _date(w.get("exit")),
                       "window": f"{w.get('signal_date')} → {w.get('entry')}～{w.get('exit')}",
                       **{b: _num(w.get(b)) for b in BOOKS}}
                      for w in src.get("windows") or [] if isinstance(w, dict) and _date(w.get("signal_date"))]
    doc["pending"] = [{"signal_date": _date(p.get("signal_date")), "window_start": _date(p.get("window_start"))}
                      for p in src.get("pending") or [] if isinstance(p, dict) and _date(p.get("signal_date"))]
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    ap.add_argument("--src", type=Path, default=SRC)
    args = ap.parse_args(argv)
    doc = build(load(args.src), datetime.now().astimezone().isoformat(timespec="seconds"), maintenance_on(), load(SPEC))
    out = args.repo / OUT_REL
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(out)
    except OSError as exc:
        print(f"FATAL: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    print(f"cohort_monitor: status={doc['status']} as_of={doc.get('as_of')} matured={doc.get('matured')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
