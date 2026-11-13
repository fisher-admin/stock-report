#!/usr/bin/env python3
"""Alert shaping for the Hermes stock wrappers (2026-10-03).

- A new failure signature for a job      -> print the full alert text.
- The same signature again (still open)  -> print one short "still failing" line.
- First success after an open incident   -> print one "recovered" line.
- Success with no open incident          -> print nothing.

It only shapes the text a wrapper prints.  The wrapper's exit code is left
untouched, so Hermes' execution ledger and incident store still record every
failure honestly.  State: ``health/alert_incidents/<job>.json`` plus an
append-only ``health/alert_incidents/history.jsonl``.  Standard library only.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import exchange_calendar  # noqa: E402

JOB_RE = re.compile(r"^[a-z0-9_]{1,64}$")
JOB_LABELS = {
    "morning_brief": "A股晨判",
    "event_quality_drift": "盘前业绩事件观察",
    "midday_analysis": "A股午盘分析",
    "stock_pipeline": "A股主选股管线",
    "daily_ai_publish": "A股晚间播报",
    "daily_closure": "A股收盘核算",
    "data_freshness_watchdog": "选股数据新鲜度检查",
    "post_publish_research": "A股后置研究",
    "evening_preflight": "晚间链预检",
}


def signature(text: str) -> str:
    """Stable fingerprint of a failure: first meaningful lines, numbers masked."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    key_lines = []
    for line in lines:
        masked = re.sub(r"\d+", "#", line)
        masked = re.sub(r"(/[^\s:]+)+", "<path>", masked)
        key_lines.append(masked)
        if len(key_lines) >= 3:
            break
    return hashlib.sha256("\n".join(key_lines).encode("utf-8")).hexdigest()[:16]


def _state_dir(health_dir: Path | None) -> Path:
    return (health_dir or exchange_calendar.default_health_dir()) / "alert_incidents"


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _history(state_dir: Path, event: dict[str, Any]) -> None:
    with (state_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def on_failure(job: str, text: str, *, rc: int, health_dir: Path | None = None) -> str:
    state_dir = _state_dir(health_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{job}.json"
    now = exchange_calendar.now_shanghai().isoformat(timespec="seconds")
    sig = signature(text)
    with (state_dir / f".{job}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _load(path)
        if state.get("status") == "open" and state.get("signature") == sig:
            state["count"] = int(state.get("count") or 1) + 1
            state["last_seen"] = now
            state["last_rc"] = rc
            exchange_calendar._write_json_atomic(path, state)  # noqa: SLF001
            _history(state_dir, {"job": job, "event": "repeat", "signature": sig, "count": state["count"], "at": now})
            label = JOB_LABELS.get(job, job)
            return (f"⚠️ 【{label}】仍未恢复（同一问题第 {state['count']} 次，首次 {state.get('first_seen')}，"
                    f"退出码 {rc}）；详情见首次告警与本机日志。")
        state = {"job": job, "status": "open", "signature": sig, "first_seen": now, "last_seen": now,
                 "count": 1, "last_rc": rc}
        exchange_calendar._write_json_atomic(path, state)  # noqa: SLF001
        _history(state_dir, {"job": job, "event": "new_incident", "signature": sig, "at": now})
        return text.rstrip("\n")


def on_success(job: str, *, health_dir: Path | None = None) -> str:
    state_dir = _state_dir(health_dir)
    path = state_dir / f"{job}.json"
    if not path.is_file():
        return ""
    now = exchange_calendar.now_shanghai().isoformat(timespec="seconds")
    with (state_dir / f".{job}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _load(path)
        if state.get("status") != "open":
            return ""
        state.update(status="recovered", recovered_at=now)
        exchange_calendar._write_json_atomic(path, state)  # noqa: SLF001
        _history(state_dir, {"job": job, "event": "recovered", "signature": state.get("signature"),
                             "count": state.get("count"), "at": now})
    label = JOB_LABELS.get(job, job)
    return f"✅ 【{label}】已恢复（此前连续失败 {state.get('count')} 次，自 {state.get('first_seen')}）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stock wrapper alert shaping")
    parser.add_argument("event", choices=["failure", "success"])
    parser.add_argument("--job", required=True)
    parser.add_argument("--rc", type=int, default=1)
    parser.add_argument("--health-dir", default="")
    args = parser.parse_args(argv)
    if not JOB_RE.fullmatch(args.job):
        print(f"invalid job name: {args.job!r}")
        return 2
    health_dir = Path(args.health_dir) if args.health_dir else None
    if args.event == "failure":
        text = sys.stdin.read()
        print(on_failure(args.job, text, rc=args.rc, health_dir=health_dir))
    else:
        message = on_success(args.job, health_dir=health_dir)
        if message:
            print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
