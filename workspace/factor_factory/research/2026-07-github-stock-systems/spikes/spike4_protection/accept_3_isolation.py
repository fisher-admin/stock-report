"""验收3: 影子运行与生产 DB 零共享状态 — MemLockStore 注入, 进程结束无残留文件.

三重检查:
  ① 影子引擎跑全量 (不写报告) 前后, spike 目录递归文件清单逐项不变 (零残留);
  ② 生产 recommendations.db 文件 (size, mtime_ns) 不变;
  ③ ro 连接上尝试写入必须抛 OperationalError (只读挂载有效);
  ④ MemLockStore 无文件后端属性 (纯内存).
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DB = "/Users/fisher/.openclaw/workspace/stock_data/03-working/recommendation_warehouse/recommendations.db"


def dir_snapshot(root):
    snap = {}
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            st = os.stat(p)
            snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return snap


def main():
    ok = True
    st0 = os.stat(DB)
    before = dir_snapshot(HERE)

    from shadow_replay import run_shadow
    res = run_shadow(None)   # no-write: 只跑引擎
    store = res["store"]

    after = dir_snapshot(HERE)
    new_files = sorted(set(after) - set(before))
    changed = sorted(k for k in before if k in after and before[k] != after[k]
                     and not k.endswith(".pyc") and "__pycache__" not in k)
    new_files = [f for f in new_files if "__pycache__" not in f]
    print(f"check1 新增文件(排除pycache): {new_files}")
    print(f"check1 变更文件(排除pycache): {changed}")
    ok &= (new_files == [] and changed == [])

    st1 = os.stat(DB)
    print(f"check2 生产DB (size,mtime_ns): before=({st0.st_size},{st0.st_mtime_ns}) "
          f"after=({st1.st_size},{st1.st_mtime_ns}) 不变={st0.st_size == st1.st_size and st0.st_mtime_ns == st1.st_mtime_ns}")
    ok &= (st0.st_size == st1.st_size and st0.st_mtime_ns == st1.st_mtime_ns)

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        con.execute("CREATE TABLE _spike4_probe (x INTEGER)")
        print("check3 ro 连接写入未被拒 -> FAIL")
        ok = False
    except sqlite3.OperationalError as e:
        print(f"check3 ro 连接写入被拒 (OperationalError: {e}) -> OK")
    finally:
        con.close()

    no_file_backend = not hasattr(store, "_con") and not hasattr(store, "_path")
    print(f"check4 MemLockStore 无文件后端属性: {no_file_backend} "
          f"(锁在内存: {len(store._locks)} 条)")
    ok &= no_file_backend

    print("ACCEPT-3:", "PASS (零残留/零共享)" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
