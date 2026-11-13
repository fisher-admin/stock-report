#!/usr/bin/env python3
"""DEPRECATED governance evaluator (Epic A / plan v1.4).

This script no longer emits promotion-authoritative verdicts.
It writes a deprecation notice and points readers to strategy_evaluation.json
plus the dual-track evaluators.

Historical behavior (20-day shadow→active, prebreakout_v41 as active) is retired.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

WS = Path(os.environ.get("OPENCLAW_WORKSPACE_DIR", str(Path.home() / ".openclaw/workspace")))
CONFIG = WS / "fisher-quant/config/strategy_governance.json"
WORKING = WS / "stock_data/03-working"
OUT = WORKING / "strategy_research/governance_evaluation_latest.json"
STRATEGY_EVALUATION = (
    WS / "stock-report/data/latest/strategy_evaluation.json"
)
if not STRATEGY_EVALUATION.exists():
    STRATEGY_EVALUATION = WORKING / "stock-report-repo/data/latest/strategy_evaluation.json"


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def main() -> None:
    gov = load(CONFIG)
    strategy_eval = load(STRATEGY_EVALUATION)

    payload = {
        "schema_version": "governance_eval.v2_deprecated",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "deprecated": True,
        "authority": "not_for_promotion",
        "gates_source": str(CONFIG),
        "config_deprecated": bool(gov.get("deprecated")),
        "message": (
            "This governance evaluation is deprecated and must not be used for promotion. "
            "Use strategy_evaluation.json and short_track / event_quality_drift evaluators."
        ),
        "authoritative_sources": {
            "strategy_evaluation": str(STRATEGY_EVALUATION),
            "short_track_evaluator": (
                "skills/stock-system-orchestrator/scripts/shadow_portfolio_evaluator.py"
            ),
            "event_quality_drift_evaluator": (
                "skills/stock-system-orchestrator/scripts/event_quality_drift_evaluator.py"
            ),
        },
        "strategy_evaluation_present": bool(strategy_eval),
        "strategy_evaluation_summary": {
            "keys": sorted(strategy_eval.keys())[:40] if isinstance(strategy_eval, dict) else [],
        },
        "verdicts": {
            "prebreakout_v41": {
                "declared_status": "alias_of_prebreakout_v43_control",
                "evaluated_status": "observe_only_not_from_this_file",
                "flags": [
                    "governance_eval_deprecated",
                    "do_not_use_for_promotion",
                ],
                "note": (
                    "Legacy active/under_review path retired. "
                    "See dual-track strategy_evaluation and shadow books."
                ),
            },
            "t1_factor_v1": {
                "declared_status": "retired_archived",
                "evaluated_status": "retired_archived",
                "flags": ["governance_eval_deprecated"],
            },
            "greenfield_o2c_v1": {
                "declared_status": "retired_archived",
                "evaluated_status": "retired_archived",
                "flags": ["governance_eval_deprecated"],
            },
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"⚠️  deprecated governance evaluation (not for promotion) -> {OUT}")
    print("   authority=not_for_promotion; use strategy_evaluation.json + dual-track evaluators")
    return


if __name__ == "__main__":
    main()
