from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_factor_audit.py"
SPEC = importlib.util.spec_from_file_location("run_factor_audit_integrity", MODULE_PATH)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUDIT
SPEC.loader.exec_module(AUDIT)

PANEL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_factor_panel.py"
PANEL_SPEC = importlib.util.spec_from_file_location("build_factor_panel_integrity", PANEL_PATH)
assert PANEL_SPEC and PANEL_SPEC.loader
PANEL = importlib.util.module_from_spec(PANEL_SPEC)
sys.modules[PANEL_SPEC.name] = PANEL
PANEL_SPEC.loader.exec_module(PANEL)


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        min_rank_icir=0.30,
        max_rank_ic_pvalue_newey_west=0.05,
        max_rank_ic_qvalue_fdr=0.05,
        phase2_gates_enabled=True,
    )


def _report(icir: float) -> dict:
    return {
        "factor_id": "test_factor",
        "status": "whitelist",
        "rank_ic_stats": {"icir": icir, "p_value_newey_west": 0.001},
        "walk_forward_ic_summary": {"wf_ic_pass": True},
        "noise_test": {"noise_robust_pass": True},
        "gate_details": {},
    }


def test_factor_direction_gate_rejects_negative_icir_even_when_magnitude_is_large() -> None:
    whitelist, coldlist = AUDIT.finalize_phase2_status([_report(-0.80)], _config())

    assert whitelist == []
    assert len(coldlist) == 1
    assert coldlist[0]["status"] == "cold_storage_phase2_stat_gate"
    assert coldlist[0]["gate_details"]["icir_pass"] is False


def test_factor_direction_gate_requires_strictly_more_than_positive_point_three() -> None:
    at_threshold, _ = AUDIT.finalize_phase2_status([_report(0.30)], _config())
    above_threshold, _ = AUDIT.finalize_phase2_status([_report(0.3001)], _config())

    assert at_threshold == []
    assert len(above_threshold) == 1
    assert above_threshold[0]["gate_details"]["icir_pass"] is True


def test_default_factor_label_enters_at_the_next_open() -> None:
    assert AUDIT.DEFAULT_LABEL_MODE == "open_to_close"
    assert AUDIT.resolve_label_column(AUDIT.DEFAULT_LABEL_MODE) == "next_return_open_to_close_1d"


def test_factor_audit_uses_base_and_stress_cost_contract() -> None:
    config = AUDIT.load_runtime_config(Path(__file__).resolve().parents[1])

    assert config.round_trip_cost == 0.003
    assert config.stress_round_trip_cost == 0.005


def test_factor_panel_uses_qfq_prices_and_skips_proxy_chip_data() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cache = Path(tmpdir)
        pd.DataFrame(
            [{
                "ts_code": "000001.SZ",
                "trade_date": "20250102",
                "open": 1.0,
                "high": 2.0,
                "low": 1.0,
                "close": 2.0,
                "pre_close": 1.0,
                "pct_chg": 100.0,
                "vol": 100.0,
                "amount": 1000.0,
            }]
        ).to_parquet(cache / "daily_20250102.parquet", index=False)
        pd.DataFrame(
            [{
                "ts_code": "000001.SZ",
                "trade_date": "20250102",
                "open_qfq": 10.0,
                "high_qfq": 11.5,
                "low_qfq": 9.8,
                "close_qfq": 11.0,
                "pre_close_qfq": 10.2,
                "source_provider": "tushare_stk_factor",
            }]
        ).to_parquet(cache / "stk_factor_20250102.parquet", index=False)
        pd.DataFrame(
            [{
                "ts_code": "000001.SZ",
                "trade_date": "20250102",
                "cost_15pct": 9.0,
                "cost_50pct": 10.0,
                "cost_85pct": 11.0,
                "weight_avg": 10.0,
                "winner_rate": 50.0,
                "source_provider": "local_same_day_cyq_proxy_regression",
                "fallback_source": "local_same_day_cyq_proxy",
            }]
        ).to_parquet(cache / "cyq_perf_20250102.parquet", index=False)

        frame = PANEL.load_market_panel(SimpleNamespace(cache_dir=cache), ["20250102"])

        assert frame.loc[0, "open"] == 10.0
        assert frame.loc[0, "close"] == 11.0
        assert frame.loc[0, "price_basis"] == "qfq"
        assert frame.loc[0, "chip_data_status"] == "proxy_rejected"
        assert "winner_rate" not in frame.columns
