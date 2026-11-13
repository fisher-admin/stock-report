#!/usr/bin/env python3
"""sync_heatmap_candidates.py — 从当日选股结果生成 heatmap 候选股高亮文件。

读取 stock-report/data/latest/{recommendation_state,greenfield_top20}.json，
写出 a-share-heatmap/public/candidates.json（key 用 000685.SZ 格式，与 treemap 对齐）。

由每日发布管线调用；heatmap 服务热加载 /candidates.json，无需重启。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

_TZ_SH = timezone(timedelta(hours=8))

WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE")
    or os.environ.get("STOCK_SYSTEM_WORKSPACE")
    or str(Path.home() / ".openclaw" / "workspace")
)
SRC_DIR = WORKSPACE / "stock-report" / "data" / "latest"
DST = WORKSPACE / "repos" / "a-share-heatmap" / "public" / "candidates.json"


def to_full_code(code: str) -> str | None:
    c = str(code or "").strip()
    if not c:
        return None
    if "." in c:
        return c.upper()
    c6 = c.split(".")[0]
    if not c6.isdigit():
        return None
    if c6.startswith(("4", "8", "92")) or c6.startswith("9"):
        return f"{c6}.BJ"
    if c6.startswith(("6", "5")):
        return f"{c6}.SH"
    return f"{c6}.SZ"


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def build() -> dict:
    rec = load_json(SRC_DIR / "recommendation_state.json")
    gf = load_json(SRC_DIR / "greenfield_top20.json")
    stocks: dict[str, dict] = {}
    for s in rec.get("final_recommendations") or []:
        if not isinstance(s, dict):
            continue
        fc = to_full_code(s.get("code") or s.get("ts_code") or "")
        if not fc:
            continue
        stocks[fc] = {
            "name": s.get("name"),
            "score": s.get("score"),
            "rank": s.get("rank"),
            "tag": "recommend",
        }
    for i, s in enumerate(gf.get("top20") or []):
        if not isinstance(s, dict):
            continue
        fc = to_full_code(s.get("ts_code") or s.get("code") or "")
        if not fc:
            continue
        if fc in stocks:
            stocks[fc]["tag"] = "both"
        else:
            stocks[fc] = {
                "name": s.get("name"),
                "score": s.get("composite_score"),
                "rank": i + 1,
                "tag": "greenfield",
            }
    return {
        "generated_at": datetime.now(_TZ_SH).isoformat(timespec="seconds"),
        "trade_date": rec.get("trade_date") or gf.get("trade_date"),
        "count": len(stocks),
        "stocks": stocks,
    }


def main() -> int:
    doc = build()
    DST.parent.mkdir(parents=True, exist_ok=True)
    tmp = DST.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DST)
    print(f"heatmap candidates: {doc['count']} stocks -> {DST} (trade_date={doc['trade_date']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
