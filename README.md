# A股选股操作系统（stock-report）

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![public-ci](https://github.com/fisher-admin/stock-report/actions/workflows/public-ci.yml/badge.svg)](https://github.com/fisher-admin/stock-report/actions/workflows/public-ci.yml)
[![GitHub Pages](https://img.shields.io/badge/Pages-live-0B3D2E)](https://fisher-admin.github.io/stock-report/)

Public code and result summaries for FisherQuant: an A-share research pipeline that scores strategies, publishes an observation list, and keeps execution at **observe-only** (no auto-order).

这是 Fisher 选股系统的公开代码与结果仓库。它包含系统架构、核心选股与验证代码、测试，以及 GitHub Pages 展示层。

**线上：** [https://fisher-admin.github.io/stock-report/](https://fisher-admin.github.io/stock-report/)

> 本仓库内容是量化研究记录，**不构成投资建议**，不保证收益，不接自动下单。股市有风险。

- 首页：`index.html` → 系统总控台
- 市场层：`market-overview.html` → 市场驾驶舱
- 策略层：`strategy-vs-market.html` → 策略中心
- 个股层：`decision-candidates.html` → 候选股决策中心
- 复盘研究层：`research-lab.html` / `recommendation-review.html`

## 本机与 GitHub 的边界

1. 行情、财务数据、数据库、历史逐股推荐、原始 AI 分析和缓存只在本机保存与计算。
2. GitHub 仓库公开系统架构、核心源代码、测试、说明和页面代码。
3. GitHub Pages 只读取明确列入白名单的结果摘要；不提供原始数据下载或全量历史明细。
4. 每次发布先做合同校验、隐私清理和白名单审计，任何一步失败都不推送。
5. 所有公开结果可追溯到交易日、运行编号和生成时间；策略未通过门槛时保持“观察”，不接自动下单。

公开合同清单：[`config/public-result-allowlist.txt`](config/public-result-allowlist.txt)。设计说明：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 本地预览 Pages

仓库是静态站点，不需要打包。不要用本机 `data.json` 当 Pages 数据源。

```bash
cd stock-report
python3 -m http.server 8080
```

打开 `http://127.0.0.1:8080/`。页面读取 `data/latest/*.json`。

## 公开层测试

不需要行情账号。用于回归「公开边界」和 Pages 渲染：

```bash
python3 -m unittest discover tests -p 'test_*.py' -v
node tests/render.test.mjs
node tests/dual-track-render.test.mjs
```

完整本机选股/回测不在这个仓库里跑；`system/` 只是可公开的核心代码快照。

## 仓库结构

- `system/`：本机选股、事件策略、评价与发布闸门的核心源代码快照
- `docs/ARCHITECTURE.md`：系统结构、数据流和公开边界
- `assets/` 与页面文件：GitHub Pages 展示层
- `data/latest/`：当前允许公开的结果合同
- `config/public-result-allowlist.txt`：唯一允许发布的数据文件清单
- `scripts/enforce_public_boundary.py`：发布前清理和审计
- `tests/`：页面与公开边界回归测试

关键公开结果包括：

- `run_manifest.json`：本轮运行总状态
- `market_state.json`：市场层状态
- `strategy_state.json`：策略层状态
- `candidate_state.json`：当前候选结果
- `review_track_latest.json`：组合级复盘摘要，不含逐股历史明细
- `research_state.json`：研究摘要

## 目标

这套站点的目标不是“展示很多页面”，而是回答一条完整链路：

**本机完成数据计算与 AI 核对 → 规则闸门验证 → 生成公开结果摘要 → GitHub Pages 展示**

完整设计见 [系统架构](docs/ARCHITECTURE.md)。

## License

[MIT](LICENSE). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
