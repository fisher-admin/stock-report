"""验收4: 三列报告 (锁生效日/被挡推荐/后验超额) 覆盖>=60交易日台账, 逐条可溯源 reason_code."""
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(HERE, "out", "run1")


def main():
    ok = True
    md = open(os.path.join(RUN, "shadow_report.md"), encoding="utf-8").read()
    # 重放覆盖交易日数
    import re
    m = re.search(r"重放交易日: (\d+)", md)
    n_days = int(m.group(1))
    print(f"check1 重放交易日覆盖: {n_days} (>=60: {n_days >= 60})")
    ok &= n_days >= 60

    with open(os.path.join(RUN, "blocked_recommendations.csv"), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"check2 被挡推荐行数: {len(rows)}")
    ok &= len(rows) > 0
    # 三列齐备
    need = {"lock_effective_date", "blocked_rec", "posthoc_excess_5d"}
    have = set(rows[0].keys())
    print(f"check3 三列齐备: {need <= have} (列: {sorted(have)})")
    ok &= need <= have
    # 逐条溯源: reason_code 非空 + created_by 指向规则 + lock_id 关联 locks_fired
    with open(os.path.join(RUN, "locks_fired.csv"), encoding="utf-8") as f:
        lock_ids = {r["lock_id"] for r in csv.DictReader(f)}
    bad = [r for r in rows if not r["reason_code"] or not r["created_by"].startswith("protection:")
           or r["lock_id"] not in lock_ids]
    print(f"check4 逐条溯源 (reason_code非空+created_by=protection:*+lock_id可关联): "
          f"违例 {len(bad)}/{len(rows)}")
    ok &= len(bad) == 0
    reasons = sorted({r["reason_code"] for r in rows})
    print(f"check5 出现的 reason_code: {reasons}")
    print("ACCEPT-4:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
