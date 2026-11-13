#!/usr/bin/env python3
"""因子生命周期看板：对前瞻影子里每个信号日的全截面分数做滚动 IC / RankIC、衰减曲线、实盘 vs 回测落差与失效预警。

信号 = comp_rank_ew6 + 其 6 个分项因子（方向调整后）。样本 = factor_factory/data/forward_shadow/scores/ 里
每个前瞻信号日的 universe 全截面（约 2200 只），远比双料王牌每日 3 只的样本丰富，是判断"因子本身是否还有效"的主证据。

  IC(d, h) = Spearman(信号_d, 次日开盘→第 h 个交易日收盘的复权收益)，h ∈ {1, 5, 10, 20}；
  只前瞻：只读已记录的 scores_*.parquet，从不回补；出场日未到的 (d, h) 记为 pending。

回测基线：composite 取自 outputs/gauntlet_v2/PHASE3.md（发现期 / 验证期 mean IC）；分项取自 registry.jsonl 的
research 结果（h=5 mean_ic）。验证期并非干净样本外，基线偏乐观，所以"落差"是下限性的提示。

分环境基线：IC 强依赖市场方向（comp_rank_ew6 h5：下跌 +0.159 / 上涨 +0.019，混合 +0.081），单一混合基线在持续上涨
行情里会误报。每个已结算 (d, h) 按 universe 等权的同口径 h 日收益（次日开盘→第 h 日收盘，= forward_shadow 的
benchmark_ew）打 up(>0) / down 标签；有分环境基线的 h（取自 outputs/gauntlet_v2/regime_conditional_ic.json 的
ex-post 分组，仅发现+验证期、不含 holdout）对每日用匹配环境的基线，其余 h 退回混合基线。

预警（只提示，不改变任何生产或预注册判定）：
  - ic_below_backtest : 滚动 20 日均值低于（逐日匹配环境的）回测基线均值 2 个标准误（需 ≥ 20 个已结算信号日）
  - ic_not_positive   : 滚动 20 日均值的 NW t < 0（均值为负）
  - negative_streak   : 连续 ≥ 5 个信号日 IC < 0（h=1 除外，噪声太大）

  python lifecycle_monitor.py        # 写 outputs/forward_shadow/lifecycle_monitor.json
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tier_monitor import nw_t  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FS = ROOT / "data/forward_shadow"
OUT = ROOT / "outputs/forward_shadow"
RESULT = OUT / "lifecycle_monitor.json"
REGISTRY = ROOT / "outputs/gauntlet_v2/registry.jsonl"
PHASE3 = ROOT / "outputs/gauntlet_v2/PHASE3.md"
REGIME_IC = ROOT / "outputs/gauntlet_v2/regime_conditional_ic.json"
REGIMES = ("up", "down")
REGIME_CN = {"上涨": "up", "下跌": "down"}
HORIZONS = (1, 5, 10, 20)
ROLL = 20
MIN_N = 100
SIGNALS = [("comp_rank_ew6", 1, "综合分 comp_rank_ew6"), ("ivol_60", -1, "特质波动率 ivol_60↓"),
           ("turn_20", -1, "20 日换手 turn_20↓"), ("turn_abn_5_60", -1, "异常换手 turn_abn_5_60↓"),
           ("val_comp_indneu", 1, "行业中性估值 val_comp_indneu↑"), ("vpd_10", 1, "量价背离 vpd_10↑"),
           ("turn_cv_20", -1, "换手变异 turn_cv_20↓")]


def spearman_ic(x: pd.Series, y: pd.Series, min_n: int = MIN_N) -> float | None:
    m = x.notna() & y.notna()
    if m.sum() < min_n:
        return None
    return float(x[m].rank().corr(y[m].rank()))


def regime_of(mkt_ret: float | None) -> str | None:
    """'up' if the universe EW h-day return > 0, 'down' otherwise (same rule as forward_shadow regime_tag)."""
    if mkt_ret is None or not np.isfinite(mkt_ret):
        return None
    return "up" if mkt_ret > 0 else "down"


def rolling_flags(ics: list[float | None], h: int, baseline: float | None,
                  regimes: list[str | None] | None = None, regime_base: dict | None = None) -> dict:
    """Flags from a chronological IC list (None = day without enough names).

    regimes: per-day 'up'/'down' tag (parallel to ics); regime_base: {'up': x, 'down': y}.  When both are given, the
    gap z compares each day with its own regime's baseline (days without a tag fall back to ``baseline``)."""
    regimes = regimes if regimes is not None else [None] * len(ics)
    keep = [(v, g) for v, g in zip(ics, regimes) if v is not None and np.isfinite(v)]
    a = [v for v, _ in keep]
    tags = [g for _, g in keep]
    rb = {k: v for k, v in (regime_base or {}).items() if k in REGIMES and v is not None}
    use_regime = len(rb) == len(REGIMES)
    out = {"n": len(a), "flags": [], "baseline_mode": "regime" if use_regime else "pooled"}
    out["regime"] = {g: {"n": sum(t == g for t in tags), "roll_n": sum(t == g for t in tags[-ROLL:]),
                         "baseline": rb.get(g)} for g in REGIMES}
    for g in REGIMES:
        sub, rsub = [v for v, t in zip(a, tags) if t == g], [v for v, t in zip(a[-ROLL:], tags[-ROLL:]) if t == g]
        out["regime"][g]["mean"] = float(np.mean(sub)) if sub else None
        out["regime"][g]["roll_mean"] = float(np.mean(rsub)) if rsub else None
    if not a:
        return out
    arr = np.asarray(a)
    out.update(mean=float(arr.mean()), std=float(arr.std(ddof=1)) if len(arr) > 1 else None,
               hit=float((arr > 0).mean()), nw_t=nw_t(arr, max(h - 1, 0)))
    out["ir"] = out["mean"] / out["std"] if out.get("std") else None
    last = arr[-ROLL:]
    out["roll_n"] = len(last)
    out["roll_mean"] = float(last.mean())
    if len(last) >= ROLL:
        t = nw_t(last, max(h - 1, 0))
        out["roll_t"] = t
        if t is not None and t < 0:
            out["flags"].append("ic_not_positive")
        b = [rb.get(g, baseline) if use_regime else baseline for g in tags[-ROLL:]]
        if all(x is not None for x in b) and len(last) > 1:
            resid = last - np.asarray(b, dtype=float)
            se = float(resid.std(ddof=1) / np.sqrt(len(resid)))
            out["baseline_expected"] = float(np.mean(b))
            out["gap_z"] = float(resid.mean()) / se if se > 0 else None
            if out["gap_z"] is not None and out["gap_z"] < -2:
                out["flags"].append("ic_below_backtest")
    if h > 1:
        streak = 0
        for v in reversed(a):
            if v < 0:
                streak += 1
            else:
                break
        out["neg_streak"] = streak
        if streak >= 5:
            out["flags"].append("negative_streak")
    return out


def load_baselines() -> dict:
    """{signal: {h: {'discovery': x, 'validation': y}}} for composites (PHASE3.md) and components (registry p2_<name>_h<H>, research role)."""
    base: dict = {}
    if PHASE3.exists():
        for ln in PHASE3.read_text().splitlines():
            m = re.match(r"\|\s*comp_rank_ew6\s*\|\s*(\d+)\s*\|\s*([+-][\d.]+)\s*\([^)]*\)\s*\|\s*([+-][\d.]+)", ln)
            if m:
                base.setdefault("comp_rank_ew6", {})[int(m.group(1))] = {"discovery": float(m.group(2)),
                                                                          "validation": float(m.group(3))}
    if REGISTRY.exists():
        pre, res = {}, {}
        for ln in REGISTRY.read_text().splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            if r.get("kind") == "preregistration":
                pre[r["registration_id"]] = r.get("spec", {})
            elif r.get("kind") == "result" and "mean_ic" in (r.get("summary") or {}):
                res[r["registration_id"]] = r["summary"]["mean_ic"]
        names = {s for s, _, _ in SIGNALS if s != "comp_rank_ew6"}
        for rid, spec in pre.items():
            m = re.fullmatch(r"p2_(.+)_h(\d+)", str(spec.get("factor_id", "")))
            if m and m.group(1) in names and rid in res and spec.get("role") == "research":
                base.setdefault(m.group(1), {})[int(m.group(2))] = {"research": float(res[rid])}
    return base


def baseline_for(base: dict, sig: str, h: int) -> float | None:
    b = base.get(sig, {}).get(h)
    if not b:
        return None
    return b.get("validation", b.get("research"))


def load_regime_baselines(path: Path = REGIME_IC) -> tuple[dict, dict]:
    """({signal: {h: {'up': ic, 'down': ic, 'up_days': n, 'down_days': n}}}, source meta) from the exploratory
    regime_conditional_ic.json (ex-post future-market split).  Refuses a file whose window reaches the holdout."""
    from gauntlet_v2 import HOLDOUT_START
    meta = {"path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path), "used": False}
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}, {**meta, "reason": "文件缺失或无法解析，全部退回混合基线"}
    window, h = str(doc.get("window", "")), doc.get("horizon")
    end = re.findall(r"\d{8}", window)
    meta.update(window=window, horizon=h, exploratory=bool(doc.get("exploratory")))
    if not end or end[-1] >= HOLDOUT_START or not isinstance(h, int):
        return {}, {**meta, "reason": f"窗口缺失或触及 holdout（≥{HOLDOUT_START}），拒用"}
    out: dict = {}
    for sig, r in (doc.get("results") or {}).items():
        grp = next((v for k, v in (r.get("by_regime") or {}).items() if k.startswith("ex-post")), None) or {}
        b = {REGIME_CN[k]: v for k, v in grp.items() if k in REGIME_CN}
        if all(g in b and b[g].get("mean") is not None for g in REGIMES):
            out[sig] = {h: {**{g: float(b[g]["mean"]) for g in REGIMES}, **{f"{g}_days": b[g].get("days") for g in REGIMES}}}
    return out, {**meta, "used": bool(out), "horizons": [h] if out else []}


def compute(now: datetime | None = None) -> dict:
    import forward_shadow as fs
    import research_panel_v2 as rp
    now = now or datetime.now().astimezone()
    files = sorted((FS / "scores").glob("scores_*.parquet"))
    dates = [p.stem.split("_")[-1] for p in files]
    base = load_baselines()
    rbase, rmeta = load_regime_baselines()
    doc = {"contract_version": "lifecycle_monitor_v1", "generated_at": now.isoformat(), "horizons": list(HORIZONS),
           "signals": {s: label for s, _, label in SIGNALS}, "signal_days": len(dates)}
    if not dates:
        return {**doc, "status": "collecting", "message": "尚无前瞻分数文件"}
    cal = fs.official_sessions("99999999")
    avail = [x for x in cal if (rp.BACKTEST_CACHE / f"daily_{x}.parquet").exists()]
    sess = [x for x in cal if dates[0] <= x <= avail[-1]]
    doc.update(first_signal=dates[0], last_signal=dates[-1], as_of=avail[-1])
    w = fs.window_mats(sess)
    ac = w["adj_close"].ffill()
    ao = w["adj_open"]
    idx = {d: i for i, d in enumerate(sess)}
    series: dict = {s: {h: [] for h in HORIZONS} for s, _, _ in SIGNALS}
    mkt: dict = {h: {} for h in HORIZONS}
    for d, p in zip(dates, files):
        i0 = idx.get(d)
        if i0 is None:
            continue
        sc = pd.read_parquet(p).set_index("ts_code")
        for h in HORIZONS:
            if i0 + h >= len(sess):
                continue
            fwd = (ac.iloc[i0 + h] / ao.iloc[i0 + 1] - 1.0).reindex(sc.index)
            mkt[h][d] = float(fwd.mean()) if fwd.notna().any() else None   # universe EW = benchmark_ew period return
            for s, sign, _ in SIGNALS:
                if s in sc.columns:
                    series[s][h].append((d, spearman_ic(sign * sc[s] if s != "comp_rank_ew6" else sc[s], fwd)))
    out, alerts = {}, []
    for s, _, label in SIGNALS:
        out[s] = {"label": label, "by_h": {}, "decay": {}}
        for h in HORIZONS:
            pts = series[s][h]
            rb = rbase.get(s, {}).get(h)
            st = rolling_flags([v for _, v in pts], h, baseline_for(base, s, h),
                               [regime_of(mkt[h].get(d)) for d, _ in pts], rb)
            st["baseline"] = base.get(s, {}).get(h)
            if rb:
                for g in REGIMES:
                    st["regime"][g]["baseline_days"] = rb.get(f"{g}_days")
            st["last_dates"] = [d for d, _ in pts][-ROLL:]
            st["spark"] = [None if v is None else round(v, 4) for _, v in pts][-60:]
            out[s]["by_h"][f"h{h}"] = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in st.items()}
            out[s]["by_h"][f"h{h}"]["regime"] = {g: {k: (round(v, 5) if isinstance(v, float) else v) for k, v in x.items()}
                                                 for g, x in st["regime"].items()}
            out[s]["decay"][f"h{h}"] = out[s]["by_h"][f"h{h}"].get("mean")
            for f in st["flags"]:
                alerts.append({"signal": s, "h": h, "flag": f, "baseline_mode": st["baseline_mode"]})
    progress = {}
    try:
        progress["books_matured_periods"] = json.loads((FS / "state.json").read_text()).get("matured_periods", 0)
        progress["books_required_periods"] = 50
        tm = json.loads((OUT / "tier_monitor.json").read_text())
        progress["tier_h5_days"] = (tm.get("spreads", {}).get("h5", {}).get("DOUBLE_vs_NON_DOUBLE", {}) or {}).get("days", 0)
        progress["tier_required_days"] = 60
    except (OSError, json.JSONDecodeError):
        pass
    matured = {f"h{h}": len(series["comp_rank_ew6"][h]) for h in HORIZONS}
    regime_days = {f"h{h}": {g: sum(regime_of(v) == g for v in mkt[h].values()) for g in REGIMES} for h in HORIZONS}
    return {**doc, "status": "ok" if matured["h1"] else "collecting", "matured": matured, "alerts": alerts,
            "results": out, "progress": progress, "min_names_per_day": MIN_N, "roll_window": ROLL,
            "regime_days": regime_days,
            "regime_baseline": {**rmeta, "definition": "up = universe 等权同口径 h 日收益（次日开盘→第 h 日收盘）> 0，否则 down；"
                                "ex-post 标签仅用于解释与基线匹配，不可交易",
                                "fallback": "无分环境基线的 h 用混合基线"},
            "note": "基线来自回测发现/验证期，验证期并非干净样本外；有分环境基线的 h 按每日市场涨/跌匹配基线；"
                    "预警仅提示，不改变任何预注册判定"}


def main() -> int:
    res = compute()
    OUT.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    m = res.get("matured", {})
    a = res.get("alerts", [])
    print(f"lifecycle_monitor: {res['status']} · 信号日 {res['signal_days']} · IC 已结算 h1 {m.get('h1', 0)}/h5 {m.get('h5', 0)}"
          f" · 预警 {len(a)}" + ("：" + "、".join(f"{x['signal']}@h{x['h']}:{x['flag']}" for x in a[:4]) if a else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
