#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE_DIR", str(Path(__file__).resolve().parents[3]))).resolve()
PUBLIC_STOCK_ANALYZER = SCRIPT_DIR.parent / "stock_analyzer"
STOCK_ANALYZER = Path(
    os.environ.get(
        "STOCK_ANALYZER_DIR",
        str(PUBLIC_STOCK_ANALYZER if PUBLIC_STOCK_ANALYZER.exists() else WORKSPACE / "skills/stock-analyzer"),
    )
)
import sys

if str(STOCK_ANALYZER) not in sys.path:
    sys.path.insert(0, str(STOCK_ANALYZER))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import backtest as bt  # noqa: E402
import pit_market_snapshot as pms  # noqa: E402
import pipeline as pl  # noqa: E402
import short_track_shadow  # noqa: E402
import shadow_portfolio_evaluator as spe  # noqa: E402
from trading_calendar_store import load_open_trade_dates  # noqa: E402


class RunnerInputError(RuntimeError):
    pass


VALIDATION_START_DATE = "20260811"


@dataclass
class RunnerPaths:
    workspace: Path
    production_strategy_backtests: Path
    backtest_cache_dir: Path
    health_dir: Path
    shadow_universe_dir: Path
    shadow_daily_basic_dir: Path
    common_pit_market_dir: Path
    short_track_daily_dir: Path
    short_track_ledger_dir: Path
    short_track_portfolio_daily_dir: Path


def build_paths(workspace_dir: Path) -> RunnerPaths:
    stock_working = workspace_dir / "stock_data" / "03-working"
    return RunnerPaths(
        workspace=workspace_dir,
        production_strategy_backtests=stock_working / "stock-report-repo" / "data" / "strategy_backtests.json",
        backtest_cache_dir=stock_working / "backtest_cache",
        health_dir=stock_working / "health",
        shadow_universe_dir=stock_working / "strategy_research" / "short_track" / "materialized" / "pit_universe",
        shadow_daily_basic_dir=stock_working / "strategy_research" / "short_track" / "materialized" / "daily_basic",
        common_pit_market_dir=stock_working / "fundamental_cache" / "pit_market",
        short_track_daily_dir=stock_working / "strategy_research" / "short_track" / "daily",
        short_track_ledger_dir=stock_working / "strategy_research" / "short_track" / "ledger",
        short_track_portfolio_daily_dir=stock_working / "strategy_research" / "short_track" / "portfolio_daily",
    )


def load_production_prebreakout_snapshot(workspace_dir: Path) -> dict[str, Any]:
    path = build_paths(workspace_dir).production_strategy_backtests
    if not path.exists():
        raise RunnerInputError(f"missing production snapshot: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RunnerInputError("invalid strategy_backtests.json payload")
    return payload


def _find_prebreakout_strategy(snapshot: dict[str, Any]) -> dict[str, Any]:
    for strategy in snapshot.get("strategies") or []:
        if isinstance(strategy, dict) and strategy.get("id") == "prebreakout_v41":
            return strategy
    raise RunnerInputError("production snapshot missing prebreakout_v41")


def _normalize_prod_top20(strategy: dict[str, Any]) -> list[dict[str, Any]]:
    rows = strategy.get("top20") or []
    if len(rows) < 20:
        raise RunnerInputError("production prebreakout_v41 top20 is incomplete")
    normalized = []
    for idx, row in enumerate(rows[:20], start=1):
        code = str(row.get("ts_code") or row.get("code") or "").strip()
        if not code:
            raise RunnerInputError("production top20 contains missing ts_code")
        normalized.append(
            {
                **row,
                "ts_code": code,
                "score": round(float(row.get("score") or 0.0), 1),
                "rank_no": int(row.get("rank_no") or row.get("rank") or idx),
                "rank": int(row.get("rank") or row.get("rank_no") or idx),
            }
        )
    return normalized


def load_health_payload(workspace_dir: Path) -> dict[str, Any]:
    path = build_paths(workspace_dir).health_dir / "data_preparation_run.json"
    if not path.exists():
        raise RunnerInputError(f"missing data-preparation health evidence: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerInputError(f"invalid data-preparation health evidence: {path}") from exc
    if "cyq_perf_proxy_derived" not in payload:
        raise RunnerInputError("health evidence does not declare CYQ proxy state")
    return payload


def _read_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise RunnerInputError(f"missing cache file: {path}")
    return pd.read_parquet(path)


def score_full_ranked_pool(trade_date: str, workspace_dir: Path) -> list[dict[str, Any]]:
    paths = build_paths(workspace_dir)
    bt.CACHE_DIR = paths.backtest_cache_dir
    stk = _read_parquet(paths.backtest_cache_dir / f"stk_factor_{trade_date}.parquet")
    cyq = _read_parquet(paths.backtest_cache_dir / f"cyq_perf_{trade_date}.parquet")
    scored = bt.score_from_bulk(stk, cyq, mode="prebreakout", trade_date=trade_date)
    ranked = []
    for idx, row in enumerate(scored, start=1):
        ranked.append(
            {
                **row,
                "rank_no": idx,
                "rank": idx,
            }
        )
    return ranked


def attach_pit_industry(
    rows: list[dict[str, Any]],
    universe: pd.DataFrame,
    *,
    omit_missing: bool = False,
) -> list[dict[str, Any]]:
    industry = universe.set_index("ts_code")["industry_name"].to_dict()
    sw_industry = universe.set_index("ts_code")["sw2021_l1_name"].to_dict()
    enriched = []
    for row in rows:
        clone = dict(row)
        ts_code = str(clone.get("ts_code") or "")
        target_sw = sw_industry.get(ts_code)
        if target_sw is None or pd.isna(target_sw) or not str(target_sw).strip():
            if omit_missing:
                continue
            raise RunnerInputError(f"missing target-date SW2021 L1 industry for {ts_code}")
        target_sw = str(target_sw).strip()
        clone["source_industry_name"] = clone.get("industry_name")
        clone["industry_name"] = target_sw
        clone["sw2021_l1_name"] = target_sw
        enriched.append(clone)
    return enriched


def validate_control_parity(prod_top20: list[dict[str, Any]], ranked_pool: list[dict[str, Any]]) -> None:
    if len(ranked_pool) < 20:
        raise RunnerInputError("ranked pool is shorter than production Top20")
    for expected, actual in zip(prod_top20, ranked_pool[:20]):
        exp_code = str(expected["ts_code"])
        act_code = str(actual.get("ts_code") or "")
        exp_score = round(float(expected.get("score") or 0.0), 1)
        act_score = round(float(actual.get("score") or 0.0), 1)
        if exp_code != act_code or exp_score != act_score:
            raise RunnerInputError(
                f"production parity failed at {exp_code}: expected score={exp_score}, got {act_code} score={act_score}"
            )


def fetch_official_stk_factor_history(client: Any, trade_dates: list[str]) -> pd.DataFrame:
    frames = []
    for trade_date in trade_dates:
        frame = client.stk_factor(trade_date=trade_date)
        if frame is None or len(frame) == 0:
            raise RunnerInputError(f"official stk_factor missing for {trade_date}")
        current_dates = set(frame.get("trade_date", pd.Series(dtype=str)).astype(str))
        if current_dates != {trade_date}:
            raise RunnerInputError(f"official stk_factor date mismatch for {trade_date}")
        if "source_provider" in frame.columns and frame["source_provider"].astype(str).str.contains("proxy", case=False).any():
            raise RunnerInputError("official stk_factor contains proxy provenance")
        clone = frame.copy()
        clone["used_proxy"] = False
        clone["completeness"] = "complete"
        clone["source"] = "tushare_stk_factor"
        frames.append(clone)
    if len(frames) < 21:
        raise RunnerInputError("official stk_factor history shorter than 21 open days")
    return pd.concat(frames, ignore_index=True)


def fetch_official_daily_basic_history(client: Any, trade_dates: list[str]) -> pd.DataFrame:
    frames = []
    for trade_date in trade_dates:
        frame = client.daily_basic(trade_date=trade_date, fields=None)
        if frame is None or len(frame) == 0:
            raise RunnerInputError(f"official daily_basic missing for {trade_date}")
        current_dates = set(frame.get("trade_date", pd.Series(dtype=str)).astype(str))
        if current_dates != {trade_date}:
            raise RunnerInputError(f"official daily_basic date mismatch for {trade_date}")
        if "source_provider" in frame.columns and frame["source_provider"].astype(str).str.contains("proxy", case=False).any():
            raise RunnerInputError("official daily_basic contains proxy provenance")
        clone = frame.copy()
        clone["used_proxy"] = False
        clone["completeness"] = "complete"
        clone["source"] = "tushare_daily_basic"
        frames.append(clone)
    if len(frames) < 21:
        raise RunnerInputError("official daily_basic history shorter than 21 open days")
    return pd.concat(frames, ignore_index=True)


def _compare_or_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if path.exists():
        existing = json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2, sort_keys=True)
        if existing != text:
            raise RunnerInputError(f"immutable snapshot mismatch: {path}")
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _write_parquet_atomic(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".parquet", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(temporary)
    try:
        frame.to_parquet(tmp_path, index=False)
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _compare_or_write_parquet(path: Path, frame: pd.DataFrame, sort_cols: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = frame.sort_values(sort_cols).reset_index(drop=True)
    if path.exists():
        existing = pd.read_parquet(path).sort_values(sort_cols).reset_index(drop=True)
        logical_existing = existing.copy()
        logical_normalized = normalized.copy()
        for logical in (logical_existing, logical_normalized):
            for column in logical.columns:
                values = logical[column].astype(object)
                values[pd.isna(values)] = None
                logical[column] = values
        try:
            pd.testing.assert_frame_equal(
                logical_existing,
                logical_normalized,
                check_dtype=False,
                check_like=False,
            )
        except AssertionError as exc:
            raise RunnerInputError(f"immutable parquet mismatch: {path}") from exc
        else:
            return
    _write_parquet_atomic(path, normalized)


def expected_signal_dates(open_trade_dates: list[str], *, as_of_date: str) -> list[str]:
    calendar = sorted({str(date) for date in open_trade_dates})
    matured: list[str] = []
    for signal_date in calendar:
        if signal_date < VALIDATION_START_DATE or signal_date > as_of_date:
            continue
        following = [date for date in calendar if date > signal_date]
        if len(following) >= 5 and following[4] <= as_of_date:
            matured.append(signal_date)
    return matured


def resolve_trade_date(workspace_dir: Path) -> str:
    snapshot = load_production_prebreakout_snapshot(workspace_dir)
    latest = str(snapshot.get("latest_trade_date") or "").strip()
    if len(latest) == 8 and latest.isdigit():
        return latest
    raise RunnerInputError("cannot resolve trade_date from production snapshot")


class ShortTrackShadowRunner:
    def __init__(self, *, workspace_dir: Path | None = None, client: Any):
        self.workspace_dir = Path(workspace_dir or WORKSPACE).resolve()
        self.client = client
        self.paths = build_paths(self.workspace_dir)

    def update_ledger(self, strategy_id: str, snapshot: dict[str, Any], open_trade_dates: list[str]) -> pd.DataFrame:
        self.paths.short_track_ledger_dir.mkdir(parents=True, exist_ok=True)
        path = self.paths.short_track_ledger_dir / f"{strategy_id}_ledger.parquet"
        existing = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=spe.LEDGER_COLUMNS)
        updated = spe.pending_rows_from_snapshot(snapshot, existing=existing, open_trade_dates=open_trade_dates)
        _write_parquet_atomic(path, updated)
        return updated

    def _load_prices_for_settlement(self) -> pd.DataFrame:
        rows = []
        for path in sorted(self.paths.backtest_cache_dir.glob("stk_factor_*.parquet")):
            df = pd.read_parquet(path, columns=["trade_date", "ts_code", "open_qfq", "close_qfq"])
            rows.append(df)
        if not rows:
            return pd.DataFrame(columns=["trade_date", "ts_code", "open_qfq", "close_qfq"])
        out = pd.concat(rows, ignore_index=True)
        out["trade_date"] = out["trade_date"].astype(str)
        out["ts_code"] = out["ts_code"].astype(str)
        return out.drop_duplicates(["trade_date", "ts_code"], keep="last")

    def _load_universe_history(self) -> pd.DataFrame:
        rows = []
        for path in sorted(self.paths.shadow_universe_dir.glob("universe_*.parquet")):
            rows.append(pd.read_parquet(path))
        if not rows:
            return pd.DataFrame(columns=["trade_date", "ts_code", "universe_flag", "tradable"])
        return pd.concat(rows, ignore_index=True)

    def build_candidate_tracking_report(
        self,
        strategy_id: str,
        strategy_version: str,
        ledger: pd.DataFrame,
        *,
        expected_signal_dates: list[str],
        operational_ok: bool,
        operational_evidence: dict[str, Any],
        as_of_date: str | None = None,
        portfolio_daily: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        if expected_signal_dates:
            verdict = spe.evaluate_short_track_promotion(
                ledger,
                expected_signal_dates=expected_signal_dates,
                validation_through_date=as_of_date,
                portfolio_daily=portfolio_daily,
            )
        else:
            verdict = {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "validation_start_date": spe.VALIDATION_START_DATE,
                "sample_trade_days": 0,
                "expected_trade_days": 0,
                "validation_through_date": as_of_date,
                "missing_trade_days": 0,
                "incomplete_trade_days": 0,
                "incomplete_trade_date_sample": [],
                "settled_security_rows": 0,
                "failed_gates": ["insufficient_matured_trade_days"],
                "all_gates_pass": False,
                "decision": "observe_only",
                "execution_authority": "observe_only_no_auto_order",
            }
        return spe.build_tracking_report(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            operational_ok=operational_ok,
            operational_evidence=operational_evidence,
            promotion_verdict=verdict,
        )

    def run(self, trade_date: str) -> dict[str, Any]:
        open_dates = load_open_trade_dates(self.paths.health_dir / "trading_calendar.json")
        recent_dates = [date for date in open_dates if date <= trade_date][-21:]
        if len(recent_dates) < 21:
            raise RunnerInputError("need at least 21 exchange open days through target trade date")

        production = load_production_prebreakout_snapshot(self.workspace_dir)
        prebreakout = _find_prebreakout_strategy(production)
        prod_top20 = _normalize_prod_top20(prebreakout)
        ranked_pool = score_full_ranked_pool(trade_date, self.workspace_dir)
        validate_control_parity(prod_top20, ranked_pool)

        pit_snapshot = pms.collect_pit_market_snapshot(self.client, trade_date)
        ranked_pool = attach_pit_industry(
            ranked_pool,
            pit_snapshot["universe"],
            omit_missing=True,
        )
        control_rows = attach_pit_industry(prod_top20, pit_snapshot["universe"])

        official_stk = fetch_official_stk_factor_history(self.client, recent_dates)
        official_daily_basic = fetch_official_daily_basic_history(self.client, recent_dates)
        balanced_frame = short_track_shadow.build_balanced_feature_frame(
            price_history=official_stk,
            daily_basic_history=official_daily_basic,
            pit_universe=pit_snapshot["universe"],
            trade_date=trade_date,
        )
        health_payload = load_health_payload(self.workspace_dir)
        if str(health_payload.get("target_trade_date") or "") != trade_date:
            raise RunnerInputError("data-preparation health date does not match shadow signal date")
        if not bool(health_payload.get("ok")) or not bool(health_payload.get("quality_ok")):
            raise RunnerInputError("data-preparation health is not complete/healthy")
        signal_cutoff = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T15:00:00+08:00"
        snapshots = short_track_shadow.build_short_track_candidate_snapshots(
            control_rows=control_rows,
            balanced_frame=balanced_frame,
            trade_date=trade_date,
            signal_cutoff=signal_cutoff,
            exchange_trade_dates=open_dates,
            health_payload=health_payload,
        )
        snapshots[short_track_shadow.TOP15_STRATEGY_ID] = short_track_shadow.build_top15_candidate_snapshot(
            ranked_pool,
            trade_date=trade_date,
            signal_cutoff=signal_cutoff,
            exchange_trade_dates=open_dates,
            health_payload=health_payload,
        )

        # Immutable shadow outputs only.
        self.paths.short_track_daily_dir.mkdir(parents=True, exist_ok=True)
        for strategy_id, payload in snapshots.items():
            path = self.paths.short_track_daily_dir / f"{strategy_id}_{trade_date}_candidate_snapshot.json"
            _compare_or_write_json(path, payload)

        _compare_or_write_parquet(
            self.paths.shadow_universe_dir / f"universe_{trade_date}.parquet",
            pit_snapshot["universe"],
            ["trade_date", "ts_code"],
        )
        _compare_or_write_parquet(
            self.paths.shadow_daily_basic_dir / f"daily_basic_{trade_date}.parquet",
            pit_snapshot["daily_basic"],
            ["trade_date", "ts_code"],
        )
        _compare_or_write_parquet(
            self.paths.common_pit_market_dir / f"universe_{trade_date}.parquet",
            pit_snapshot["universe"],
            ["trade_date", "ts_code"],
        )
        _compare_or_write_parquet(
            self.paths.common_pit_market_dir / f"daily_basic_{trade_date}.parquet",
            pit_snapshot["daily_basic"],
            ["trade_date", "ts_code"],
        )

        prices = self._load_prices_for_settlement()
        universe_history = self._load_universe_history()
        tracking_reports = {}
        portfolio_daily_paths = {}
        matured_signal_dates = expected_signal_dates(open_dates, as_of_date=trade_date)
        for strategy_id, payload in snapshots.items():
            ledger = self.update_ledger(strategy_id, payload, open_dates)
            settled = spe.settle_ledger(
                ledger,
                prices=prices,
                pit_universe=universe_history,
                open_trade_dates=open_dates,
                as_of_date=trade_date,
            )
            ledger_path = self.paths.short_track_ledger_dir / f"{strategy_id}_ledger.parquet"
            _write_parquet_atomic(ledger_path, settled)
            portfolio_daily = spe.build_staggered_portfolio_daily_evidence(
                settled,
                prices=prices,
                pit_universe=universe_history,
                open_trade_dates=open_dates,
                as_of_date=trade_date,
            )
            portfolio_daily_path = (
                self.paths.short_track_portfolio_daily_dir
                / f"{strategy_id}_portfolio_daily.parquet"
            )
            _write_parquet_atomic(portfolio_daily_path, portfolio_daily)
            report = self.build_candidate_tracking_report(
                strategy_id,
                payload["strategy_version"],
                settled,
                expected_signal_dates=matured_signal_dates,
                operational_ok=True,
                operational_evidence={
                    "snapshot_path": str(self.paths.short_track_daily_dir / f"{strategy_id}_{trade_date}_candidate_snapshot.json"),
                    "ledger_path": str(ledger_path),
                    "portfolio_daily_path": str(portfolio_daily_path),
                },
                as_of_date=trade_date,
                portfolio_daily=portfolio_daily,
            )
            report_path = spe.tracking_report_path(self.paths.short_track_daily_dir, strategy_id, trade_date)
            spe.write_json_atomic(report_path, report)
            tracking_reports[strategy_id] = str(report_path)
            portfolio_daily_paths[strategy_id] = str(portfolio_daily_path)

        return {
            "status": "ok",
            "trade_date": trade_date,
            "snapshot_paths": {
                strategy_id: str(self.paths.short_track_daily_dir / f"{strategy_id}_{trade_date}_candidate_snapshot.json")
                for strategy_id in snapshots
            },
            "tracking_reports": tracking_reports,
            "portfolio_daily_paths": portfolio_daily_paths,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily shadow runner for short-track candidate snapshots")
    parser.add_argument("--trade-date", default="")
    args = parser.parse_args(argv)
    workspace_dir = Path(os.environ.get("OPENCLAW_WORKSPACE_DIR", str(WORKSPACE))).resolve()
    trade_date = args.trade_date.strip() or resolve_trade_date(workspace_dir)
    client = pl.init_tushare()
    runner = ShortTrackShadowRunner(workspace_dir=workspace_dir, client=client)
    runner.run(trade_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
