#!/usr/bin/env python3
"""Sync Qlib sidecar experiment results back to factor_factory artifacts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
DEFAULT_REPORT = ROOT / "qlib_lab/experiments/qlib_factor_arena_report.json"
DEFAULT_SCORECARD = ROOT / "outputs/evolution/qlib_factor_scorecard.json"
DEFAULT_DECISION_LOG = ROOT / "outputs/evolution/qlib_production_decision_log.md"
TZ = ZoneInfo("Asia/Shanghai")


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--scorecard", type=Path, default=DEFAULT_SCORECARD)
    parser.add_argument("--decision-log", type=Path, default=DEFAULT_DECISION_LOG)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.report.exists():
        raise FileNotFoundError(args.report)
    report = json.loads(args.report.read_text(encoding="utf-8"))

    scorecard = {
        "generated_at": iso_now(),
        "source_report": str(args.report),
        "qlib_version": report.get("qlib_version"),
        "sidecar_only": True,
        "label_maturity": report.get("label_maturity", {}),
        "train_window": report.get("train_window"),
        "test_window": report.get("test_window"),
        "model_scorecards": report.get("models", []),
        "promotion_gate": {
            "eligible_for_promotion": False,
            "reason": "Qlib sidecar is a second validation layer; production promotion still requires factor_factory maturity, OOS, overfit, and label gates.",
        },
    }
    args.scorecard.parent.mkdir(parents=True, exist_ok=True)
    args.scorecard.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")

    best = None
    for model in scorecard["model_scorecards"]:
        mean_ic = ((model.get("rank_ic") or {}).get("mean"))
        if mean_ic is None:
            continue
        if best is None or mean_ic > best["rank_ic"]["mean"]:
            best = model

    lines = [
        "# Qlib Production Decision Log",
        "",
        f"- generated_at: {scorecard['generated_at']}",
        f"- source_report: {args.report}",
        f"- qlib_version: {scorecard['qlib_version']}",
        "- role: sidecar validation only",
        "- production_action: no direct promotion",
        "",
        "## Best sidecar model by RankIC",
    ]
    if best is None:
        lines.append("- no model produced a finite RankIC")
    else:
        lines.extend(
            [
                f"- model: {best['model']}",
                f"- rank_ic_mean: {best['rank_ic']['mean']}",
                f"- rank_ic_count: {best['rank_ic']['count']}",
                f"- top_bottom_spread_mean: {(best.get('top_bottom_spread') or {}).get('mean')}",
            ]
        )
    lines.extend(
        [
            "",
            "## Gate",
            "- Qlib output may update research scorecards.",
            "- Qlib output must not directly update production weights.",
            "- Factor promotion remains frozen unless factor_factory label maturity, OOS, and overfit gates pass.",
        ]
    )
    args.decision_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"scorecard": str(args.scorecard), "decision_log": str(args.decision_log)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
