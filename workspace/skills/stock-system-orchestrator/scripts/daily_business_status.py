#!/usr/bin/env python3
"""Honest end-of-day business status for the stock system (2026-10-03).

Called by the 18:45 closure wrapper after its own work.  Reports what actually
happened today -- selection, publication, broadcast hand-off and Hermes'
delivery record, closure -- even when an upstream stage failed, and writes a
durable record ``health/daily_business_status/<date>.json``.  It never marks a
step successful from a printed report or a local receipt alone.

Standard library only.  Read-only except for its own status file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import exchange_calendar  # noqa: E402

BROADCAST_JOB_NAME = "股票系统-AI分析与报告发布"
PIPELINE_JOB_NAME = "股票系统-A股选股与双轨观察"


def load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def same_local_day(text: Any, date: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10].replace("-", "") == date
    if parsed.tzinfo is not None and exchange_calendar.SHANGHAI is not None:
        parsed = parsed.astimezone(exchange_calendar.SHANGHAI)
    return parsed.strftime("%Y%m%d") == date


def hermes_job(jobs_path: Path, name: str) -> dict[str, Any]:
    payload = load(jobs_path)
    for job in payload.get("jobs") or []:
        if isinstance(job, dict) and job.get("name") == name:
            return job
    return {}


def build_status(date: str, *, health_dir: Path, hermes_jobs: Path, closure_rc: int | None,
                 calendar_path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    session = exchange_calendar.classify_date(date, calendar_path=calendar_path, now=now)
    outcomes_dir = health_dir / "business_outcomes" / date
    outcomes = {}
    if outcomes_dir.is_dir():
        for path in sorted(outcomes_dir.glob("*.json")):
            record = load(path)
            outcomes[path.stem] = record.get("outcome")

    orchestrator = load(health_dir / "orchestrator_run.json")
    ran_today = same_local_day(orchestrator.get("generated_at"), date)
    selection: dict[str, Any]
    if ran_today:
        failed = next((s.get("stage") for s in orchestrator.get("stages") or [] if not s.get("ok")), None)
        selection = {
            "state": "succeeded" if orchestrator.get("selection_ok") is True else "failed",
            "run_id": orchestrator.get("run_id"),
            "trade_date": orchestrator.get("trade_date"),
            "status": orchestrator.get("status"),
            "failed_stage": orchestrator.get("failed_stage") or failed,
            "publish_ok": orchestrator.get("publish_ok"),
        }
    elif outcomes.get("stock_pipeline") == "skipped_market_closed":
        selection = {"state": "skipped_market_closed"}
    else:
        selection = {"state": "not_run_today"}

    publication = load(health_dir / "publication_validation.json")
    receipt = load(health_dir / "deployment_receipt.json")
    pub_today = same_local_day(publication.get("checked_at"), date)
    if pub_today and publication.get("passed") is True and publication.get("pushed_sha"):
        confirmed = (receipt.get("remote_confirmed") is True and receipt.get("local_sha") == publication.get("pushed_sha"))
        published = {"state": "pushed_remote_confirmed" if confirmed else "pushed_unconfirmed",
                     "sha": str(publication.get("pushed_sha"))[:12], "mode": publication.get("mode"),
                     "outcome": publication.get("push_outcome")}
    elif pub_today and publication.get("passed") is not True:
        published = {"state": "blocked_by_final_gate", "blockers": (publication.get("blockers") or [])[:5]}
    elif pub_today and publication.get("changed_after_validation"):
        published = {"state": "aborted_changed_after_validation"}
    else:
        published = {"state": "no_publication_today"}

    delivery = load(health_dir / "delivery_receipt.json")
    job = hermes_job(hermes_jobs, BROADCAST_JOB_NAME)
    job_today = same_local_day(job.get("last_run_at"), date)
    broadcast: dict[str, Any] = {
        "handoff": (
            delivery.get("status") or ("legacy_record_without_transport_status" if delivery.get("delivered_at") else None)
        ) if same_local_day(delivery.get("recorded_at") or delivery.get("handed_at") or delivery.get("delivered_at"), date) else None,
        "trade_date": delivery.get("trade_date"),
    }
    if job_today:
        broadcast["hermes_last_status"] = job.get("last_status")
        broadcast["hermes_delivery_error"] = job.get("last_delivery_error")
        if broadcast["handoff"] == "handed_to_hermes_stdout":
            broadcast["transport"] = (
                "hermes_reported_no_delivery_error" if job.get("last_status") == "ok" and not job.get("last_delivery_error")
                else "hermes_reported_delivery_problem"
            )
    else:
        broadcast["transport"] = "no_hermes_run_record_today"

    return {
        "schema_version": 1,
        "date": date,
        "generated_at": (now or exchange_calendar.now_shanghai()).isoformat(timespec="seconds"),
        "session": session.get("status"),
        "session_reason": session.get("reason"),
        "business_outcomes": outcomes,
        "selection": selection,
        "publication": published,
        "broadcast": broadcast,
        "closure_returncode": closure_rc,
        "notes": [
            "message_id is not available for Hermes cron deliveries; 'hermes_reported_no_delivery_error' "
            "means Hermes recorded no delivery error, not that a reader saw the message.",
            "Optional post-publish research runs at 20:15 and is reported separately.",
        ],
    }


def render(status: dict[str, Any]) -> str:
    sel = status["selection"]
    pub = status["publication"]
    bc = status["broadcast"]
    lines = [f"📋 【A股当日业务状态】{status['date']}"]
    if status.get("session") == "closed":
        lines.append("交易日历：今日休市（业务任务应跳过）")
    elif status.get("session") != "open":
        lines.append(f"交易日历：⚠️ 不可用（{status.get('session_reason')}）")
    if sel["state"] == "succeeded":
        lines.append(f"选股：✅ 成功（批次交易日 {sel.get('trade_date')}，{sel.get('run_id')}）")
    elif sel["state"] == "failed":
        lines.append(f"选股：❌ 失败（阶段 {sel.get('failed_stage') or '未知'}，状态 {sel.get('status')}）")
    elif sel["state"] == "skipped_market_closed":
        lines.append("选股：⏸ 休市跳过")
    else:
        lines.append("选股：⚠️ 今日没有主管线运行记录")
    if pub["state"] == "pushed_remote_confirmed":
        mode = "新批次" if pub.get("mode") == "new_batch" else "刷新"
        lines.append(f"发布：✅ 已推送并远端确认 {pub.get('sha')}（最终检查通过，{mode}）")
    elif pub["state"] == "blocked_by_final_gate":
        lines.append("发布：⛔ 被最终发布检查拦截：" + "；".join(str(b) for b in pub.get("blockers") or []))
    elif pub["state"] == "aborted_changed_after_validation":
        lines.append("发布：⛔ rebase 后内容与已验证内容不同，已中止推送")
    elif pub["state"] == "pushed_unconfirmed":
        lines.append(f"发布：⚠️ 已记录推送 {pub.get('sha')} 但远端回执不一致")
    else:
        lines.append("发布：— 今日无新的发布记录")
    handoff = bc.get("handoff")
    transport = bc.get("transport")
    if handoff == "handed_to_hermes_stdout":
        tail = {"hermes_reported_no_delivery_error": "Hermes 记录无投递错误（无消息ID）",
                "hermes_reported_delivery_problem": f"Hermes 记录异常：{bc.get('hermes_last_status')} {bc.get('hermes_delivery_error') or ''}".strip(),
                "no_hermes_run_record_today": "未找到 Hermes 今日运行记录"}.get(transport, "投递结果未知")
        lines.append(f"播报：已交给 Hermes 投递；{tail}")
    elif handoff == "legacy_record_without_transport_status":
        lines.append("播报：旧格式记录（只记录了打印时间，无投递结果）")
    elif handoff == "skipped_duplicate_batch":
        lines.append("播报：同交易日同名单已播报过，本次未重复播报")
    else:
        lines.append("播报：今日未播报推荐名单")
    rc = status.get("closure_returncode")
    lines.append("收盘核算：✅ 完成" if rc == 0 else f"收盘核算：⚠️ 异常（退出码 {rc}）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Honest daily business status")
    parser.add_argument("--date", default="")
    parser.add_argument("--closure-rc", type=int, default=None)
    parser.add_argument("--health-dir", default="")
    parser.add_argument("--hermes-jobs", default=str(Path.home() / ".hermes" / "cron" / "jobs.json"))
    args = parser.parse_args(argv)
    health_dir = Path(args.health_dir) if args.health_dir else exchange_calendar.default_health_dir()
    date = args.date or exchange_calendar.today_shanghai()
    status = build_status(date, health_dir=health_dir, hermes_jobs=Path(args.hermes_jobs), closure_rc=args.closure_rc)
    out = health_dir / "daily_business_status" / f"{date}.json"
    exchange_calendar._write_json_atomic(out, status)  # noqa: SLF001 - shared stdlib helper
    print(render(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
