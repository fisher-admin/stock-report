#!/usr/bin/env python3
"""回归测试：strategy_publication_layer.compute_strategy_gate 统一硬门槛（#7）。

任一【可评估且不达标】项（AI覆盖率/数据新鲜度/复盘样本数/样本外ICIR/净超额）→ research_only=True，
该策略只显示「研究观察」、main/买入降级为 watch。未计算的项记 n/a、不阻断但如实公示缺口。

运行：python3 test_strategy_gate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import strategy_publication_layer as spl  # noqa: E402


def _rows(n_ai: int, n_total: int = 20) -> list[dict]:
    return [{"ai_score": 70 if i < n_ai else None} for i in range(n_total)]


def main() -> int:
    rv = {"date_stats": [{}] * 8}      # 复盘样本充足
    rv_few = {"date_stats": [{}] * 1}  # 复盘样本不足

    # 1) 健康策略（无 OOS gate）：AI 18/20、数据新鲜、复盘 8 → tradeable，oos 记 n/a 不阻断
    g = spl.compute_strategy_gate("prebreakout_v41", _rows(18), rv, "20260618", "20260618")
    assert g["verdict"] == "tradeable", g
    assert g["gates"]["oos_icir"]["status"] == "n/a"

    # 2) AI 覆盖率 0 → research_only
    g = spl.compute_strategy_gate("prebreakout_v41", _rows(0), rv, "20260618", "20260618")
    assert g["research_only"] and "ai_coverage" in g["failed_gates"], g

    # 3) 数据陈旧 → research_only
    g = spl.compute_strategy_gate("greenfield_o2c_v1", _rows(18), rv, "20260616", "20260618")
    assert "data_freshness" in g["failed_gates"], g

    # 4) 复盘样本不足 → research_only
    g = spl.compute_strategy_gate("greenfield_o2c_v1", _rows(18), rv_few, "20260618", "20260618")
    assert "review_samples" in g["failed_gates"], g

    # 5) 样本外不达标（ICIR<0.3 且净超额<0）→ research_only
    sg = {"oos_icir": 0.1651, "top20_net_excess_mean_daily": -0.0018}
    g = spl.compute_strategy_gate("t1_factor_v1", _rows(18), rv, "20260618", "20260618", signal_gate=sg)
    assert "oos_icir" in g["failed_gates"] and "net_excess" in g["failed_gates"], g

    # 5b) 方向必须为正：负 ICIR 即使绝对值大也不能通过
    sg_neg = {"oos_icir": -0.42, "top20_net_excess_mean_daily": 0.001}
    g = spl.compute_strategy_gate("t1_factor_v1", _rows(18), rv, "20260618", "20260618", signal_gate=sg_neg)
    assert "oos_icir" in g["failed_gates"], g

    # 5c) 边界值 0.30 也不能通过
    sg_edge = {"oos_icir": 0.30, "top20_net_excess_mean_daily": 0.001}
    g = spl.compute_strategy_gate("t1_factor_v1", _rows(18), rv, "20260618", "20260618", signal_gate=sg_edge)
    assert "oos_icir" in g["failed_gates"], g

    # 6) 样本外达标 + 其它全过 → tradeable
    sg2 = {"oos_icir": 0.42, "top20_net_excess_mean_daily": 0.001}
    g = spl.compute_strategy_gate("t1_factor_v1", _rows(18), rv, "20260618", "20260618", signal_gate=sg2)
    assert g["verdict"] == "tradeable", g

    # 7) 多项同时不达标 → 全部进 failed_gates
    g = spl.compute_strategy_gate("greenfield_o2c_v1", _rows(0), rv_few, "20260616", "20260618")
    assert set(["ai_coverage", "data_freshness", "review_samples"]).issubset(set(g["failed_gates"])), g

    print("compute_strategy_gate 硬门槛回归测试全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
