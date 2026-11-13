#!/usr/bin/env python3
"""Daily diagnostic report: v45 vs v44 (plan Epic C). Not a promotion metric."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_report(v44: dict[str, Any], v45: dict[str, Any]) -> dict[str, Any]:
    c44 = [str(r.get("ts_code") or "") for r in (v44.get("candidates") or [])]
    c45 = [str(r.get("ts_code") or "") for r in (v45.get("candidates") or [])]
    s44, s45 = set(c44), set(c45)
    hash_ok = str(v44.get("input_universe_hash") or "") == str(v45.get("input_universe_hash") or "")
    codes_ok = set(v44.get("confirmed_universe_ts_codes") or s44) == set(
        v45.get("confirmed_universe_ts_codes") or s45
    )
    return {
        "schema_version": "v45_vs_v44_diagnostic.v1",
        "generated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "trade_date": str(v45.get("signal_date") or v45.get("trade_date") or ""),
        "primary_compare": "prebreakout_v45_cross_sectional vs prebreakout_v44_balanced",
        "not_for_promotion": True,
        "universe": {
            "input_universe_hash_match": hash_ok,
            "confirmed_code_set_match": codes_ok,
            "v44_hash": v44.get("input_universe_hash"),
            "v45_hash": v45.get("input_universe_hash"),
            "v44_size": v44.get("input_universe_size"),
            "v45_size": v45.get("input_universe_size"),
        },
        "topn_overlap": {
            "jaccard": round(jaccard(s44, s45), 4),
            "shared_count": len(s44 & s45),
            "only_v44": sorted(s44 - s45),
            "only_v45": sorted(s45 - s44),
        },
        "v45_diagnostics": v45.get("diagnostics") or {},
        "v45_cs_pipeline": v45.get("cs_pipeline"),
        "v45_validation_start_date": v45.get("validation_start_date"),
        "execution_authority": "observe_only_no_auto_order",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write v45 vs v44 diagnostic report")
    parser.add_argument("--daily-dir", required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    daily = Path(args.daily_dir)
    trade_date = args.trade_date
    v44_path = daily / f"prebreakout_v44_balanced_{trade_date}_candidate_snapshot.json"
    v45_path = daily / f"prebreakout_v45_cross_sectional_{trade_date}_candidate_snapshot.json"
    if not v44_path.exists() or not v45_path.exists():
        print(json.dumps({"ok": False, "reason": "missing_snapshots"}, ensure_ascii=False))
        return 1
    report = build_report(_load(v44_path), _load(v45_path))
    out = Path(args.output) if args.output else daily / f"v45_vs_v44_diagnostic_{trade_date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(out), "jaccard": report["topn_overlap"]["jaccard"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
