"""strategy_locks 表 + LockStore 协议 + Mem/Db 双实现 (spike4).

规格来源: deep/freqtrade_protections.md §3.1 表 schema / §3.2 冻结边界语义 / §3.3 接口形状.
纪律:
- GPL-3.0: 全部自写, 零 freqtrade 代码复制; 语义表 §2 为行为基准.
- 修 freqtrade pairlock_middleware 的类属性单例反模式 (§1 判决表): LockStore 为协议,
  MemLockStore/DbLockStore 构造注入, 实例态, 无任何类级可变状态.
- sqlite 文件独立于 recommendations.db (影子/spike 期不碰生产库).

边界语义 (对照 §2 语义表, 标 † 为显式反转):
- S1 锁区间左闭右开: is_locked = active ∧ (lock_end_date > as_of ∨
  (lock_end_date == as_of ∧ phase 先于 unlock_at_phase)). 解锁日该相位起即可用.
- S2/S5 日历版: 解锁点只对齐交易日相位边界; 落在非交易日顺延 (材料化在 materialize_unlock).
- S4/S6: end 锚定触发事件日, 重评估不延长; 判定取最长 (longest wins).
- O1 去重: 同 (scope, scope_key, reason_code, lock_end_date, unlock_phase, side) 不重复落库.
- O2 吸收: 提议锁被现有严格更晚 end 的同域锁覆盖 → 吸收不落新锁.
- O3: global 锁抑制同义 strategy/symbol 锁.
- G4 我方版: 查询 side='entry' 命中 {'entry','*'}; 查询 side='*' 只命中 '*'.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional, Protocol

from calendar_util import TradingCalendar

# --- 相位序 (日内管线相位: 开盘前发布闸 -> 收盘 -> 复盘后) ---
PHASE_ORDER = {"open": 0, "close": 1, "after_review": 2}
# unlock_at_phase 存储值 -> 解锁日相位
UNLOCK_PHASE_MAP = {"next_open": "open", "next_close": "close", "after_review": "after_review"}

VALID_SCOPES = ("strategy", "symbol", "global")
VALID_SIDES = ("entry", "exit_blocked", "*")
VALID_UNLOCK_TYPES = ("trading_days", "at_phase", "condition", "manual")

REASON_CODES = (
    "CONSECUTIVE_LOSS", "DRAWDOWN_BRAKE", "LOW_EDGE", "POST_FAILURE_COOLDOWN",
    "LIMIT_DOWN_SEALED", "LIMIT_DOWN_INTRADAY", "SUSPENDED", "MARKET_STRESS",
    "MANUAL", "AUTO_DEDUP_DAILY",
)


class LockConfigError(ValueError):
    """配置 fail-fast (§3.2 条10)."""


# --- strategy_locks 表 DDL (§3.1: 16 列 + 2 索引; 规格表把 unlock_time/unlock_by 并作一行故称15字段) ---
STRATEGY_LOCKS_DDL = """
CREATE TABLE IF NOT EXISTS strategy_locks (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    scope                 TEXT NOT NULL CHECK (scope IN ('strategy','symbol','global')),
    scope_key             TEXT NOT NULL,
    side                  TEXT NOT NULL DEFAULT 'entry'
                          CHECK (side IN ('entry','exit_blocked','*')),
    reason_code           TEXT NOT NULL,
    reason_detail         TEXT,
    lock_time             TEXT NOT NULL,            -- ISO8601 显式时区 +08:00 (修 tz-naive 坑)
    unlock_condition_type TEXT NOT NULL
                          CHECK (unlock_condition_type IN ('trading_days','at_phase','condition','manual')),
    unlock_after_days     INTEGER,
    unlock_at_phase       TEXT,
    unlock_condition      TEXT,
    lock_end_date         TEXT,                     -- 物化解锁交易日 YYYYMMDD
    active                INTEGER NOT NULL DEFAULT 1,
    created_by            TEXT NOT NULL,
    unlock_time           TEXT,
    unlock_by             TEXT
);
CREATE INDEX IF NOT EXISTS idx_locks_scope ON strategy_locks (scope, scope_key, active);
CREATE INDEX IF NOT EXISTS idx_locks_end ON strategy_locks (lock_end_date, active);
"""


@dataclass
class Lock:
    scope: str
    scope_key: str
    side: str
    reason_code: str
    reason_detail: str
    lock_time: str
    unlock_condition_type: str
    unlock_after_days: Optional[int]
    unlock_at_phase: str            # 'next_open' / 'next_close' / 'after_review'
    unlock_condition: Optional[str]
    lock_end_date: str              # 物化解锁交易日
    active: int = 1
    created_by: str = ""
    unlock_time: Optional[str] = None
    unlock_by: Optional[str] = None
    id: Optional[int] = None

    def end_key(self) -> tuple:
        """锁截止序键: (解锁日, 解锁相位序). 用于比长短 (S6)."""
        return (self.lock_end_date, PHASE_ORDER[UNLOCK_PHASE_MAP[self.unlock_at_phase]])


@dataclass
class UnlockSpec:
    """恢复条件 (§3.1 unlock_* 字段的入参形态)."""
    type: str                       # trading_days / at_phase / condition / manual
    anchor_day: str                 # 触发事件日 (S4: end 锚定事件, 不锚定评估时刻)
    after_days: Optional[int] = None
    at_phase: str = "next_open"
    condition: Optional[str] = None

    def validate(self):
        if self.type not in VALID_UNLOCK_TYPES:
            raise LockConfigError(f"unlock type invalid: {self.type}")
        if self.at_phase not in UNLOCK_PHASE_MAP:
            raise LockConfigError(f"unlock phase invalid: {self.at_phase}")
        # 互斥参数 fail-fast (§3.2 条10 / T6 同族)
        if self.type == "trading_days":
            if not self.after_days or self.after_days < 1:
                raise LockConfigError("trading_days unlock requires after_days>=1")
            if self.condition:
                raise LockConfigError("trading_days unlock must not carry condition")
        elif self.type == "at_phase":
            if self.after_days:
                raise LockConfigError("at_phase unlock must not carry after_days")
        elif self.type == "condition":
            if not self.condition:
                raise LockConfigError("condition unlock requires condition expr")


def materialize_unlock(spec: UnlockSpec, cal: TradingCalendar) -> tuple[str, str]:
    """UnlockSpec -> (lock_end_date, unlock_at_phase). S2/S5 日历版: 非交易日顺延."""
    spec.validate()
    if spec.type == "trading_days":
        end = cal.add_trading_days(spec.anchor_day, spec.after_days)
        return end, spec.at_phase
    if spec.type == "at_phase":
        # 锁到下一交易日的指定相位 (DrawdownBrake: after_review 复盘过后)
        end = cal.next_trading_day(spec.anchor_day)
        return end, spec.at_phase
    if spec.type == "condition":
        # 条件式: 无确定 end, 物化为远期哨兵日, 由条件评估器解锁 (spike4 影子期不实现评估器)
        return "99991231", spec.at_phase
    # manual
    return "99991231", spec.at_phase


def _phase_rank(phase: str) -> int:
    if phase not in PHASE_ORDER:
        raise LockConfigError(f"invalid query phase: {phase}")
    return PHASE_ORDER[phase]


def _lock_active_at(lk: Lock, as_of_day: str, phase: str) -> bool:
    """S1 左闭右开: 解锁日该相位起即可用."""
    if not lk.active:
        return False
    if lk.lock_end_date > as_of_day:
        return True
    if lk.lock_end_date == as_of_day:
        return _phase_rank(phase) < PHASE_ORDER[UNLOCK_PHASE_MAP[lk.unlock_at_phase]]
    return False


def _side_matches(query_side: str, lock_side: str) -> bool:
    """G4 我方版: 查具体 side 命中 {该侧,'*'}; 查 '*' 只命中 '*'."""
    if query_side == "*":
        return lock_side == "*"
    return lock_side in (query_side, "*")


class LockStore(Protocol):
    def lock(self, scope: str, scope_key: str, side: str, reason_code: str,
             detail: str, unlock_spec: UnlockSpec, now: str, created_by: str) -> Lock: ...
    def is_locked(self, scope_key: str, as_of_day: str, phase: str, side: str = "entry") -> bool: ...
    def longest_lock(self, scope_key: str, as_of_day: str, phase: str = "open") -> Optional[Lock]: ...
    def unlock_by_reason(self, reason_code: str, now: str, by: str) -> int: ...
    def active_locks(self, as_of_day: str, phase: str = "open") -> list[Lock]: ...


class _BaseLockStore:
    """去重/吸收/判定逻辑共用; 存取原语由子类提供 (双实现语义一致的结构保证)."""

    def __init__(self, calendar: TradingCalendar):
        self._cal = calendar  # 实例态注入, 无类属性单例

    # -- 子类原语 --
    def _iter_locks(self) -> Iterable[Lock]:
        raise NotImplementedError

    def _insert(self, lk: Lock) -> Lock:
        raise NotImplementedError

    def _deactivate(self, lk: Lock, now: str, by: str) -> None:
        raise NotImplementedError

    # -- 协议实现 --
    def lock(self, scope: str, scope_key: str, side: str, reason_code: str,
             detail: str, unlock_spec: UnlockSpec, now: str, created_by: str) -> Lock:
        if scope not in VALID_SCOPES:
            raise LockConfigError(f"invalid scope: {scope}")
        if side not in VALID_SIDES:
            raise LockConfigError(f"invalid side: {side}")
        if reason_code not in REASON_CODES:
            raise LockConfigError(f"invalid reason_code: {reason_code}")
        if scope == "global" and scope_key != "*":
            raise LockConfigError("global scope requires scope_key='*'")
        end_date, end_phase = materialize_unlock(unlock_spec, self._cal)
        proposed_key = (end_date, PHASE_ORDER[UNLOCK_PHASE_MAP[end_phase]])

        for existing in self._iter_locks():
            if not existing.active:
                continue
            same_target = (existing.scope == scope and existing.scope_key == scope_key
                           and existing.side == side)
            # O1 去重: 同域同因同 end 同侧 -> 返回已有锁
            if (same_target and existing.reason_code == reason_code
                    and existing.end_key() == proposed_key):
                return existing
            # O2 吸收: 现有同域锁 end 严格晚于提议 end -> 吸收
            if same_target and existing.end_key() > proposed_key:
                return existing
            # O3: global 锁在提议 end 时点仍活跃 (严格>, 对齐 O2 语义) -> 吸收
            if (scope != "global" and existing.scope == "global"
                    and _side_matches(side, existing.side)
                    and existing.end_key() > proposed_key):
                return existing

        lk = Lock(scope=scope, scope_key=scope_key, side=side, reason_code=reason_code,
                  reason_detail=detail, lock_time=now,
                  unlock_condition_type=unlock_spec.type,
                  unlock_after_days=unlock_spec.after_days,
                  unlock_at_phase=end_phase, unlock_condition=unlock_spec.condition,
                  lock_end_date=end_date, active=1, created_by=created_by)
        return self._insert(lk)

    def is_locked(self, scope_key: str, as_of_day: str, phase: str, side: str = "entry") -> bool:
        """自身 ∨ global (G1/O3)."""
        for lk in self._iter_locks():
            if not _lock_active_at(lk, as_of_day, phase):
                continue
            if not _side_matches(side, lk.side):
                continue
            if lk.scope == "global" or lk.scope_key == scope_key:
                return True
        return False

    def longest_lock(self, scope_key: str, as_of_day: str, phase: str = "open") -> Optional[Lock]:
        cands = [lk for lk in self._iter_locks()
                 if _lock_active_at(lk, as_of_day, phase)
                 and (lk.scope == "global" or lk.scope_key == scope_key)]
        if not cands:
            return None
        return max(cands, key=lambda lk: lk.end_key())

    def unlock_by_reason(self, reason_code: str, now: str, by: str) -> int:
        n = 0
        for lk in list(self._iter_locks()):
            if lk.active and lk.reason_code == reason_code:
                self._deactivate(lk, now, by)
                n += 1
        return n

    def active_locks(self, as_of_day: str, phase: str = "open") -> list[Lock]:
        out = [lk for lk in self._iter_locks() if _lock_active_at(lk, as_of_day, phase)]
        out.sort(key=lambda lk: (lk.scope, lk.scope_key, lk.reason_code, lk.lock_end_date, lk.side))
        return out


class MemLockStore(_BaseLockStore):
    """影子回放用: 纯内存, 进程结束零残留."""

    def __init__(self, calendar: TradingCalendar):
        super().__init__(calendar)
        self._locks: list[Lock] = []   # 实例态
        self._next_id = 1

    def _iter_locks(self):
        return list(self._locks)

    def _insert(self, lk: Lock) -> Lock:
        lk = replace(lk, id=self._next_id)
        self._next_id += 1
        self._locks.append(lk)
        return lk

    def _deactivate(self, lk: Lock, now: str, by: str) -> None:
        for i, cur in enumerate(self._locks):
            if cur.id == lk.id:
                self._locks[i] = replace(cur, active=0, unlock_time=now, unlock_by=by)
                return


_COLS = ("scope", "scope_key", "side", "reason_code", "reason_detail", "lock_time",
         "unlock_condition_type", "unlock_after_days", "unlock_at_phase",
         "unlock_condition", "lock_end_date", "active", "created_by",
         "unlock_time", "unlock_by")


class DbLockStore(_BaseLockStore):
    """生产形态: 独立 sqlite 文件 (不在 recommendations.db 内). 每次操作即时 commit (崩溃可恢复)."""

    def __init__(self, db_path: str, calendar: TradingCalendar):
        super().__init__(calendar)
        self._path = db_path
        self._con = sqlite3.connect(db_path)
        self._con.executescript(STRATEGY_LOCKS_DDL)
        self._con.commit()

    def close(self):
        self._con.close()

    def _iter_locks(self):
        cur = self._con.execute(
            f"SELECT id, {', '.join(_COLS)} FROM strategy_locks ORDER BY id")
        for row in cur.fetchall():
            kw = dict(zip(("id",) + _COLS, row))
            yield Lock(**kw)

    def _insert(self, lk: Lock) -> Lock:
        vals = [getattr(lk, c) for c in _COLS]
        cur = self._con.execute(
            f"INSERT INTO strategy_locks ({', '.join(_COLS)}) "
            f"VALUES ({', '.join('?' * len(_COLS))})", vals)
        self._con.commit()
        return replace(lk, id=cur.lastrowid)

    def _deactivate(self, lk: Lock, now: str, by: str) -> None:
        self._con.execute(
            "UPDATE strategy_locks SET active=0, unlock_time=?, unlock_by=? WHERE id=?",
            (now, by, lk.id))
        self._con.commit()
