#!/usr/bin/env python3
"""Memory guard: stop runaway Python/ffmpeg jobs before macOS panics.

2026-10-25 11:23 and 2026-10-26 11:43 (Asia/Shanghai) this 16 GB Mac
kernel-panicked ("watchdog timeout: no checkins from watchdogd", compressor
"LOW swap space") while ad-hoc Python process pools (video analysis/render,
not the stock system) held 25-47 GiB resident. With FileVault on, the reboot
stopped at the unlock screen and every Hermes job stayed down until login.

Runs under launchd (ai.openclaw.memory-guard). Every SAMPLE_SECONDS it reads
the kernel memory pressure level and swap; after TRIGGER_SAMPLES consecutive
critical samples it terminates the process group of Python/ffmpeg processes
with the largest resident memory. The stock system (~/.openclaw), Hermes
(~/.hermes) and this guard are never touched. Each action is logged to
stock_data/03-working/health/memory_guard.jsonl and announced on Discord.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path(os.environ.get("STOCK_WORKSPACE") or "/Users/fisher/.openclaw/workspace")
HEALTH_DIR = WORKSPACE / "stock_data/03-working/health"
EVENT_LOG = HEALTH_DIR / "memory_guard.jsonl"
STATE_FILE = HEALTH_DIR / "memory_guard_state.json"
DISCORD_TARGET = os.environ.get("MEMORY_GUARD_DISCORD", "discord:1492058190492467230")

SAMPLE_SECONDS = 10
TRIGGER_SAMPLES = 2          # ~20 s of sustained pressure; watchdogd panics after 90 s of stall
COOLDOWN_SECONDS = 30
TERM_GRACE_SECONDS = 5
MIN_VICTIM_BYTES = 1 << 30   # never kill a group smaller than 1 GiB; that is not the culprit
LOW_SWAP_BYTES = 512 << 20
PRESSURE_WARN, PRESSURE_CRITICAL = 2, 4

TARGET_NAMES = re.compile(r"^(python(\d(\.\d+)?)?|ffmpeg)$", re.IGNORECASE)
PROTECTED_MARKERS = ("/.openclaw/", "/.hermes/", "memory_guard")


def now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def sysctl(name: str) -> str:
    return subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True).stdout.strip()


def parse_swap_free(text: str) -> int | None:
    match = re.search(r"free\s*=\s*([\d.]+)([KMG])", text)
    if not match:
        return None
    return int(float(match.group(1)) * {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[match.group(2)])


def sample() -> dict:
    try:
        level = int(sysctl("kern.memorystatus_vm_pressure_level") or 0)
    except ValueError:
        level = 0
    return {"pressure_level": level, "swap_free": parse_swap_free(sysctl("vm.swapusage"))}


def is_critical(s: dict) -> bool:
    if s["pressure_level"] >= PRESSURE_CRITICAL:
        return True
    swap_free = s.get("swap_free")
    return s["pressure_level"] >= PRESSURE_WARN and swap_free is not None and swap_free < LOW_SWAP_BYTES


def list_processes() -> list[dict]:
    out = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,rss=,comm=,args="],
                         capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        pid, pgid, rss, comm = parts[:4]
        procs.append({"pid": int(pid), "pgid": int(pgid), "rss": int(rss) * 1024,
                      "name": os.path.basename(comm), "args": parts[4] if len(parts) > 4 else comm})
    return procs


def choose_victim(procs: list[dict], self_pid: int) -> dict | None:
    """Largest-RSS process group among unprotected Python/ffmpeg processes."""
    groups: dict[int, list[dict]] = {}
    for p in procs:
        if p["pid"] == self_pid or not TARGET_NAMES.match(p["name"]):
            continue
        if any(marker in p["args"] for marker in PROTECTED_MARKERS):
            continue
        groups.setdefault(p["pgid"], []).append(p)
    if not groups:
        return None
    pgid, members = max(groups.items(), key=lambda item: sum(p["rss"] for p in item[1]))
    total = sum(p["rss"] for p in members)
    if total < MIN_VICTIM_BYTES:
        return None
    return {"pgid": pgid, "rss": total, "members": members}


def terminate(victim: dict) -> list[int]:
    pids = [p["pid"] for p in victim["members"]]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(TERM_GRACE_SECONDS)
    killed = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except ProcessLookupError:
            pass
    return killed


def record(event: dict) -> None:
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG.open("a") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def announce(text: str) -> None:
    subprocess.run(["/usr/bin/osascript", "-e", f'display notification "{text[:180]}" with title "内存守护"'],
                   capture_output=True, timeout=10)
    hermes = Path.home() / ".local/bin/hermes"
    cmd = [str(hermes) if hermes.exists() else "hermes", "send", "-q", "-t", DISCORD_TARGET, text]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def describe(victim: dict) -> str:
    top = max(victim["members"], key=lambda p: p["rss"])
    cmd = top["args"][:160]
    return (f"{len(victim['members'])} 个进程（pgid {victim['pgid']}）共 {victim['rss'] / (1 << 30):.1f} GiB；"
            f"最大者 pid {top['pid']}：{cmd}")


def main() -> int:
    self_pid = os.getpid()
    streak = 0
    last_action = 0.0
    while True:
        s = sample()
        streak = streak + 1 if is_critical(s) else 0
        STATE_FILE.write_text(json.dumps({"at": now(), "streak": streak, **s}, ensure_ascii=False))
        if streak >= TRIGGER_SAMPLES and time.monotonic() - last_action >= COOLDOWN_SECONDS:
            victim = choose_victim(list_processes(), self_pid)
            last_action = time.monotonic()
            if victim is None:
                record({"at": now(), "action": "none", "reason": "no unprotected python/ffmpeg group >= 1 GiB", **s})
            else:
                killed = terminate(victim)
                event = {"at": now(), "action": "terminated", "pgid": victim["pgid"], "rss": victim["rss"],
                         "pids": [p["pid"] for p in victim["members"]], "sigkill": killed,
                         "commands": sorted({p["args"][:200] for p in victim["members"]})[:5], **s}
                record(event)
                announce(f"🧯 【内存守护】内存压力临界（level {s['pressure_level']}），已终止 {describe(victim)}。"
                         "这是为防止系统 panic 重启（10-25/10-26 两次事故）；股票系统与 Hermes 不受影响。")
            streak = 0
        time.sleep(SAMPLE_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
