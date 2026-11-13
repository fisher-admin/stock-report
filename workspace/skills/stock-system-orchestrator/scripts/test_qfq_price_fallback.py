import tempfile
import unittest
from pathlib import Path
from unittest import mock
import pandas as pd
from qfq_price_fallback import load_cached_qfq_prices, merge_qfq_with_daily_fallback


class AdjustmentContractTests(unittest.TestCase):
    def test_daily_basic_and_backup_files_are_not_price_inputs(self):
        official, _ = self.frames()
        daily = official[["trade_date", "ts_code", "close"]].copy()
        daily["open"] = daily["close"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official.to_parquet(root / "stk_factor_20260813.parquet")
            daily.to_parquet(root / "daily_20260813.parquet")
            daily.to_parquet(root / "daily_basic_20260813.parquet")
            daily.to_parquet(root / "daily_20260813_backup.parquet")
            result = load_cached_qfq_prices(root, mock.Mock(), minimum_fallback_date="20260811")
        self.assertEqual(len(result), 1)

    def test_rds_prices_are_retained_without_unnecessary_factor_refetch(self):
        official, _ = self.frames()
        official["source_provider"] = "rds"
        daily = official[["trade_date", "ts_code", "close"]].copy()
        daily["open"] = daily["close"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official.to_parquet(root / "stk_factor_20260813.parquet")
            daily.to_parquet(root / "daily_20260813.parquet")
            client = mock.Mock()
            result = load_cached_qfq_prices(root, client, minimum_fallback_date="20260811")
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0].close_qfq, 25.0)
        client.adj_factor.assert_not_called()

    def test_new_provider_does_not_bypass_adjustment_or_proxy_guards(self):
        for provider in ("rds", "promax"):
            official, _ = self.frames()
            official["source_provider"] = provider
            official["used_proxy"] = True
            self.assertTrue(merge_qfq_with_daily_fallback(official, pd.DataFrame()).empty)
            official["used_proxy"] = False
            official["adj_factor"] = float("nan")
            self.assertTrue(merge_qfq_with_daily_fallback(official, pd.DataFrame()).empty)

    def test_local_technical_fallback_cannot_impersonate_official_qfq(self):
        official, daily = self.frames()
        official["adj_factor"] = float("nan")
        official["source_provider"] = "local_history_derived_technical"
        self.assertTrue(merge_qfq_with_daily_fallback(official, pd.DataFrame()).empty)

    def test_unproven_daily_qfq_columns_are_rejected(self):
        _, daily = self.frames()
        daily = daily.rename(columns={"open": "open_qfq", "close": "close_qfq"})
        self.assertTrue(merge_qfq_with_daily_fallback(pd.DataFrame(), daily).empty)

    def frames(self):
        official = pd.DataFrame({"trade_date": ["20260813"], "ts_code": ["A"], "open_qfq": [25.0], "close_qfq": [25.0], "close": [50.0], "adj_factor": [2.0]})
        daily = pd.DataFrame({"trade_date": ["20260812"], "ts_code": ["A"], "open": [100.0], "close": [100.0]})
        return official, daily

    def test_raw_prices_without_factors_are_not_called_qfq(self):
        official, daily = self.frames()
        self.assertEqual(len(merge_qfq_with_daily_fallback(official, daily)), 1)

    def test_explicit_factor_and_official_anchor_preserve_adjustment_basis(self):
        official, daily = self.frames()
        daily["adj_factor"] = 1.0
        result = merge_qfq_with_daily_fallback(official, daily)
        added = result.loc[result.trade_date == "20260812"].iloc[0]
        self.assertEqual(added.close_qfq, 25.0)
        self.assertEqual(added.price_source, "official_daily_adj_factor_anchored_qfq")

    def test_official_factors_are_cached_and_reused(self):
        official, daily = self.frames()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official.to_parquet(root / "stk_factor_20260813.parquet")
            daily.to_parquet(root / "daily_20260812.parquet")
            client = mock.Mock()
            client.adj_factor.return_value = pd.DataFrame({"trade_date": ["20260812"], "ts_code": ["A"], "adj_factor": [1.0]})
            first = load_cached_qfq_prices(root, client, minimum_fallback_date="20260811")
            second = load_cached_qfq_prices(root, client, minimum_fallback_date="20260811")
            pd.testing.assert_frame_equal(first, second)
            self.assertEqual(first.loc[first.trade_date == "20260812", "close_qfq"].iloc[0], 25.0)
            client.adj_factor.assert_called_once_with(trade_date="20260812")

    def test_invalid_factor_response_is_not_cached(self):
        official, daily = self.frames()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official.to_parquet(root / "stk_factor_20260813.parquet")
            daily.to_parquet(root / "daily_20260812.parquet")
            client = mock.Mock()
            client.adj_factor.return_value = pd.DataFrame({"trade_date": ["20260813"], "ts_code": ["A"], "adj_factor": [1.0]})
            with self.assertRaisesRegex(ValueError, "invalid official adjustment"):
                load_cached_qfq_prices(root, client, minimum_fallback_date="20260811")
            self.assertFalse((root / "adj_factor_20260812.parquet").exists())

    def test_duplicate_price_keys_are_rejected(self):
        official, daily = self.frames()
        with self.assertRaisesRegex(ValueError, "duplicate official"):
            merge_qfq_with_daily_fallback(pd.concat([official, official]), daily)

if __name__ == "__main__":
    unittest.main()
