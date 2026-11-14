#!/usr/bin/env python3
"""micro_v1 · 特征构造(规格见 RESEARCH_SPEC_micro_v1.md; 只做构造, 不读收益)。

输入: outputs/overnight_v0/micro_v1/ticks_min.jsonl.gz(tdx_pull_ticks.py 输出), head.parquet
输出: outputs/overnight_v0/micro_v1/feat_A.parquet(截止 14:45, 只用分钟 < 14:45), feat_B.parquet(截止 14:56)
"""
from __future__ import annotations
import gzip, json, sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
from overnight_factor_model import OUT  # noqa: E402

M = OUT / "micro_v1"
FEATS = ["ob_day", "ob_pm", "ob_30", "ob_10", "lg_ob_day", "lg_ob_30", "xl_ob_day", "lg_share_30r", "size_30r", "intens_10r", "amt_10r",
         "ret_10", "ret_30", "up_share_30", "dd_30", "vwap_dev_30", "kyle_60", "auc_share", "ob_open30", "ob_flow_corr_60"]


def ob(b, s):
    t = b + s
    return (b - s) / t if t > 0 else np.nan


def feats(rec, cut):
    ks = sorted(k for k in rec["m"] if "09:30" <= k < cut)
    if len(ks) < 61:
        return None
    X = np.array([rec["m"][k] for k in ks], float)          # 末价, 最高, 最低, 主买, 主卖, 中性, 笔数, 大买, 大卖, 超大买, 超大卖
    p, hi, B, S, Nn, cnt, LB, LS, XB, XS = X[:, 0], X[:, 1], X[:, 3], X[:, 4], X[:, 5], X[:, 6], X[:, 7], X[:, 8], X[:, 9], X[:, 10]
    amt = B + S + Nn; tot = amt.sum(); n = len(ks)
    if tot <= 0 or (p <= 0).any():
        return None
    w = lambda k: slice(n - k, n)                            # noqa: E731
    pm = np.array([k >= "13:00" for k in ks]); op = np.array([k < "10:00" for k in ks])
    r = np.diff(p) / p[:-1]
    o = {"ob_day": ob(B.sum(), S.sum()), "ob_pm": ob(B[pm].sum(), S[pm].sum()) if pm.any() else np.nan,
         "ob_30": ob(B[w(30)].sum(), S[w(30)].sum()), "ob_10": ob(B[w(10)].sum(), S[w(10)].sum()),
         "lg_ob_day": (LB.sum() - LS.sum()) / tot, "lg_ob_30": (LB[w(30)].sum() - LS[w(30)].sum()) / max(amt[w(30)].sum(), 1.0),
         "xl_ob_day": (XB.sum() - XS.sum()) / tot}
    lg_day = (LB.sum() + LS.sum()) / tot; lg30 = (LB[w(30)].sum() + LS[w(30)].sum()) / max(amt[w(30)].sum(), 1.0)
    o["lg_share_30r"] = lg30 / lg_day if lg_day > 0 else np.nan
    o["size_30r"] = (amt[w(30)].sum() / max(cnt[w(30)].sum(), 1)) / (tot / max(cnt.sum(), 1))
    o["intens_10r"] = (cnt[w(10)].sum() / 10) / (cnt.sum() / n)
    o["amt_10r"] = amt[w(10)].sum() / (tot / n * 10)
    o["ret_10"] = p[-1] / p[-11] - 1; o["ret_30"] = p[-1] / p[-31] - 1
    o["up_share_30"] = float((r[-30:] > 0).mean())
    o["dd_30"] = p[-1] / hi[w(30)].max() - 1
    a30, p30 = amt[w(30)], p[w(30)]
    o["vwap_dev_30"] = p[-1] / (a30.sum() / (a30 / p30).sum()) - 1 if a30.sum() > 0 else np.nan
    f = (B - S)[1:][-60:] / (tot / n); rr = r[-60:]
    o["kyle_60"] = float(np.polyfit(f, rr, 1)[0] * 1e4) if f.std() > 0 else np.nan
    o["ob_flow_corr_60"] = float(np.corrcoef(f, rr)[0, 1]) if f.std() > 0 and rr.std() > 0 else np.nan
    auc = rec.get("auc")
    o["auc_share"] = (auc[0] * auc[1] * 100) / (tot + auc[0] * auc[1] * 100) if auc else np.nan
    o["ob_open30"] = ob(B[op].sum(), S[op].sum()) if op.any() else np.nan
    o["n_min"] = n
    return o


def main():
    H = pd.read_parquet(M / "head.parquet")
    keep = set(zip(H.ts_code, H.trade_date))
    rows = {"A": [], "B": []}
    with gzip.open(M / "ticks_min.jsonl.gz", "rt") as g:
        for line in g:
            code, d, rec = json.loads(line)
            if (code, d) not in keep:
                continue
            for proto, cut in (("A", "14:45"), ("B", "14:56")):
                o = feats(rec, cut)
                if o:
                    rows[proto].append({"ts_code": code, "trade_date": d, **o})
    for proto, L in rows.items():
        F = pd.DataFrame(L).drop_duplicates(["ts_code", "trade_date"])
        F.to_parquet(M / f"feat_{proto}.parquet")
        print(proto, F.shape, "coverage of head", round(len(F) / len(keep), 3), "minutes median", F.n_min.median())
        print(F[FEATS].describe().T[["count", "mean", "50%", "std"]].round(4).to_string())


if __name__ == "__main__":
    main()
