# Fisher Tracker — 选股策略追踪系统设计方案

## 一、定位

**一句话**：Fisher选股v4.0的专属管理后台，追踪每日选股、验证实际收益、可视化因子表现。

**与TradingAgents-CN的区别**：
- TradingAgents = AI聊天分析（通用，重LLM）
- Fisher Tracker = 策略绩效追踪（专属，重数据）

---

## 二、核心功能

### 2.1 每日选股报告 📊
- 展示当日TOP20完整信息（得分、因子评分、价格、涨跌）
- 历史报告日历视图，点击任意日期查看当天选股
- 因子权重雷达图 + 各因子得分条形图

### 2.2 收益追踪 📈 （核心亮点）
- TOP20每只股票在选出后 T+1、T+2、T+3、T+5、T+10 的实际收益
- 支持手动触发"立即刷新"获取最新价格
- 颜色编码：红涨绿跌（A股习惯）
- 汇总统计：命中率、平均收益、最大赢家/输家
- 对比基准：同期沪深300涨跌

### 2.3 策略绩效仪表盘 🎯
- 累计收益曲线（日度）
- 滚动Sharpe、最大回撤
- 因子IC趋势图（哪个因子最近有效/失效）
- 周报/月报自动汇总

### 2.4 回测管理 🔬
- 展示历史回测结果
- 不同参数组合对比
- 因子权重调整模拟

### 2.5 实时监控 👀
- 手动触发：一键更新TOP20当前价格
- 盘中涨跌实时刷新（Tushare实时接口）
- 异常预警：某只股票跌超5%高亮提醒

---

## 三、技术架构

### 方案选择：独立轻量系统（不改造TradingAgents）

**理由**：
1. TradingAgents太重（MongoDB+Redis+FastAPI+Vue3），改造成本高
2. 我们的需求很聚焦，不需要用户管理、LLM集成等
3. 轻量系统更适合Mac mini资源

### 技术栈

```
┌─────────────────────────────────────────┐
│           Fisher Tracker 前端            │
│     Vue3 + Tailwind + ECharts           │
│     (单页应用，Nginx托管)                │
└──────────────┬──────────────────────────┘
               │ REST API
┌──────────────▼──────────────────────────┐
│           Fisher Tracker 后端            │
│     FastAPI (Python 3.14)               │
│     - /api/reports     每日报告          │
│     - /api/tracking    收益追踪          │
│     - /api/dashboard   仪表盘           │
│     - /api/backtest    回测管理          │
│     - /api/realtime    实时数据          │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│              数据层                      │
│  ┌─────────┐  ┌──────────┐  ┌────────┐ │
│  │ SQLite  │  │ Parquet  │  │Tushare │ │
│  │ 选股记录 │  │ 行情数据  │  │实时API │ │
│  │ 追踪记录 │  │ (已有6125)│  │        │ │
│  │ 回测结果 │  │          │  │        │ │
│  └─────────┘  └──────────┘  └────────┘ │
└─────────────────────────────────────────┘
```

### 为什么选SQLite而不是MongoDB？
- 我们的数据是结构化的（选股记录、收益追踪）
- SQLite零运维，单文件，备份简单
- Mac mini不需要再跑一个数据库服务
- 数据量小（每天20条，一年7000条）

---

## 四、数据模型

### 4.1 daily_selections（每日选股）
```sql
CREATE TABLE daily_selections (
    id INTEGER PRIMARY KEY,
    trade_date TEXT NOT NULL,        -- 交易日 YYYYMMDD
    ts_code TEXT NOT NULL,           -- 股票代码
    name TEXT,                       -- 股票名称
    score REAL,                      -- 综合得分
    rank INTEGER,                    -- 排名
    close_price REAL,                -- 选出时收盘价
    change_pct REAL,                 -- 选出当日涨跌幅
    factor_scores TEXT,              -- JSON: 各因子得分
    strategy_version TEXT,           -- 策略版本
    UNIQUE(trade_date, ts_code)
);
```

### 4.2 tracking_prices（收益追踪）
```sql
CREATE TABLE tracking_prices (
    id INTEGER PRIMARY KEY,
    selection_date TEXT NOT NULL,    -- 选出日期
    ts_code TEXT NOT NULL,
    track_date TEXT NOT NULL,        -- 追踪日期
    track_day INTEGER,               -- T+N (1,2,3,5,10...)
    close_price REAL,                -- 追踪日收盘价
    return_pct REAL,                 -- 相对选出日收益率
    benchmark_return REAL,           -- 同期沪深300收益
    updated_at TEXT,                 -- 更新时间
    UNIQUE(selection_date, ts_code, track_date)
);
```

### 4.3 daily_summary（每日汇总）
```sql
CREATE TABLE daily_summary (
    trade_date TEXT PRIMARY KEY,
    total_stocks INTEGER,            -- 扫描总数
    strategy TEXT,                   -- 策略名称
    avg_score REAL,                  -- TOP20平均分
    factor_weights TEXT,             -- JSON: 因子权重
    backtest_info TEXT,              -- JSON: 回测摘要
    verify_hit_rate REAL,            -- 验证命中率
    verify_avg_return REAL,          -- 验证平均收益
    created_at TEXT
);
```

### 4.4 backtest_results（回测记录）
```sql
CREATE TABLE backtest_results (
    id INTEGER PRIMARY KEY,
    run_date TEXT,
    strategy TEXT,
    params TEXT,                     -- JSON: 因子权重等参数
    total_return REAL,
    sharpe REAL,
    win_rate REAL,
    max_drawdown REAL,
    daily_data TEXT,                 -- JSON: 每日明细
    created_at TEXT
);
```

---

## 五、与现有Pipeline集成

### 数据流

```
Fisher选股 Pipeline (每日17:00)
    │
    ├── 1. 生成 selection_history/YYYYMMDD.json  ← 已有
    ├── 2. 生成 data.json → GitHub Pages          ← 已有
    ├── 3. 写入 SQLite daily_selections            ← 新增
    ├── 4. 触发收益追踪（更新历史选股的T+N价格）    ← 新增
    └── 5. 推送摘要到Discord                       ← 已有
```

### Pipeline改动最小化
- pipeline.py 末尾新增一个 `sync_to_tracker()` 函数
- 调用Tracker后端API写入数据
- 或直接写SQLite（同一台机器，更简单）

---

## 六、页面设计

### P1: 首页仪表盘
```
┌──────────────────────────────────────────┐
│  Fisher Tracker v1.0                     │
│  策略: Fisher选股 v4.0 | 累计运行: 30天   │
├──────────────────────────────────────────┤
│  ┌─────────┐ ┌─────────┐ ┌─────────┐    │
│  │累计收益  │ │ 命中率   │ │ Sharpe  │    │
│  │ +15.89% │ │  54.7%  │ │  1.71   │    │
│  └─────────┘ └─────────┘ └─────────┘    │
│                                          │
│  [累计收益曲线图 - ECharts]               │
│                                          │
│  [因子IC趋势图]                          │
├──────────────────────────────────────────┤
│  最近5日选股收益追踪                      │
│  日期    | 命中率 | 平均T+1 | 平均T+3    │
│  02-26   | --     | --      | --         │
│  02-25   | 60%    | +1.2%   | +2.1%      │
│  ...                                     │
└──────────────────────────────────────────┘
```

### P2: 每日报告详情
```
┌──────────────────────────────────────────┐
│  📅 2026-02-26 选股报告                   │
│  [日历选择器]                             │
├──────────────────────────────────────────┤
│  TOP 20 列表                             │
│  ┌────┬──────┬─────┬─────┬────┬────┬────┐│
│  │排名│ 代码  │得分  │选出价│T+1 │T+3 │T+5││
│  │ 1  │大位科技│80.6│12.55│+2.1│+3.5│-- ││
│  │ 2  │协鑫集成│79.0│ 5.03│-0.5│+1.2│-- ││
│  │...                                   ││
│  └────┴──────┴─────┴─────┴────┴────┴────┘│
│                                          │
│  点击股票 → 展开因子雷达图 + K线          │
│                                          │
│  [🔄 刷新实时价格] 按钮                   │
└──────────────────────────────────────────┘
```

### P3: 收益追踪
```
┌──────────────────────────────────────────┐
│  📈 收益追踪                              │
│  [时间范围: 最近7天 / 30天 / 全部]        │
├──────────────────────────────────────────┤
│  热力图: 每日TOP20的T+N收益               │
│  (行=股票, 列=T+1~T+10, 颜色=收益)       │
│                                          │
│  ┌──────────────────────────────────┐    │
│  │ 收益分布直方图                    │    │
│  │ (T+1/T+3/T+5 分别展示)          │    │
│  └──────────────────────────────────┘    │
│                                          │
│  最佳/最差选股排行                        │
└──────────────────────────────────────────┘
```

### P4: 回测管理
```
┌──────────────────────────────────────────┐
│  🔬 回测记录                              │
├──────────────────────────────────────────┤
│  回测列表                                │
│  日期    | 策略版本 | 收益  | Sharpe     │
│  02-26   | v4.0    | +15.9%| 1.71       │
│                                          │
│  [对比模式] 选择多个回测叠加曲线          │
└──────────────────────────────────────────┘
```

---

## 七、部署方案

### Docker Compose (与TradingAgents共存)

```yaml
# fisher-tracker/docker-compose.yml
services:
  tracker-backend:
    image: python:3.14-slim
    container_name: fisher-tracker-backend
    ports:
      - "8100:8100"
    volumes:
      - ./backend:/app
      - ./data:/app/data          # SQLite + 数据文件
      - ../../stock_data:/stock_data:ro  # 只读访问已有数据
    command: uvicorn app.main:app --host 0.0.0.0 --port 8100
    
  tracker-frontend:
    image: nginx:alpine
    container_name: fisher-tracker-frontend
    ports:
      - "3100:80"
    volumes:
      - ./frontend/dist:/usr/share/nginx/html
      - ./nginx.conf:/etc/nginx/conf.d/default.conf
```

### 端口规划
| 服务 | 本地端口 | frp外网端口 |
|------|---------|------------|
| Tracker前端+API | 3100 | 65179 |
| TradingAgents | 3000 | 65177 |

### 资源预估
- 磁盘: ~100MB (SQLite + 前端)
- 内存: ~200MB (FastAPI很轻)
- CPU: 极低（只在刷新时有负载）

---

## 八、开发计划

### Phase 1: 基础框架 (1-2天)
- [ ] FastAPI后端骨架 + SQLite数据模型
- [ ] 导入已有selection_history数据
- [ ] /api/reports 每日报告API
- [ ] /api/tracking 收益追踪API
- [ ] Vue3前端骨架 + 首页仪表盘
- [ ] Docker部署 + frp外网

### Phase 2: 收益追踪 (1天)
- [ ] T+N收益自动计算（Tushare拉取历史价格）
- [ ] 手动刷新实时价格
- [ ] 热力图 + 收益分布图
- [ ] Pipeline集成（pipeline.py → Tracker API）

### Phase 3: 可视化增强 (1天)
- [ ] ECharts累计收益曲线
- [ ] 因子IC趋势图
- [ ] 因子雷达图
- [ ] 回测管理页面

### Phase 4: 高级功能 (按需)
- [ ] 盘中实时监控（WebSocket推送）
- [ ] 因子权重模拟调整
- [ ] 周报/月报自动生成
- [ ] 移动端适配

---

## 九、与TradingAgents-CN的关系

**保留TradingAgents-CN**：作为AI深度分析的补充工具
**Fisher Tracker为主系统**：日常使用的策略管理平台

两者可以通过API互通：
- Tracker选出TOP5 → 调用TradingAgents做AI分析
- TradingAgents分析结果 → 展示在Tracker的股票详情页

---

*设计日期: 2026-02-27*
