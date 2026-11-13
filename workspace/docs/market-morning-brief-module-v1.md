# 市场晨判模块设计 v1

更新时间：2026-03-21 00:56 Asia/Shanghai
负责人：小小鱼

## 1. 目标

在每天早上 08:00（Asia/Shanghai）之前，生成一份“市场晨判”结果，用于判断：

- 当日开盘前整体风险级别
- 隔夜外围冲击是否足以影响 A 股开盘
- 哪些行业/风格需要重点观察
- 今日是否适合进攻、谨慎或防守

这不是单一的 A50 指标展示，而是一个“隔夜市场预判器”。

---

## 2. 输出对象

建议产物：

- `stock_data/03-working/market_brief/market_morning_brief_YYYYMMDD.json`
- 页面展示块（后续挂到市场驾驶舱首页顶部）
- 频道播报块（可选）

建议核心字段：

- `trade_date`
- `generated_at`
- `overnight_regime`（偏积极 / 中性 / 谨慎 / 高风险）
- `overnight_risk_score`（0-100）
- `a50_change_pct`
- `golden_dragon_change_pct`
- `us_index_summary`
- `cnh_change_pct`
- `offset_score`
- `offset_reason`
- `key_drivers[]`
- `focus_sectors[]`
- `opening_playbook[]`

---

## 3. 核心输入项

### 3.1 主要风险输入

- 富时 A50 夜盘涨跌幅
- 纳斯达克中国金龙指数涨跌幅
- 美股三大指数收盘表现
- 离岸人民币（CNH）变化
- 隔夜重大新闻 / 政策 / 黑天鹅事件

### 3.2 可选增强项

- 美债收益率变化
- 原油 / 黄金大幅波动
- 重要中概龙头涨跌（阿里、腾讯 ADR、拼多多、京东等）
- 大宗商品与资源股相关价格波动

---

## 4. v1 规则框架

### 4.1 高风险预警

触发建议：

- `a50_change_pct <= -1.0`
- 且 `golden_dragon_change_pct <= -2.0` 或 `cnh_change_pct` 明显走弱
- 且无明确政策利好 / 数据利好对冲

输出：

- `overnight_regime = 高风险`
- `overnight_risk_score >= 75`

### 4.2 谨慎观察

触发建议：

- `a50_change_pct` 在 `-0.5% ~ -1.0%`
- 或外围风险资产整体偏弱
- 或有不确定利空但尚未形成一致性冲击

输出：

- `overnight_regime = 谨慎`
- `overnight_risk_score 55~74`

### 4.3 中性

触发建议：

- A50、金龙、汇率均无明显方向性冲击
- 无重大利空 / 利好

输出：

- `overnight_regime = 中性`
- `overnight_risk_score 35~54`

### 4.4 偏积极

触发建议：

- `a50_change_pct >= +0.8%`
- 且金龙同步偏强
- 或有明确政策 / 数据利好对冲

输出：

- `overnight_regime = 偏积极`
- `overnight_risk_score < 35`

---

## 5. 利好对冲定义

### 可计入对冲

- 明确政策利好
- 降准 / 降息 / 稳市场表态
- 产业扶持政策
- 明显超预期经济数据
- 海外风险明显缓和
- 汇率回稳 + 中概同步转强

### 不计入对冲

- 模糊传闻
- 二手消息
- 情绪喊话
- 无证据的自媒体预期

建议字段：

- `offset_score`
- `offset_reason`
- `offset_confidence`

---

## 6. 输出解释模板

### 标准输出结构

- 隔夜风险等级
- 关键驱动因素（3 条）
- 重点观察行业（3-5 个）
- 开盘应对建议（3 条）

### 示例

- 隔夜风险等级：高风险
- 驱动：
  - A50 -1.23%
  - 金龙 -2.80%
  - CNH 走弱，且无政策对冲
- 重点观察：银行、红利、黄金、防御板块
- 开盘建议：
  - 前 30 分钟不追高
  - 等待市场主线确认
  - 高弹性题材降仓观察

---

## 7. 与新系统的关系

市场晨判模块属于新系统的“市场层”第一组件，后续将接入：

- 市场驾驶舱首页顶部
- 全市场行业驾驶舱
- 策略 vs 市场对照页
- 候选股决策页的风险背景说明

---

## 8. 后续实施顺序

1. 确认 A50 / 金龙 / CNH / 新闻数据源
2. 生成 `market_morning_brief_YYYYMMDD.json`
3. 做页面展示块
4. 再接入行业页与策略对照页
