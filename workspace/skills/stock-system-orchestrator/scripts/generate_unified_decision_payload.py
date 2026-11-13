#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from orchestrator_common import PUBLISHED_REPO

ROOT = PUBLISHED_REPO / "data" / "recommendation_analytics"
LATEST = PUBLISHED_REPO / "data" / "latest"
OUT = ROOT / "unified_decision_payload.json"


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def main() -> int:
    market_state = load(LATEST / "market_state.json")
    strategy_state = load(LATEST / "strategy_state.json")
    candidate_state = load(LATEST / "candidate_state.json")
    system_verdict = load(LATEST / "system_verdict.json")
    strategy = ((strategy_state.get("strategies") or [{}])[0]) if strategy_state else {}

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "v4-compat",
        "compatibility_mode": True,
        "source_of_truth": "data/latest/system_verdict.json",
        "morning_brief": market_state.get("morning") or {},
        "market_heatmap": {
            "latest_trade_date": market_state.get("latest_trade_date"),
            "top_sectors": market_state.get("top_market_sectors") or [],
        },
        "strategy_heatmap": {
            "latest_recommend_date": strategy_state.get("latest_trade_date"),
            "top_sectors": [
                {
                    "sector_name": name,
                    "heat_rank": idx + 1,
                    "heat_ema_5": None,
                }
                for idx, name in enumerate(strategy.get("top_industries") or [])
            ],
        },
        "industry_actions": market_state.get("industry_actions") or [],
        "system_verdict": {
            "decision_trade_date": ((system_verdict.get("dates") or {}).get("decision_trade_date")),
            "action": ((system_verdict.get("final_action") or {}).get("action")),
            "label": ((system_verdict.get("final_action") or {}).get("label")),
        },
        "candidate_rollup": {
            "role_counts": candidate_state.get("role_counts") or {},
            "strategy_summaries": candidate_state.get("strategy_summaries") or [],
        },
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"created={OUT}")
    print(json.dumps({
        "version": payload["version"],
        "decision_trade_date": payload["system_verdict"]["decision_trade_date"],
        "final_action": payload["system_verdict"]["action"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
