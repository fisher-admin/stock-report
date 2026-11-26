#!/usr/bin/env python3
"""Export the #/today research card: Core7P profitable-gated top-25 (research / forward-unproven).

Reads the producer file written by the Core7P product builder
  factor_factory/data/forward_shadow/v2/product/core7p_top25_<YYYYMMDD>.json   (latest date wins)
validates it (strict board check, 20..30 holdings, unique codes, every dedt_ttm > 0 = the product constraint,
SW1 industry cap <= 4, official bool, dedt_source allowlist, names mostly present)
and writes <repo>/data/latest/core7p_today_state.json (schema core7p_today_v1).

Non-blocking by design (the publisher calls it with ``|| log WARN``):
  * source missing      -> overwrite existing output with unavailable if present, else write nothing; exit 0
  * source invalid      -> publish generic ``unavailable`` state (never stale/bad rows or raw errors), exit 1
  * unchanged (ignoring generated_at) -> keep the file, no empty publish

  export_core7p_today.py [--repo PATH] [--workspace PATH] [--expected-date YYYYMMDD] [--check]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
SCHEMA = "core7p_today_v1"
SRC_SCHEMA = "core7p_top25_v1"
PRODUCT_NAME = "Core7P 盈利门控精选池"
LABEL = "研究 / 前向未证：未经前向验证的读后参数描述性展示，非投资建议；扣非为正是产品约束而非 Alpha 证据"
BOARD = re.compile(r"^(60[0-9]{4}\.SH|00[0-9]{4}\.SZ|30[0-9]{4}\.SZ)\Z")
SRC_RE = re.compile(r"^core7p_top25_([0-9]{8})\.json\Z")
DATE_RE = re.compile(r"^[0-9]{8}\Z")
ACCT_RE = re.compile(r"^[0-9]+\Z")
PRIVATE_PATH = re.compile(r"/(?:Users|home|srv|root)/[^/\s\"']+/")
ALLOWED_DEDT_SOURCES = frozenset({
    "seed_profit_dedt",
    "em_deduct_parent_netprofit",
    "em_deduct_eps_derived",
})
MAX_SW1_INDUSTRY = 4
MIN_N, MAX_N = 20, 30
HOLDING_KEYS = (
    "rank", "ts_code", "code", "name", "industry", "score_pct", "dedt_ttm", "dedt_yoy", "rev_yoy",
    "roe_dedt", "ocf_ttm", "ocf_applicable", "pe_ttm", "pb", "dedt_source", "latest_report_date",
    "latest_ann_date", "action", "plan_status", "constrained_rank",
)
HOLDING_NUM_FIELDS = ("score_pct", "dedt_yoy", "rev_yoy", "roe_dedt", "ocf_ttm", "pe_ttm", "pb")
HOLDING_STR_FIELDS = ("name", "industry", "latest_report_date", "latest_ann_date", "action", "plan_status")
HOLDING_INT_FIELDS = ("rank", "constrained_rank")
VOLATILE = ("generated_at", "source_generated_at")


def workspace() -> Path:
    return Path(os.environ.get("STOCK_SYSTEM_WORKSPACE") or os.environ.get("OPENCLAW_WORKSPACE_DIR")
                or Path(__file__).resolve().parents[3])


def latest_source(w: Path) -> Path | None:
    d = w / "factor_factory/data/forward_shadow/v2/product"
    if not d.is_dir():
        return None
    found = sorted((m.group(1), p) for p in d.iterdir() if (m := SRC_RE.match(p.name)))
    return found[-1][1] if found else None


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        fv = float(v)
    except OverflowError:
        return False
    return math.isfinite(fv)


def _valid_date_str(v) -> bool:
    if not isinstance(v, str) or not DATE_RE.fullmatch(v):
        return False
    try:
        datetime.strptime(v, "%Y%m%d")
    except ValueError:
        return False
    return True


def _opt_float(v) -> float | None:
    return float(v) if _num(v) else None


def _opt_int(v) -> int | None:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else None


def _str(v, default: str = "") -> str:
    return v if isinstance(v, str) else default


def check_source(
    src: dict,
    *,
    filename_date: str | None = None,
    today: str | None = None,
) -> list[str]:
    errs: list[str] = []
    if src.get("schema") != SRC_SCHEMA:
        errs.append(f"schema {src.get('schema')!r} != {SRC_SCHEMA}")
    sig = src.get("signal_date")
    if not _valid_date_str(sig):
        errs.append("signal_date missing/invalid")
    else:
        if filename_date is not None and sig != filename_date:
            errs.append(f"signal_date {sig!r} != filename date {filename_date!r}")
        today_str = today or datetime.now(TZ).strftime("%Y%m%d")
        if sig > today_str:
            errs.append(f"signal_date {sig!r} is in the future (> {today_str})")
    if src.get("validation_errors"):
        errs.append("source has validation_errors")
    if not isinstance(src.get("official"), bool):
        errs.append("official must be boolean")
    h = src.get("holdings")
    if not isinstance(h, list) or not all(isinstance(x, dict) for x in h):
        return errs + ["holdings missing/invalid"]
    if not MIN_N <= len(h) <= MAX_N:
        errs.append(f"holdings size {len(h)} outside {MIN_N}..{MAX_N}")
    codes = [x.get("ts_code") for x in h]
    bad = [c for c in codes if not isinstance(c, str) or not BOARD.fullmatch(c)]
    if bad:
        errs.append(f"board violation (only 主板/创业板 60/00/30 allowed): {bad[:5]}")
    str_codes = [c for c in codes if isinstance(c, str)]
    if len(set(str_codes)) != len(str_codes):
        errs.append("duplicate holdings")
    mism = [
        x.get("ts_code")
        for x in h
        if isinstance(x.get("ts_code"), str)
        and (not isinstance(x.get("code"), str) or x.get("code") != x["ts_code"][:6])
    ]
    if mism:
        errs.append(f"code != ts_code[:6]: {mism[:5]}")
    nonpos = [x.get("ts_code") for x in h if not _num(x.get("dedt_ttm")) or x["dedt_ttm"] <= 0]
    if nonpos:
        errs.append(f"dedt_ttm must be > 0 (product constraint): {nonpos[:5]}")
    bad_src = [
        x.get("ts_code")
        for x in h
        if not isinstance(x.get("dedt_source"), str) or x.get("dedt_source") not in ALLOWED_DEDT_SOURCES
    ]
    if bad_src:
        errs.append(f"dedt_source invalid: {bad_src[:5]}")
    bad_types: list[str] = []
    for idx, x in enumerate(h):
        label = x.get("ts_code") if isinstance(x.get("ts_code"), str) else f"row[{idx}]"
        ocf_app = x.get("ocf_applicable")
        if ocf_app is not None and not isinstance(ocf_app, bool):
            bad_types.append(f"{label}.ocf_applicable")
        for k in HOLDING_NUM_FIELDS:
            v = x.get(k)
            if v is not None and not _num(v):
                bad_types.append(f"{label}.{k}")
        for k in HOLDING_INT_FIELDS:
            v = x.get(k)
            if v is not None and (not _int(v) or v <= 0):
                bad_types.append(f"{label}.{k}")
        for k in HOLDING_STR_FIELDS:
            v = x.get(k)
            if v is not None and not isinstance(v, str):
                bad_types.append(f"{label}.{k}")
    if bad_types:
        errs.append(f"invalid holding field types: {bad_types[:5]}")
    bad_holding_dates: list[str] = []
    sig_valid = _valid_date_str(sig)
    for idx, x in enumerate(h):
        label = x.get("ts_code") if isinstance(x.get("ts_code"), str) else f"row[{idx}]"
        for k in ("latest_report_date", "latest_ann_date"):
            v = x.get(k)
            if v is None or v == "" or not isinstance(v, str):
                continue
            if not _valid_date_str(v):
                bad_holding_dates.append(f"{label}.{k}={v!r}")
            elif sig_valid and v > sig:
                bad_holding_dates.append(f"{label}.{k}={v!r}>signal_date")
    if bad_holding_dates:
        errs.append(f"invalid/future holding dates: {bad_holding_dates[:5]}")
    ind_counts: dict[str, int] = {}
    for x in h:
        ind = x.get("industry")
        key = ind.strip() if isinstance(ind, str) and ind.strip() else "UNKNOWN"
        ind_counts[key] = ind_counts.get(key, 0) + 1
    if ind_counts and max(ind_counts.values()) > MAX_SW1_INDUSTRY:
        errs.append(f"SW1 industry cap > {MAX_SW1_INDUSTRY} (max={max(ind_counts.values())})")
    if sum(1 for x in h if not isinstance(x.get("name"), str) or not x.get("name").strip()) > max(2, len(h) // 5):
        errs.append("too many holdings without a name")
    if PRIVATE_PATH.search(json.dumps(src, ensure_ascii=False)):
        errs.append("private absolute path in source")
    return errs


def _build_coverage(raw) -> dict:
    c = raw if isinstance(raw, dict) else {}
    dp = c.get("degraded_pillars")
    return {
        "qarp_real_dedt_cov": _opt_float(c.get("qarp_real_dedt_cov")),
        "acc_real_cov": _opt_float(c.get("acc_real_cov")),
        "ocf_nonfin_cov": _opt_float(c.get("ocf_nonfin_cov")),
        "degraded_pillars": [p for p in dp if isinstance(p, str)] if isinstance(dp, list) else [],
    }


def _build_rollback(raw) -> dict:
    r = raw if isinstance(raw, dict) else {}
    return {
        "rule": _str(r.get("rule")),
        "sessions": _opt_int(r.get("sessions")) or 0,
        "rel_excess_60d": _opt_float(r.get("rel_excess_60d")),
        "triggered": r.get("triggered") if isinstance(r.get("triggered"), bool) else False,
    }


def _build_friction(raw) -> dict:
    f = raw if isinstance(raw, dict) else {}
    accounts_out: dict[str, dict[str, int | float]] = {}
    raw_accts = f.get("accounts")
    if isinstance(raw_accts, dict):
        for acct, av in raw_accts.items():
            if not isinstance(acct, str) or not ACCT_RE.fullmatch(acct) or not isinstance(av, dict):
                continue
            nb = _opt_int(av.get("names_buyable"))
            cd = _opt_float(av.get("cash_drag_pct"))
            if nb is not None and cd is not None:
                accounts_out[acct] = {"names_buyable": nb, "cash_drag_pct": cd}
    out: dict = {
        "lot": _opt_int(f.get("lot")),
        "min_commission_rmb": _opt_float(f.get("min_commission_rmb")),
        "stamp_duty_sell_bps": _opt_float(f.get("stamp_duty_sell_bps")),
        "accounts": accounts_out,
    }
    slip = _opt_float(f.get("slippage_bps"))
    if slip is not None:
        out["slippage_bps"] = slip
    return out


def build(w: Path | None = None, *, expected_date: str | None = None, today: str | None = None) -> dict:
    """Return {doc, errors, source}; doc is None when the source is missing or invalid."""
    w = w or workspace()
    p = latest_source(w)
    if p is None:
        return {"doc": None, "errors": [], "source": None}
    try:
        src = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(src, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        return {"doc": None, "errors": [f"cannot read {p.name}: {e}"], "source": p.name}
    m = SRC_RE.match(p.name)
    filename_date = m.group(1) if m else None
    try:
        errs = check_source(src, filename_date=filename_date, today=today)
    except (TypeError, ValueError, OverflowError) as e:
        return {"doc": None, "errors": [f"validation error: {e}"], "source": p.name}
    if errs:
        return {"doc": None, "errors": errs, "source": p.name}
    try:
        holdings = []
        for x in src["holdings"]:
            row = {
                "rank": _opt_int(x.get("rank")),
                "ts_code": x["ts_code"],
                "code": x["code"],
                "name": x.get("name") if isinstance(x.get("name"), str) else None,
                "industry": x.get("industry") if isinstance(x.get("industry"), str) else None,
                "score_pct": _opt_float(x.get("score_pct")),
                "dedt_ttm": float(x["dedt_ttm"]),
                "dedt_yoy": _opt_float(x.get("dedt_yoy")),
                "rev_yoy": _opt_float(x.get("rev_yoy")),
                "roe_dedt": _opt_float(x.get("roe_dedt")),
                "ocf_ttm": _opt_float(x.get("ocf_ttm")),
                "ocf_applicable": x.get("ocf_applicable") if isinstance(x.get("ocf_applicable"), bool) else None,
                "pe_ttm": _opt_float(x.get("pe_ttm")),
                "pb": _opt_float(x.get("pb")),
                "dedt_source": x["dedt_source"],
                "latest_report_date": x.get("latest_report_date") if isinstance(x.get("latest_report_date"), str) else None,
                "latest_ann_date": x.get("latest_ann_date") if isinstance(x.get("latest_ann_date"), str) else None,
                "action": x.get("action") if isinstance(x.get("action"), str) else None,
                "plan_status": x.get("plan_status") if isinstance(x.get("plan_status"), str) else None,
            }
            cr = _opt_int(x.get("constrained_rank"))
            if cr is not None:
                row["constrained_rank"] = cr
            holdings.append(row)
    except (TypeError, ValueError, OverflowError) as e:
        return {"doc": None, "errors": [f"build error: {e}"], "source": p.name}
    holdings.sort(key=lambda x: (x["rank"] if _int(x.get("rank")) else 10**6))
    for i, x in enumerate(holdings):
        x["rank"] = i + 1
    sig = str(src["signal_date"])
    stale = bool(expected_date and DATE_RE.fullmatch(expected_date) and sig < expected_date)
    doc = {
        "schema": SCHEMA,
        "status": "ok",
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "signal_date": sig,
        "stale": stale,
        "source_generated_at": src.get("generated_at") if isinstance(src.get("generated_at"), str) else None,
        "product": {
            "name": PRODUCT_NAME,
            "book": _str(src.get("book")) or "core7p_profitable_top25",
            "rule": _str(src.get("rule")),
            "spec_hash": src.get("spec_hash") if isinstance(src.get("spec_hash"), str) else None,
            "official": src["official"],
            "source_status": _str(src.get("status")) or "ok",
            "label": LABEL,
        },
        "coverage": _build_coverage(src.get("coverage")),
        "rollback": _build_rollback(src.get("rollback")),
        "friction": _build_friction(src.get("friction")),
        "holdings": holdings,
    }
    return {"doc": doc, "errors": [], "source": p.name}


def unavailable(reason: str) -> dict:
    return {"schema": SCHEMA, "status": "unavailable", "reason": reason,
            "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")}


def _stable(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in VOLATILE}


def write_if_changed(out: Path, doc: dict) -> bool:
    """Atomic write; False (and keep the file) when unchanged apart from generated_at."""
    try:
        if _stable(json.loads(out.read_text(encoding="utf-8"))) == _stable(doc):
            return False
    except (OSError, ValueError):
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=out.parent, suffix=".tmp", delete=False
        ) as f:
            tmp_path = Path(f.name)
            f.write(json.dumps(doc, ensure_ascii=False, indent=1, allow_nan=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return True


def _resolve_expected_date(repo: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit if DATE_RE.fullmatch(explicit) else None
    ew6 = repo / "data/latest/ew6_today_state.json"
    if not ew6.is_file():
        return None
    try:
        payload = json.loads(ew6.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sig = payload.get("signal_date") if isinstance(payload, dict) else None
    return sig if isinstance(sig, str) and DATE_RE.fullmatch(sig) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=None)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--expected-date", default=None, help="expected YYYYMMDD signal_date for freshness check")
    ap.add_argument("--check", action="store_true", help="build and validate only, do not write")
    a = ap.parse_args(argv)
    w = Path(a.workspace) if a.workspace else workspace()
    repo = Path(a.repo) if a.repo else w / "stock-report"
    out = repo / "data/latest/core7p_today_state.json"
    expected_date = _resolve_expected_date(repo, a.expected_date)
    r = build(w, expected_date=expected_date)
    if r["doc"] is None and not r["errors"]:
        if not a.check and out.exists():
            try:
                write_if_changed(out, unavailable("暂无最新 Core7P 产品文件"))
            except OSError:
                pass
        print(json.dumps({"status": "unavailable", "reason": "no core7p_top25 product file yet"}, ensure_ascii=False))
        return 0
    if r["errors"]:
        if not a.check:
            try:
                write_if_changed(out, unavailable("Core7P 产品文件未通过发布校验，暂不展示"))
            except OSError:
                pass
        print(json.dumps({"status": "invalid", "errors": r["errors"]}, ensure_ascii=False))
        return 1
    doc = r["doc"]
    info = {
        "status": "ok",
        "signal_date": doc["signal_date"],
        "stale": doc["stale"],
        "holdings": len(doc["holdings"]),
        "source": r["source"],
        "source_status": doc["product"]["source_status"],
    }
    if not a.check:
        try:
            changed = write_if_changed(out, doc)
        except OSError as e:
            print(json.dumps({"status": "invalid", "errors": [f"write failed: {e}"]}, ensure_ascii=False))
            return 1
        if not changed:
            info["unchanged"] = True
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
