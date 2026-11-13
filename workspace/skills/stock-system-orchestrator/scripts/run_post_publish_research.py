#!/usr/bin/env python3
"""Run post-publish research pipeline.

Executes attached research stages (portfolio evaluation, factor attribution, daily pan, factory search)
after the matching production release has been confirmed. Failures here update research_state but
do not compromise or alter the published production ranking.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from managed_process import run_managed
from research_alert import notify_failure, retry_failures, EXISTING_STOCK_TARGET

from orchestrator_common import (
    HEALTH_DIR,
    PREFERRED_PYTHON,
    PUBLISHED_REPO,
    now_str,
    write_json,
)
from production_release_gate import (
    parse_duration,
    pipeline_lock_active,
    resolve_expected_trade_date,
    wait_for_release,
)

SCRIPT_DIR = Path(__file__).resolve().parent

POST_PUBLISH_RESEARCH_STAGES: list[tuple[str, Path]] = [
    ("shortTrackGeneration", SCRIPT_DIR / "short_track_shadow_runner.py"),
    ("factorAttribution", SCRIPT_DIR / "factor_attribution_report.py"),
    ("prebreakoutDailyPan", SCRIPT_DIR / "prebreakout_factory_search_daily.py"),
    ("prebreakoutShadowResearch", SCRIPT_DIR / "prebreakout_factory.py"),
]
POST_STAGE_TIMEOUT_SECONDS = {
    # This stage calls external market-data APIs. It is optional and must never
    # consume the production window again.
    # 20261009 (user-approved): 600s killed the stage on 0930/1009 (manual 1009 run took 1180s);
    # 1800s still fits the 7200s Hermes cron budget alongside the later stages (~1700s measured).
    "shortTrackGeneration": 1800,
    "factorAttribution": 1800,
    "prebreakoutDailyPan": 2400,
    "prebreakoutShadowResearch": 3600,
}
TRADING_CALENDAR = HEALTH_DIR / "trading_calendar.json"


def effective_script_timeout(env=None, config_path=None) -> int:
    """Mirror Hermes env/config/default precedence without importing the scheduler."""
    env = os.environ if env is None else env
    def positive(value):
        try:
            seconds = int(float(value))
            return seconds if seconds > 0 else None
        except (ValueError, TypeError, OverflowError):
            return None
    override = positive(env.get("HERMES_CRON_SCRIPT_TIMEOUT"))
    if override is not None:
        return override
    config_path = config_path or Path(env.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "config.yaml"
    try:
        import yaml
        config = yaml.safe_load(Path(config_path).read_text()) or {}
        return positive(config.get("cron", {}).get("script_timeout_seconds")) or 3600
    except ImportError:
        return 3600
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return 3600
    return 3600


class ResearchDeadline:
    """One monotonic budget, including release waiting; never expand stage limits."""
    def __init__(self, outer_seconds: int, *, clock=time.monotonic):
        if outer_seconds <= 600:
            raise ValueError("outer timeout must exceed the 600-second safety reserve")
        self.clock = clock
        now = clock()
        self.started = float(os.environ.get('STOCK_RESEARCH_STARTED_MONOTONIC', now))
        if not math.isfinite(self.started) or not 0 <= self.started <= now:
            raise ValueError('invalid wrapper monotonic start')
        self.work_deadline = self.started + outer_seconds - 600
        self.finish_deadline = self.started + outer_seconds - 300

    def remaining(self) -> float:
        return max(0, self.work_deadline - self.clock())

    def stage_timeout(self, cap: float) -> int:
        return max(0, int(min(cap, self.remaining())))

    def notification_timeout(self) -> float:
        return max(0, min(20, self.finish_deadline - self.clock() - 2))


def release_paths() -> dict[str, Path]:
    return {
        "manifest": PUBLISHED_REPO / "data/latest/run_manifest.json",
        "readiness": HEALTH_DIR / "ai_publish_readiness.json",
        "validation": HEALTH_DIR / "validation_report.json",
        "orchestrator": HEALTH_DIR / "orchestrator_run.json",
        "receipt": HEALTH_DIR / "deployment_receipt.json",
        "lock_dir": HEALTH_DIR / "stock_pipeline_gate.lockdir",
    }


def _output_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def mark_factory_timeout(trade_date: str, run_id: str, result: dict) -> None:
    """Turn a killed factory's lingering running marker into an explicit timeout."""
    if not str(result.get("script") or "").endswith("prebreakout_factory.py"):
        return
    status_path = HEALTH_DIR / "prebreakout_factory_status.json"
    current = {}
    if status_path.is_file():
        try:
            current = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            current = {}
    if current.get("state") != "running":
        return
    payload = dict(current)
    payload.update(
        {
            "ok": False,
            "state": "timeout",
            "run_id": str(current.get("run_id") or run_id),
            "trade_date": str(current.get("trade_date") or trade_date),
            "finished_at": now_str(),
            "error": f"subprocess timeout after {result.get('timeout_seconds', 'unknown')}s",
        }
    )
    write_json(status_path, payload)


def run_stage(
    name: str,
    script: Path,
    env: dict[str, str],
    script_args: list[str] | None = None,
    timeout_s: int | None = None,
) -> dict:
    python_exec = str(PREFERRED_PYTHON if PREFERRED_PYTHON.exists() else Path(sys.executable))
    timeout_seconds = int(POST_STAGE_TIMEOUT_SECONDS.get(name, 1800) if timeout_s is None else timeout_s)
    if timeout_seconds <= 0:
        return {"stage": name, "script": str(script), "ok": False,
                "returncode": None, "error": "total_budget_exhausted", "timeout_seconds": 0}
    started = time.monotonic()
    try:
        proc = run_managed(
            [python_exec, str(script), *(script_args or [])],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
        return {
            "stage": name,
            "script": str(script),
            "returncode": proc.returncode,
            "ok": proc.returncode == 0,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "timeout_seconds": timeout_seconds,
            "stdout_tail": "\n".join(_output_text(proc.stdout).splitlines()[-40:]),
            "stderr_tail": "\n".join(_output_text(proc.stderr).splitlines()[-40:]),
        }
    except subprocess.TimeoutExpired as exc:
        stdout_text = _output_text(exc.stdout)
        stderr_text = _output_text(exc.stderr)
        return {
            "stage": name,
            "script": str(script),
            "returncode": None,
            "ok": False,
            "timeout": True,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "timeout_seconds": timeout_seconds,
            "stdout_tail": "\n".join(stdout_text.splitlines()[-40:]) if stdout_text else "",
            "stderr_tail": "\n".join(stderr_text.splitlines()[-40:]) if stderr_text else "",
        }


    except (RuntimeError, OSError, ValueError) as exc:
        # No further job may write while termination of this job is uncertain.
        return {'stage': name, 'script': str(script), 'ok': False,
                'returncode': None, 'cleanup_unconfirmed': True,
                'error': f'managed_process_failure:{type(exc).__name__}',
                'elapsed_seconds': round(time.monotonic() - started, 3),
                'timeout_seconds': timeout_seconds}


def stage_arguments(name: str, trade_date: str) -> list[str]:
    if name == "factorAttribution":
        # Refresh recent panels, but preserve full-history attribution statistics.
        return ["--as-of", trade_date, "--refresh-days", "1"]
    return ["--trade-date", trade_date]


def configured_alert_target():
    target = None
    if os.environ.get('STOCK_RESEARCH_ALERTS') == '1':
        jobs_path = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'cron/jobs.json'
        try:
            jobs = json.loads(jobs_path.read_text()).get('jobs', [])
            if any(job.get('script') == 'stock/post_publish_research.sh' and job.get('deliver') == EXISTING_STOCK_TARGET for job in jobs):
                target = EXISTING_STOCK_TARGET
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    return target


def record_failure_alert(trade_date: str, run_id: str, result: dict, *, timeout_s=20) -> dict:
    try:
        return notify_failure(root=HEALTH_DIR / 'research_alerts', run_id=run_id,
                       trade_date=trade_date, stage=result['stage'],
                       error_kind='timeout' if result.get('timeout') else 'stage_failed',
                       target=configured_alert_target(), timeout_s=timeout_s)
    except (OSError, ValueError, TypeError, KeyError):
        # Original stage failure remains in the already persisted summary.
        print('[post-publish-research] Failure alert could not be recorded; original failure retained')
        return {'status': 'unknown'}


def archive_previous_summary(out_file: Path) -> None:
    """Keep prior terminal evidence before replacing the latest-status pointer."""
    if out_file.is_file():
        previous_bytes = out_file.read_bytes()
        previous = json.loads(previous_bytes)
        archive = HEALTH_DIR / "research_run_archive" / (hashlib.sha256(previous_bytes).hexdigest() + ".json")
        if not archive.exists():
            write_json(archive, previous)


def main() -> int:
    try:
        budget = ResearchDeadline(effective_script_timeout())
    except ValueError as exc:
        write_json(HEALTH_DIR / "post_publish_research_run.json", {
            "state": "blocked", "all_ok": False, "stages": [],
            "finished_at": now_str(), "error": str(exc),
        })
        print(f"[post-publish-research] Invalid time budget: {exc}")
        return 1
    parser = argparse.ArgumentParser(description="Run post-publish stock research pipeline")
    parser.add_argument("--trade-date", default="", help="Trade date YYYYMMDD")
    args = parser.parse_args()

    requested_trade_date = str(args.trade_date or "").strip()
    expected_trade_date = resolve_expected_trade_date(
        TRADING_CALENDAR,
        explicit=requested_trade_date,
        env=os.environ,
    )
    raw_gate_timeout = os.environ.get("OPENCLAW_PRODUCTION_RELEASE_TIMEOUT_SECONDS", "3600")
    raw_gate_interval = os.environ.get("OPENCLAW_PRODUCTION_RELEASE_INTERVAL_SECONDS", "2")
    if not expected_trade_date:
        release = None
        gate_errors = ["无法确定本次应消费的交易日"]
    else:
        release, gate_errors = wait_for_release(
            release_paths(),
            expected_trade_date=expected_trade_date,
            timeout_s=min(budget.remaining(), parse_duration(raw_gate_timeout, default=3600, minimum=0, maximum=7200)),
            interval_s=parse_duration(raw_gate_interval, default=2, minimum=0.01, maximum=30),
            lock_fn=pipeline_lock_active,
        )
    if release is None:
        archive_previous_summary(HEALTH_DIR / "post_publish_research_run.json")
        summary = {
            "generated_at": now_str(),
            "trade_date": requested_trade_date,
            "run_id": "",
            "state": "blocked",
            "finished_at": now_str(),
            "stages": [],
            "all_ok": False,
            "release_gate_errors": gate_errors,
        }
        out_file = HEALTH_DIR / "post_publish_research_run.json"
        write_json(out_file, summary)
        # Independent research invocation identity, never a fabricated
        # successful production run or a replacement for its original failure.
        event = record_failure_alert(expected_trade_date or 'unknown',
                                     'research-gate-' + uuid.uuid4().hex,
                                     {'stage': 'releaseGate'}, timeout_s=budget.notification_timeout())
        retry_failures([event], root=HEALTH_DIR / 'research_alerts',
                       target=configured_alert_target(), deadline=budget.finish_deadline)
        print(f"[post-publish-research] Release gate blocked research: {gate_errors[-1] if gate_errors else 'not ready'}")
        print(f"[post-publish-research] Summary written to {out_file}")
        return 1

    trade_date = str(release["trade_date"])
    run_id = str(release["run_id"])
    env = os.environ.copy()
    env["OPENCLAW_TARGET_TRADE_DATE"] = trade_date
    env["OPENCLAW_RUN_ID"] = run_id
    env["PYTHONUNBUFFERED"] = "1"
    started_at = str((release["orchestrator"] or {}).get("started_at") or "")
    if started_at:
        env["OPENCLAW_RUN_STARTED_AT"] = started_at

    summary_started_at = now_str()
    out_file = HEALTH_DIR / "post_publish_research_run.json"
    archive_previous_summary(out_file)
    recovery = os.environ.get("STOCK_RESEARCH_RECOVERY") == "1"
    research_identity = {
        "research_run_id": "research-" + uuid.uuid4().hex,
        "source_run_id": run_id,
        "execution_mode": "manual_recovery" if recovery else "scheduled",
        "input_temporality": "historical_reconstruction" if recovery else "scheduled_acquisition",
    }
    env["STOCK_RESEARCH_RUN_ID"] = research_identity["research_run_id"]
    results = []
    alerts = []
    has_error = False
    cleanup_unconfirmed = False

    def persist_progress(current_stage: str | None) -> None:
        write_json(out_file, {
            **research_identity,
            "generated_at": now_str(),
            "started_at": summary_started_at,
            "trade_date": trade_date,
            "run_id": run_id,
            "state": "running",
            "current_stage": current_stage,
            "stages": list(results),
            "all_ok": False if has_error else None,
        })

    persist_progress(None)
    display_date = trade_date
    print(f"[post-publish-research] Starting post-publish research stages (trade_date={display_date})...")
    for stage_name, script_path in POST_PUBLISH_RESEARCH_STAGES:
        if cleanup_unconfirmed:
            results.append({'stage': stage_name, 'script': str(script_path), 'ok': False,
                            'error': 'prior_cleanup_unconfirmed', 'not_executed': True})
            persist_progress(None)
            continue
        persist_progress(stage_name)
        if not script_path.exists():
            print(f"[post-publish-research] Stage {stage_name} script not found: {script_path}")
            results.append({"stage": stage_name, "script": str(script_path), "ok": False, "error": "script_not_found"})
            has_error = True
            persist_progress(None)
            alerts.append(record_failure_alert(trade_date, run_id, results[-1], timeout_s=budget.notification_timeout()))
            continue
        print(f"[post-publish-research] Running {stage_name}...")
        script_args = stage_arguments(stage_name, trade_date)
        res = run_stage(stage_name, script_path, env, script_args, budget.stage_timeout(POST_STAGE_TIMEOUT_SECONDS.get(stage_name, 1800)))
        results.append(res)
        cleanup_unconfirmed = res.get('cleanup_unconfirmed') is True
        if not res["ok"]:
            print(f"[post-publish-research] WARNING: {stage_name} failed (non-blocking for production)")
            has_error = True
            if res.get("timeout"):
                mark_factory_timeout(trade_date, run_id, res)
        persist_progress(None)

        if not res['ok']:
            alerts.append(record_failure_alert(trade_date, run_id, res, timeout_s=budget.notification_timeout()))

    summary = {
        **research_identity,
        "generated_at": now_str(),
        "started_at": summary_started_at,
        "finished_at": now_str(),
        "trade_date": trade_date,
        "run_id": run_id,
        "state": "failed" if has_error else "completed",
        "stages": results,
        "all_ok": not has_error,
    }
    write_json(out_file, summary)
    if alerts:
        retry_failures(alerts, root=HEALTH_DIR / 'research_alerts',
                       target=configured_alert_target(), deadline=budget.finish_deadline)
    for failed in (result for result in results if not result.get("ok")):
        reason = failed.get("stderr_tail") or failed.get("stdout_tail") or failed.get("error") or "stage failed"
        if failed.get("timeout"):
            reason = f"timed out after {failed.get('timeout_seconds')} seconds"
        print(f"[post-publish-research] FAILED {failed['stage']}: {str(reason).splitlines()[-1]}")
    print(f"[post-publish-research] Completed. Summary written to {out_file}")
    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
