#!/usr/bin/env python3
"""Benchmark policy, price return resolution, and coverage degradation rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class UnknownPriceGap(Exception):
    pass


@dataclass(frozen=True)
class PriceReturnResult:
    return_value: float | None
    provenance: str


@dataclass(frozen=True)
class Coverage:
    overall: float
    sh: float
    sz: float
    bj: float


DEFAULT_OVERALL_MIN = 0.99
DEFAULT_BOARD_MIN = 0.95


@dataclass(frozen=True)
class CoverageEvaluation:
    status: str
    overall_ok: bool
    sh_ok: bool
    sz_ok: bool
    bj_ok: bool
    reason: str = ""


def evaluate_coverage(
    coverage: Coverage,
    overall_min: float = DEFAULT_OVERALL_MIN,
    board_min: float = DEFAULT_BOARD_MIN,
) -> CoverageEvaluation:
    sh_ok = coverage.sh >= board_min
    sz_ok = coverage.sz >= board_min
    bj_ok = coverage.bj >= board_min
    overall_ok = coverage.overall >= overall_min

    if overall_ok and sh_ok and sz_ok and bj_ok:
        return CoverageEvaluation(
            status="complete",
            overall_ok=True,
            sh_ok=True,
            sz_ok=True,
            bj_ok=True,
            reason="All boards and overall meet threshold",
        )
    elif overall_ok or (sh_ok and sz_ok):
        return CoverageEvaluation(
            status="degraded",
            overall_ok=overall_ok,
            sh_ok=sh_ok,
            sz_ok=sz_ok,
            bj_ok=bj_ok,
            reason="Board coverage below threshold, degraded to observation",
        )
    else:
        return CoverageEvaluation(
            status="failed",
            overall_ok=overall_ok,
            sh_ok=sh_ok,
            sz_ok=sz_ok,
            bj_ok=bj_ok,
            reason="Overall or main board coverage severely incomplete",
        )


def price_return(
    previous_close: float | None,
    current_close: float | None,
    suspension: bool = False,
) -> PriceReturnResult:
    if previous_close is None:
        return PriceReturnResult(return_value=None, provenance="missing_previous_close")

    if current_close is None:
        if suspension:
            return PriceReturnResult(return_value=0.0, provenance="suspension_carry")
        raise UnknownPriceGap(f"Unknown missing current price for previous_close={previous_close}")

    if previous_close <= 0:
        return PriceReturnResult(return_value=None, provenance="invalid_previous_close")

    ret = (current_close - previous_close) / previous_close
    return PriceReturnResult(return_value=ret, provenance="normal")


def build_tracking_report(
    operational_status: str,
    effectiveness_status: str,
    expected_trade_days: int,
    sample_trade_days: int,
    failed_gates: list[str],
    execution_authority: str = "observe_only_no_auto_order",
) -> dict[str, Any]:
    return {
        "operational_status": operational_status,
        "effectiveness_status": effectiveness_status,
        "expected_trade_days": expected_trade_days,
        "sample_trade_days": sample_trade_days,
        "failed_gates": failed_gates,
        "execution_authority": execution_authority,
        "flow_status": "degraded" if operational_status == "degraded_observation" else operational_status,
    }
