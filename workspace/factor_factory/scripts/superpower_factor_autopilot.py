#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Superpower-style autopilot for factor_factory.

Dry-run first: reads the latest factor_factory state, selects the next route
according to the latest autonomous optimization plan, and writes machine-readable
state/decision artifacts. Execute mode is intentionally conservative and does
not publish or lift promotion freeze.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path("/Users/fisher/.openclaw/workspace/factor_factory")
CONFIG_PATH = ROOT / "config/superpower_factor_autopilot.yaml"


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_load_error": str(exc), "_path": str(path)}


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        # The config is deliberately simple. If PyYAML is unavailable, return
        # defaults and keep dry-run usable.
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def stat_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    st = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "size_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, TZ).isoformat(timespec="seconds"),
    }


def newest(paths: list[Path]) -> Path | None:
    existing = [p for p in paths if p.exists()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def shell_check(cmd: list[str]) -> dict[str, Any]:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        return {"cmd": cmd, "exit_code": cp.returncode, "stdout": cp.stdout.strip(), "stderr": cp.stderr.strip()}
    except Exception as exc:
        return {"cmd": cmd, "exit_code": None, "error": str(exc)}


@dataclass
class RouteDecision:
    route: str
    reason: str
    allowed_in_dry_run: bool
    execute_allowed: bool
    next_actions: list[str]
    expected_artifacts: list[str]
    blockers: list[str]


def discover_superpowers(cfg: dict[str, Any]) -> dict[str, Any]:
    sys_cfg = cfg.get("system", {})
    repo = Path(sys_cfg.get("superpowers_repo") or "/Users/fisher/.hermes/vendor_imports/superpowers")
    skills_link = Path(sys_cfg.get("superpowers_skills_link") or "/Users/fisher/.agents/skills/superpowers")
    hook = repo / "hooks/session-start"
    result = {
        "repo": stat_info(repo),
        "skills_dir": stat_info(repo / "skills"),
        "skills_link": stat_info(skills_link),
        "using_superpowers_skill": stat_info(repo / "skills/using-superpowers/SKILL.md"),
        "writing_plans_skill": stat_info(repo / "skills/writing-plans/SKILL.md"),
        "subagent_driven_development_skill": stat_info(repo / "skills/subagent-driven-development/SKILL.md"),
        "verification_skill": stat_info(repo / "skills/verification-before-completion/SKILL.md"),
    }
    if hook.exists():
        check = shell_check(["bash", str(hook)])
        raw = check.get("stdout") or ""
        hook_ok = False
        has_bootstrap = False
        ctx_len = 0
        try:
            obj = json.loads(raw)
            ctx = obj.get("additionalContext") or obj.get("additional_context") or (obj.get("hookSpecificOutput") or {}).get("additionalContext") or ""
            hook_ok = True
            has_bootstrap = "using-superpowers" in ctx
            ctx_len = len(ctx)
        except Exception:
            pass
        result["session_start_hook"] = {
            "path": str(hook),
            "json_ok": hook_ok,
            "contains_using_superpowers": has_bootstrap,
            "additional_context_len": ctx_len,
            "exit_code": check.get("exit_code"),
        }
    return result


def latest_stage_control() -> tuple[Path | None, dict[str, Any]]:
    candidates = [Path(p) for p in glob.glob(str(ROOT / "outputs/evolution/current_combo_stage*_control_sync.json"))]
    if not candidates:
        return None, {}

    def stage_num(path: Path) -> int:
        m = re.search(r"stage(\d+)", path.name)
        return int(m.group(1)) if m else -1

    path = max(candidates, key=lambda p: (stage_num(p), p.stat().st_mtime))
    return path, load_json(path)


def latest_blocker() -> tuple[Path | None, dict[str, Any], bool]:
    patterns = [
        str(ROOT / "outputs/evolution/p1_long_only_infeasible_blocker*.json"),
        str(ROOT / "outputs/evolution/*blocker*.json"),
    ]
    paths: list[Path] = []
    for pat in patterns:
        paths.extend(Path(p) for p in glob.glob(pat))
    path = newest(list(set(paths)))
    if path is None:
        return None, {}, False
    payload = load_json(path)
    status = str(payload.get("status") or payload.get("decision") or "").lower()
    resolved = "resolved" in status or ".resolved_" in path.name
    return path, payload, not resolved


def collect_state(cfg: dict[str, Any]) -> dict[str, Any]:
    evo = ROOT / "outputs/evolution"
    green = ROOT / "outputs/greenfield_multifactor"
    stage_path, stage_payload = latest_stage_control()
    blocker_path, blocker_payload, blocker_active = latest_blocker()
    p1_audit_path = evo / "p1_greenfield_guardrail_audit.json"
    p1_audit = load_json(p1_audit_path)
    validation = load_json(green / "validation_summary.json")
    portfolio = load_json(green / "portfolio_spec.json")
    walkforward = load_json(green / "walkforward_report.json")
    analog = load_json(green / "analog_validation.json")
    promotion = load_json(evo / "factor_promotion_decision.json")

    return {
        "generated_at": now_iso(),
        "root": str(ROOT),
        "config": str(CONFIG_PATH),
        "superpowers": discover_superpowers(cfg),
        "optimization_plan": {
            "md": stat_info(ROOT / "outputs/evolution/factor_factory_superpower_autonomous_execution_plan_20260503.md"),
            "json": load_json(ROOT / "outputs/evolution/factor_factory_superpower_autonomous_execution_plan_20260503.json"),
        },
        "p1_greenfield": {
            "audit_path": str(p1_audit_path),
            "audit": {
                "decision": p1_audit.get("decision"),
                "failed_guardrails": p1_audit.get("failed_guardrails"),
                "promotion_freeze_lifted": p1_audit.get("promotion_freeze_lifted"),
                "generated_at": p1_audit.get("generated_at"),
            },
            "validation_summary": {
                "status": validation.get("status"),
                "scenario_id": ((validation.get("run_context") or {}).get("scenario_meta") or {}).get("scenario_id"),
                "walkforward_c2c_excess": validation.get("walkforward_close_to_close_excess_total_return"),
                "analog_c2c_total": validation.get("analog_close_to_close_total_return"),
            },
            "portfolio": {
                "trade_date": portfolio.get("trade_date"),
                "selected_factor_count": len(portfolio.get("selected_factors", [])) if isinstance(portfolio.get("selected_factors"), list) else portfolio.get("selected_factor_count"),
            },
            "artifact_stats": {
                "walkforward_report": stat_info(green / "walkforward_report.json"),
                "analog_validation": stat_info(green / "analog_validation.json"),
                "validation_summary": stat_info(green / "validation_summary.json"),
                "portfolio_spec": stat_info(green / "portfolio_spec.json"),
            },
        },
        "stage_release_continuation": {
            "latest_control_path": str(stage_path) if stage_path else None,
            "latest_control": {
                "decision": stage_payload.get("decision"),
                "promotion_freeze": ((stage_payload.get("global_constraints") or {}).get("promotion_freeze") if isinstance(stage_payload.get("global_constraints"), dict) else stage_payload.get("promotion_freeze")),
                "stage": stage_payload.get("stage") or stage_payload.get("round") or (stage_path.name if stage_path else None),
            },
        },
        "promotion_decision": {
            "path": str(evo / "factor_promotion_decision.json"),
            "global_constraints": promotion.get("global_constraints"),
            "decision": promotion.get("decision") or promotion.get("overall_decision"),
        },
        "latest_blocker": {
            "path": str(blocker_path) if blocker_path else None,
            "active": blocker_active,
            "status": blocker_payload.get("status"),
            "decision": blocker_payload.get("decision"),
            "promotion_freeze_lifted": blocker_payload.get("promotion_freeze_lifted"),
        },
    }


def decide_route(state: dict[str, Any], cfg: dict[str, Any]) -> RouteDecision:
    p1 = state["p1_greenfield"]
    audit = p1["audit"]
    failed = audit.get("failed_guardrails") or []
    decision = audit.get("decision")
    freeze_lifted = bool(audit.get("promotion_freeze_lifted"))
    blocker = state.get("latest_blocker") or {}

    if blocker.get("active") and failed:
        return RouteDecision(
            route="blocker_transform",
            reason="latest blocker is active and audit still has failed guardrails",
            allowed_in_dry_run=True,
            execute_allowed=True,
            next_actions=[
                "Read latest blocker original_blocker/next_executable_path.",
                "Classify blocker type and run only whitelisted repair actions.",
                "Write transformed blocker or resolved decision after verification.",
            ],
            expected_artifacts=["outputs/evolution/*transformed*.json", "outputs/evolution/*auto_intervention_progress*.json"],
            blockers=[],
        )

    if failed:
        return RouteDecision(
            route="research_candidate_repair",
            reason="guardrail audit has failed_guardrails; repair factors/weights without lowering guardrails",
            allowed_in_dry_run=True,
            execute_allowed=True,
            next_actions=[
                "Run materialization-only and inspect failed guardrail dimensions.",
                "Add or replace genuine long-side factors; do not use mechanical inversion.",
                "Run full backtest and guardrail audit.",
            ],
            expected_artifacts=[
                "outputs/greenfield_multifactor/validation_summary.json",
                "outputs/evolution/p1_greenfield_guardrail_audit.json",
            ],
            blockers=[],
        )

    if decision == "guardrail_pass_research_candidate" and not freeze_lifted:
        return RouteDecision(
            route="promotion_governance",
            reason="P1 v12 guardrails pass but promotion_freeze_lifted=false; latest plan forbids release without governance",
            allowed_in_dry_run=True,
            execute_allowed=True,
            next_actions=[
                "Write p1_promotion_governance_decision.json with release eligibility checks.",
                "Check independent_oos_days >= 60 and holdout_days >= 60 before any freeze lift.",
                "If insufficient sample, keep research_candidate and schedule extended OOS/holdout generation.",
                "Record Qlib round with qlib_lab venv after governance artifact exists.",
            ],
            expected_artifacts=[
                "outputs/evolution/p1_promotion_governance_decision.json",
                "outputs/evolution/p1_research_candidate_extended_oos.json",
                "outputs/evolution/p1_research_candidate_qlib_recorder_sync.json",
            ],
            blockers=[],
        )

    stage = state.get("stage_release_continuation") or {}
    if stage.get("latest_control_path"):
        return RouteDecision(
            route="o2c_c2o_validation",
            reason="Stage release-continuation exists; latest plan requires o2c/c2o independent validation without extrapolating c2c release",
            allowed_in_dry_run=True,
            execute_allowed=True,
            next_actions=[
                "Build/refresh multi-label validation context.",
                "Run o2c and c2o label-specific replay, nested validation, walkforward and holdout.",
                "Write separate c2c/o2c/c2o decision; keep c2c-only release scope if other labels fail.",
            ],
            expected_artifacts=[
                "outputs/evolution/current_combo_nested_validation.o2c_label_specific.json",
                "outputs/evolution/current_combo_nested_validation.c2o_label_specific.json",
                "outputs/evolution/current_combo_multi_label_decision.json",
            ],
            blockers=[],
        )

    return RouteDecision(
        route="data_label_refresh",
        reason="no urgent blocker/research candidate/release continuation route detected; refresh data and context",
        allowed_in_dry_run=True,
        execute_allowed=True,
        next_actions=["Run factor_evolution_cron_context.py", "Refresh data/label readiness reports."],
        expected_artifacts=["outputs/evolution/validation_readiness_and_label_gap_report.json"],
        blockers=[],
    )


def write_outputs(state: dict[str, Any], decision: RouteDecision, cfg: dict[str, Any], mode: str) -> dict[str, Path]:
    outs = (cfg.get("outputs") or {})
    state_path = ROOT / (outs.get("state") or "outputs/evolution/superpower_autopilot_state.json")
    decision_path = ROOT / (outs.get("latest_decision") or "outputs/evolution/superpower_autopilot_latest_decision.json")
    journal_path = ROOT / (outs.get("journal") or "outputs/evolution/superpower_autopilot_journal.md")
    for p in [state_path, decision_path, journal_path]:
        p.parent.mkdir(parents=True, exist_ok=True)

    payload_state = {
        "schema_version": "2026-05-03.superpower_factor_autopilot.state.v1",
        "mode": mode,
        **state,
    }
    payload_decision = {
        "schema_version": "2026-05-03.superpower_factor_autopilot.decision.v1",
        "generated_at": now_iso(),
        "mode": mode,
        "route": decision.route,
        "reason": decision.reason,
        "allowed_in_dry_run": decision.allowed_in_dry_run,
        "execute_allowed": decision.execute_allowed,
        "next_actions": decision.next_actions,
        "expected_artifacts": decision.expected_artifacts,
        "blockers": decision.blockers,
        "hard_rules_active": cfg.get("hard_rules", {}),
        "optimization_plan": str(ROOT / "outputs/evolution/factor_factory_superpower_autonomous_execution_plan_20260503.md"),
    }
    state_path.write_text(json.dumps(payload_state, ensure_ascii=False, indent=2), encoding="utf-8")
    decision_path.write_text(json.dumps(payload_decision, ensure_ascii=False, indent=2), encoding="utf-8")

    line = (
        f"\n## {now_iso()} — mode={mode} route={decision.route}\n"
        f"- reason: {decision.reason}\n"
        f"- next_actions: {json.dumps(decision.next_actions, ensure_ascii=False)}\n"
        f"- expected_artifacts: {json.dumps(decision.expected_artifacts, ensure_ascii=False)}\n"
    )
    with journal_path.open("a", encoding="utf-8") as f:
        f.write(line)
    return {"state": state_path, "decision": decision_path, "journal": journal_path}


def maybe_write_governance(decision: RouteDecision, state: dict[str, Any], cfg: dict[str, Any], mode: str) -> Path | None:
    if decision.route != "promotion_governance":
        return None
    if mode not in {"dry-run", "execute"}:
        return None
    gov_cfg = cfg.get("promotion_governance", {})
    path = ROOT / "outputs/evolution/p1_promotion_governance_decision.json"
    # Conservative default: current available artifacts do not prove >=60+60
    # independent OOS/holdout for P1 v12, so release remains blocked.
    payload = {
        "schema_version": "2026-05-03.p1_promotion_governance_decision.v1",
        "generated_at": now_iso(),
        "mode": mode,
        "source_audit": state["p1_greenfield"]["audit_path"],
        "source_decision": state["p1_greenfield"]["audit"].get("decision"),
        "promotion_freeze_lifted_before_governance": state["p1_greenfield"]["audit"].get("promotion_freeze_lifted"),
        "release_candidate_allowed": False,
        "research_candidate_allowed": True,
        "production_allowed": False,
        "checks": {
            "guardrail_passed": state["p1_greenfield"]["audit"].get("failed_guardrails") == [],
            "min_independent_oos_days_required": gov_cfg.get("min_independent_oos_days", 60),
            "min_holdout_days_required": gov_cfg.get("min_holdout_days", 60),
            "independent_oos_days_observed": None,
            "holdout_days_observed": None,
            "train_validation_test_split_required": bool(gov_cfg.get("require_train_validation_test_split", True)),
            "test_set_not_used_for_selection_required": bool(gov_cfg.get("require_test_set_not_used_for_selection", True)),
            "qlib_recorder_required": bool(gov_cfg.get("require_qlib_recorder", True)),
        },
        "decision": "keep_research_candidate_promotion_frozen_until_extended_oos_holdout_and_qlib_governance",
        "next_executable_path": [
            "Generate p1_research_candidate_extended_oos.json with explicit independent_oos_days and holdout_days.",
            "Run qlib recorder with qlib_lab/venv python for P1 research candidate artifacts.",
            "Re-run governance only after sample counts and recorder evidence are present.",
        ],
        "hard_rules": [
            "do_not_lift_promotion_freeze_without_governance",
            "do_not_publish_without_user_confirmation",
            "do_not_lower_guardrails",
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--mode", choices=["dry-run", "execute"], default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_yaml(Path(args.config))
    mode = args.mode or ((cfg.get("execution") or {}).get("default_mode") or "dry-run")
    if Path.cwd().resolve() != ROOT.resolve():
        os.chdir(ROOT)

    state = collect_state(cfg)
    decision = decide_route(state, cfg)
    paths = write_outputs(state, decision, cfg, mode)
    governance_path = maybe_write_governance(decision, state, cfg, mode)

    result = {
        "status": "ok",
        "mode": mode,
        "route": decision.route,
        "reason": decision.reason,
        "state": str(paths["state"]),
        "decision": str(paths["decision"]),
        "journal": str(paths["journal"]),
        "governance": str(governance_path) if governance_path else None,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
