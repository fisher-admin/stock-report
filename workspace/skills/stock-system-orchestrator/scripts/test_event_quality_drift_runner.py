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
    def test_strategy_copy_and_selection_follow_the_shared_frozen_file(self):
        # Regression 2026-10-20: the 22:15 job froze universe_20261019 in the shared store; the 08:15 refetch had
        # 3 different is_suspended flags and was frozen separately in the strategy store -> settlement fail closed.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shared, strategy = root / "shared" / "universe_x.parquet", root / "strategy" / "universe_x.parquet"
            shared.parent.mkdir()
            first = pd.DataFrame({"trade_date": ["20261019"] * 2, "ts_code": ["601059.SH", "603759.SH"],
                                  "is_suspended": [True, False]})
            first.to_parquet(shared, index=False)
            refetch = first.assign(is_suspended=[False, True])

            used = runner_module._freeze_shared_then_strategy(shared, strategy, refetch)

            pd.testing.assert_frame_equal(used, first)
            pd.testing.assert_frame_equal(pd.read_parquet(strategy), first)
            pd.testing.assert_frame_equal(pd.read_parquet(shared), first)
            self.assertTrue(list((shared.parent / "revisions").glob("universe_x.rev-*.parquet")))

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

    def test_immutable_parquet_keeps_first_write_when_vendor_floats_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "daily_basic.parquet"
            first = pd.DataFrame({"ts_code": ["000001.SZ"], "dv_ratio": [2.4409]})
            drifted = pd.DataFrame({"ts_code": ["000001.SZ"], "dv_ratio": [2.4435]})
            runner_module._atomic_parquet(path, first, immutable=True)
            runner_module._atomic_parquet(path, drifted, immutable=True)
            kept = pd.read_parquet(path)
            self.assertAlmostEqual(float(kept.iloc[0]["dv_ratio"]), 2.4409)

    def test_immutable_json_keeps_first_write_when_payload_drifts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshot.json"
            first = {"strategy_id": "event_quality_drift_v1", "stamp": "first"}
            drifted = {"strategy_id": "event_quality_drift_v1", "stamp": "second"}
            runner_module._atomic_json(path, first, immutable=True)
            runner_module._atomic_json(path, drifted, immutable=True)
            kept = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(kept["stamp"], "first")

    def test_load_prices_fills_ipo_day_from_daily_without_overwriting_stk_factor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            cache = workspace / "stock_data/03-working/backtest_cache"
            cache.mkdir(parents=True)
            pd.DataFrame(
                {
                    "trade_date": ["20260813"],
                    "ts_code": ["920138.BJ"],
                    "open_qfq": [51.01],
                    "close_qfq": [46.59],
                    "close": [46.59],
                    "adj_factor": [1.0],
                }
            ).to_parquet(cache / "stk_factor_20260813.parquet", index=False)
            pd.DataFrame(
                {
                    "trade_date": ["20260812", "20260813"],
                    "ts_code": ["920138.BJ", "920138.BJ"],
                    "open": [53.0, 99.0],
                    "close": [53.26, 99.0],
                }
            ).to_parquet(cache / "daily_20260812.parquet", index=False)
            pd.DataFrame({"trade_date": ["20260812"], "ts_code": ["920138.BJ"], "adj_factor": [1.0]}).to_parquet(cache / "adj_factor_20260812.parquet", index=False)
            runner = runner_module.EventQualityDriftRunner(
                workspace_dir=workspace,
                client=QualityClient(pd.DataFrame()),
            )
            prices = runner._load_prices()
            ipo = prices[(prices["trade_date"] == "20260812") & (prices["ts_code"] == "920138.BJ")].iloc[0]
            nxt = prices[(prices["trade_date"] == "20260813") & (prices["ts_code"] == "920138.BJ")].iloc[0]
            self.assertEqual(float(ipo["open_qfq"]), 53.0)
            self.assertEqual(float(ipo["close_qfq"]), 53.26)
            self.assertEqual(float(nxt["open_qfq"]), 51.01)
            self.assertEqual(float(nxt["close_qfq"]), 46.59)

    def test_universe_history_fills_missing_event_days_from_common_pit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            event_dir = workspace / "stock_data/03-working/strategy_research/event_quality_drift_v1/materialized/pit_universe"
            common_dir = workspace / "stock_data/03-working/fundamental_cache/pit_market/v2"
            event_dir.mkdir(parents=True)
            common_dir.mkdir(parents=True)
            pd.DataFrame(
                {"trade_date": ["20260812"], "ts_code": ["000001.SZ"], "universe_flag": [1], "tradable": [1]}
            ).to_parquet(event_dir / "universe_20260812.parquet", index=False)
            pd.DataFrame(
                {"trade_date": ["20260813"], "ts_code": ["000001.SZ"], "universe_flag": [1], "tradable": [1]}
            ).to_parquet(common_dir / "universe_20260813.parquet", index=False)
            runner = runner_module.EventQualityDriftRunner(
                workspace_dir=workspace,
                client=QualityClient(pd.DataFrame()),
            )
            history = runner._load_universe_history()
            self.assertEqual(sorted(history["trade_date"].astype(str).unique()), ["20260812", "20260813"])

    def test_corrected_inputs_do_not_overwrite_legacy_event_materialization(self):
        paths = runner_module.build_paths(Path('/example'))
        self.assertEqual(paths.materialized_universe.name, 'v2')
        self.assertEqual(paths.materialized_valuation.name, 'v2')

    def test_settlement_rejects_cross_version_changes_to_tradability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = runner_module.EventQualityDriftRunner(workspace_dir=root, client=QualityClient(pd.DataFrame()))
            legacy = root / 'stock_data/03-working/strategy_research/event_quality_drift_v1/materialized/pit_universe'
            current = legacy / 'v2'
            current.mkdir(parents=True)
            old = pd.DataFrame({'trade_date': ['20260812'], 'ts_code': ['000001.SZ'], 'universe_flag': [1], 'tradable': [1]})
            old.to_parquet(legacy / 'universe_20260812.parquet', index=False)
            changed = old.copy(); changed['tradable'] = 0
            changed.to_parquet(current / 'universe_20260812.parquet', index=False)
            with self.assertRaises(runner_module.RunnerInputError):
                runner._load_universe_history()

    def test_settlement_allows_only_future_noneligible_additions_and_keeps_first_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = runner_module.EventQualityDriftRunner(workspace_dir=Path(tmp), client=QualityClient(pd.DataFrame()))
            first_dir = runner.paths.materialized_universe
            later_dir = runner.paths.common_pit_market
            first_dir.mkdir(parents=True)
            later_dir.mkdir(parents=True)
            old = pd.DataFrame({"trade_date": ["20260929"], "ts_code": ["000001.SZ"],
                                "universe_flag": [1], "tradable": [1], "is_suspended": [0], "list_date": ["19910403"]})
            future = {"trade_date": "20260929", "ts_code": "001246.SZ", "universe_flag": 0,
                      "tradable": 0, "is_suspended": 0, "list_date": "20260930"}
            first = first_dir / "universe_20260929.parquet"
            later = later_dir / first.name
            old.to_parquet(first, index=False)
            original = first.read_bytes()
            pd.concat([old, pd.DataFrame([future])], ignore_index=True).to_parquet(later, index=False)
            loaded = runner._load_universe_history()
            pd.testing.assert_frame_equal(loaded.reset_index(drop=True), old)
            self.assertEqual(first.read_bytes(), original)

    def test_future_addition_exception_cannot_hide_other_snapshot_changes(self):
        base = pd.DataFrame({"trade_date": ["20260929"], "ts_code": ["000001.SZ"],
                             "universe_flag": [1], "tradable": [1], "is_suspended": [0], "list_date": ["19910403"]})
        good = {"trade_date": "20260929", "ts_code": "001246.SZ", "universe_flag": 0,
                "tradable": 0, "is_suspended": 0, "list_date": "20260930"}
        cases = [{**good, "list_date": date} for date in ["20260928", "20260929", "20261301", "", None]]
        cases += [{**good, key: value} for key in ["universe_flag", "tradable"]
                  for value in [1, "", None, float("nan"), float("inf"), False, "bad"]]
        for extra in cases:
            with self.subTest(extra=extra):
                candidate = pd.concat([base.astype({"universe_flag": object, "tradable": object}),
                                       pd.DataFrame([extra], dtype=object)], ignore_index=True)
                with self.assertRaises(AssertionError):
                    runner_module._assert_settlement_universe_equivalent(base, candidate)
        candidate = pd.concat([base, pd.DataFrame([good])], ignore_index=True)
        for field in ["universe_flag", "tradable", "is_suspended"]:
            changed = candidate.copy()
            changed.loc[0, field] = 1 if field == "is_suspended" else 0
            with self.subTest(field=field), self.assertRaises(AssertionError):
                runner_module._assert_settlement_universe_equivalent(base, changed)
        changed = candidate.astype({"tradable": float})
        changed.loc[0, "tradable"] = 1.000001
        with self.assertRaises(AssertionError):
            runner_module._assert_settlement_universe_equivalent(base, changed)
        with self.assertRaises(AssertionError):
            runner_module._assert_settlement_universe_equivalent(candidate, base)
        second = {**good, "ts_code": "001247.SZ", "list_date": "20261001"}
        runner_module._assert_settlement_universe_equivalent(
            base, pd.concat([candidate, pd.DataFrame([second])], ignore_index=True))
        bad = {**second, "list_date": "20260929"}
        with self.assertRaises(AssertionError):
            runner_module._assert_settlement_universe_equivalent(
                base, pd.concat([candidate, pd.DataFrame([bad])], ignore_index=True))
        with self.assertRaises(runner_module.evaluator.EvaluationContractError):
            runner_module._assert_settlement_universe_equivalent(
                base, pd.concat([candidate, pd.DataFrame([good])], ignore_index=True))

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
