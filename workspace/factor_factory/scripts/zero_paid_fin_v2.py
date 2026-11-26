#!/usr/bin/env python3
"""Isolated zero-paid financial store reader + PIT fundamentals engine (v2 track only).

Used ONLY by the Core7P-native shadow (``forward_shadow_v2``).  No v1 code path may import
or read this module / its data.  It does NOT import ``aegis_scorer`` (frozen); the logic of
``compute_pit_fundamentals_at_date`` is re-implemented here, with these deliberate additions:

* extra outputs: dedt_ttm, np_ttm, rev_ttm, dedt_yoy, rev_yoy, ocf_ttm, ocf_applicable,
  dedt_source, latest_report_date, latest_ann_date (+ mv_yuan / be_yuan convenience arrays);
* the hard board filter is ``mandate_guard.is_allowed_mandate_code`` (60/00/30 only) applied to
  every table;
* forward zero-paid capture (``em_ann_store``) rows are merged in under strict PIT rules;
* forecast events use a relaxed pre-period-end filter ((eff_ann - end_date).days >= -45);
  income/express events keep the aegis filter (eff_ann >= end_date); all keep lag <= 150 days;
* strict ``dedt_ttm`` never mixes parent net profit across quarters (``dedt_ttm`` is NaN and
  ``dedt_source`` is ``fallback_parent_netprofit`` / ``missing`` whenever any contributing
  quarter lacks real deducted profit).

DATA SOURCES (probed 2026-11-26)
--------------------------------
Immutable seed ``factor_factory/data/zero_paid_fin_v2/seed/<sub>/<YYYYMMDD>.parquet`` (Tushare
units: yuan except forecast net_profit_min/max in WAN yuan = 1e4):
  income_vip(ts_code, ann_date, f_ann_date, end_date, report_type, total_revenue, n_income_attr_p,
  update_flag), fina_indicator_vip(profit_dedt cumulative yuan), forecast_vip, express,
  cf_v2(n_cashflow_act cumulative yuan), bs_v2, early/sw_member.parquet (l1_name ...).
  update_flag '0' = first version, '1' = revised -> first announcement = lowest update_flag.
Forward capture ``stock_data/03-working/em_ann_store/<tbl>/seen_<day>[_N].parquet`` (all STRING
columns + _key,_hash,_seen_date,_seen_at; ``em_ann_store`` is append-only versions: ``seen_<day>.parquet``
stores every newly seen version per _key and files are never rewritten, so the FIRST-SEEN version,
min _seen_at, is used as the first announcement):
  report  (RPT_LICO_FN_CPD): SECUCODE (ts_code form), REPORTDATE, UPDATE_DATE, NOTICE_DATE,
          BASIC_EPS, DEDUCT_BASIC_EPS, TOTAL_OPERATE_INCOME, PARENT_NETPROFIT (yuan, cumulative)
  income  (RPT_DMSK_FN_INCOME): SECUCODE, NOTICE_DATE, REPORT_DATE, PARENT_NETPROFIT,
          TOTAL_OPERATE_INCOME, DEDUCT_PARENT_NETPROFIT (cumulative deducted NP, yuan)  <- direct dedt
  cashflow(RPT_DMSK_FN_CASHFLOW): SECUCODE, NOTICE_DATE, REPORT_DATE, NETCASH_OPERATE (yuan, cum.)
  forecast(RPT_PUBLIC_OP_NEWPREDICT): SECUCODE, NOTICE_DATE, REPORT_DATE, PREDICT_FINANCE_CODE
          ('004' = 归属于上市公司股东的净利润, '005' = 扣非, '002' 净利润, '003' EPS, '006' 营收),
          PREDICT_AMT_LOWER/UPPER are YUAN (seed net_profit_min/max are WAN: ratio EM/seed*1e-4 == 1).
  express (RPT_FCI_PERFORMANCEE): SECUCODE, UPDATE_DATE, REPORT_DATE, TOTAL_OPERATE_INCOME,
          PARENT_NETPROFIT (yuan).
Merge rules: visible seed always wins for any (ts_code,end_date) it covers as of d_target; EM fills
only uncovered (ts_code,end_date) keys (later periods or seed gaps).  EM availability:
eff_ann = max(notice/update date, _seen_date[, _actual_seen_date]) with valid 8-digit _seen_date
required (else fail-closed '99999999'); visible from the NEXT session (side='right').
EM dedt source priority:
income.DEDUCT_PARENT_NETPROFIT ('em_deduct_parent_netprofit') > report eps-derived
PARENT_NETPROFIT*DEDUCT_BASIC_EPS/BASIC_EPS ('em_deduct_eps_derived') > parent NP fallback
('fallback_parent_netprofit', used only in ep_dedt/roe_dedt while dedt_ttm remains NaN and
NOT counted as "real" dedt in coverage()).
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mandate_guard as mg  # noqa: E402

FLOOR_MV, FLOOR_NA = 0.0025, 0.0050
WINDOW_SESSIONS = 130
MAX_LAG_DAYS = 150
FC_PRE_END_DAYS = -45
REAL_DEDT_SOURCES = ("seed_profit_dedt", "em_deduct_eps_derived", "em_deduct_parent_netprofit")
NONFIN_OCF_EXCLUDED_L1 = ("银行", "非银金融")
FC_PARENT_NP_NAME = "归属于上市公司股东的净利润"
FC_PARENT_NP_CODE = "004"

EM_TABLES = ("report", "income", "cashflow", "forecast", "express")


# ----------------------------------------------------------------------------- helpers
def _default_workspace(workspace=None) -> Path:
    if workspace is not None:
        return Path(workspace)
    for env in ("STOCK_SYSTEM_WORKSPACE", "OPENCLAW_WORKSPACE_DIR"):
        if os.environ.get(env):
            return Path(os.environ[env])
    return Path(__file__).resolve().parents[2]


def _s(series: pd.Series) -> pd.Series:
    """str-normalised object column ('' for missing)."""
    return series.astype("string").fillna("").astype(object)


def _ymd(series: pd.Series) -> pd.Series:
    """'2026-10-24 00:00:00' | '20261024' -> '20261024' ('' if not parseable)."""
    t = series.astype("string").fillna("").str.strip().str.replace("-", "", regex=False).str[:8]
    t = t.where((t.str.len() == 8) & t.str.isdigit(), "")
    return t.astype(object)


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _is_ymd8(val) -> bool:
    return isinstance(val, str) and len(val) == 8 and val.isdigit()


def _mandate_filter(df: pd.DataFrame, col: str = "ts_code") -> pd.DataFrame:
    if df.empty:
        return df
    uniq = pd.Index(df[col].unique())
    ok = pd.Series(mg.is_allowed_mandate_board(list(uniq)), index=uniq)
    return df[df[col].map(ok).fillna(False).to_numpy(dtype=bool)].reset_index(drop=True)


def _map_avail_date(eff_ann: np.ndarray, cal_all: np.ndarray) -> np.ndarray:
    """First trading session STRICTLY AFTER ``eff_ann`` (side='right'); '99999999' if none."""
    eff = np.asarray(eff_ann, dtype="U8")
    cal_all = np.asarray(cal_all, dtype="U8")
    out = np.full(len(eff), "99999999", dtype="U8")
    if len(eff) == 0 or len(cal_all) == 0:
        return out
    pre = eff < cal_all[0]
    out[pre] = eff[pre]
    in_range = (~pre) & (eff < cal_all[-1])
    pos = np.searchsorted(cal_all, eff[in_range], side="right")
    valid = pos < len(cal_all)
    idx_in = np.where(in_range)[0]
    out[idx_in[valid]] = cal_all[pos[valid]]
    return out


def _secucode_from_plain(code6: pd.Series) -> pd.Series:
    c = code6.astype("string").fillna("").str.strip()
    suffix = np.where(c.str[:1].isin(["6", "9"]), ".SH", np.where(c.str[:1].isin(["0", "2", "3"]), ".SZ", ".BJ"))
    return (c + pd.Series(suffix, index=c.index)).astype(object)


def _first_seen(df: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    """Keep the first-seen version (min _seen_at, then _seen_date) per key, preferring valid 8-digit _seen_date."""
    if df.empty:
        return df
    d = df.copy()
    sd = d["_seen_date"].astype("string").fillna("")
    d["_bad_seen"] = (sd.str.len() != 8) | (~sd.str.isdigit())
    d = d.sort_values(["_bad_seen", "_seen_at", "_seen_date"], kind="mergesort").drop(columns=["_bad_seen"])
    return d.drop_duplicates(key_cols, keep="first").reset_index(drop=True)


def _read_dir(dirpath: Path, pattern: str, cols: list[str] | None = None) -> pd.DataFrame:
    if not dirpath.exists():
        return pd.DataFrame()
    frames = []
    for fp in sorted(dirpath.glob(pattern)):
        if fp.name.startswith("._"):
            continue
        raw = pd.read_parquet(fp)
        if cols is not None:
            raw = raw[[c for c in cols if c in raw.columns]]
        frames.append(raw)
    frames = [f for f in frames if len(f)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _eff_ann_two(a: pd.Series, f: pd.Series) -> np.ndarray:
    """eff_ann = max(ann_date, f_ann_date) (aegis rule)."""
    av, fv = a.to_numpy(dtype=object), f.to_numpy(dtype=object)
    a_ok = np.array([_is_ymd8(x) for x in av], dtype=bool)
    f_ok = np.array([_is_ymd8(x) for x in fv], dtype=bool)
    mx = np.where(av >= fv, av, fv)
    return np.where(a_ok & f_ok, mx, np.where(f_ok, fv, np.where(a_ok, av, "99999999"))).astype(object)


def _q_back(y_arr: np.ndarray, q_arr: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    tot = y_arr * 4 + (q_arr - 1) - k
    return tot // 4, (tot % 4) + 1


def _ttm_from_cum(
    K: pd.DataFrame,
    col: str,
    avail_col: str = "avail",
    cap_override: str | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """TTM and TTM one year earlier from cumulative values; K indexed by unique (ts_code,y,q) with
    ``avail_col`` (str).  Lookups only see periods with avail <= cap (aegis look_R)."""
    idx = K.index
    cap = np.full(len(idx), cap_override, dtype=object) if cap_override is not None else K[avail_col].to_numpy()
    lv0, lv1 = idx.get_level_values(0), idx.get_level_values(1)

    def look(dy: int, q_fixed: int | None) -> np.ndarray:
        q_l = idx.get_level_values(2) if q_fixed is None else np.full(len(idx), q_fixed)
        key = pd.MultiIndex.from_arrays([lv0, lv1 + dy, q_l])
        v = K[col].reindex(key).to_numpy(dtype=np.float64)
        av = K[avail_col].reindex(key).fillna("99999999").to_numpy()
        return np.where(av <= cap, v, np.nan)

    q_arr = K["q"].to_numpy()
    c_val = K[col].to_numpy(dtype=np.float64)
    if cap_override is not None:
        c_val = np.where(K[avail_col].to_numpy() <= cap, c_val, np.nan)
    c_fy1, c_ly, c_fy2, c_ly2 = look(-1, 4), look(-1, None), look(-2, 4), look(-2, None)
    ttm = np.where(q_arr == 4, c_val, c_val + c_fy1 - c_ly)
    ttm_ly = np.where(q_arr == 4, c_fy1, c_ly + c_fy2 - c_ly2)
    return ttm, ttm_ly


def _select_latest(K: pd.DataFrame, t_pos: int, cal_pit: np.ndarray, extra_filter=None) -> pd.DataFrame:
    """aegis K_reg/K_ev selection: one row per (code, avail) with max end_date, drop rows whose
    end_date regressed vs the running max, keep rows within WINDOW_SESSIONS of t_pos."""
    K = K.sort_values(["ts_code", "avail", "end_date"]).drop_duplicates(["ts_code", "avail"], keep="last").copy()
    K["_e"] = K["end_date"].astype(int)
    K = K[K["_e"] >= K.groupby("ts_code")["_e"].cummax()].drop(columns=["_e"]).reset_index(drop=True)
    K["apos"] = np.searchsorted(cal_pit, K["avail"].to_numpy().astype("U8"), side="left").astype(np.int64)
    K = K[(K["apos"] <= t_pos) & (t_pos - K["apos"] <= WINDOW_SESSIONS)].sort_values("apos", kind="mergesort")
    return K.reset_index(drop=True)


# ----------------------------------------------------------------------------- store
class ZeroPaidFinStoreV2:
    def __init__(self, workspace=None, seed_dir=None, em_dir=None, daily_basic_dir=None, sw_path=None):
        self.workspace = _default_workspace(workspace)
        ff = self.workspace / "factor_factory"
        self.seed_dir = Path(seed_dir) if seed_dir is not None else ff / "data/zero_paid_fin_v2/seed"
        self.em_dir = Path(em_dir) if em_dir is not None else self.workspace / "stock_data/03-working/em_ann_store"
        self.daily_basic_dir = (
            Path(daily_basic_dir)
            if daily_basic_dir is not None
            else self.workspace / "stock_data/03-working/fundamental_cache/pit_market/v2"
        )
        self.sw_path = Path(sw_path) if sw_path is not None else self.seed_dir / "early/sw_member.parquet"
        self._cache: dict[str, object] = {}
        self._db_cache: dict[str, pd.DataFrame | None] = {}
        self.last_diag: dict = {}

    # ---- seed tables -------------------------------------------------------------------------
    def _seed(self, sub: str, cols: list[str], str_cols: list[str]) -> pd.DataFrame:
        key = "seed_" + sub
        if key in self._cache:
            return self._cache[key]  # type: ignore[return-value]
        raw = _read_dir(self.seed_dir / sub, "*.parquet", cols)
        if raw.empty:
            out = pd.DataFrame({c: pd.Series(dtype=object) for c in cols})
        else:
            for c in cols:
                if c not in raw.columns:
                    raw[c] = np.nan
            out = _mandate_filter(raw[cols].copy())
            for c in str_cols:
                out[c] = _s(out[c])
        self._cache[key] = out
        return out

    @property
    def inc_raw(self) -> pd.DataFrame:
        return self._seed("income_vip", ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type",
                                         "total_revenue", "n_income_attr_p", "update_flag"],
                          ["ts_code", "ann_date", "f_ann_date", "end_date", "update_flag"])

    @property
    def fi_raw(self) -> pd.DataFrame:
        return self._seed("fina_indicator_vip", ["ts_code", "ann_date", "end_date", "profit_dedt", "update_flag"],
                          ["ts_code", "ann_date", "end_date", "update_flag"])

    @property
    def fc_raw(self) -> pd.DataFrame:
        return self._seed("forecast_vip", ["ts_code", "ann_date", "end_date", "net_profit_min", "net_profit_max", "update_flag"],
                          ["ts_code", "ann_date", "end_date", "update_flag"])

    @property
    def ex_raw(self) -> pd.DataFrame:
        return self._seed("express", ["ts_code", "ann_date", "end_date", "revenue", "n_income", "update_flag"],
                          ["ts_code", "ann_date", "end_date", "update_flag"])

    @property
    def cf_raw(self) -> pd.DataFrame:
        return self._seed("cf_v2", ["ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "n_cashflow_act", "update_flag"],
                          ["ts_code", "ann_date", "f_ann_date", "end_date", "update_flag"])

    # ---- EM forward capture --------------------------------------------------------------------
    def _em_raw(self, tbl: str) -> pd.DataFrame:
        key = "em_" + tbl
        if key in self._cache:
            return self._cache[key]  # type: ignore[return-value]
        raw = _read_dir(self.em_dir / tbl, "seen_*.parquet")
        if not raw.empty:
            for c in ("_key", "_seen_date", "_seen_at"):
                if c not in raw.columns:
                    raw[c] = ""
            for c in ("_key", "_seen_date", "_seen_at"):
                raw[c] = _s(raw[c])
            if "_actual_seen_date" in raw.columns:
                raw["_actual_seen_date"] = _ymd(raw["_actual_seen_date"])
            if "SECUCODE" in raw.columns:
                sec = _s(raw["SECUCODE"])
                if "SECURITY_CODE" in raw.columns:
                    miss = sec == ""
                    if miss.any():
                        sec = sec.where(~miss, _secucode_from_plain(raw["SECURITY_CODE"]))
            elif "SECURITY_CODE" in raw.columns:
                sec = _secucode_from_plain(raw["SECURITY_CODE"])
            else:
                sec = pd.Series([""] * len(raw), index=raw.index, dtype=object)
            raw["ts_code"] = sec
            raw = _mandate_filter(raw)
        self._cache[key] = raw
        return raw

    def _em_eff(self, ann: pd.Series, seen: pd.Series, actual_seen: pd.Series | None = None) -> np.ndarray:
        """Fail-closed effective announcement date: require valid 8-digit _seen_date (else '99999999')."""
        a = ann.to_numpy(dtype=object)
        s = seen.to_numpy(dtype=object)
        act = actual_seen.to_numpy(dtype=object) if actual_seen is not None else None
        out = np.full(len(s), "99999999", dtype=object)
        for i, s_i in enumerate(s):
            if not _is_ymd8(s_i):
                continue
            eff_seen = s_i
            if act is not None and _is_ymd8(act[i]) and act[i] > eff_seen:
                eff_seen = act[i]
            a_i = a[i]
            out[i] = a_i if (_is_ymd8(a_i) and a_i >= eff_seen) else eff_seen
        return out

    def em_R(self, version: str = "first") -> pd.DataFrame:
        """EM income-statement-like rows per (ts_code,end_date): total_revenue, n_income_attr_p,
        dedt_cum_em, dedt_label, eff_ann.  ``version``: 'first' (PIT, default) or 'latest'."""
        key = "em_R_" + version
        if key in self._cache:
            return self._cache[key]  # type: ignore[return-value]
        cols = ["ts_code", "end_date", "eff_ann", "total_revenue", "n_income_attr_p", "dedt_cum_em", "dedt_label"]

        def pick(df: pd.DataFrame, k: list[str]) -> pd.DataFrame:
            if version == "latest":
                d = df.sort_values(["_seen_at", "_seen_date"], kind="mergesort")
                return d.drop_duplicates(k, keep="last").reset_index(drop=True)
            return _first_seen(df, k)

        rep, inc = self._em_raw("report"), self._em_raw("income")
        parts = {}
        if not rep.empty and "REPORTDATE" in rep.columns:
            r = rep.copy()
            r["end_date"] = _ymd(r["REPORTDATE"])
            ann = _ymd(r["NOTICE_DATE"]) if "NOTICE_DATE" in r.columns else _ymd(r["UPDATE_DATE"])
            if "UPDATE_DATE" in r.columns and "NOTICE_DATE" in r.columns:
                ann = ann.where(ann != "", _ymd(r["UPDATE_DATE"]))
            r["eff"] = self._em_eff(ann, r["_seen_date"], r.get("_actual_seen_date"))
            r = r[r["end_date"] != ""]
            r = pick(r, ["ts_code", "end_date"])
            beps, deps = _num(r.get("BASIC_EPS")), _num(r.get("DEDUCT_BASIC_EPS"))
            npv = _num(r.get("PARENT_NETPROFIT"))
            with np.errstate(divide="ignore", invalid="ignore"):
                derived = np.where(np.isfinite(beps) & (beps != 0) & np.isfinite(deps) & np.isfinite(npv), npv * deps / beps, np.nan)
            parts["rep"] = pd.DataFrame({
                "ts_code": r["ts_code"], "end_date": r["end_date"], "eff_rep": r["eff"],
                "rev_rep": _num(r.get("TOTAL_OPERATE_INCOME")), "np_rep": npv, "dedt_rep": derived,
            })
        if not inc.empty and "REPORT_DATE" in inc.columns:
            r = inc.copy()
            r["end_date"] = _ymd(r["REPORT_DATE"])
            r["eff"] = self._em_eff(_ymd(r["NOTICE_DATE"]), r["_seen_date"], r.get("_actual_seen_date"))
            r = r[r["end_date"] != ""]
            r = pick(r, ["ts_code", "end_date"])
            parts["inc"] = pd.DataFrame({
                "ts_code": r["ts_code"], "end_date": r["end_date"], "eff_inc": r["eff"],
                "rev_inc": _num(r.get("TOTAL_OPERATE_INCOME")), "np_inc": _num(r.get("PARENT_NETPROFIT")),
                "dedt_inc": _num(r.get("DEDUCT_PARENT_NETPROFIT")),
            })
        if not parts:
            out = pd.DataFrame({c: pd.Series(dtype=object) for c in cols})
            self._cache[key] = out
            return out
        if "rep" in parts and "inc" in parts:
            m = parts["rep"].merge(parts["inc"], on=["ts_code", "end_date"], how="outer")
        else:
            m = list(parts.values())[0].copy()
        for c in ("eff_rep", "eff_inc", "rev_rep", "np_rep", "dedt_rep", "rev_inc", "np_inc", "dedt_inc"):
            if c not in m.columns:
                m[c] = np.nan if not c.startswith("eff") else ""
        for c in ("eff_rep", "eff_inc"):
            m[c] = m[c].fillna("").astype(object)
        effs = np.vstack([m["eff_rep"].to_numpy(dtype=object), m["eff_inc"].to_numpy(dtype=object)])
        eff = np.where(effs[0] >= effs[1], effs[0], effs[1]).astype(object)
        rev = m["rev_inc"].where(m["rev_inc"].notna(), m["rev_rep"])
        npv = m["np_inc"].where(m["np_inc"].notna(), m["np_rep"])
        use_inc = m["dedt_inc"].notna()
        dedt = m["dedt_inc"].where(use_inc, m["dedt_rep"])
        label = np.where(use_inc, "em_deduct_parent_netprofit", np.where(m["dedt_rep"].notna(), "em_deduct_eps_derived", ""))
        out = pd.DataFrame({"ts_code": m["ts_code"], "end_date": m["end_date"], "eff_ann": eff, "total_revenue": rev,
                            "n_income_attr_p": npv, "dedt_cum_em": dedt, "dedt_label": label})
        self._cache[key] = out
        return out

    def _em_R_as_of(self, d_target: str, cal_pit: np.ndarray, codes_set: set[str]) -> pd.DataFrame:
        """Strictly causal EM report+income merge as of ``d_target``: each table is filtered to
        eff < d_target and avail <= d_target BEFORE _first_seen and BEFORE merging."""
        cols = ["ts_code", "end_date", "eff_ann", "avail", "total_revenue", "n_income_attr_p",
                "dedt_cum", "dedt_label", "dedt_avail"]
        rep, inc = self._em_raw("report"), self._em_raw("income")
        rep_df = pd.DataFrame()
        if not rep.empty and "REPORTDATE" in rep.columns:
            r = rep[rep["ts_code"].isin(codes_set)].copy()
            if len(r):
                r["end_date"] = _ymd(r["REPORTDATE"])
                ann = _ymd(r["NOTICE_DATE"]) if "NOTICE_DATE" in r.columns else _ymd(r["UPDATE_DATE"])
                if "UPDATE_DATE" in r.columns and "NOTICE_DATE" in r.columns:
                    ann = ann.where(ann != "", _ymd(r["UPDATE_DATE"]))
                r["eff_rep"] = self._em_eff(ann, r["_seen_date"], r.get("_actual_seen_date"))
                r = r[(r["end_date"] != "") & (r["end_date"] <= d_target) & (r["eff_rep"] < d_target)].copy()
                if len(r):
                    r["avail_rep"] = _map_avail_date(r["eff_rep"].to_numpy(), cal_pit)
                    r = r[r["avail_rep"] <= d_target].copy()
                    if len(r):
                        r = _first_seen(r, ["ts_code", "end_date"])
                        beps, deps = _num(r.get("BASIC_EPS")), _num(r.get("DEDUCT_BASIC_EPS"))
                        npv = _num(r.get("PARENT_NETPROFIT"))
                        with np.errstate(divide="ignore", invalid="ignore"):
                            derived = np.where(
                                np.isfinite(beps) & (beps != 0) & np.isfinite(deps) & np.isfinite(npv),
                                npv * deps / beps,
                                np.nan,
                            )
                        rep_df = pd.DataFrame({
                            "ts_code": r["ts_code"], "end_date": r["end_date"],
                            "eff_rep": r["eff_rep"], "avail_rep": r["avail_rep"],
                            "rev_rep": _num(r.get("TOTAL_OPERATE_INCOME")), "np_rep": npv, "dedt_rep": derived,
                        })

        inc_df = pd.DataFrame()
        if not inc.empty and "REPORT_DATE" in inc.columns:
            r = inc[inc["ts_code"].isin(codes_set)].copy()
            if len(r):
                r["end_date"] = _ymd(r["REPORT_DATE"])
                r["eff_inc"] = self._em_eff(_ymd(r["NOTICE_DATE"]), r["_seen_date"], r.get("_actual_seen_date"))
                r = r[(r["end_date"] != "") & (r["end_date"] <= d_target) & (r["eff_inc"] < d_target)].copy()
                if len(r):
                    r["avail_inc"] = _map_avail_date(r["eff_inc"].to_numpy(), cal_pit)
                    r = r[r["avail_inc"] <= d_target].copy()
                    if len(r):
                        r = _first_seen(r, ["ts_code", "end_date"])
                        inc_df = pd.DataFrame({
                            "ts_code": r["ts_code"], "end_date": r["end_date"],
                            "eff_inc": r["eff_inc"], "avail_inc": r["avail_inc"],
                            "rev_inc": _num(r.get("TOTAL_OPERATE_INCOME")),
                            "np_inc": _num(r.get("PARENT_NETPROFIT")),
                            "dedt_inc": _num(r.get("DEDUCT_PARENT_NETPROFIT")),
                        })

        if rep_df.empty and inc_df.empty:
            return pd.DataFrame(columns=cols)
        if not rep_df.empty and not inc_df.empty:
            m = rep_df.merge(inc_df, on=["ts_code", "end_date"], how="outer")
        elif not rep_df.empty:
            m = rep_df.copy()
        else:
            m = inc_df.copy()

        for c in ("eff_rep", "avail_rep", "eff_inc", "avail_inc"):
            if c not in m.columns:
                m[c] = ""
            m[c] = m[c].fillna("").astype(object)
        for c in ("rev_rep", "np_rep", "dedt_rep", "rev_inc", "np_inc", "dedt_inc"):
            if c not in m.columns:
                m[c] = np.nan

        use_inc_dedt = m["dedt_inc"].notna()
        use_rep_dedt = (~use_inc_dedt) & m["dedt_rep"].notna()
        has_inc = m["eff_inc"] != ""
        has_rep = m["eff_rep"] != ""

        rev = np.where(
            use_inc_dedt,
            m["rev_inc"].where(m["rev_inc"].notna(), m["rev_rep"]),
            np.where(
                use_rep_dedt,
                m["rev_rep"].where(m["rev_rep"].notna(), m["rev_inc"]),
                m["rev_inc"].where(m["rev_inc"].notna(), m["rev_rep"]),
            ),
        )
        npv = np.where(
            use_inc_dedt,
            m["np_inc"].where(m["np_inc"].notna(), m["np_rep"]),
            np.where(
                use_rep_dedt,
                m["np_rep"].where(m["np_rep"].notna(), m["np_inc"]),
                m["np_inc"].where(m["np_inc"].notna(), m["np_rep"]),
            ),
        )
        dedt = m["dedt_inc"].where(use_inc_dedt, m["dedt_rep"])
        label = np.where(use_inc_dedt, "em_deduct_parent_netprofit", np.where(use_rep_dedt, "em_deduct_eps_derived", ""))
        eff = np.where(
            use_inc_dedt,
            m["eff_inc"],
            np.where(
                use_rep_dedt,
                m["eff_rep"],
                np.where(has_inc & (m["rev_inc"].notna() | m["np_inc"].notna() | (~has_rep)), m["eff_inc"], m["eff_rep"]),
            ),
        )
        avail = np.where(
            use_inc_dedt,
            m["avail_inc"],
            np.where(
                use_rep_dedt,
                m["avail_rep"],
                np.where(has_inc & (m["rev_inc"].notna() | m["np_inc"].notna() | (~has_rep)), m["avail_inc"], m["avail_rep"]),
            ),
        )
        dedt_avail = np.where(use_inc_dedt | use_rep_dedt, avail, "99999999")
        return pd.DataFrame({
            "ts_code": m["ts_code"].to_numpy(dtype=object),
            "end_date": m["end_date"].to_numpy(dtype=object),
            "eff_ann": eff.astype(object),
            "avail": avail.astype(object),
            "total_revenue": rev.astype(np.float64),
            "n_income_attr_p": npv.astype(np.float64),
            "dedt_cum": dedt.to_numpy(dtype=np.float64),
            "dedt_label": label.astype(object),
            "dedt_avail": dedt_avail.astype(object),
        })

    def em_express(self, d_target: str | None = None) -> pd.DataFrame:
        if d_target is None and "em_ex" in self._cache:
            return self._cache["em_ex"]  # type: ignore[return-value]
        x = self._em_raw("express")
        cols = ["ts_code", "end_date", "ann_date", "revenue", "n_income"]
        if x.empty or "REPORT_DATE" not in x.columns:
            out = pd.DataFrame({c: pd.Series(dtype=object) for c in cols})
        else:
            x = x.copy()
            x["end_date"] = _ymd(x["REPORT_DATE"])
            ann = _ymd(x["UPDATE_DATE"]) if "UPDATE_DATE" in x.columns else _ymd(x["NOTICE_DATE"])
            x["ann_date"] = self._em_eff(ann, x["_seen_date"], x.get("_actual_seen_date"))
            x = x[x["end_date"] != ""]
            if d_target is not None:
                x = x[(x["end_date"] <= d_target) & (x["ann_date"] < d_target)]
            x = _first_seen(x, ["ts_code", "end_date"])
            out = pd.DataFrame({"ts_code": x["ts_code"], "end_date": x["end_date"], "ann_date": x["ann_date"],
                                "revenue": _num(x.get("TOTAL_OPERATE_INCOME")), "n_income": _num(x.get("PARENT_NETPROFIT"))})
        if d_target is None:
            self._cache["em_ex"] = out
        return out

    def em_forecast(self, d_target: str | None = None) -> pd.DataFrame:
        """Parent-net-profit forecast items only; np_cum = midpoint (yuan)."""
        if d_target is None and "em_fc" in self._cache:
            return self._cache["em_fc"]  # type: ignore[return-value]
        f = self._em_raw("forecast")
        cols = ["ts_code", "end_date", "notice", "ann_date", "np_cum"]
        if f.empty or "REPORT_DATE" not in f.columns:
            out = pd.DataFrame({c: pd.Series(dtype=object) for c in cols})
        else:
            f = f.copy()
            name = _s(f["PREDICT_FINANCE"]) if "PREDICT_FINANCE" in f.columns else pd.Series([""] * len(f), index=f.index, dtype=object)
            code = _s(f["PREDICT_FINANCE_CODE"]) if "PREDICT_FINANCE_CODE" in f.columns else pd.Series([""] * len(f), index=f.index, dtype=object)
            f = f[(name == FC_PARENT_NP_NAME) | (code == FC_PARENT_NP_CODE)].copy()
            f["end_date"] = _ymd(f["REPORT_DATE"])
            f["notice"] = _ymd(f["NOTICE_DATE"])
            f = f[(f["end_date"] != "") & (f["notice"] != "")]
            f["ann_date"] = self._em_eff(f["notice"], f["_seen_date"], f.get("_actual_seen_date"))
            if d_target is not None:
                f = f[f["ann_date"] < d_target]
            f = _first_seen(f, ["ts_code", "end_date", "notice"])
            lo, up = _num(f["PREDICT_AMT_LOWER"]), _num(f["PREDICT_AMT_UPPER"])
            out = pd.DataFrame({"ts_code": f["ts_code"], "end_date": f["end_date"], "notice": f["notice"], "ann_date": f["ann_date"],
                                "np_cum": (lo + up) * 0.5})
            out = out.dropna(subset=["np_cum"]).reset_index(drop=True)
        if d_target is None:
            self._cache["em_fc"] = out
        return out

    def em_cashflow(self, d_target: str | None = None) -> pd.DataFrame:
        if d_target is None and "em_cf" in self._cache:
            return self._cache["em_cf"]  # type: ignore[return-value]
        c = self._em_raw("cashflow")
        if c.empty or "REPORT_DATE" not in c.columns:
            out = pd.DataFrame({k: pd.Series(dtype=object) for k in ["ts_code", "end_date", "eff_ann", "ocf_cum"]})
        else:
            c = c.copy()
            c["end_date"] = _ymd(c["REPORT_DATE"])
            c["eff"] = self._em_eff(_ymd(c["NOTICE_DATE"]), c["_seen_date"], c.get("_actual_seen_date"))
            c = c[c["end_date"] != ""]
            if d_target is not None:
                c = c[(c["end_date"] <= d_target) & (c["eff"] < d_target)]
            c = _first_seen(c, ["ts_code", "end_date"])
            out = pd.DataFrame({"ts_code": c["ts_code"], "end_date": c["end_date"], "eff_ann": c["eff"],
                                "ocf_cum": _num(c.get("NETCASH_OPERATE"))})
        if d_target is None:
            self._cache["em_cf"] = out
        return out

    # ---- industry --------------------------------------------------------------------------------
    def industry_map(self, d_target: str | None = None) -> dict[str, str]:
        """PIT SW L1 industry map: when ``d_target`` is given, keep only intervals covering ``d_target``
        (in_date <= d_target and out_date missing or d_target < out_date), returning '' for unknowns."""
        cache_key = f"ind_{d_target or 'latest'}"
        if cache_key in self._cache:
            return self._cache[cache_key]  # type: ignore[return-value]
        m: dict[str, str] = {}
        if self.sw_path.exists():
            sw = pd.read_parquet(self.sw_path)
            if not sw.empty and "l1_name" in sw.columns:
                sw = _mandate_filter(sw.copy())
                sw = sw[sw["l1_name"].notna()].copy()
                for c in ("in_date", "out_date"):
                    sw[c] = _ymd(sw[c]) if c in sw.columns else ""
                if d_target is not None:
                    d_target = str(d_target).replace("-", "")
                    in_ok = (sw["in_date"].str.len() == 8) & (sw["in_date"] <= d_target)
                    out_ok = (sw["out_date"] == "") | (d_target < sw["out_date"])
                    sw = sw[in_ok & out_ok].sort_values(["ts_code", "in_date"], kind="mergesort")
                    sw = sw.drop_duplicates("ts_code", keep="last")
                else:
                    sw["_cur"] = sw["out_date"] == ""
                    sw = sw.sort_values(["ts_code", "_cur", "in_date"], kind="mergesort").drop_duplicates("ts_code", keep="last")
                m = dict(zip(sw["ts_code"].astype(str), sw["l1_name"].astype(str)))
        self._cache[cache_key] = m
        return m

    # ---- market (mv / pb history for event-level floors) ------------------------------------------
    def _daily_basic(self, d: str) -> pd.DataFrame | None:
        if d in self._db_cache:
            return self._db_cache[d]
        fp = self.daily_basic_dir / f"daily_basic_{d}.parquet"
        out = None
        if fp.exists():
            out = pd.read_parquet(fp, columns=["ts_code", "total_mv", "pb"])
            out["ts_code"] = out["ts_code"].astype(str)
            out["trade_date"] = d
        self._db_cache[d] = out
        return out

    def _mv_pb_for(self, tm1_dates: np.ndarray, codes: list[str], d_target: str, total_mv_d, pb_d, mv_pb_hist) -> pd.DataFrame:
        frames = []
        if mv_pb_hist is not None and len(mv_pb_hist):
            h = mv_pb_hist[["ts_code", "trade_date", "total_mv", "pb"]].copy()
            h["trade_date"] = h["trade_date"].astype(str)
            frames.append(h)
        else:
            for d in sorted(set(str(x) for x in tm1_dates)):
                one = self._daily_basic(d)
                if one is not None:
                    frames.append(one[["ts_code", "trade_date", "total_mv", "pb"]])
        frames.append(pd.DataFrame({"ts_code": list(codes), "trade_date": d_target,
                                    "total_mv": np.asarray(total_mv_d, dtype=np.float64), "pb": np.asarray(pb_d, dtype=np.float64)}))
        return pd.concat(frames, ignore_index=True).drop_duplicates(["ts_code", "trade_date"], keep="last")

    # ---- R rows (income + fina_indicator + EM fill) -----------------------------------------------
    def _seed_R(self, d_target: str, cal_pit: np.ndarray, codes_set: set[str]) -> pd.DataFrame:
        inc = self.inc_raw
        inc = inc[inc["ts_code"].isin(codes_set)].copy()
        if "report_type" in inc.columns:
            rt = inc["report_type"].astype("string").fillna("")
            inc = inc[(rt == "1") | (rt == "")]
        inc["eff_ann"] = _eff_ann_two(inc["ann_date"], inc["f_ann_date"])
        inc = inc[(inc["eff_ann"] < d_target) & (inc["end_date"].str.len() == 8) & (inc["end_date"] <= d_target)]
        inc = inc.sort_values(["ts_code", "end_date", "update_flag", "eff_ann"], kind="mergesort").drop_duplicates(["ts_code", "end_date"], keep="first").copy()
        inc["avail"] = _map_avail_date(inc["eff_ann"].to_numpy(), cal_pit)
        inc = inc[inc["avail"] <= d_target].copy()

        fi = self.fi_raw
        fi = fi[fi["ts_code"].isin(codes_set)].dropna(subset=["profit_dedt"]).copy()
        fi = fi[(fi["ann_date"].str.len() == 8) & (fi["ann_date"] < d_target) & (fi["end_date"].str.len() == 8) & (fi["end_date"] <= d_target)]
        fi = fi.sort_values(["ts_code", "end_date", "update_flag", "ann_date"], kind="mergesort").drop_duplicates(["ts_code", "end_date"], keep="first").copy()
        fi["avail_dt"] = _map_avail_date(fi["ann_date"].to_numpy(), cal_pit)
        fi = fi[fi["avail_dt"] <= d_target]

        R = inc.merge(fi[["ts_code", "end_date", "profit_dedt", "avail_dt"]], on=["ts_code", "end_date"], how="left")
        R["profit_dedt"] = _num(R["profit_dedt"])
        R["n_income_attr_p"] = _num(R["n_income_attr_p"])
        R["total_revenue"] = _num(R["total_revenue"])
        okd = R["avail_dt"].notna() & (R["avail_dt"].fillna("99999999") <= d_target) & np.isfinite(R["profit_dedt"])
        av_inc = R["avail"].astype(str).to_numpy(dtype=object)
        av_fi = R["avail_dt"].fillna("").astype(str).to_numpy(dtype=object)
        R["dedt_avail"] = np.where(okd, np.where(av_inc >= av_fi, av_inc, av_fi), "99999999").astype(object)
        R["dedt_cum"] = np.where(okd, R["profit_dedt"], np.nan)
        R["dedt_label"] = np.where(np.isfinite(R["dedt_cum"]), "seed_profit_dedt", "")
        return R[["ts_code", "end_date", "eff_ann", "avail", "total_revenue", "n_income_attr_p",
                  "dedt_cum", "dedt_label", "dedt_avail"]]

    # ---- main --------------------------------------------------------------------------------------
    def pit_fundamentals(self, d_target: str, codes: list[str], cal_pit, total_mv_d, pb_d,
                         ind_names=None, mv_pb_hist: pd.DataFrame | None = None) -> dict[str, np.ndarray]:
        """Strict-PIT fundamentals at ``d_target`` aligned to ``codes``.

        total_mv_d is in 万元 (x1e4 applied here, same as aegis); pb_d raw PB.  ``cal_pit`` is the
        sorted array of open dates <= d_target with ``cal_pit[-1] == d_target``.  ``ind_names``:
        optional iterable of SW1 names aligned with codes (else PIT seed sw_member is used).  Event-level
        MV/PB history (for aegis floors and sue_mv) comes from ``mv_pb_hist`` (ts_code, trade_date,
        total_mv, pb) or the pit_market/v2 daily_basic files; missing -> NaN (aegis behaviour)."""
        cal_pit = np.asarray([str(x) for x in cal_pit], dtype="U8")
        d_target = str(d_target)
        assert str(cal_pit[-1]) == d_target, "cal_pit[-1] must equal d_target"
        codes = list(codes)
        n = len(codes)
        total_mv_d = np.asarray(total_mv_d, dtype=np.float64)
        pb_d = np.asarray(pb_d, dtype=np.float64)
        allowed = mg.is_allowed_mandate_board(codes)
        codes_set = {c for c, a in zip(codes, allowed) if a}
        t_pos = len(cal_pit) - 1
        cidx = pd.Index(codes)

        # ---------------- R rows (causal seed + causal EM fill)
        R_seed = self._seed_R(d_target, cal_pit, codes_set)
        seed_inc_keys = set(zip(R_seed["ts_code"], R_seed["end_date"]))
        R_em = self._em_R_as_of(d_target, cal_pit, codes_set)
        if len(R_em) and seed_inc_keys:
            keys = pd.MultiIndex.from_arrays([R_em["ts_code"], R_em["end_date"]])
            R_em = R_em[~keys.isin(list(seed_inc_keys))].copy()
        n_em_R = len(R_em)
        R = pd.concat([R_seed, R_em], ignore_index=True) if len(R_em) else R_seed.copy()
        R["dedt_cum"] = _num(R["dedt_cum"])
        R["core_cum"] = np.where(np.isfinite(R["dedt_cum"]), R["dedt_cum"], R["n_income_attr_p"])
        R["y"] = R["end_date"].str[:4].astype(int)
        R["q"] = R["end_date"].str[4:6].astype(int) // 3
        R = R.drop_duplicates(["ts_code", "y", "q"], keep="first")
        KR = R.set_index(["ts_code", "y", "q"], drop=False)
        KR.index.names = ["_c", "_y", "_q"]

        # Strict dedt_ttm (NO mixing of parent NP!) vs fallback core_ttm_fb (for QARP only)
        dedt_ttm_strict, dedt_ttm_strict_ly = _ttm_from_cum(KR, "dedt_cum", avail_col="dedt_avail", cap_override=d_target)
        core_ttm_fb, _core_ttm_fb_ly = _ttm_from_cum(KR, "core_cum")
        KR["np_ttm"], KR["np_ttm_ly"] = _ttm_from_cum(KR, "n_income_attr_p")
        KR["rev_ttm"], KR["rev_ttm_ly"] = _ttm_from_cum(KR, "total_revenue")

        # Strict TTM source across ALL contributing quarters (curr; and (y-1,4), (y-1,q) when q != 4)
        idx_kr = KR.index
        lv0, lv1 = idx_kr.get_level_values(0), idx_kr.get_level_values(1)
        q_kr = KR["q"].to_numpy()
        lbl_curr = np.where(
            np.isfinite(KR["dedt_cum"].to_numpy(dtype=np.float64)) & (KR["dedt_avail"].to_numpy() <= d_target),
            KR["dedt_label"].fillna("").to_numpy(dtype=object),
            "",
        )
        key_fy1 = pd.MultiIndex.from_arrays([lv0, lv1 - 1, np.full(len(idx_kr), 4)])
        key_ly = pd.MultiIndex.from_arrays([lv0, lv1 - 1, q_kr])
        lbl_fy1 = np.where(
            KR["dedt_avail"].reindex(key_fy1).fillna("99999999").to_numpy() <= d_target,
            KR["dedt_label"].reindex(key_fy1).fillna("").to_numpy(dtype=object),
            "",
        )
        lbl_ly = np.where(
            KR["dedt_avail"].reindex(key_ly).fillna("99999999").to_numpy() <= d_target,
            KR["dedt_label"].reindex(key_ly).fillna("").to_numpy(dtype=object),
            "",
        )
        is_q4_kr = q_kr == 4
        has_eps = (lbl_curr == "em_deduct_eps_derived") | (~is_q4_kr & ((lbl_fy1 == "em_deduct_eps_derived") | (lbl_ly == "em_deduct_eps_derived")))
        has_emp = (lbl_curr == "em_deduct_parent_netprofit") | (~is_q4_kr & ((lbl_fy1 == "em_deduct_parent_netprofit") | (lbl_ly == "em_deduct_parent_netprofit")))
        strict_ok = np.isfinite(dedt_ttm_strict)
        fb_ok = np.isfinite(core_ttm_fb)
        KR["dedt_ttm"] = dedt_ttm_strict
        KR["dedt_ttm_ly"] = dedt_ttm_strict_ly
        KR["core_ttm_fb"] = core_ttm_fb
        KR["dsrc"] = np.where(
            strict_ok,
            np.where(has_eps, "em_deduct_eps_derived", np.where(has_emp, "em_deduct_parent_netprofit", "seed_profit_dedt")),
            np.where(fb_ok, "fallback_parent_netprofit", "missing"),
        ).astype(object)

        K_reg = KR.reset_index(drop=True)
        lag_days = (pd.to_datetime(K_reg["eff_ann"]) - pd.to_datetime(K_reg["end_date"])).dt.days
        K_reg = K_reg[(lag_days >= 0) & (lag_days <= 180) & (K_reg["avail"] >= "20170103")].copy()
        kr = _select_latest(K_reg, t_pos, cal_pit).drop_duplicates("ts_code", keep="last").set_index("ts_code")

        # ---------------- events (R + express + forecast)
        fc_s = self.fc_raw
        fc = fc_s[fc_s["ts_code"].isin(codes_set)].dropna(subset=["net_profit_min", "net_profit_max", "ann_date"]).copy()
        fc = fc[(fc["ann_date"].str.len() == 8) & (fc["ann_date"] < d_target) & (fc["ann_date"] >= "20170101") & (fc["end_date"].str.len() == 8)]
        fc = fc.sort_values(["ts_code", "end_date", "ann_date", "update_flag"], kind="mergesort").drop_duplicates(["ts_code", "end_date", "ann_date"], keep="first")
        fc["avail"] = _map_avail_date(fc["ann_date"].to_numpy(), cal_pit)
        fc = fc[fc["avail"] <= d_target].copy()
        fc["np_cum"] = (_num(fc["net_profit_min"]) + _num(fc["net_profit_max"])) * 0.5 * 1e4
        fc["eff_ann"] = fc["ann_date"]
        fc = fc[["ts_code", "end_date", "eff_ann", "avail", "np_cum"]]
        # EM forecasts: only announcements newer than anything the VISIBLE seed has for that (code, period)
        emf = self.em_forecast(d_target=d_target)
        n_em_fc = 0
        if len(emf):
            emf = emf[emf["ts_code"].isin(codes_set) & (emf["ann_date"] < d_target) & (emf["ann_date"] >= "20170101")].copy()
            if len(emf):
                emf["avail"] = _map_avail_date(emf["ann_date"].to_numpy(), cal_pit)
                emf = emf[emf["avail"] <= d_target].copy()
            if len(emf):
                seed_max = fc.groupby(["ts_code", "end_date"])["eff_ann"].max() if len(fc) else pd.Series(dtype=object)
                sm = pd.Series(list(zip(emf["ts_code"], emf["end_date"])), index=emf.index).map(seed_max).astype(object)
                sm = sm.where(sm.notna(), "")
                emf = emf[emf["notice"] > sm]
                emf = emf.sort_values(["ts_code", "end_date", "ann_date"], kind="mergesort").drop_duplicates(["ts_code", "end_date", "ann_date"], keep="first")
                n_em_fc = len(emf)
                emf = emf.rename(columns={"ann_date": "eff_ann"})[["ts_code", "end_date", "eff_ann", "avail", "np_cum"]]
                fc = pd.concat([fc, emf], ignore_index=True)
        fc["rev_cum"] = np.nan
        fc["src"] = "F"

        ex_s = self.ex_raw
        ex = ex_s[ex_s["ts_code"].isin(codes_set)].dropna(subset=["n_income", "ann_date"]).copy()
        ex = ex[(ex["ann_date"].str.len() == 8) & (ex["ann_date"] < d_target) & (ex["ann_date"] >= "20170101") & (ex["end_date"].str.len() == 8)]
        ex = ex.sort_values(["ts_code", "end_date", "ann_date", "update_flag"], kind="mergesort").drop_duplicates(["ts_code", "end_date", "ann_date"], keep="first")
        ex["avail"] = _map_avail_date(ex["ann_date"].to_numpy(), cal_pit)
        ex = ex[ex["avail"] <= d_target].copy()
        ex["np_cum"] = _num(ex["n_income"])
        ex["rev_cum"] = _num(ex["revenue"])
        ex["eff_ann"] = ex["ann_date"]
        skeys = set(zip(ex["ts_code"], ex["end_date"]))
        ex = ex[["ts_code", "end_date", "eff_ann", "avail", "np_cum", "rev_cum"]]
        exm = self.em_express(d_target=d_target)
        if len(exm):
            exm = exm[exm["ts_code"].isin(codes_set) & (exm["ann_date"] < d_target) & (exm["ann_date"] >= "20170101") & exm["n_income"].notna()].copy()
            if len(exm):
                exm["avail"] = _map_avail_date(exm["ann_date"].to_numpy(), cal_pit)
                exm = exm[exm["avail"] <= d_target].copy()
            if len(exm):
                exm = exm[np.array([(a, b) not in skeys for a, b in zip(exm["ts_code"], exm["end_date"])], dtype=bool)]
                exm = exm.assign(np_cum=exm["n_income"], rev_cum=exm["revenue"], eff_ann=exm["ann_date"])[["ts_code", "end_date", "eff_ann", "avail", "np_cum", "rev_cum"]]
                ex = pd.concat([ex, exm], ignore_index=True)
        ex["src"] = "X"

        r_ev = R[R["eff_ann"] >= "20170101"][["ts_code", "end_date", "eff_ann", "avail", "dedt_cum", "dedt_avail", "n_income_attr_p", "total_revenue"]].copy()
        r_ev["np_cum"] = r_ev["n_income_attr_p"]
        r_ev["rev_cum"] = r_ev["total_revenue"]
        r_ev["src"] = "R"
        ev = pd.concat(
            [
                r_ev[["ts_code", "end_date", "src", "eff_ann", "avail", "np_cum", "rev_cum", "dedt_cum", "dedt_avail"]],
                ex[["ts_code", "end_date", "src", "eff_ann", "avail", "np_cum", "rev_cum"]].assign(dedt_cum=np.nan, dedt_avail="99999999"),
                fc[["ts_code", "end_date", "src", "eff_ann", "avail", "np_cum", "rev_cum"]].assign(dedt_cum=np.nan, dedt_avail="99999999"),
            ],
            ignore_index=True,
        )
        ev = ev[ev["ts_code"].isin(codes_set)].copy()
        lag_ev = (pd.to_datetime(ev["eff_ann"]) - pd.to_datetime(ev["end_date"])).dt.days
        lag_min = np.where(ev["src"] == "F", FC_PRE_END_DAYS, 0)
        ev = ev[(lag_ev >= lag_min) & (lag_ev <= MAX_LAG_DAYS) & (ev["avail"] >= "20170103") & (ev["avail"] <= d_target)].copy()
        ev["pri"] = ev["src"].map({"R": 0, "X": 1, "F": 2})
        ev = ev.sort_values(["ts_code", "end_date", "avail", "pri"], kind="mergesort").drop_duplicates(["ts_code", "end_date", "avail"], keep="first").reset_index(drop=True)

        # K_ev selection BEFORE the heavy per-row math (rows are independent -> identical result)
        ev = _select_latest(ev, t_pos, cal_pit)

        if len(ev):
            tm1_idx = np.maximum(ev["apos"].to_numpy() - 1, 0)
            ev["Tm1"] = cal_pit[tm1_idx]
            B_mv = self._mv_pb_for(ev["Tm1"].unique(), codes, d_target, total_mv_d, pb_d, mv_pb_hist).rename(columns={"trade_date": "Tm1"})
            ev = ev.merge(B_mv, on=["ts_code", "Tm1"], how="left")
            ev["mv"] = _num(ev["total_mv"]) * 1e4
            ev["pb_f"] = _num(ev["pb"])
            with np.errstate(divide="ignore", invalid="ignore"):
                ev["be"] = np.where(ev["pb_f"] > 0, ev["mv"] / ev["pb_f"], np.nan)
            fl_arr = np.where(ev["mv"].notna(), np.fmax(ev["mv"].to_numpy() * FLOOR_MV, np.nan_to_num(ev["be"].to_numpy() * FLOOR_NA)), np.nan)
            ev["y"] = ev["end_date"].str[:4].astype(int)
            ev["q"] = ev["end_date"].str[4:6].astype(int) // 3
            ev_cap = ev["avail"].to_numpy()
            use_dt = (ev["src"] == "R") & ev["dedt_cum"].notna() & (ev["dedt_avail"].to_numpy() <= ev_cap)
            ev_codes = ev["ts_code"].to_numpy()
            ev_y, ev_q = ev["y"].to_numpy(), ev["q"].to_numpy()

            def get_cum_at(y_t, q_t, col_name):
                key = pd.MultiIndex.from_arrays([ev_codes, y_t, q_t])
                v = KR[col_name].reindex(key).to_numpy(dtype=np.float64)
                av_col = "dedt_avail" if col_name == "dedt_cum" else "avail"
                av = KR[av_col].reindex(key).fillna("99999999").to_numpy()
                return np.where(av <= ev_cap, v, np.nan)

            def get_sq_hist(k: int, kind: str) -> np.ndarray:
                yk, qk = _q_back(ev_y, ev_q, k)
                col = "core_cum" if kind == "dt" else ("n_income_attr_p" if kind == "np" else "total_revenue")
                if k == 0:
                    src = {"dt": "dedt_cum", "np": "np_cum", "rev": "rev_cum"}[kind]
                    c0 = ev[src].to_numpy(dtype=np.float64)
                    if kind == "dt":
                        c0 = np.where(ev["dedt_avail"].to_numpy() <= ev_cap, c0, np.nan)
                else:
                    c0 = get_cum_at(yk, qk, col)
                yp, qp = _q_back(yk, qk, 1)
                c_prev = get_cum_at(yp, qp, col)
                return np.where(qk == 1, c0, c0 - c_prev)

            S = []
            for k in range(17):
                s_dt, s_np = get_sq_hist(k, "dt"), get_sq_hist(k, "np")
                S.append(np.where(use_dt, np.where(np.isfinite(s_dt), s_dt, s_np), s_np))

            def den_vec(v):
                d = np.fmax(np.abs(v), np.where(np.isfinite(fl_arr), fl_arr, 0.0))
                return np.where(np.isfinite(v) & (d > 0), d, np.nan)

            def yoy_vec(a, b):
                with np.errstate(divide="ignore", invalid="ignore"):
                    return np.where(np.isfinite(a) & np.isfinite(b), (a - b) / den_vec(b), np.nan)

            yoy0, yoy1 = yoy_vec(S[0], S[4]), yoy_vec(S[1], S[5])
            jump = yoy0 - yoy1
            ttm = lambda k: S[k] + S[k + 1] + S[k + 2] + S[k + 3]  # noqa: E731
            is_q1, is_q4 = ev_q == 1, ev_q == 4
            jump = np.where(is_q1, yoy0 - yoy_vec(ttm(1), ttm(5)), jump)
            fy0, fy0_l = ttm(0), ttm(4)
            n9, n9_l = S[1] + S[2] + S[3], S[5] + S[6] + S[7]
            jump_final = np.where(is_q4, yoy_vec(fy0, fy0_l) - yoy_vec(n9, n9_l), jump)

            diffs = np.vstack([S[k] - S[k + 4] for k in range(1, 9)])
            n_valid = np.sum(np.isfinite(diffs), axis=0)
            with warnings.catch_warnings(), np.errstate(invalid="ignore"):
                warnings.simplefilter("ignore", RuntimeWarning)
                std_sq = np.nanstd(diffs, axis=0, ddof=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                sue_sq = np.where((n_valid >= 6) & (std_sq > 0) & np.isfinite(S[0] - S[4]), (S[0] - S[4]) / std_sq, np.nan)
            diffs_ttm = np.vstack([ttm(k) - ttm(k + 4) for k in range(1, 9)])
            n_valid_t = np.sum(np.isfinite(diffs_ttm), axis=0)
            with warnings.catch_warnings(), np.errstate(invalid="ignore"):
                warnings.simplefilter("ignore", RuntimeWarning)
                std_ttm = np.nanstd(diffs_ttm, axis=0, ddof=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                sue_fy = np.where((n_valid_t >= 6) & (std_ttm > 0) & np.isfinite(fy0 - fy0_l), (fy0 - fy0_l) / std_ttm, np.nan)
                sue_final = np.where(is_q4, sue_fy, sue_sq)
                mv_e = ev["mv"].to_numpy()
                sue_mv = np.where(np.isfinite(S[0] - S[4]) & (mv_e > 0), (S[0] - S[4]) / mv_e, np.nan)

            RV = [get_sq_hist(k, "rev") for k in range(8)]
            rjump = yoy_vec(RV[0], RV[4]) - yoy_vec(RV[1], RV[5])
            rfy0, rfy0_l = RV[0] + RV[1] + RV[2] + RV[3], RV[4] + RV[5] + RV[6] + RV[7]
            rn9, rn9_l = RV[1] + RV[2] + RV[3], RV[5] + RV[6] + RV[7]
            ev["jump"] = jump_final
            ev["sue"] = sue_final
            ev["sue_mv"] = sue_mv
            ev["rev_jump"] = np.where(is_q4, yoy_vec(rfy0, rfy0_l) - yoy_vec(rn9, rn9_l), rjump)
            mv_hist_cov = float(np.isfinite(mv_e).mean())
        else:
            for c in ("jump", "sue", "sue_mv", "rev_jump"):
                ev[c] = np.nan
            mv_hist_cov = float("nan")
        ke_ok = ev  # already windowed + sorted by apos
        ke_last = ke_ok.drop_duplicates("ts_code", keep="last").set_index("ts_code")

        # ---------------- assemble outputs
        mv_yuan = total_mv_d * 1e4
        with np.errstate(divide="ignore", invalid="ignore"):
            be_yuan = np.where(pb_d > 0, mv_yuan / pb_d, np.nan)

        def from_kr(col: str, numeric: bool = True):
            s = kr[col].reindex(cidx)
            return s.to_numpy(dtype=np.float64) if numeric else s.to_numpy(dtype=object)

        dedt_ttm = from_kr("dedt_ttm")
        core_ttm_for_val = from_kr("core_ttm_fb")
        dedt_for_qarp = np.where(np.isfinite(dedt_ttm), dedt_ttm, core_ttm_for_val)
        np_ttm, rev_ttm = from_kr("np_ttm"), from_kr("rev_ttm")
        dedt_ttm_ly, rev_ttm_ly = from_kr("dedt_ttm_ly"), from_kr("rev_ttm_ly")
        with np.errstate(divide="ignore", invalid="ignore"):
            ep_dedt = np.where(mv_yuan > 0, dedt_for_qarp / mv_yuan, np.nan)
            roe_dedt = np.where(be_yuan > 0, dedt_for_qarp / be_yuan, np.nan)
            floor_d = np.fmax(FLOOR_MV * np.nan_to_num(mv_yuan), FLOOR_NA * np.nan_to_num(be_yuan))
            den = np.fmax(np.abs(dedt_ttm_ly), floor_d)
            dedt_yoy = np.where(np.isfinite(dedt_ttm) & np.isfinite(dedt_ttm_ly) & (den > 0), (dedt_ttm - dedt_ttm_ly) / den, np.nan)
            rev_yoy = np.where(np.isfinite(rev_ttm) & np.isfinite(rev_ttm_ly) & (rev_ttm_ly > 0), rev_ttm / rev_ttm_ly - 1.0, np.nan)
        src_row = from_kr("dsrc", numeric=False)
        dedt_source = np.where(pd.notna(src_row), src_row, "missing").astype(object)
        latest_report_date = kr["end_date"].reindex(cidx).fillna("").to_numpy(dtype=object)
        latest_ann_date = kr["eff_ann"].reindex(cidx).fillna("").to_numpy(dtype=object)

        ke_rx = ke_ok[ke_ok["src"].isin(["R", "X"]) & ke_ok["rev_jump"].notna()].drop_duplicates("ts_code", keep="last").set_index("ts_code")
        res = {
            "ep_dedt": ep_dedt,
            "roe_dedt": roe_dedt,
            "jump": ke_last["jump"].reindex(cidx).to_numpy(dtype=np.float64),
            "rev_jump": ke_rx["rev_jump"].reindex(cidx).to_numpy(dtype=np.float64),
            "sue": ke_last["sue"].reindex(cidx).to_numpy(dtype=np.float64),
            "sue_mv": ke_last["sue_mv"].reindex(cidx).to_numpy(dtype=np.float64),
            "ev_age": (t_pos - ke_last["apos"].reindex(cidx)).to_numpy(dtype=np.float64),
            "dedt_ttm": dedt_ttm,
            "np_ttm": np_ttm,
            "rev_ttm": rev_ttm,
            "dedt_yoy": dedt_yoy,
            "rev_yoy": rev_yoy,
            "dedt_source": dedt_source,
            "latest_report_date": latest_report_date,
            "latest_ann_date": latest_ann_date,
            "mv_yuan": mv_yuan,
            "be_yuan": be_yuan,
        }
        # ---------------- OCF
        ocf_ttm, ocf_app = self._ocf(d_target, cal_pit, codes, codes_set, ind_names, t_pos)
        res["ocf_ttm"], res["ocf_applicable"] = ocf_ttm, ocf_app
        # mandate: codes outside the mandate never carry values
        bad = ~allowed
        res = {k: np.array(v, copy=True) for k, v in res.items()}
        if bad.any():
            for k, v in res.items():
                if v.dtype == object:
                    v[bad] = "missing" if k == "dedt_source" else ""
                elif v.dtype == bool:
                    v[bad] = False
                else:
                    v[bad] = np.nan
        self.last_diag = {"n_em_R_rows_used": int(n_em_R), "n_em_forecast_rows": int(n_em_fc),
                          "n_events_window": int(len(ev)), "mv_hist_cov_events": mv_hist_cov, "n_codes_not_in_mandate": int(bad.sum())}
        return res

    def _ocf(self, d_target, cal_pit, codes, codes_set, ind_names, t_pos):
        n = len(codes)
        cidx = pd.Index(codes)
        if ind_names is not None:
            inds = np.asarray(list(ind_names), dtype=object)
        else:
            im = self.industry_map(d_target)
            inds = np.array([im.get(c, "") for c in codes], dtype=object)
        applicable = np.array([x not in NONFIN_OCF_EXCLUDED_L1 for x in inds], dtype=bool)
        applicable &= mg.is_allowed_mandate_board(codes)

        cf = self.cf_raw
        cf = cf[cf["ts_code"].isin(codes_set)].dropna(subset=["n_cashflow_act"]).copy()
        if "report_type" in cf.columns:
            rt = cf["report_type"].astype("string").fillna("")
            cf = cf[(rt == "1") | (rt == "")]
        cf["eff_ann"] = _eff_ann_two(cf["ann_date"], cf["f_ann_date"])
        cf = cf[(cf["eff_ann"] < d_target) & (cf["end_date"].str.len() == 8) & (cf["end_date"] <= d_target)]
        cf = cf.sort_values(["ts_code", "end_date", "update_flag", "eff_ann"], kind="mergesort").drop_duplicates(["ts_code", "end_date"], keep="first")
        cf["avail"] = _map_avail_date(cf["eff_ann"].to_numpy(), cal_pit)
        cf = cf[cf["avail"] <= d_target].copy()
        seed_keys = set(zip(cf["ts_code"], cf["end_date"]))
        cf = cf.assign(ocf_cum=_num(cf["n_cashflow_act"]))[["ts_code", "end_date", "eff_ann", "avail", "ocf_cum"]]
        em = self.em_cashflow(d_target=d_target)
        if len(em):
            em = em[em["ts_code"].isin(codes_set) & (em["eff_ann"] < d_target) & (em["end_date"] <= d_target) & em["ocf_cum"].notna()].copy()
            if len(em):
                em["avail"] = _map_avail_date(em["eff_ann"].to_numpy(), cal_pit)
                em = em[em["avail"] <= d_target].copy()
            if len(em):
                em = em[np.array([(a, b) not in seed_keys for a, b in zip(em["ts_code"], em["end_date"])], dtype=bool)]
                cf = pd.concat([cf, em[["ts_code", "end_date", "eff_ann", "avail", "ocf_cum"]]], ignore_index=True)
        out = np.full(n, np.nan)
        if len(cf):
            cf["y"] = cf["end_date"].str[:4].astype(int)
            cf["q"] = cf["end_date"].str[4:6].astype(int) // 3
            cf = cf.drop_duplicates(["ts_code", "y", "q"], keep="first")
            KC = cf.set_index(["ts_code", "y", "q"], drop=False)
            KC.index.names = ["_c", "_y", "_q"]
            KC["ocf_ttm"], _ = _ttm_from_cum(KC, "ocf_cum")
            lag = (pd.to_datetime(KC["eff_ann"]) - pd.to_datetime(KC["end_date"])).dt.days
            Kc = KC.reset_index(drop=True)
            Kc = Kc[(lag.to_numpy() >= 0) & (lag.to_numpy() <= 180) & (Kc["avail"] >= "20170103")]
            last = _select_latest(Kc, t_pos, cal_pit).drop_duplicates("ts_code", keep="last").set_index("ts_code")
            out = last["ocf_ttm"].reindex(cidx).to_numpy(dtype=np.float64)
        out = np.where(applicable, out, np.nan)
        return out, applicable

    # ---- coverage -------------------------------------------------------------------------------------
    @staticmethod
    def coverage(result: dict, mask) -> dict:
        m = np.asarray(mask, dtype=bool)
        tot = int(m.sum())
        if tot == 0:
            return {k: float("nan") for k in ("qarp_real_dedt_cov", "acc_real_cov", "ocf_nonfin_cov", "rev_ttm_cov", "dedt_ttm_cov")} | {"n_mask": 0}
        fin = lambda k: np.isfinite(np.asarray(result[k], dtype=np.float64))  # noqa: E731
        real_src = np.isin(np.asarray(result["dedt_source"], dtype=object), REAL_DEDT_SOURCES)
        app = m & np.asarray(result["ocf_applicable"], dtype=bool)
        return {
            "n_mask": tot,
            "qarp_real_dedt_cov": float((m & fin("ep_dedt") & real_src).sum() / tot),
            "acc_real_cov": float((m & (fin("jump") | fin("sue"))).sum() / tot),
            "ocf_nonfin_cov": float((app & fin("ocf_ttm")).sum() / app.sum()) if app.sum() else float("nan"),
            "rev_ttm_cov": float((m & fin("rev_ttm")).sum() / tot),
            "dedt_ttm_cov": float((m & fin("dedt_ttm")).sum() / tot),
        }


# ----------------------------------------------------------------------------- reconciliation
def _agree(a: np.ndarray, b: np.ndarray, rel: float = 0.005) -> tuple[float, int]:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() == 0:
        return float("nan"), 0
    d = np.abs(a[ok] - b[ok])
    thr = rel * np.maximum(np.abs(a[ok]), np.abs(b[ok]))
    return float(np.mean((d <= thr) | (d < 1.0))), int(ok.sum())


def reconcile_2026q3(ws=None, end_date: str = "20260930", store: ZeroPaidFinStoreV2 | None = None, em_version: str = "first") -> dict:
    """Seed first-version rows for ``end_date`` vs the EM forward capture (relative diff < 0.5%)."""
    st = store or ZeroPaidFinStoreV2(workspace=ws)
    inc = st.inc_raw
    inc = inc[inc["end_date"] == end_date]
    inc = inc.sort_values(["ts_code", "update_flag", "ann_date"], kind="mergesort").drop_duplicates("ts_code", keep="first")
    fi = st.fi_raw
    fi = fi[(fi["end_date"] == end_date)].dropna(subset=["profit_dedt"])
    fi = fi.sort_values(["ts_code", "update_flag", "ann_date"], kind="mergesort").drop_duplicates("ts_code", keep="first")
    em = st.em_R(em_version)
    em = em[em["end_date"] == end_date]
    j = inc.merge(em, on="ts_code", how="inner", suffixes=("_s", "_e"))
    out: dict = {"end_date": end_date, "em_version": em_version, "n_seed_codes": int(len(inc)), "n_em_codes": int(len(em)),
                 "n_overlap_codes": int(len(j))}
    out["revenue_agree"], out["revenue_n"] = _agree(j["total_revenue_s"], j["total_revenue_e"])
    out["parent_np_agree"], out["parent_np_n"] = _agree(j["n_income_attr_p_s"], j["n_income_attr_p_e"])
    jd = fi[["ts_code", "profit_dedt"]].merge(em, on="ts_code", how="inner")
    out["dedt_agree"], out["dedt_n"] = _agree(jd["profit_dedt"], jd["dedt_cum_em"])  # direct (income table) + eps-derived fallback
    out["dedt_by_label"] = {}
    for lab, g in jd.groupby("dedt_label"):
        if lab:
            a, nn = _agree(g["profit_dedt"], g["dedt_cum_em"])
            out["dedt_by_label"][lab] = {"agree": a, "n": nn}
    # eps-derived only (report table), independent of the income table
    rep = st._em_raw("report")
    if len(rep) and "REPORTDATE" in rep.columns:
        r = rep.copy()
        r["end_date"] = _ymd(r["REPORTDATE"])
        r = r[r["end_date"] == end_date]
        r = (_first_seen(r, ["ts_code", "end_date"]) if em_version == "first"
             else r.sort_values(["_seen_at"], kind="mergesort").drop_duplicates(["ts_code", "end_date"], keep="last"))
        b, d_, n_ = _num(r["BASIC_EPS"]), _num(r["DEDUCT_BASIC_EPS"]), _num(r["PARENT_NETPROFIT"])
        with np.errstate(divide="ignore", invalid="ignore"):
            r["dedt_eps"] = np.where((b != 0) & b.notna() & d_.notna(), n_ * d_ / b, np.nan)
        je = fi[["ts_code", "profit_dedt"]].merge(r[["ts_code", "dedt_eps"]], on="ts_code", how="inner")
        out["dedt_eps_derived_agree"], out["dedt_eps_derived_n"] = _agree(je["profit_dedt"], je["dedt_eps"])
    return out


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(reconcile_2026q3(), ensure_ascii=False, indent=2, default=str))
