#!/usr/bin/env python3
"""持仓级风格 / 行业归因 + 因子冗余矩阵（前瞻）。

回答"这组票赚的是不是真 alpha"：对每个前瞻信号日，在 universe 全截面上做一次回归

    fwd_i = a + Σ_k β_k · style_ik + Σ_j γ_j · industry_ij + e_i        （fwd = 次日开盘起 h 日复权收益）

某组持仓 g（双料王牌 / v4.3 全体 / buffered_top100 / avoid_losers_70）相对 universe 的超额恒等于

    mean_g(fwd) − mean_U(fwd) = Σ_k (x̄_gk − x̄_Uk)·β_k + Σ_j (d̄_gj − d̄_Uj)·γ_j + (ē_g − ē_U)
                               = 风格贡献 + 行业贡献 + 选股 alpha（残差；ē_U = 0）

风格 = 市值(ln circ_mv)、价值(val_comp_indneu)、动量(20 日)、5 日涨幅、换手(turn_20)、波动(ivol_60)，截面去极值后 z 分。
冗余：6 个分项（方向调整后）的 Spearman 截面相关矩阵（跨前瞻信号日平均）、有效因子数 N_eff = (Σλ)²/Σλ²。

只读，只前瞻：读 forward_shadow/scores、tier_ledger、books；写 outputs/forward_shadow/style_attribution.json。

  python style_attribution.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[1]
FS = ROOT / "data/forward_shadow"
OUT = ROOT / "outputs/forward_shadow"
RESULT = OUT / "style_attribution.json"
HORIZONS = (1, 5)
STYLES = [("size", "市值"), ("value", "价值"), ("mom20", "动量20日"), ("ret5", "5日涨幅"), ("turn", "换手"), ("vol", "波动")]
COMPONENTS = [("ivol_60", -1), ("turn_20", -1), ("turn_abn_5_60", -1), ("val_comp_indneu", 1), ("vpd_10", 1),
              ("turn_cv_20", -1)]
GROUP_LABELS = {"DOUBLE": "双料王牌", "V43_ALL": "v4.3 Top-20 全体", "TOP100": "buffered_top100", "AVOID70": "avoid_losers_70"}
WINSOR = (0.01, 0.99)
MIN_UNIVERSE = 300


def zscore(s: pd.Series) -> pd.Series:
    lo, hi = s.quantile(WINSOR[0]), s.quantile(WINSOR[1])
    c = s.clip(lo, hi)
    sd = c.std(ddof=0)
    return (c - c.mean()) / sd if sd > 0 else c * 0.0


def decompose(fwd: pd.Series, styles: pd.DataFrame, industry: pd.Series, group: list[str]) -> dict | None:
    """Exact decomposition of mean_g(fwd) − mean_U(fwd). styles: z-scored, NaN→0 applied here."""
    df = styles.copy().fillna(0.0)
    df["fwd"] = fwd
    ind = industry.reindex(df.index).fillna("UNKNOWN").astype(str)
    df = df[df["fwd"].notna()]
    if len(df) < MIN_UNIVERSE:
        return None
    ind = ind.reindex(df.index)
    dummies = pd.get_dummies(ind, drop_first=True, dtype=float)
    X = pd.concat([pd.Series(1.0, index=df.index, name="const"), df[[k for k, _ in STYLES]], dummies], axis=1)
    y = df["fwd"].to_numpy()
    beta, *_ = np.linalg.lstsq(X.to_numpy(), y, rcond=None)
    b = pd.Series(beta, index=X.columns)
    resid = pd.Series(y - X.to_numpy() @ beta, index=df.index)
    g = [c for c in group if c in df.index]
    if not g:
        return None
    dx = X.loc[g].mean() - X.mean()
    style = {k: float(dx[k] * b[k]) for k, _ in STYLES}
    industry_part = float(sum(dx[c] * b[c] for c in dummies.columns))
    alpha = float(resid.loc[g].mean() - resid.mean())
    total = float(df.loc[g, "fwd"].mean() - df["fwd"].mean())
    shares = ind.loc[g].value_counts(normalize=True)
    return {"n": len(g), "excess": total, "style": style, "industry": industry_part, "alpha": alpha,
            "check": total - (sum(style.values()) + industry_part + alpha),
            "exposure": {k: float(X.loc[g, k].mean()) for k, _ in STYLES},
            "top_industry": str(shares.index[0]), "top_industry_share": float(shares.iloc[0]),
            "betas": {k: float(b[k]) for k, _ in STYLES}}


def redundancy(frames: list[pd.DataFrame]) -> dict:
    """Mean cross-sectional Spearman matrix of the direction-adjusted components and N_eff."""
    mats = []
    for sc in frames:
        cols = {f: sign * sc[f] for f, sign in COMPONENTS if f in sc.columns}
        if len(cols) == len(COMPONENTS):
            mats.append(pd.DataFrame(cols).rank().corr().to_numpy())
    if not mats:
        return {}
    m = np.mean(mats, axis=0)
    lam = np.clip(np.linalg.eigvalsh(m), 0, None)
    names = [f for f, _ in COMPONENTS]
    pairs = [{"a": names[i], "b": names[j], "rho": round(float(m[i, j]), 3)}
             for i in range(len(names)) for j in range(i + 1, len(names)) if abs(m[i, j]) >= 0.5]
    return {"days": len(mats), "names": names, "matrix": np.round(m, 3).tolist(),
            "n_eff": round(float(lam.sum() ** 2 / (lam ** 2).sum()), 2), "n_factors": len(names),
            "high_pairs": sorted(pairs, key=lambda p: -abs(p["rho"]))}


def compute(now: datetime | None = None) -> dict:
    import forward_shadow as fs
    import research_panel_v2 as rp
    now = now or datetime.now().astimezone()
    files = sorted((FS / "scores").glob("scores_*.parquet"))
    dates = [p.stem.split("_")[-1] for p in files]
    doc = {"contract_version": "style_attribution_v1", "generated_at": now.isoformat(), "styles": dict(STYLES),
           "groups": GROUP_LABELS, "signal_days": len(dates)}
    if not dates:
        return {**doc, "status": "collecting", "message": "尚无前瞻分数文件"}
    cal = fs.official_sessions("99999999")
    avail = [x for x in cal if (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists()]
    first_i = cal.index(dates[0])
    sess = [x for x in cal[max(0, first_i - 25): first_i + 1] + [x for x in cal if dates[0] < x <= avail[-1]]]
    w = fs.window_mats(sess)
    ac, ao = w["adj_close"].ffill(), w["adj_open"]
    idx = {d: i for i, d in enumerate(sess)}
    tier = {r["signal_date"]: r for r in _jl(FS / "tier_ledger.jsonl") if r.get("official")}
    books = {b["signal_date"]: b for b in _jl(FS / "books.jsonl") if b.get("official") and b.get("is_rebalance")}
    acc: dict = {h: {g: [] for g in GROUP_LABELS} for h in HORIZONS}
    frames = []
    for d, p in zip(dates, files):
        i0 = idx.get(d)
        if i0 is None or i0 < 20:
            continue
        sc = pd.read_parquet(p).set_index("ts_code")
        frames.append(sc)
        styles = pd.DataFrame({
            "size": np.log(w["circ_mv"].loc[d].reindex(sc.index)),
            "value": sc["val_comp_indneu"], "mom20": (ac.iloc[i0] / ac.iloc[i0 - 20] - 1).reindex(sc.index),
            "ret5": (ac.iloc[i0] / ac.iloc[i0 - 5] - 1).reindex(sc.index), "turn": sc["turn_20"], "vol": sc["ivol_60"]})
        styles = styles.apply(zscore)
        groups = {}
        if d in tier:
            m = tier[d]["members"]
            groups["DOUBLE"] = [x["code"] for x in m if x["tier"] == "DOUBLE"]
            groups["V43_ALL"] = [x["code"] for x in m]
        if d in books:
            groups["TOP100"], groups["AVOID70"] = books[d]["buffered_top100"], books[d]["avoid_losers_70"]
        for h in HORIZONS:
            if i0 + h >= len(sess):
                continue
            fwd = (ac.iloc[i0 + h] / ao.iloc[i0 + 1] - 1.0).reindex(sc.index)
            for g, codes in groups.items():
                r = decompose(fwd, styles, sc["industry"], codes)
                if r:
                    acc[h][g].append({"date": d, **r})
    out = {}
    for h in HORIZONS:
        out[f"h{h}"] = {}
        for g, rows in acc[h].items():
            if not rows:
                continue
            m = lambda f: float(np.mean([f(r) for r in rows]))  # noqa: E731
            out[f"h{h}"][g] = {
                "label": GROUP_LABELS[g], "days": len(rows), "avg_names": m(lambda r: r["n"]),
                "excess": m(lambda r: r["excess"]), "alpha": m(lambda r: r["alpha"]),
                "industry": m(lambda r: r["industry"]),
                "style": {k: m(lambda r, k=k: r["style"][k]) for k, _ in STYLES},
                "exposure": {k: m(lambda r, k=k: r["exposure"][k]) for k, _ in STYLES},
                "alpha_share": None,
                "top_industry": max(set(r["top_industry"] for r in rows), key=[r["top_industry"] for r in rows].count),
                "top_industry_share": m(lambda r: r["top_industry_share"]), "max_identity_error": max(abs(r["check"]) for r in rows)}
            e = out[f"h{h}"][g]["excess"]
            out[f"h{h}"][g]["alpha_share"] = (out[f"h{h}"][g]["alpha"] / e) if abs(e) > 1e-6 else None
    matured = {f"h{h}": max((len(v) for v in acc[h].values()), default=0) for h in HORIZONS}
    return {**doc, "status": "ok" if matured["h1"] else "collecting", "as_of": avail[-1], "matured": matured,
            "results": out, "redundancy": redundancy(frames), "baseline_corr_note": "Phase3 发现期：ivol_60 与 turn_20 秩相关 +0.69",
            "note": "恒等分解：超额 = 风格贡献 + 行业贡献 + 选股 alpha（残差）。小组（如双料王牌每日约 3 只）单日噪声极大，"
                    "只看跨日平均；样本不足时的方向不构成结论"}


def _jl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def main() -> int:
    res = compute()
    OUT.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    red = res.get("redundancy") or {}
    print(f"style_attribution: {res['status']} · 信号日 {res['signal_days']} · 已结算 {res.get('matured')}"
          f" · 有效因子数 {red.get('n_eff', '—')}/{red.get('n_factors', '—')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
