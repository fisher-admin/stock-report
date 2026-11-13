"""Full new-day shadow run with real inputs and isolated writable outputs.

This is a diagnostic replay, never a live release or a clean scheduled day.
"""
import argparse
from dataclasses import replace
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import patch

workspace = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(workspace / "skills/stock-system-orchestrator/scripts"))
from probe_short_track_inputs_20260909 import TracedClient
import short_track_shadow_runner as module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True)
    args = parser.parse_args()
    td = args.trade_date
    paths = module.build_paths(workspace)
    original_short = paths.short_track_daily_dir.parent
    protected = list(original_short.rglob("*")) + [paths.production_strategy_backtests]
    digests = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected if path.is_file()}
    base = paths.health_dir / "audit_sandboxes"
    base.mkdir(exist_ok=True)
    isolated = Path(tempfile.mkdtemp(prefix=f"short-new-day-{td}-", dir=base))
    shadow = isolated / "short_track"
    shutil.copytree(original_short, shadow)
    assert not list(shadow.rglob(f"*{td}*")), "target day already has frozen input; not a new-day test"
    cache = isolated / "backtest_cache"
    cache.mkdir()
    for pattern in ("stk_factor_*.parquet", "daily_*.parquet", "adj_factor_*.parquet"):
        for source in paths.backtest_cache_dir.glob(pattern):
            (cache / source.name).symlink_to(source)
    runner = module.ShortTrackShadowRunner(workspace_dir=workspace, client=TracedClient())
    runner.paths = replace(paths,
        backtest_cache_dir=cache,
        shadow_universe_dir=shadow / "materialized/pit_universe",
        shadow_daily_basic_dir=shadow / "materialized/daily_basic",
        common_pit_market_dir=isolated / "common_pit",
        short_track_daily_dir=shadow / "daily",
        short_track_ledger_dir=shadow / "ledger",
        short_track_portfolio_daily_dir=shadow / "portfolio_daily")

    def guard(event, args):
        if event == "open":
            path, mode, flags = args
            writing = bool(set(str(mode or "")) & set("wax+")) or bool((flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            targets = [path] if writing else []
        elif event in {"os.rename", "os.remove", "os.rmdir", "os.mkdir"}:
            targets = args[:2] if event == "os.rename" else args[:1]
        else:
            return
        for target in targets:
            if isinstance(target, (str, bytes, os.PathLike)) and not Path(os.fsdecode(target)).resolve().is_relative_to(isolated):
                raise RuntimeError(f"diagnostic blocked write outside isolated outputs: {target}")

    sys.addaudithook(guard)
    print(json.dumps({"isolated_output": str(isolated), "trade_date": td, "new_day": True}), flush=True)
    faulthandler.dump_traceback_later(60, repeat=True)
    started = time.monotonic()
    industry_map = json.loads(module.bt.INDUSTRY_MAP_PATH.read_text())
    with patch.object(module.bt, "load_industry_map", return_value=industry_map):
        result = runner.run(td)
    elapsed = time.monotonic() - started
    faulthandler.cancel_dump_traceback_later()
    unchanged = all(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in digests.items())
    current_files = {p for p in original_short.rglob("*") if p.is_file()}
    unchanged = unchanged and current_files == {p for p in digests if p.is_relative_to(original_short)}
    reports = {sid: json.loads(Path(path).read_text()) for sid, path in result["tracking_reports"].items()}
    healthy = len(reports) == 4 and all(r.get("operational_status") == "healthy" for r in reports.values())
    evidence = {"result": result, "elapsed_seconds": round(elapsed, 3), "production_unchanged": unchanged,
                "operational_statuses": {sid: r.get("operational_status") for sid, r in reports.items()},
                "verified": result.get("status") == "ok" and healthy and unchanged and elapsed < 600,
                "acceptance_kind": "isolated_historical_new_day_generation_not_scheduled"}
    (isolated / "verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps(evidence, ensure_ascii=False), flush=True)
    return 0 if evidence["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
