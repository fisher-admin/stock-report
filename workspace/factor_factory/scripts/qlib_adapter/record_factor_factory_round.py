#!/usr/bin/env python3
"""Record factor_factory round outputs into the local Qlib recorder.

This script is the bridge from the production factor factory into the Qlib
sidecar experiment store.  It logs summary params/metrics and attaches the
round's detailed factor_factory artifacts to a Qlib/MLflow recorder.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
QLIB_MLRUNS = ROOT / "qlib_lab/mlruns"
EXPERIMENT_NAME = "factor_factory_rounds"
TZ = ZoneInfo("Asia/Shanghai")

JSON_ARTIFACTS = [
    "outputs/evolution/qlib_factor_scorecard.json",
    "outputs/evolution/qlib_integration_deployment_report.json",
    "outputs/evolution/validation_readiness_and_label_gap_report.json",
    "outputs/evolution/candidate_factor_hypotheses.json",
    "outputs/evolution/factor_iteration_pool.json",
    "outputs/evolution/factor_lineage.json",
    "outputs/evolution/factor_validation_report.json",
    "outputs/evolution/portfolio_backtest_report.json",
    "outputs/evolution/overfit_audit_report.json",
    "outputs/evolution/factor_promotion_decision.json",
    "outputs/evolution/panel_label_rebuild_stall_report.json",
    "outputs/greenfield_multifactor/market_state_hypotheses.json",
    "outputs/greenfield_multifactor/factor_pool_snapshot.json",
    "outputs/greenfield_multifactor/current_factor_leaders.json",
    "outputs/greenfield_multifactor/walkforward_report.json",
    "outputs/greenfield_multifactor/analog_validation.json",
    "outputs/greenfield_multifactor/portfolio_spec.json",
    "outputs/greenfield_multifactor/current_selection_snapshot.json",
    "outputs/greenfield_multifactor/validation_summary.json",
    "qlib_lab/experiments/qlib_factor_arena_report.json",
    "qlib_lab/exports/qlib_dataset_config.json",
    "qlib_lab/exports/panel_for_qlib.meta.json",
]

TEXT_ARTIFACTS = [
    "outputs/evolution/qlib_production_decision_log.md",
    "outputs/evolution/qlib_integration_deployment_report.md",
    "outputs/evolution/validation_readiness_and_label_gap_report.md",
    "outputs/evolution/daily_factor_evolution_digest.md",
    "outputs/evolution/panel_label_rebuild_stall_report.md",
    "outputs/greenfield_multifactor/market_state_hypotheses.md",
    "outputs/greenfield_multifactor/market_state_refinement.md",
    "outputs/greenfield_multifactor/factor_pool_engineering_audit.md",
    "outputs/greenfield_multifactor/validation_verdict.md",
]


def iso_now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_optional_manifest(root: Path, overlay_manifest: Path | None) -> Path | None:
    if overlay_manifest is None:
        return None
    candidate = overlay_manifest.expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"overlay manifest not found: {candidate}")
    return candidate


def dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def bool_metric(value: Any) -> float:
    return 1.0 if bool(value) else 0.0


def best_model_by_rank_ic(models: list[dict[str, Any]]) -> dict[str, Any] | None:
    finite = [
        model
        for model in models
        if ((model.get("rank_ic") or {}).get("mean")) is not None
    ]
    if not finite:
        return None
    return max(finite, key=lambda model: (model.get("rank_ic") or {}).get("mean"))


def existing_artifacts(root: Path) -> list[str]:
    paths = []
    for rel in JSON_ARTIFACTS + TEXT_ARTIFACTS:
        path = root / rel
        if path.exists():
            paths.append(str(path))
    return paths


def build_round_payload(root: Path, round_name: str, overlay_manifest: Path | None = None) -> dict[str, Any]:
    scorecard = load_json(root / "outputs/evolution/qlib_factor_scorecard.json")
    readiness = load_json(root / "outputs/evolution/validation_readiness_and_label_gap_report.json")
    deployment = load_json(root / "outputs/evolution/qlib_integration_deployment_report.json")
    greenfield_validation = load_json(root / "outputs/greenfield_multifactor/validation_summary.json")
    greenfield_portfolio = load_json(root / "outputs/greenfield_multifactor/portfolio_spec.json")
    greenfield_walkforward = load_json(root / "outputs/greenfield_multifactor/walkforward_report.json")
    greenfield_analog = load_json(root / "outputs/greenfield_multifactor/analog_validation.json")
    overlay_payload = load_json(overlay_manifest) if overlay_manifest is not None else {}
    overlay_bundle = (
        overlay_payload.get("overlay_bundle")
        or overlay_payload.get("overlay_constraints")
        or overlay_payload.get("overlays")
        or {}
    ) if overlay_payload else {}

    model_scorecards = scorecard.get("model_scorecards", [])
    best_model = best_model_by_rank_ic(model_scorecards)
    label_maturity = scorecard.get("label_maturity", {})
    promotion_gate = scorecard.get("promotion_gate", {})
    readiness_gate = readiness.get("validation_readiness", {})
    label_gap = readiness.get("label_gap", {})

    params = {
        "round_name": round_name,
        "recorded_at": iso_now(),
        "qlib_version": scorecard.get("qlib_version") or deployment.get("qlib_version", "unknown"),
        "label_maturity_status": label_maturity.get("status", "unknown"),
        "promotion_eligible": str(bool(promotion_gate.get("eligible_for_promotion", False))).lower(),
        "sidecar_only": "true",
        "best_model_by_rank_ic": (best_model or {}).get("model", "none"),
        "mainline": greenfield_validation.get("mainline", "unknown"),
        "legacy_prebreakout_role": greenfield_validation.get("legacy_prebreakout_role", "unknown"),
        "greenfield_trade_date": str(greenfield_portfolio.get("trade_date", "unknown")),
    }
    metrics = {
        "qlib_model_count": float(len(model_scorecards)),
        "audit_weight_ready": bool_metric(readiness_gate.get("audit_weight_ready", False)),
        "audit_weight_blocked": bool_metric(readiness_gate.get("audit_weight_blocked", False)),
        "latest_rows_with_any_missing_label": float(label_gap.get("latest_rows_with_any_missing_label") or 0),
        "promotion_eligible": bool_metric(promotion_gate.get("eligible_for_promotion", False)),
        "greenfield_selected_factor_count": float(len(greenfield_portfolio.get("selected_factors", []))),
    }
    if overlay_manifest is not None:
        blacklist = overlay_bundle.get("industry_blacklist") or []
        params.update(
            {
                "overlay_manifest": str(overlay_manifest),
                "overlay_manifest_path": str(overlay_manifest),
                "overlay_contract_version": str(
                    overlay_payload.get("contract_version")
                    or overlay_payload.get("schema_version")
                    or "unknown"
                ),
                "overlay_candidate_id": str(overlay_payload.get("candidate_id") or "unknown"),
                "overlay_top_n": overlay_bundle.get("top_n"),
                "overlay_max_names_per_industry": overlay_bundle.get("max_names_per_industry"),
                "overlay_industry_blacklist_count": len(blacklist),
            }
        )
        metrics["overlay_manifest_provided"] = 1.0
        if overlay_bundle.get("top_n") is not None:
            metrics["overlay_top_n"] = float(overlay_bundle["top_n"])
        if overlay_bundle.get("max_names_per_industry") is not None:
            metrics["overlay_max_names_per_industry"] = float(overlay_bundle["max_names_per_industry"])
        metrics["overlay_industry_blacklist_count"] = float(len(blacklist))
    if best_model is not None:
        metrics["best_rank_ic_mean"] = float(best_model["rank_ic"]["mean"])
        metrics["best_rank_ic_count"] = float(best_model["rank_ic"].get("count") or 0)
        spread = best_model.get("top_bottom_spread") or {}
        if spread.get("mean") is not None:
            metrics["best_top_bottom_spread_mean"] = float(spread["mean"])
    if greenfield_validation:
        for key in [
            "walkforward_close_to_close_excess_total_return",
            "walkforward_open_to_close_excess_total_return",
            "analog_close_to_close_total_return",
            "analog_open_to_close_total_return",
        ]:
            value = greenfield_validation.get(key)
            if value is not None:
                metrics[key] = float(value)
    walkforward_aggregate = greenfield_walkforward.get("aggregate", {}) if isinstance(greenfield_walkforward, dict) else {}
    analog_aggregate = greenfield_analog.get("aggregate", {}) if isinstance(greenfield_analog, dict) else {}
    for prefix, aggregate in {
        "walkforward_close_to_close": walkforward_aggregate.get("close_to_close", {}),
        "walkforward_open_to_close": walkforward_aggregate.get("open_to_close", {}),
        "analog_close_to_close": analog_aggregate.get("close_to_close", {}),
        "analog_open_to_close": analog_aggregate.get("open_to_close", {}),
    }.items():
        if (aggregate or {}).get("total_return") is not None:
            metrics[f"{prefix}_total_return"] = float(aggregate["total_return"])
        if (aggregate or {}).get("sharpe") is not None:
            metrics[f"{prefix}_sharpe"] = float(aggregate["sharpe"])

    artifact_paths = existing_artifacts(root)
    if overlay_manifest is not None:
        artifact_paths.append(str(overlay_manifest))

    return {
        "params": params,
        "metrics": metrics,
        "artifact_paths": dedupe_paths(artifact_paths),
        "overlay_manifest_summary": {
            "path": str(overlay_manifest) if overlay_manifest is not None else None,
            "candidate_id": overlay_payload.get("candidate_id") if overlay_payload else None,
            "overlay_bundle": overlay_bundle if overlay_payload else {},
        },
    }


def record_to_qlib(payload: dict[str, Any], recorder_name: str, mlruns_uri: str) -> dict[str, Any]:
    import qlib
    from qlib.workflow import R

    qlib.init(
        provider_uri=str(ROOT / "qlib_lab/qlib_data"),
        exp_manager={
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {
                "uri": mlruns_uri,
                "default_exp_name": EXPERIMENT_NAME,
            },
        },
        logging_level="WARNING",
    )

    with R.start(experiment_name=EXPERIMENT_NAME, recorder_name=recorder_name):
        R.log_params(**payload["params"])
        R.log_metrics(**payload["metrics"])
        for artifact in payload["artifact_paths"]:
            artifact_path = "factor_factory_round"
            R.log_artifact(artifact, artifact_path=artifact_path)
        recorder = R.get_recorder()
        return {
            "experiment_name": EXPERIMENT_NAME,
            "recorder_id": recorder.id,
            "recorder_name": recorder.name,
            "artifact_count": len(payload["artifact_paths"]),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--round-name", default=f"factor_factory_round_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument("--mlruns-uri", default=f"file:{QLIB_MLRUNS}")
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=ROOT / "outputs/evolution/qlib_recorder_sync_latest.json",
    )
    parser.add_argument("--overlay-manifest", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    overlay_manifest = resolve_optional_manifest(args.root, args.overlay_manifest)
    payload = build_round_payload(args.root, args.round_name, overlay_manifest=overlay_manifest)
    if args.dry_run:
        result = {
            "dry_run": True,
            "overlay_manifest": str(overlay_manifest) if overlay_manifest is not None else None,
            "payload": payload,
        }
    else:
        result = {
            "dry_run": False,
            "overlay_manifest": str(overlay_manifest) if overlay_manifest is not None else None,
            "payload_summary": {
                "param_count": len(payload["params"]),
                "metric_count": len(payload["metrics"]),
                "artifact_count": len(payload["artifact_paths"]),
            },
            "qlib_recorder": record_to_qlib(payload, args.round_name, args.mlruns_uri),
        }
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
