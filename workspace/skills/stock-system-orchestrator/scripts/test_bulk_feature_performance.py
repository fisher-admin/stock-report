"""Keep the existing trailing-window values while removing per-stock callbacks."""
import unittest
from unittest import mock

import numpy as np
import pandas as pd
import prebreakout_factory as factory


bt = factory.bt


def reference_panel(frames):
    panel = pd.concat(frames, ignore_index=True).dropna(subset=['ts_code', 'close'])
    panel = panel.sort_values(['ts_code', 'trade_date'], kind='mergesort')
    grouped = panel.groupby('ts_code', sort=False)
    if 'vol' in panel.columns:
        panel['prev_vol_ma5'] = grouped['vol'].transform(lambda s: s.shift(1).rolling(5, min_periods=5).mean())
        panel['log_vol'] = np.log1p(panel['vol'].clip(lower=0))
        panel['turnover_stab'] = -panel.groupby('ts_code', sort=False)['log_vol'].transform(lambda s: s.rolling(5, min_periods=5).std())
    if 'pct_chg' in panel.columns:
        panel['volatility_20d'] = grouped['pct_chg'].transform(lambda s: s.rolling(20, min_periods=10).std())
    panel['close_m5'] = grouped['close'].transform(lambda s: s.shift(5))
    panel['close_m20'] = grouped['close'].transform(lambda s: s.shift(20))
    return panel


class BulkFeaturePerformanceTests(unittest.TestCase):
    def frames(self, *, optional=True):
        dates = pd.bdate_range('2026-07-01', periods=35).strftime('%Y%m%d').tolist()
        frames = {}
        for i, day in enumerate(dates):
            rows = []
            for j, code in enumerate(('000002.SZ', '000001.SZ', '600001.SH', '600003.SH')):
                if j == 2 and i % 4 == 0:
                    continue  # preserve missing-session semantics
                row = {'ts_code': code, 'close': 10 + j + i / 10, 'trade_date': day}
                if optional:
                    row.update(vol=np.nan if i == 6 else 100 + i * j,
                               pct_chg=np.nan if i == 9 else (i % 7 - 3) / 10)
                rows.append(row)
            if i == 8:
                rows.append({'ts_code': None, 'close': 2, 'trade_date': day})
            frames[day] = pd.DataFrame(rows)
        return dates, frames

    def build(self, dates, frames):
        with mock.patch.object(bt, '_FEATURE_PANEL', None), mock.patch.object(bt, '_FEATURE_PANEL_RANGE', None), \
             mock.patch.object(bt, '_list_cached_daily_dates', return_value=dates), \
             mock.patch.object(bt, '_load_daily_frame_for_features', side_effect=frames.get):
            return bt._ensure_feature_panel(dates[0], dates[-1])

    def test_values_and_order_are_exactly_unchanged_with_missing_sessions(self):
        for optional in (True, False):
            with self.subTest(optional=optional):
                dates, frames = self.frames(optional=optional)
                before = {day: frame.copy(deep=True) for day, frame in frames.items()}
                expected = reference_panel(list(frames.values()))
                actual = self.build(dates, frames)
                pd.testing.assert_frame_equal(actual, expected, check_exact=True)
                for day in dates:
                    pd.testing.assert_frame_equal(frames[day], before[day])

    def test_rolling_features_do_not_dispatch_python_callback_for_each_stock(self):
        dates, frames = self.frames()
        group_type = type(frames[dates[0]].groupby('ts_code')['close'])
        original = group_type.transform
        callbacks = []
        def observed(group, func, *args, **kwargs):
            if callable(func):
                callbacks.append(func)
            return original(group, func, *args, **kwargs)
        with mock.patch.object(group_type, 'transform', observed):
            self.build(dates, frames)
        self.assertEqual(len(callbacks), 0, 'per-stock Python transforms still scale with the full universe')

    def test_all_invalid_prices_preserve_empty_panel(self):
        day = '20260911'
        frames = {day: pd.DataFrame([{'ts_code': '000001.SZ', 'close': np.nan,
                                     'vol': 1.0, 'pct_chg': 0.0, 'trade_date': day}])}
        expected = reference_panel(list(frames.values()))
        actual = self.build([day], frames)
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)
