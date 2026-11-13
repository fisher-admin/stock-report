"""验收6 ties: cs_rank 并列值语义在注册表声明(average)并以手算金值锁定;
换 pandas 版本重跑值不变(本文件不依赖 parquet/数据文件, 可在任意 venv 直跑).

金值即锁: 期望值为手工推导常数, 任何 pandas 版本下 rank 语义漂移都会使本测试失败。
复跑(主 venv):   /Users/fisher/.openclaw/venv/bin/python -m unittest tests.test_a6_ties -v
复跑(异版本 venv): <其它venv>/bin/python -m unittest tests.test_a6_ties -v  (见 scripts/run_ties_cross_version.sh)
"""
import sys
import unittest

import numpy as np
import pandas as pd

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parity_lib.factors import CS_RANK_TIES, f_cs_rank_ret5  # noqa: E402
from parity_lib.panel import Panel                            # noqa: E402
from parity_lib.registry import get_spec                      # noqa: E402

SESSIONS = ["20260601", "20260602", "20260603", "20260604", "20260605",
            "20260608", "20260609"]
CODES = ["A1", "B2", "C3", "D4", "E5", "F6"]


def _panel_with_ties() -> Panel:
    """[合成样本, 已注明] 构造 20260609 截面 ret5 = [0.10, 0.20, 0.20, 0.30, 0.20, NaN]。"""
    base = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0])
    last = np.array([11.0, 12.0, 12.0, 13.0, 12.0, np.nan])
    rows = [base] * 6 + [last]
    close = pd.DataFrame(rows, index=SESSIONS, columns=CODES, dtype=float)
    close.index.name = "trade_date"
    return Panel({"close": close}, SESSIONS, SESSIONS[0], "20260609", "20260609")


class TestTiesLocked(unittest.TestCase):

    def test_ties_declared_in_registry(self):
        self.assertEqual(get_spec("cs_rank_ret5").ties, "average")
        self.assertEqual(CS_RANK_TIES, "average")

    def test_golden_values_average(self):
        """手算金值: 5 个有效值 [0.10, 0.20×3, 0.30], average+pct
        => [1/5, 3/5, 3/5, 5/5, 3/5, NaN]。"""
        out = f_cs_rank_ret5(_panel_with_ties()).loc["20260609"]
        expected = np.array([1 / 5, 3 / 5, 3 / 5, 5 / 5, 3 / 5, np.nan])
        got = out.to_numpy(dtype=float)
        for i, (g, e) in enumerate(zip(got, expected)):
            if np.isnan(e):
                self.assertTrue(np.isnan(g), f"{CODES[i]}: NaN(停牌/无值)不参与排位")
            else:
                self.assertEqual(g, e, f"{CODES[i]}: average-pct 金值不符 (pandas={pd.__version__})")

    def test_alternative_semantics_differ(self):
        """对照: 若语义换成 min, 并列组值=2/5 != 3/5 —— 证明声明锁定是有内容的。"""
        panel = _panel_with_ties()
        c = panel.field("close")
        ret5 = c / c.shift(5) - 1.0
        min_rank = ret5.rank(axis=1, method="min", pct=True).loc["20260609"]
        self.assertEqual(min_rank.iloc[1], 2 / 5)
        self.assertNotEqual(min_rank.iloc[1], 3 / 5)


if __name__ == "__main__":
    print(f"python={sys.version.split()[0]} pandas={pd.__version__} numpy={np.__version__}")
    unittest.main(verbosity=2)
