# 旧 market_analyzer.py 到新系统字段映射 v1

更新时间：2026-03-21 00:56 Asia/Shanghai
负责人：小小鱼

## 1. 结论

旧的 `daily_stock_analysis/src/market_analyzer.py` 已具备新系统市场层的一部分核心零件，可直接复用，不需要从零开始。

主要价值：

- 指数行情
- 市场涨跌统计
- 涨停/跌停统计
- 两市成交额
- 板块涨跌榜
- 市场新闻搜索
- LLM 市场复盘报告

---

## 2. 现有对象

文件：
- `/Users/fisher/daily_stock_analysis/src/market_analyzer.py`

核心对象：
- `MarketIndex`
- `MarketOverview`

### `MarketIndex` 当前字段

- `code`
- `name`
- `current`
- `change`
- `change_pct`
- `open`
- `high`
- `low`
- `prev_close`
- `volume`
- `amount`
- `amplitude`

### `MarketOverview` 当前字段

- `date`
- `indices`
- `up_count`
- `down_count`
- `flat_count`
- `limit_up_count`
- `limit_down_count`
- `total_amount`
- `top_sectors`
- `bottom_sectors`

---

## 3. 映射到新系统市场层

### 市场状态对象（新）

| 新字段 | 旧来源 | 说明 |
|---|---|---|
| `trade_date` | `MarketOverview.date` | 交易日 |
| `market_indices` | `MarketOverview.indices` | 指数快照 |
| `up_count` | `MarketOverview.up_count` | 上涨家数 |
| `down_count` | `MarketOverview.down_count` | 下跌家数 |
| `flat_count` | `MarketOverview.flat_count` | 平盘家数 |
| `limit_up_count` | `MarketOverview.limit_up_count` | 涨停家数 |
| `limit_down_count` | `MarketOverview.limit_down_count` | 跌停家数 |
| `total_amount` | `MarketOverview.total_amount` | 两市成交额 |
| `top_sectors` | `MarketOverview.top_sectors` | 领涨板块 |
| `bottom_sectors` | `MarketOverview.bottom_sectors` | 领跌板块 |

### 新系统需要补充的字段

旧系统没有，但新系统应新增：

- `breadth_score`
- `risk_score`
- `style_bias`
- `market_regime`
- `overnight_risk_score`
- `overnight_regime`
- `offset_score`
- `focus_sectors`
- `opening_playbook`

---

## 4. 可直接复用的方法

### 4.1 指数行情

旧方法：
- `_get_main_indices()`

新系统用途：
- 市场驾驶舱顶部指数条
- 市场状态计算输入

### 4.2 涨跌统计

旧方法：
- `_get_market_statistics()`

新系统用途：
- 市场广度
- 风险偏好
- 日内情绪状态

### 4.3 板块涨跌榜

旧方法：
- `_get_sector_rankings()`

新系统用途：
- 板块全景看板
- 市场行业驾驶舱补充信息

### 4.4 市场新闻搜索

旧方法：
- `search_market_news()`

新系统用途：
- 隔夜消息对冲判断
- 晨判中的 `offset_reason`
- 市场复盘与记录

---

## 5. 不建议直接复用的部分

### 5.1 直接输出最终复盘文案

旧系统偏向“生成文字报告”，而新系统需要先结构化数据，再由页面和 Agent 组合输出。

建议：
- 保留其新闻搜索与市场信息采集能力
- 不直接把整段报告当成新系统核心对象

### 5.2 旧系统的单页式组织方式

旧系统更像“生成大盘复盘文本”，而新系统需要：

- 市场层
- 行业层
- 策略层
- 个股层
- 验证层

因此应复用数据采集，不直接复用页面结构。

---

## 6. 建议的复用途径

### Phase A

先抽取旧系统中的：
- 指数行情
- 涨跌统计
- 板块涨跌榜
- 市场新闻搜索

形成新系统统一数据对象：
- `market_state_YYYYMMDD.json`
- `market_morning_brief_YYYYMMDD.json`

### Phase B

在新系统页面中使用这些数据对象，而不是直接调用旧系统的报告文本。

---

## 7. 一句话结论

旧 `market_analyzer.py` 很值得复用，但应复用其“数据获取与基础分析能力”，不要直接照搬其“文本复盘输出形态”。
