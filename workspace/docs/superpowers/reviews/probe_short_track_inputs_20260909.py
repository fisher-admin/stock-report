"""Read-only new-day input probe. No snapshots, settlement, publication or orders."""
import argparse
import faulthandler
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

workspace = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(workspace / "skills/stock-system-orchestrator/scripts"))
import short_track_shadow_runner as runner


def timed(label, function):
    start = time.monotonic()
    print(json.dumps({"begin": label}), flush=True)
    try:
        result = function()
    except Exception as exc:
        print(json.dumps({"end": label, "seconds": round(time.monotonic() - start, 3), "error_type": type(exc).__name__}), flush=True)
        raise
    print(json.dumps({"end": label, "seconds": round(time.monotonic() - start, 3), "rows": len(result) if hasattr(result, "__len__") else None}), flush=True)
    return result


class TracedClient:
    def __init__(self):
        self.client = runner.pl.init_tushare()

    def __getattr__(self, name):
        def call(**kwargs):
            # Only names and date/category selectors; never credentials or raw rows.
            label = name + ":" + json.dumps({key: val for key, val in kwargs.items() if key in {"trade_date", "start_date", "end_date", "list_status", "l1_code"}}, sort_keys=True)
            return timed(label, lambda: getattr(self.client, name)(**kwargs))
        return call


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", required=True)
    args = parser.parse_args()
    td = args.trade_date
    dates = [d for d in runner.load_open_trade_dates(runner.build_paths(workspace).health_dir / "trading_calendar.json") if d <= td][-21:]
    assert len(dates) == 21
    # Read the exact existing industry map, never trigger its on-disk refresh.
    industry_map = json.loads(runner.bt.INDUSTRY_MAP_PATH.read_text())
    faulthandler.dump_traceback_later(60, repeat=True)
    with patch.object(runner.bt, "load_industry_map", return_value=industry_map):
        ranked = timed("score_full_ranked_pool", lambda: runner.score_full_ranked_pool(td, workspace))
    production = runner.load_production_prebreakout_snapshot(workspace)
    if str(production.get("latest_trade_date") or production.get("trade_date") or "") == td:
        expected = runner._normalize_prod_top20(runner._find_prebreakout_strategy(production))
        runner.validate_control_parity(expected, ranked)
        print(json.dumps({"production_control_parity": True}), flush=True)
    client = TracedClient()
    pit = timed("collect_pit_market_snapshot", lambda: runner.pms.collect_pit_market_snapshot(client, td))
    prices = timed("official_stk_history", lambda: runner.fetch_official_stk_factor_history(client, dates))
    basics = timed("official_daily_basic_history", lambda: runner.fetch_official_daily_basic_history(client, dates))
    frame = timed("balanced_feature_frame", lambda: runner.short_track_shadow.build_balanced_feature_frame(price_history=prices, daily_basic_history=basics, pit_universe=pit["universe"], trade_date=td))
    print(json.dumps({"probe_complete": True, "trade_date": td, "ranked_count": len(ranked), "balanced_rows": len(frame), "writes_requested": False}), flush=True)
    faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
