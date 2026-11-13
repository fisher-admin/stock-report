#!/usr/bin/env python3
"""One automatic retry of a failed evening chain (Hermes job, weekdays 21:20).

Transient causes (late cyq_perf 10-12, git push timeout 09-09, provider 503)
used to cost the whole evening because nothing reran the 19:30 pipeline until
a human did. This reruns it once per trade date when the 19:30 run failed and
nothing holds the pipeline lock, then sends the evening broadcast. Silent when
the 19:30 run already succeeded. Deterministic failures fail again quickly and
the alert policy reports them as a repeat, not a new incident.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path(os.environ.get("STOCK_WORKSPACE") or "/Users/fisher/.openclaw/workspace")
HEALTH_DIR = WORKSPACE / "stock_data/03-working/health"
ORCHESTRATOR_RUN = HEALTH_DIR / "orchestrator_run.json"
PIPELINE_LOCK = HEALTH_DIR / "stock_pipeline_gate.lockdir"
HERMES_SCRIPTS = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser() / "scripts/stock"
PIPELINE_WRAPPER = HERMES_SCRIPTS / "stock_pipeline_gate.sh"
BROADCAST_WRAPPER = HERMES_SCRIPTS / "daily_ai_publish.sh"


def _pid_alive(pid: int) -> bool:
    """Portable liveness probe (no external `kill` binary in slim Linux images)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def lock_held(lockdir: Path = PIPELINE_LOCK) -> bool:
    owner = lockdir / "owner.txt"
    if not owner.exists():
        return lockdir.exists()
    pid = owner.read_text().split("|", 1)[0].strip()
    return pid.isdigit() and _pid_alive(int(pid))


def decide(trade_date: str, manifest: dict, *, locked: bool, marker: Path) -> str:
    if str(manifest.get("target_trade_date") or "") == trade_date and manifest.get("status") == "succeeded":
        return "already_ok"
    if str(manifest.get("target_trade_date") or "") == trade_date and manifest.get("failure_category") == "market_halt":
        return "market_halt"  # business rule: a rerun would halt again
    if locked:
        return "running"
    if marker.exists():
        return "already_retried"
    return "retry"


def run(script: Path) -> tuple[int, str]:
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, cwd=str(WORKSPACE))
    return proc.returncode, (proc.stdout or "").strip()


def main() -> int:
    trade_date = today()
    marker = HEALTH_DIR / f"evening_retry_{trade_date}.json"
    try:
        manifest = json.loads(ORCHESTRATOR_RUN.read_text())
    except (OSError, ValueError):
        manifest = {}
    decision = decide(trade_date, manifest, locked=lock_held(), marker=marker)
    if decision != "retry":
        return 0

    record = {"trade_date": trade_date, "started_at": datetime.now().isoformat(timespec="seconds"),
              "failed_run_id": manifest.get("run_id"), "failed_status": manifest.get("status")}
    marker.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(f"🔁 【晚间链自动补跑】19:30 主管线未成功（{manifest.get('run_id') or '无记录'}），自动重跑一次。")
    rc, out = run(PIPELINE_WRAPPER)
    record.update(pipeline_rc=rc)
    if out:
        print(out)
    if rc == 0:
        b_rc, b_out = run(BROADCAST_WRAPPER)
        record.update(broadcast_rc=b_rc)
        if b_out:
            print(b_out)
    record["finished_at"] = datetime.now().isoformat(timespec="seconds")
    marker.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if rc == 0 and record.get("broadcast_rc") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
