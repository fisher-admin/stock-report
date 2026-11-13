# 新因子假设：外部项目借鉴批次（2026-08-25）

> **抽样验证结论（2026-08-25，400股×133日，2折 walk-forward）**：
> - H1 `g_max_vol_down_day`：**反向且超正交线**（C2C IC -0.018，t -1.89；vs g_trend_quality_20 ρ=0.33）→ **KILL**（可作反转口径重提）
> - H2 `g_surge_yang_oversold`：**零预测力**（IC ≈0，两折翻号）→ **KILL**
> - H3 `g_pattern_dtw_sim`：400股初筛通过，但 **1000股扩样后信号稀释**（C2C IC 0.028→0.018，t 1.27→0.77；Q5-Q1 t 仅 0.14）。
>   月度分段：3月+0.049 / 6月-0.063 / 7月+0.170 / 8月初-0.33，正IC天数占比 55% —— regime 驱动特征明显，非稳定 alpha。
>   **处置：降级为观察名单**，注册表暂留（正交干净、不占预算），不做全量重建；待面板拉长到 ≥250 日后复测。
>
> 证据：`outputs/greenfield_multifactor/external_borrowing_sample_wf_20260825.{md,json}`

来源：对 `Dzy-HW-XD/a-share-quant-selector`（碗口反弹选股）的策略要素提炼，
经第一性原理重构后转化为三个可回测的因子假设。原始项目工程层级低
（无回测框架/无OOS验证/参数硬编码），只吸收思想，不引代码。

入队路径：`greenfield_multifactor_lib.add_greenfield_factors()` 新增因子列 →
`config/greenfield_multifactor_v1.yaml` 注册 → 走标准 walk-forward + OOS + overfit 验证。

---

## H1. g_max_vol_down_day（最大阴量剔除 → 连续化因子）❌ KILLED

**原始规则**：M 天回顾期内成交量最大的一天若是阴线（收<开），直接剔除。
**连续化重构**：二元过滤无法进入线性组合与 IC 检验，改为连续因子：

```
argmax_vol_day_ret = ret_1d 在 argmax(vol, M窗口) 那一天的取值
g_max_vol_down_day = argmax_vol_day_ret   # 越大越好 = 最大量日是阳线（资金进场）
```

- **假设**：回顾期内最大成交量日收阳（放量上涨）代表资金进场；收阴代表出货。
  最大量日收益越负，后续 5-10 日收益越差。
- **家族**：volume_structure
- **窗口**：M=10（与原项目一致），可加 M=20 对照
- **预期方向**：正（IC > 0）
- **风险**：与 f_volume_ratio_5、f_amount_zscore_20 存在相关性，需过正交检验
  （|rho| < 0.30 对 prebreakout_v41 基准）

## H2. g_surge_yang_oversold（放量阳线 + KDJ J值低位）❌ KILLED

**原始规则**：近 M 天存在放量阳线（vol >= 前日*N倍 且 收>开）且 J <= 阈值。
**连续化重构**：事件哑变量 × 超卖深度：

```
surge_yang = max over M窗口 of [ vol_ratio_1d * 1{ret_intraday>0} ]   # 放量阳线强度
kdj_j = 3*K - 2*D（标准 KDJ(9,3,3)）
g_surge_yang_oversold = z(surge_yang) * (-z(kdj_j))   # 放量强 且 J 低 → 高分
```

- **假设**：短期放量上攻后回踩到超卖区，是"洗盘而非出货"的信号，
  后续反弹概率高于无量超卖。
- **家族**：volume_reversal
- **窗口**：M=5；KDJ(9,3,3)
- **预期方向**：正
- **风险**：J 值在小盘股噪声大；需按市值分组检验稳健性

## H3. g_pattern_dtw_sim（DTW 形态相似度因子）✅ KEPT — 唯一通过初筛

**原始规则**：10 个历史成功案例模板，四维加权（双线30%/量比25%/DTW形态25%/KDJ20%）。
**连续化重构**：去掉主观模板，保留纯价格形态维度，改为"与自身历史上突破前形态的自相似度"：

```
窗口 W=20 日归一化价格曲线（除以窗口首日价）
g_pattern_dtw_sim = -DTW(curve_t-19..t, curve_t-39..t-20) / W
  # 当前20日形态与前一20日形态的 DTW 距离，越小=形态复制/整理越规整
```

- **假设**：形态自相似（平台整理、规则震荡）后的突破延续性强于杂乱波动。
  这保留了原项目"形态匹配有预测力"的核心直觉，同时避免 10 样本模板的过拟合。
- **家族**：pattern_similarity
- **窗口**：W=20，步长 20；用 fastdtw 或自实现 O(W^2)（W=20 可接受）
- **预期方向**：负相关（DTW 距离小 → 收益高），实现时取负号使 IC 方向为正
- **风险**：计算量 O(N×W^2)，全市场×全历史需分块缓存；先在抽样 universe 验证

---

## 验证计划（统一）

1. 实现：`greenfield_multifactor_lib.add_greenfield_factors()` 追加三列
2. 注册：`config/greenfield_multifactor_v1.yaml` backtestable_factors 追加三条（family 如上）
3. 跑 `build_greenfield_multifactor_pool.py` 重建面板
4. 验收标准（与现有 greenfield 因子一致）：
   - walk-forward IC 均值与 t-stat 达到现有因子中位数水平
   - 对 prebreakout_v41 |rho| < 0.30
   - OOS 无衰减、overfit 审计通过
5. 产出：新面板 parquet + 因子诊断 JSON + 对照报告
