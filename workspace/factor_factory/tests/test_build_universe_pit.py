from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_universe.py"
SPEC = importlib.util.spec_from_file_location("build_universe_pit", MODULE_PATH)
assert SPEC and SPEC.loader
UNIVERSE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = UNIVERSE
SPEC.loader.exec_module(UNIVERSE)


def _pit_rows(trade_date: str, *, used_proxy: bool = False) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": trade_date,
                "ts_code": "000001.SZ",
                "name": "历史名称",
                "list_status_at_date": "L",
                "is_st": False,
                "is_suspended": False,
                "listing_days": 1000,
                "universe_flag": 1,
                "tradable": 1,
                "used_proxy": used_proxy,
                "completeness": "complete",
            }
        ]
    )


def test_pit_universe_loader_requires_every_date_and_rejects_proxy_rows() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _pit_rows("20250102").to_parquet(root / "universe_20250102.parquet", index=False)

        loaded = UNIVERSE.load_pit_universe_panel(root, ["20250102"])
        assert loaded.loc[0, "name"] == "历史名称"

        with pytest.raises(UNIVERSE.UniverseDataIntegrityError):
            UNIVERSE.load_pit_universe_panel(root, ["20250102", "20250103"])

        _pit_rows("20250103", used_proxy=True).to_parquet(
            root / "universe_20250103.parquet", index=False
        )
        with pytest.raises(UNIVERSE.UniverseDataIntegrityError):
            UNIVERSE.load_pit_universe_panel(root, ["20250102", "20250103"])
