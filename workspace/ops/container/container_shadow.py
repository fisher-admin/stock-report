#!/usr/bin/env python3
"""Parallel comparison: run a job's command in the container next to the host run.

Phase 4A switches jobs from the host to the container image one at a time,
each only after its container run has matched the host for a while.  The
wrapper calls this twice, both best-effort (never changes the job's result):

  container_shadow.py run  --job J [--capture F] -- CMD...   # container first
  ... host run ...
  printf "%s" "$OUT" | container_shadow.py host --job J --rc N [--capture F]

The container run goes first so the host run rewrites any shared health file
last; ``--capture`` snapshots such a file after each run for the comparison.
Record: stock_data/03-working/health/container_shadow/<YYYYMMDD>/<job>.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path(os.environ.get("STOCK_WORKSPACE") or "/Users/fisher/.openclaw/workspace")
SHADOW_DIR = WORKSPACE / "stock_data/03-working/health/container_shadow"
RUN_SH = WORKSPACE / "ops/container/run.sh"
DOCKER = os.environ.get("DOCKER") or shutil.which("docker") or "/usr/local/bin/docker"
VOLATILE_KEYS = {"generated_at", "updated_at", "started_at", "finished_at", "elapsed_seconds", "duration_seconds"}


def record_path(job: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    return SHADOW_DIR / now.strftime("%Y%m%d") / f"{job}.json"


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def snapshot(capture: str | None):
    if not capture:
        return None
    path = Path(capture) if Path(capture).is_absolute() else WORKSPACE / capture
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {"_unreadable": f"{type(exc).__name__}"}


def strip_volatile(value):
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    return value


def differences(container: dict, host: dict) -> list[str]:
    diffs = []
    if container.get("rc") != host.get("rc"):
        diffs.append(f"rc: container={container.get('rc')} host={host.get('rc')}")
    if (container.get("stdout") or "").strip() != (host.get("stdout") or "").strip():
        diffs.append("stdout differs")
    c_cap, h_cap = strip_volatile(container.get("capture")), strip_volatile(host.get("capture"))
    if c_cap is not None or h_cap is not None:
        if isinstance(c_cap, dict) and isinstance(h_cap, dict):
            for key in sorted(set(c_cap) | set(h_cap)):
                if c_cap.get(key) != h_cap.get(key):
                    diffs.append(f"capture.{key} differs")
        elif c_cap != h_cap:
            diffs.append("capture differs")
    return diffs


def docker_ready() -> bool:
    try:
        return subprocess.run([DOCKER, "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run_container(job: str, command: list[str], capture: str | None, timeout: int) -> dict:
    started = time.monotonic()
    side: dict = {"at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")}
    if not docker_ready():
        side["status"] = "docker_unavailable"
        return side
    env = {**os.environ, "DOCKER": DOCKER}
    try:
        proc = subprocess.run(["bash", str(RUN_SH), *command], capture_output=True, text=True,
                              timeout=timeout, cwd=str(WORKSPACE), env=env)
        side.update(status="ran", rc=proc.returncode, stdout=proc.stdout[-4000:], stderr=proc.stderr[-2000:])
    except subprocess.TimeoutExpired:
        side["status"] = "timeout"
    side["seconds"] = round(time.monotonic() - started, 1)
    side["capture"] = snapshot(capture)
    return side


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("side", choices=["run", "host"])
    parser.add_argument("--job", required=True)
    parser.add_argument("--rc", type=int)
    parser.add_argument("--capture")
    parser.add_argument("--timeout", type=int, default=300)
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    path = record_path(args.job)
    if args.side == "run":
        record = {"job": args.job, "container": run_container(args.job, command, args.capture, args.timeout)}
        save(path, record)
        return 0
    record = load(path) or {"job": args.job}
    record["host"] = {"at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                      "rc": args.rc, "stdout": sys.stdin.read()[-4000:], "capture": snapshot(args.capture)}
    container = record.get("container") or {}
    if container.get("status") == "ran":
        record["diffs"] = differences(container, record["host"])
        record["match"] = not record["diffs"]
    else:
        record["match"] = None  # container side did not run; not evidence either way
    save(path, record)
    return 0


if __name__ == "__main__":
    sys.exit(main())
