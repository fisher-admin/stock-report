#!/usr/bin/env python3
"""data_freshness_watchdog.py — 荐股系统数据新鲜度看门狗（独立于主管线）。

目的：主管线自己挂掉时不会自己报警（2026-06 停更事故就是预检每天失败、连续 4 天无人察觉）。
本脚本独立检查"数据是否停更"，发现陈旧就响亮告警并以非零退出码结束，便于 cron / openclaw
心跳捕获。建议每个交易日收盘后（如 20:30）单独调度一次。

检查项：
  1. recommendation_warehouse.db：recommendation_fact 最新推荐日、price_daily_cache 最新行情日；
  2. stock-report/data/latest/run_manifest.json：发布层 trade_date 与生成时间；
  3. health/runs/ 最近一份运行报告：preflight/各 stage 是否 ok，失败原因；
  4. 用"最近应有交易日"（粗略跳过周末）判断滞后天数，超阈值即判 STALE。

只读，零三方依赖（stdlib + sqlite3）。退出码 0=正常，1=陈旧/异常（用于告警）。
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

WORKSPACE = Path(
    os.environ.get("OPENCLAW_WORKSPACE")
    or os.environ.get("STOCK_SYSTEM_WORKSPACE")
    or str(Path.home() / ".openclaw" / "workspace")
)
DB_PATH = WORKSPACE / "stock_data" / "03-working" / "recommendation_warehouse" / "recommendations.db"
RUN_MANIFEST = WORKSPACE / "stock-report" / "data" / "latest" / "run_manifest.json"
HEALTH_RUNS = WORKSPACE / "stock_data" / "03-working" / "health" / "runs"
STATUS_FILE = WORKSPACE / "stock_data" / "03-working" / "health" / "data_freshness_watchdog.json"
DELIVERY_RECEIPT = WORKSPACE / "stock_data" / "03-working" / "health" / "delivery_receipt.json"
POST_RESEARCH_RUN = WORKSPACE / "stock_data" / "03-working" / "health" / "post_publish_research_run.json"
FACTORY_STATUS = WORKSPACE / "stock_data" / "03-working" / "health" / "prebreakout_factory_status.json"
TRADING_CALENDAR = WORKSPACE / "stock_data" / "03-working" / "health" / "trading_calendar.json"
ALERT_LOG = Path(os.environ.get("OPENCLAW_LOG_DIR", str(Path.home() / ".openclaw" / "logs"))) / "data-freshness-watchdog.log"

# Discord 告警：与主管线 stage6 同一组环境变量；都没设时从配置文件兜底读取（便于 launchd/cron 无 env 场景）。
# 用户只需在 Discord 频道建一个 incoming webhook，把 URL 写进下面任一处，管线与看门狗即共用。
DISCORD_WEBHOOK_ENV_KEYS = (
    "OPENCLAW_DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL",
    "STOCK_SYSTEM_DISCORD_WEBHOOK_URL", "OMX_DISCORD_WEBHOOK_URL",
)
DISCORD_WEBHOOK_FILE = Path.home() / ".openclaw" / "credentials" / "discord-webhook"
# Bot 直发模式（复用 openclaw 的 Discord bot，发到指定频道；优先于 webhook）。
DISCORD_BOT_TOKEN_ENV_KEYS = ("DISCORD_BOT_TOKEN", "OPENCLAW_DISCORD_BOT_TOKEN", "DISCORD_TOKEN")
DISCORD_ALERT_CONFIG = Path.home() / ".openclaw" / "credentials" / "discord-alert.json"
OPENCLAW_JSON = Path.home() / ".openclaw" / "openclaw.json"

# 滞后阈值（交易日）：最新数据落后"最近应有交易日"超过这么多个交易日即告警。
STALE_TRADING_DAYS = int(os.environ.get("WATCHDOG_STALE_TRADING_DAYS", "1"))
# 20261010: Hermes 晚间链整体提前 2 小时（17:30 主管线 / 18:00 发布 / 20:15 后置研究）。
SAME_DAY_DEADLINE = os.environ.get("WATCHDOG_SAME_DAY_DEADLINE", "18:30")
def _due_time(text: str, default: _dt.time) -> _dt.time:
    try:
        hour, minute = (int(v) for v in text.split(":", 1))
        return _dt.time(hour, minute)
    except (TypeError, ValueError):
        return default


# 可由 config/stock_schedule_overrides.env 覆盖（例如回退到 19:30 链时设为 22:15，见 ops/schedule_contingency.py）
POST_RESEARCH_DUE_TIME = _due_time(os.environ.get("WATCHDOG_POST_RESEARCH_DUE", "20:15"), _dt.time(20, 15))


def _discord_webhook_url() -> str:
    """解析 Discord webhook URL：先环境变量（与 stage6 同名），再 credentials/discord-webhook 文件。"""
    for key in DISCORD_WEBHOOK_ENV_KEYS:
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    try:
        for line in DISCORD_WEBHOOK_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line and not line.startswith("http"):
                line = line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("http"):
                return line
    except OSError:
        pass
    return ""


def _discord_bot_token() -> str:
    """Bot token：先环境变量，再复用 openclaw.json 的 channels.discord.token。"""
    for key in DISCORD_BOT_TOKEN_ENV_KEYS:
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    try:
        doc = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
        token = ((doc.get("channels", {}) or {}).get("discord", {}) or {}).get("token", "")
        return token.strip() if token else ""
    except Exception:  # noqa: BLE001
        return ""


def _discord_channel_id() -> str:
    val = os.environ.get("DISCORD_ALERT_CHANNEL_ID")
    if val and val.strip():
        return val.strip()
    try:
        return str(json.loads(DISCORD_ALERT_CONFIG.read_text(encoding="utf-8")).get("channel_id") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _send_discord(text: str) -> bool:
    """优先用 bot token 直发指定频道（复用 openclaw Discord bot）；否则退回 incoming webhook。
    告警尽力而为，未配置或失败返回 False，不影响退出码。"""
    payload = json.dumps({"content": text[:1900]}).encode("utf-8")
    # 1) Bot 直发频道。
    token, channel = _discord_bot_token(), _discord_channel_id()
    if token and channel:
        try:
            req = urllib.request.Request(
                f"https://discord.com/api/v10/channels/{channel}/messages", data=payload,
                headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                         "User-Agent": "stock-watchdog/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 204):
                    return True
        except Exception:  # noqa: BLE001
            pass
    # 2) Incoming webhook 兜底。
    url = _discord_webhook_url()
    if url:
        try:
            req = urllib.request.Request(
                url, data=json.dumps({"content": text[:1900], "username": "荐股系统看门狗"}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except Exception:  # noqa: BLE001
            pass
    return False


def _macos_notify(title: str, text: str) -> bool:
    """macOS 本机通知（登录会话内可见）；非 mac 或失败返回 False。"""
    if sys.platform != "darwin":
        return False
    safe = text.replace('"', "'")[:240]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{title}"'],
                       check=False, timeout=8)
        return True
    except Exception:  # noqa: BLE001
        return False


def dispatch_alert(report: dict) -> dict:
    """多通道告警：飞书 webhook（若已配）+ macOS 通知 + 告警日志。状态文件总是写。"""
    lines = ["⚠️ 荐股系统数据停更/异常告警", f"检查时间：{report['checked_at']}", f"应到交易日：{report['expected_trading_day']}"]
    lines += [f"· {s}" for s in report.get("stale_findings", [])]
    last = report.get("last_run", {})
    if not last.get("ok"):
        lines.append(f"· 最近运行未通过：{last.get('run_file')}（{last.get('preflight_reason') or '见报告'}）")
    text = "\n".join(lines)
    runtime_owner = str(os.environ.get("STOCK_HEALTH_RUNTIME") or "openclaw").strip().lower()
    if runtime_owner == "hermes":
        sent = {"delivery_owner": "hermes", "discord": False, "macos": False}
    else:
        sent = {
            "delivery_owner": "openclaw",
            "discord": _send_discord(text),
            "macos": _macos_notify("荐股系统告警", text),
        }
    try:
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ALERT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[{report['checked_at']}] " + text.replace("\n", " | ") + f" | sent={sent}\n")
    except OSError:
        pass
    return sent


def _today() -> _dt.date:
    return _dt.datetime.now().date()


def _recent_trading_day(ref: _dt.date) -> _dt.date:
    """最近一个应有数据的交易日（粗略：跳过周六日；不含法定节假日日历，故允许 1 日缓冲）。"""
    d = ref
    while d.weekday() >= 5:  # 5=周六 6=周日
        d -= _dt.timedelta(days=1)
    return d


def _load_open_dates(calendar_path: Path) -> tuple[list[str], str, str]:
    try:
        payload = json.loads(calendar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return [], "", ""
    raw_dates = payload.get("open_dates") if isinstance(payload, dict) else None
    if not isinstance(raw_dates, list):
        return [], "", ""
    dates = sorted(
        {
            str(value)
            for value in raw_dates
            if len(str(value)) == 8 and str(value).isdigit()
        }
    )
    return dates, str(payload.get("from") or ""), str(payload.get("to") or "")


def _expected_trading_day(
    ref: _dt.date,
    *,
    calendar_path: Path = TRADING_CALENDAR,
) -> tuple[_dt.date, list[str]]:
    open_dates, date_from, date_to = _load_open_dates(calendar_path)
    ref_text = ref.strftime("%Y%m%d")
    if open_dates and date_from <= ref_text <= date_to:
        candidates = [value for value in open_dates if value <= ref_text]
        if candidates:
            resolved = _parse_yyyymmdd(candidates[-1])
            if resolved is not None:
                return resolved, open_dates
    return _recent_trading_day(ref), []


def _allowed_stale_trading_days(
    now_local: _dt.datetime,
    *,
    open_dates: list[str],
) -> int:
    """Allow T-1 intraday, but require same-day data after the evening SLA."""
    try:
        deadline_hour, deadline_minute = (int(value) for value in SAME_DAY_DEADLINE.split(":", 1))
        deadline = _dt.time(deadline_hour, deadline_minute)
    except (TypeError, ValueError):
        deadline = _dt.time(18, 30)
    today_text = now_local.strftime("%Y%m%d")
    is_open_day = today_text in open_dates if open_dates else now_local.weekday() < 5
    if is_open_day and now_local.time() >= deadline:
        return 0
    return STALE_TRADING_DAYS


def _trading_days_between(
    start: _dt.date,
    end: _dt.date,
    *,
    open_dates: list[str] | None = None,
) -> int:
    """start~end 之间的交易日数（粗略跳周末），end 含、start 不含。"""
    if end <= start:
        return 0
    if open_dates:
        start_text = start.strftime("%Y%m%d")
        end_text = end.strftime("%Y%m%d")
        return sum(1 for value in open_dates if start_text < value <= end_text)
    n = 0
    d = start
    while d < end:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _parse_yyyymmdd(value) -> _dt.date | None:
    s = "".join(ch for ch in str(value or "") if ch.isdigit())[:8]
    if len(s) != 8:
        return None
    try:
        return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def check_db() -> dict:
    out = {"ok": True, "notes": []}
    if not DB_PATH.exists():
        return {"ok": False, "notes": [f"DB 不存在：{DB_PATH}"]}
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        out["max_recommend_date"] = conn.execute(
            "select max(recommend_date) from recommendation_fact"
        ).fetchone()[0]
        out["max_price_date"] = conn.execute(
            "select max(trade_date) from price_daily_cache"
        ).fetchone()[0]
        row = conn.execute(
            "select run_at, latest_trade_date, ok from sync_runs order by run_at desc limit 1"
        ).fetchone()
        out["last_sync"] = {"run_at": row[0], "latest_trade_date": row[1], "ok": row[2]} if row else None
    finally:
        conn.close()
    return out


def check_manifest() -> dict:
    if not RUN_MANIFEST.exists():
        return {"ok": False, "notes": [f"run_manifest 不存在：{RUN_MANIFEST}"]}
    try:
        doc = json.loads(RUN_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"ok": False, "notes": ["run_manifest 读取失败或内容不完整"]}
    if not isinstance(doc, dict):
        return {"ok": False, "notes": ["run_manifest 不是 JSON 对象"]}
    return {"ok": True, "trade_date": doc.get("trade_date"), "generated_at": doc.get("generated_at"),
            "published": doc.get("published"), "ai_complete": doc.get("ai_complete")}


def check_last_run() -> dict:
    files = sorted(glob.glob(str(HEALTH_RUNS / "stock-*.json")))
    if not files:
        return {"ok": False, "notes": ["无任何运行报告"]}
    latest = files[-1]
    try:
        doc = json.loads(Path(latest).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"ok": False, "notes": ["最近运行报告读取失败或内容不完整"], "run_file": Path(latest).name}
    if not isinstance(doc, dict):
        return {"ok": False, "notes": ["最近运行报告不是 JSON 对象"], "run_file": Path(latest).name}
    pre = doc.get("preflight", {})
    selection_ok = doc.get("selection_ok")
    publish_ok = doc.get("publish_ok")
    if selection_ok is None or publish_ok is None:
        business_ok = bool(doc.get("ok"))
    else:
        business_ok = bool(selection_ok and publish_ok)
    return {
        "ok": business_ok,
        "run_id": str(doc.get("run_id") or ((doc.get("run") or {}).get("run_id")) or ""),
        "trade_date": str(doc.get("trade_date") or ((doc.get("run") or {}).get("trade_date")) or ""),
        "selection_ok": selection_ok,
        "research_ok": doc.get("research_ok"),
        "publish_ok": publish_ok,
        "run_file": Path(latest).name,
        "preflight_ok": pre.get("ok"),
        "preflight_reason": pre.get("reason"),
        "stages_run": len(doc.get("stages", []) or []),
    }


def publish_recovered(last_run: dict, receipt: dict, expected_date: str) -> bool:
    return bool(
        last_run.get("selection_ok") is True
        and receipt.get("remote_confirmed") is True
        and receipt.get("run_id") == last_run.get("run_id")
        and receipt.get("trade_date") == expected_date
        and receipt.get("local_sha")
        and receipt.get("local_sha") == receipt.get("remote_sha")
    )


def check_auxiliary_runs(
    *,
    expected_trade_date: str,
    last_run: dict,
    delivery: dict,
    post_research: dict,
    factory_status: dict,
    now_local: _dt.datetime | None = None,
    running_grace_seconds: int = 5400,
    post_research_due_time: _dt.time | None = None,
) -> list[str]:
    """Check user-visible sidecars that the data freshness check cannot infer."""
    expected = str(expected_trade_date or "")
    findings: list[str] = []
    current_run = str(last_run.get("run_id") or "")
    current_date = str(last_run.get("trade_date") or "")
    now_value = now_local or _dt.datetime.now().astimezone()

    def running_within_grace(payload: dict) -> bool:
        if str(payload.get("state") or "") != "running":
            return False
        started_text = str(payload.get("started_at") or "")
        try:
            started = _dt.datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=now_value.tzinfo)
        return 0 <= (now_value - started.astimezone(now_value.tzinfo)).total_seconds() <= running_grace_seconds

    if last_run.get("ok") is True and last_run.get("publish_ok") is True and current_date == expected:
        if str(delivery.get("run_id") or "") != current_run or str(delivery.get("trade_date") or "") != expected:
            findings.append("当天 AI 播报回执缺失或仍停在旧运行")

        post_matches = (
            str(post_research.get("run_id") or "") == current_run
            and str(post_research.get("trade_date") or "") == expected
        )
        post_running = post_matches and running_within_grace(post_research)
        local_clock = now_value.timetz().replace(tzinfo=None)
        post_is_due = post_research_due_time is None or local_clock >= post_research_due_time
        if (
            (post_is_due or post_matches)
            and not post_running
            and (not post_matches or post_research.get("all_ok") is not True)
        ):
            findings.append("当天后置研究未完成或仍停在旧运行")

        factory_state = str(factory_status.get("state") or "")
        factory_date = str(factory_status.get("trade_date") or "")
        factory_running = factory_date == expected and running_within_grace(factory_status)
        if factory_date == expected and not factory_running and factory_state not in {"", "completed"}:
            findings.append(f"后置工厂状态仍为 {factory_state}，不是最终完成态")
    return findings


def read_optional_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def recover_last_run_if_publish_healed(
    last: dict,
    manifest: dict,
    expected: _dt.date,
    receipt: dict | None = None,
) -> dict:
    """A later successful v2 publish verified by receipt clears the watchdog."""
    if last.get("ok"):
        return last
    if last.get("selection_ok") is False:
        return last
    if last.get("publish_ok") is not False:
        return last
    expected_compact = str(last.get("trade_date") or expected.strftime("%Y%m%d"))

    receipt_data = receipt if receipt is not None else {}
    if receipt is None:
        receipt_file = WORKSPACE / "stock_data" / "03-working" / "health" / "deployment_receipt.json"
        if receipt_file.is_file():
            try:
                receipt_data = json.loads(receipt_file.read_text(encoding="utf-8"))
            except Exception:
                receipt_data = {}

    if publish_recovered(last, receipt_data, expected_compact):
        healed = dict(last)
        healed["ok"] = True
        healed["publish_recovered"] = True
        return healed

    return last


def alerts_disabled(
    *,
    disable_flag: Path | None = None,
    runtime_owner: str | None = None,
) -> bool:
    owner = str(runtime_owner or os.environ.get("STOCK_HEALTH_RUNTIME") or "openclaw").strip().lower()
    flag = disable_flag or (Path.home() / ".openclaw" / "control" / "stock_alerts.disabled")
    return owner != "hermes" and flag.exists()


def format_watchdog_message(report: dict) -> str:
    """User-facing Chinese prompt only. Empty string means stay silent."""
    if report.get("healthy") and report.get("auxiliary_healthy", True):
        return ""
    if report.get("healthy") and not report.get("auxiliary_healthy", True):
        lines = ["⚠️ 【选股附属任务异常】"]
        lines += [f"- {item}" for item in report.get("auxiliary_findings") or []]
        lines.append("主管线、行情与已发布选股结果正常；附属研究异常不影响已发布选股结果。")
        return "\n".join(lines)
    expected = report.get("expected_trading_day") or "未知"
    lines = [
        "⚠️ 【选股数据可能停更】",
        f"今天应有交易日：{expected}",
    ]
    for finding in report.get("stale_findings") or []:
        lines.append(f"- {finding}")
    last = report.get("last_run") or {}
    if not last.get("ok"):
        if last.get("selection_ok") is False:
            lines.append("- 最近一次选股主线没有跑完，当天结果还不能用。")
        elif last.get("publish_ok") is False:
            lines.append("- 最近一次选股已经算出，但当天报告没有发出去。")
        else:
            lines.append("- 最近一次选股运行没有通过。")
    lines.append("请确认收盘管线是否执行、行情和推荐有没有停在旧日期。")
    return "\n".join(lines)


def main() -> int:
    # OpenClaw-side stock alerts kill-switch (Hermes owns runtime now).
    if alerts_disabled():
        return 0

    checked_now = _dt.datetime.now()
    today = checked_now.date()
    expected, open_dates = _expected_trading_day(today)
    allowed_stale_days = _allowed_stale_trading_days(checked_now, open_dates=open_dates)

    # 东财 clist 备用源交叉校验（2026-08-25 新增）：
    # 主管线 DB 停更但备用源显示当天有真实行情时，说明是"管线故障"而非"假日"，
    # 告警文案会更准确；备用源自身失败不影响主判定（best-effort）。
    em_crosscheck: dict = {"ok": None, "note": "skipped"}
    try:
        em_lib_dir = Path(__file__).resolve().parent / "lib"
        if str(em_lib_dir) not in sys.path:
            sys.path.insert(0, str(em_lib_dir))
        from em_quote_source import check_today_data  # noqa: PLC0415

        em_ok, em_info = check_today_data(expected.strftime("%Y%m%d"))
        em_crosscheck = dict(em_info)
        em_crosscheck["ok"] = em_ok
        if not em_ok and em_info.get("reason", "").startswith("remote_failed"):
            em_crosscheck["note"] = "em_source_unavailable"
    except Exception as exc:  # noqa: BLE001
        em_crosscheck = {"ok": None, "note": f"error: {exc}"}

    db = check_db()
    mani = check_manifest()
    last = recover_last_run_if_publish_healed(check_last_run(), mani, expected)
    delivery = read_optional_json(DELIVERY_RECEIPT)
    post_research = read_optional_json(POST_RESEARCH_RUN)
    factory_status = read_optional_json(FACTORY_STATUS)

    stale = []
    for label, value in (
        ("推荐库最新推荐日", db.get("max_recommend_date")),
        ("行情最新日", db.get("max_price_date")),
        ("公开报告交易日", mani.get("trade_date")),
    ):
        d = _parse_yyyymmdd(value)
        lag = _trading_days_between(d, expected, open_dates=open_dates) if d else None
        if d is None:
            stale.append(f"{label}无法识别（当前值：{value!r}）")
        elif lag is not None and lag > allowed_stale_days:
            stale.append(f"{label}还停在 {d.isoformat()}，落后约 {lag} 个交易日（应到 {expected.isoformat()}）")
    auxiliary_findings = check_auxiliary_runs(
        expected_trade_date=expected.strftime("%Y%m%d"),
        last_run=last,
        delivery=delivery,
        post_research=post_research,
        factory_status=factory_status,
        now_local=checked_now.astimezone(),
        post_research_due_time=POST_RESEARCH_DUE_TIME,
    )

    last_run_bad = not last.get("ok")
    healthy = not stale and not last_run_bad

    report = {
        "checked_at": checked_now.isoformat(timespec="seconds"),
        "expected_trading_day": expected.isoformat(),
        "calendar_source": "official_cache" if open_dates else "weekday_fallback",
        "same_day_deadline": SAME_DAY_DEADLINE,
        "post_research_due_time": POST_RESEARCH_DUE_TIME.strftime("%H:%M"),
        "allowed_stale_trading_days": allowed_stale_days,
        "em_crosscheck": em_crosscheck,
        "healthy": healthy,
        "stale_findings": stale,
        "auxiliary_healthy": not auxiliary_findings,
        "auxiliary_findings": auxiliary_findings,
        "db": db,
        "manifest": mani,
        "last_run": last,
        "delivery": delivery,
        "post_research": post_research,
        "factory_status": factory_status,
    }

    try:
        write_json_atomic(STATUS_FILE, report)
    except OSError:
        pass

    message = format_watchdog_message(report)
    if not message:
        return 0
    print(message)
    return 1 if not healthy else 2


if __name__ == "__main__":
    raise SystemExit(main())
