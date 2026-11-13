"""Nightly digest for the comp_rank_ew6 forward shadow: v4.3 cross-audit, top-100 leaders, bottom-10 losers.

Read-only on production paths: reads forward_shadow ledger + scores, the published v4.3 picks in
stock-report (working tree, else the publish commit for that date) and stock_list.csv for names.
Writes only factor_factory/outputs/forward_shadow/{digest_<DATE>,latest_digest}.md.
The last stdout block is a compact plain-text summary (the 23:30 Hermes job delivers it to Discord).

  python forward_shadow_digest.py [--date YYYYMMDD]   # default: state.json last_signal_date
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_universe as bu  # noqa: E402
import forward_shadow as fs  # noqa: E402
import tier_monitor as tm  # noqa: E402
import research_panel_v2 as rp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
FS = ROOT / "data/forward_shadow"
OUT = ROOT / "outputs/forward_shadow"
SPEC_PATH = ROOT / "contracts/forward_shadow_v1.json"
STOCK_REPORT = WORKSPACE / "stock-report"
REC_REL = "data/latest/recommendation_state.json"
STOCK_LIST = WORKSPACE / "stock_data/01-root/stock_list.csv"
PANEL = ROOT / "data/research_panel_v2.parquet"
DROP = 0.30          # avoid_losers_70: bottom 30% by composite excluded, NaN kept (gauntlet_v2.rule_avoid_losers)
TOP_PCT = 0.20       # DOUBLE-PASS also granted to the top 20% by composite
SHORT = {"ivol_60": "ivol60↓", "turn_20": "turn20↓", "turn_abn_5_60": "tabn↓", "val_comp_indneu": "val↑",
         "vpd_10": "vpd10↑", "turn_cv_20": "tcv20↓"}
T_DOUBLE, T_KEEP, T_DANGER = "[DOUBLE-PASS / TOP-100]", "[PASS / KEEP-70]", "[DANGER / EXCLUDED-30]"


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def to_ts(code: str) -> str:
    if "." in code:
        return code
    return code + (".SH" if code[0] in "69" else ".BJ" if code[0] in "48" else ".SZ")


def v43_picks(d: str) -> tuple[list[dict], str]:
    """Published v4.3 final_recommendations for trade date d, and where they came from."""
    p = STOCK_REPORT / REC_REL
    if p.exists():
        rec = json.loads(p.read_text())
        if rec.get("trade_date") == d:
            return rec.get("final_recommendations", []), f"stock-report/{REC_REL} (working tree)"
    try:
        sha = subprocess.run(["git", "-C", str(STOCK_REPORT), "log", "-1", "--format=%h", "--fixed-strings",
                              f"--grep=publish(v2): {d}"], capture_output=True, text=True, check=True).stdout.strip()
        if sha:
            rec = json.loads(subprocess.run(["git", "-C", str(STOCK_REPORT), "show", f"{sha}:{REC_REL}"],
                                            capture_output=True, text=True, check=True).stdout)
            if rec.get("trade_date") == d:
                return rec.get("final_recommendations", []), f"stock-report@{sha}:{REC_REL}"
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return [], "not published for this date"


def name_map() -> pd.Series:
    names = pd.Series(dtype=str)
    if PANEL.exists():
        p = pd.read_parquet(PANEL, columns=["ts_code", "trade_date", "name"])
        names = p.sort_values("trade_date").drop_duplicates("ts_code", keep="last").set_index("ts_code")["name"]
    if STOCK_LIST.exists():
        sl = pd.read_csv(STOCK_LIST, dtype=str).set_index("ts_code")["name"]
        names = sl.combine_first(names)
    return names


def exclusion_reasons(d: str, sessions: list[str], codes: list[str]) -> tuple[dict[str, list[str]], set[str]]:
    """Why each code fails the greenfield universe on d: the same filters as rp.recompute_universe + board rule.
    Also returns the set passing every filter, so the caller can check it equals the official universe."""
    cfg = bu.load_runtime_config(rp.ROOT)
    daily_dates = [x for x in sessions[-90:] if (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists()]
    daily = bu.load_all_daily_panel([(x, rp.BACKTEST_CACHE / f"daily_{x}.parquet") for x in daily_dates])
    daily["trade_date"] = daily["trade_date"].dt.strftime("%Y%m%d")
    g = daily.groupby("ts_code", group_keys=False)
    lb = cfg.avg_amount_lookback_days
    daily["is_trading_day"] = ((daily["amount"] > 0) & (daily["vol"] > 0)).astype(int)
    daily["avg_amount_lookback"] = g["amount"].transform(lambda s: s.rolling(lb, min_periods=min(20, lb)).mean())
    daily["trading_ratio_lookback"] = g["is_trading_day"].transform(
        lambda s: s.rolling(lb, min_periods=min(20, lb)).mean())
    snap, _ = rp.load_pit_snapshot(d)
    day = daily[daily["trade_date"] == d].merge(snap, on=["trade_date", "ts_code"], how="inner").set_index("ts_code")
    reported = day["is_suspended"].fillna(True).astype(bool)
    checks = {
        "not_listed": day["list_status_at_date"].astype(str) != "L",
        f"listed_<{cfg.minimum_listing_days}d": pd.to_numeric(day["listing_days"], errors="coerce").fillna(-1)
        < cfg.minimum_listing_days,
        f"price_<{cfg.min_price:g}": day["close"].fillna(0) < cfg.min_price,
        "pit_universe_flag_0": ~(pd.to_numeric(day["universe_flag"], errors="coerce") > 0),
        "not_tradable": ~(pd.to_numeric(day["tradable"], errors="coerce") > 0),
        "ST": day["is_st"].astype(bool) | bu.mark_st_flags(day.reset_index(), cfg.st_name_patterns).to_numpy(),
        "suspended": reported,
        f"trading_ratio_<{cfg.min_daily_trading_days_ratio:g}": day["trading_ratio_lookback"].fillna(0)
        < cfg.min_daily_trading_days_ratio,
    }
    pre = pd.concat(checks, axis=1).any(axis=1)  # the liquidity quantile is taken before the board rule (as upstream)
    checks["STAR/BSE"] = pd.Series(~fs.board_ok(day.index.astype(str)), index=day.index)
    pre_ok = day[~pre]
    thr = float(pre_ok["avg_amount_lookback"].quantile(cfg.avg_amount_percentile_threshold))
    pct_amt = pre_ok["avg_amount_lookback"].rank(pct=True)
    passing = set(pre_ok.index[pre_ok["avg_amount_lookback"] >= thr]) - set(day.index[checks["STAR/BSE"]])
    out = {}
    for c in codes:
        if c not in day.index:
            out[c] = ["no_daily_or_pit_row"]
            continue
        why = [k for k, m in checks.items() if bool(m.get(c, False))]
        a = day.at[c, "avg_amount_lookback"]
        if not why and a < thr:
            why = [f"{lb}d_amount_bottom_{cfg.avg_amount_percentile_threshold:.0%} "
                   f"(avg {a / 1e5:.2f}亿 < {thr / 1e5:.2f}亿, pct {pct_amt.get(c, np.nan):.2f})"]  # amount is in 千元
        out[c] = why or ["passes all filters?"]
    return out, passing


def shadow_scores(d: str, sessions: list[str], uni_codes: list[str], comp: pd.Series, extras: dict[str, str],
                  spec_factors: list) -> tuple[pd.DataFrame, float]:
    """Score each out-of-universe code as if it alone joined the official universe (runner's own factor code).
    Returns per-code shadow composite, its percentile against the official composite distribution, factor ranks,
    and the max abs drift of recomputed official composites vs the stored scores (parity check)."""
    w = fs.window_mats(sessions)
    ind = fs.industry_map()
    base = pd.DataFrame({"ts_code": uni_codes})
    parity = float((fs.composite(fs.factor_values(w, d, base, ind)) - comp.reindex(uni_codes)).abs().max())
    official = comp.dropna().to_numpy()
    rows = []
    for c, industry in extras.items():
        ind_c = ind.copy()
        if c not in ind_c.index and industry:
            ind_c.loc[c] = industry
        fv = fs.factor_values(w, d, pd.DataFrame({"ts_code": uni_codes + [c]}), ind_c)
        R = pd.DataFrame({f: (fv[f] * sign).rank(pct=True) for f, sign in spec_factors})
        sc = fs.composite(fv).get(c, np.nan)
        rows.append({"code": c, "shadow_comp": sc,
                     "shadow_pct": float((official <= sc).mean()) if pd.notna(sc) else np.nan,
                     **{SHORT[f]: R.at[c, f] for f, _ in spec_factors}})
    return pd.DataFrame(rows).set_index("code") if rows else pd.DataFrame(), parity


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def build(d: str) -> tuple[str, str]:
    sp = FS / "scores" / f"scores_{d}.parquet"
    if not sp.exists():
        raise SystemExit(f"scores_{d}.parquet missing")
    s = pd.read_parquet(sp).set_index("ts_code")
    spec = json.loads(SPEC_PATH.read_text())
    books = [b for b in read_jsonl(FS / "books.jsonl") if b.get("official")]
    rec = next((b for b in books if b["signal_date"] == d), None)
    if rec is None:
        raise SystemExit(f"no official book record for {d}")
    held = next((b for b in reversed(books) if b["signal_date"] <= d and b.get("is_rebalance")), None)
    top100 = set(held["buffered_top100"]) if held else set()
    state = json.loads((FS / "state.json").read_text()) if (FS / "state.json").exists() else {}
    matured = state.get("matured_periods", 0) if state.get("last_signal_date") == d else "n/a"

    R = pd.DataFrame({f: (s[f] * sign).rank(pct=True) for f, sign in spec["factors"]})
    comp = s["comp_rank_ew6"]
    pct = comp.rank(pct=True)
    pos = comp.rank(ascending=False, method="min")
    excluded = set(s.index[pct.notna() & (pct <= DROP)])
    checks = []
    if rec.get("is_rebalance") and "avoid_losers_70" in rec:
        same = set(s.index) - excluded == set(rec["avoid_losers_70"])
        checks.append(f"avoid_losers_70 recomputed from scores {'matches' if same else 'DIFFERS FROM'} the book")
    names = name_map()

    def leaders(idx) -> pd.DataFrame:
        t = pd.DataFrame({"code": idx, "name": names.reindex(idx).fillna("-").values,
                          "industry": s.loc[idx, "industry"].values, "composite": comp.loc[idx].round(4).values,
                          "pos": pos.loc[idx].astype("Int64").values})
        for f in SHORT:
            t[SHORT[f]] = R.loc[idx, f].round(2).values
        return t

    # Held buffered_top100 members persist between rebalances and can leave today's universe (liquidity cut, suspension,
    # ...) -> no composite today. Rank only those still scored; list the rest instead of crashing (2026-10-16 incident).
    top100_live = comp.reindex(sorted(top100)).dropna()
    top100_gone = sorted(top100 - set(top100_live.index))
    if top100_gone:
        why_gone, _ = exclusion_reasons(d, [z for z in fs.official_sessions(d) if z <= d][-fs.WINDOW:], top100_gone)
        gone_txt = "; ".join(f"{c} {names.get(c, '-')}: {'/'.join(why_gone.get(c, ['?']))}" for c in top100_gone)
        checks.append(f"{len(top100_gone)} held buffered_top100 member(s) not scored today: {gone_txt}")
    lead = leaders(list(top100_live.sort_values(ascending=False).index[:20])) if len(top100_live) else pd.DataFrame()
    losers = leaders(list(comp.loc[list(excluded)].sort_values().index[:10]))

    picks, src = v43_picks(d)
    picks = [p for p in picks if str(p.get("strategy_version", "")).startswith("4.3")] or picks
    rows = []
    for p in sorted(picks, key=lambda x: x.get("rank", 99)):
        ts = to_ts(str(p["code"]))
        if ts not in s.index:
            tier, why, c, q, bucket = T_DANGER, "not in greenfield universe", None, None, "oou"
        elif pd.isna(comp[ts]):
            tier, why, c, q, bucket = T_KEEP, "composite NaN (kept by rule)", None, None, "keep"
        else:
            c, q = round(float(comp[ts]), 4), round(float(pct[ts]), 3)
            if ts in top100 or q > 1 - TOP_PCT:
                tier, bucket = T_DOUBLE, "double"
                why = "in buffered_top100" if ts in top100 else f"top {TOP_PCT:.0%} composite"
            elif ts in excluded:
                tier, why, bucket = T_DANGER, "bottom 30% composite", "b30"
            else:
                tier, why, bucket = T_KEEP, "inside avoid_losers_70", "keep"
        rows.append({"v43_rank": p.get("rank"), "code": ts, "name": p.get("name", "-"), "industry": p.get("industry", "-"),
                     "v43_score": p.get("score"), "composite": c, "pct": q, "tier": tier, "reason": why,
                     "bucket": bucket})
    x = pd.DataFrame(rows)

    # Out-of-universe picks: exact exclusion reason + shadow score (ledger and official universe untouched).
    cutoff = float(comp.loc[list(excluded)].max()) if excluded else float("nan")
    shadow = pd.DataFrame()
    oou = x.loc[x["bucket"] == "oou", ["code", "industry"]] if len(x) else pd.DataFrame()
    if len(oou):
        try:
            sessions = [z for z in fs.official_sessions(d) if z <= d][-fs.WINDOW:]
            reasons, passing = exclusion_reasons(d, sessions, oou["code"].tolist())
            checks.append(f"universe filters re-derived for exclusion reasons "
                          f"{'match' if passing == set(s.index) else 'DO NOT MATCH'} the official universe")
            sh, parity = shadow_scores(d, sessions, list(s.index), comp, dict(zip(oou["code"], oou["industry"])),
                                       spec["factors"])
            checks.append(f"shadow scorer reproduces official composites (max abs diff {parity:.1e})")
            sh["exclusion_reason"] = [", ".join(reasons[c]) for c in sh.index]
            sh["shadow_zone"] = np.where(sh["shadow_pct"].isna(), "n/a (<4 factors)",
                                         np.where(sh["shadow_pct"] <= DROP, "shadow bottom-30%", "shadow safe"))
            shadow = sh
            zone = x["code"].map(sh["shadow_zone"])
            x.loc[x["bucket"] == "oou", "reason"] = "out-of-universe · " + zone[x["bucket"] == "oou"].fillna("n/a")
            x.loc[zone == "shadow bottom-30%", "bucket"] = "oou_b30"
            x.loc[zone == "shadow safe", "bucket"] = "oou_safe"
        except Exception as e:  # the digest must still ship if shadow scoring breaks
            checks.append(f"shadow scoring failed: {type(e).__name__}: {e}")

    try:  # tier ledger for the DOUBLE-PASS monitor: append-only, one row per signal date
        checks.append(f"tier ledger: {tm.record_day(d, x, rec, 'forward_shadow_digest')}")
    except Exception as e:  # never block the digest
        checks.append(f"tier ledger failed: {type(e).__name__}: {e}")

    def group(*buckets: str) -> list[str]:
        if x.empty:
            return []
        return [f"{r.name}({r.code[:6]})" for r in x[x["bucket"].isin(buckets)].itertuples()]

    dbl, keep = group("double"), group("keep")
    dz_b30, dz_sb30, dz_safe, dz_unk = group("b30"), group("oou_b30"), group("oou_safe"), group("oou")
    held_note = (f"book from rebalance {held['signal_date']}" if held and held["signal_date"] != d
                 else "rebalanced today" if held else "no rebalance yet")

    summary = [f"📒 绿地前瞻影子 comp_rank_ew6 · 信号日 {d}",
               f"universe {rec['universe_size']} · 覆盖 {rec['composite_coverage']:.2%} · 已结算 {matured} 期 · {held_note}",
               f"v4.3 Top-{len(x)} 对照 (来源 {src.split(' ')[0]}):" if len(x) else "v4.3 当日未发布，跳过对照"]
    if len(x):
        n_dz = len(dz_b30) + len(dz_sb30) + len(dz_safe) + len(dz_unk)
        summary += [f"- DOUBLE-PASS {len(dbl)}: {', '.join(dbl) or '无'}",
                    f"- KEEP-70 {len(keep)}",
                    f"- DANGER {n_dz}:",
                    f"  · in-universe bottom-30% {len(dz_b30)}: {', '.join(dz_b30) or '无'}",
                    f"  · out-of-universe shadow bottom-30% {len(dz_sb30)}: {', '.join(dz_sb30) or '无'}",
                    f"  · out-of-universe shadow safe {len(dz_safe)}"]
        if dz_unk:
            summary += [f"  · out-of-universe unscored {len(dz_unk)}"]
    summary = "\n".join(summary)

    md = [f"# Forward shadow digest · {d}", "",
          f"- spec `{rec['spec_hash']}` · recorded `{rec['recorded_at']}` · entry deadline `{rec['entry_deadline']}`",
          f"- universe {rec['universe_size']} · composite coverage {rec['composite_coverage']:.2%} · "
          f"matured periods {matured} · rebalance today: {rec.get('is_rebalance')} ({held_note})",
          f"- excluded (bottom 30%): {len(excluded)} · buffered_top100 held: {len(top100)}",
          *[f"- check: {c}" for c in checks], "",
          "## Summary", "", "```", summary, "```", "",
          "## v4.3 Top-20 vs comp_rank_ew6", "", f"Source: `{src}`", "",
          md_table(x) if len(x) else "_No v4.3 picks published for this date._", "",
          f"DOUBLE-PASS = in buffered_top100 or top {TOP_PCT:.0%} composite; DANGER = bottom 30% composite "
          "or not in the greenfield universe.", "",
          "## Out-of-universe v4.3 picks: exclusion reason and shadow score", "",
          (md_table(shadow.reset_index().merge(x[["code", "v43_rank", "name"]], on="code")
                    [["v43_rank", "code", "name", "exclusion_reason", "shadow_comp", "shadow_pct", "shadow_zone",
                      *SHORT.values()]].sort_values("v43_rank").round(3))
           if len(shadow) else "_None._"), "",
          f"Shadow score: each pick scored with the runner's own factor code as if it alone joined the official "
          f"universe; shadow_pct is its percentile against the {int(comp.notna().sum())} official composites. "
          f"Bottom-30% cutoff composite {cutoff:.4f} (danger when shadow_pct <= {DROP:.2f}). Research only: the "
          "official universe and ledger are unchanged.", "",
          "## buffered_top100 leaders (top 20 by composite)", "",
          md_table(lead) if len(lead) else "_No buffered_top100 book yet._", "",
          "## Bottom 10 (excluded by avoid_losers_70)", "", md_table(losers), "",
          "Factor columns are direction-adjusted cross-sectional percentile ranks (1 = best).", ""]
    return "\n".join(md), summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    a = ap.parse_args(argv)
    d = a.date or json.loads((FS / "state.json").read_text())["last_signal_date"]
    md, summary = build(d)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"digest_{d}.md").write_text(md)
    (OUT / "latest_digest.md").write_text(md)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
