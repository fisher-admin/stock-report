#!/usr/bin/env python3
"""Run the stock pipeline and emit a deterministic health summary."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_STOCK_ROOT = Path(os.environ.get("STOCK_SYSTEM_ROOT", "/Users/fisher/.openclaw"))
_WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", str(_STOCK_ROOT / "workspace")))
_VENV = Path(os.environ.get("STOCK_SYSTEM_VENV", str(_STOCK_ROOT / "venv")))
_SCRIPTS = _WORKSPACE / "skills/stock-system-orchestrator/scripts"

RUN_PIPELINE = _SCRIPTS / "run_pipeline.py"
STAGE4_AI_PUBLISH = _SCRIPTS / "stage4_ai_publish.py"
STAGE5_RECOMMENDATION_DB_SYNC = _SCRIPTS / "stage5_recommendation_db_sync.py"
VALIDATE_RUN = _SCRIPTS / "validate_run.py"
V2_PUBLISH = _SCRIPTS / "publish_stock_report_v2.sh"
BACKFILL_SCRIPT = _WORKSPACE / "scripts/stock_ai_backfill_incremental.py"
BACKFILL_PYTHON = Path(
    os.environ.get(
        "STOCK_SYSTEM_DAILY_ANALYSIS_PYTHON",
        str(_WORKSPACE / "daily_stock_analysis/venv/bin/python3"),
    )
)
PREFERRED_PYTHON = Path(os.environ.get("STOCK_SYSTEM_PYTHON", str(_VENV / "bin/python")))
HEALTH_DIR = _WORKSPACE / "stock_data/03-working/health"
ORCHESTRATOR_RUN = HEALTH_DIR / "orchestrator_run.json"
AI_PUBLISH = HEALTH_DIR / "ai_publish_readiness.json"
VALIDATION = HEALTH_DIR / "validation_report.json"
RECOMMENDATION_SYNC = HEALTH_DIR / "recommendation_db_sync.json"
TRADE_DATE_RE = re.compile(r"^\d{8}$")
PRE_CLOSE_RERUN_HOUR = int(os.environ.get("OPENCLAW_PRE_CLOSE_RERUN_HOUR", "15"))
MARKET_HALT_EXIT = 4  # run_pipeline.MARKET_HALT_EXIT: market gate halt, not a fault


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def tail(text: str, lines: int = 20) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def run_python(python_exec: str, script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([python_exec, str(script)], capture_output=True, text=True, env=env)


def valid_trade_date(value: str | None) -> str:
    text = str(value or "").strip()
    return text if TRADE_DATE_RE.fullmatch(text) else ""


def pick_latest_recorded_trade_date() -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    docs = [
        ("validation.run.trade_date", load_json(VALIDATION)),
        ("orchestrator.run.trade_date", load_json(ORCHESTRATOR_RUN)),
        ("ai_publish.run.trade_date", load_json(AI_PUBLISH)),
        ("recommendation_sync.run.trade_date", load_json(RECOMMENDATION_SYNC)),
    ]
    for label, doc in docs:
        trade_date = valid_trade_date((doc.get("run") or {}).get("trade_date"))
        if trade_date:
            candidates.append((trade_date, label))
    validation = load_json(VALIDATION)
    checks = validation.get("checks") or {}
    for label, value in [
        ("validation.strategy_latest_trade_date", checks.get("strategy_latest_trade_date")),
        ("validation.latest_common_trade_date", checks.get("latest_common_trade_date")),
    ]:
        trade_date = valid_trade_date(value)
        if trade_date:
            candidates.append((trade_date, label))
    if not candidates:
        return "", ""
    return max(candidates, key=lambda item: item[0])


def resolve_target_trade_date(
    explicit_trade_date: str | None,
    *,
    now_local: datetime | None = None,
) -> tuple[str, str]:
    explicit = valid_trade_date(explicit_trade_date)
    if explicit:
        return explicit, "cli"

    env_trade_date = valid_trade_date(os.environ.get("OPENCLAW_TARGET_TRADE_DATE"))
    if env_trade_date:
        return env_trade_date, "env"

    current = now_local or datetime.now()
    today = current.strftime("%Y%m%d")
    latest_recorded, source = pick_latest_recorded_trade_date()
    # 收盘前：禁止默认成“今天”，避免盘前/盘中快照冒充当日收盘。
    if current.hour < PRE_CLOSE_RERUN_HOUR:
        if latest_recorded and latest_recorded < today:
            return latest_recorded, f"auto-pre-close-rerun:{source}"
        return "", ""
    # 收盘后工作日：必须锁今天，禁止无参跑滑回上一个已落盘交易日。
    if current.weekday() < 5:
        return today, "auto-post-close-today"
    return "", ""


def check_auto_session(trade_date: str) -> dict | None:
    """Return a refusal report unless ``trade_date`` is an open exchange day."""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import exchange_calendar  # noqa: PLC0415 - stdlib-only module beside the orchestrator

    decision = exchange_calendar.classify_date(trade_date)
    if decision.get("status") == "open":
        return None
    return {
        "ok": False,
        "pipeline_returncode": None,
        "requested_trade_date": trade_date,
        "session_status": decision.get("status"),
        "reason": decision.get("reason") or "automatically inferred date is not an open exchange session",
        "failure_codes": ["SESSION_NOT_OPEN" if decision.get("status") == "closed" else "CALENDAR_UNAVAILABLE"],
        "primary_failure_code": "SESSION_NOT_OPEN" if decision.get("status") == "closed" else "CALENDAR_UNAVAILABLE",
    }


def same_run(doc: dict, run_id: str) -> bool:
    return bool(run_id) and str(((doc.get("run") or {}).get("run_id")) or "") == run_id


def first_failed_stage(orchestrator: dict) -> dict:
    for stage in orchestrator.get("stages") or []:
        if not stage.get("ok"):
            return stage
    return {}


def preflight_causes(orchestrator: dict) -> list[str]:
    """Name the concrete preflight failure instead of a generic deploy error."""
    preflight = orchestrator.get("preflight") or {}
    if preflight.get("ok") is not False:
        return []
    causes = [f"preflight: {preflight.get('reason') or 'preflight failed'}"]
    report = (preflight.get("env_drift_probe") or {}).get("report") or {}
    for name, env in sorted((report.get("locked_environments") or {}).items()):
        if not env.get("ok"):
            causes.append(f"locked_env {name}: {env.get('error') or 'probe failed'} ({env.get('path')})")
    for name, module in sorted((report.get("modules") or {}).items()):
        if not module.get("ok"):
            causes.append(f"module {name}: {module.get('error') or 'import failed'}")
    return causes


def collect_root_causes(
    orchestrator: dict,
    ai_publish: dict,
    validation: dict,
    recommendation_sync: dict,
    current_run_id: str,
) -> list[str]:
    causes: list[str] = preflight_causes(orchestrator)
    failed_stage = first_failed_stage(orchestrator)
    if failed_stage:
        causes.append(f"stage_failed: {failed_stage.get('stage')}")
        stage_stderr = tail(str(failed_stage.get("stderr_tail") or ""))
        if stage_stderr:
            causes.append(f"stage_error: {stage_stderr}")

    if same_run(ai_publish, current_run_id):
        publish_error = str((ai_publish.get("publish") or {}).get("error") or "")
        if publish_error:
            causes.append(f"publish: {publish_error}")

        pull_stderr = str((ai_publish.get("publish") or {}).get("git_pull_stderr_tail") or "")
        if pull_stderr:
            causes.append(f"git_pull: {pull_stderr}")

    if same_run(recommendation_sync, current_run_id):
        recommendation_error = str((recommendation_sync.get("published_repo_sync") or {}).get("error") or recommendation_sync.get("error") or "")
        if recommendation_error:
            causes.append(f"recommendation_db: {recommendation_error}")

    if same_run(validation, current_run_id):
        validation_checks = validation.get("checks") or {}
        if not validation.get("ok"):
            if not (validation_checks.get("analytics_dates") or {}).get("all_match", True):
                causes.append("validation: analytics dates mismatch")
            if not (validation_checks.get("recommendation_db") or {}).get("ok", True):
                causes.append("validation: recommendation warehouse sync failed")
            if not (validation_checks.get("ai_publish") or {}).get("published", True):
                causes.append("validation: publish not completed")
            if not (validation_checks.get("ai_publish") or {}).get("ai_complete", True):
                causes.append("validation: ai incomplete")
            if not (validation_checks.get("data_preparation") or {}).get("date_aligned_with_strategy", True):
                causes.append("validation: data preparation date mismatch")

    if orchestrator and orchestrator.get("warnings"):
        causes.extend([f"warning: {item}" for item in orchestrator.get("warnings") or []])

    if orchestrator and not bool(orchestrator.get("publish_ok")):
        deploy_result = orchestrator.get("deploy") or {}
        publisher = deploy_result.get("deploy") or {}
        deploy_error = str(
            publisher.get("error")
            or deploy_result.get("error")
            or orchestrator.get("error")
            or ""
        )
        # A failed preflight never reaches deploy; the generic fallback would
        # hide the real cause behind "remote publication did not complete".
        if deploy_error or not causes:
            causes.append(f"deploy: {deploy_error or 'remote publication did not complete'}")

    deduped: list[str] = []
    for item in causes:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def classify_failure_codes(report: dict, ai_publish: dict, validation: dict, recommendation_sync: dict) -> list[str]:
    codes: list[str] = []
    text = "\n".join(report.get("root_causes") or [])
    low = text.lower()

    if any(line.startswith(("locked_env ", "module ")) for line in low.splitlines()):
        codes.append("ENVIRONMENT_DRIFT")
    if "preflight:" in low:
        codes.append("PREFLIGHT_FAILED")
    if "analytics dates mismatch" in low:
        codes.append("ANALYTICS_DATE_MISMATCH")
    if "ai incomplete" in low:
        codes.append("AI_INCOMPLETE")
    if "publish not completed" in low or not report.get("published"):
        codes.append("PUBLISH_BLOCKED")
    if "recommendation warehouse sync failed" in low or not ((recommendation_sync or {}).get("ok") is True):
        codes.append("RECOMMENDATION_DB_FAILED")
    if "data preparation date mismatch" in low:
        codes.append("DATA_PREPARATION_DATE_MISMATCH")
    if "git_pull:" in low or "ssl" in low or "libressl" in low:
        codes.append("GIT_NETWORK_FAILURE")
    if "modulenotfounderror" in low or "no module named" in low or "python" in low and "site-packages" in low:
        codes.append("ENVIRONMENT_DRIFT")
    if report.get("pipeline_returncode") not in (0, None) and not codes:
        codes.append("PIPELINE_STAGE_FAILED")
    if report.get("ok") and not codes:
        codes.append("OK")

    deduped: list[str] = []
    for code in codes:
        if code not in deduped:
            deduped.append(code)
    return deduped


def needs_incremental_backfill(report: dict, ai_publish: dict, current_run_id: str) -> bool:
    progress = report.get("ai_progress") or {}
    pending = int(progress.get("pending") or 0)
    if pending <= 0:
        return False
    if not same_run(ai_publish, current_run_id):
        return False
    checks = ai_publish.get("checks") or {}
    if str(checks.get("strategy_latest_trade_date") or "") != str(report.get("trade_date") or ""):
        return False
    if not report.get("published"):
        return True
    return not bool(report.get("ai_complete"))


def run_incremental_backfill(orchestrator: dict, report: dict) -> dict:
    run_id = str(report.get("run_id") or "")
    started_at = str(orchestrator.get("started_at") or "")
    trade_date = str(report.get("trade_date") or "")
    env = os.environ.copy()
    if run_id:
        env["OPENCLAW_RUN_ID"] = run_id
    if started_at:
        env["OPENCLAW_RUN_STARTED_AT"] = started_at
    if trade_date:
        env["OPENCLAW_TARGET_TRADE_DATE"] = trade_date

    backfill_python = str(BACKFILL_PYTHON if BACKFILL_PYTHON.exists() else Path(sys.executable))
    orchestrator_python = str(PREFERRED_PYTHON if PREFERRED_PYTHON.exists() else Path(sys.executable))
    backfill_proc = run_python(backfill_python, BACKFILL_SCRIPT, env)
    stage4_proc = run_python(orchestrator_python, STAGE4_AI_PUBLISH, env)
    stage5_proc = run_python(orchestrator_python, STAGE5_RECOMMENDATION_DB_SYNC, env)
    validate_proc = run_python(orchestrator_python, VALIDATE_RUN, env)
    payload = {
        "attempted": True,
        "backfill_python_exec": backfill_python,
        "orchestrator_python_exec": orchestrator_python,
        "backfill_returncode": backfill_proc.returncode,
        "backfill_stdout_tail": tail(backfill_proc.stdout, lines=40),
        "backfill_stderr_tail": tail(backfill_proc.stderr, lines=40),
        "stage4_returncode": stage4_proc.returncode,
        "stage4_stdout_tail": tail(stage4_proc.stdout, lines=20),
        "stage4_stderr_tail": tail(stage4_proc.stderr, lines=20),
        "stage5_returncode": stage5_proc.returncode,
        "stage5_stdout_tail": tail(stage5_proc.stdout, lines=20),
        "stage5_stderr_tail": tail(stage5_proc.stderr, lines=20),
        "validate_returncode": validate_proc.returncode,
        "validate_stdout_tail": tail(validate_proc.stdout, lines=20),
        "validate_stderr_tail": tail(validate_proc.stderr, lines=20),
        "publisher": "publish_stock_report_v2.sh",
        "publisher_returncode": None,
        "publisher_status": "not_attempted",
        "publisher_stdout_tail": "",
        "publisher_stderr_tail": "",
    }
    stages_ok = (
        backfill_proc.returncode == 0
        and stage4_proc.returncode == 0
        and stage5_proc.returncode == 0
        and validate_proc.returncode == 0
    )
    validation = load_json(VALIDATION)
    publish_ready = bool(validation.get("publish_ready"))
    if not stages_ok:
        payload["publisher_status"] = "skipped_upstream_failure"
        payload["publisher_stderr_tail"] = "v2 publisher skipped: backfill/stage4/stage5/validate failed"
    elif not publish_ready:
        payload["publisher_status"] = "skipped_not_ready"
        payload["publisher_stderr_tail"] = (
            "v2 publisher skipped: validation.publish_ready=false; "
            "the current artifact remains local and the last valid public artifact is preserved"
        )
    elif not V2_PUBLISH.exists():
        payload["publisher_status"] = "missing"
        payload["publisher_stderr_tail"] = f"v2 publisher missing: {V2_PUBLISH}"
    else:
        try:
            v2_proc = subprocess.run(
                ["bash", str(V2_PUBLISH)],
                cwd=str(_WORKSPACE),
                capture_output=True,
                text=True,
                env=env,
                timeout=300,
            )
            payload["publisher_returncode"] = v2_proc.returncode
            payload["publisher_status"] = "succeeded" if v2_proc.returncode == 0 else "failed"
            payload["publisher_stdout_tail"] = tail(v2_proc.stdout, lines=20)
            payload["publisher_stderr_tail"] = tail(v2_proc.stderr, lines=20)
        except subprocess.TimeoutExpired:
            payload["publisher_returncode"] = -1
            payload["publisher_status"] = "timeout"
            payload["publisher_stderr_tail"] = "v2 publisher timed out"
    return payload


def market_halt(orchestrator: dict) -> dict | None:
    if orchestrator.get("failure_category") != "market_halt":
        return None
    halt = dict((orchestrator.get("self_healing") or {}).get("policy_halt") or {})
    regime = halt.get("regime") or "高风险"
    risk = halt.get("risk_score")
    risk_text = f"，风险分 {risk}" if risk is not None else ""
    halt["text"] = f"市场{regime}{risk_text}，按规则暂停今日选股与发布（非系统故障）"
    return halt


def build_report(
    proc: subprocess.CompletedProcess[str],
    orchestrator: dict,
    ai_publish: dict,
    validation: dict,
    recommendation_sync: dict,
    requested_trade_date: str = "",
    requested_trade_date_source: str = "",
) -> tuple[dict, str]:
    current_run_id = str(orchestrator.get("run_id") or ((orchestrator.get("run") or {}).get("run_id")) or "")
    ai_publish_current = same_run(ai_publish, current_run_id)
    validation_current = same_run(validation, current_run_id)
    validation_ok = bool(validation.get("ok")) if validation_current else False
    selection_ok = bool(orchestrator.get("selection_ok", validation_ok))
    research_ok = bool(orchestrator.get("research_ok", True))
    publish_ok = bool(
        orchestrator.get(
            "publish_ok",
            (orchestrator.get("deploy") or {}).get("ok", False),
        )
    )
    pipeline_process_ok = proc.returncode == 0
    ai_status_counts = ((ai_publish.get("checks") or {}).get("ai_status_counts") or {}) if ai_publish_current else {}
    explicit_label_coverage = (ai_publish.get("checks") or {}).get("explicit_label_coverage") if ai_publish_current else None
    report = {
        "ok": bool(pipeline_process_ok and validation_ok and selection_ok and publish_ok),
        "pipeline_returncode": proc.returncode,
        "pipeline_process_ok": pipeline_process_ok,
        "run_id": current_run_id,
        "trade_date": str(
            (orchestrator.get("run") or {}).get("trade_date")
            or (validation.get("run") or {}).get("trade_date")
            or (ai_publish.get("run") or {}).get("trade_date")
            or requested_trade_date
            or ""
        ),
        "requested_trade_date": requested_trade_date,
        "requested_trade_date_source": requested_trade_date_source,
        "orchestrator_ok": bool(orchestrator.get("ok")),
        "selection_ok": selection_ok,
        "research_ok": research_ok,
        "publish_ok": publish_ok,
        "validation_ok": validation_ok,
        "publish_ready": bool(validation.get("publish_ready")) if validation_current else False,
        "published": publish_ok,
        "artifacts_published": bool(ai_publish.get("published")) if ai_publish_current else False,
        "publish_mode": str(ai_publish.get("publish_mode") or "") if ai_publish_current else "",
        "ai_complete": bool(validation.get("ai_complete")) if validation_current else False,
        "ai_progress": {
            "ready": ai_status_counts.get("ready"),
            "pending": ai_status_counts.get("pending"),
            "total": ai_status_counts.get("total"),
            "explicit_label_coverage": explicit_label_coverage,
        },
        "stale_health": {
            "ai_publish": not ai_publish_current,
            "validation": not validation_current,
            "recommendation_sync": not same_run(recommendation_sync, current_run_id),
        },
        "root_causes": collect_root_causes(orchestrator, ai_publish, validation, recommendation_sync, current_run_id),
        "stdout_tail": tail(proc.stdout),
        "stderr_tail": tail(proc.stderr),
    }
    report["failure_codes"] = classify_failure_codes(report, ai_publish, validation, recommendation_sync)
    halt = market_halt(orchestrator)
    if halt:
        # A market-gate halt is a business rule, not a fault: never report it
        # as PUBLISH_BLOCKED / SELF_HEALING_EXHAUSTED.
        report["market_halt"] = halt
        report["root_causes"] = [f"market_gate: {halt['text']}"]
        report["failure_codes"] = ["MARKET_HALTED"]
    report["primary_failure_code"] = next((code for code in report["failure_codes"] if code != "OK"), report["failure_codes"][0] if report["failure_codes"] else "UNKNOWN")
    return report, current_run_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the stock pipeline gate with deterministic health summary")
    parser.add_argument("--trade-date", help="Target trade date like YYYYMMDD; before market close, omitted value auto-falls back to the latest recorded trade date")
    args = parser.parse_args()

    requested_trade_date, requested_trade_date_source = resolve_target_trade_date(args.trade_date)
    # 2026-10-03: an automatically inferred date must be a confirmed open
    # session.  A closed day or an unusable calendar never starts a new batch
    # (explicit --trade-date replays of past sessions are still allowed).
    if requested_trade_date and requested_trade_date_source.startswith("auto"):
        session_check = check_auto_session(requested_trade_date)
        if session_check is not None:
            print(json.dumps(session_check, ensure_ascii=False, indent=2))
            return 3
    env = os.environ.copy()
    if requested_trade_date:
        env["OPENCLAW_TARGET_TRADE_DATE"] = requested_trade_date

    python_exec = str(PREFERRED_PYTHON if PREFERRED_PYTHON.exists() else Path(sys.executable))
    pipeline_cmd = [python_exec, str(RUN_PIPELINE)]
    if requested_trade_date:
        pipeline_cmd.extend(["--trade-date", requested_trade_date])
    proc = subprocess.run(pipeline_cmd, capture_output=True, text=True, env=env)
    orchestrator = load_json(ORCHESTRATOR_RUN)
    ai_publish = load_json(AI_PUBLISH)
    validation = load_json(VALIDATION)
    recommendation_sync = load_json(RECOMMENDATION_SYNC)
    report, current_run_id = build_report(
        proc,
        orchestrator,
        ai_publish,
        validation,
        recommendation_sync,
        requested_trade_date=requested_trade_date,
        requested_trade_date_source=requested_trade_date_source,
    )

    if report.get("market_halt"):
        print(f"MARKET_HALT {report['market_halt']['text']}")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return MARKET_HALT_EXIT

    if needs_incremental_backfill(report, ai_publish, current_run_id):
        report["auto_backfill"] = run_incremental_backfill(orchestrator, report)
        ai_publish = load_json(AI_PUBLISH)
        validation = load_json(VALIDATION)
        recommendation_sync = load_json(RECOMMENDATION_SYNC)
        refreshed_report, _ = build_report(
            proc,
            orchestrator,
            ai_publish,
            validation,
            recommendation_sync,
            requested_trade_date=requested_trade_date,
            requested_trade_date_source=requested_trade_date_source,
        )
        refreshed_report["auto_backfill"] = report["auto_backfill"]
        report = refreshed_report

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
