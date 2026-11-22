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
import v43_retirement  # noqa: E402

BROADCAST_JOB_NAME = "股票系统-AI分析与报告发布"
PIPELINE_JOB_NAME = "股票系统-A股选股与双轨观察"


def _detect_retired(health_dir: Path, workspace: Path | None, retired: bool | None) -> bool:
    if retired is not None:
        return retired
    env = os.environ.get("STOCK_V43_RETIRED")
    if env in ("0", "1"):
        return env == "1"
    if workspace is not None:
        return v43_retirement.is_retired(workspace)
    if len(health_dir.parents) >= 3 and health_dir.name == "health":
        candidate = health_dir.parents[2]
        if (candidate / "config" / "v43_retirement.json").exists():
            return v43_retirement.is_retired(candidate)
    return False


def _infer_workspace(health_dir: Path, workspace: Path | None) -> Path:
    if workspace is not None:
        return workspace
    if len(health_dir.parents) >= 3 and health_dir.name == "health":
        return health_dir.parents[2]
    return v43_retirement.workspace()


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


def default_hermes_jobs() -> Path:
    """Hermes job store: $HERMES_HOME first (the VPS runs Hermes from /srv/hermes-stock/hermes-home), then ~/.hermes."""
    for home in (os.environ.get("HERMES_HOME"), "/srv/hermes-stock/hermes-home"):
        if home and (Path(home) / "cron/jobs.json").is_file():
            return Path(home) / "cron/jobs.json"
    return Path.home() / ".hermes" / "cron" / "jobs.json"


def hermes_job(jobs_path: Path, name: str) -> dict[str, Any]:
    payload = load(jobs_path)
    for job in payload.get("jobs") or []:
        if isinstance(job, dict) and job.get("name") == name:
            return job
    return {}


def _build_zero_paid_status(
    date: str,
    *,
    health_dir: Path,
    hermes_jobs: Path,
    closure_rc: int | None,
    session: dict[str, Any],
    outcomes: dict[str, Any],
    ws: Path,
    now: datetime | None,
) -> dict[str, Any]:
    s1 = load(health_dir / "zero_paid_stage1.json")
    s1_today = str(s1.get("target_trade_date") or "") == date
    if s1_today and s1.get("ok") is True:
        stage1: dict[str, Any] = {
            "state": "succeeded",
            "daily_rows": (s1.get("steps") or {}).get("daily", {}).get("rows"),
            "pit_rows": (s1.get("steps") or {}).get("pit_snapshot", {}).get("rows"),
            "seconds": s1.get("seconds"),
        }
    elif s1_today:
        stage1 = {"state": "failed", "outputs": s1.get("outputs")}
    elif outcomes.get("stock_pipeline") == "skipped_market_closed":
        stage1 = {"state": "skipped_market_closed"}
    else:
        stage1 = {"state": "not_run_today"}

    zpd = load(health_dir / "zero_paid_daily" / f"run_{date}.json")
    if zpd and str(zpd.get("date") or "") == date:
        probs = zpd.get("problems") or []
        zero_paid_daily: dict[str, Any] = {
            "state": "succeeded" if not probs else "degraded",
            "reconcile": (zpd.get("reconcile") or {}).get("verdict"),
            "prices": (zpd.get("prices") or {}).get("verdict"),
            "em_ann": (zpd.get("em_ann") or {}).get("status"),
            "problems": probs[:5],
        }
    else:
        zero_paid_daily = {"state": "not_run_today"}

    ew6_state = load(ws / "factor_factory/data/forward_shadow/state.json")
    if str(ew6_state.get("signal_date") or "") == date:
        ew6: dict[str, Any] = {
            "state": "recorded" if ew6_state.get("status") == "recorded" else str(ew6_state.get("status") or "unknown"),
            "universe": ew6_state.get("universe"),
            "composite_coverage": ew6_state.get("composite_coverage"),
            "is_rebalance": ew6_state.get("is_rebalance"),
        }
    else:
        ew6 = {"state": "not_recorded_today", "last_signal_date": ew6_state.get("signal_date")}

    pub = load(health_dir / "ew6_publish" / "publication_validation.json")
    receipt = load(health_dir / "deployment_receipt.json")
    pub_today = (
        str(pub.get("batch") or "") == f"ew6_{date}"
        or same_local_day(pub.get("pushed_at") or pub.get("validated_at"), date)
    )
    if pub_today and pub.get("passed") is True and pub.get("pushed_sha"):
        confirmed = (
            receipt.get("remote_confirmed") is True
            and receipt.get("local_sha") == pub.get("pushed_sha")
        )
        published: dict[str, Any] = {
            "state": "pushed_remote_confirmed" if confirmed else "pushed_unconfirmed",
            "sha": str(pub.get("pushed_sha"))[:12],
            "mode": pub.get("mode") or "v43_retired_ew6",
            "outcome": pub.get("push_outcome"),
        }
    elif pub_today and pub.get("passed") is not True:
        published = {"state": "blocked_by_final_gate", "blockers": (pub.get("blockers") or [])[:5]}
    else:
        published = {"state": "no_publication_today"}

    multi_state = load(ws / "factor_factory/data/forward_shadow/multi_v1/state.json")
    multi_job = hermes_job(hermes_jobs, "股票系统-多账本前向影子multi_v1·晚间")
    if str(multi_state.get("signal_date") or "") == date:
        multi_v1: dict[str, Any] = {"state": str(multi_state.get("status") or "recorded"), "signal_date": date}
    elif same_local_day(multi_job.get("last_run_at"), date):
        multi_v1 = {
            "state": "ok_waiting_ann" if multi_job.get("last_status") == "ok" else "failed",
            "hermes_last_status": multi_job.get("last_status"),
            "last_error": multi_job.get("last_error"),
        }
    else:
        multi_v1 = {"state": "pending_or_not_run", "last_signal_date": multi_state.get("signal_date")}

    return {
        "schema_version": 2,
        "mode": "zero_paid_v43_retired",
        "date": date,
        "generated_at": (now or exchange_calendar.now_shanghai()).isoformat(timespec="seconds"),
        "session": session.get("status"),
        "session_reason": session.get("reason"),
        "business_outcomes": outcomes,
        "stage1": stage1,
        "zero_paid_daily": zero_paid_daily,
        "ew6_shadow": ew6,
        "multi_v1": multi_v1,
        "selection": {"state": "v43_retired"},
        "publication": published,
        "broadcast": {"handoff": "v43_retired", "transport": "v43_retired"},
        "closure_returncode": closure_rc,
        "notes": [
            "v4.3 is retired under the zero-paid architecture; #/today publishes EW6 buffered_top100.",
            "multi_v1 evening waits for next-morning 08:55 announcement fetch when x_jump_ms is pending.",
        ],
    }


def build_status(date: str, *, health_dir: Path, hermes_jobs: Path, closure_rc: int | None,
                 calendar_path: Path | None = None, now: datetime | None = None,
                 workspace: Path | None = None, retired: bool | None = None) -> dict[str, Any]:
    session = exchange_calendar.classify_date(date, calendar_path=calendar_path, now=now)
    outcomes_dir = health_dir / "business_outcomes" / date
    outcomes = {}
    if outcomes_dir.is_dir():
        for path in sorted(outcomes_dir.glob("*.json")):
            record = load(path)
            outcomes[path.stem] = record.get("outcome")

    if _detect_retired(health_dir, workspace, retired):
        return _build_zero_paid_status(
            date,
            health_dir=health_dir,
            hermes_jobs=hermes_jobs,
            closure_rc=closure_rc,
            session=session,
            outcomes=outcomes,
            ws=_infer_workspace(health_dir, workspace),
            now=now,
        )

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


def _render_zero_paid(status: dict[str, Any]) -> str:
    s1 = status.get("stage1") or {}
    zpd = status.get("zero_paid_daily") or {}
    ew6 = status.get("ew6_shadow") or {}
    pub = status.get("publication") or {}
    mv1 = status.get("multi_v1") or {}
    lines = [f"📋 【A股交易日业务汇总 · 零付费模式】交易日 {status['date']}"]
    if status.get("session") == "closed":
        lines.append("交易日历：今日休市（业务任务应跳过）")
    elif status.get("session") != "open":
        lines.append(f"交易日历：⚠️ 不可用（{status.get('session_reason')}）")
    if s1.get("state") == "succeeded":
        lines.append(f"盘后准备(stage1)：✅ 完成（日线 {s1.get('daily_rows')} 只，耗时 {s1.get('seconds')}s）")
    elif s1.get("state") == "skipped_market_closed":
        lines.append("盘后准备(stage1)：⏸ 休市跳过")
    elif s1.get("state") == "failed":
        lines.append(f"盘后准备(stage1)：❌ 失败（{s1.get('outputs')}）")
    else:
        lines.append("盘后准备(stage1)：⚠️ 今日无运行记录")
    if zpd.get("state") == "succeeded":
        lines.append(f"零付费核对(A1/A4/A5)：✅ 全部通过（日历={zpd.get('reconcile')}，行情={zpd.get('prices')}，公告={zpd.get('em_ann')}）")
    elif zpd.get("state") == "degraded":
        lines.append("零付费核对(A1/A4/A5)：⚠️ 存在异常：" + "；".join(str(p) for p in zpd.get("problems") or []))
    else:
        lines.append("零付费核对(A1/A4/A5)：— 今日尚未运行")
    if ew6.get("state") == "recorded":
        cov = ew6.get("composite_coverage")
        cov_txt = f"{cov:.2%}" if isinstance(cov, (int, float)) else str(cov)
        reb = "调仓日" if ew6.get("is_rebalance") else "持有日"
        lines.append(f"EW6 影子记账：✅ 已记录（{reb}，股票池 {ew6.get('universe')}，覆盖率 {cov_txt}）")
    else:
        lines.append(f"EW6 影子记账：⚠️ 今日未记账（最近 {ew6.get('last_signal_date') or '无'}）")
    if pub.get("state") == "pushed_remote_confirmed":
        lines.append(f"页面发布(#/today EW6)：✅ 已推送并远端确认 {pub.get('sha')}")
    elif pub.get("state") == "blocked_by_final_gate":
        lines.append("页面发布(#/today EW6)：⛔ 被发布检查拦截：" + "；".join(str(b) for b in pub.get("blockers") or []))
    elif pub.get("state") == "pushed_unconfirmed":
        lines.append(f"页面发布(#/today EW6)：⚠️ 已推送 {pub.get('sha')} 但远端回执不一致")
    else:
        lines.append("页面发布(#/today EW6)：— 今日无新的发布记录")
    if mv1.get("state") in ("recorded", "ok_waiting_ann"):
        tail = "已记账" if mv1.get("state") == "recorded" else "晚间已就绪（08:55 公告后首次正式落盘）"
        lines.append(f"多账本影子(multi_v1)：✅ {tail}")
    elif mv1.get("state") == "failed":
        lines.append(f"多账本影子(multi_v1)：❌ 失败（{mv1.get('last_error') or mv1.get('hermes_last_status')}）")
    else:
        lines.append("多账本影子(multi_v1)：— 今日尚未运行")
    rc = status.get("closure_returncode")
    lines.append("收盘核算：✅ 完成（v4.3 已退役，旧闭环已跳过）" if rc == 0 else f"收盘核算：⚠️ 异常（退出码 {rc}）")
    return "\n".join(lines)


def render(status: dict[str, Any]) -> str:
    if status.get("mode") == "zero_paid_v43_retired":
        return _render_zero_paid(status)
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


def previous_session(today: str, health_dir: Path) -> str:
    """Last open session strictly before today (fail closed when the calendar has none)."""
    cal = exchange_calendar.load_calendar(health_dir / "trading_calendar.json")
    prior = [d for d in cal["open_dates"] if d < today]
    if not prior:
        raise exchange_calendar.CalendarError(f"no open session before {today} in the trading calendar")
    return prior[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Honest daily business status")
    parser.add_argument("--date", default="")
    parser.add_argument("--closure-rc", type=int, default=None)
    parser.add_argument("--health-dir", default="")
    parser.add_argument("--hermes-jobs", default=str(default_hermes_jobs()))
    parser.add_argument("--previous-session", action="store_true",
                        help="summarize the last open session before today (08:30 morning report, 2026-11-21)")
    args = parser.parse_args(argv)
    health_dir = Path(args.health_dir) if args.health_dir else exchange_calendar.default_health_dir()
    if args.previous_session and not args.date:
        date = previous_session(exchange_calendar.today_shanghai(), health_dir)
    else:
        date = args.date or exchange_calendar.today_shanghai()
    status = build_status(date, health_dir=health_dir, hermes_jobs=Path(args.hermes_jobs), closure_rc=args.closure_rc)
    out = health_dir / "daily_business_status" / f"{date}.json"
    exchange_calendar._write_json_atomic(out, status)  # noqa: SLF001 - shared stdlib helper
    print(render(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
