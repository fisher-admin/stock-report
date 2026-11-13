#!/usr/bin/env python
"""Spike-5 全量验收复跑入口.

用法: /Users/fisher/.openclaw/venv/bin/python run_all.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))

MODULES = [
    ("验收1 恒等回放(rtol=0)", "test_a1_identity_replay"),
    ("验收2 欠窗必炸", "test_a2_underflow"),
    ("验收3 低报审计(lookback±1)", "test_a3_lookback_audit"),
    ("验收4 停牌 NaN 不 ffill", "test_a4_suspension"),
    ("验收5 复权 raw/hfq/qfq", "test_a5_adjustment"),
    ("验收6 ties 声明锁定", "test_a6_ties"),
    ("验收7 未注册禁入", "test_a7_registry_gate"),
    ("#52 永久回归", "test_52_regression"),
]


def main():
    import numpy as np
    import pandas as pd
    print(f"python={sys.version.split()[0]} pandas={pd.__version__} numpy={np.__version__}")
    results = []
    all_ok = True
    for label, mod in MODULES:
        suite = unittest.defaultTestLoader.loadTestsFromName(mod)
        runner = unittest.TextTestRunner(verbosity=2)
        print(f"\n===== {label} ({mod}) =====")
        r = runner.run(suite)
        ok = r.wasSuccessful()
        all_ok &= ok
        results.append((label, mod, r.testsRun, len(r.failures), len(r.errors),
                        len(r.skipped), "PASS" if ok else "FAIL"))
    print("\n========== 验收汇总 ==========")
    for label, mod, n, f, e, s, verdict in results:
        print(f"{verdict:4s}  {label:32s} tests={n} failures={f} errors={e} skipped={s}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
