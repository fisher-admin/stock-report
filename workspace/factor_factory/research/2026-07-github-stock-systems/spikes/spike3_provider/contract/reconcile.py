"""Cross-source reconciliation.

- Daily bars: unadjusted OHLC overlap |relative diff| <= 1e-4 (nautilus.md §1.2
  R1 tolerance). Breach => hard alert + ReconcileBreach (fail-closed).
- Names/industry: dual-source mismatch list -> manifest (allowed to exist,
  must be explicit).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .core import ReconcileBreach
from .models import DailyBarV1, SecurityMetaV1

PRICE_TOL = 1e-4
PRICE_FIELDS = ("open", "high", "low", "close")


@dataclass
class DailyReconcileReport:
    source_a: str
    source_b: str
    n_overlap_bars: int = 0
    n_compared_values: int = 0
    max_rel_diff: float = 0.0
    breaches: List[dict] = field(default_factory=list)
    volume_note: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.n_overlap_bars > 0 and not self.breaches

    def to_dict(self) -> dict:
        return {
            "source_a": self.source_a, "source_b": self.source_b,
            "n_overlap_bars": self.n_overlap_bars,
            "n_compared_values": self.n_compared_values,
            "max_rel_diff": self.max_rel_diff,
            "tolerance": PRICE_TOL,
            "passed": self.passed,
            "breaches": self.breaches[:50],
            "volume_note": self.volume_note,
        }


def _rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-12)


def reconcile_daily(
    rows_a: Dict[str, List[DailyBarV1]],
    rows_b: Dict[str, List[DailyBarV1]],
    strict: bool = True,
) -> DailyReconcileReport:
    a_name = next((r[0].source for r in rows_a.values() if r), "a")
    b_name = next((r[0].source for r in rows_b.values() if r), "b")
    rep = DailyReconcileReport(source_a=a_name, source_b=b_name)

    idx_b: Dict[Tuple[str, str], DailyBarV1] = {}
    for sym, rows in rows_b.items():
        for r in rows:
            idx_b[(sym, r.trade_date.isoformat())] = r

    vol_diffs: List[float] = []
    for sym, rows in rows_a.items():
        for ra in rows:
            rb = idx_b.get((sym, ra.trade_date.isoformat()))
            if rb is None:
                continue
            rep.n_overlap_bars += 1
            for f in PRICE_FIELDS:
                va, vb = getattr(ra, f), getattr(rb, f)
                d = _rel(va, vb)
                rep.n_compared_values += 1
                rep.max_rel_diff = max(rep.max_rel_diff, d)
                if d > PRICE_TOL:
                    rep.breaches.append({
                        "symbol": sym, "trade_date": ra.trade_date.isoformat(),
                        "field": f, a_name: va, b_name: vb, "rel_diff": d,
                    })
            if ra.volume > 0 and rb.volume > 0:
                vol_diffs.append(_rel(ra.volume, rb.volume))

    if vol_diffs:
        vol_diffs.sort()
        rep.volume_note = (
            f"volume rel-diff median={vol_diffs[len(vol_diffs)//2]:.2e} "
            f"max={vol_diffs[-1]:.2e} (informational, not gated)")

    if strict and rep.breaches:
        raise ReconcileBreach(
            f"HARD ALERT: {len(rep.breaches)} price value(s) exceed |rel diff|<={PRICE_TOL} "
            f"between {a_name} and {b_name}; first={rep.breaches[0]}")
    return rep


def reconcile_names(
    rows_a: List[SecurityMetaV1],
    rows_b: List[SecurityMetaV1],
) -> dict:
    """Name mismatch list is allowed to exist but must be explicit in manifest."""
    map_a = {r.symbol: r for r in rows_a}
    map_b = {r.symbol: r for r in rows_b}
    both = sorted(set(map_a) & set(map_b))
    only_a = sorted(set(map_a) - set(map_b))
    only_b = sorted(set(map_b) - set(map_a))
    name_mismatches = []
    for s in both:
        if map_a[s].name != map_b[s].name:
            name_mismatches.append({"symbol": s,
                                    map_a[s].source: map_a[s].name,
                                    map_b[s].source: map_b[s].name})
    industry_cov_a = sum(1 for r in rows_a if r.industry)
    return {
        "n_common": len(both),
        "n_only_" + (rows_a[0].source if rows_a else "a"): len(only_a),
        "n_only_" + (rows_b[0].source if rows_b else "b"): len(only_b),
        "only_a_sample": only_a[:20],
        "only_b_sample": only_b[:20],
        "n_name_mismatches": len(name_mismatches),
        "name_mismatches": name_mismatches[:100],
        "industry_coverage_tushare": industry_cov_a,
        "industry_note": "industry single-sourced from tushare stock_basic; akshare side has no industry column",
    }
