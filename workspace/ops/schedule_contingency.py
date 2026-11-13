#!/usr/bin/env python3
"""17:30 live field test + pre-authorised revert to the proven 19:30 evening chain (2026-10-10 directive).

Run by a one-shot Hermes no-agent job every 10 minutes from 18:00 to 23:50 on the field-test day.
Empty stdout = silent; anything printed is delivered as the job's notification.

Trigger (sticky once set, evaluated from 18:00):
  data preparation for the field-test date is not complete by 18:00, or completed with T-1 staleness /
  degraded sources / proxy chip data.
Revert timing (avoids same-day duplicate runs):
  - no publication for the date, pipeline not running, before 19:20  -> revert now (19:30 chain rescues today)
  - otherwise                                                        -> revert at the first run >= 22:50
Revert: `hermes cron edit` back to the backed-up expressions (jobs.json.bak-20261010-2hshift) and
config/stock_schedule_overrides.env restoring the watchdog deadlines (20:30 / 22:15).
State: stock_data/03-working/health/schedule_contingency_<date>.json (idempotent).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

WS = Path(os.environ.get("STOCK_SYSTEM_WORKSPACE", Path.home() / ".openclaw" / "workspace"))
HEALTH = WS / "stock_data" / "03-working" / "health"
OVERRIDES = WS / "config" / "stock_schedule_overrides.env"
JOBS_FILE = Path.home() / ".hermes" / "cron" / "jobs.json"
REVERT = {  # job id -> (shifted 2026-10-10, proven original)
    "5dce7c4c3c59": ("30 17 * * 1-5", "30 19 * * 1-5"),
    "c250ca5da3c6": ("0 18 * * 1-5", "0 20 * * 1-5"),
    "8533a2e1d789": ("45 18 * * 1-5", "45 20 * * 1-5"),
    "4d45dc1d3ec9": ("5 19 * * 1-5", "5 21 * * 1-5"),
    "037730684d14": ("15 20 * * 1-5", "15 22 * * 1-5"),
}
OVERRIDE_TEXT = (
    "# 由 ops/schedule_contingency.py 写入：晚间链已回退到 19:30。删除本文件即恢复脚本默认值（18:30 / 20:15）。\n"
    "WATCHDOG_SAME_DAY_DEADLINE=20:30\nWATCHDOG_POST_RESEARCH_DUE=22:15\n"
)


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def lock_active() -> bool:
    sys.path.insert(0, str(WS / "skills" / "stock-system-orchestrator" / "scripts"))
    try:
        from production_release_gate import pipeline_lock_active
    except Exception:  # noqa: BLE001 — be conservative: unknown == running
        return True
    return pipeline_lock_active(HEALTH / "stock_pipeline_gate.lockdir")


def assess(date: str, now: datetime) -> dict:
    dp = load(HEALTH / "data_preparation_run.json")
    latest = dp.get("latest_by_type") or {}
    same_date = str(dp.get("target_trade_date") or "") == date
    complete = (same_date and dp.get("ok") is True and dp.get("same_day_ready") is True
                and not dp.get("degraded") and not dp.get("cyq_perf_proxy_derived")
                and bool(latest) and all(str(v) == date for v in latest.values()))
    stale = same_date and dp.get("ok") is True and not complete
    gen = str(dp.get("generated_at") or "")
    cutoff = f"{date[:4]}-{date[4:6]}-{date[6:]} 18:00:00"
    receipt = load(HEALTH / "deployment_receipt.json")
    return {
        "checked_at": now.isoformat(timespec="seconds"), "data_prep_target": dp.get("target_trade_date"),
        "data_prep_ok": dp.get("ok"), "data_prep_generated_at": gen, "latest_by_type": latest,
        "complete": complete, "ready_by_18": complete and gen <= cutoff, "stale_or_degraded": stale,
        "published": str(receipt.get("trade_date") or "") == date and receipt.get("remote_confirmed") is True,
    }


def hermes_bin() -> str:
    return shutil.which("hermes") or str(Path.home() / ".local" / "bin" / "hermes")


def revert(dry_run: bool) -> list[str]:
    notes = []
    for job_id, (_new, old) in REVERT.items():
        if dry_run:
            notes.append(f"[dry-run] hermes cron edit {job_id} --schedule '{old}'")
            continue
        proc = subprocess.run([hermes_bin(), "cron", "edit", job_id, "--schedule", old],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"hermes cron edit {job_id} failed: {proc.stderr.strip()[:200]}")
    if not dry_run:
        OVERRIDES.write_text(OVERRIDE_TEXT, encoding="utf-8")
        jobs = {j["id"]: j for j in load(JOBS_FILE).get("jobs", [])}
        bad = [i for i, (_n, old) in REVERT.items() if (jobs.get(i, {}).get("schedule") or {}).get("expr") != old]
        if bad:
            raise RuntimeError(f"jobs.json not reverted for {bad}")
        notes += [f"{jobs[i]['name']} → {old}（下次 {jobs[i].get('next_run_at')}）" for i, (_n, old) in REVERT.items()]
    return notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True, help="field-test trade date, e.g. 20261012")
    ap.add_argument("--now", default="", help="testing: override local time (YYYY-MM-DD HH:MM)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert-now", action="store_true", help="manual: revert immediately")
    args = ap.parse_args(argv)
    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M").astimezone() if args.now else datetime.now().astimezone()
    state_path = HEALTH / f"schedule_contingency_{args.date}.json"
    state = load(state_path)
    if state.get("reverted_at") or state.get("passed_at"):
        return 0
    if not args.revert_now:
        if now.strftime("%Y%m%d") != args.date or now.strftime("%H:%M") < "18:00":
            return 0
    a = assess(args.date, now)
    state.setdefault("history", []).append(a)

    def save():
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.revert_now and not state.get("triggered_at"):
        if a["ready_by_18"]:
            state["passed_at"] = a["checked_at"]
            save()
            print(f"✅ 【17:30 实盘验证通过】{args.date} 当日 daily/stk_factor/cyq_perf 于 {a['data_prep_generated_at']} 就绪"
                  f"（早于 18:00），继续保持 17:30 晚间链。")
            return 0
        state["triggered_at"] = a["checked_at"]
        state["trigger_reason"] = ("T-1 陈旧或降级数据" if a["stale_or_degraded"]
                                   else "数据准备 18:00 前未完成（疑似 cyq_perf 晚到）")

    hhmm = now.strftime("%H:%M")
    running = lock_active()
    rescue = not a["published"] and not running and hhmm < "19:20"
    if args.revert_now or rescue or hhmm >= "22:50":
        try:
            notes = revert(args.dry_run)
        except Exception as exc:  # noqa: BLE001
            state["revert_error"] = str(exc)
            save()
            print(f"🚨 【晚间链回退失败】{exc}\n请手动执行：hermes cron edit（见 ops/schedule_contingency.py REVERT 表）")
            return 1
        state["reverted_at"] = a["checked_at"]
        state["revert_mode"] = "manual" if args.revert_now else ("rescue_19_30_today" if rescue else "after_chain_22_50")
        save()
        print(f"⚠️ 【晚间链已回退至 19:30】触发原因：{state.get('trigger_reason', '手动')}\n"
              f"方式：{state['revert_mode']}；数据准备 {a['data_prep_generated_at'] or '未完成'}，已发布={a['published']}\n"
              + "\n".join(notes) + "\n看门狗阈值已恢复 20:30 / 22:15（config/stock_schedule_overrides.env）。")
        return 0
    if not state.get("deferral_announced"):
        state["deferral_announced"] = a["checked_at"]
        save()
        print(f"⚠️ 【17:30 实盘验证未通过】{state['trigger_reason']}；数据准备={a['data_prep_generated_at'] or '未完成'}，"
              f"已发布={a['published']}，主管线运行中={running}。为避免当日重复运行，将于 22:50 后自动回退至 19:30 链。")
        return 0
    save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
