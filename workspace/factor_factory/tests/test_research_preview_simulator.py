#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "research_preview_simulator.py"


def load_module():
    spec = importlib.util.spec_from_file_location("research_preview_simulator", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ResearchPreviewSimulatorTests(unittest.TestCase):
    def test_run_simulation_executes_t_plus_1_and_publishes_cost_basis(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            module.OUTPUT_ROOT = Path(tmpdir)
            module.TOP_N = 1
            panel = pd.DataFrame(
                {
                    "trade_date": ["20260102", "20260102", "20260103", "20260103"],
                    "ts_code": ["000001.SZ", "000002.SZ", "000001.SZ", "000002.SZ"],
                    "open": [100.0, 100.0, 200.0, 100.0],
                    "close": [120.0, 110.0, 220.0, 90.0],
                    "open_qfq": [10.0, 10.0, 20.0, 10.0],
                    "close_qfq": [12.0, 11.0, 22.0, 9.0],
                    "high": [12.0, 11.0, 22.0, 10.0],
                    "low": [9.5, 9.5, 19.5, 8.5],
                    "pct_chg": [2.0, 1.0, 3.0, -1.0],
                    "amount": [100000.0, 100000.0, 100000.0, 100000.0],
                    "avg_amount_lookback": [100000.0, 100000.0, 100000.0, 100000.0],
                    "industry": ["银行", "软件", "银行", "软件"],
                    "g_intraday_range_expansion": [5.0, 1.0, 0.0, 0.0],
                }
            )
            module.load_panel = lambda: panel.copy()

            module.run_simulation(weights={"g_intraday_range_expansion": 1.0})

            daily = json.loads((module.OUTPUT_ROOT / "daily_log_20260103.json").read_text(encoding="utf-8"))
            cumulative = json.loads((module.OUTPUT_ROOT / "cumulative_stats.json").read_text(encoding="utf-8"))
            self.assertEqual(daily["signal_trade_date"], "20260102")
            self.assertEqual(daily["trade_date"], "20260103")
            self.assertEqual(daily["selected_stocks"][0]["code"], "000001.SZ")
            self.assertAlmostEqual(daily["portfolio_o2c_return"], 0.10, places=6)
            self.assertAlmostEqual(daily["portfolio_o2c_return_net_base"], 0.097, places=6)
            self.assertAlmostEqual(daily["portfolio_o2c_return_net_stress"], 0.095, places=6)
            self.assertEqual(daily["price_basis"], "qfq")
            self.assertEqual(cumulative["price_basis"], "qfq")
            self.assertAlmostEqual(cumulative["round_trip_cost_base"], 0.003, places=9)
            self.assertAlmostEqual(cumulative["round_trip_cost_stress"], 0.005, places=9)
            self.assertEqual(cumulative["total_days"], 1)

    def test_run_simulation_raises_on_nonfinite_execution_return(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            module.OUTPUT_ROOT = Path(tmpdir)
            module.TOP_N = 1
            panel = pd.DataFrame(
                {
                    "trade_date": ["20260102", "20260102", "20260103", "20260103"],
                    "ts_code": ["000001.SZ", "000002.SZ", "000001.SZ", "000002.SZ"],
                    "open": [100.0, 100.0, 200.0, 100.0],
                    "close": [120.0, 110.0, np.inf, 90.0],
                    "open_qfq": [10.0, 10.0, 20.0, 10.0],
                    "close_qfq": [12.0, 11.0, np.inf, 9.0],
                    "high": [12.0, 11.0, 22.0, 10.0],
                    "low": [9.5, 9.5, 19.5, 8.5],
                    "pct_chg": [2.0, 1.0, 3.0, -1.0],
                    "amount": [100000.0, 100000.0, 100000.0, 100000.0],
                    "avg_amount_lookback": [100000.0, 100000.0, 100000.0, 100000.0],
                    "industry": ["银行", "软件", "银行", "软件"],
                    "g_intraday_range_expansion": [5.0, 1.0, 0.0, 0.0],
                }
            )
            module.load_panel = lambda: panel.copy()

            with self.assertRaises(ValueError):
                module.run_simulation(weights={"g_intraday_range_expansion": 1.0})
            self.assertFalse((module.OUTPUT_ROOT / "cumulative_stats.json").exists())

    def test_run_simulation_uses_window_local_execution_dates(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            module.OUTPUT_ROOT = Path(tmpdir)
            module.TOP_N = 1
            panel = pd.DataFrame(
                {
                    "trade_date": ["20260102", "20260103", "20260104"],
                    "ts_code": ["000001.SZ", "000001.SZ", "000001.SZ"],
                    "open_qfq": [10.0, 11.0, 12.0],
                    "close_qfq": [10.5, 11.5, 12.6],
                    "open": [100.0, 110.0, 120.0],
                    "close": [105.0, 115.0, 126.0],
                    "high": [10.6, 11.6, 12.7],
                    "low": [9.9, 10.9, 11.9],
                    "pct_chg": [1.0, 2.0, 3.0],
                    "amount": [100000.0, 100000.0, 100000.0],
                    "avg_amount_lookback": [100000.0, 100000.0, 100000.0],
                    "industry": ["银行", "银行", "银行"],
                    "g_intraday_range_expansion": [0.1, 5.0, 0.0],
                }
            )
            module.load_panel = lambda: panel.copy()

            module.run_simulation(
                weights={"g_intraday_range_expansion": 1.0},
                start_date="20260103",
                end_date="20260104",
            )

            daily = json.loads((module.OUTPUT_ROOT / "daily_log_20260104.json").read_text(encoding="utf-8"))
            self.assertEqual(daily["signal_trade_date"], "20260103")
            self.assertEqual(daily["trade_date"], "20260104")
            self.assertAlmostEqual(daily["portfolio_o2c_return"], 0.05, places=6)

    def test_run_simulation_requires_qfq_prices(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            module.OUTPUT_ROOT = Path(tmpdir)
            module.TOP_N = 1
            panel = pd.DataFrame(
                {
                    "trade_date": ["20260102", "20260102", "20260103", "20260103"],
                    "ts_code": ["000001.SZ", "000002.SZ", "000001.SZ", "000002.SZ"],
                    "open": [10.0, 10.0, 20.0, 10.0],
                    "close": [12.0, 11.0, 22.0, 9.0],
                    "high": [12.0, 11.0, 22.0, 10.0],
                    "low": [9.5, 9.5, 19.5, 8.5],
                    "pct_chg": [2.0, 1.0, 3.0, -1.0],
                    "amount": [100000.0, 100000.0, 100000.0, 100000.0],
                    "avg_amount_lookback": [100000.0, 100000.0, 100000.0, 100000.0],
                    "industry": ["银行", "软件", "银行", "软件"],
                    "g_intraday_range_expansion": [5.0, 1.0, 0.0, 0.0],
                }
            )
            module.load_panel = lambda: panel.copy()

            with self.assertRaises(ValueError):
                module.run_simulation(weights={"g_intraday_range_expansion": 1.0})

    def test_run_simulation_logs_friday_to_monday_planned_entry_time(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            module.OUTPUT_ROOT = Path(tmpdir)
            module.TOP_N = 1
            panel = pd.DataFrame(
                {
                    "trade_date": ["20260807", "20260810"],
                    "ts_code": ["000001.SZ", "000001.SZ"],
                    "open_qfq": [10.0, 10.5],
                    "close_qfq": [10.2, 10.8],
                    "open": [100.0, 105.0],
                    "close": [102.0, 108.0],
                    "high": [10.3, 10.9],
                    "low": [9.9, 10.4],
                    "pct_chg": [1.0, 2.0],
                    "amount": [100000.0, 100000.0],
                    "avg_amount_lookback": [100000.0, 100000.0],
                    "industry": ["银行", "银行"],
                    "g_intraday_range_expansion": [5.0, 0.0],
                }
            )
            module.load_panel = lambda: panel.copy()

            module.run_simulation(weights={"g_intraday_range_expansion": 1.0})
            daily = json.loads((module.OUTPUT_ROOT / "daily_log_20260810.json").read_text(encoding="utf-8"))

        self.assertEqual(daily["signal_trade_date"], "20260807")
        self.assertEqual(daily["planned_entry_time"], "2026-08-10T09:30:00+08:00")

    def test_validate_cumulative_stats_allows_long_histories(self):
        module = load_module()
        stats = {
            "total_days": 401,
            "o2c_total_return": 0.25,
            "win_rate": 0.2,
        }
        module.validate_cumulative_stats(stats)


if __name__ == "__main__":
    unittest.main()
