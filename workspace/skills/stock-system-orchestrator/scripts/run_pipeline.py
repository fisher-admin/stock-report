#!/usr/bin/env python3
"""Orchestrate the staged stock pipeline with preflight gates."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from orchestrator_common import (
    HEALTH_DIR,
    PREFERRED_PYTHON,
    PUBLISHED_REPO,
    RUNS_DIR,
    WORKSPACE,
    WORKING_REPO,
    git_status,
    load_json,
    now_str,
    now_utc_iso,
    write_json,
)
from stage6_deploy_and_notify import deploy_and_notify, notify_fatal, notify_warning

ENV_DRIFT_PROBE = WORKSPACE / "scripts" / "env_drift_probe.py"
ENV_DRIFT_REPORT = HEALTH_DIR / "env_drift_probe.json"

SCRIPT_DIR = Path(__file__).resolve().parent
SYSTEM_VERDICT_PATH = PUBLISHED_REPO / "data" / "latest" / "system_verdict.json"
RUN_MANIFEST_PATH = PUBLISHED_REPO / "data" / "latest" / "run_manifest.json"
STAGE_HEALTH_PATHS = {
    "dataPreparation": HEALTH_DIR / "data_preparation_run.json",
    "coreSelection": HEALTH_DIR / "core_selection_run.json",
    "strategySuite": HEALTH_DIR / "strategy_suite_run.json",
    "aiEnrichmentAndPublish": HEALTH_DIR / "ai_publish_readiness.json",
    "aiBackfill": HEALTH_DIR / "ai_backfill_loop.json",
    "recommendationWarehouse": HEALTH_DIR / "recommendation_db_sync.json",
    "validation": HEALTH_DIR / "validation_report.json",
    "deployAndNotify": HEALTH_DIR / "stage6_deploy_and_notify.json",
    # 启动前夕二次加工厂状态
    "prebreakoutShadowResearch": HEALTH_DIR / "prebreakout_factory_status.json",
    # 日淘轻量 Top3（跟随主管线，非阻塞）
    "prebreakoutDailyPan": HEALTH_DIR / "prebreakout_daily_pan_status.json",
}
SELF_HEAL_MAX_ATTEMPTS = 3
MARKET_HALT_EXIT = 4
# 业务规则阻断（2026-10-26）：市场门禁判高风险是按设计停手，不是故障；
# 自愈修不了也不该重试，直接以 market_halt 收尾。
POLICY_HALT_CODES = {"MARKET_BLOCKED"}

# 各 stage 的 subprocess 超时（秒）：防止某一阶段挂死拖垮整条管线（无超时=永久阻塞）。
# 未在此列出的 stage 名（如自愈修复组里复用的 stage）落到 DEFAULT_STAGE_TIMEOUT_SECONDS。
STAGE_TIMEOUT_SECONDS: dict[str, int] = {
    # 20261010: 主管线提前到 17:30。cyq_perf 等当日数据可能晚于 17:30 入库，Stage1 内部每 600s 重试，
    # 预算放宽到 4800s（可等到约 18:47），晚到日退化为接近原 19:30 的节奏而不是失败停发。
    "dataPreparation": 4800,
    "strategySuite": 3600,
    "aiEnrichmentAndPublish": 3600,
    # O2C 专用生成 + prebreakout 回填合计常 >30min；默认 1800 会导致 aiBackfill 被 SIGTERM，
    # 留下 O2C 0/20 半成品并拖垮后续 push（20260715 事故）。
    "aiBackfill": 3600,
    # 日淘轻量：约 15–30min；超时则跳过，工厂仍可用昨日配方
    "prebreakoutDailyPan": 2100,
    "prebreakoutShadowResearch": 1200,
}
DEFAULT_STAGE_TIMEOUT_SECONDS = 1800

REMEDIATION_GROUPS: dict[str, list[tuple[str, Path, bool]]] = {
    "market_refresh": [
        ("dataPreparation", SCRIPT_DIR / "stage1_data_prepare.py", False),
        ("marketSnapshot", SCRIPT_DIR / "generate_market_snapshot.py", False),
        ("marketMorningBrief", SCRIPT_DIR / "generate_market_morning_brief.py", False),
        ("marketIndustryHeatmap", SCRIPT_DIR / "generate_market_industry_heatmap.py", False),
        ("unifiedDecisionPayload", SCRIPT_DIR / "generate_unified_decision_payload.py", False),
        ("validation", SCRIPT_DIR / "validate_run.py", True),
        ("latestStateSync", SCRIPT_DIR / "generate_latest_states.py", False),
    ],
    "strategy_publish_refresh": [
        ("strategySuite", SCRIPT_DIR / "stage3_strategy_suite.py", False),
        ("aiEnrichmentAndPublish", SCRIPT_DIR / "stage4_ai_publish.py", False),
        ("recommendationWarehouse", SCRIPT_DIR / "stage5_recommendation_db_sync.py", False),
        ("validation", SCRIPT_DIR / "validate_run.py", True),
        ("latestStateSync", SCRIPT_DIR / "generate_latest_states.py", False),
    ],
    "warehouse_refresh": [
        ("recommendationWarehouse", SCRIPT_DIR / "stage5_recommendation_db_sync.py", False),
        ("validation", SCRIPT_DIR / "validate_run.py", True),
        ("latestStateSync", SCRIPT_DIR / "generate_latest_states.py", False),
    ],
    "validation_refresh": [
        ("validation", SCRIPT_DIR / "validate_run.py", True),
        ("latestStateSync", SCRIPT_DIR / "generate_latest_states.py", False),
    ],
}

BLOCKER_REMEDIATION_MAP = {
    "MISSING_DECISION_TRADE_DATE": ["market_refresh", "strategy_publish_refresh", "validation_refresh"],
    "MISSING_MARKET_TRADE_DATE": ["market_refresh"],
    "MARKET_TRADE_DATE_MISMATCH": ["market_refresh"],
    "MORNING_TRADE_DATE_MISMATCH": ["market_refresh"],
    "MORNING_MARKET_DATA_MISMATCH": ["market_refresh"],
    "MISSING_STRATEGY_TRADE_DATE": ["strategy_publish_refresh"],
    "STRATEGY_TRADE_DATE_MISMATCH": ["strategy_publish_refresh"],
    "CANDIDATE_TRADE_DATE_MISMATCH": ["strategy_publish_refresh"],
    "MISSING_DATA_JSON_TRADE_DATE": ["strategy_publish_refresh"],
    "DATA_JSON_TRADE_DATE_MISMATCH": ["strategy_publish_refresh"],
    "MISSING_RECOMMEND_DATE": ["warehouse_refresh"],
    "RECOMMENDATION_DATE_STALE": ["warehouse_refresh"],
    "MISSING_PRICE_DATE": ["warehouse_refresh"],
    "PRICE_DATE_STALE": ["warehouse_refresh"],
    "VALIDATION_STALE": ["validation_refresh"],
}
REMEDIATION_PRIORITY = ["market_refresh", "strategy_publish_refresh", "warehouse_refresh", "validation_refresh"]

# Three-state status groups (plan v1.4). Deploy failure must not erase selection success.
SELECTION_STAGE_NAMES = {
    "dataPreparation",
    "coreSelection",
    "strategySuite",
    "aiEnrichmentAndPublish",
    "recommendationWarehouse",
    "validation",
    "latestStateSync",
}
RESEARCH_STAGE_NAMES = {
    "marketSnapshot",
    "prebreakoutDailyPan",
    "prebreakoutShadowResearch",
}


def output_text(value: object) -> str:
    """Normalize subprocess output; TimeoutExpired may expose bytes despite text=True."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def mark_pipeline_failed(payload: dict, *, failed_stage: str, failure_category: str) -> dict:
    finished_at = now_utc_iso()
    payload.update(
        {
            "ok": False,
            "status": "failed",
            "failed_stage": failed_stage,
            "failure_category": failure_category,
            "completed_at": finished_at,
            "finished_at": finished_at,
        }
    )
    return payload


def mark_pipeline_succeeded(payload: dict) -> dict:
    finished_at = now_utc_iso()
    payload.update(
        {
            "status": "succeeded",
            "completed_at": finished_at,
            "finished_at": finished_at,
        }
    )
    return payload


def compute_three_state_status(payload: dict) -> dict:
    """Split selection / research / publish success so SSL deploy cannot mask selection.

    Returns selection_ok, research_ok, publish_ok plus a summary ok that is the AND of
    the three (backward compatible). Stages that did not run are ignored for that group
    (except selection required stages that always run when include_core_selection is set).
    """
    stages = {
        str(item.get("stage") or item.get("name") or ""): item
        for item in (payload.get("stages") or [])
        if isinstance(item, dict)
    }

    def _stage_ok(name: str) -> bool | None:
        if name not in stages:
            return None
        return bool(stages[name].get("ok"))

    selection_required = ["dataPreparation", "strategySuite", "validation", "latestStateSync"]
    if payload.get("include_core_selection"):
        selection_required.insert(1, "coreSelection")
    selection_results = [(name, _stage_ok(name)) for name in selection_required]
    # Required selection stages must have run and passed.
    selection_ok = all(result is True for _name, result in selection_results)
    # Optional selection-path stages: if present and failed, selection_ok stays based on required;
    # record them but do not force false unless they are in the required list.
    for name in SELECTION_STAGE_NAMES:
        if name in selection_required:
            continue
        status = _stage_ok(name)
        if status is False and name in {"validation"}:
            selection_ok = False

    research_ran = [name for name in RESEARCH_STAGE_NAMES if name in stages]
    if not research_ran:
        research_ok = True
    else:
        # Research may correctly fail (e.g. proxy chip block). That is research_ok=False,
        # not a selection or publish failure.
        research_ok = all(bool(stages[name].get("ok")) for name in research_ran)

    deploy = payload.get("deploy") or {}
    if "ok" in deploy:
        publish_ok = bool(deploy.get("ok"))
    elif "deployAndNotify" in stages:
        publish_ok = bool(stages["deployAndNotify"].get("ok"))
    else:
        publish_ok = False

    return {
        "selection_ok": bool(selection_ok),
        "research_ok": bool(research_ok),
        "publish_ok": bool(publish_ok),
        "ok": bool(selection_ok and research_ok and publish_ok),
        "selection_stages": {
            name: stages[name].get("ok") if name in stages else None
            for name in sorted(SELECTION_STAGE_NAMES)
        },
        "research_stages": {
            name: stages[name].get("ok") if name in stages else None
            for name in sorted(RESEARCH_STAGE_NAMES)
        },
    }


def build_production_stages(include_core_selection: bool) -> list[tuple[str, Path]]:
    stages: list[tuple[str, Path]] = [("dataPreparation", SCRIPT_DIR / "stage1_data_prepare.py")]
    stages.append(("marketSnapshot", SCRIPT_DIR / "generate_market_snapshot.py"))
    if include_core_selection:
        stages.append(("coreSelection", SCRIPT_DIR / "stage2_core_selection.py"))
    stages.extend(
        [
            ("strategySuite", SCRIPT_DIR / "stage3_strategy_suite.py"),
            ("aiEnrichmentAndPublish", SCRIPT_DIR / "stage4_ai_publish.py"),
            ("aiBackfill", SCRIPT_DIR / "stage4_ai_backfill_loop.py"),
            ("recommendationWarehouse", SCRIPT_DIR / "stage5_recommendation_db_sync.py"),
            ("validation", SCRIPT_DIR / "validate_run.py"),
            ("latestStateSync", SCRIPT_DIR / "generate_latest_states.py"),
        ]
    )
    return stages


def build_stages(include_core_selection: bool) -> list[tuple[str, Path]]:
    return build_production_stages(include_core_selection)


def build_run_env(run_id: str, started_at: str, target_trade_date: str = "") -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_RUN_ID"] = run_id
    env["OPENCLAW_RUN_STARTED_AT"] = started_at
    if target_trade_date:
        env["OPENCLAW_TARGET_TRADE_DATE"] = target_trade_date
    # 管线内嵌的 AI 回填循环限界：网关正常时一遍生成即早退。
    # 20260716: 15min 不够跑完 O2C 20 股专用生成器（常 10–25min）+ prebreakout 补填，
    # 会被主线超时 SIGTERM，留下 O2C 0/20 半成品。放宽到 45min；仍可由环境变量覆盖。
    # 2026-10-03: Stage 1 retries internally (600s interval, default 7200s
    # budget) but this orchestrator kills dataPreparation after its stage
    # timeout (2400s).  Bound Stage 1's own deadline inside the stage timeout
    # so it ends with a durable terminal record instead of being SIGKILLed
    # mid-sleep.  An explicit operator override is still respected.
    stage1_budget = max(STAGE_TIMEOUT_SECONDS.get("dataPreparation", DEFAULT_STAGE_TIMEOUT_SECONDS) - 180, 0)
    current_budget = str(env.get("OPENCLAW_STAGE1_MAX_WAIT_SECONDS") or "").strip()
    if not current_budget.isdigit() or int(current_budget) > stage1_budget:
        env["OPENCLAW_STAGE1_MAX_WAIT_SECONDS"] = str(stage1_budget)
    env.setdefault("OPENCLAW_AI_BACKFILL_MAX_WAIT_SECONDS", "2700")
    env.setdefault("OPENCLAW_AI_BACKFILL_RETRY_INTERVAL_SECONDS", "180")
    env.setdefault("OPENCLAW_AI_BACKFILL_O2C_TIMEOUT_SECONDS", "2400")
    return env


def load_stage_health(stage_name: str) -> dict:
    path = STAGE_HEALTH_PATHS.get(stage_name)
    if not path or not path.exists():
        return {}
    payload = load_json(path)
    return {
        "path": str(path),
        "ok": payload.get("ok"),
        "run": payload.get("run"),
        "trade_date": ((payload.get("run") or {}).get("trade_date")),
        "publish_mode": payload.get("publish_mode"),
        "ai_complete": payload.get("ai_complete"),
        "publish_ready": payload.get("publish_ready"),
        "canonical_objects": payload.get("canonical_objects"),
    }


def load_system_verdict() -> dict:
    return load_json(SYSTEM_VERDICT_PATH) if SYSTEM_VERDICT_PATH.exists() else {}


def verdict_blockers(verdict: dict) -> list[dict]:
    blockers = list(((verdict.get("date_contract") or {}).get("blockers") or []))
    if blockers:
        return blockers
    gates = verdict.get("gates") or {}
    for gate in gates.values():
        gate_blockers = gate.get("blockers") or []
        if gate_blockers:
            blockers.extend(gate_blockers)
    return blockers


def policy_halt_for_verdict(verdict: dict) -> dict | None:
    """Return the market-gate halt when it is the only blocker, else None."""
    codes = {str(item.get("code") or "") for item in verdict_blockers(verdict)}
    if not codes or not codes <= POLICY_HALT_CODES:
        return None
    gate = (verdict.get("gates") or {}).get("market_gate") or {}
    evidence = gate.get("evidence") or {}
    return {
        "codes": sorted(codes),
        "summary": gate.get("summary") or "市场门禁阻断",
        "regime": evidence.get("regime"),
        "risk_score": evidence.get("risk_score"),
    }


def remediation_groups_for_verdict(verdict: dict) -> list[str]:
    groups: list[str] = []
    seen: set[str] = set()
    for blocker in verdict_blockers(verdict):
        code = str(blocker.get("code") or "").strip()
        for group in BLOCKER_REMEDIATION_MAP.get(code, []):
            if group not in seen:
                groups.append(group)
                seen.add(group)
    if not groups and (verdict.get("final_action") or {}).get("action") == "halt":
        groups = ["validation_refresh"]
    groups.sort(key=lambda name: REMEDIATION_PRIORITY.index(name) if name in REMEDIATION_PRIORITY else 999)
    return groups


def run_script(name: str, script: Path, env: dict[str, str], script_args: list[str] | None = None) -> dict:
    python_exec = str(PREFERRED_PYTHON if PREFERRED_PYTHON.exists() else Path(sys.executable))
    timeout_seconds = STAGE_TIMEOUT_SECONDS.get(name, DEFAULT_STAGE_TIMEOUT_SECONDS)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [python_exec, str(script), *(script_args or [])],
            capture_output=True, text=True, env=env, timeout=timeout_seconds,
        )
        result = {
            "stage": name,
            "script": str(script),
            "script_args": script_args or [],
            "python_exec": python_exec,
            "returncode": proc.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": "\n".join(output_text(proc.stdout).splitlines()[-40:]),
            "stderr_tail": "\n".join(output_text(proc.stderr).splitlines()[-40:]),
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        stdout_tail = "\n".join(output_text(exc.stdout).splitlines()[-40:]) if exc.stdout else ""
        stderr_tail = "\n".join(output_text(exc.stderr).splitlines()[-40:]) if exc.stderr else ""
        result = {
            "stage": name,
            "script": str(script),
            "script_args": script_args or [],
            "python_exec": python_exec,
            "returncode": None,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "ok": False,
            "timeout": True,
            "timeout_seconds": timeout_seconds,
        }
    health = load_stage_health(name)
    if health:
        result["health"] = health
    return result


def append_stage_result(payload: dict, result: dict) -> None:
    payload.setdefault("stages", []).append(result)
    health = result.get("health") or {}
    trade_date = health.get("trade_date")
    if trade_date and not payload.get("trade_date"):
        payload["trade_date"] = trade_date


def run_remediation_group(group_name: str, env: dict[str, str], payload: dict, attempt_record: dict) -> bool:
    group_scripts = REMEDIATION_GROUPS.get(group_name, [])
    group_record = {"group": group_name, "scripts": [], "ok": True}
    attempt_record.setdefault("groups", []).append(group_record)
    print(f"[Self-Healing] 执行修复组: {group_name}")
    blocker_codes = set(attempt_record.get("blocker_codes") or [])
    target_trade_date = (
        payload.get("trade_date")
        or str(((load_system_verdict().get("dates") or {}).get("decision_trade_date")) or "")
    )
    for stage_name, script_path, allow_failure in group_scripts:
        script_args: list[str] = []
        if (
            stage_name == "recommendationWarehouse"
            and target_trade_date
            and blocker_codes & {"PRICE_DATE_STALE", "RECOMMENDATION_DATE_STALE", "MISSING_PRICE_DATE", "MISSING_RECOMMEND_DATE"}
        ):
            script_args = ["--target-date", target_trade_date]
        result = run_script(stage_name, script_path, env, script_args=script_args)
        result["self_healing_group"] = group_name
        result["self_healing_allow_failure"] = allow_failure
        append_stage_result(payload, result)
        group_record["scripts"].append({
            "stage": stage_name,
            "script": str(script_path),
            "ok": result.get("ok"),
            "allow_failure": allow_failure,
            "returncode": result.get("returncode"),
        })
        if not result.get("ok") and not allow_failure:
            print(f"[Self-Healing] 修复组 {group_name} 中断：{stage_name} 失败")
            group_record["ok"] = False
            return False
    return True


def maybe_self_heal(run_env: dict[str, str], payload: dict) -> bool:
    healing = payload.setdefault("self_healing", {"attempts": [], "completed": False})
    for attempt in range(1, SELF_HEAL_MAX_ATTEMPTS + 1):
        verdict = load_system_verdict()
        action = ((verdict.get("final_action") or {}).get("action")) or ""
        if action != "halt":
            healing["completed"] = True
            healing["final_action"] = action
            return True

        policy_halt = policy_halt_for_verdict(verdict)
        if policy_halt:
            print(f"[Self-Healing] 市场门禁阻断（{policy_halt['regime']}，风险分 {policy_halt['risk_score']}）属业务规则，不做自愈。")
            healing["policy_halt"] = policy_halt
            healing["completed"] = False
            healing["final_action"] = action
            return False

        blockers = verdict_blockers(verdict)
        blocker_codes = [str(item.get("code") or "UNKNOWN") for item in blockers]
        groups = remediation_groups_for_verdict(verdict)
        attempt_record = {
            "attempt": attempt,
            "final_action_before": action,
            "blocker_codes": blocker_codes,
            "groups": [],
        }
        healing["attempts"].append(attempt_record)

        if not groups:
            print("[Self-Healing] 未找到可执行的修复映射，停止自愈。")
            attempt_record["fatal"] = "no_remediation_mapping"
            break

        print(f"[Self-Healing] 第 {attempt} 次自愈开始，blockers={blocker_codes}")
        for code in blocker_codes:
            print(f"[Self-Healing] 捕获 {code}，准备触发修复。")

        warning_notify = notify_warning(
            run_id=payload.get("run_id") or "",
            trade_date=payload.get("trade_date") or ((verdict.get("dates") or {}).get("decision_trade_date")) or "",
            attempt=attempt,
            blocker_codes=blocker_codes,
            remediation_groups=groups,
        )
        attempt_record["warning_notification"] = warning_notify

        all_groups_ok = True
        for group_name in groups:
            if not run_remediation_group(group_name, run_env, payload, attempt_record):
                all_groups_ok = False
                break

        verdict_after = load_system_verdict()
        attempt_record["final_action_after"] = ((verdict_after.get("final_action") or {}).get("action")) or ""
        attempt_record["date_contract_status_after"] = ((verdict_after.get("date_contract") or {}).get("status")) or ""

        if attempt_record["final_action_after"] != "halt":
            print(f"[Self-Healing] 第 {attempt} 次自愈完成，系统已放行。")
            healing["completed"] = True
            healing["final_action"] = attempt_record["final_action_after"]
            return True

        if not all_groups_ok:
            print(f"[Self-Healing] 第 {attempt} 次自愈未完成，修复脚本执行失败。")
        else:
            print(f"[Self-Healing] 第 {attempt} 次自愈后仍为 halt，继续下一轮。")

    final_verdict = load_system_verdict()
    healing["completed"] = False
    healing["final_action"] = ((final_verdict.get("final_action") or {}).get("action")) or "halt"
    return False


def _validate_run_id_consistency(run_id: str) -> list:
    """Check that completed health files all reference the current run_id."""
    import json
    mismatches = []
    health_files = [
        HEALTH_DIR / "data_preparation_run.json",
        HEALTH_DIR / "strategy_suite_run.json",
        HEALTH_DIR / "ai_publish_readiness.json",
    ]
    for fpath in health_files:
        if fpath.exists():
            try:
                data = json.loads(fpath.read_text())
                file_run_id = (data.get("run") or {}).get("run_id")
                if file_run_id and file_run_id != run_id:
                    mismatches.append(f"{fpath.name}: expected {run_id}, got {file_run_id}")
            except Exception:
                pass
    return mismatches


def run_env_drift_probe(env: dict[str, str]) -> dict:
    if not ENV_DRIFT_PROBE.exists():
        return {"attempted": False, "ok": True, "reason": "probe script missing"}
    python_exec = str(PREFERRED_PYTHON if PREFERRED_PYTHON.exists() else Path(sys.executable))
    proc = subprocess.run([python_exec, str(ENV_DRIFT_PROBE)], capture_output=True, text=True, env=env)
    report = load_json(ENV_DRIFT_REPORT) if ENV_DRIFT_REPORT.exists() else {}
    return {
        "attempted": True,
        "python_exec": python_exec,
        "returncode": proc.returncode,
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-20:]),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-20:]),
        "ok": proc.returncode == 0 and bool(report.get("ok")),
        "report": report,
    }


def reconcile_run_manifest() -> dict:
    """竞态兜底(20260708): run_manifest 的 validation_ok/publish_ready/publish_mode/ai_complete
    由 latestStateSync 快照生成, 但 remediation 补救块可能在最后一次 latestStateSync 之后再跑一遍
    validation(生成更新的 validation_report), 导致 manifest 落后于磁盘最终校验(曾出现 manifest
    validation_ok=false 而磁盘 validation_report=true 的12秒竞态, 误导看板/下游)。
    部署前从磁盘最新 validation_report + ai_publish_readiness 回填这4个派生字段, 保证部署出去的
    manifest 反映最终校验。仅 patch 这几个字段, 幂等、不重建其余状态。"""
    if not RUN_MANIFEST_PATH.exists():
        return {"reconciled": False, "reason": "no_manifest"}
    try:
        manifest = load_json(RUN_MANIFEST_PATH)
    except Exception as exc:  # noqa: BLE001
        return {"reconciled": False, "reason": f"load_failed: {type(exc).__name__}"}
    v_path = STAGE_HEALTH_PATHS["validation"]
    r_path = STAGE_HEALTH_PATHS["aiEnrichmentAndPublish"]
    validation = load_json(v_path) if v_path.exists() else {}
    readiness = load_json(r_path) if r_path.exists() else {}
    changed: dict[str, dict] = {}
    patches = [
        ("validation_ok", validation, "ok", bool),
        ("publish_ready", validation, "publish_ready", bool),
        ("publish_mode", readiness, "publish_mode", lambda v: v),
        ("ai_complete", readiness, "ai_complete", bool),
    ]
    for key, src, field, cast in patches:
        if src and field in src:
            new_val = cast(src[field])
            if manifest.get(key) != new_val:
                changed[key] = {"old": manifest.get(key), "new": new_val}
                manifest[key] = new_val
    if changed:
        manifest["validation_report_generated_at"] = validation.get("generated_at")
        manifest["reconciled_at"] = now_utc_iso()
        write_json(RUN_MANIFEST_PATH, manifest)
    return {"reconciled": bool(changed), "changed": changed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the staged stock orchestrator")
    parser.add_argument(
        "--with-core-selection",
        action="store_true",
        help="Also run stage2 core selection before the retained two-strategy suite",
    )
    parser.add_argument(
        "--trade-date",
        default="",
        help="Target trade date like YYYYMMDD; when provided, Stage 1 and downstream stages reuse this trade date instead of defaulting to now",
    )
    args = parser.parse_args()
    target_trade_date = str(args.trade_date or os.environ.get("OPENCLAW_TARGET_TRADE_DATE") or "").strip()

    # 收盘前裸跑护栏(20260710事故): 工作日15:05收盘确认前不带--trade-date跑管线,
    # stage1会把当日(盘前=昨收快照冒名/盘中=未收盘)标成最新交易日→选股/发布全被污染
    # (当日07:47盘前裸跑已发生: 11个提交把标为0710的盘前/盘中数据推上发布页, 紧急回滚)。
    # 工作日00:00-15:05之间必须显式--trade-date锁定目标日, 否则拒跑。
    _now = datetime.now()
    if not target_trade_date and _now.weekday() < 5 and _now.strftime("%H:%M") < "15:05":
        print(
            "[orchestrator] FATAL: 工作日收盘前(00:00-15:05)禁止不带 --trade-date 裸跑管线——"
            "盘前/盘中数据会冒充当日收盘污染选股与发布。补跑历史请加 --trade-date YYYYMMDD,"
            "当日数据请等15:05后再跑。"
        )
        return 1
    # 收盘后工作日无参跑：锁今天，避免各阶段各自回落到上一个已落盘交易日。
    if not target_trade_date and _now.weekday() < 5:
        target_trade_date = _now.strftime("%Y%m%d")
        print(f"[orchestrator] post-close default trade_date={target_trade_date}")

    run_id = f"stock-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    started_at = now_utc_iso()
    run_env = build_run_env(run_id, started_at, target_trade_date=target_trade_date)

    payload = {
        "generated_at": now_str(),
        "run_id": run_id,
        "started_at": started_at,
        "repo": str(WORKING_REPO),
        "core_selection_policy": {
            "mode": "opt-in",
            "included_this_run": args.with_core_selection,
        },
        "target_trade_date": target_trade_date or None,
        "preflight": {},
        "stages": [],
        "warnings": [],
        "ok": False,
        "status": "running",
        "include_core_selection": args.with_core_selection,
        "artifacts": {
            "latest_manifest": str(HEALTH_DIR / "orchestrator_run.json"),
            "run_manifest": str(RUNS_DIR / f"{run_id}.json"),
        },
    }

    git = git_status(WORKING_REPO)
    payload["preflight"]["git"] = git
    env_probe = run_env_drift_probe(run_env)
    payload["preflight"]["env_drift_probe"] = env_probe
    payload["preflight"]["ok"] = (
        git["behind"] == 0
        and (git["clean"] or git["allowed_output_changes_only"])
        and bool(env_probe.get("ok"))
    )
    if not payload["preflight"]["ok"]:
        reasons: list[str] = []
        if git["behind"] != 0 or not (git["clean"] or git["allowed_output_changes_only"]):
            reasons.append("working repo must not be behind origin; output-only changes are allowed, any other dirty state blocks the run")
        if not env_probe.get("ok"):
            reasons.append("python environment drift probe failed; see preflight.env_drift_probe.report")
        payload["preflight"]["reason"] = " | ".join(reasons)
        payload["fatal_notification"] = notify_fatal(
            run_id=run_id,
            trade_date=target_trade_date or "",
            error_code="PREFLIGHT_FAILED",
            reason=payload["preflight"]["reason"],
            blockers=[],
        )
        mark_pipeline_failed(
            payload,
            failed_stage="preflight",
            failure_category="preflight_failed",
        )
        out = HEALTH_DIR / "orchestrator_run.json"
        write_json(out, payload)
        write_json(RUNS_DIR / f"{run_id}.json", payload)
        print(f"[orchestrator] preflight failed: {out}")
        return 1

    # Stages that are allowed to fail without aborting the pipeline.
    NON_BLOCKING_STAGES = {
        "marketSnapshot",
        "prebreakoutDailyPan",
        "prebreakoutShadowResearch",
        "aiEnrichmentAndPublish",
        "aiBackfill",
        "recommendationWarehouse",
    }

    for stage_name, script_path in build_stages(include_core_selection=args.with_core_selection):
        script_args: list[str] = []
        # 工厂链路：与主管线锁定同一交易日（P0 日期贯通）
        if stage_name in ("prebreakoutDailyPan", "prebreakoutShadowResearch") and target_trade_date:
            script_args = ["--trade-date", target_trade_date]
        result = run_script(stage_name, script_path, run_env, script_args=script_args)
        append_stage_result(payload, result)
        if stage_name == "dataPreparation":
            resolved = str(
                ((result.get("health") or {}).get("trade_date"))
                or payload.get("trade_date")
                or ""
            ).strip()
            if resolved:
                run_env["OPENCLAW_TARGET_TRADE_DATE"] = resolved
                # 2026-10-03: in-process helpers (stage6 publisher env, health
                # snapshots) read os.environ; keep it on the locked batch date.
                os.environ["OPENCLAW_TARGET_TRADE_DATE"] = resolved
                target_trade_date = resolved
                payload["trade_date"] = resolved
                print(f"[orchestrator] locked trade_date={resolved} after stage1")
        if not result["ok"]:
            if stage_name in NON_BLOCKING_STAGES:
                print(f"[orchestrator] WARNING: non-blocking stage {stage_name} failed - continuing")
                payload["warnings"].append(f"{stage_name} failed (non-blocking)")
            else:
                timeout_reason = (
                    f"stage timed out after {result.get('timeout_seconds')}s" if result.get("timeout") else ""
                )
                payload["fatal_notification"] = notify_fatal(
                    run_id=run_id,
                    trade_date=payload.get("trade_date") or target_trade_date or "",
                    error_code=f"STAGE_TIMEOUT_{stage_name}" if result.get("timeout") else f"STAGE_FAILED_{stage_name}",
                    reason=timeout_reason or result.get("stderr_tail") or result.get("stdout_tail") or f"{stage_name} failed",
                    blockers=[],
                    stderr_tail=result.get("stderr_tail") or "",
                )
                mark_pipeline_failed(
                    payload,
                    failed_stage=stage_name,
                    failure_category="stage_timeout" if result.get("timeout") else "stage_failed",
                )
                out = HEALTH_DIR / "orchestrator_run.json"
                write_json(out, payload)
                write_json(RUNS_DIR / f"{run_id}.json", payload)
                print(f"[orchestrator] stage failed: {stage_name}")
                return 1

    healed = maybe_self_heal(run_env, payload)
    if not healed and (payload.get("self_healing") or {}).get("policy_halt"):
        payload["error"] = "MARKET_HALTED"
        mark_pipeline_failed(payload, failed_stage="marketGate", failure_category="market_halt")
        write_json(HEALTH_DIR / "orchestrator_run.json", payload)
        write_json(RUNS_DIR / f"{run_id}.json", payload)
        print("[orchestrator] market gate halt: 市场高风险，按规则暂停今日选股与发布")
        return MARKET_HALT_EXIT
    if not healed:
        payload["error"] = "SELF_HEALING_EXHAUSTED"
        final_verdict = load_system_verdict()
        payload["fatal_notification"] = notify_fatal(
            run_id=run_id,
            trade_date=payload.get("trade_date") or ((final_verdict.get("dates") or {}).get("decision_trade_date")) or "",
            error_code="SELF_HEALING_EXHAUSTED",
            reason="自愈达到上限后 system_verdict 仍为 halt",
            blockers=verdict_blockers(final_verdict),
        )
        mark_pipeline_failed(
            payload,
            failed_stage="selfHealing",
            failure_category="self_healing_exhausted",
        )
        out = HEALTH_DIR / "orchestrator_run.json"
        write_json(out, payload)
        write_json(RUNS_DIR / f"{run_id}.json", payload)
        print("[orchestrator] fatal: self-healing exhausted and system still halted")
        return 1

    # Validate run_id consistency across health files
    _run_id_mismatches = _validate_run_id_consistency(run_id)
    if _run_id_mismatches:
        print(f"[orchestrator] WARNING: run_id mismatches detected: {_run_id_mismatches}")
        payload["warnings"].extend([f"run_id mismatch: {m}" for m in _run_id_mismatches])

    # 竞态兜底(20260708): 部署前从磁盘最新校验回填run_manifest的派生字段, 见reconcile_run_manifest。
    payload["manifest_reconcile"] = reconcile_run_manifest()
    if payload["manifest_reconcile"].get("reconciled"):
        print(f"[orchestrator] run_manifest reconciled: {payload['manifest_reconcile'].get('changed')}")

    deploy_result = deploy_and_notify(
        run_id=run_id,
        trade_date=payload.get("trade_date") or "",
    )
    payload["deploy"] = deploy_result
    three = compute_three_state_status(payload)
    payload["selection_ok"] = three["selection_ok"]
    payload["research_ok"] = three["research_ok"]
    payload["publish_ok"] = three["publish_ok"]
    payload["status_split"] = three
    # Overall ok remains AND of three states; deploy SSL failure no longer implies selection failed.
    payload["ok"] = three["ok"]
    if not deploy_result.get("ok"):
        payload["error"] = "DEPLOY_FAILED"
        mark_pipeline_failed(
            payload,
            failed_stage="deployAndNotify",
            failure_category="deploy_failed",
        )
        out = HEALTH_DIR / "orchestrator_run.json"
        write_json(out, payload)
        write_json(RUNS_DIR / f"{run_id}.json", payload)
        print(
            "[orchestrator] deploy failed: "
            f"selection_ok={payload['selection_ok']} research_ok={payload['research_ok']} "
            f"publish_ok={payload['publish_ok']}"
        )
        return 1

    mark_pipeline_succeeded(payload)
    out = HEALTH_DIR / "orchestrator_run.json"
    write_json(out, payload)
    write_json(RUNS_DIR / f"{run_id}.json", payload)
    print(
        f"[orchestrator] completed: selection_ok={payload['selection_ok']} "
        f"research_ok={payload['research_ok']} publish_ok={payload['publish_ok']} -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
