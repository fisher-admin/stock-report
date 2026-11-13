"""Missing historical input is retried, not fetched anew for each rolling window."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import factor_attribution_engine as engine


class MissingClient:
    def __init__(self):
        self.calls = 0

    def daily_basic(self, **kwargs):
        self.calls += 1
        return pd.DataFrame()


class PitFetchBackoffTests(unittest.TestCase):
    def test_repeated_missing_day_uses_one_retry_batch_then_expires(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine.time, 'sleep'), \
             mock.patch.object(engine.time, 'monotonic', return_value=100) as clock:
            client = MissingClient()
            session = engine.PitFetchSession(client, Path(tmp))
            self.assertIsNone(session.load_daily_basic('20260415', []))
            self.assertIsNone(session.load_daily_basic('20260415', []))
            self.assertEqual(client.calls, 4, 'same missing input fetched again before backoff')
            clock.return_value = 401
            self.assertIsNone(session.load_daily_basic('20260415', []))
            self.assertEqual(client.calls, 8, 'missing input must be retried after bounded backoff')

    def test_new_local_data_immediately_overrides_backoff(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine.time, 'sleep'), \
             mock.patch.object(engine.time, 'monotonic', return_value=100):
            root = Path(tmp)
            client = MissingClient()
            session = engine.PitFetchSession(client, root / 'cache')
            source = root / 'daily_basic.parquet'
            self.assertIsNone(session.load_daily_basic('20260415', [source]))
            raw = pd.DataFrame([{'ts_code':'000001.SZ', 'turnover_rate':1.0, 'circ_mv':2.0, 'total_mv':3.0}])
            raw.to_parquet(source, index=False)
            actual = session.load_daily_basic('20260415', [source])
            pd.testing.assert_frame_equal(actual, engine._normalize_daily_basic(raw, '20260415'))
            self.assertEqual(client.calls, 4)
            source.unlink()  # a changed local input must not resurrect an old miss
            self.assertIsNone(session.load_daily_basic('20260415', [source]))
            self.assertEqual(client.calls, 8)

    def test_remote_recovery_after_expiry_returns_actual_data(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine.time, 'sleep'), \
             mock.patch.object(engine.time, 'monotonic', return_value=100) as clock:
            client = MissingClient()
            session = engine.PitFetchSession(client, Path(tmp))
            self.assertIsNone(session.load_daily_basic('20260415', []))
            raw = pd.DataFrame([{'ts_code':'000001.SZ', 'turnover_rate':1.0, 'circ_mv':2.0, 'total_mv':3.0}])
            clock.return_value = 401
            with mock.patch.object(client, 'daily_basic', return_value=raw):
                actual = session.load_daily_basic('20260415', [])
            pd.testing.assert_frame_equal(actual, engine._normalize_daily_basic(raw, '20260415'))

    def test_backoff_is_date_and_session_scoped(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine.time, 'sleep'), \
             mock.patch.object(engine.time, 'monotonic', return_value=100):
            client = MissingClient()
            session = engine.PitFetchSession(client, Path(tmp))
            session.load_daily_basic('20260415', [])
            session.load_daily_basic('20260416', [])
            self.assertEqual(client.calls, 8)
            another = engine.PitFetchSession(client, Path(tmp))
            another.load_daily_basic('20260415', [])
            self.assertEqual(client.calls, 12)
