#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
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
                    "adj_factor": 1.0,
                    "close": 10.2 + idx / 10,
                    "open": 10.0 + idx / 10,
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
    def test_evaluation_error_cannot_be_reported_as_healthy(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            runner = module.ShortTrackShadowRunner(workspace_dir=Path(tmp), client=mock.Mock())
            report = runner.build_candidate_tracking_report(
                "test", "1", pd.DataFrame(), expected_signal_dates=[], operational_ok=True,
                operational_evidence={"portfolio_eval_error": "missing official price"}, as_of_date="20260904")
        self.assertEqual(report["operational_status"], "degraded_observation")

    def test_cli_preserves_evaluation_failure_exit_code(self):
        module = load_runner_module()
        with mock.patch.object(module.pl, "init_tushare"), mock.patch.object(
            module.ShortTrackShadowRunner, "run", return_value={"status": "failed"}
        ), mock.patch.object(module.sys, "stderr"):
            self.assertEqual(module.main(["--trade-date", "20260904"]), 1)

    def test_complete_frozen_day_is_loaded_for_resume_without_upstream_rebuild(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = module.build_paths(Path(tmpdir))
            paths.short_track_daily_dir.mkdir(parents=True)
            strategy_ids = (
                module.short_track_shadow.CONTROL_STRATEGY_ID,
                module.short_track_shadow.TOP15_STRATEGY_ID,
                module.short_track_shadow.BALANCED_STRATEGY_ID,
                module.short_track_shadow.CROSS_SECTIONAL_STRATEGY_ID,
            )
            for strategy_id in strategy_ids:
                registry = module.short_track_shadow.STRATEGY_REGISTRY[strategy_id]
                version = registry["strategy_version"]
                count = registry["max_names"]
                snapshot = {
                    "artifact_kind": "candidate_snapshot",
                    "artifact_type": "candidate_snapshot",
                    "strategy_id": strategy_id,
                    "strategy_version": version,
                    "trade_date": "20260904",
                    "signal_date": "20260904",
                    "signal_data_cutoff": "2026-09-04T15:00:00+08:00",
                    "execution_authority": "observe_only_no_auto_order",
                    "used_proxy": False,
                    "rank_change": 0,
                    "completeness_status": "complete",
                    "publish_mode": "observe_only",
                    "observe_only": True,
                    "config_hash": registry.get("config_hash", registry.get("expected_config_hash")),
                    "input_hash": "input",
                    "candidates": [
                        {
                            "rank": index,
                            "rank_no": index,
                            "ts_code": f"{index:06d}.SZ",
                            "weight": 1.0 / count,
                            "strategy_id": strategy_id,
                            "strategy_version": version,
                            "used_proxy": False,
                            "rank_change": 0,
                        } for index in range(1, count + 1)
                    ],
                }
                snapshot["rows"] = list(snapshot["candidates"])
                (paths.short_track_daily_dir / f"{strategy_id}_20260904_candidate_snapshot.json").write_text(
                    json.dumps(snapshot),
                    encoding="utf-8",
                )
            paths.shadow_universe_dir.mkdir(parents=True)
            paths.shadow_daily_basic_dir.mkdir(parents=True)
            pd.DataFrame(
                {
                    "trade_date": ["20260904"] * 20,
                    "ts_code": [f"{index:06d}.SZ" for index in range(1, 21)],
                    "universe_flag": [1] * 20,
                    "tradable": [1] * 20,
                }
            ).to_parquet(paths.shadow_universe_dir / "universe_20260904.parquet", index=False)
            pd.DataFrame(
                {
                    "trade_date": ["20260904"],
                    "ts_code": ["000001.SZ"],
                    "volume_ratio": [0.74],
                }
            ).to_parquet(paths.shadow_daily_basic_dir / "daily_basic_20260904.parquet", index=False)
            paths.common_pit_market_dir.mkdir(parents=True)
            for directory, name in ((paths.shadow_universe_dir, "universe"), (paths.shadow_daily_basic_dir, "daily_basic")):
                frozen = directory / f"{name}_20260904.parquet"
                pd.read_parquet(frozen).to_parquet(paths.common_pit_market_dir / frozen.name, index=False)

            state = module.load_frozen_run_state(paths, "20260904")
            self.assertEqual(state, module.load_frozen_run_state(paths, "20260904"))
            snapshot_path = paths.short_track_daily_dir / f"{strategy_ids[0]}_20260904_candidate_snapshot.json"
            original = snapshot_path.read_text()
            changed = json.loads(original)
            changed["candidates"] = changed["candidates"][:1]
            changed["candidates"][0]["weight"] = 1.0
            changed["rows"] = changed["candidates"]
            snapshot_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(module.RunnerInputError, "candidate count"):
                module.load_frozen_run_state(paths, "20260904")
            snapshot_path.write_text(original)
            changed = json.loads(original)
            changed["input_hash"] = "changed-but-still-present"
            snapshot_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(module.RunnerInputError, "integrity receipt"):
                module.load_frozen_run_state(paths, "20260904")

        self.assertIsNotNone(state)
        snapshots, v45_failure = state
        self.assertEqual(set(snapshots), set(strategy_ids))
        self.assertIsNone(v45_failure)

    def test_runner_resumes_frozen_day_before_calling_upstream_generation(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = module.ShortTrackShadowRunner(
                workspace_dir=Path(tmpdir),
                client=RunnerClient("20260904"),
            )
            for directory, name in ((runner.paths.shadow_universe_dir, "universe"), (runner.paths.shadow_daily_basic_dir, "daily_basic")):
                directory.mkdir(parents=True)
                pd.DataFrame({"trade_date": ["20260904"], "ts_code": ["000001.SZ"]}).to_parquet(directory / f"{name}_20260904.parquet")
            open_dates = [
                date.strftime("%Y%m%d")
                for date in pd.bdate_range(end="2026-09-04", periods=21)
            ]
            with mock.patch.object(
                module,
                "load_open_trade_dates",
                return_value=open_dates,
            ), mock.patch.object(
                module,
                "load_frozen_run_state",
                return_value=({}, None),
            ) as load_frozen, mock.patch.object(
                module,
                "load_production_prebreakout_snapshot",
                side_effect=AssertionError("upstream generation must not run"),
            ), mock.patch.object(
                runner,
                "_load_prices_for_settlement",
                return_value=pd.DataFrame(),
            ), mock.patch.object(
                runner,
                "_load_universe_history",
                return_value=pd.DataFrame(),
            ):
                result = runner.run("20260904")

        load_frozen.assert_called_once_with(runner.paths, "20260904")
        self.assertEqual(result["status"], "ok")

    def test_official_history_uses_one_batched_request_when_endpoint_supports_ranges(self):
        module = load_runner_module()
        trade_dates = [d.strftime("%Y%m%d") for d in pd.bdate_range(end="2026-08-27", periods=21)]
        stk, daily_basic = make_official_history(trade_dates)
        stk["used_proxy"] = "False"
        daily_basic["used_proxy"] = "False"

        class BatchClient:
            def __init__(self):
                self.stk_calls = []
                self.basic_calls = []

            def stk_factor(self, **kwargs):
                self.stk_calls.append(kwargs)
                return stk.copy()

            def daily_basic(self, **kwargs):
                self.basic_calls.append(kwargs)
                return daily_basic.copy()

        client = BatchClient()
        result_stk = module.fetch_official_stk_factor_history(client, trade_dates)
        result_basic = module.fetch_official_daily_basic_history(client, trade_dates)
        self.assertEqual(len(client.stk_calls), 1)
        self.assertEqual(len(client.basic_calls), 1)
        self.assertEqual(client.stk_calls[0], {"start_date": trade_dates[0], "end_date": trade_dates[-1]})
        self.assertEqual(client.basic_calls[0], {"start_date": trade_dates[0], "end_date": trade_dates[-1], "fields": None})
        self.assertEqual(set(result_stk["trade_date"].astype(str)), set(trade_dates))
        self.assertEqual(set(result_basic["trade_date"].astype(str)), set(trade_dates))

    def test_research_history_reuses_valid_exact_date_cache_without_network(self):
        module = load_runner_module()
        dates = [d.strftime("%Y%m%d") for d in pd.bdate_range(end="2026-08-27", periods=21)]
        stk, basic = make_official_history(dates)
        client = mock.Mock()
        client.stk_factor.side_effect = AssertionError("cached history must not hit network")
        client.daily_basic.side_effect = AssertionError("cached history must not hit network")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = module.research_history_context(client, "stk_factor", dates)
            for label, frame in (("stk_factor", stk), ("daily_basic", basic)):
                frame["source_provider"] = "rds"
                if label == "stk_factor":
                    frame.attrs["research_history_context"] = context
                for date in dates:
                    frame.loc[frame.trade_date == date].to_parquet(root / f"{label}_{date}.parquet")
            a = module.fetch_official_stk_factor_history(client, dates, cache_dirs=[root], download_cache=root / "fetched")
            b = module.fetch_official_daily_basic_history(client, dates, cache_dirs=[root], download_cache=root / "fetched")
        self.assertEqual(set(a.trade_date), set(dates))
        self.assertEqual(len(a), len(stk))
        self.assertEqual(len(b), len(basic))
        self.assertEqual(set(a.source), {"rds_stk_factor"})

    def test_daily_history_reuses_current_pit_input_without_refetch(self):
        module = load_runner_module()
        dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-08-03", periods=21)]
        _, basic = make_official_history(dates)
        current = basic.loc[basic.trade_date == dates[-1]].copy()
        original = current.copy(deep=True)
        client = mock.Mock()
        client.daily_basic.side_effect = lambda **kw: basic.loc[basic.trade_date == kw["trade_date"]].copy()
        with tempfile.TemporaryDirectory() as tmp:
            result = module.fetch_official_daily_basic_history(
                client, dates, cache_dirs=[], download_cache=Path(tmp),
                prefetched_frames={dates[-1]: current})
        self.assertEqual(client.daily_basic.call_count, 20)
        self.assertNotIn(dates[-1], {c.kwargs["trade_date"] for c in client.daily_basic.call_args_list})
        self.assertEqual(set(result.trade_date), set(dates))
        pd.testing.assert_frame_equal(current, original)

    def test_daily_history_does_not_accept_invalid_prefetched_input(self):
        module = load_runner_module()
        dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-08-03", periods=21)]
        _, basic = make_official_history([dates[-1]])
        for change in ({"trade_date": dates[0]}, {"used_proxy": True}, {"completeness": "partial"}, {"source": "proxy"}):
            with self.subTest(change=change):
                client = mock.Mock()
                invalid = basic.assign(**change)
                with self.assertRaises(module.RunnerInputError):
                    module.fetch_official_daily_basic_history(
                        client, dates, cache_dirs=[], prefetched_frames={dates[-1]: invalid})
                client.daily_basic.assert_not_called()

    @mock.patch.dict(os.environ, {"STOCK_SHORT_TRACK_INCREMENTAL": "0"})  # full-refetch path under test
    def test_research_history_refetches_proxy_and_wrong_date_not_whole_range(self):
        module = load_runner_module()
        dates = [d.strftime("%Y%m%d") for d in pd.bdate_range(end="2026-08-27", periods=21)]
        stk, _ = make_official_history(dates)
        stk["source_provider"] = "rds"
        client = mock.Mock()
        client.stk_factor.side_effect = lambda **kw: stk.loc[stk.trade_date == kw["trade_date"]].copy()
        stk.attrs["research_history_context"] = module.research_history_context(client, "stk_factor", dates)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for date in dates:
                frame = stk.loc[stk.trade_date == date].copy()
                if date == dates[0]:
                    frame["source_provider"] = "proxy"
                if date == dates[1]:
                    frame["trade_date"] = "19990101"
                frame.to_parquet(root / f"stk_factor_{date}.parquet")
            result = module.fetch_official_stk_factor_history(client, dates, cache_dirs=[root], download_cache=root / "fetched")
            self.assertEqual({c.kwargs["trade_date"] for c in client.stk_factor.call_args_list}, set(dates[:2]))
            self.assertEqual(set(result.trade_date), set(dates))
            client.stk_factor.reset_mock()
            module.fetch_official_stk_factor_history(client, dates, cache_dirs=[root], download_cache=root / "fetched")
            client.stk_factor.assert_not_called()

    @mock.patch.dict(os.environ, {"STOCK_SHORT_TRACK_INCREMENTAL": "0"})  # full-refetch path under test
    def test_factor_cache_from_different_acquisition_context_is_not_mixed(self):
        module = load_runner_module()
        dates = [d.strftime("%Y%m%d") for d in pd.bdate_range(end="2026-08-27", periods=21)]
        stk, _ = make_official_history(dates)
        client = mock.Mock()
        client.stk_factor.side_effect = lambda **kw: stk.loc[stk.trade_date == kw["trade_date"]].copy()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for date in dates:
                old = stk.loc[stk.trade_date == date].copy()
                old.attrs["research_history_context"] = "old-basis"
                old["close_qfq"] = 0.01
                old.to_parquet(root / f"stk_factor_{date}.parquet")
            result = module.fetch_official_stk_factor_history(client, dates, cache_dirs=[root], download_cache=root / "fetched")
            self.assertEqual(client.stk_factor.call_count, 21)
            self.assertGreater(result.close_qfq.min(), 1)

    def test_corrected_shared_pit_uses_versioned_namespace(self):
        module = load_runner_module()
        self.assertEqual(module.build_paths(Path('/example')).common_pit_market_dir.name, 'v2')

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

    def test_production_filters_drop_bj_so_control_parity_holds(self):
        module = load_runner_module()
        prod = make_prod_snapshot_rows()
        scored = [
            {"ts_code": "920055.BJ", "score": 78.0, "name": "北交所高分", "industry_name": "机械"},
            *[
                {
                    "ts_code": row["ts_code"],
                    "score": row["score"],
                    "name": row["name"],
                    "industry_name": row["industry_name"],
                }
                for row in prod
            ],
        ]
        name_map = {row["ts_code"]: row["name"] for row in prod}
        name_map["920055.BJ"] = "北交所高分"
        filtered = module.apply_production_control_filters(
            scored,
            name_map=name_map,
            industry_map={},
            industry_cap=0,
        )
        self.assertNotIn("920055.BJ", [row["ts_code"] for row in filtered[:20]])
        module.validate_control_parity(prod, filtered)

    def test_production_filters_drop_st_names(self):
        module = load_runner_module()
        scored = [
            {"ts_code": "600000.SH", "score": 90.0, "name": "ST示例"},
            {"ts_code": "600001.SH", "score": 80.0, "name": "正常股"},
        ]
        filtered = module.apply_production_control_filters(
            scored,
            name_map={"600000.SH": "ST示例", "600001.SH": "正常股"},
            industry_map={},
            industry_cap=0,
        )
        self.assertEqual([row["ts_code"] for row in filtered], ["600001.SH"])

    def test_existing_snapshot_is_reused_instead_of_mismatch(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "prebreakout_v43_control_20260818_candidate_snapshot.json"
            base = {
                "artifact_kind": "candidate_snapshot",
                "artifact_type": "candidate_snapshot",
                "strategy_id": "prebreakout_v43_control",
                "strategy_version": "4.3+hash",
                "trade_date": "20260818",
                "signal_date": "20260818",
                "signal_data_cutoff": "2026-08-18T15:00:00+08:00",
                "execution_authority": "observe_only_no_auto_order",
                "used_proxy": False,
                "rank_change": 0,
                "completeness_status": "complete",
                "publish_mode": "observe_only",
                "observe_only": True,
                "config_hash": "cfg",
                "input_hash": "input",
                "candidates": [
                    {
                        "rank": 1,
                        "rank_no": 1,
                        "ts_code": "000001.SZ",
                        "strategy_id": "prebreakout_v43_control",
                        "strategy_version": "4.3+hash",
                        "used_proxy": False,
                        "rank_change": 0,
                    }
                ],
            }
            base["rows"] = list(base["candidates"])
            first = {**base, "stamp": "first"}
            second = {**base, "stamp": "second"}
            kept = module.adopt_or_write_immutable_snapshot(path, first)
            reused = module.adopt_or_write_immutable_snapshot(path, second)
            self.assertEqual(kept["stamp"], "first")
            self.assertEqual(reused["stamp"], "first")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["stamp"], "first")

    def test_existing_snapshot_with_wrong_date_or_partial_candidates_is_rejected(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "prebreakout_v43_control_20260818_candidate_snapshot.json"
            expected = {
                "artifact_kind": "candidate_snapshot",
                "artifact_type": "candidate_snapshot",
                "strategy_id": "prebreakout_v43_control",
                "strategy_version": "4.3+hash",
                "trade_date": "20260818",
                "signal_date": "20260818",
                "signal_data_cutoff": "2026-08-18T15:00:00+08:00",
                "execution_authority": "observe_only_no_auto_order",
                "used_proxy": False,
                "rank_change": 0,
                "completeness_status": "complete",
                "publish_mode": "observe_only",
                "observe_only": True,
                "config_hash": "cfg",
                "input_hash": "input",
                "candidates": [
                    {
                        "rank": 1,
                        "rank_no": 1,
                        "ts_code": "000001.SZ",
                        "strategy_id": "prebreakout_v43_control",
                        "strategy_version": "4.3+hash",
                        "used_proxy": False,
                        "rank_change": 0,
                    }
                ],
            }
            expected["rows"] = list(expected["candidates"])
            tampered = {**expected, "trade_date": "20260817", "candidates": []}
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(module.RunnerInputError):
                module.adopt_or_write_immutable_snapshot(path, expected)

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
            client = RunnerClient("20260811")
            client.daily_basic = mock.Mock(wraps=client.daily_basic)
            runner = module.ShortTrackShadowRunner(workspace_dir=workspace, client=client)
            open_dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-07-14", "2026-08-20")]
            stk_hist, db_hist = make_official_history(open_dates[-21:])
            with mock.patch.object(module, "load_production_prebreakout_snapshot", return_value=prod), \
                mock.patch.object(module, "score_full_ranked_pool", return_value=make_scored_rows()), \
                mock.patch.object(module, "load_open_trade_dates", return_value=open_dates), \
                mock.patch.object(module, "fetch_official_stk_factor_history", return_value=stk_hist), \
                mock.patch.object(module.short_track_shadow, "build_balanced_feature_frame", return_value=make_balanced_frame()):
                result = runner.run("20260811")
            self.assertEqual(sum(c.kwargs["trade_date"] == "20260811" for c in client.daily_basic.call_args_list), 1)
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
            # Successful day produces control + top15 + v44 + v45 (4 books).
            self.assertEqual(len(list(portfolio_dir.glob("*_portfolio_daily.parquet"))), 4)
            self.assertTrue(
                (short_dir / "prebreakout_v45_cross_sectional_20260811_candidate_snapshot.json").exists()
            )

    def test_runner_v45_failure_does_not_block_required_tracks(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            prod_path = workspace / "stock_data/03-working/stock-report-repo/data/strategy_backtests.json"
            prod_path.parent.mkdir(parents=True, exist_ok=True)
            prod = {
                "latest_trade_date": "20260811",
                "strategies": [{"id": "prebreakout_v41", "top20": make_prod_snapshot_rows()}],
            }
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
                mock.patch.object(module.short_track_shadow, "build_balanced_feature_frame", return_value=make_balanced_frame()), \
                mock.patch.object(
                    module.short_track_shadow,
                    "build_cross_sectional_candidate_snapshot",
                    side_effect=module.short_track_shadow.ShortTrackInputError("forced v45 fail"),
                ):
                result = runner.run("20260811")
            self.assertEqual(result["status"], "ok")
            self.assertIsNotNone(result.get("v45_failure"))
            short_dir = workspace / "stock_data/03-working/strategy_research/short_track/daily"
            portfolio_dir = workspace / "stock_data/03-working/strategy_research/short_track/portfolio_daily"
            # Required three books only; v45 has failure marker, no portfolio.
            self.assertEqual(len(list(portfolio_dir.glob("*_portfolio_daily.parquet"))), 3)
            self.assertTrue(
                (short_dir / "prebreakout_v45_cross_sectional_20260811_failure.json").exists()
            )
            self.assertFalse(
                (short_dir / "prebreakout_v45_cross_sectional_20260811_candidate_snapshot.json").exists()
            )
            # Production snapshot untouched.
            self.assertEqual(json.loads(prod_path.read_text(encoding="utf-8")), prod)

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

    def test_short_track_runner_lives_only_in_post_publish_research(self):
        stage3_path = SCRIPT_DIR / "stage3_strategy_suite.py"
        post_research_path = SCRIPT_DIR / "run_post_publish_research.py"
        stage3_text = stage3_path.read_text(encoding="utf-8")
        post_research_text = post_research_path.read_text(encoding="utf-8")
        self.assertNotIn("run_step('short_track_shadow'", stage3_text)
        self.assertIn('("shortTrackGeneration", SCRIPT_DIR / "short_track_shadow_runner.py")', post_research_text)

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
                as_of_date="20260818",
            )
            self.assertEqual(report["trade_date"], "20260818")
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

    def test_daily_fallback_fills_ipo_day_without_overwriting_stk_factor(self):
        module = load_runner_module()
        stk = pd.DataFrame(
            {
                "trade_date": ["20260813"],
                "ts_code": ["920138.BJ"],
                "open_qfq": [51.01],
                "close_qfq": [46.59],
                "close": [46.59],
                "adj_factor": [1.0],
            }
        )
        daily = pd.DataFrame(
            {
                "trade_date": ["20260812", "20260813"],
                "ts_code": ["920138.BJ", "920138.BJ"],
                "open": [53.0, 99.0],
                "close": [53.26, 99.0],
                "adj_factor": [1.0, 1.0],
            }
        )
        out = module.merge_qfq_with_daily_fallback(stk, daily)
        ipo = out[(out["trade_date"] == "20260812") & (out["ts_code"] == "920138.BJ")].iloc[0]
        next_day = out[(out["trade_date"] == "20260813") & (out["ts_code"] == "920138.BJ")].iloc[0]
        self.assertEqual(float(ipo["open_qfq"]), 53.0)
        self.assertEqual(float(ipo["close_qfq"]), 53.26)
        self.assertEqual(float(next_day["open_qfq"]), 51.01)
        self.assertEqual(float(next_day["close_qfq"]), 46.59)

    def test_resolve_trade_date_prefers_pipeline_lock(self):
        module = load_runner_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            with mock.patch.dict(os.environ, {"OPENCLAW_TARGET_TRADE_DATE": "20260819"}, clear=False):
                self.assertEqual(module.resolve_trade_date(workspace), "20260819")

    def test_fallback_observation_tracking_stays_healthy_observe_only(self):
        module = load_runner_module()
        report = module.fallback_observation_tracking(
            strategy_id="prebreakout_v43_control",
            strategy_version="4.3+test",
            trade_date="20260820",
            error="EvaluationContractError: all-A benchmark has no eligible PIT constituents on 20260819",
        )
        self.assertEqual(report["operational_status"], "degraded_observation")
        self.assertEqual(report["flow_status"], "degraded")
        self.assertEqual(report["trade_date"], "20260820")
        self.assertEqual(report["execution_authority"], "observe_only_no_auto_order")
        self.assertEqual(report["effectiveness_status"], "not_validated")
        self.assertIn("portfolio_eval_error", report["operational_evidence"])


if __name__ == "__main__":
    unittest.main()
