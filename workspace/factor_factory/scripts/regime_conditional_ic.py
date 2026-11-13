#!/usr/bin/env python3
"""探索性（非预注册、不设闸门）：comp_rank_ew6 及 6 个分项的 IC 是否随市场环境 / 行业环境变化。

只用 gauntlet 的发现 + 验证期（< 20260701，holdout 保持锁定，本脚本不触碰）。h=5，IC = 每日全截面 Spearman
（因子 vs 次日开盘起 5 日超额收益）。三类条件：
  ex-ante  信号日已知：市场（等权）过去 20 日收益三分位、过去 20 日已实现波动三分位 —— 可用于"环境切换"
  ex-post  未来 5 日等权市场收益 上涨/下跌 —— 仅用于解释，不可交易
  industry 行业中性化 IC（行业内去均值后）vs 行业间 IC；各行业内 IC 与综合分的行业载荷

结果只描述性报告，不修改任何冻结口径；任何基于此的"环境择时"规则必须新增预注册后再前瞻验证。

  python regime_conditional_ic.py      # 写 outputs/gauntlet_v2/regime_conditional_ic.{json,md}
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gauntlet_v2 as gv  # noqa: E402
import phase3_run as p3  # noqa: E402
from tier_monitor import nw_t  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/gauntlet_v2"
H = 5


def tercile(s: pd.Series) -> pd.Series:
    r = s.rank(pct=True)
    return pd.cut(r, [0, 1 / 3, 2 / 3, 1.0], labels=["低", "中", "高"], include_lowest=True)


def stat(ic: pd.Series) -> dict:
    a = ic.dropna().to_numpy()
    if len(a) < 20:
        return {"days": int(len(a)), "mean": None, "t": None, "hit": None}
    return {"days": int(len(a)), "mean": round(float(a.mean()), 4), "t": round(float(nw_t(a, H - 1)), 2),
            "hit": round(float((a > 0).mean()), 3)}


def by_group(ic: pd.Series, lab: pd.Series) -> dict:
    df = pd.concat([ic.rename("ic"), lab.rename("g")], axis=1).dropna()
    return {str(k): stat(g["ic"]) for k, g in df.groupby("g", observed=True)}


def main() -> int:
    ctx = gv.load_context(allow_holdout=False)
    ranks = p3.component_ranks(ctx)
    ranks["comp_rank_ew6"] = p3.comp_rank_ew6(ranks.drop(columns=[c for c in ["comp_rank_ew6"] if c in ranks]))
    d = ctx.panel["trade_date"].to_numpy()
    m = gv._mask(ctx, H)
    y = gv.excess(ctx, H)
    # market environment from the panel itself (EW mean daily return of the universe)
    px = pd.read_parquet(gv.DATA / "research_panel_v2.parquet", columns=["trade_date", "pct_chg"])
    mkt = (px.groupby("trade_date")["pct_chg"].mean() / 100.0).sort_index()
    trail = (1 + mkt).rolling(20).apply(np.prod, raw=True) - 1
    vol = mkt.rolling(20).std()
    fwd5 = pd.Series(ctx.labels[f"fwd_open_{H}"].to_numpy()).groupby(d).mean()
    regimes = {"ex-ante · 市场过去 20 日收益": tercile(trail), "ex-ante · 市场过去 20 日波动": tercile(vol),
               "ex-post · 未来 5 日市场": pd.Series(np.where(fwd5 > 0, "上涨", "下跌"), index=fwd5.index)}
    res = {}
    for col in ranks.columns:
        ic = gv.daily_rank_ic(d[m], ranks[col].to_numpy()[m], y[m])
        res[col] = {"overall": stat(ic), "by_regime": {k: by_group(ic, lab) for k, lab in regimes.items()}}
    # industry: between vs within
    ind = ctx.panel["industry"].fillna("UNKNOWN").to_numpy()
    comp = ranks["comp_rank_ew6"]
    df = pd.DataFrame({"d": d, "ind": ind, "f": comp.to_numpy(), "y": y})[m].dropna()
    df["f_in"] = df["f"] - df.groupby(["d", "ind"])["f"].transform("mean")
    df["f_bt"] = df.groupby(["d", "ind"])["f"].transform("mean")
    ic_in = gv.daily_rank_ic(df["d"].to_numpy(), df["f_in"].to_numpy(), df["y"].to_numpy())
    ic_bt = gv.daily_rank_ic(df["d"].to_numpy(), df["f_bt"].to_numpy(), df["y"].to_numpy())
    per = {}
    for k, g in df.groupby("ind"):
        if g["d"].nunique() >= 200:
            per[k] = stat(gv.daily_rank_ic(g["d"].to_numpy(), g["f"].to_numpy(), g["y"].to_numpy(), min_n=15))
    load = df.groupby("ind")["f"].mean().sort_values()
    top_ind = df.assign(top=df["f"] >= df.groupby("d")["f"].transform(lambda s: s.quantile(0.9))).query("top")["ind"].value_counts(normalize=True)
    industry = {"within_industry_ic": stat(ic_in), "between_industry_ic": stat(ic_bt),
                "per_industry_ic": {k: per[k] for k in sorted(per, key=lambda x: -(per[x]["mean"] or -9))},
                "loading_lowest": {k: round(float(v), 3) for k, v in load.head(5).items()},
                "loading_highest": {k: round(float(v), 3) for k, v in load.tail(5).items()},
                "top_decile_industry_share": {k: round(float(v), 3) for k, v in top_ind.head(8).items()}}
    doc = {"generated_at": datetime.now().astimezone().isoformat(), "window": f"{df['d'].min()}–{df['d'].max()}",
           "horizon": H, "exploratory": True, "results": res, "industry": industry,
           "note": "探索性描述，非预注册；holdout 未触碰；ex-post 分组不可交易"}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "regime_conditional_ic.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    print(json.dumps({"window": doc["window"], "comp": res["comp_rank_ew6"], "industry": {k: industry[k] for k in
          ("within_industry_ic", "between_industry_ic", "loading_lowest", "loading_highest", "top_decile_industry_share")}},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
