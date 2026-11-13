#!/usr/bin/env python3
"""18:00 Stock report broadcaster.

Waits for 17:30 main pipeline publication receipt and delivers a concise Chinese report once.
Pure broadcaster: never publishes or mutates git repository directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ORCHESTRATOR_SCRIPTS = Path(__file__).resolve().parent.parent / "skills/stock-system-orchestrator/scripts"
if str(ORCHESTRATOR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATOR_SCRIPTS))

from production_release_gate import (  # noqa: E402
    parse_duration,
    pipeline_lock_active as _pipeline_lock_active,
    resolve_expected_trade_date,
    wait_for_release,
    write_json_atomic,
)

WORKSPACE = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", "/Users/fisher/.openclaw/workspace"))
HEALTH_DIR = WORKSPACE / "stock_data/03-working/health"
AI_ANALYSIS_DIR = WORKSPACE / "stock_data/03-working/ai_analysis"
SYSTEM_VERDICT = WORKSPACE / "stock-report/data/latest/system_verdict.json"
RUN_MANIFEST = WORKSPACE / "stock-report/data/latest/run_manifest.json"
PUBLISHED_DATA_JSON = WORKSPACE / "stock-report/data.json"
VALIDATION_REPORT = HEALTH_DIR / "validation_report.json"
AI_READINESS = HEALTH_DIR / "ai_publish_readiness.json"
ORCHESTRATOR_RUN = HEALTH_DIR / "orchestrator_run.json"
PIPELINE_LOCK_DIR = HEALTH_DIR / "stock_pipeline_gate.lockdir"
RECEIPT_FILE = HEALTH_DIR / "deployment_receipt.json"
DELIVERY_RECEIPT = HEALTH_DIR / "delivery_receipt.json"
TRADING_CALENDAR = HEALTH_DIR / "trading_calendar.json"
EXPECTED_SEATS = 20
ALLOWED_FINAL_ACTIONS = {"observe_only", "halt", "cautious_execute", "conditional_execute", "execute"}


def publication_validation_path() -> Path:
    return HEALTH_DIR / "publication_validation.json"


def broadcast_ledger_path() -> Path:
    return HEALTH_DIR / "broadcast_ledger.json"


def load_json(path: Path) -> dict | list:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _normalize_broadcast_row(row: dict) -> dict:
    item = dict(row)
    if not item.get("sector_name"):
        item["sector_name"] = item.get("industry_name") or item.get("industry") or ""
    return item


def load_broadcast_rows(trade_date: str, expected_seats: int) -> list[dict]:
    """Prefer the published same-day contract; fall back to the working analysis file."""
    ai_file = AI_ANALYSIS_DIR / f"{trade_date}.json" if trade_date else None
    working_rows = load_json(ai_file) if ai_file and ai_file.exists() else []
    if not isinstance(working_rows, list):
        working_rows = []
    working_by_code = {
        str(row.get("code") or row.get("ts_code") or "").split(".")[0]: row
        for row in working_rows
        if isinstance(row, dict)
    }

    published = load_json(PUBLISHED_DATA_JSON)
    if isinstance(published, dict):
        pub_date = str(published.get("latest_trade_date") or published.get("trade_date") or "")
        top20 = published.get("top20")
        if pub_date == trade_date and isinstance(top20, list) and len(top20) >= expected_seats:
            rows = []
            for row in top20[:expected_seats]:
                if not isinstance(row, dict):
                    continue
                item = _normalize_broadcast_row(row)
                code = str(item.get("code") or item.get("ts_code") or "").split(".")[0]
                source = working_by_code.get(code) or {}
                if not item.get("model_name"):
                    item["model_name"] = source.get("model_name") or "copilot/gemini-3.8-flash"
                rows.append(item)
            return rows

    return [_normalize_broadcast_row(row) for row in working_rows if isinstance(row, dict)]


def _run_id(doc: dict) -> str:
    return str(doc.get("run_id") or ((doc.get("run") or {}).get("run_id")) or "")


def _trade_date(doc: dict) -> str:
    return str(doc.get("trade_date") or ((doc.get("run") or {}).get("trade_date")) or "")


def pipeline_lock_active(lock_dir: Path = PIPELINE_LOCK_DIR) -> bool:
    return _pipeline_lock_active(lock_dir)


def receipt_matches(receipt: dict | None, run_id: str, trade_date: str) -> bool:
    if not receipt or not isinstance(receipt, dict):
        return False
    return bool(
        receipt.get("remote_confirmed") is True
        and receipt.get("run_id") == run_id
        and receipt.get("trade_date") == trade_date
        and receipt.get("local_sha")
        and receipt.get("local_sha") == receipt.get("remote_sha")
    )


def wait_for_receipt(run_id: str, trade_date: str, timeout_s: int | None = None, interval_s: float | None = None) -> dict | None:
    effective_timeout = timeout_s if timeout_s is not None else float(os.environ.get("OPENCLAW_RECEIPT_TIMEOUT_SECONDS", "1200"))
    effective_interval = interval_s if interval_s is not None else float(os.environ.get("OPENCLAW_RECEIPT_INTERVAL_SECONDS", "2.0"))
    deadline = time.monotonic() + effective_timeout
    while time.monotonic() < deadline:
        if RECEIPT_FILE.is_file():
            try:
                receipt = json.loads(RECEIPT_FILE.read_text(encoding="utf-8"))
                if receipt_matches(receipt, run_id, trade_date):
                    return receipt
            except Exception:
                pass
        time.sleep(effective_interval)
    return None


def release_paths() -> dict[str, Path]:
    return {
        "manifest": RUN_MANIFEST,
        "readiness": AI_READINESS,
        "validation": VALIDATION_REPORT,
        "orchestrator": ORCHESTRATOR_RUN,
        "receipt": RECEIPT_FILE,
        "lock_dir": PIPELINE_LOCK_DIR,
    }


def wait_for_completed_release(timeout_s: float | None = None, interval_s: float | None = None) -> tuple[dict | None, list[str]]:
    expected_trade_date = resolve_expected_trade_date(
        TRADING_CALENDAR,
        env=os.environ,
        load_fn=load_json,
    )
    if not expected_trade_date:
        return None, ["无法确定本次应消费的交易日"]
    raw_timeout = timeout_s
    if raw_timeout is None:
        raw_timeout = os.environ.get(
            "OPENCLAW_RECEIPT_TIMEOUT_SECONDS",
            os.environ.get("OPENCLAW_PRODUCTION_RELEASE_TIMEOUT_SECONDS", "3600"),
        )
    raw_interval = interval_s
    if raw_interval is None:
        raw_interval = os.environ.get("OPENCLAW_RECEIPT_INTERVAL_SECONDS", "2.0")
    effective_timeout = parse_duration(raw_timeout, default=3600, minimum=0, maximum=7200)
    effective_interval = parse_duration(raw_interval, default=2, minimum=0.01, maximum=30)
    return wait_for_release(
        release_paths(),
        load_fn=load_json,
        lock_fn=pipeline_lock_active,
        expected_trade_date=expected_trade_date,
        timeout_s=effective_timeout,
        interval_s=effective_interval,
    )


def is_already_delivered(run_id: str) -> bool:
    if not DELIVERY_RECEIPT.is_file():
        return False
    try:
        data = json.loads(DELIVERY_RECEIPT.read_text(encoding="utf-8"))
        return str(data.get("run_id") or "") == str(run_id)
    except Exception:
        return False


def record_delivery(run_id: str, trade_date: str, fingerprint: str = "",
                    status: str = "handed_to_hermes_stdout") -> None:
    """Record the hand-off honestly.

    This process only prints the report; Hermes delivers stdout after the
    script exits.  The record therefore says ``handed_to_hermes_stdout`` --
    not "delivered".  Transport acceptance is reconciled later from Hermes'
    own job record (daily business status).
    """
    now = datetime.now().astimezone().isoformat()
    payload = {
        "run_id": run_id,
        "trade_date": trade_date,
        "candidate_fingerprint": fingerprint or None,
        "status": status,
        "handed_at": now if status == "handed_to_hermes_stdout" else None,
        "recorded_at": now,
        "transport_confirmation": (
            "pending_hermes_delivery_result" if status == "handed_to_hermes_stdout" else "not_sent"
        ),
    }
    write_json_atomic(DELIVERY_RECEIPT, payload)
    if status != "handed_to_hermes_stdout":
        return
    ledger = load_json(broadcast_ledger_path())
    if not isinstance(ledger, dict):
        ledger = {}
    ledger[trade_date] = {
        "run_id": run_id,
        "candidate_fingerprint": fingerprint or None,
        "status": "handed_to_hermes_stdout",
        "handed_at": now,
    }
    write_json_atomic(broadcast_ledger_path(), ledger)


def candidate_fingerprint(trade_date: str, rows: list[dict]) -> str:
    codes = sorted(str(row.get("code") or row.get("ts_code") or "").split(".")[0] for row in rows)
    return hashlib.sha256(json.dumps([trade_date, codes]).encode("utf-8")).hexdigest()


def already_broadcast_batch(trade_date: str, fingerprint: str) -> bool:
    ledger = load_json(broadcast_ledger_path())
    entry = ledger.get(trade_date) if isinstance(ledger, dict) else None
    return isinstance(entry, dict) and entry.get("candidate_fingerprint") == fingerprint


def validate_published_batch(manifest: dict, receipt: dict, verdict: dict, publication: dict) -> list[str]:
    """2026-10-03: broadcast only a batch whose final published content passed
    the binding publication gate and whose verdict is not halt."""
    errors: list[str] = []
    run_id = _run_id(manifest)
    trade_date = _trade_date(manifest)
    final_action = verdict.get("final_action") if isinstance(verdict, dict) else None
    action = final_action.get("action") if isinstance(final_action, dict) else None
    if not isinstance(final_action, dict) or action not in ALLOWED_FINAL_ACTIONS:
        errors.append("已发布裁决不可读或 final_action 结构异常")
    elif action == "halt":
        errors.append("最终裁决为暂缓(halt)，不播报新名单")
    if isinstance(verdict, dict):
        verdict_run = str((verdict.get("run") or {}).get("run_id") or "")
        verdict_date = str((verdict.get("dates") or {}).get("decision_trade_date") or "")
        if verdict_run != run_id or verdict_date != trade_date:
            errors.append(f"已发布裁决不属于本批次: verdict={verdict_run}/{verdict_date} manifest={run_id}/{trade_date}")
    if not isinstance(publication, dict) or publication.get("passed") is not True:
        errors.append("本批次没有通过最终发布检查的记录")
    else:
        batch = publication.get("batch") or {}
        if batch.get("run_id") != run_id or batch.get("trade_date") != trade_date:
            errors.append(f"最终发布检查记录属于其他批次: {batch}")
        pushed_sha = str(publication.get("pushed_sha") or "")
        if not pushed_sha or pushed_sha != str((receipt or {}).get("local_sha") or ""):
            errors.append("最终发布检查记录与远端发布回执不一致")
    return errors


def validate_publish_state(
    manifest: dict,
    readiness: dict,
    validation: dict,
    orchestrator: dict,
    *,
    now_local: datetime | None = None,
) -> list[str]:
    errors: list[str] = []
    manifest_date = _trade_date(manifest)
    if not manifest_date:
        errors.append("run_manifest 缺 trade_date")

    ids = {
        "manifest": _run_id(manifest),
        "ai_readiness": _run_id(readiness),
        "validation": _run_id(validation),
        "orchestrator": _run_id(orchestrator),
    }
    if not all(ids.values()) or len(set(ids.values())) != 1:
        errors.append(f"运行状态不一致: {ids}")

    dates = {
        "manifest": manifest_date,
        "ai_readiness": _trade_date(readiness),
        "validation": _trade_date(validation),
        "orchestrator": _trade_date(orchestrator),
        "strategy": str((readiness.get("checks") or {}).get("strategy_latest_trade_date") or ""),
    }
    nonempty_dates = [value for value in dates.values() if value]
    if not nonempty_dates or any(value != manifest_date for value in nonempty_dates):
        errors.append(f"交易日状态不一致: {dates}")

    if readiness.get("base_ok") is not True:
        errors.append("AI 发布基础结果未就绪")
    if validation.get("ok") is not True:
        errors.append("发布前业务校验未通过")
    if orchestrator.get("selection_ok") is not True:
        errors.append("本轮主选股未完成")

    completed_at = str(orchestrator.get("completed_at") or "")
    try:
        completed_date = datetime.fromisoformat(completed_at.replace("Z", "+00:00")).astimezone().date()
    except (ValueError, TypeError):
        completed_date = None
    today = (now_local or datetime.now().astimezone()).date()
    if completed_date != today:
        errors.append(f"本轮主管线不是今天完成: completed_at={completed_at or 'missing'}")
    return errors


def _model_summary(rows: list[dict]) -> str:
    counts = Counter(str(row.get("model_name") or "unknown") for row in rows)
    return " / ".join(f"{name}×{count}" for name, count in sorted(counts.items())) or "unknown"


def main() -> int:
    release, release_errors = wait_for_completed_release()
    if release is None:
        print("⏳ 【A股播报暂缓】主管线尚未完成或未检测到匹配的远程发布回执。")
        for error in release_errors[-3:]:
            print(f"  - {error}")
        return 10

    manifest = release["manifest"]
    readiness = release["readiness"]
    validation = release["validation"]
    orchestrator = release["orchestrator"]
    state_errors = validate_publish_state(manifest, readiness, validation, orchestrator)
    if state_errors:
        print("🚨 AI 播报状态不一致，已暂停播报：")
        for error in state_errors:
            print(f"  - {error}")
        return 2

    run_id = _run_id(manifest)
    trade_date = _trade_date(manifest)

    # Idempotent deduplication: already broadcasted this run
    if is_already_delivered(run_id):
        return 0

    receipt = release["receipt"]
    verdict = load_json(SYSTEM_VERDICT)
    publication = load_json(publication_validation_path())
    batch_errors = validate_published_batch(manifest, receipt, verdict, publication)
    if batch_errors:
        print("🚨 本批次未满足播报条件，未播报推荐名单：")
        for error in batch_errors:
            print(f"  - {error}")
        return 2

    status_counts = (readiness.get("checks") or {}).get("ai_status_counts") or {}
    expected_seats = int(status_counts.get("total") or EXPECTED_SEATS)
    ready_count = int(status_counts.get("ready") or 0)
    ai_rows = load_broadcast_rows(trade_date, expected_seats)
    if ready_count > len(ai_rows):
        print(
            f"🚨 AI 状态与分析文件不一致：状态称 ready={ready_count}，"
            f"但可播报合同只有 {len(ai_rows)} 行；已暂停播报。"
        )
        return 2
    failed_count = max(expected_seats - ready_count, 0)

    buy_count = sum(1 for r in ai_rows if any(k in str(r.get("ai_advice") or "") for k in ("买入", "做多", "主攻")))
    watch_count = sum(1 for r in ai_rows if any(k in str(r.get("ai_advice") or "") for k in ("观察", "持仓", "持有", "中性")))
    avoid_count = sum(1 for r in ai_rows if any(k in str(r.get("ai_advice") or "") for k in ("回避", "卖出", "避险", "减仓")))
    known_advice_count = buy_count + watch_count + avoid_count
    unknown_advice_count = max(len(ai_rows) - known_advice_count, 0)

    fingerprint = candidate_fingerprint(trade_date, ai_rows)
    if already_broadcast_batch(trade_date, fingerprint):
        # Same trading day, same list: a re-run must not broadcast it again.
        record_delivery(run_id, trade_date, fingerprint, status="skipped_duplicate_batch")
        return 0
    ledger = load_json(broadcast_ledger_path())
    same_day_update = isinstance(ledger, dict) and isinstance(ledger.get(trade_date), dict)

    scores = [float(r["ai_score"]) for r in ai_rows if isinstance(r.get("ai_score"), (int, float))]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    print("=" * 50)
    title_suffix = "（同交易日名单更新）" if same_day_update else ""
    print(f"📊 【A股选股 AI深度分析报告】 交易日: {trade_date}{title_suffix}")
    print(
        f"🎯 生产席位: {len(ai_rows)}/{expected_seats} | 有效就绪: {ready_count} "
        f"| 失败/未就绪: {failed_count}"
    )
    dist_str = f"观察/持仓 {watch_count} | 回避 {avoid_count} | 主攻/买入 {buy_count}"
    if unknown_advice_count:
        dist_str += f" | 无法识别建议标签 {unknown_advice_count}"
    print(f"📈 观点分布: {dist_str}")
    print(f"⭐ AI 综合均分: {avg_score} 分 (模型: {_model_summary(ai_rows)})")
    print("🛡️ 执行权限: 观察性质 (observe_only_no_auto_order) — 严禁自动下单")
    print("=" * 50)

    # Top 3 Candidates
    if ai_rows:
        print("📌 重点观察标的 (Top 3):")
        for i, item in enumerate(ai_rows[:3], 1):
            name = item.get("name", "")
            code = item.get("code", item.get("ts_code", ""))
            sector = item.get("sector_name", "")
            advice = item.get("ai_advice", "观察")
            score = item.get("ai_score", "--")
            summary = item.get("ai_summary", "")
            if len(summary) > 60:
                summary = summary[:60] + "..."
            print(f"  {i}. {name}({code}) [{sector}] | 建议:{advice} | 评分:{score}")
            if summary:
                print(f"     └ {summary}")

    short_sha = str(receipt.get("local_sha") or "")[:7]
    print(f"\n✅ GitHub Pages 发布已由 17:30 主链确认 (commit: {short_sha})")
    record_delivery(run_id, trade_date, fingerprint)
    return 0


if __name__ == "__main__":
    sys.exit(main())
