#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import factor_attribution as fa
import factor_attribution_engine as engine


class FactorAttributionUnitTests(unittest.TestCase):
    def test_spearman_perfect_correlation(self):
        x = np.arange(50, dtype=float)
        y = x * 2 + 1
        self.assertAlmostEqual(fa.spearman_corr(x, y), 1.0, places=6)
        self.assertAlmostEqual(fa.spearman_corr(x, -y), -1.0, places=6)

    def test_ic_matches_known_rank_relationship(self):
        rng = np.random.default_rng(0)
        n = 200
        factor = pd.Series(rng.normal(size=n))
        noise = pd.Series(rng.normal(size=n) * 0.1)
        excess = factor.rank() + noise  # strongly related to factor ranks
        ic = fa.ic_for_day(factor, excess)
        self.assertGreater(ic, 0.7)

    def test_lookahead_positive_control_detects_leak(self):
        """If factor is built from T+1 return, IC at h=1 is near-perfect (leak detector)."""
        rng = np.random.default_rng(1)
        n = 300
        future_excess = pd.Series(rng.normal(size=n))
        leaked_factor = future_excess.copy()  # illegal: factor sees T+1
        ic = fa.ic_for_day(leaked_factor, future_excess)
        self.assertGreater(ic, 0.95)

    def test_normal_path_cannot_use_future_prices_in_forward_returns(self):
        # Build a tiny calendar and price panel: factor date T has prices only up to T
        calendar = ["20260105", "20260106", "20260107"]
        rows = []
        for day, px in zip(calendar, [10.0, 11.0, 12.0]):
            rows.append({"ts_code": "000001.SZ", "trade_date": day, "close_qfq": px, "open_qfq": px, "high_qfq": px, "low_qfq": px, "vol": 1, "amount": 1, "macd_dif": 0, "macd_dea": 0})
        prices = pd.DataFrame(rows)
        r1 = fa.forward_close_returns(prices, signal_date="20260105", horizon=1, calendar=calendar)
        self.assertAlmostEqual(float(r1["000001.SZ"]), 0.1)
        # If end price missing, null not -100%
        r_miss = fa.forward_close_returns(prices.iloc[:1], signal_date="20260105", horizon=1, calendar=calendar)
        self.assertTrue(r_miss.empty or r_miss.isna().all())

    def test_decile_stable_ties_and_remainder(self):
        # 25 names -> sizes 3,3,3,3,3,2,2,2,2,2
        values = pd.Series([1.0] * 5 + list(range(20)))
        codes = pd.Series([f"{i:06d}.SZ" for i in range(25)])
        labels = fa.assign_deciles(values, codes, n=10)
        self.assertEqual(int(labels.notna().sum()), 25)
        self.assertEqual(set(labels.dropna().astype(int)), set(range(1, 11)))
        # identical values: lower ts_code gets lower rank within ties
        tied = pd.Series([1.0, 1.0, 1.0, 1.0, 1.0])
        tied_codes = pd.Series(["000005.SZ", "000001.SZ", "000003.SZ", "000002.SZ", "000004.SZ"])
        lab = fa.assign_deciles(tied, tied_codes, n=10)
        # After sort by (value, ts_code): 001,002,003,004,005 → positions 0..4
        ordered = lab.iloc[tied_codes.argsort().argsort().to_numpy() if False else lab.index]
        # 000001 should be earliest among ties
        idx_001 = list(tied_codes).index("000001.SZ")
        idx_005 = list(tied_codes).index("000005.SZ")
        self.assertLessEqual(int(lab.iloc[idx_001]), int(lab.iloc[idx_005]))
        # named index must not crash (ts_code index+column ambiguity)
        f = pd.Series([1.0, 2.0, 3.0] * 15, index=pd.Index([f"{i:06d}.SZ" for i in range(45)], name="ts_code"))
        e = pd.Series(np.arange(45, dtype=float), index=f.index)
        c = pd.Series(f.index.astype(str), index=f.index)
        stats = fa.decile_stats_for_day(f, e, c)
        self.assertTrue(np.isfinite(stats["top_minus_bottom"]))

    def test_newey_west_matches_naive_when_no_autocorr(self):
        rng = np.random.default_rng(2)
        x = rng.normal(size=500)
        t_nw = fa.newey_west_t(x, lag=4)
        mean = float(np.mean(x))
        se = float(np.std(x, ddof=1) / np.sqrt(len(x)))
        t_naive = mean / se
        self.assertAlmostEqual(t_nw, t_naive, places=1)

    def test_newey_west_more_conservative_with_strong_autocorr(self):
        # AR(1) with high rho
        rng = np.random.default_rng(3)
        n = 400
        e = rng.normal(size=n)
        x = np.zeros(n)
        for i in range(1, n):
            x[i] = 0.9 * x[i - 1] + e[i]
        t_nw = fa.newey_west_t(x, lag=10)
        mean = float(np.mean(x))
        se = float(np.std(x, ddof=1) / np.sqrt(n))
        t_naive = mean / se
        # With positive serial correlation, NW se should be larger → |t| smaller
        self.assertLess(abs(t_nw), abs(t_naive) + 1e-9)

    def test_json_safe_drops_nan_for_strict_dumps(self):
        payload = fa.json_safe({"icir": float("nan"), "arr": np.array([1.0, np.inf]), "ok": True})
        encoded = json.dumps(payload, allow_nan=False)
        self.assertIn("null", encoded)
        self.assertNotIn("NaN", encoded)

    def test_half_life_summary_classifies_growing_factor(self):
        per_factor = {
            "volume_turnover_stability": {
                "1": {"ic": {"mean_ic": 0.04, "n_days": 40, "status": "ok"}},
                "20": {"ic": {"mean_ic": 0.10, "n_days": 40, "status": "ok"}},
            }
        }
        text = fa.describe_ic_half_life(per_factor, names=["volume_turnover_stability"])
        self.assertIn("随持有期增强", text)
        self.assertIn("volume_turnover_stability", text)

    def test_public_factor_attribution_state_is_diagnostic_only(self):
        result = {
            "coverage": {"as_of": "20260813", "aggregation_scope": "full_history", "signal_days_ok": 80},
            "homology": {"flag_same_factor_family": True},
            "reverse_test": {"meets_phase2_trigger": True},
            "repro_metadata": {"generated_at": "2026-08-13T22:00:00+08:00", "config_hash": "abc"},
            "per_factor": {
                "volume_turnover_stability": {
                    "5": {
                        "ic": {
                            "mean_ic": 0.04,
                            "icir": 0.5,
                            "t_nw": 2.2,
                            "n_days": 80,
                            "status": "ok",
                        },
                        "decile_monotonicity_mean": 0.2,
                    }
                }
            },
        }
        state = fa.build_public_factor_attribution_state(result)
        self.assertFalse(state["promotion_eligible"])
        self.assertTrue(state["not_for_promotion"])
        self.assertFalse(state["reverse_phase2_auto_start"])
        self.assertTrue(state["reverse_phase2_trigger"])
        self.assertTrue(state["q1_decile_monotonicity_warning"])
        self.assertEqual(state["strongest_positive_ic_t5"]["factor"], "volume_turnover_stability")
        encoded = json.dumps(state)
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn("stock_data/", encoded)
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            latest = ws / "stock-report" / "data" / "latest"
            latest.mkdir(parents=True)
            paths = fa.build_paths(ws)
            written = fa.write_public_factor_attribution_state(result, workspace=ws, paths=paths)
            self.assertEqual(written["published"], "factor_attribution_state.json")
            public = json.loads((latest / "factor_attribution_state.json").read_text(encoding="utf-8"))
            self.assertFalse(public["promotion_eligible"])
            working = json.loads(paths.public_state.read_text(encoding="utf-8"))
            self.assertEqual(working["evidence_scope"], "diagnostic_only")

    def test_resolve_refresh_days_daily_default_is_one(self):
        import factor_attribution_report as report

        self.assertEqual(
            report.resolve_refresh_days(refresh_days=None, max_days=None, as_of="latest", env={}),
            1,
        )
        self.assertEqual(
            report.resolve_refresh_days(refresh_days=0, max_days=None, as_of="latest", env={}),
            0,
        )
        self.assertIsNone(
            report.resolve_refresh_days(
                refresh_days=None, max_days=None, as_of="latest", env={"OPENCLAW_FACTOR_ATTR_FULL": "1"}
            )
        )

    def test_panel_missing_v45_columns(self):
        frame = pd.DataFrame({"ts_code": ["000001.SZ"], "composite": [1.0]})
        self.assertTrue(fa.panel_missing_v45_columns(frame))
        for col in fa.V45_FACTOR_NAMES + ["v45_composite"]:
            frame[col] = 0.0
        self.assertFalse(fa.panel_missing_v45_columns(frame))
        # empty factor/excess -> nan ic
        f = pd.Series([1.0, 2.0])
        e = pd.Series([np.nan, np.nan])
        self.assertTrue(np.isnan(fa.ic_for_day(f, e)))

    def test_forward_returns_use_official_calendar_not_next_cache_file(self):
        official = ["20260407", "20260408", "20260409"]
        # Price files exist only for 0407 and 0409 — 0408 is a real session with missing cache.
        rows = [
            {"ts_code": "000001.SZ", "trade_date": "20260407", "close_qfq": 10.0},
            {"ts_code": "000001.SZ", "trade_date": "20260409", "close_qfq": 12.0},
        ]
        prices = pd.DataFrame(rows)
        r_official = fa.forward_close_returns(prices, signal_date="20260407", horizon=1, calendar=official)
        self.assertTrue(np.isnan(float(r_official["000001.SZ"])))
        # File-name calendar would wrongly treat 0409 as T+1 (20% instead of missing).
        r_files = fa.forward_close_returns(
            prices, signal_date="20260407", horizon=1, calendar=["20260407", "20260409"]
        )
        self.assertAlmostEqual(float(r_files["000001.SZ"]), 0.2)

    def test_price_slice_cannot_see_sessions_after_T(self):
        calendar = ["20260105", "20260106", "20260107"]
        rows = []
        for day, px in zip(calendar, [10.0, 99.0, 12.0]):
            rows.append({"ts_code": "000001.SZ", "trade_date": day, "close_qfq": px})
        prices = pd.DataFrame(rows)
        sliced = fa.slice_prices_for_signal(prices, trade_date="20260105", calendar=calendar)
        self.assertEqual(set(sliced["trade_date"]), {"20260105"})
        self.assertNotIn(99.0, set(sliced["close_qfq"]))

    def test_significant_nw_requires_min_inference_days(self):
        self.assertEqual(fa.MIN_INFERENCE_DAYS, 30)
        self.assertFalse(fa.significant_nw_t(t_nw=6.79, n_days=5))
        self.assertFalse(fa.significant_nw_t(t_nw=6.79, n_days=29))
        self.assertTrue(fa.significant_nw_t(t_nw=2.1, n_days=fa.MIN_INFERENCE_DAYS))
        self.assertFalse(fa.significant_nw_t(t_nw=1.9, n_days=80))

    def test_summarize_marks_insufficient_sample_below_threshold(self):
        short = [0.01] * 29
        ic = fa.summarize_ic_series(short, horizon=5)
        self.assertEqual(ic["status"], "insufficient_sample")
        self.assertEqual(ic["n_days"], 29)
        self.assertIsNotNone(ic["mean_ic"])
        spread = fa.summarize_spread_series(short, horizon=5)
        self.assertEqual(spread["status"], "insufficient_sample")
        full = fa.summarize_ic_series([0.01] * 30, horizon=5)
        self.assertEqual(full["status"], "ok")

    def test_q1_picks_signed_extremes_and_ignores_small_samples(self):
        names = fa.FACTOR_NAMES
        per_factor = {}
        for name in names:
            per_factor[name] = {"5": {"ic": {"mean_ic": 0.01, "icir": 0.1, "t_nw": 0.5, "n_days": 40, "status": "ok"}}}
        per_factor[names[0]]["5"]["ic"] = {
            "mean_ic": 0.04,
            "icir": 0.5,
            "t_nw": 2.2,
            "n_days": 64,
            "status": "ok",
        }
        per_factor[names[1]]["5"]["ic"] = {
            "mean_ic": -0.20,
            "icir": -1.0,
            "t_nw": -3.0,
            "n_days": 64,
            "status": "ok",
        }
        per_factor[names[2]]["5"]["ic"] = {
            "mean_ic": 0.90,
            "icir": 9.0,
            "t_nw": 15.0,
            "n_days": 5,
            "status": "insufficient_sample",
        }
        pending = {"n_days": 0, "mean_ic": None, "icir": None, "t_nw": None, "status": "pending"}
        ok_small = {"mean_ic": 0.01, "icir": 0.1, "t_nw": 0.5, "n_days": 40, "status": "ok"}
        per_factor["composite"] = {"5": {"ic": dict(ok_small)}}
        extremes = fa.pick_signed_ic_extremes(per_factor)
        self.assertEqual(extremes["positive"][0], names[0])
        self.assertEqual(extremes["negative"][0], names[1])
        md = engine.render_markdown_report(
            {
                "schema_version": fa.SCHEMA_VERSION,
                "evidence_scope": fa.EVIDENCE_SCOPE,
                "returns_definition": "test",
                "repro_metadata": {"config_hash": "x", "min_inference_days": 30},
                "coverage": {
                    "as_of": "20260812",
                    "aggregation_scope": "full_history",
                    "signal_days_ok": 64,
                    "signal_days_considered": 64,
                    "signal_days_skipped": 0,
                },
                "per_factor": {
                    name: {
                        str(h): {
                            "ic": (per_factor.get(name) or {"5": {"ic": pending}})["5"]["ic"]
                            if h == 5
                            else pending,
                            "top_minus_bottom": {"mean": None},
                            "decile_mean_excess": {"mean_excess": [None] * 10},
                        }
                        for h in fa.HORIZONS
                    }
                    for name in list(names) + ["composite"]
                },
                "correlation": {"matrix": None},
                "homology": {"note": "test", "mean_pairwise_spearman": -0.036822916},
                "reverse_test": {
                    "wording": "test",
                    "significant_horizons_nw_t_gt_2": [],
                    "meets_phase2_trigger": False,
                    "rows": [
                        {
                            "horizon": 5,
                            "bottom_minus_top_mean": 0.0005123614145451756,
                            "t_nw": 0.07979730771575873,
                            "n_days": 64,
                            "significant_nw_t_gt_2": False,
                        }
                    ],
                },
            }
        )
        self.assertIn("最强正 IC", md)
        self.assertIn("最强负 IC", md)
        self.assertNotIn("|mean IC| 最大", md)
        self.assertIn("0.0005", md)
        self.assertIn("-0.0368", md)
        self.assertNotIn("0.07979730771575873", md)

    def test_archive_previous_report_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = fa.AttributionPaths(
                root=root,
                cache=root / "cache",
                latest_json=root / "factor_ic_latest.json",
                latest_md=root / "factor_ic_report_latest.md",
                public_state=root / "factor_attribution_state.json",
            )
            paths.latest_md.write_text("old report", encoding="utf-8")
            paths.latest_json.write_text(
                json.dumps({"coverage": {"as_of": "20260812"}}), encoding="utf-8"
            )
            dest = fa.archive_previous_markdown_report(paths, incoming_as_of="20260813")
            self.assertIsNotNone(dest)
            self.assertEqual(dest.name, "factor_ic_report_20260812.md")
            self.assertEqual(dest.read_text(encoding="utf-8"), "old report")
            for i in range(35):
                (root / f"factor_ic_report_{20260101 + i:08d}.md").write_text("x", encoding="utf-8")
            fa.prune_report_archives(root, keep=30)
            kept = sorted(p.name for p in root.glob("factor_ic_report_????????.md"))
            self.assertEqual(len(kept), 30)
            self.assertNotIn("factor_ic_report_20260101.md", kept)

    def test_seed_cache_prefers_newest_sibling_not_largest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "cache" / "cfg_old" / "day_panels"
            new = root / "cache" / "cfg_new" / "day_panels"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            for i in range(3):
                (old / f"panel_2026010{i+1}.parquet").write_text("old", encoding="utf-8")
            (new / "panel_20260812.parquet").write_text("new", encoding="utf-8")
            (new / "skip_20260811.json").write_text("{}", encoding="utf-8")
            import os
            os.utime(old, (1_700_000_000, 1_700_000_000))
            os.utime(new, (1_800_000_000, 1_800_000_000))
            paths = fa.AttributionPaths(
                root=root,
                cache=root / "cache" / "cfg_next",
                latest_json=root / "factor_ic_latest.json",
                latest_md=root / "factor_ic_report_latest.md",
                public_state=root / "factor_attribution_state.json",
            )
            fa.seed_cache_from_previous_config(paths)
            dest = paths.cache / "day_panels"
            self.assertTrue((dest / "panel_20260812.parquet").exists())
            self.assertTrue((dest / "skip_20260811.json").exists())
            self.assertFalse((dest / "panel_20260101.parquet").exists())

    def test_decile_means_keep_all_ten_groups(self):
        daily = [[float(i) for i in range(10)], [float(i + 1) for i in range(10)]]
        out = fa.summarize_decile_means(daily)
        self.assertEqual(len(out["mean_excess"]), 10)
        self.assertAlmostEqual(out["mean_excess"][0], 0.5)
        self.assertAlmostEqual(out["mean_excess"][9], 9.5)

    def test_v43_top20_snapshot_excluded_from_ic(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmpdir := tmp)
            daily = ws / "stock_data/03-working/strategy_research/short_track/daily"
            daily.mkdir(parents=True)
            rows = [{"ts_code": f"{i:06d}.SZ", "score": float(i)} for i in range(20)]
            path = daily / "prebreakout_v43_control_20260811_candidate_snapshot.json"
            path.write_text(json.dumps({"candidates": rows}), encoding="utf-8")
            art = fa.load_v43_composite_artifact(ws, "20260811")
            self.assertTrue(art["range_restricted"])
            self.assertTrue(art["excluded_from_ic"])
            self.assertEqual(art["n_names"], 20)

    def test_refresh_window_does_not_shrink_full_history_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            n_days = 40
            calendar = pd.bdate_range("2026-01-05", periods=n_days).strftime("%Y%m%d").tolist()
            cache_dir = ws / "stock_data/03-working/backtest_cache"
            cache_dir.mkdir(parents=True)
            health = ws / "stock_data/03-working/health"
            health.mkdir(parents=True)
            (health / "trading_calendar.json").write_text(
                json.dumps({"source": "test", "open_dates": calendar}), encoding="utf-8"
            )
            rng = np.random.default_rng(0)
            names = [f"{i:06d}.SZ" for i in range(40)]
            for day in calendar:
                rows = []
                px = 10.0 + rng.normal(0, 0.1)
                for code in names:
                    rows.append(
                        {
                            "ts_code": code,
                            "trade_date": day,
                            "close_qfq": px + rng.normal(0, 0.01),
                            "open_qfq": px,
                            "high_qfq": px,
                            "low_qfq": px,
                            "vol": 1.0,
                            "amount": 1.0,
                            "macd_dif": 0.0,
                            "macd_dea": 0.0,
                        }
                    )
                pd.DataFrame(rows).to_parquet(cache_dir / f"stk_factor_{day}.parquet", index=False)

            paths = fa.build_paths(ws)
            paths.cache.mkdir(parents=True, exist_ok=True)
            panel_dir = paths.cache / "day_panels"
            panel_dir.mkdir(parents=True, exist_ok=True)
            signal_days = calendar[20:]
            for day in signal_days:
                scores = rng.normal(size=len(names))
                frame = pd.DataFrame({"ts_code": names})
                for col in fa.FACTOR_NAMES:
                    frame[col] = scores + rng.normal(0, 0.01, size=len(names))
                frame["composite"] = frame[fa.FACTOR_NAMES].mean(axis=1)
                frame["composite_reversed"] = -frame["composite"]
                frame["trade_date"] = day
                frame.to_parquet(panel_dir / f"panel_{day}.parquet", index=False)

            result = engine.run_attribution(
                workspace=ws,
                as_of="latest",
                refresh_days=3,
                fetch_missing=False,
                official_calendar=calendar,
                write_latest=True,
            )
            self.assertEqual(result["coverage"]["aggregation_scope"], "full_history")
            self.assertEqual(result["coverage"]["signal_days_ok"], len(signal_days))
            self.assertGreater(result["coverage"]["signal_days_ok"], 3)
            latest = json.loads(paths.latest_json.read_text(encoding="utf-8"))
            self.assertEqual(latest["coverage"]["signal_days_ok"], len(signal_days))
            # 15-day-style trigger must not fire on a 3-day refresh of a longer sample.
            n1 = latest["reverse_test"]["rows"][0]["n_days"]
            self.assertGreater(n1, 3)
            public_state = json.loads(paths.public_state.read_text(encoding="utf-8"))
            self.assertFalse(public_state["promotion_eligible"])
            self.assertEqual(public_state["aggregation_scope"], "full_history")
            self.assertFalse(public_state["reverse_phase2_auto_start"])

    def test_replay_slice_used_by_build_day_panel(self):
        calendar = pd.bdate_range("2026-01-05", periods=25).strftime("%Y%m%d").tolist()
        trade_date = calendar[20]
        future = calendar[21]
        rows = []
        for day in calendar:
            poison = 999.0 if day == future else 10.0
            rows.append(
                {
                    "ts_code": "000001.SZ",
                    "trade_date": day,
                    "close_qfq": poison,
                    "open_qfq": poison,
                    "high_qfq": poison,
                    "low_qfq": poison,
                    "vol": 1.0,
                    "amount": 1.0,
                    "macd_dif": 0.0,
                    "macd_dea": 0.0,
                    "used_proxy": False,
                    "completeness": "complete",
                }
            )
        prices = pd.DataFrame(rows)
        sliced = fa.slice_prices_for_signal(prices, trade_date=trade_date, calendar=calendar)
        self.assertEqual(sliced["trade_date"].max(), trade_date)
        self.assertTrue((sliced["close_qfq"] < 20).all())
        # Official T+1 with a cache hole must stay NaN even if T+2 exists.
        hole_cal = list(calendar)
        prices_hole = prices[prices["trade_date"] != future].copy()
        ret = fa.forward_close_returns(prices_hole, signal_date=trade_date, horizon=1, calendar=hole_cal)
        self.assertTrue(ret.empty or ret.isna().all())

    def test_daily_basic_fetch_requests_full_provider_fields(self):
        # The RDS/Promax client rejects projections that drop its required
        # daily_basic columns; a narrowed request never succeeds.
        class StrictClient:
            def __init__(self):
                self.calls = []

            def daily_basic(self, *, trade_date, fields=None):
                self.calls.append(fields)
                if fields is not None:
                    raise RuntimeError("daily_basic missing required fields: close,pb,pe_ttm")
                return pd.DataFrame(
                    {"ts_code": ["000001.SZ"], "trade_date": [trade_date], "close": [10.0], "pe_ttm": [5.0],
                     "pb": [1.0], "turnover_rate": [1.5], "circ_mv": [1e5], "total_mv": [2e5]}
                )

        with tempfile.TemporaryDirectory() as tmp:
            client = StrictClient()
            session = engine.PitFetchSession(client, Path(tmp) / "pit_fetch")
            out = session.load_daily_basic("20260914", [])
            self.assertIsNotNone(out)
            self.assertEqual(client.calls, [None])
            self.assertTrue((Path(tmp) / "pit_fetch" / "daily_basic_20260914.parquet").exists())

    def test_daily_basic_reuses_short_track_input_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            paths = engine._local_daily_basic_paths(ws, "20261012", ws / "attr")
            self.assertIn(
                ws / "stock_data/03-working/strategy_research/short_track/input_cache/daily_basic_20261012.parquet",
                paths,
            )


if __name__ == "__main__":
    unittest.main()
