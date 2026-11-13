#!/usr/bin/env python3
"""Stage 5: sync recommendation warehouse and backfill historical recommendations."""

from __future__ import annotations

import argparse

from orchestrator_common import write_health_snapshot
from recommendation_warehouse import SYNC_REPORT_PATH, sync_recommendation_warehouse


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync recommendation warehouse and exports")
    parser.add_argument(
        "--target-date",
        default="",
        help="Expected target trade date like YYYYMMDD. When provided, stage5 only accepts same-day recommendation and price state.",
    )
    args = parser.parse_args()
    target_date = str(args.target_date or "").strip() or None
    try:
        payload = sync_recommendation_warehouse(target_date=target_date)
    except Exception as exc:
        payload = {
            "generated_at": None,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latest_recommend_date": target_date,
            "target_date": target_date,
        }
    trade_date = str(payload.get("latest_recommend_date") or "")
    write_health_snapshot(SYNC_REPORT_PATH, payload, trade_date=trade_date or None)
    print(f"[stage5] wrote recommendation sync report: {SYNC_REPORT_PATH}")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
