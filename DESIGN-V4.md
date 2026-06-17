# 前端 v4 视觉重做规范（2026-06-15 · Brokerage Pro / 铜金）

用户对 v3 不满：「太像工程师后台」「缺视觉重点、图表太少」。已确认方向：
**B · Brokerage Pro**（暖调抬升式券商旗舰风），强调色 **铜金/香槟金**，深浅双主题跟随系统。

v4 只重做**视觉与呈现层**（app.css 设计系统 + charts.js 图表 + 各 render 模块的层次/图表/卡片结构），
**完全保留** v3 的：信息架构（5 主页 + 薄壳）、数据接线、诚实性规范（DESIGN-V3.md 第 0 节仍是铁律）、
manifest/loader/model 数据层、app.js 交互、测试框架。权威视觉参照：`_mockups/dashboard-B.html`。

## 1. 设计令牌（写入新 app.css，替换现有令牌；类名尽量沿用以减少 render 改动）

### 涨跌色（A股红涨绿跌，最突出）
```
--up:#f5455c; --up-soft:#ff6b7d;     /* 涨·红 */
--down:#1fbf7a; --down-soft:#3ed79a; /* 跌·绿 */
--flat:#9aa0ad;
```

### 品牌色 = 铜金/香槟金（用户指定，替换 mockup 的暖橙 #f0a02a）
```
--brand:#c0883a;       /* 铜金（深色主题主品牌） */
--brand-2:#e6c178;     /* 香槟金高光 */
--accent:#5b8def;      /* 冷蓝点缀：命中率折线/AI摘要/汇率 */
--accent-2:#7aa6ff;
浅色主题 --brand:#a06a1c;（铜金压暗，保证白底对比度≥4.5）
```

### 深色（默认 · 略带暖调）/ 浅色（精致白底）—— 直接采用 mockup-B 的两套表面/油墨/阴影体系
```
dark:  --bg:#14110d --surface:#1e1a14 --surface-2:#262019 --card:#211c15 --line:#3a3225
       --ink:#f4ede0 --ink-2:#c7bda9 --ink-3:#8f8674 --ink-4:#6a6353
       --shadow / --shadow-lg（暖黑柔阴影）--glow-brand（铜金描边光晕）
light: --bg:#f4f1ea --surface:#ffffff --card:#ffffff --line:#e3ddd0
       --ink:#2a2519 --ink-2:#544c3c --ink-3:#897f6b --ink-4:#a89e88（阴影偏暖棕）
```
主题切换：`:root[data-theme="dark|light"]`；**默认跟随系统**（app.js：无 localStorage 时按
`prefers-color-scheme`），顶栏切换按钮写入 localStorage。亮/暗两套都必须精致达标（v3 亮色破损是诊断项）。

### 形状/节奏/字体
- 圆角 --radius:14px / --radius-sm:10px；卡片**抬升式**：`background:var(--card); box-shadow:var(--shadow);`
  hover 上浮 + --glow-brand；区块之间靠卡片高度与留白节奏制造层次（不是均匀后台密度）。
- 数字一律 `.num { font-variant-numeric: tabular-nums; font-feature-settings:'tnum' }`，关键数字大而精致。
- 字体：system-ui + PingFang/Microsoft YaHei；数字可用略紧字距。触达 44px、:focus-visible 铜金描边。
- 响应式三档：>1080 侧/宽栏；640–1080 双栏；<640 单列（导航横滚、宽表转卡片）。

## 2. 图表系统（重写 charts.js，全 SVG 纯函数，Node 可跑，role=img+aria-label）

mockup-B/C 已验证这些图表的画法，v4 收编为正式组件，至少 6 类：
- `gauge(score)` — 半圆刻度盘：渐变弧（绿-黄-红）+ 指针 + 中心大数字 + 0/50/100 刻度（风险/情绪温度用）
- `sparkline(values,{tone})` — 指数迷你走势，末点高亮，涨红跌绿
- `comboBarLine(bars,line)` — 逐日收益柱（正红负绿/零基线/网格）叠加命中率折线（右轴蓝），战绩用
- `equityCurve(points)` — 净值/累计收益曲线 + 水下回撤红色填充 + 低点/末点标注，战绩与首页招牌图用
- `divergingBars(items)` — 行业涨跌发散条（正向右红/负向左绿/中线），市场行业用
- `donut(segments)` 或 `weightBars` — 策略权重环/条
- `heatGrid(rows)` — 行业热力网格（保留，色随涨跌方向+强度）
- `scoreBar(value)` — 候选评分横条
空数据一律返回占位（不编造）。颜色全用 CSS 变量（`var(--up)` 等），随主题走。

## 3. 各页面呈现改造（render/* 在 v3 逻辑上重排版式 + 注入图表，数据接线不变）

**通用骨架（shell.js）**：抬升式顶栏（品牌铜金 + 数据日期徽章 + 主题切换）；侧/顶导航；过期横幅；
英雄区容器；免责页脚。Hero 升级为「大裁决标签 + 风险刻度盘并排 + 纪律胶囊」的焦点区。

- **dashboard 今日操作** = mockup-B 首页：英雄（裁决+gauge+纪律胶囊）→ 市场一眼（8 指数卡含 sparkline + 涨跌面进度条）→ 晨判 AI 摘要 → 今日执行清单（纪律说明卡 + 主攻/观察表，含股名）→ 近期战绩 comboBarLine → 三策略权重条 → 个股一瞥卡网格。
- **candidates 个股推荐**：策略 Tab 不变；每张候选卡升级为抬升式卡片 + scoreBar 评分条 + 红绿涨跌 + AI 三态徽章；保留 ai-none 真实因子小表与执行四件套。诚实性不变。
- **review 历史战绩**：KPI 横幅大数字 → equityCurve（净值+回撤填充，招牌图）→ comboBarLine（逐日收益+命中率）→ 月度汇总表 → 全部推荐表（筛选/加载更多）→ 重复推荐榜（带累计收益）→ 口径+下载。负收益照实。
- **market 市场行情** Tab：指数 sparkline 网格 + divergingBars 行业 + heatGrid；行业动作表。
- **research 系统说明** Tab：五步链路卡 + 四闸门状态 + 三策略 donut 权重 + 数据新鲜度表 + 免责全文。
- **sentiment 情绪因子**：分布卡 + gauge 情绪温度（已约束宽度）+ comboBarLine/趋势 + 逐日表。

## 4. 工程约束与验收
- 纯函数 render；转义纪律不变；诚实性 grep 必须仍为空（无硬编码业绩/模板话术）。
- 测试 `node tests/render.test.mjs` 全绿（现有断言不许放松；图表/版式改动若触发文案断言则同步更新）。
- 浏览器逐页实测：10 视图 × 深/浅主题 × 移动端，无控制台错误、无溢出、无主题破损。
- 顺手修复功能核查（workflow wltts8aci）确认的 P0/P1。
- `_mockups/` 是参照，最终不随站点发布（加进 .gitignore 或重做后删除）。
