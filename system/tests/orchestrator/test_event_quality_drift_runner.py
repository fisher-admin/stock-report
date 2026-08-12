#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import event_quality_drift_runner as runner_module
from test_event_quality_drift_v1 import make_inputs


class QualityClient:
    def __init__(self, quality: pd.DataFrame):
        self.quality = quality.copy()

    def fina_indicator(self, *, ts_code: str, start_date: str, end_date: str, fields=None):
        return self.quality[self.quality["ts_code"] == ts_code].copy()


def _calendar() -> list[str]:
    return [date.strftime("%Y%m%d") for date in pd.bdate_range("2024-10-01", periods=180)]


def _market_snapshot(valuation: pd.DataFrame, universe: pd.DataFrame) -> dict:
    daily = valuation.copy()
    daily["trade_date"] = "20250103"
    daily["used_proxy"] = False
    daily["completeness"] = "complete"
    universe_frame = universe.copy()
    universe_frame["trade_date"] = "20250103"
    return {
        "trade_date": "20250103",
        "daily_basic": daily,
        "universe": universe_frame,
        "metadata": {
            "trade_date": "20250103",
            "source": "tushare_pit",
            "used_proxy": False,
            "completeness": "complete",
        },
    }


class EventQualityDriftRunnerTests(unittest.TestCase):
    def test_immutable_parquet_accepts_logically_equal_nullable_dtypes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshot.parquet"
            first = pd.DataFrame(
                {
                    "ts_code": pd.Series(["000001.SZ", None], dtype="string"),
                    "sw2021_l1_name": pd.Series(["银行", None], dtype="string"),
                }
            )
            logically_equal = pd.DataFrame(
                {
                    "ts_code": pd.Series(["000001.SZ", None], dtype=object),
                    "sw2021_l1_name": pd.Series(["银行", None], dtype=object),
                }
            )

            runner_module._atomic_parquet(path, first, immutable=True)
            runner_module._atomic_parquet(path, logically_equal, immutable=True)

    def _workspace(self, root: Path) -> tuple[runner_module.EventQualityDriftRunner, dict]:
        events, quality, valuation, universe = make_inputs()
        pit_path = (
            root
            / "stock_data/03-working/fundamental_cache/pit/pit_yjyg.parquet"
        )
        pit_path.parent.mkdir(parents=True, exist_ok=True)
        events.to_parquet(pit_path, index=False)
        calendar_path = root / "stock_data/03-working/health/trading_calendar.json"
        calendar_path.parent.mkdir(parents=True, exist_ok=True)
        calendar_path.write_text(
            json.dumps({"source": "test", "open_dates": _calendar()}),
            encoding="utf-8",
        )
        runner = runner_module.EventQualityDriftRunner(
            workspace_dir=root,
            client=QualityClient(quality),
        )
        return runner, _market_snapshot(valuation, universe)

    def test_daily_run_writes_immutable_snapshot_pool_ledger_and_observe_only_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runner, market = self._workspace(workspace)
            with mock.patch.object(
                runner_module.pms,
                "collect_pit_market_snapshot",
                return_value=market,
            ):
                first = runner.run_signal_date("20250103", allow_stale_research=True)
                second = runner.run_signal_date("20250103", allow_stale_research=True)

            self.assertEqual(first["status"], "ok")
            self.assertEqual(first, second)
            snapshot = json.loads(Path(first["snapshot_path"]).read_text(encoding="utf-8"))
            self.assertEqual(snapshot["strategy_id"], "event_quality_drift_v1")
            self.assertEqual(snapshot["evidence_scope"], "auxiliary_only")
            self.assertFalse(snapshot["revision_chain_complete"])
            self.assertEqual(snapshot["execution_authority"], "observe_only_no_auto_order")
            self.assertEqual(snapshot["rank_change"], 0)
            self.assertLessEqual(snapshot["portfolio_position_count"], 20)
            self.assertTrue(all(row["rank_change"] == 0 for row in snapshot["candidates"]))

            ledger = pd.read_parquet(first["ledger_path"])
            self.assertEqual(len(ledger), len(snapshot["ranked_events"]))
            self.assertEqual(int(ledger["is_selected"].sum()), len(snapshot["candidates"]))
            self.assertTrue(ledger["return_20d_net"].isna().all())
            self.assertEqual(set(ledger["settlement_status"]), {"pending"})
            active = runner.active_positions_for_entry(
                ledger,
                entry_trade_date=snapshot["planned_entry_time"][:10].replace("-", ""),
                open_trade_dates=_calendar(),
            )
            self.assertEqual(len(active), len(snapshot["candidates"]))

            report = json.loads(Path(first["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["decision"], "observe_only")
            self.assertFalse(report["all_gates_pass"])
            self.assertEqual(report["execution_authority"], "observe_only_no_auto_order")
            self.assertFalse((workspace / "stock_data/03-working/backtest_cache").exists())

    def test_missing_new_events_is_an_explicit_noop_not_a_stale_recommendation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runner, market = self._workspace(workspace)
            with mock.patch.object(
                runner_module.pms,
                "collect_pit_market_snapshot",
                return_value=market,
            ):
                result = runner.run_signal_date("20250104", allow_stale_research=True)
            self.assertEqual(result["status"], "no_new_announcement_events")
            self.assertNotIn("snapshot_path", result)
            self.assertTrue(Path(result["pit_universe_path"]).exists())

    def test_new_but_untradable_event_is_explicitly_skipped_not_a_failed_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runner, market = self._workspace(workspace)
            market["universe"]["tradable"] = 0
            market["universe"]["universe_flag"] = 0
            with mock.patch.object(
                runner_module.pms,
                "collect_pit_market_snapshot",
                return_value=market,
            ):
                result = runner.run_signal_date("20250103", allow_stale_research=True)

            self.assertEqual(result["status"], "no_eligible_announcement_events")
            self.assertEqual(result["execution_authority"], "observe_only_no_auto_order")
            report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["decision"], "observe_only")
            self.assertFalse(report["all_gates_pass"])
            self.assertIn("no_eligible_announcement_events", report["failed_gates"])

    def test_live_mode_refuses_to_backdate_an_entry_after_the_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner, market = self._workspace(Path(tmpdir))
            with mock.patch.object(
                runner_module.pms,
                "collect_pit_market_snapshot",
                return_value=market,
            ), self.assertRaises(runner_module.RunnerInputError):
                runner.run_signal_date("20250103", allow_stale_research=False)

    def test_replay_dates_include_each_open_day_and_weekend_announcement_day(self):
        events = pd.DataFrame(
            {
                "announce_date": ["20250103", "20250104", "20250104", "20250106"],
            }
        )
        dates = runner_module.replay_signal_dates(
            start_date="20250103",
            end_date="20250106",
            open_trade_dates=["20250103", "20250106"],
            pit_events=events,
        )

        self.assertEqual(dates, ["20250103", "20250104", "20250106"])

    def test_replay_range_is_chronological_and_always_marks_stale_research(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runner, _ = self._workspace(Path(tmpdir))
            with mock.patch.object(
                runner, "run_signal_date", side_effect=lambda date, **kwargs: {
                    "signal_date": date,
                    "historical": kwargs["allow_stale_research"],
                }
            ) as run_one, mock.patch.object(
                runner,
                "open_trade_dates",
                return_value=["20250103", "20250106"],
            ), mock.patch.object(
                runner,
                "load_pit_events",
                return_value=pd.DataFrame({"announce_date": ["20250104"]}),
            ):
                result = runner.run_replay("20250103", "20250106")

            self.assertEqual(result["processed_signal_dates"], 3)
            self.assertEqual(
                [call.args[0] for call in run_one.call_args_list],
                ["20250103", "20250104", "20250106"],
            )
            self.assertTrue(
                all(call.kwargs["allow_stale_research"] for call in run_one.call_args_list)
            )


if __name__ == "__main__":
    unittest.main()
