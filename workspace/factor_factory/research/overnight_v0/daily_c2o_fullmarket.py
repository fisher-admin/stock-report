#!/usr/bin/env python3
"""全主板日线口径隔夜段(收盘->次日开盘)披露(research-only, 2026-11-12)。
用于核对 5min 研究800样本的代表性。入场以收盘价代理(非可成交口径), 只看 C2O/O2C 段的方向与量级。"""
import glob, json, numpy as np, pandas as pd, pyarrow.parquet as pq
import os
from pathlib import Path
WS = Path(os.environ.get("STOCK_WS") or Path(__file__).resolve().parents[3])  # 本机=~/.openclaw/workspace, VPS=/srv/hermes-stock/workspace
pan = pq.read_table(WS/"factor_factory/data/research_panel_v2.parquet", columns=["ts_code","trade_date","market_board","avg_amount_lookback","pct_chg","high","low","close","pre_close"]).to_pandas()
pan = pan[(pan.market_board=="主板") & (pan.avg_amount_lookback>=3e5)]
rows=[]
for f in glob.glob(str(WS/"factor_factory/data/price_hist/daily/*.parquet"))+glob.glob(str(WS/"factor_factory/data/delisted_hist/daily/*.parquet")):
    d=pq.read_table(f,columns=["ts_code","trade_date","open","close","pre_close"]).to_pandas().sort_values("trade_date")
    d["n_open"]=d.open.shift(-1); d["n_pre"]=d.pre_close.shift(-1); d["n_close"]=d.close.shift(-1)
    rows.append(d[["ts_code","trade_date","n_open","n_pre","n_close"]])
nx=pd.concat(rows).drop_duplicates(["ts_code","trade_date"])
df=pan.merge(nx,on=["ts_code","trade_date"],how="left")
df["c2o"]=df.n_open/df.n_pre-1; df["o2c"]=df.n_close/df.n_open-1
df["lim_up"]=(df.pre_close*1.1+1e-9).round(2); df["at_limit"]=df.close>=df.lim_up-1e-6
rng=df.high-df.low; df["clv"]=((df.close-df.low)/rng).where(rng>0)
def nw(x,lag=5):
    x=np.asarray(x,float);n=len(x);e=x-x.mean();v=e@e/n
    for k in range(1,lag+1): v+=2*(1-k/(lag+1))*(e[k:]@e[:-k])/n
    return float(x.mean()/np.sqrt(v/n))
out={"rows":int(len(df)),"dates":[df.trade_date.min(),df.trade_date.max()],"missing_next":float(df.c2o.isna().mean())}
def stat(s,name):
    day=s.groupby("trade_date")[["c2o","o2c"]].mean().dropna()
    out[name]={"n":int(len(s)),"days":int(len(day)),"c2o_bp":round(day.c2o.mean()*1e4,2),"c2o_t":round(nw(day.c2o),2),"o2c_bp":round(day.o2c.mean()*1e4,2),"c2o_day_win":round(float((day.c2o>0).mean()),3)}
stat(df,"all_liquid_main")
stat(df[~df.at_limit],"not_limit_up_close")
stat(df[df.at_limit],"limit_up_close(不可买)")
stat(df[(~df.at_limit)&df.pct_chg.between(0,6)&(df.clv>=0.55)],"M_proxy_EOD(含收盘前视)")
for lo,hi in [(-11,-5),(-5,-2),(-2,0),(0,2),(2,4),(4,6),(6,9.5)]:
    stat(df[(~df.at_limit)&(df.pct_chg>lo)&(df.pct_chg<=hi)],f"pct({lo},{hi}]")
for y,s in df[~df.at_limit].groupby(df.trade_date.str[:4]): stat(s,f"year_{y}")
(WS/"factor_factory/outputs/overnight_v0/daily_c2o_fullmarket.json").write_text(json.dumps(out,ensure_ascii=False,indent=1))
for k,v in out.items(): print(k,v)
