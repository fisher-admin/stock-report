#!/usr/bin/env python3
"""Daily, observation-only runner for event_quality_drift_v1."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE_DIR", str(Path(__file__).resolve().parents[3]))
).resolve()

import sys

STOCK_ANALYZER = WORKSPACE / "skills" / "stock-analyzer"
if str(STOCK_ANALYZER) not in sys.path:
    sys.path.insert(0, str(STOCK_ANALYZER))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import event_quality_drift_evaluator as evaluator  # noqa: E402
import event_quality_drift_v1 as strategy  # noqa: E402
import pit_market_snapshot as pms  # noqa: E402
from trading_calendar_store import load_open_trade_dates  # noqa: E402


class RunnerInputError(RuntimeError):
    pass


SKIPPABLE_EMPTY_EVENT_REASONS = frozenset(
    {
        "no target-date events have complete point-in-time growth history",
        "new events have no complete point-in-time quality/valuation/universe match",
        "new events have no finite scoring inputs",
    }
)


@dataclass(frozen=True)
class RunnerPaths:
    workspace: Path
    pit_events: Path
    calendar: Path
    backtest_cache: Path
    root: Path
    daily: Path
    materialized_quality: Path
    materialized_valuation: Path
    materialized_universe: Path
    common_pit_market: Path
    ledger: Path
    portfolio_daily: Path
    revision_manifest: Path


def build_paths(workspace: Path) -> RunnerPaths:
    working = workspace / "stock_data" / "03-working"
    root = working / "strategy_research" / strategy.STRATEGY_ID
    return RunnerPaths(
        workspace=workspace,
        pit_events=working / "fundamental_cache" / "pit" / "pit_yjyg.parquet",
        calendar=working / "health" / "trading_calendar.json",
        backtest_cache=working / "backtest_cache",
        root=root,
        daily=root / "daily",
        materialized_quality=root / "materialized" / "quality",
        materialized_valuation=root / "materialized" / "valuation",
        materialized_universe=root / "materialized" / "pit_universe",
        common_pit_market=working / "fundamental_cache" / "pit_market",
        ledger=root / "ledger" / f"{strategy.STRATEGY_ID}_ledger.parquet",
        portfolio_daily=root / "ledger" / f"{strategy.STRATEGY_ID}_portfolio_daily.parquet",
        revision_manifest=root / "revision_chain_manifest.json",
    )


def _date8(value: Any, *, field: str) -> str:
    text = str(value or "").replace("-", "")[:8]
    if len(text) != 8 or not text.isdigit():
        raise RunnerInputError(f"{field} must be YYYYMMDD")
    return text


def _atomic_json(path: Path, payload: dict[str, Any], *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    if immutable and path.exists():
        existing = json.dumps(
            json.loads(path.read_text(encoding="utf-8")),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        if existing != canonical:
            raise RunnerInputError(f"immutable event snapshot mismatch: {path}")
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_parquet(path: Path, frame: pd.DataFrame, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = frame.reset_index(drop=True)
    if immutable and path.exists():
        existing = pd.read_parquet(path).reset_index(drop=True)
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
            raise RunnerInputError(
                f"immutable event materialization mismatch: {path}"
            ) from exc
        else:
            return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".parquet", dir=str(path.parent))
    os.close(fd)
    tmp = Path(temporary)
    try:
        normalized.to_parquet(tmp, index=False)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _ts_code(symbol: Any) -> str:
    digits = "".join(character for character in str(symbol) if character.isdigit())[:6]
    if len(digits) != 6:
        raise RunnerInputError(f"invalid event symbol: {symbol!r}")
    suffix = "SH" if digits.startswith("6") else ("BJ" if digits.startswith(("4", "8", "9")) else "SZ")
    return f"{digits}.{suffix}"


def replay_signal_dates(
    *,
    start_date: str,
    end_date: str,
    open_trade_dates: list[str],
    pit_events: pd.DataFrame,
) -> list[str]:
    """Return every open day plus any non-open day with a new announcement."""
    start = _date8(start_date, field="replay_start")
    end = _date8(end_date, field="replay_end")
    if start > end:
        raise RunnerInputError("replay_start must not be after replay_end")
    if "announce_date" not in pit_events.columns:
        raise RunnerInputError("announcement store has no announce_date for replay")
    open_days = {
        _date8(value, field="open_trade_date")
        for value in open_trade_dates
        if start <= str(value).replace("-", "")[:8] <= end
    }
    announcement_days = {
        _date8(value, field="announce_date")
        for value in pit_events["announce_date"].dropna().tolist()
        if start <= str(value).replace("-", "")[:8] <= end
    }
    return sorted(open_days | announcement_days)


class EventQualityDriftRunner:
    def __init__(self, *, workspace_dir: Path | None = None, client: Any):
        self.workspace = Path(workspace_dir or WORKSPACE).resolve()
        self.client = client
        self.paths = build_paths(self.workspace)

    def open_trade_dates(self) -> list[str]:
        dates = load_open_trade_dates(self.paths.calendar)
        if not dates:
            raise RunnerInputError(f"persisted exchange calendar is missing or invalid: {self.paths.calendar}")
        return dates

    def load_pit_events(self) -> pd.DataFrame:
        if not self.paths.pit_events.exists():
            raise RunnerInputError(f"missing point-in-time announcement store: {self.paths.pit_events}")
        frame = pd.read_parquet(self.paths.pit_events)
        required = {
            "symbol",
            "field",
            "period",
            "value",
            "announce_date",
            "available_at",
            "revision_seq",
            "source",
        }
        missing = sorted(required - set(frame.columns))
        if missing:
            raise RunnerInputError(f"announcement store missing columns: {missing}")
        if frame["source"].astype(str).str.contains("proxy", case=False, na=False).any():
            raise RunnerInputError("announcement store contains proxy provenance")
        frame = frame.copy()
        frame["announce_date"] = frame["announce_date"].astype(str).str.replace("-", "", regex=False)
        frame["used_proxy"] = False
        frame["completeness"] = "complete"
        return frame

    def revision_chain_complete(self, signal_date: str) -> bool:
        if not self.paths.revision_manifest.exists():
            return False
        try:
            payload = json.loads(self.paths.revision_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or not bool(payload.get("revision_chain_complete")):
            return False
        start = str(payload.get("coverage_start") or "")
        through = str(payload.get("complete_through") or "")
        missing_dates = {str(value) for value in payload.get("missing_dates") or []}
        return bool(start <= signal_date <= through and signal_date not in missing_dates)

    def fetch_quality_history(self, codes: list[str], signal_date: str) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for code in sorted(set(codes)):
            frame = self.client.fina_indicator(
                ts_code=code,
                start_date="20180101",
                end_date=signal_date,
                fields=None,
            )
            if frame is None or len(frame) == 0:
                raise RunnerInputError(f"official fina_indicator history is missing for {code}")
            clone = frame.copy()
            required = {
                "ts_code",
                "ann_date",
                "end_date",
                "roe",
                "grossprofit_margin",
                "debt_to_assets",
            }
            missing = sorted(required - set(clone.columns))
            if missing:
                raise RunnerInputError(f"fina_indicator missing columns for {code}: {missing}")
            clone["ann_date"] = clone["ann_date"].astype(str).str.replace("-", "", regex=False)
            clone = clone[clone["ann_date"] <= signal_date].copy()
            if clone.empty:
                raise RunnerInputError(f"no point-in-time quality statement is visible for {code}")
            if "source_provider" in clone.columns and clone["source_provider"].astype(str).str.contains(
                "proxy", case=False, na=False
            ).any():
                raise RunnerInputError(f"fina_indicator used proxy provenance for {code}")
            clone["used_proxy"] = False
            clone["completeness"] = "complete"
            clone["source"] = "tushare_fina_indicator"
            frames.append(clone)
        return (
            pd.concat(frames, ignore_index=True)
            .sort_values(["ts_code", "ann_date", "end_date"])
            .reset_index(drop=True)
        )

    def active_positions_for_entry(
        self,
        ledger: pd.DataFrame,
        *,
        entry_trade_date: str,
        open_trade_dates: list[str],
    ) -> list[dict[str, Any]]:
        if ledger.empty:
            return []
        target = _date8(entry_trade_date, field="entry_trade_date")
        selected = ledger[ledger["is_selected"].fillna(False).astype(bool)].copy()
        active: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, row in selected.sort_values(["signal_date", "quant_rank", "ts_code"]).iterrows():
            signal_date = _date8(row.get("signal_date"), field="signal_date")
            calendar_target = evaluator._calendar_targets(signal_date, open_trade_dates)
            if calendar_target is None:
                continue
            entry, exits = calendar_target
            exit_20 = exits.get(20)
            if exit_20 is None or not (entry <= target <= exit_20):
                continue
            code = str(row.get("ts_code") or "")
            if code in seen:
                raise RunnerInputError(f"active event book contains duplicate security: {code}")
            seen.add(code)
            active.append(
                {
                    "ts_code": code,
                    "industry": str(row.get("industry") or "UNKNOWN"),
                    "weight": float(row.get("weight") or 0.0),
                }
            )
        return active

    def _load_ledger(self) -> pd.DataFrame:
        if not self.paths.ledger.exists():
            return pd.DataFrame(columns=evaluator.LEDGER_COLUMNS)
        return pd.read_parquet(self.paths.ledger)

    def _load_prices(self) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for path in sorted(self.paths.backtest_cache.glob("stk_factor_*.parquet")):
            try:
                frame = pd.read_parquet(
                    path,
                    columns=["trade_date", "ts_code", "open_qfq", "close_qfq"],
                )
            except (OSError, ValueError, KeyError):
                continue
            frames.append(frame)
        if not frames:
            return pd.DataFrame(columns=["trade_date", "ts_code", "open_qfq", "close_qfq"])
        return pd.concat(frames, ignore_index=True).drop_duplicates(
            ["trade_date", "ts_code"], keep="last"
        )

    def _load_universe_history(self) -> pd.DataFrame:
        frames = [pd.read_parquet(path) for path in sorted(self.paths.materialized_universe.glob("universe_*.parquet"))]
        if not frames:
            return pd.DataFrame(columns=["trade_date", "ts_code", "universe_flag", "tradable"])
        return pd.concat(frames, ignore_index=True)

    def _no_eligible_event_result(
        self,
        *,
        signal_date: str,
        valuation_date: str,
        valuation_path: Path,
        universe_path: Path,
        existing: pd.DataFrame,
        open_dates: list[str],
        new_event_count: int,
        rejection_reason: str,
        quality_path: Path | None = None,
    ) -> dict[str, Any]:
        """Record a healthy no-candidate day without hiding data-contract failures."""
        portfolio_daily: pd.DataFrame | None = None
        settled = existing
        result_paths: dict[str, str] = {}
        if not existing.empty:
            prices = self._load_prices()
            universe_history = self._load_universe_history()
            settled = evaluator.settle_ledger(
                existing,
                prices=prices,
                pit_universe=universe_history,
                open_trade_dates=open_dates,
                as_of_date=signal_date,
            )
            _atomic_parquet(self.paths.ledger, settled)
            portfolio_daily = evaluator.build_persistent_portfolio_daily_evidence(
                settled,
                prices=prices,
                pit_universe=universe_history,
                open_trade_dates=open_dates,
                as_of_date=signal_date,
            )
            _atomic_parquet(self.paths.portfolio_daily, portfolio_daily)
            result_paths = {
                "ledger_path": str(self.paths.ledger),
                "portfolio_daily_path": str(self.paths.portfolio_daily),
            }
        verdict = evaluator.evaluate_event_quality_drift_promotion(
            settled,
            portfolio_daily=portfolio_daily,
        )
        gates = dict(verdict["gates"])
        gates["no_eligible_announcement_events"] = False
        failed_gates = list(
            dict.fromkeys(
                [*verdict["failed_gates"], "no_eligible_announcement_events"]
            )
        )
        report = {
            **verdict,
            "strategy_id": strategy.STRATEGY_ID,
            "strategy_version": strategy.STRATEGY_VERSION,
            "artifact_kind": "candidate_tracking_report",
            "operational_status": "healthy_no_eligible_candidates",
            "effectiveness_status": "not_applicable_no_eligible_events",
            "signal_date": signal_date,
            "valuation_date": valuation_date,
            "new_announcement_event_count": int(new_event_count),
            "eligible_event_count": 0,
            "rejection_reason": rejection_reason,
            "revision_chain_complete": self.revision_chain_complete(signal_date),
            "evidence_scope": "auxiliary_only",
            "gates": gates,
            "failed_gates": failed_gates,
            "all_gates_pass": False,
            "decision": "observe_only",
            "execution_authority": "observe_only_no_auto_order",
            "valuation_path": str(valuation_path),
            "pit_universe_path": str(universe_path),
            **result_paths,
        }
        if quality_path is not None:
            report["quality_path"] = str(quality_path)
        report_path = self.paths.daily / f"{strategy.STRATEGY_ID}_{signal_date}_candidate_tracking.json"
        _atomic_json(report_path, report)
        return {
            "status": "no_eligible_announcement_events",
            "signal_date": signal_date,
            "valuation_date": valuation_date,
            "report_path": str(report_path),
            "pit_universe_path": str(universe_path),
            "rejection_reason": rejection_reason,
            "execution_authority": "observe_only_no_auto_order",
            **result_paths,
        }

    def run_replay(self, start_date: str, end_date: str) -> dict[str, Any]:
        start = _date8(start_date, field="replay_start")
        end = _date8(end_date, field="replay_end")
        events = self.load_pit_events()
        dates = replay_signal_dates(
            start_date=start,
            end_date=end,
            open_trade_dates=self.open_trade_dates(),
            pit_events=events,
        )
        if not dates:
            raise RunnerInputError("replay range contains no open day or announcement event")
        status_counts: dict[str, int] = {}
        last_result: dict[str, Any] | None = None
        for signal_date in dates:
            last_result = self.run_signal_date(
                signal_date,
                allow_stale_research=True,
            )
            status = str(last_result.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        result = {
            "status": "ok",
            "replay_start": start,
            "replay_end": end,
            "processed_signal_dates": len(dates),
            "first_processed_signal_date": dates[0],
            "last_processed_signal_date": dates[-1],
            "status_counts": status_counts,
            "last_result": last_result,
            "execution_authority": "observe_only_no_auto_order",
        }
        manifest = self.paths.root / "replay" / f"replay_{start}_{end}.json"
        _atomic_json(manifest, result)
        result["manifest_path"] = str(manifest)
        return result

    def run_signal_date(self, signal_date: str, *, allow_stale_research: bool = False) -> dict[str, Any]:
        signal_date = _date8(signal_date, field="signal_date")
        open_dates = self.open_trade_dates()
        following = [date for date in open_dates if date > signal_date]
        if not following:
            raise RunnerInputError("persisted exchange calendar has no next open after signal date")
        entry_date = following[0]
        planned_entry = datetime.fromisoformat(
            f"{entry_date[:4]}-{entry_date[4:6]}-{entry_date[6:]}T09:30:00+08:00"
        )
        now = datetime.now(timezone(timedelta(hours=8)))
        events = self.load_pit_events()
        current = events[events["announce_date"] == signal_date].copy()
        if not current.empty and not allow_stale_research and now >= planned_entry:
            raise RunnerInputError(
                "signal is stale: next-open entry has already passed; live backdating is forbidden"
            )
        valuation_dates = [date for date in open_dates if date <= signal_date]
        if not valuation_dates:
            raise RunnerInputError("exchange calendar has no valuation date at or before announcement")
        valuation_date = valuation_dates[-1]
        market = pms.collect_pit_market_snapshot(self.client, valuation_date)
        if str(market.get("trade_date")) != valuation_date:
            raise RunnerInputError("PIT market snapshot date does not match the requested valuation date")
        valuation = market["daily_basic"].copy()
        universe = market["universe"].copy()
        valuation_path = self.paths.materialized_valuation / f"daily_basic_{valuation_date}.parquet"
        universe_path = self.paths.materialized_universe / f"universe_{valuation_date}.parquet"
        _atomic_parquet(valuation_path, valuation, immutable=True)
        _atomic_parquet(universe_path, universe, immutable=True)
        _atomic_parquet(
            self.paths.common_pit_market / f"daily_basic_{valuation_date}.parquet",
            valuation,
            immutable=True,
        )
        _atomic_parquet(
            self.paths.common_pit_market / f"universe_{valuation_date}.parquet",
            universe,
            immutable=True,
        )
        existing = self._load_ledger()
        if current.empty:
            result = {
                "status": "no_new_announcement_events",
                "signal_date": signal_date,
                "valuation_date": valuation_date,
                "pit_universe_path": str(universe_path),
                "execution_authority": "observe_only_no_auto_order",
            }
            if existing.empty:
                return result
            settled = evaluator.settle_ledger(
                existing,
                prices=self._load_prices(),
                pit_universe=self._load_universe_history(),
                open_trade_dates=open_dates,
                as_of_date=signal_date,
            )
            _atomic_parquet(self.paths.ledger, settled)
            portfolio_daily = evaluator.build_persistent_portfolio_daily_evidence(
                settled,
                prices=self._load_prices(),
                pit_universe=self._load_universe_history(),
                open_trade_dates=open_dates,
                as_of_date=signal_date,
            )
            _atomic_parquet(self.paths.portfolio_daily, portfolio_daily)
            verdict = evaluator.evaluate_event_quality_drift_promotion(
                settled,
                portfolio_daily=portfolio_daily,
            )
            report_path = self.paths.daily / f"{strategy.STRATEGY_ID}_{signal_date}_candidate_tracking.json"
            _atomic_json(
                report_path,
                {
                    **verdict,
                    "artifact_kind": "candidate_tracking_report",
                    "operational_status": "ok",
                    "signal_date": signal_date,
                    "new_announcement_event_count": 0,
                    "ledger_path": str(self.paths.ledger),
                    "portfolio_daily_path": str(self.paths.portfolio_daily),
                },
            )
            result.update(
                {
                    "ledger_path": str(self.paths.ledger),
                    "portfolio_daily_path": str(self.paths.portfolio_daily),
                    "report_path": str(report_path),
                }
            )
            return result
        target_codes = sorted({_ts_code(symbol) for symbol in current["symbol"].tolist()})
        tradable_codes = set(
            universe.loc[
                (pd.to_numeric(universe["universe_flag"], errors="coerce") > 0)
                & (pd.to_numeric(universe["tradable"], errors="coerce") > 0),
                "ts_code",
            ].astype(str)
        )
        valuation_codes = set(valuation["ts_code"].astype(str))
        eligible_target_codes = sorted(set(target_codes) & tradable_codes & valuation_codes)
        if not eligible_target_codes:
            return self._no_eligible_event_result(
                signal_date=signal_date,
                valuation_date=valuation_date,
                valuation_path=valuation_path,
                universe_path=universe_path,
                existing=existing,
                open_dates=open_dates,
                new_event_count=len(target_codes),
                rejection_reason="no announced security is present in the point-in-time tradable universe and valuation snapshot",
            )
        quality = self.fetch_quality_history(eligible_target_codes, signal_date)
        prior_signals = existing[
            existing.get("signal_date", pd.Series(dtype=str)).astype(str) != signal_date
        ].copy() if not existing.empty else existing
        active_positions = self.active_positions_for_entry(
            prior_signals,
            entry_trade_date=entry_date,
            open_trade_dates=open_dates,
        )
        quality_path = self.paths.materialized_quality / f"quality_{signal_date}.parquet"
        try:
            snapshot = strategy.build_event_quality_drift_snapshot(
                pit_events=events,
                quality_history=quality,
                valuation_history=valuation,
                pit_universe=universe,
                announce_date=signal_date,
                exchange_trade_dates=open_dates,
                revision_chain_complete=self.revision_chain_complete(signal_date),
                active_positions=active_positions,
            )
        except strategy.EventDataIntegrityError as exc:
            if str(exc) not in SKIPPABLE_EMPTY_EVENT_REASONS:
                raise
            _atomic_parquet(quality_path, quality, immutable=True)
            return self._no_eligible_event_result(
                signal_date=signal_date,
                valuation_date=valuation_date,
                valuation_path=valuation_path,
                universe_path=universe_path,
                existing=existing,
                open_dates=open_dates,
                new_event_count=len(target_codes),
                rejection_reason=str(exc),
                quality_path=quality_path,
            )
        snapshot["live_entry_eligible"] = not allow_stale_research
        snapshot["historical_replay"] = bool(allow_stale_research)
        daily_path = self.paths.daily / f"{strategy.STRATEGY_ID}_{signal_date}_candidate_snapshot.json"
        _atomic_json(daily_path, snapshot, immutable=True)
        _atomic_parquet(quality_path, quality, immutable=True)
        updated = evaluator.pending_rows_from_snapshot(
            snapshot,
            existing=existing,
            open_trade_dates=open_dates,
        )
        settled = evaluator.settle_ledger(
            updated,
            prices=self._load_prices(),
            pit_universe=self._load_universe_history(),
            open_trade_dates=open_dates,
            as_of_date=signal_date,
        )
        _atomic_parquet(self.paths.ledger, settled)
        portfolio_daily = evaluator.build_persistent_portfolio_daily_evidence(
            settled,
            prices=self._load_prices(),
            pit_universe=self._load_universe_history(),
            open_trade_dates=open_dates,
            as_of_date=signal_date,
        )
        _atomic_parquet(self.paths.portfolio_daily, portfolio_daily)
        verdict = evaluator.evaluate_event_quality_drift_promotion(
            settled,
            portfolio_daily=portfolio_daily,
        )
        report = {
            **verdict,
            "artifact_kind": "candidate_tracking_report",
            "operational_status": "ok",
            "signal_date": signal_date,
            "snapshot_path": str(daily_path),
            "ledger_path": str(self.paths.ledger),
            "portfolio_daily_path": str(self.paths.portfolio_daily),
            "revision_chain_complete": bool(snapshot["revision_chain_complete"]),
            "evidence_scope": snapshot["evidence_scope"],
        }
        report_path = self.paths.daily / f"{strategy.STRATEGY_ID}_{signal_date}_candidate_tracking.json"
        _atomic_json(report_path, report)
        return {
            "status": "ok",
            "signal_date": signal_date,
            "snapshot_path": str(daily_path),
            "ledger_path": str(self.paths.ledger),
            "portfolio_daily_path": str(self.paths.portfolio_daily),
            "report_path": str(report_path),
            "execution_authority": "observe_only_no_auto_order",
        }


def default_signal_date() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    return (now.date() - timedelta(days=1)).strftime("%Y%m%d")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Premarket event-quality research runner")
    parser.add_argument("--signal-date", default="")
    parser.add_argument("--allow-stale-research", action="store_true")
    parser.add_argument("--replay-start", default="")
    parser.add_argument("--replay-end", default="")
    args = parser.parse_args(argv)
    from pipeline import init_tushare

    runner = EventQualityDriftRunner(workspace_dir=WORKSPACE, client=init_tushare())
    replay_start = args.replay_start.strip()
    replay_end = args.replay_end.strip()
    if bool(replay_start) != bool(replay_end):
        parser.error("--replay-start and --replay-end must be supplied together")
    if replay_start:
        if args.signal_date.strip():
            parser.error("--signal-date cannot be combined with a replay range")
        result = runner.run_replay(replay_start, replay_end)
    else:
        result = runner.run_signal_date(
            args.signal_date.strip() or default_signal_date(),
            allow_stale_research=bool(args.allow_stale_research),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
