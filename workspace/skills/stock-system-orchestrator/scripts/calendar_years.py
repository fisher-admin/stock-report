#!/usr/bin/env python3
"""Zero-paid yearly exchange calendar (A1, 2026-11-20).

The paid trade_cal is gone (11-19), so the calendar for a new year is built from free, verifiable facts:

  1. provisional  rule "A-share open days = weekdays − State Council statutory holidays" (make-up working
                  Saturdays/Sundays never trade).  Built as soon as the State Council notice is out (early Nov).
  2. confirmed    each month is checked against the SZSE official month calendar
                  (szse.cn/api/report/exchange/onepersistenthour/monthList); SZSE returns an empty list until the
                  exchanges publish their closing notice (usually late Dec), so unpublished months simply stay
                  provisional.  Any disagreement is a conflict: open_dates are NOT changed, the run exits 4.
  3. reconcile    every evening: "did the market actually trade today?" from the tdx daily cache already on disk
                  (no request) vs the calendar.  A traded closed day or a silent open day exits 4.

Files: stock_data/03-working/health/calendar_years/calendar_<year>.json (+ requests.log).  open_dates are written
once and never silently rewritten; status changes are appended to "history".  The self-healing trade_cal
(skills/stock-analyzer/selfheal_client.py) serves dates beyond the last paid calendar from these files.

Request budget: confirm = at most one SZSE request per still-provisional month, >= 3 s apart, no retries, and the
loop stops at the first unpublished month.  build / reconcile make no requests.

Usage:
  calendar_years.py build --year 2027
  calendar_years.py confirm --year 2027
  calendar_years.py reconcile [--date YYYYMMDD]
  calendar_years.py status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
EXIT_OK, EXIT_ERROR, EXIT_CONFLICT = 0, 2, 4
SZSE_URL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
SZSE_GAP_SECONDS = 3.0

# Statutory holidays per State Council notice (full closure ranges, inclusive).  Weekend make-up working days are
# irrelevant for the exchanges and therefore not listed.
NOTICES: dict[int, dict[str, Any]] = {
    2027: {
        "notice": "国务院办公厅关于2027年部分节假日安排的通知（国办发明电〔2026〕10号）",
        "published": "2026-11-05",
        "url": "https://www.news.cn/politics/20261105/f90cd6ad4df94ab4833b141e52ccfc13/c.html",
        "holidays": {
            "元旦": ("2027-01-01", "2027-01-03"),
            "春节": ("2027-02-05", "2027-02-13"),
            "清明节": ("2027-04-03", "2027-04-05"),
            "劳动节": ("2027-05-01", "2027-05-05"),
            "端午节": ("2027-06-09", "2027-06-09"),
            "中秋节": ("2027-09-15", "2027-09-15"),
            "国庆节": ("2027-10-01", "2027-10-07"),
        },
        "expected_open_days": 243,
    },
}


def _workspace() -> Path:
    return Path(os.environ.get("STOCK_SYSTEM_WORKSPACE") or os.environ.get("OPENCLAW_WORKSPACE_DIR")
                or Path(__file__).resolve().parents[3])


def years_dir(ws: Path | None = None) -> Path:
    return (ws or _workspace()) / "stock_data/03-working/health/calendar_years"


def year_path(year: int, ws: Path | None = None) -> Path:
    return years_dir(ws) / f"calendar_{year}.json"


def now_text() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _days(a: str, b: str) -> list[date]:
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def provisional_open_dates(year: int) -> tuple[list[str], list[str]]:
    """(open_dates, closed_weekdays) by rule; raises if the notice is unknown or the count disagrees."""
    spec = NOTICES.get(year)
    if not spec:
        raise ValueError(f"no State Council notice registered for {year}")
    holidays = {d for a, b in spec["holidays"].values() for d in _days(a, b)}
    if any(d.year != year for d in holidays):
        raise ValueError("holiday range crosses the year boundary; register it under both years")
    opened, closed = [], []
    for d in _days(f"{year}-01-01", f"{year}-12-31"):
        if d.weekday() >= 5:
            continue
        (closed if d in holidays else opened).append(d.strftime("%Y%m%d"))
    if spec.get("expected_open_days") and len(opened) != spec["expected_open_days"]:
        raise ValueError(f"{year}: rule gives {len(opened)} open days, notice check expects {spec['expected_open_days']}")
    return opened, closed


def build(year: int, ws: Path | None = None) -> dict[str, Any]:
    path = year_path(year, ws)
    opened, closed = provisional_open_dates(year)
    if path.exists():
        cur = json.loads(path.read_text(encoding="utf-8"))
        if cur.get("open_dates") != opened:
            raise RuntimeError(f"{path} exists with different open_dates; refusing to overwrite (ledger purity)")
        return {"status": "unchanged", "path": str(path), "open_days": len(opened)}
    spec = NOTICES[year]
    payload = {
        "schema": "calendar_year_v1",
        "year": year,
        "from": f"{year}0101",
        "to": f"{year}1231",
        "status": "provisional",
        "rule": "open = weekdays - State Council statutory holidays (weekend make-up days do not trade)",
        "source": "state_council_rule",
        "notice": spec["notice"], "notice_published": spec["published"], "notice_url": spec["url"],
        "holidays": {k: list(v) for k, v in spec["holidays"].items()},
        "closed_weekdays": closed,
        "open_dates": opened,
        "open_days": len(opened),
        "months": {f"{year}-{m:02d}": {"status": "provisional"} for m in range(1, 13)},
        "generated_at": now_text(),
        "history": [{"at": now_text(), "event": "built_provisional", "open_days": len(opened)}],
    }
    _write_atomic(path, payload)
    return {"status": "built", "path": str(path), "open_days": len(opened)}


def _log_request(ws: Path | None, line: dict[str, Any]) -> None:
    p = years_dir(ws) / "requests.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now_text(), **line}, ensure_ascii=False) + "\n")


def fetch_szse_month(month: str, ws: Path | None = None) -> list[dict[str, Any]]:
    import requests  # noqa: PLC0415

    t0 = time.time()
    try:
        r = requests.get(SZSE_URL, params={"month": month}, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.szse.cn/"})
        status = r.status_code
        data = r.json().get("data") if status == 200 else None
    except Exception as exc:  # noqa: BLE001
        _log_request(ws, {"month": month, "error": type(exc).__name__, "sec": round(time.time() - t0, 2)})
        raise
    _log_request(ws, {"month": month, "http": status, "rows": len(data or []), "sec": round(time.time() - t0, 2)})
    if status != 200 or not isinstance(data, list):
        raise RuntimeError(f"SZSE monthList {month}: http {status}")
    return data


def compare_month(cal: dict[str, Any], month: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    y, m = int(month[:4]), int(month[5:7])
    expect_days = {d.strftime("%Y-%m-%d") for d in _days(f"{month}-01", (date(y + (m == 12), m % 12 + 1, 1)
                                                                       - timedelta(days=1)).isoformat())}
    got = {str(r.get("jyrq")): str(r.get("jybz")) for r in rows}
    if set(got) != expect_days or any(v not in ("0", "1") for v in got.values()):
        return {"ok": False, "reason": "incomplete_or_malformed", "rows": len(rows)}
    official = sorted(k.replace("-", "") for k, v in got.items() if v == "1")
    ours = sorted(d for d in cal["open_dates"] if d.startswith(month.replace("-", "")))
    if official != ours:
        return {"ok": False, "reason": "disagree", "only_szse": sorted(set(official) - set(ours)),
                "only_provisional": sorted(set(ours) - set(official))}
    return {"ok": True, "open_days": len(official)}


def confirm(year: int, ws: Path | None = None, fetch=fetch_szse_month, sleep=time.sleep) -> tuple[int, dict]:
    path = year_path(year, ws)
    cal = json.loads(path.read_text(encoding="utf-8"))
    todo = [m for m, v in sorted(cal["months"].items()) if v["status"] == "provisional"]
    report: dict[str, Any] = {"year": year, "checked": [], "confirmed": [], "unpublished": None, "conflicts": {}}
    first = True
    for month in todo:
        if not first:
            sleep(SZSE_GAP_SECONDS)
        first = False
        rows = fetch(month, ws)
        report["checked"].append(month)
        if not rows:
            report["unpublished"] = month          # later months cannot be published either
            break
        res = compare_month(cal, month, rows)
        if res["ok"]:
            cal["months"][month] = {"status": "confirmed_szse", "confirmed_at": now_text(), "open_days": res["open_days"]}
            report["confirmed"].append(month)
        else:
            cal["months"][month] = {"status": "conflict", "checked_at": now_text(), **res}
            report["conflicts"][month] = res
            break                                    # fail closed on the first conflict
    if report["confirmed"] or report["conflicts"]:
        if all(v["status"] == "confirmed_szse" for v in cal["months"].values()):
            cal["status"] = "confirmed"
        elif report["conflicts"]:
            cal["status"] = "conflict"
        cal["history"].append({"at": now_text(), "event": "confirm", "confirmed": report["confirmed"],
                               "conflicts": list(report["conflicts"])})
        _write_atomic(path, cal)
    report["status"] = cal["status"]
    return (EXIT_CONFLICT if report["conflicts"] else EXIT_OK), report


# ---------------------------------------------------------------------------------------------- reconcile
def calendar_open(day: str, ws: Path | None = None) -> bool | None:
    """Open/closed from the persisted calendar, else the year file; None when nothing covers the day."""
    w = ws or _workspace()
    persisted = w / "stock_data/03-working/health/trading_calendar.json"
    if persisted.exists():
        cal = json.loads(persisted.read_text(encoding="utf-8"))
        if str(cal.get("from")) <= day <= str(cal.get("to")):
            return day in set(map(str, cal["open_dates"]))
    yp = year_path(int(day[:4]), w)
    if yp.exists():
        return day in set(json.loads(yp.read_text(encoding="utf-8"))["open_dates"])
    return None


def traded(day: str, ws: Path | None = None, min_active: int = 1000) -> bool | None:
    """True/False from the tdx daily cache already on disk; None when the cache for the day is absent."""
    p = (ws or _workspace()) / f"stock_data/03-working/backtest_cache/daily_{day}.parquet"
    if not p.exists():
        return None
    import pandas as pd  # noqa: PLC0415

    f = pd.read_parquet(p, columns=["vol"])
    return int((pd.to_numeric(f["vol"], errors="coerce") > 0).sum()) >= min_active


def reconcile(day: str, ws: Path | None = None) -> tuple[int, dict]:
    is_open = calendar_open(day, ws)
    did = traded(day, ws)
    r = {"date": day, "calendar_open": is_open, "tdx_traded": did}
    if is_open is None:
        r["verdict"] = "calendar_missing"
        return EXIT_ERROR, r
    if did is None:
        r["verdict"] = "ok_closed" if not is_open else "tdx_cache_missing"
        return (EXIT_OK if not is_open else EXIT_ERROR), r
    if is_open == did:
        r["verdict"] = "ok"
        return EXIT_OK, r
    r["verdict"] = "traded_on_closed_day" if did else "no_trade_on_open_day"
    return EXIT_CONFLICT, r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--year", type=int, required=True)
    c = sub.add_parser("confirm"); c.add_argument("--year", type=int, required=True)
    r = sub.add_parser("reconcile"); r.add_argument("--date")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            out, rc = build(a.year), EXIT_OK
        elif a.cmd == "confirm":
            rc, out = confirm(a.year)
        elif a.cmd == "reconcile":
            rc, out = reconcile(a.date or datetime.now(TZ).strftime("%Y%m%d"))
        else:
            out, rc = {}, EXIT_OK
            for p in sorted(years_dir().glob("calendar_*.json")):
                cal = json.loads(p.read_text(encoding="utf-8"))
                out[cal["year"]] = {"status": cal["status"], "open_days": cal["open_days"],
                                    "months": {m: v["status"] for m, v in cal["months"].items()}}
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return EXIT_ERROR
    print(json.dumps(out, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
