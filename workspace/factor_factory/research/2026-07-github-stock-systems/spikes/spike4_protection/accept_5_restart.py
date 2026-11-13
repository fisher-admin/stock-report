"""验收5: DbLockStore 重启恢复 — 落锁 -> SIGKILL 杀进程 -> 新进程判定一致.

流程: 子进程打开 DbLockStore(独立 sqlite 文件), 落 3 条锁 (strategy/global/相位锁),
写出判定快照后进入 sleep; 父进程 SIGKILL (-9) 硬杀; 父进程新开 DbLockStore
对同一批 (scope_key, day, phase) 重算判定, 与子进程快照逐项对比.
"""
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "out", "restart_locks.db")
SNAP = os.path.join(HERE, "out", "restart_child_snapshot.json")

QUERIES = [
    ("stratA", "20260310", "open", "entry"),
    ("stratA", "20260311", "open", "entry"),
    ("stratA", "20260312", "open", "entry"),
    ("anything", "20260312", "open", "entry"),   # global 覆盖
    ("anything", "20260313", "open", "entry"),
    ("stratB", "20260310", "open", "entry"),
    ("stratA", "20260311", "close", "entry"),
]

CHILD = r"""
import json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))          # = out/
sys.path.insert(0, os.path.dirname(HERE))                   # spike 根目录
from calendar_util import TradingCalendar
from lock_store import DbLockStore, UnlockSpec

DAYS = ["20260309","20260310","20260311","20260312","20260313","20260316"]
cal = TradingCalendar(DAYS)
store = DbLockStore(sys.argv[1], cal)
now = "20260309T20:45:00+08:00"
store.lock("strategy","stratA","entry","CONSECUTIVE_LOSS","child",
           UnlockSpec(type="trading_days", anchor_day="20260309", after_days=2), now, "test")
store.lock("global","*","entry","DRAWDOWN_BRAKE","child",
           UnlockSpec(type="trading_days", anchor_day="20260309", after_days=3,
                      at_phase="next_close"), now, "test")
queries = json.loads(sys.argv[2])
snap = {"|".join(q): store.is_locked(q[0], q[1], q[2], q[3]) for q in queries}
with open(sys.argv[3], "w") as f:
    json.dump(snap, f, sort_keys=True)
print("READY", flush=True)
time.sleep(300)   # 等着被 SIGKILL, 不走正常关闭路径
"""


def main():
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    for p in (DB_PATH, SNAP, DB_PATH + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    child_py = os.path.join(HERE, "out", "_restart_child.py")
    with open(child_py, "w") as f:
        f.write(CHILD)
    proc = subprocess.Popen(
        [sys.executable, child_py, DB_PATH, json.dumps(QUERIES), SNAP],
        stdout=subprocess.PIPE, text=True, cwd=HERE)
    line = proc.stdout.readline().strip()
    assert line == "READY", f"child not ready: {line!r}"
    os.kill(proc.pid, signal.SIGKILL)   # 硬杀: 不给清理机会
    proc.wait()
    print(f"child pid={proc.pid} SIGKILLed, returncode={proc.returncode}")
    assert proc.returncode == -signal.SIGKILL

    # 新进程 (本进程即是) 重开同一 DB 文件
    sys.path.insert(0, HERE)
    from calendar_util import TradingCalendar
    from lock_store import DbLockStore
    cal = TradingCalendar(["20260309", "20260310", "20260311", "20260312", "20260313", "20260316"])
    store2 = DbLockStore(DB_PATH, cal)
    with open(SNAP) as f:
        child_snap = json.load(f)
    ok = True
    for q in QUERIES:
        key = "|".join(q)
        got = store2.is_locked(q[0], q[1], q[2], q[3])
        same = got == child_snap[key]
        print(f"{key}: child={child_snap[key]} restarted={got} {'OK' if same else 'MISMATCH'}")
        ok = ok and same
    n_locks = len(store2.active_locks("20260309"))
    print(f"重启后可见锁记录: {n_locks} (期望 2)")
    ok = ok and n_locks == 2
    store2.close()
    print("ACCEPT-5:", "PASS (杀进程重启判定一致)" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
