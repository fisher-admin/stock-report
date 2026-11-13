#!/usr/bin/env python3
"""Export the public factor-lifecycle contract: data/latest/factor_lifecycle.json.

Source: factor_factory/outputs/forward_shadow/lifecycle_monitor.json (23:30 forward-shadow job; public file lags
one trading day).  Aggregates only: per-signal IC statistics, no per-stock rows.  Research display only.
Missing/invalid input writes an explicit "unavailable" contract.  Exit 0 on missing input; 2 on a write error.
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
SRC = WS / "factor_factory" / "outputs" / "forward_shadow" / "lifecycle_monitor.json"
STYLE_SRC = WS / "factor_factory" / "outputs" / "forward_shadow" / "style_attribution.json"
OUT_REL = "data/latest/factor_lifecycle.json"
CONTRACT = "factor_lifecycle_v1"
HONESTY = ("前瞻监控 · 仅研究观察 · 非买入建议。IC = 前瞻信号日全截面分数与次日开盘起 h 日收益的 Spearman 相关；"
           "回测基线来自发现/验证期（验证期并非干净样本外，偏乐观），h5 按未来 h 日市场涨/跌分环境匹配基线；"
           "预警仅提示，不改变任何预注册判定。")
KEEP = ("status", "message", "as_of", "first_signal", "last_signal", "signal_days", "horizons", "signals", "matured",
        "alerts", "results", "progress", "roll_window", "min_names_per_day", "regime_days", "regime_baseline", "note")


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build(src: dict | None, now: str, style: dict | None = None) -> dict:
    base = {"contract_version": CONTRACT, "generated_at": now, "title": "因子生命周期看板", "honesty_banner": HONESTY,
            "research_only": True, "execution_authority": "observe_only_no_auto_order"}
    if not src or src.get("contract_version") != "lifecycle_monitor_v1":
        return {**base, "status": "unavailable", "reason": "lifecycle_monitor.json 缺失或版本不符"}
    doc = {**base, **{k: src[k] for k in KEEP if k in src}}
    if style and style.get("contract_version") == "style_attribution_v1":
        doc["style"] = {k: style[k] for k in ("status", "as_of", "signal_days", "matured", "styles", "groups", "results",
                                              "redundancy", "baseline_corr_note", "note") if k in style}
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    ap.add_argument("--src", type=Path, default=SRC)
    args = ap.parse_args(argv)
    doc = build(load(args.src), datetime.now().astimezone().isoformat(timespec="seconds"), load(STYLE_SRC))
    out = args.repo / OUT_REL
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(out)
    except OSError as exc:
        print(f"FATAL: cannot write {out}: {exc}", file=sys.stderr)
        return 2
    print(f"factor_lifecycle: status={doc['status']} as_of={doc.get('as_of')} alerts={len(doc.get('alerts') or [])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
