
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
