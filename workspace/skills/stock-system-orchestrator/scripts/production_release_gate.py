#!/usr/bin/env python3
"""Shared gate for jobs that must consume a completed production release."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Union


JsonLoader = Callable[[Path], Union[dict, list]]
LockChecker = Callable[[Path], bool]
SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TRADE_DATE_RE = re.compile(r"^\d{8}$")


def load_json(path: Path) -> dict | list:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def parse_duration(value: object, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(parsed):
        return float(default)
    return min(max(parsed, float(minimum)), float(maximum))


def resolve_expected_trade_date(
    calendar_path: Path,
    *,
    explicit: str = "",
    env: dict | None = None,
    now_local: datetime | None = None,
    load_fn: JsonLoader = load_json,
) -> str:
    for candidate in (
        str(explicit or "").strip(),
        str((env if env is not None else os.environ).get("OPENCLAW_TARGET_TRADE_DATE") or "").strip(),
    ):
        if TRADE_DATE_RE.fullmatch(candidate):
            return candidate

    calendar = load_fn(calendar_path)
    if not isinstance(calendar, dict):
        return ""
    current = now_local or datetime.now().astimezone()
    today = current.strftime("%Y%m%d")
    date_from = str(calendar.get("from") or "")
    date_to = str(calendar.get("to") or "")
    if not (TRADE_DATE_RE.fullmatch(date_from) and TRADE_DATE_RE.fullmatch(date_to)):
        return ""
    if not (date_from <= today <= date_to):
        return ""
    open_dates = sorted(
        {
            str(value)
            for value in (calendar.get("open_dates") or [])
            if TRADE_DATE_RE.fullmatch(str(value))
        }
    )
    candidates = [value for value in open_dates if value <= today]
    return candidates[-1] if candidates else ""


def doc_value(doc: dict, key: str) -> str:
    return str(doc.get(key) or ((doc.get("run") or {}).get(key)) or "")


def receipt_matches(receipt: dict, run_id: str, trade_date: str) -> bool:
    return bool(
        isinstance(receipt, dict)
        and receipt.get("remote_confirmed") is True
        and str(receipt.get("run_id") or "") == run_id
        and str(receipt.get("trade_date") or "") == trade_date
        and receipt.get("local_sha")
        and receipt.get("local_sha") == receipt.get("remote_sha")
    )


def deploy_recovered(orchestrator: dict, receipt: dict, run_id: str, trade_date: str) -> bool:
    """A failed deploy stage healed by a later remote-confirmed receipt for this run.

    20261009: same rule as generate_system_verdict.resolve_publish_status.  Only
    a failure confined to deployAndNotify after a successful selection can be
    healed; the original failure record is never rewritten.
    """
    return bool(
        orchestrator.get("selection_ok") is True
        and orchestrator.get("publish_ok") is False
        and str(orchestrator.get("failed_stage") or "") == "deployAndNotify"
        and receipt_matches(receipt, run_id, trade_date)
    )


def pipeline_lock_active(lock_dir: Path) -> bool:
    if not lock_dir.is_dir():
        return False
    owner_file = lock_dir / "owner.txt"
    try:
        owner_text = owner_file.read_text(encoding="utf-8").strip()
        if "|" in owner_text:
            pid_text, started_text, _token = (owner_text.split("|", 2) + ["", ""])[:3]
        else:
            pid_text, started_text = owner_text.split()[0], ""
        owner_pid = int(pid_text)
    except (OSError, ValueError, IndexError):
        return True
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if started_text:
        try:
            proc = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(owner_pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        observed = str(proc.stdout or "").strip()
        if proc.returncode == 0 and observed and observed != started_text.strip():
            return False
    return True


def _completed_today(orchestrator: dict, now_local: datetime) -> bool:
    completed_at = str(orchestrator.get("completed_at") or "")
    try:
        completed_date = datetime.fromisoformat(completed_at.replace("Z", "+00:00")).astimezone().date()
    except (TypeError, ValueError):
        return False
    return completed_date == now_local.date()


def inspect_release(
    paths: dict[str, Path],
    *,
    load_fn: JsonLoader = load_json,
    lock_fn: LockChecker = pipeline_lock_active,
    now_local: datetime | None = None,
    expected_trade_date: str = "",
) -> tuple[dict | None, list[str]]:
    """Return a matching release bundle, or explain why it is not ready yet."""

    docs = {name: load_fn(path) for name, path in paths.items() if name != "lock_dir"}
    required = ("manifest", "readiness", "validation", "orchestrator", "receipt")
    if any(not isinstance(docs.get(name), dict) or not docs[name] for name in required):
        return None, ["生产发布状态文件尚未完整生成"]

    manifest = docs["manifest"]
    readiness = docs["readiness"]
    validation = docs["validation"]
    orchestrator = docs["orchestrator"]
    receipt = docs["receipt"]

    ids = {name: doc_value(docs[name], "run_id") for name in required[:-1]}
    run_id = doc_value(manifest, "run_id") or doc_value(orchestrator, "run_id")
    if not run_id or any(not value for value in ids.values()) or len(set(ids.values())) != 1:
        return None, [f"运行状态不一致: {ids}"]
    if not SAFE_RUN_ID_RE.fullmatch(run_id):
        return None, [f"run_id 格式非法: {run_id!r}"]

    dates = {name: doc_value(docs[name], "trade_date") for name in required[:-1]}
    trade_date = dates["manifest"] or dates["orchestrator"]
    if not TRADE_DATE_RE.fullmatch(trade_date):
        return None, [f"交易日格式非法: {trade_date!r}"]
    strategy_date = str((readiness.get("checks") or {}).get("strategy_latest_trade_date") or "")
    if strategy_date:
        dates["strategy"] = strategy_date
    non_empty_dates = [value for value in dates.values() if value]
    if not trade_date or any(value != trade_date for value in non_empty_dates):
        return None, [f"交易日状态不一致: {dates}"]
    if expected_trade_date and trade_date != str(expected_trade_date):
        return None, [f"发布交易日不符合要求: expected={expected_trade_date} actual={trade_date}"]

    if lock_fn(paths["lock_dir"]):
        return None, ["主选股管线仍在运行"]
    if not receipt_matches(receipt, run_id, trade_date):
        return None, ["未检测到匹配的远程发布回执"]
    if readiness.get("base_ok") is not True:
        return None, ["AI 发布基础结果未就绪"]
    if validation.get("ok") is not True:
        return None, ["发布前业务校验未通过"]
    publish_recovered = deploy_recovered(orchestrator, receipt, run_id, trade_date)
    if orchestrator.get("selection_ok") is not True or (
        orchestrator.get("publish_ok") is not True and not publish_recovered
    ):
        return None, ["本轮主选股或远程发布尚未完成"]

    current = now_local or datetime.now().astimezone()
    if not _completed_today(orchestrator, current):
        return None, ["本轮主管线尚未在今天完成"]

    return {
        "manifest": manifest,
        "readiness": readiness,
        "validation": validation,
        "orchestrator": orchestrator,
        "receipt": receipt,
        "run_id": run_id,
        "trade_date": trade_date,
        "publish_recovered": publish_recovered,
    }, []


def terminal_pipeline_failure(
    paths: dict[str, Path],
    *,
    expected_trade_date: str,
    load_fn: JsonLoader = load_json,
    lock_fn: LockChecker = pipeline_lock_active,
) -> str:
    """Return a current-run terminal failure, never a partial or older attempt."""

    if not TRADE_DATE_RE.fullmatch(str(expected_trade_date or "")):
        return ""
    orchestrator = load_fn(paths["orchestrator"])
    if not isinstance(orchestrator, dict) or not orchestrator:
        return ""
    trade_date = doc_value(orchestrator, "trade_date") or str(orchestrator.get("target_trade_date") or "")
    if trade_date != str(expected_trade_date):
        return ""
    if str(orchestrator.get("status") or "") != "failed":
        return ""
    if lock_fn(paths["lock_dir"]):
        return ""

    failed_stage = str(orchestrator.get("failed_stage") or "")
    if not failed_stage:
        failed = [
            str(item.get("stage") or item.get("name") or "")
            for item in (orchestrator.get("stages") or [])
            if isinstance(item, dict) and item.get("ok") is False
        ]
        failed_stage = failed[-1] if failed else "unknown"
    run_id = doc_value(orchestrator, "run_id") or "unknown"
    return (
        "主管线已终止失败: "
        f"run_id={run_id} trade_date={trade_date} failed_stage={failed_stage}"
    )


def wait_for_release(
    paths: dict[str, Path],
    *,
    load_fn: JsonLoader = load_json,
    lock_fn: LockChecker = pipeline_lock_active,
    expected_trade_date: str = "",
    timeout_s: float = 3600.0,
    interval_s: float = 2.0,
    now_fn: Callable[[], datetime] | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[dict | None, list[str]]:
    safe_timeout = parse_duration(timeout_s, default=3600, minimum=0, maximum=7200)
    safe_interval = parse_duration(interval_s, default=2, minimum=0.01, maximum=30)
    deadline = monotonic_fn() + safe_timeout
    last_errors: list[str] = []
    while True:
        now_local = now_fn() if now_fn else datetime.now().astimezone()
        bundle, errors = inspect_release(
            paths,
            load_fn=load_fn,
            lock_fn=lock_fn,
            now_local=now_local,
            expected_trade_date=expected_trade_date,
        )
        if bundle is not None:
            return bundle, []
        last_errors = errors
        terminal_error = terminal_pipeline_failure(
            paths,
            expected_trade_date=expected_trade_date,
            load_fn=load_fn,
            lock_fn=lock_fn,
        )
        if terminal_error:
            return None, [terminal_error]
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            return None, last_errors
        sleep_fn(min(safe_interval, remaining))
