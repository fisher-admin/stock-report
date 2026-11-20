#!/usr/bin/env python3
"""Export the #/today contract after the v4.3 retirement (A2, 2026-11-20): EW6 forward-shadow holdings.

Reads only the forward_shadow_v1 ledger (frozen contract, research-only, paper):
  books.jsonl     latest official record (signal date) and the latest rebalance (the book currently held)
  scores_<D>      today's EW6 composite rank of each held name
  PIT universe_D  names and SW industry
and writes <repo>/data/latest/ew6_today_state.json.  No returns are published: the v1 contract evaluates only after
50 matured periods, and before that the honest public content is "what the paper book holds".

  export_ew6_today.py [--repo PATH] [--check]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("Asia/Shanghai")
H = 5   # forward_shadow_v1 cadence: rebalance on every 5th official session from the anchor
SCHEMA = "ew6_today_v1"
LABEL = "前向验证纸面记录：按冻结合同每日记账，不下单、非投资建议；尚未证明超额收益（合同在满 50 个成熟期后才评估）"


def workspace() -> Path:
    return Path(os.environ.get("STOCK_SYSTEM_WORKSPACE") or os.environ.get("OPENCLAW_WORKSPACE_DIR")
                or Path(__file__).resolve().parents[3])


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def build(ws: Path | None = None) -> dict:
    w = ws or workspace()
    fs = w / "factor_factory/data/forward_shadow"
    books = [b for b in _jsonl(fs / "books.jsonl") if b.get("official")]
    if not books:
        return {"schema": SCHEMA, "status": "unavailable", "reason": "forward_shadow_v1 has no official record"}
    last = books[-1]
    reb = next(b for b in reversed(books) if b.get("is_rebalance"))
    d, held = str(last["signal_date"]), list(reb["buffered_top100"])
    cal = json.loads((w / "stock_data/03-working/health/trading_calendar.json").read_text(encoding="utf-8"))
    sessions = sorted(map(str, cal["open_dates"]))
    anchor = str(last.get("anchor") or books[0]["signal_date"])
    after = [x for x in sessions if x > d]
    entry = next((x for x in sessions if x > str(reb["signal_date"])), None)
    fwd = [x for x in sessions if x >= anchor]
    nxt = next((x for i, x in enumerate(fwd) if x > d and i % H == 0), None)
    sc = pd.read_parquet(fs / f"scores/scores_{d}.parquet", columns=["ts_code", "comp_rank_ew6"])
    sc["rank_pct"] = sc["comp_rank_ew6"].rank(pct=True)
    pit = w / f"stock_data/03-working/fundamental_cache/pit_market/v2/universe_{d}.parquet"
    names = pd.read_parquet(pit, columns=["ts_code", "name", "sw2021_l1_name", "industry_name"]) if pit.exists() \
        else pd.DataFrame(columns=["ts_code", "name", "sw2021_l1_name", "industry_name"])
    f = pd.DataFrame({"ts_code": held}).merge(sc, on="ts_code", how="left").merge(names, on="ts_code", how="left")
    f = f.sort_values("rank_pct", ascending=False, na_position="last").reset_index(drop=True)
    holdings = [{
        "rank": i + 1, "code": r.ts_code[:6], "ts_code": r.ts_code, "name": None if pd.isna(r.name) else str(r.name),
        "industry": next((str(x) for x in (r.sw2021_l1_name, r.industry_name) if isinstance(x, str) and x), None),
        "score_pct": None if pd.isna(r.rank_pct) else round(float(r.rank_pct), 4),
    } for i, r in enumerate(f.itertuples(index=False))]
    retirement = {}
    rp = w / "config/v43_retirement.json"
    if rp.exists():
        r = json.loads(rp.read_text(encoding="utf-8"))
        retirement = {k: r.get(k) for k in ("retired", "retired_at", "last_signal_date")}
    official = [b for b in books]
    return {
        "schema": SCHEMA, "status": "ok", "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": d, "signal_date": d, "next_session": after[0] if after else None,
        "product": {"name": "EW6 等权六因子 · 前向影子持仓", "book": "buffered_top100",
                    "rule": "入选综合分前 15%、跌出前 35% 才卖出，行业上限 15 只，等权 100 只；每 5 个交易日调仓，T+1 开盘成交",
                    "contract": "forward_shadow_v1", "spec_hash": last.get("spec_hash"), "label": LABEL},
        "book": {"rebalance_signal_date": reb["signal_date"], "entry_date": entry, "next_rebalance_signal_date": nxt,
                 "size": len(holdings),
                 "note": "合同规定调仓日若是缺口日（未按时记账）则不补调，沿用上一期持仓"},
        "ledger": {"anchor": anchor, "official_records": len(official),
                   "first_signal_date": books[0]["signal_date"], "last_signal_date": d,
                   "data_sources": "零付费：tdx 日线 + 东财估值自愈（2026-11-19 起）；口径变化按 11-19 前后分段评估"},
        "v43": {"status": "retired", **retirement,
                "note": "v4.3 依赖 Tushare 筹码与技术因子（cyq_perf / stk_factor），零付费下无法运行，已正式退役并归档"},
        "holdings": holdings,
    }


def check(doc: dict) -> list[str]:
    errs = []
    if doc.get("schema") != SCHEMA or doc.get("status") != "ok":
        return [f"status {doc.get('status')}: {doc.get('reason', '')}"]
    h = doc["holdings"]
    if not 50 <= len(h) <= 100:
        errs.append(f"holdings size {len(h)} outside 50..100")
    if len({x["ts_code"] for x in h}) != len(h):
        errs.append("duplicate holdings")
    if sum(1 for x in h if not x["name"]) > 5:
        errs.append("more than 5 holdings without a name")
    if not doc["product"]["label"]:
        errs.append("label missing")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=str(workspace() / "stock-report"))
    ap.add_argument("--check", action="store_true", help="build and validate only, do not write")
    a = ap.parse_args(argv)
    doc = build()
    errs = check(doc)
    if errs:
        print(json.dumps({"status": "invalid", "errors": errs}, ensure_ascii=False))
        return 1
    if not a.check:
        out = Path(a.repo) / "data/latest/ew6_today_state.json"
        try:   # unchanged apart from generated_at -> keep the file (no empty publish)
            old = json.loads(out.read_text(encoding="utf-8"))
            if {k: v for k, v in old.items() if k != "generated_at"} == {k: v for k, v in doc.items() if k != "generated_at"}:
                print(json.dumps({"status": "ok", "unchanged": True, "signal_date": doc["signal_date"],
                                  "holdings": len(doc["holdings"])}, ensure_ascii=False))
                return 0
        except (OSError, ValueError):
            pass
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, out)
    print(json.dumps({"status": "ok", "signal_date": doc["signal_date"], "holdings": len(doc["holdings"]),
                      "rebalance": doc["book"]["rebalance_signal_date"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
