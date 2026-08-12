#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import shadow_portfolio_evaluator as spe  # noqa: E402


def load_runner_module():
    spec = importlib.util.spec_from_file_location("short_track_shadow_runner", SCRIPT_DIR / "short_track_shadow_runner.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_prod_snapshot_rows():
    rows = []
    for rank in range(1, 21):
        rows.append(
            {
                "code": f"{rank:06d}.SZ",
                "ts_code": f"{rank:06d}.SZ",
                "name": f"生产{rank}",
                "industry_name": "银行" if rank <= 10 else "电子",
                "score": round(100.0 - rank / 10, 1),
                "rank": rank,
                "rank_no": rank,
            }
        )
    return rows


def make_scored_rows():
    rows = []
    for rank in range(1, 31):
        rows.append(
            {
                "ts_code": f"{rank:06d}.SZ",
                "stock_code": f"{rank:06d}",
                "name": f"候选{rank}",
                "score": round(100.0 - rank / 10, 1),
                "rank": rank,
                "rank_no": rank,
                "industry_name": "银行" if rank <= 10 else "电子",
                "sw2021_l1_name": "银行" if rank <= 10 else "电子",
            }
        )
    return rows


def make_balanced_frame(trade_date: str = "20260811") -> pd.DataFrame:
    rows = []
    for idx in range(1, 31):
        rows.append(
            {
                "trade_date": trade_date,
                "ts_code": f"{idx:06d}.SZ",
                "name": f"平衡{idx}",
                "industry_name": "银行" if idx <= 12 else "电子",
                "sw2021_l1_name": "银行" if idx <= 12 else "电子",
                "open_qfq": 10.0 + idx / 10,
                "close_qfq": 10.2 + idx / 10,
                "high_qfq": 10.4 + idx / 10,
                "low_qfq": 9.9 + idx / 10,
                "macd_dif": 0.01 * idx,
                "macd_dea": 0.008 * idx,
                "volume_ratio": 1.0 + idx / 50,
                "turnover_rate": 0.8 + idx / 100,
                "ret_5d": idx / 10,
                "ret_20d": idx / 5,
                "amount": 1000000.0 + idx * 1000,
                "amount_ma20": 900000.0 + idx * 900,
                "circ_mv": 500.0 + idx * 10,
                "total_mv": 600.0 + idx * 12,
                "listing_days": 400,
                "list_status": "L",
                "is_st": False,
                "is_suspended": False,
                "universe_flag": 1,
                "used_proxy": False,
                "completeness": "complete",
                "realized_vol_5d": 0.02 + idx / 10000,
                "realized_vol_20d": 0.03 + idx / 10000,
                "macd_hist_prev": 0.001 * idx,
                "volume_cv_20": 0.1 + idx / 1000,
                "turnover_cv_20": 0.08 + idx / 1000,
                "max_abs_return_20": 0.05 + idx / 1000,
                "source": "tushare_pit",
                "provenance": "unit-test",
            }
        )
    return pd.DataFrame(rows)


def make_official_history(trade_dates: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    stk_rows = []
    db_rows = []
    for td in trade_dates:
        for idx in range(1, 31):
            stk_rows.append(
                {
                    "trade_date": td,
                    "ts_code": f"{idx:06d}.SZ",
                    "open_qfq": 10.0 + idx / 10,
                    "close_qfq": 10.2 + idx / 10,
                    "high_qfq": 10.4 + idx / 10,
                    "low_qfq": 9.9 + idx / 10,
                    "macd_dif": 0.01 * idx,
                    "macd_dea": 0.008 * idx,
                    "vol": 100000 + idx,
                    "amount": 1000000.0 + idx * 1000,
                    "source_provider": "tushare",
                    "used_proxy": False,
                    "completeness": "complete",
                }
            )
            db_rows.append(
                {
                    "trade_date": td,
                    "ts_code": f"{idx:06d}.SZ",
                    "close": 10.2 + idx / 10,
                    "turnover_rate": 0.8 + idx / 100,
                    "turnover_rate_f": 0.7 + idx / 100,
                    "volume_ratio": 1.0 + idx / 50,
                    "pe": 10.0,
                    "pe_ttm": 10.5,
                    "pb": 1.0,
                    "ps": 1.1,
                    "ps_ttm": 1.0,
                    "dv_ratio": 0.0,
                    "dv_ttm": 0.0,
                    "total_share": 100.0,
                    "float_share": 90.0,
                    "free_share": 80.0,
                    "total_mv": 600.0 + idx * 12,
                    "circ_mv": 500.0 + idx * 10,
                    "used_proxy": False,
                    "completeness": "complete",
                    "source": "tushare_pit",
                    "provenance": "unit-test",
                }
            )
    return pd.DataFrame(stk_rows), pd.DataFrame(db_rows)


class RunnerClient:
    def __init__(self, trade_date: str):
        self.trade_date = trade_date

    def stock_basic(self, *, list_status: str, fields=None):
        return pd.DataFrame(
            {
                "ts_code": [f"{idx:06d}.SZ" for idx in range(1, 31)],
                "symbol": [f"{idx:06d}" for idx in range(1, 31)],
                "name": [f"样本{idx}" for idx in range(1, 31)],
                "industry": ["银行" if idx <= 15 else "电子" for idx in range(1, 31)],
                "market": ["主板"] * 30,
                "list_date": ["20200101"] * 30,
                "delist_date": [""] * 30,
                "list_status": [list_status] * 30,
            }
        ) if list_status == "L" else pd.DataFrame(
            columns=["ts_code", "symbol", "name", "industry", "market", "list_date", "delist_date", "list_status"]
        )

    def stock_st(self, *, trade_date: str):
        return pd.DataFrame({"ts_code": []})

    def suspend_d(self, *, trade_date: str):
        return pd.DataFrame({"ts_code": []})

    def daily_basic(self, *, trade_date: str, fields=None):
        _, daily_basic = make_official_history([trade_date])
        return daily_basic[daily_basic["trade_date"] == trade_date].copy()

    def index_classify(self, *, src: str, level: str):
        return pd.DataFrame(
            {
                "index_code": ["801780.SI", "801080.SI"],
                "industry_name": ["银行", "电子"],
                "level": ["L1", "L1"],
                "src": ["SW2021", "SW2021"],
            }
        )

    def index_member_all(self, *, l1_code: str):
        rows = []
        for idx in range(1, 31):
            row_l1_code = "801780.SI" if idx <= 12 else "801080.SI"
            if row_l1_code != l1_code:
                continue
            rows.append(
                {
                    "l1_code": row_l1_code,
                    "l1_name": "银行" if idx <= 12 else "电子",
                    "ts_code": f"{idx:06d}.SZ",
                    "in_date": "20200101",
                    "out_date": "",
                    "is_new": "Y",
                }
            )
        return pd.DataFrame(rows)

    def trade_cal(self, *, exchange: str, start_date: str, end_date: str, fields: str | None = None):
        all_dates = pd.bdate_range("2026-07-14", "2026-08-20")
        return pd.DataFrame(
            {
                "cal_date": [d.strftime("%Y%m%d") for d in all_dates],
                "is_open": [1] * len(all_dates),
                "pretrade_date": [all_dates[max(i - 1, 0)].strftime("%Y%m%d") for i in range(len(all_dates))],
            }
        )

    def stk_factor(self, *, trade_date: str):
        stk, _ = make_official_history([trade_date])
        return stk[stk["trade_date"] == trade_date].copy()


class ShortTrackShadowRunnerTests(unittest.TestCase):
    def test_immutable_parquet_accepts_logically_equal_nullable_dtypes(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "universe.parquet"
            first = pd.DataFrame(
                {
                    "trade_date": pd.Series(["20260810"], dtype="string"),
                    "ts_code": pd.Series(["000001.SZ"], dtype="string"),
                    "industry": pd.Series([None], dtype="string"),
                }
            )
            logically_equal = pd.DataFrame(
                {
                    "trade_date": pd.Series(["20260810"], dtype=object),
                    "ts_code": pd.Series(["000001.SZ"], dtype=object),
                    "industry": pd.Series([None], dtype=object),
                }
            )

            module._compare_or_write_parquet(
                path,
                first,
                ["trade_date", "ts_code"],
            )
            module._compare_or_write_parquet(
                path,
                logically_equal,
                ["trade_date", "ts_code"],
            )

    def test_ranked_pool_can_omit_unmapped_industry_without_proxy_fill(self):
        module = load_runner_module()
        rows = make_scored_rows()[:2]
        universe = pd.DataFrame(
            {
                "ts_code": [rows[0]["ts_code"], rows[1]["ts_code"]],
                "industry_name": ["银行", "中成药"],
                "sw2021_l1_name": ["银行", None],
            }
        )

        attached = module.attach_pit_industry(rows, universe, omit_missing=True)

        self.assertEqual([row["ts_code"] for row in attached], [rows[0]["ts_code"]])
        self.assertEqual(attached[0]["industry_name"], "银行")

    def test_runner_fails_when_control_top20_parity_breaks(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            prod = {"latest_trade_date": "20260811", "strategies": [{"id": "prebreakout_v41", "top20": make_prod_snapshot_rows()}]}
            runner = module.ShortTrackShadowRunner(workspace_dir=workspace, client=RunnerClient("20260811"))
            with mock.patch.object(module, "load_production_prebreakout_snapshot", return_value=prod), \
                mock.patch.object(module, "score_full_ranked_pool", return_value=make_scored_rows()[:-1] + [dict(make_scored_rows()[-1], score=1.0)]):
                with self.assertRaises(module.RunnerInputError):
                    runner.run("20260811")

    def test_runner_fails_when_less_than_21_trade_days(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            prod = {"latest_trade_date": "20260811", "strategies": [{"id": "prebreakout_v41", "top20": make_prod_snapshot_rows()}]}
            runner = module.ShortTrackShadowRunner(workspace_dir=workspace, client=RunnerClient("20260811"))
            short_dates = [f"202607{day:02d}" for day in range(1, 10)]
            with mock.patch.object(module, "load_production_prebreakout_snapshot", return_value=prod), \
                mock.patch.object(module, "score_full_ranked_pool", return_value=make_scored_rows()), \
                mock.patch.object(module, "load_open_trade_dates", return_value=short_dates):
                with self.assertRaises(module.RunnerInputError):
                    runner.run("20260811")

    def test_runner_writes_shadow_outputs_without_touching_production(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            prod_path = workspace / "stock_data/03-working/stock-report-repo/data/strategy_backtests.json"
            prod_path.parent.mkdir(parents=True, exist_ok=True)
            prod = {"latest_trade_date": "20260811", "strategies": [{"id": "prebreakout_v41", "top20": make_prod_snapshot_rows()}]}
            prod_path.write_text(json.dumps(prod, ensure_ascii=False), encoding="utf-8")
            health_path = workspace / "stock_data/03-working/health/data_preparation_run.json"
            health_path.parent.mkdir(parents=True, exist_ok=True)
            health_path.write_text(
                json.dumps(
                    {
                        "target_trade_date": "20260811",
                        "ok": True,
                        "quality_ok": True,
                        "cyq_perf_proxy_derived": False,
                        "quality_errors": [],
                    }
                ),
                encoding="utf-8",
            )
            runner = module.ShortTrackShadowRunner(workspace_dir=workspace, client=RunnerClient("20260811"))
            open_dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-07-14", "2026-08-20")]
            stk_hist, db_hist = make_official_history(open_dates[-21:])
            with mock.patch.object(module, "load_production_prebreakout_snapshot", return_value=prod), \
                mock.patch.object(module, "score_full_ranked_pool", return_value=make_scored_rows()), \
                mock.patch.object(module, "load_open_trade_dates", return_value=open_dates), \
                mock.patch.object(module, "fetch_official_stk_factor_history", return_value=stk_hist), \
                mock.patch.object(module, "fetch_official_daily_basic_history", return_value=db_hist), \
                mock.patch.object(module.short_track_shadow, "build_balanced_feature_frame", return_value=make_balanced_frame()):
                result = runner.run("20260811")
            self.assertEqual(result["status"], "ok")
            short_dir = workspace / "stock_data/03-working/strategy_research/short_track/daily"
            self.assertTrue(any(short_dir.glob("*candidate_snapshot.json")))
            self.assertEqual(json.loads(prod_path.read_text(encoding="utf-8")), prod)
            self.assertFalse((workspace / "factor_factory/data/universe/universe_20260811.parquet").exists())
            self.assertFalse((workspace / "stock_data/03-working/backtest_cache/daily_basic_20260811.parquet").exists())
            materialized = workspace / "stock_data/03-working/strategy_research/short_track/materialized"
            self.assertTrue((materialized / "pit_universe/universe_20260811.parquet").exists())
            self.assertTrue((materialized / "daily_basic/daily_basic_20260811.parquet").exists())
            portfolio_dir = workspace / "stock_data/03-working/strategy_research/short_track/portfolio_daily"
            self.assertEqual(len(list(portfolio_dir.glob("*_portfolio_daily.parquet"))), 3)

    def test_runner_ledger_is_idempotent(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runner = module.ShortTrackShadowRunner(workspace_dir=workspace, client=RunnerClient("20260811"))
            snapshot = {
                "strategy_id": "prebreakout_v43_control",
                "strategy_version": "4.3+8a5054a13fc32f0e",
                "signal_date": "20260811",
                "signal_data_cutoff": "2026-08-11T15:00:00+08:00",
                "planned_entry_time": "2026-08-12T09:30:00+08:00",
                "holding_period_days": 5,
                "diagnostic_holding_period_days": [1, 3],
                "used_proxy": False,
                "completeness_status": "complete",
                "round_trip_cost": 0.003,
                "stress_round_trip_cost": 0.005,
                "benchmark": "all_a_tradable_equal_weight",
                "settlement_status": "pending_settlement",
                "rank_change": 0,
                "publish_mode": "observe_only",
                "candidates": [
                    {"ts_code": "000001.SZ", "rank": 1, "industry": "银行", "weight": 0.5},
                    {"ts_code": "000002.SZ", "rank": 2, "industry": "电子", "weight": 0.5},
                ],
            }
            ledger1 = runner.update_ledger("prebreakout_v43_control", snapshot, ["20260811", "20260812", "20260813", "20260814", "20260817", "20260818"])
            ledger2 = runner.update_ledger("prebreakout_v43_control", snapshot, ["20260811", "20260812", "20260813", "20260814", "20260817", "20260818"])
            self.assertEqual(len(ledger1), len(ledger2))
            self.assertEqual(len(ledger2), 2)

    def test_stage3_marks_runner_as_noncritical_attached_failure(self):
        stage3_path = SCRIPT_DIR / "stage3_strategy_suite.py"
        text = stage3_path.read_text(encoding="utf-8")
        self.assertIn("short_track_shadow", text)
        self.assertIn("attached_failed_steps", text)
        self.assertNotIn("CRITICAL_STEPS = {'prebreakout_suite', 'strategy_publication_layer', 'short_track_shadow'}", text)

    def test_runner_tracking_report_observe_only_contract(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runner = module.ShortTrackShadowRunner(workspace_dir=workspace, client=RunnerClient("20260811"))
            ledger = spe.pending_rows_from_snapshot(
                {
                    "strategy_id": "prebreakout_v43_control",
                    "strategy_version": "4.3+8a5054a13fc32f0e",
                    "signal_date": "20260811",
                    "signal_data_cutoff": "2026-08-11T15:00:00+08:00",
                    "planned_entry_time": "2026-08-12T09:30:00+08:00",
                    "holding_period_days": 5,
                    "diagnostic_holding_period_days": [1, 3],
                    "used_proxy": False,
                    "completeness_status": "complete",
                    "round_trip_cost": 0.003,
                    "stress_round_trip_cost": 0.005,
                    "benchmark": "all_a_tradable_equal_weight",
                    "settlement_status": "pending_settlement",
                    "rank_change": 0,
                    "publish_mode": "observe_only",
                    "candidates": [
                        {"ts_code": "000001.SZ", "rank": 1, "industry": "银行", "weight": 0.5},
                        {"ts_code": "000002.SZ", "rank": 2, "industry": "电子", "weight": 0.5},
                    ],
                },
                existing=pd.DataFrame(),
                open_trade_dates=["20260811", "20260812", "20260813", "20260814", "20260817", "20260818"],
            )
            report = runner.build_candidate_tracking_report(
                "prebreakout_v43_control",
                "4.3+8a5054a13fc32f0e",
                ledger,
                expected_signal_dates=["20260811"],
                operational_ok=True,
                operational_evidence={"snapshot_path": "/tmp/x.json"},
            )
            self.assertEqual(report["execution_authority"], "observe_only_no_auto_order")
            self.assertIn("operational_status", report)
            self.assertIn("effectiveness_status", report)

    def test_atomic_writers_never_leave_partial_final_artifacts(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            json_path = root / "snapshot.json"
            with mock.patch.object(module.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    module._compare_or_write_json(json_path, {"ok": True})
            self.assertFalse(json_path.exists())

            parquet_path = root / "ledger.parquet"
            with mock.patch.object(module.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    module._write_parquet_atomic(parquet_path, pd.DataFrame({"x": [1]}))
            self.assertFalse(parquet_path.exists())


if __name__ == "__main__":
    unittest.main()
