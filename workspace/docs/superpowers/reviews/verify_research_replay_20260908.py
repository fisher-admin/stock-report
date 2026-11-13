"""Historical release replay; no production gate edits or scheduled-summary overwrite."""
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

WORKSPACE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKSPACE / "skills/stock-system-orchestrator/scripts"))
import run_post_publish_research as research

OUT = Path(__file__).with_name("research-replay-20260908.json")
DATE = "20260907"
RUN_ID = "stock-20260907T113012Z"
original_gate, original_write, original_stage = research.wait_for_release, research.write_json, research.run_stage
health = research.HEALTH_DIR
scheduled = health / "post_publish_research_run.json"
daily = WORKSPACE / "stock_data/03-working/strategy_research/short_track/daily"

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    orchestrator = json.loads(research.release_paths()["orchestrator"].read_text())
    assert orchestrator["run_id"] == RUN_ID and orchestrator["trade_date"] == DATE
    previous = json.loads(scheduled.read_text())
    assert previous["state"] == "failed" and previous["run_id"] == RUN_ID
    snapshots = sorted(daily.glob(f"*_{DATE}_candidate_snapshot.json"))
    assert len(snapshots) == 4
    frozen_before = {p.name: digest(p) for p in snapshots}
    scheduled_before = digest(scheduled)
    historical_now = datetime.fromisoformat(orchestrator["completed_at"].replace("Z", "+00:00")).astimezone()
    started = time.monotonic()
    started_wall = datetime.now().astimezone()
    progress = {"verification_mode": "historical_real_data_replay", "pid": os.getpid(),
                "run_id": RUN_ID, "trade_date": DATE, "state": "running", "stages": [],
                "started_at_actual": started_wall.isoformat(), "gate_clock": historical_now.isoformat()}

    def gate(*args, **kwargs):
        return original_gate(*args, **{**kwargs, "now_fn": lambda: historical_now, "timeout_s": 0})

    def write(path, payload):
        if Path(path) == scheduled:
            progress.update({k: v for k, v in payload.items() if k != "started_at"})
            progress["updated_at_actual"] = datetime.now().astimezone().isoformat()
            return original_write(OUT, progress)
        return original_write(path, payload)

    def stage(*args, **kwargs):
        progress["current_stage"] = args[0]
        original_write(OUT, progress)
        result = original_stage(*args, **kwargs)
        progress["stages"].append(result)
        progress["elapsed_seconds"] = round(time.monotonic() - started, 3)
        original_write(OUT, progress)
        print(json.dumps({"finished_stage": args[0], "ok": result["ok"],
                          "seconds": result["elapsed_seconds"]}), flush=True)
        return result

    with patch.object(research, "wait_for_release", gate), patch.object(research, "write_json", write), \
         patch.object(research, "run_stage", stage), \
         patch.object(sys, "argv", ["replay", "--trade-date", DATE]):
        code = research.main()
    progress["elapsed_seconds"] = round(time.monotonic() - started, 3)
    progress["current_stage"] = None
    progress["scheduled_failure_preserved"] = digest(scheduled) == scheduled_before
    progress["frozen_snapshots_unchanged"] = frozen_before == {p.name: digest(p) for p in snapshots}
    checks = []
    for path in daily.glob(f"*_{DATE}_candidate_tracking.json"):
        value = json.loads(path.read_text())
        checks.append(value.get("operational_status") == "healthy")
    factor = json.loads((WORKSPACE / "stock_data/03-working/factor_attribution/factor_attribution_state.json").read_text())
    factory = json.loads((health / "prebreakout_factory_latest.json").read_text())
    search = json.loads((WORKSPACE / "stock_data/03-working/strategy_research/prebreakout_shadow/factory/search/search_report_daily_latest.json").read_text())
    progress["artifact_checks"] = {
        "four_healthy_tracks": len(checks) == 4 and all(checks),
        "factor_current": factor.get("as_of") == DATE and factor.get("signal_days_ok", 0) > 0
                          and not factor.get("promotion_eligible")
                          and datetime.fromisoformat(factor["generated_at"]) >= started_wall,
        "factory_current": factory.get("trade_date") == DATE
                           and datetime.fromisoformat(factory["generated_at"]).astimezone() >= started_wall,
        "search_current": search.get("trade_date") == DATE
                          and datetime.fromisoformat(search["generated_at"]).astimezone() >= started_wall,
    }
    verified = code == 0 and all(progress["artifact_checks"].values()) and progress["scheduled_failure_preserved"] and progress["frozen_snapshots_unchanged"]
    progress["verification_passed"] = verified
    original_write(OUT, progress)
    print(json.dumps({"exit": code, "verification_passed": verified, "evidence": str(OUT)}), flush=True)
    return 0 if verified else 1

if __name__ == "__main__":
    raise SystemExit(main())
