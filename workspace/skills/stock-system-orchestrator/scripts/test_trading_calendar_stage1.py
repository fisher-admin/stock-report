#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent


def load_stage1_module():
    if "python_env_bootstrap" not in sys.modules:
        bootstrap = types.ModuleType("python_env_bootstrap")
        bootstrap.bootstrap_openclaw_venv = lambda: {"active": False}
        sys.modules["python_env_bootstrap"] = bootstrap
    if "tushare" not in sys.modules:
        ts = types.ModuleType("tushare")
        ts.set_token = lambda token=None: None
        sys.modules["tushare"] = ts
    if "pipeline" not in sys.modules:
        pipeline = types.ModuleType("pipeline")
        pipeline.TUSHARE_TOKEN = "offline-test-token"
        pipeline.init_tushare = lambda: None
        sys.modules["pipeline"] = pipeline
    if "backtest" not in sys.modules:
        backtest = types.ModuleType("backtest")
        backtest.ENABLE_AKSHARE_CYQ_FALLBACK = True
        backtest.get_cached = lambda *args, **kwargs: None
        sys.modules["backtest"] = backtest
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("stage1_data_prepare", SCRIPT_DIR / "stage1_data_prepare.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Stage1TradingCalendarTests(unittest.TestCase):
    def test_proxy_is_rejected_but_does_not_cancel_provider_retry_budget(self):
        module = load_stage1_module()
        ok, errors = module.apply_quality_guards(
            quality_ok=True, quality_errors=[], calendar_ok=True,
            refreshed={"cyq_perf": {"provider": "local_same_day_cyq_proxy_regression"}},
        )
        self.assertFalse(ok)
        self.assertIn("cyq proxy forbidden", errors)
        self.assertFalse(module.has_fatal_quality_errors(errors))

    def test_materialize_trading_calendar_writes_persistent_open_days(self):
        module = load_stage1_module()

        class FakePro:
            def trade_cal(self, **kwargs):
                return pd.DataFrame(
                    {
                        "cal_date": ["20260807", "20260808", "20260809", "20260810", "20260930"],
                        "is_open": [1, 0, 0, 1, 1],
                        "pretrade_date": ["20260806", "20260807", "20260807", "20260807", "20260929"],
                    }
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "trading_calendar.json"
            payload = module.materialize_trading_calendar(FakePro(), "20260810", output_path=out)
            written = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(payload["source"], "tushare_trade_cal")
        self.assertEqual(written["from"], "20260807")
        self.assertEqual(written["to"], "20260930")
        self.assertEqual(written["open_dates"], ["20260807", "20260810", "20260930"])

    def test_materialize_trading_calendar_rejects_invalid_payload(self):
        module = load_stage1_module()

        class BadPro:
            def trade_cal(self, **kwargs):
                return pd.DataFrame({"cal_date": ["20260810"], "is_open": [None]})

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "trading_calendar.json"
            with self.assertRaises(ValueError):
                module.materialize_trading_calendar(BadPro(), "20260810", output_path=out)
            self.assertFalse(out.exists())

    def test_apply_quality_guards_rejects_missing_calendar_and_proxy_cyq(self):
        module = load_stage1_module()
        ok, errors = module.apply_quality_guards(
            quality_ok=True,
            quality_errors=[],
            refreshed={"cyq_perf": {"provider": "local_same_day_cyq_proxy_regression"}},
            calendar_ok=False,
        )
        self.assertFalse(ok)
        self.assertIn("trading calendar unavailable or invalid", errors)
        self.assertIn("cyq proxy forbidden", errors)

    def test_primary_provider_is_not_marked_degraded(self):
        module = load_stage1_module()
        for provider in ("rds", "promax", "rds+promax", "rds_primary_promax_fallback"):
            self.assertFalse(module.is_degraded_fallback_source(provider), provider)
        self.assertTrue(module.is_degraded_fallback_source("local_same_day_cyq_proxy_regression"))

    def test_main_returns_nonzero_when_calendar_materialize_fails(self):
        module = load_stage1_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = "20260810"
            cache_dir = Path(tmpdir) / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            for data_type in ("stk_factor", "daily", "cyq_perf"):
                (cache_dir / f"{data_type}_{target}.parquet").write_text("x", encoding="utf-8")
            health_path = Path(tmpdir) / "health.json"
            payloads: list[dict] = []
            env = {"OPENCLAW_TARGET_TRADE_DATE": target}

            def fake_write_health(trade_date, payload):
                payloads.append(json.loads(json.dumps(payload)))
                health_path.write_text(json.dumps(payload), encoding="utf-8")
                return health_path

            with mock.patch.object(module, "WORKSPACE", Path(tmpdir)), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(module.ts, "set_token", return_value=None), \
                mock.patch.object(module.pl, "TUSHARE_TOKEN", "offline-test-token"), \
                mock.patch.object(module.pl, "init_tushare", return_value=object()), \
                mock.patch.object(module, "latest_open_trade_date", return_value=(target, {"selection_source": "test"})), \
                mock.patch.object(module, "materialize_trading_calendar", side_effect=ValueError("calendar bad")), \
                mock.patch.object(module, "collect_cache_dates", return_value={k: {target} for k in ("stk_factor", "daily", "cyq_perf")}), \
                mock.patch.object(module.bt, "get_cached", side_effect=lambda *args: pd.DataFrame({"source_provider": ["tushare"]})), \
                mock.patch.object(module, "inspect_cache_quality", return_value=(True, [])), \
                mock.patch.object(module, "CACHE", cache_dir), \
                mock.patch.object(module, "write_health", side_effect=fake_write_health), \
                mock.patch.object(module.time, "sleep", side_effect=AssertionError("sleep should not be called")):
                rc = module.main()

        self.assertEqual(rc, 1)
        self.assertTrue(payloads)
        self.assertFalse(payloads[-1]["ok"])
        self.assertIn("trading calendar unavailable or invalid", payloads[-1]["quality_errors"])

    def test_main_fails_closed_on_proxy_cyq_provider(self):
        module = load_stage1_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = "20260810"
            cache_dir = Path(tmpdir) / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            for data_type in ("stk_factor", "daily", "cyq_perf"):
                (cache_dir / f"{data_type}_{target}.parquet").write_text("x", encoding="utf-8")
            payloads: list[dict] = []
            env = {"OPENCLAW_TARGET_TRADE_DATE": target}

            def fake_write_health(trade_date, payload):
                payloads.append(json.loads(json.dumps(payload)))
                return Path(tmpdir) / "health.json"

            def fake_get_cached(_pro, _trade_date, data_type):
                provider = "local_same_day_cyq_proxy_regression" if data_type == "cyq_perf" else "tushare"
                return pd.DataFrame({"source_provider": [provider]})

            with mock.patch.object(module, "WORKSPACE", Path(tmpdir)), \
                mock.patch.object(module, "MAX_WAIT_SECONDS", 0), \
                mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(module.ts, "set_token", return_value=None), \
                mock.patch.object(module.pl, "TUSHARE_TOKEN", "offline-test-token"), \
                mock.patch.object(module.pl, "init_tushare", return_value=object()), \
                mock.patch.object(module, "latest_open_trade_date", return_value=(target, {"selection_source": "test"})), \
                mock.patch.object(module, "materialize_trading_calendar", return_value={"open_dates": [target, "20260811"], "to": "20261031"}), \
                mock.patch.object(module, "collect_cache_dates", return_value={k: {target} for k in ("stk_factor", "daily", "cyq_perf")}), \
                mock.patch.object(module.bt, "get_cached", side_effect=fake_get_cached), \
                mock.patch.object(module, "inspect_cache_quality", return_value=(True, [])), \
                mock.patch.object(module, "CACHE", cache_dir), \
                mock.patch.object(module, "write_health", side_effect=fake_write_health), \
                mock.patch.object(module.time, "sleep", side_effect=AssertionError("sleep should not be called")):
                rc = module.main()

        self.assertEqual(rc, 1)
        self.assertTrue(payloads)
        self.assertFalse(payloads[-1]["ok"])
        self.assertIn("cyq proxy forbidden", " ".join(payloads[-1]["quality_errors"]))

    def test_main_retries_proxy_until_real_provider_recovers(self):
        module = load_stage1_module()
        target = "20260810"
        cyq_calls = []
        payloads = []
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "cache"
            cache.mkdir()
            for label in module.DATA_TYPES:
                (cache / f"{label}_{target}.parquet").write_bytes(b"test-fixture")
            def fetch(_pro, _date, label):
                if label == "cyq_perf":
                    cyq_calls.append(label)
                source = "local_same_day_cyq_proxy" if label == "cyq_perf" and len(cyq_calls) == 1 else "rds"
                return pd.DataFrame({"source_provider": [source]})
            with mock.patch.object(module, "WORKSPACE", Path(tmpdir)), \
                 mock.patch.object(module, "CACHE", cache), \
                 mock.patch.object(module.ts, "set_token", side_effect=AssertionError("provider client must not persist a legacy token")) as legacy_token, \
                 mock.patch.object(module.pl, "init_tushare", return_value=object()), \
                 mock.patch.object(module, "latest_open_trade_date", return_value=(target, {})), \
                 mock.patch.object(module, "materialize_trading_calendar", return_value={"open_dates": [target]}), \
                 mock.patch.object(module.bt, "get_cached", side_effect=fetch), \
                 mock.patch.object(module, "inspect_cache_quality", return_value=(True, [])), \
                 mock.patch.object(module, "write_health", side_effect=lambda date, doc: payloads.append(doc.copy()) or cache), \
                 mock.patch.object(module.time, "sleep") as sleep:
                result = module.main()
            self.assertEqual(result, 0)
            legacy_token.assert_not_called()
            self.assertEqual(len(cyq_calls), 2)
            sleep.assert_called_once_with(module.RETRY_INTERVAL_SECONDS)
            self.assertFalse(payloads[0]["ok"])
            self.assertFalse(payloads[0]["same_day_ready"])
            self.assertTrue(payloads[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
