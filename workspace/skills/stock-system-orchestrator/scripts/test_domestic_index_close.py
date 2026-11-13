#!/usr/bin/env python3
"""Post-close domestic index resolution (20261009 ChiNext intraday-spot incident)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import domestic_index_close as dic  # noqa: E402
import generate_latest_states as states  # noqa: E402
import generate_midday_analysis as midday  # noqa: E402
import validate_publication_contract as contract  # noqa: E402
from test_validate_publication_contract import _base_latest, _write  # noqa: E402

SH = dic.SH_TZ
EVENING = datetime(2026, 10, 9, 19, 47, tzinfo=SH)
NOON = datetime(2026, 10, 9, 12, 0, tzinfo=SH)

# Real 20261009 values: provider had SH/SZ but no 399006.SZ row.
PROVIDER_ROWS = {
    ("000001.SH", "20261009"): {"trade_date": "20261009", "close": 3813.7913, "pre_close": 3811.9043, "pct_chg": 0.0495, "source_provider": "rds"},
    ("399001.SZ", "20261009"): {"trade_date": "20261009", "close": 12641.8584, "pre_close": 12620.897, "pct_chg": 0.1661, "source_provider": "rds"},
}
SINA_BARS = {
    "sz399006": [("2026-09-30", 3135.277), ("2026-10-08", 3036.657), ("2026-10-09", 3043.335)],
    "sh000001": [("2026-10-08", 3811.904), ("2026-10-09", 3813.791)],
    "sz399001": [("2026-10-08", 12620.897), ("2026-10-09", 12641.858)],
}
NOON_SPOT_CHINEXT = {
    "key": "chinext", "symbol": "sz399006", "label": "创业板指", "source_kind": "intraday_spot",
    "provider": "akshare", "close": 2957.325, "prev_close": 3036.657, "change_pct": -2.6125,
    "as_of": None, "bar_count": 0, "source_error": None,
}


class FakePro:
    def __init__(self, rows=None, raise_times=0):
        self.rows = PROVIDER_ROWS if rows is None else rows
        self.raise_times = raise_times
        self.calls: list[tuple[str, str]] = []

    def index_daily(self, ts_code, trade_date):
        self.calls.append((ts_code, trade_date))
        if self.raise_times > 0:
            self.raise_times -= 1
            raise ConnectionError("provider timeout")
        row = self.rows.get((ts_code, trade_date))
        return pd.DataFrame([row]) if row else pd.DataFrame()


class FakeAk:
    def __init__(self, bars=None):
        self.bars = SINA_BARS if bars is None else bars
        self.calls: list[str] = []

    def stock_zh_index_daily(self, symbol):
        self.calls.append(symbol)
        rows = self.bars.get(symbol) or []
        return pd.DataFrame({"date": [d for d, _ in rows], "close": [c for _, c in rows]})


def no_sleep(_seconds):
    return None


class IsPostCloseTests(unittest.TestCase):
    def test_same_day_before_settle_is_not_post_close(self):
        self.assertFalse(dic.is_post_close("20261009", NOON))
        self.assertFalse(dic.is_post_close("20261009", datetime(2026, 10, 9, 15, 10, tzinfo=SH)))

    def test_same_day_after_settle_and_prior_days_are_post_close(self):
        self.assertTrue(dic.is_post_close("20261009", EVENING))
        self.assertTrue(dic.is_post_close("20261008", datetime(2026, 10, 9, 8, 0, tzinfo=SH)))

    def test_future_or_invalid_date_is_not_post_close(self):
        self.assertFalse(dic.is_post_close("20261012", EVENING))
        self.assertFalse(dic.is_post_close("", EVENING))


class ResolveSettledCloseTests(unittest.TestCase):
    def test_provider_close_is_used_first(self):
        ak = FakeAk()
        snap = dic.resolve_settled_close("shanghai", "20261009", pro=FakePro(), ak_module=ak, now=EVENING, sleep=no_sleep)
        self.assertEqual(snap["source_kind"], "exact_close")
        self.assertEqual(snap["provider"], "rds")
        self.assertAlmostEqual(snap["change_pct"], 0.0495, places=3)
        self.assertEqual(snap["as_of"], "20261009")
        self.assertEqual(ak.calls, [])

    def test_missing_provider_row_is_retried_then_falls_back_to_sina_daily_bar(self):
        pro, ak, sleeps = FakePro(), FakeAk(), []
        snap = dic.resolve_settled_close("chinext", "20261009", pro=pro, ak_module=ak, now=EVENING, sleep=sleeps.append)
        self.assertEqual(len(pro.calls), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(snap["source_kind"], "exact_close")
        self.assertEqual(snap["provider"], "akshare_sina_daily")
        self.assertEqual(snap["close"], 3043.335)
        self.assertEqual(snap["prev_close"], 3036.657)
        self.assertAlmostEqual(snap["change_pct"], 0.2199, places=3)

    def test_transient_provider_error_recovers_on_retry(self):
        ak = FakeAk()
        snap = dic.resolve_settled_close("shenzhen", "20261009", pro=FakePro(raise_times=1), ak_module=ak, now=EVENING, sleep=no_sleep)
        self.assertEqual(snap["provider"], "rds")
        self.assertEqual(ak.calls, [])

    def test_stale_sina_bar_is_not_accepted_for_the_session(self):
        ak = FakeAk({"sz399006": [("2026-09-30", 3135.277), ("2026-10-08", 3036.657)]})
        snap = dic.resolve_settled_close("chinext", "20261009", pro=FakePro(), ak_module=ak, now=EVENING, sleep=no_sleep)
        self.assertEqual(snap["source_kind"], "unavailable")
        self.assertIsNone(snap["change_pct"])
        self.assertIn("no 20261009 bar", snap["source_error"])

    def test_everything_missing_is_explicitly_unavailable(self):
        snap = dic.resolve_settled_close("chinext", "20261009", pro=False, ak_module=None, now=EVENING, sleep=no_sleep)
        self.assertEqual(snap["source_kind"], "unavailable")
        self.assertEqual(snap["provider"], "unavailable")
        self.assertIsNone(snap["close"])
        self.assertIn("index_daily client unavailable", snap["source_error"])
        self.assertIn("akshare unavailable", snap["source_error"])

    def test_resolution_before_settlement_is_refused(self):
        with self.assertRaises(ValueError):
            dic.resolve_settled_close("chinext", "20261009", pro=FakePro(), ak_module=FakeAk(), now=NOON)


class RefreshDomesticCloseTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(dic.time, "sleep", no_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    def noon_snapshot(self):
        return {
            "shanghai": {"key": "shanghai", "source_kind": "intraday_spot", "provider": "akshare", "change_pct": 0.3066, "as_of": None},
            "shenzhen": {"key": "shenzhen", "source_kind": "intraday_spot", "provider": "akshare", "change_pct": -0.5, "as_of": None},
            "chinext": dict(NOON_SPOT_CHINEXT),
            "spx": {"key": "spx", "source_kind": "exact", "change_pct": 0.4},
        }

    def test_20261009_replay_replaces_noon_chinext_spot_with_settled_close(self):
        snap = states.refresh_domestic_close(self.noon_snapshot(), "20261009", pro=FakePro(), ak_module=FakeAk(), now=EVENING)
        for key in ("shanghai", "shenzhen", "chinext"):
            self.assertEqual(snap[key]["source_kind"], "exact_close", key)
            self.assertEqual(snap[key]["as_of"], "20261009", key)
        self.assertEqual(snap["chinext"]["provider"], "akshare_sina_daily")
        self.assertGreater(snap["chinext"]["change_pct"], 0)
        self.assertEqual(snap["spx"]["change_pct"], 0.4)  # non-domestic entries untouched

    def test_unresolvable_index_becomes_unavailable_never_keeps_spot(self):
        snap = states.refresh_domestic_close(self.noon_snapshot(), "20261009", pro=FakePro(), ak_module=FakeAk({}), now=EVENING)
        self.assertEqual(snap["shanghai"]["source_kind"], "exact_close")
        self.assertEqual(snap["chinext"]["source_kind"], "unavailable")
        self.assertIsNone(snap["chinext"]["change_pct"])
        self.assertIsNone(snap["chinext"]["close"])
        self.assertTrue(snap["chinext"]["source_error"])

    def test_existing_same_day_settled_close_is_not_downgraded(self):
        current = self.noon_snapshot()
        current["chinext"] = {"key": "chinext", "source_kind": "exact_close", "provider": "rds",
                              "close": 3043.3, "prev_close": 3036.7, "change_pct": 0.22, "as_of": "20261009"}
        snap = states.refresh_domestic_close(current, "20261009", pro=FakePro(), ak_module=FakeAk({}), now=EVENING)
        self.assertEqual(snap["chinext"]["source_kind"], "exact_close")
        self.assertEqual(snap["chinext"]["change_pct"], 0.22)

    def test_previous_day_settled_close_is_replaced(self):
        current = self.noon_snapshot()
        current["chinext"] = {"key": "chinext", "source_kind": "exact_close", "change_pct": -3.1455, "as_of": "20261008"}
        snap = states.refresh_domestic_close(current, "20261009", pro=FakePro(), ak_module=FakeAk({}), now=EVENING)
        self.assertEqual(snap["chinext"]["source_kind"], "unavailable")

    def test_before_settlement_snapshot_is_left_unchanged(self):
        pro = FakePro()
        snap = states.refresh_domestic_close(self.noon_snapshot(), "20261009", pro=pro, ak_module=FakeAk(), now=NOON)
        self.assertEqual(snap["chinext"]["source_kind"], "intraday_spot")
        self.assertEqual(pro.calls, [])


class MiddaySessionSnapshotTests(unittest.TestCase):
    def setUp(self):
        stub = lambda *args, **kwargs: (None, "stubbed in test")  # noqa: E731
        patches = [mock.patch.object(midday, name, stub)
                   for name in ("fetch_akshare_global_index", "fetch_yfinance_snapshot", "fetch_cnh_snapshot_with_fallback")]
        patches += [
            mock.patch.object(midday, "_TS_PRO", FakePro()),
            mock.patch.object(midday, "ak", FakeAk()),
            mock.patch.object(dic.time, "sleep", no_sleep),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_post_close_run_never_falls_back_to_spot(self):
        def forbid(*args, **kwargs):
            raise AssertionError("spot source must not be used after the close")
        with mock.patch.object(midday, "fetch_akshare_domestic_index", forbid):
            snap, _ = midday.fetch_session_snapshot("20261009", now=EVENING)
        for key in ("shanghai", "shenzhen", "chinext"):
            self.assertEqual(snap[key]["source_kind"], "exact_close", key)
        self.assertEqual(snap["chinext"]["provider"], "akshare_sina_daily")

    def test_post_close_missing_index_is_unavailable_not_spot(self):
        with mock.patch.object(midday, "ak", FakeAk({})):
            snap, _ = midday.fetch_session_snapshot("20261009", now=EVENING)
        self.assertEqual(snap["chinext"]["source_kind"], "unavailable")
        self.assertIsNone(snap["chinext"]["change_pct"])

    def test_intraday_run_labels_fallbacks_honestly(self):
        spot = midday.build_snapshot(key="chinext", label="创业板指", symbol="sz399006", source_kind="intraday_spot",
                                     provider="akshare", close=2957.3, prev_close=3036.7, as_of=None)
        with mock.patch.object(midday, "fetch_akshare_domestic_index", lambda *a, **k: (spot, None)):
            snap, _ = midday.fetch_session_snapshot("20261009", now=NOON)
        self.assertEqual(snap["chinext"]["source_kind"], "intraday_spot")
        # global entries that failed are labelled unavailable, never "exact"
        self.assertEqual(snap["spx"]["source_kind"], "unavailable")


class ContractMissingIndexTests(unittest.TestCase):
    def _run(self, latest: Path) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(contract, "LATEST", latest), mock.patch.object(
            sys, "argv", ["validate_publication_contract.py", "--strict"]
        ), contextlib.redirect_stdout(output):
            result = contract.main()
        return result, output.getvalue()

    def _write_market(self, latest: Path, chinext: dict) -> None:
        _write(latest, "market_context.json", {"trade_date": "20260810", "close_actuals": {"breadth": 0.59}})
        _write(latest, "market_state.json", {"session_snapshot": {
            "shanghai": {"source_kind": "exact_close", "as_of": "20260810", "change_pct": 0.05},
            "shenzhen": {"source_kind": "exact_close", "as_of": "20260810", "change_pct": 0.17},
            "chinext": chinext,
        }})

    def test_stale_spot_contradiction_still_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            _base_latest(latest)
            self._write_market(latest, dict(NOON_SPOT_CHINEXT))
            result, output = self._run(latest)
        self.assertNotEqual(result, 0, output)
        self.assertIn("指数与涨跌面矛盾", output)

    def test_unavailable_index_degrades_with_warning_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            latest = Path(tmpdir)
            _base_latest(latest)
            self._write_market(latest, {"source_kind": "unavailable", "as_of": "20260810", "change_pct": None})
            result, output = self._run(latest)
        self.assertEqual(result, 0, output)
        self.assertIn("指数结算收盘缺失", output)
        self.assertNotIn("指数与涨跌面矛盾", output)


if __name__ == "__main__":
    unittest.main()
