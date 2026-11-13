# 启动前夕加工厂 · 运维检查清单

**范围**：影子研究层（日淘 + 工厂 + 观察页）  
**不在范围**：生产 PREBREAKOUT 选股（应保持冻结）  
**时区**：Asia/Shanghai  
**文档路径**：`~/.openclaw/workspace/ops/PREBREAKOUT_FACTORY_RUNBOOK.md`  
**优化方案**：`ops/PREBREAKOUT_FACTORY_IMPROVEMENT_PLAN.md`

---

## 红牌（2026-07-16 · Codex 审查后整改中）

1. **不得**依据冠军 edge /「战胜生产」做实盘或改生产配置。  
2. `ok=true` 与 cron Status=ok **不足以**单独证明完成——以 status 的 `finished_at` + 产物 `trade_date` + 检查器为准。  
3. 弱市空仓可能正常；`real_production_control_status != available` 时 **禁止**称战胜真生产。  
4. 日均超额默认相对 **研究基线**（生产权重 + 实验门控），不是真实生产策略。  
5. 市场 20 日为 **流动性代理**（`liquidity_proxy_mom20`），不是沪深300。  
6. 补历史日期请统一：`OPENCLAW_TARGET_TRADE_DATE=YYYYMMDD` 或 `--trade-date`（日淘与工厂必须同一日）。

### 正式体检（优先用这个，不要手抄旧脚本）

```bash
~/.openclaw/venv/bin/python ~/.openclaw/workspace/ops/check_prebreakout_factory.py
# exit 0=通过  2=警告（弱市空仓等）  1=失败
```

一致性冒烟：

```bash
~/.openclaw/venv/bin/python \
  ~/.openclaw/workspace/skills/stock-system-orchestrator/scripts/test_prebreakout_recipe_parity.py
```

---

## 0. 每日时间表

| 时刻 | 任务 | 工厂相关 |
|------|------|----------|
| **19:30** | A股选股+启动前夕（主管线） | 日淘 Top3 → 工厂出厂 → 刷观察数据 |
| **20:00** | AI 报告 | 一般不跑工厂 |
| **20:45** | 收盘闭环 | **补刷工厂**（默认不再日淘） |
| **21:05** | S3 跟踪 | 无关 |

**期望窗口**

- 日淘：约 15–30 分钟（超时约 35 分钟）
- 工厂：通常几分钟级（超时约 20 分钟）
- **建议 20:15–20:30 做第一次检查；20:50 后做补跑确认**

---

## 1. 开场 2 分钟：调度是否在跑

```bash
# Gateway / cron 是否正常
openclaw cron list | egrep "A股选股|收盘闭环|Status|Name"

# 关键：看这两条
# - A股选股+启动前夕深度分析   19:30  工作日  Last 应为今日、Status ok
# - 每日收盘闭环-验证+治理+告警 20:45  工作日
```

| 检查项 | 正常 | 异常 |
|--------|------|------|
| 19:30 任务 Last | 今日时间 | 停在昨天 / error / 未触发 |
| Gateway | `openclaw gateway` 在跑 | 进程挂了 → 整条链不跑 |

---

## 2. 核心健康文件（必查 3 个）

工作区根：`~/.openclaw/workspace`

| # | 文件 | 含义 |
|---|------|------|
| A | `stock_data/03-working/health/prebreakout_daily_pan_status.json` | **日淘**是否成功 |
| B | `stock_data/03-working/health/prebreakout_factory_status.json` | **工厂**是否成功 |
| C | `stock-report/data/latest/prebreakout_shadow_watch.json` | **观察页数据**是否刷新 |

### 2.1 一键体检（正式检查器）

```bash
~/.openclaw/venv/bin/python ~/.openclaw/workspace/ops/check_prebreakout_factory.py
# 可选锁定交易日：
# ~/.openclaw/venv/bin/python .../check_prebreakout_factory.py --trade-date 20260716
```

检查器会：用交易日历（非自然日）、解析失败不中断整段、兼容 `observation_champion_id`、判断弱市空仓为 WARN、核对三端 `trade_date` 一致性、读取 `finished_at` 新鲜度。

### 2.2 判定标准

| 项 | 通过 | 说明 |
|----|------|------|
| 日淘 `ok` | `true` | 失败为非阻塞，主选股仍可能成功 |
| 工厂 `ok` | `true` | 观察名单依赖它 |
| `trade_date` | **当天交易日**（如 `20260716`） | 停在昨天 = 未跑或失败 |
| `generated_at` | 今日 19:30 之后 | 20:45 后再刷一次也正常 |
| `production_frozen` | `true` | 若变 `false` 要立刻人工确认（异常） |
| 日淘 `top_n` | `3` | 并应有 3 个 `experiment_files` |
| 冠军 | 一般为 **日淘 Top2 可交易** 配方 id | 与 `observation_champion` 一致为佳 |

---

## 3. 业务结果检查（不只看 ok）

### 3.1 日淘是否产出 Top3 配方

```bash
ls -lt ~/.openclaw/workspace/stock_data/03-working/strategy_research/prebreakout_shadow/experiments/exp_gold_daily_top*.yaml | head -10
```

期望：今日有/更新过 `top1` / `top2` / `top3` 三个相关 yaml。

### 3.2 观察页名单（允许空仓）

| 现象 | 是否故障 | 处理 |
|------|----------|------|
| 有 1–4 只股票 | 正常 | 记录名单即可 |
| **0 只 + market_note 含 risk_off / cap=0** | **正常**（弱市门控） | 记入观察日志，不重跑 |
| 0 只 + 无弱市说明 + ok=false | 异常 | 见 [§5 手工补跑](#5-失败时手工补跑按顺序) |
| `trade_date` 旧 | 异常 | 查 19:30/20:45 是否执行 |

示例：`risk_off mom20=-7.08%→cap=0` → 空仓是设计行为，不是故障。

### 3.3 在线页是否跟上

| 入口 | URL / 位置 |
|------|------------|
| 观察页 | https://fisher-admin.github.io/stock-report/prebreakout-shadow.html |
| JSON | https://fisher-admin.github.io/stock-report/data/latest/prebreakout_shadow_watch.json |
| 系统侧栏 | **研究观察 → 启动前夕加工厂** |

检查步骤：

1. 页首 `trade_date` / 生成时间是否为今日  
2. 冠军名是否为日淘可交易 Top2  
3. 若本地 JSON 已新、线上仍旧：等 Pages 1–3 分钟后强刷；仍旧则查 git push（[§4](#4-发布--推送检查)）

---

## 4. 发布 / 推送检查

工厂会写本地 `stock-report`；是否已上 GitHub Pages 取决于主管线后半段 deploy。

```bash
cd ~/.openclaw/workspace/stock-report
git status -sb
git log -3 --oneline -- data/latest/prebreakout_shadow_watch.json prebreakout-shadow.html

# 本地与远端
git fetch origin 2>/dev/null
git rev-parse HEAD origin/main 2>/dev/null
```

| 状态 | 动作 |
|------|------|
| `prebreakout_shadow_watch.json` 有未提交修改 | 确认是否应由管线自动 push；必要时人工 commit+push（需授权） |
| 本地新、远端旧 | 检查 stage6 / publish 是否失败 |
| 远端已更新、Pages 旧 | 等部署或清 CDN/强刷 |

---

## 5. 失败时：手工补跑（按顺序）

**原则**：先工厂刷新，再考虑日淘；生产策略不动。

### 5.1 仅补工厂（推荐先做，对应 20:45 逻辑）

```bash
bash ~/.openclaw/workspace/ops/run_prebreakout_shadow.sh
```

### 5.2 强制再跑日淘 + 工厂

```bash
export PREBREAKOUT_RERUN_DAILY_PAN=1
bash ~/.openclaw/workspace/ops/run_prebreakout_shadow.sh
```

或分步：

```bash
PY="$HOME/.openclaw/venv/bin/python"
SCR="$HOME/.openclaw/workspace/skills/stock-system-orchestrator/scripts"

# 1) 日淘（约 15–30min）
$PY "$SCR/prebreakout_factory_search_daily.py"

# 2) 工厂出厂
$PY "$SCR/prebreakout_factory.py"

# 3) 再跑 §2 一键体检
```

### 5.3 指定交易日（补历史）

```bash
PY="$HOME/.openclaw/venv/bin/python"
SCR="$HOME/.openclaw/workspace/skills/stock-system-orchestrator/scripts"
$PY "$SCR/prebreakout_factory.py" --trade-date YYYYMMDD
```

（日淘是否支持同参以脚本 `--help` 为准。）

---

## 6. 常见故障速查

| 症状 | 可能原因 | 处理 |
|------|----------|------|
| 日淘/工厂 `trade_date` 停昨天 | 19:30 cron 未跑 / Gateway 挂 | 查 cron Last；重启 gateway；补跑 §5 |
| 日淘 `ok=false` | 超时(>35min)、数据缺、异常 | 看 status 全文；可单独重跑日淘；工厂仍可用旧配方 |
| 工厂 `ok=false` | 实验 yaml 坏、原料子分缺失 | 查 `prebreakout_factory_latest` / 日志；补跑工厂 |
| 观察页 0 只 | 弱市 cap=0 | **不修**，记观察 |
| 线上页无入口/旧名单 | 未 push 或 Pages 延迟 | §4；强刷 |
| 主选股失败但工厂 ok | 非阻塞设计 | 分别排主线与工厂 |
| 工厂失败但主选股 ok | 非阻塞 | 只补工厂，不必重跑全管线 |

**超时配置（参考）**

- 日淘 stage：2100s  
- 工厂 stage：1200s  
- 均为 `NON_BLOCKING`，失败不拖垮选股主结果  

---

## 7. 红线（运维禁止项）

| 禁止 | 原因 |
|------|------|
| 改 `pipeline.PREBREAKOUT_CONFIG` / 生产权重「顺手优化」 | 工厂约定：生产冻结 |
| 把观察页名单当生产下单列表自动执行 | 研究观察，非交易指令 |
| 日淘失败就 `kill` 整条选股重来 | 应用 §5 局部补跑 |
| 未确认就 force-push `stock-report` | 影响线上 Pages |

确认健康的标志之一：`production_frozen: true`。

---

## 8. 每日签字表

```
日期：________  检查人：________  时间：________

[ ] openclaw cron：19:30 选股 Last=今日 / Status=ok
[ ] 日淘 status ok=true，trade_date=今日，top_n=3
[ ] 工厂 status ok=true，trade_date=今日
[ ] watch.json generated_at 在 19:30 后，champion 有值
[ ] production_frozen=true
[ ] 名单：有 N 只 / 或 空仓且 market_note=risk_off（圈一）
[ ] GitHub Pages 与本地 trade_date 一致（或已记录延迟）
[ ] 异常及处理：_______________________________

结论：□ 正常  □ 弱市空仓（正常）  □ 已补跑恢复  □ 需升级
```

---

## 9. 关键路径速查

```text
~/.openclaw/workspace/
├── ops/
│   ├── PREBREAKOUT_FACTORY_RUNBOOK.md   # 本文档
│   └── run_prebreakout_shadow.sh        # 20:45 补跑入口
├── skills/stock-system-orchestrator/scripts/
│   ├── prebreakout_factory_search_daily.py  # 日淘
│   ├── prebreakout_factory.py               # 工厂
│   └── run_pipeline.py                      # 主管线挂载
├── stock_data/03-working/
│   ├── health/
│   │   ├── prebreakout_daily_pan_status.json
│   │   ├── prebreakout_factory_status.json
│   │   └── prebreakout_factory_latest.json
│   └── strategy_research/prebreakout_shadow/
│       ├── experiments/                     # 配方 YAML
│       └── factory/search/search_report_daily_latest.json
└── stock-report/
    ├── prebreakout-shadow.html
    └── data/latest/prebreakout_shadow_watch.json
```

---

## 10. 最小可行检查（只给 1 分钟时）

```bash
~/.openclaw/venv/bin/python ~/.openclaw/workspace/ops/check_prebreakout_factory.py
# 或三行：
jq '{ok,state,trade_date,finished_at,champion,production_config_fingerprint}' \
  ~/.openclaw/workspace/stock_data/03-working/health/prebreakout_factory_status.json
jq '{trade_date,production_frozen,real_production_control_status,research_only,market_note,n:(.latest_picks|length)}' \
  ~/.openclaw/workspace/stock-report/data/latest/prebreakout_shadow_watch.json
```

**全绿**：工厂 `ok=true` + `state=completed` + 日期=期望交易日 + `production_frozen=true`。  
**名单为空**：先读 `market_note`，弱市则检查器给 WARN(exit 2)。  
**真生产对照**：`real_production_control_status=available` 才允许讨论「战胜真生产」。

---

## 11. 背景摘要（给新接手的人）

### 工厂定位

| 环节 | 含义 |
|------|------|
| 原料 | 生产 `score_stock_prebreakout` 子分（权重/代码冻结） |
| 加工 | 实验 YAML：权重重组、因子乘子、门控 |
| 质检 | 与生产对照的 T+1 开→收回测 + 当日名单 |
| 产物 | 观察页 + health JSON；**不写回生产** |

### 主管线联动

```text
工作日 19:30  主管线
  → strategySuite
  → prebreakoutDailyPan      # 轻量日淘 Top3
  → prebreakoutShadowResearch # 工厂出厂 + 观察页
  → AI / 发布 …

工作日 20:45  收盘闭环
  → ops/run_prebreakout_shadow.sh
  → 默认只刷工厂（设 PREBREAKOUT_RERUN_DAILY_PAN=1 可再淘）
```

### 日淘 Top3 角色

| 角色 | 含义 |
|------|------|
| #1 综合金 | gold / 稳定性优先 |
| #2 可交易 | 空仓少优先 → **默认观察冠军** |
| #3 风格对照 | 与前两套有差异，防同质 |

### 两层「Top」勿混

1. **淘沙 Top3** = 3 套加工配方（实验）  
2. **出厂股票** = 冠军配方当日过门控后的几只（弱市可为 0）

---

## 修订记录

| 日期 | 说明 |
|------|------|
| 2026-07-16 | 初版：日淘跟随主管线后的运维检查清单 |
| 2026-07-16 | P0/P1：红牌口径、正式检查器、真 holdout/对照/日期贯通/原子写/锁 |
