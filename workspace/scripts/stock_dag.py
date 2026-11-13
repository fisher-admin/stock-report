#!/usr/bin/env python3
"""Trade-date dependency-graph executor for the stock chain (phase 3, 2026-10-30).

Replaces "ten cron jobs at fixed clock times guessing whether upstream ran" with
one state machine per open trade date.  A Hermes heartbeat calls ``tick`` every
five minutes; each step starts once its earliest time has passed and its
upstream steps are done, retries per policy, is marked skipped (silently) when
an upstream failed, and missed (with one alert) when its deadline passes.
Forward-only steps are never run after their deadline.

Modes (config/stock_dag.json "mode"):
  shadow - launch nothing; read the real Hermes runs and record when the
           executor *would* have started or skipped each step, for comparison.
  live   - launch the existing wrapper scripts and relay their output.

State: stock_data/03-working/health/dag/<trade_date>.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
WORKSPACE = Path(os.environ.get("STOCK_WORKSPACE") or "/Users/fisher/.openclaw/workspace")
CONFIG = WORKSPACE / "config" / "stock_dag.json"
HEALTH = WORKSPACE / "stock_data" / "03-working" / "health"
STATE_DIR = HEALTH / "dag"
CALENDAR = HEALTH / "trading_calendar.json"
HERMES = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
SCRIPTS = HERMES / "scripts"
HERMES_OUTPUT = HERMES / "cron" / "output"
RELAY_LINES = 40

TERMINAL = {"success", "failed", "skipped", "missed"}


# ---------------------------------------------------------------- calendar/time
def open_dates(calendar_path: Path = CALENDAR) -> list[str]:
    data = json.loads(calendar_path.read_text())
    dates = sorted(str(d) for d in data.get("open_dates") or [])
    if not dates:
        raise RuntimeError("trading calendar has no open dates")
    return dates


def at(date: str, hhmm: str) -> datetime:
    return datetime.strptime(f"{date} {hhmm}", "%Y%m%d %H:%M").replace(tzinfo=TZ)


def next_open(date: str, dates: list[str]) -> str:
    later = [d for d in dates if d > date]
    if not later:
        raise RuntimeError(f"calendar has no open day after {date}")
    return later[0]


def window(step: dict, cycle: str, dates: list[str]) -> tuple[datetime, datetime]:
    earliest = at(cycle, step["earliest"])
    deadline = str(step["deadline"])
    if deadline.startswith("next_open "):
        end = at(next_open(cycle, dates), deadline.split(" ", 1)[1])
    else:
        end = at(cycle, deadline)
    return earliest, end


def active_cycles(now: datetime, dates: list[str]) -> list[str]:
    """Trade dates whose steps may still be pending: the previous and current open day."""
    today = now.strftime("%Y%m%d")
    past = [d for d in dates if d < today]
    cycles = past[-1:] + ([today] if today in dates else [])
    return cycles


# ---------------------------------------------------------------------- state
def load_state(cycle: str, steps: list[dict]) -> dict:
    path = STATE_DIR / f"{cycle}.json"
    state = json.loads(path.read_text()) if path.is_file() else {"trade_date": cycle, "steps": {}}
    for step in steps:
        state["steps"].setdefault(step["id"], {"status": "waiting", "attempts": 0})
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{state['trade_date']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ------------------------------------------------------------------ decisions
def upstream_verdict(step: dict, steps_state: dict) -> str:
    """'ready', 'wait', or 'skip' (an upstream failed / was skipped / missed)."""
    for dep in step.get("depends_on") or []:
        status = steps_state[dep]["status"]
        if status in {"failed", "skipped", "missed"}:
            return "skip"
        if status != "success":
            return "wait"
    for dep in step.get("after") or []:
        if steps_state[dep]["status"] not in TERMINAL:
            return "wait"
    return "ready"


def decide(step: dict, record: dict, steps_state: dict, now: datetime, earliest: datetime,
           deadline: datetime) -> str:
    """Next action for one step: none / start / retry / skip / miss."""
    status = record["status"]
    if status in TERMINAL or status == "running":
        return "none"
    if now >= deadline:
        return "miss"
    verdict = upstream_verdict(step, steps_state)
    if verdict == "skip":
        return "skip"
    if verdict == "wait" or now < earliest:
        return "none"
    if status == "retry_wait":
        due = datetime.fromisoformat(record["retry_at"])
        return "retry" if now >= due else "none"
    return "start"


# -------------------------------------------------------------- shadow (observe)
RUN_TIME = re.compile(r"\*\*Run Time:\*\* (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def observed_runs(job_id: str, start: datetime, end: datetime) -> list[dict]:
    """Real Hermes runs of ``job_id`` that finished inside [start, end]."""
    runs = []
    folder = HERMES_OUTPUT / job_id
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        head = path.read_text(errors="replace")[:600]
        match = RUN_TIME.search(head)
        if not match:
            continue
        finished = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        if start <= finished <= end:
            runs.append({"finished_at": iso(finished),
                         "status": "failed" if "script failed" in head else "success"})
    return runs


def shadow_tick(step: dict, record: dict, steps_state: dict, now: datetime, earliest: datetime,
                deadline: datetime) -> None:
    """Mirror the real run into the state; note what the executor would have done."""
    action = decide(step, record, steps_state, now, earliest, deadline)
    if action in {"start", "retry"} and "would_start_at" not in record:
        record["would_start_at"] = iso(now)
    if action == "skip" and "would_skip_at" not in record:
        record["would_skip_at"] = iso(now)
    runs = observed_runs(step["hermes_job"], earliest - timedelta(minutes=30), min(now, deadline))
    if runs:
        record["observed"] = runs
        last = runs[-1]
        if last["status"] == "success" or len(runs) >= int((step.get("retry") or {}).get("max_attempts", 1)):
            record["status"] = last["status"]
            record["finished_at"] = last["finished_at"]
    elif now >= deadline and record["status"] not in TERMINAL:
        record["status"] = "missed"


# ----------------------------------------------------------------- live (launch)
def launch(step: dict, cycle: str, record: dict, now: datetime) -> None:
    run_dir = STATE_DIR / "runs" / cycle
    run_dir.mkdir(parents=True, exist_ok=True)
    attempt = record["attempts"] + 1
    log = run_dir / f"{step['id']}.{attempt}.log"
    rc_file = run_dir / f"{step['id']}.{attempt}.rc"
    script = SCRIPTS / step["script"]
    command = f'bash "{script}" >"{log}" 2>&1; echo $? >"{rc_file}"'
    process = subprocess.Popen(["/bin/bash", "-c", command], cwd=str(WORKSPACE),
                               start_new_session=True, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record.update(status="running", attempts=attempt, pid=process.pid, started_at=iso(now),
                  log=str(log), rc_file=str(rc_file))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def collect(step: dict, record: dict, now: datetime, messages: list[str]) -> None:
    rc_file = Path(record["rc_file"])
    if rc_file.is_file():
        rc = int((rc_file.read_text().strip() or "-1"))
    elif alive(int(record["pid"])):
        return
    else:
        rc = -1  # died without writing a return code
    record.update(rc=rc, finished_at=iso(now))
    output = Path(record["log"]).read_text(errors="replace").strip() if Path(record["log"]).is_file() else ""
    retry = step.get("retry") or {}
    if rc == 0:
        record["status"] = "success"
        if output:
            messages.append("\n".join(output.splitlines()[-RELAY_LINES:]))
        return
    if record["attempts"] < int(retry.get("max_attempts", 1)):
        record["status"] = "retry_wait"
        record["retry_at"] = iso(now + timedelta(minutes=int(retry.get("delay_minutes", 60))))
        messages.append(f"🔁 【执行器】{step['id']} 失败（退出码 {rc}），{record['retry_at'][11:16]} 自动重试。")
        return
    record["status"] = "failed"
    text = output or f"🚨 【执行器】{step['id']} 失败（退出码 {rc}）"
    messages.append("\n".join(text.splitlines()[-RELAY_LINES:]))


# ------------------------------------------------------------------------ tick
def tick(now: datetime | None = None, config_path: Path = CONFIG) -> list[str]:
    now = now or datetime.now(TZ)
    config = json.loads(config_path.read_text())
    mode = config.get("mode", "shadow")
    steps = config["steps"]
    dates = open_dates()
    messages: list[str] = []
    today = now.strftime("%Y%m%d")
    for cycle in active_cycles(now, dates):
        if cycle != today and not (STATE_DIR / f"{cycle}.json").is_file():
            continue  # never judge a past day the executor did not track (first run)
        state = load_state(cycle, steps)
        state["mode"] = mode
        skipped_now, missed_now = [], []
        for step in steps:  # config order is topological
            record = state["steps"][step["id"]]
            earliest, deadline = window(step, cycle, dates)
            if mode == "shadow":
                shadow_tick(step, record, state["steps"], now, earliest, deadline)
                continue
            if record["status"] == "running":
                collect(step, record, now, messages)
            action = decide(step, record, state["steps"], now, earliest, deadline)
            if action in {"start", "retry"}:
                launch(step, cycle, record, now)
            elif action == "skip":
                record.update(status="skipped", finished_at=iso(now))
                skipped_now.append(step["id"])
            elif action == "miss":
                record.update(status="missed", finished_at=iso(now))
                if not step.get("advisory"):
                    missed_now.append(step["id"] + ("（只前瞻，记为缺口）" if step.get("forward_only") else ""))
        if skipped_now:
            messages.append(f"⏭️ 【执行器】{cycle} 因上游失败跳过：{'、'.join(skipped_now)}（根因见上方告警）")
        if missed_now:
            messages.append(f"⏰ 【执行器】{cycle} 错过截止仍未完成：{'、'.join(missed_now)}")
        state["updated_at"] = iso(now)
        save_state(state)
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["tick", "status"])
    args = parser.parse_args(argv)
    if args.command == "status":
        for path in sorted(STATE_DIR.glob("*.json"))[-2:]:
            state = json.loads(path.read_text())
            print(state["trade_date"], {k: v["status"] for k, v in state["steps"].items()})
        return 0
    for message in tick():
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
