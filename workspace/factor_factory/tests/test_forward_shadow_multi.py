"""Fault-injection tests for forward_shadow_multi_v1 (no network, no writes outside multi_v1/test)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.dont_write_bytecode = True
import numpy as np, pandas as pd
import forward_shadow_multi as m
import kline_quality_gate as kq

TZ = m.TZ
OK_PROV = {"universe_degraded_mode": "none"}
V1_OK = {"official": True}


def test_fail_closed_deadline():
    now = datetime.now(TZ)
    assert m.is_late(None, now) is True
    assert m.is_late(now + timedelta(hours=1), now) is False
    assert m.is_late(now - timedelta(seconds=1), now) is True


def test_official_rules():
    assert m.official_base(False, False, OK_PROV, V1_OK) is True
    assert m.official_base(True, False, OK_PROV, V1_OK) is False                      # test run
    assert m.official_base(False, True, OK_PROV, V1_OK) is False                      # late
    for bad in ("pit_proxy_accepted", "pit_ffill_1d", "static_st_fallback", "no_official_universe"):
        assert m.official_base(False, False, {"universe_degraded_mode": bad}, V1_OK) is False
    assert m.official_base(False, False, OK_PROV, {"official": False}) is False       # v1 itself late
    assert m.official_base(False, False, OK_PROV, None) is False


def test_board_mandate():
    codes = ["600000.SH", "000001.SZ", "300750.SZ", "301001.SZ", "688981.SH", "689009.SH", "830799.BJ",
             "430047.BJ", "920001.BJ", "873527.BJ"]
    assert m.board_ok(codes).tolist() == [True] * 4 + [False] * 6


def _px(n=100):
    idx = [f"{600000 + i}.SH" for i in range(n)]
    return pd.DataFrame({"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "pre_close": 10.0, "vol": 1000.0},
                        index=pd.Index(idx, name="ts_code"))


def test_kline_gate():
    px = _px(); uni = list(px.index)
    assert kq.check_day(px, uni)["ok"]
    bad = px.copy(); bad.iloc[0, bad.columns.get_loc("high")] = 9.0          # high < close
    assert kq.check_day(bad, uni)["ok"] is False                               # 1 bad of 100 > 0.5%
    jump = px.copy(); jump.iloc[1, jump.columns.get_loc("close")] = 12.0       # +20% on a 10% board
    jump.iloc[1, jump.columns.get_loc("high")] = 12.0
    assert kq.check_day(jump, uni)["ok"] is False
    assert kq.check_day(px.iloc[:95], uni)["reason"] == "coverage"
    assert kq.check_day(None, uni)["ok"] is False


def test_book_lists_and_targets():
    n = 200
    df = pd.DataFrame({"ts_code": [f"{600000 + i}.SH" for i in range(n)], "comp_rank_ew6": np.arange(n, dtype=float),
                       "aegis_micro_5p": np.arange(n, dtype=float)[::-1], "aegis_core_7p": np.arange(n, dtype=float),
                       "ind_sw1_name": ["A"] * 100 + ["B"] * 100})
    L = m.book_lists(df, "aegis_micro5p_10_30")
    assert len(L["entry"]) == 21 and L["entry"][0] == "600000.SH" and len(L["hold"]) == 61   # inclusive >= 1-p, as research
    q = {c: (0.0, 0.0, 0.0, True, True) for c in df.ts_code}
    q["600000.SH"] = (0.1, 0.0, 0.1, False, True)                              # limit-up open: cannot buy
    new = m._targets("aegis_micro5p_10_30", L, set(), q)
    assert "600000.SH" not in new and len(new) == 20
    kept = {f"{600000 + i}.SH" for i in range(40, 60)}                         # 20 kept inside hold zone
    new2 = m._targets("aegis_micro5p_10_30", L, kept, q)
    assert kept <= set(new2) and len(new2) == 20   # size = #buyable entry (20), all kept stay
    Lc = m.book_lists(df, "aegis_core7p_top30_h90_cap4")
    new3 = m._targets("aegis_core7p_top30_h90_cap4", Lc, set(), q)
    assert len(new3) == 8                                                       # 2 industries x cap 4


# ---------------------------------------------------------------- 11-18 pre-launch review additions

def test_kline_gate_nan_false_negatives():
    px = _px(); uni = list(px.index)
    a = px.copy(); a["pre_close"] = np.nan                                     # all pre_close NaN
    assert kq.check_day(a, uni)["ok"] is False
    b = px.copy(); b["high"] = np.nan; b["low"] = np.nan                       # all high/low NaN
    assert kq.check_day(b, uni)["ok"] is False
    c = px.copy(); c.iloc[:5, c.columns.get_loc("open")] = np.nan              # vol > 0 but open NaN (5% of rows)
    assert kq.check_day(c, uni)["ok"] is False
    e = px.copy(); e.iloc[:5, e.columns.get_loc("vol")] = np.nan               # NaN volume
    assert kq.check_day(e, uni)["ok"] is False
    s = px.copy(); s.iloc[0, s.columns.get_loc("vol")] = 0.0; s.iloc[0, s.columns.get_loc("open")] = np.nan
    assert kq.check_day(s, uni)["ok"] is True                                 # suspended row with NaN open is fine


def test_parity_ok():
    assert m.parity_ok({"a": float("nan"), "b": float("nan")}, 2000) is False   # all([]) trap
    assert m.parity_ok({}, 2000) is False
    assert m.parity_ok({"a": 1.0, "b": 0.9995}, 50) is False                   # too few rows
    assert m.parity_ok({"a": 1.0, "b": 0.9995}, 2000) is True
    assert m.parity_ok({"a": 1.0, "b": 0.998}, 2000) is False


def test_should_wait_ann():
    now = datetime(2026, 11, 19, 23, 50, tzinfo=TZ)
    dl = datetime(2026, 11, 20, 9, 30, tzinfo=TZ)
    assert m.should_wait_ann(False, False, True, True, dl, now)               # x_jump still running
    assert m.should_wait_ann(False, False, False, False, dl, now)             # 23:50, fetch not ok
    assert m.should_wait_ann(False, False, False, False, dl, dl - timedelta(minutes=110))   # 07:40
    assert not m.should_wait_ann(False, False, False, False, dl, dl - timedelta(minutes=35))  # 08:55: decide
    assert not m.should_wait_ann(False, False, False, True, dl, now)          # fetch ok: decide
    assert not m.should_wait_ann(False, True, True, False, None, now)         # late: decide (non-official)
    assert not m.should_wait_ann(True, False, True, False, dl, now)           # test run


def test_resolve_session():
    cal = ["20261113", "20261116", "20261117", "20261118"]                    # Fri, Mon, Tue, Wed
    assert m.resolve_session("20261116", "prev", cal) == "20261113"           # Monday morning -> Friday
    assert m.resolve_session("20261115", "prev", cal) is None                 # Sunday: quiet exit
    assert m.resolve_session("20261118", "today", cal) == "20261118"
    assert m.resolve_session("20261114", "today", cal) is None


# ---- replay with synthetic quotes
CAL = [f"2026110{i}" for i in range(1, 10)] + [f"202611{i}" for i in range(10, 30)]


def _dec(d, k, official=True, codes=("600000.SH", "600001.SH"), anchor=CAL[0]):
    L = {"entry": list(codes), "hold": list(codes)}
    return {"signal_date": d, "anchor": anchor, "subbook": k, "universe": list(codes) + ["600009.SH"],
            "official": official, "degraded": False,
            "books": {b: (L if m.BOOKS[b][3] == "pct" else {**L, "size": 2, "industry": {}}) for b in m.BOOKS},
            "official_books": {b: official for b in m.BOOKS}}


def _flat_quotes(monkeypatch):
    monkeypatch.setattr(m, "day_quotes", lambda d, st: {c: (0.0, 0.01, 0.01, True, True)
                                                         for c in ("600000.SH", "600001.SH", "600009.SH")})
    monkeypatch.setattr(m, "_st", lambda d: set())


def test_replay_missed_rebalance_not_official(monkeypatch):
    _flat_quotes(monkeypatch)
    decs = [_dec(CAL[i], i % 5) for i in range(14) if i != 7]                  # signal CAL[7] missing (gap night)
    df = m.replay(decs, CAL, CAL[14])
    b = df[df.book == "aegis_micro5p_10_30"].set_index("date")
    assert bool(b.loc[CAL[6], "official_day"]) is True
    assert bool(b.loc[CAL[8], "official_day"]) is False                        # sub-book 2 missed at CAL[7]
    assert bool(b.loc[CAL[12], "official_day"]) is False                       # sub-book 2 still on the stale book
    assert bool(b.loc[CAL[13], "official_day"]) is True                        # its next rebalance (signal CAL[12]) executed
    be = df[df.book == "bench_ew"].set_index("date")
    assert np.isfinite(be.loc[CAL[8], "ret_9.6bp"])                            # last_u fallback, no NaN
    assert bool(be.loc[CAL[8], "official_day"]) is False
    assert bool(be.loc[CAL[7], "official_day"]) is True


def test_replay_late_decision_drives_continuous_track(monkeypatch):
    _flat_quotes(monkeypatch)
    decs = [_dec(CAL[i], i % 5, official=(i != 6)) for i in range(12)]
    df = m.replay(decs, CAL, CAL[12])
    b = df[df.book == "ew6_retain_15_30"].set_index("date")
    assert bool(b.loc[CAL[7], "official_day"]) is False and np.isfinite(b.loc[CAL[7], "ret_9.6bp"])


# ---- run(): clock re-read, ann wait, gap reasons (all I/O monkeypatched into tmp_path)
def _wire_run(monkeypatch, tmp_path, now_seq, ann_ok=True, xj=False):
    import json as _json
    monkeypatch.setattr(m, "OUT", tmp_path)
    monkeypatch.setattr(m.fs, "committed_and_clean", lambda p: True)
    spec = tmp_path / "spec.json"; spec.write_text(_json.dumps({"spec_hash": m.spec_hash()}))
    monkeypatch.setattr(m, "SPEC_PATH", spec)
    monkeypatch.setattr(m.fs, "official_sessions", lambda upto: ["20261118", "20261119", "20261120"])
    monkeypatch.setattr(m, "v1_recorded", lambda d: {"official": True})
    sc = tmp_path / "v1scores"; sc.mkdir(); (sc / "scores").mkdir()
    pd.DataFrame({"ts_code": ["600000.SH"]}).to_parquet(sc / "scores" / "scores_20261119.parquet")
    monkeypatch.setattr(m, "FS_V1", sc)
    monkeypatch.setattr(m, "day_prices", lambda d: None)
    monkeypatch.setattr(m.kq, "check_day", lambda px, u: {"ok": True})
    monkeypatch.setattr(m, "ann_fetch_ok", lambda d, p: ann_ok)
    monkeypatch.setattr(m, "x_jump_running", lambda: xj)
    df = pd.DataFrame({"ts_code": ["600000.SH", "600001.SH"], "comp_rank_ew6": [1.0, 0.5], "aegis_micro_5p": [1.0, 0.5],
                       "aegis_core_7p": [1.0, 0.5], "ind_sw1_name": ["A", "B"]})
    monkeypatch.setattr(m, "score_day", lambda d, P: (df, {"universe_degraded_mode": "none", "degraded_mode": "none"}))
    monkeypatch.setattr(m, "settle", lambda P, cal, d: {})
    monkeypatch.setattr(m.rp, "git_commit", lambda: "test")
    it = iter(now_seq)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(it)
    monkeypatch.setattr(m, "datetime", _DT)


def _rows(p):
    import json as _json
    return [_json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_run_clock_reread_crossing_deadline(monkeypatch, tmp_path):
    t0 = datetime(2026, 11, 20, 9, 29, 45, tzinfo=TZ); t1 = datetime(2026, 11, 20, 9, 30, 5, tzinfo=TZ)
    _wire_run(monkeypatch, tmp_path, [t0, t1, t1, t1])
    out = m.run("20261119", False)
    assert out["status"] == "decided" and out["official"] is False             # crossed 09:30 while scoring
    assert _rows(tmp_path / "gaps.jsonl")[-1]["reason"] == "late"


def test_run_waits_for_ann_then_core7p_stale_gap(monkeypatch, tmp_path):
    t_eve = datetime(2026, 11, 19, 23, 50, tzinfo=TZ)
    _wire_run(monkeypatch, tmp_path, [t_eve], ann_ok=False)
    assert m.run("20261119", False)["status"] == "waiting_for_x_jump_ms_ann_fetch"
    assert not (tmp_path / "decisions.jsonl").exists()
    t_855 = datetime(2026, 11, 20, 8, 55, tzinfo=TZ)
    (tmp_path / "b").mkdir()
    _wire_run(monkeypatch, tmp_path / "b", [t_855, t_855, t_855], ann_ok=False)
    out = m.run("20261119", False)
    assert out["official"] is True and out["official_books"]["aegis_core7p_10_30"] is False
    assert out["official_books"]["aegis_micro5p_10_30"] is True
    assert _rows(tmp_path / "b" / "gaps.jsonl")[-1]["reason"] == "core7p_ann_stale"


def test_run_waits_while_x_jump_running(monkeypatch, tmp_path):
    _wire_run(monkeypatch, tmp_path, [datetime(2026, 11, 20, 8, 55, tzinfo=TZ)], ann_ok=True, xj=True)
    assert m.run("20261119", False)["status"] == "waiting_for_x_jump_ms_ann_fetch"
