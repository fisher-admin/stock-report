# 当前公开层验收要点

## 一、总控台
- `index.html` 可正常打开
- 能读取 `data/latest/run_manifest.json`
- 能显示 `trade_date / run_id / validation / publish / ai_complete`

## 二、市场层
- `market-overview.html` 可正常打开
- 能显示晨判、午盘、市场热力和行业动作表
- 若午盘公开文件缺失，允许回退到 working 产物生成 latest state，但应标记来源

## 三、策略层
- `strategy-vs-market.html` 可显示：
  - 策略数量
  - 激活 / 观察 / 降级状态
  - 市场命中主线数量

## 四、个股层
- `decision-candidates.html` 可显示标准候选卡：
  - 来源策略
  - 市场动作
  - AI结论
  - 触发 / 失效条件
  - 风险提示

## 五、复盘研究层
- `research-lab.html` 与 `recommendation-review.html` 可正常打开
- 能读取 review / research / validation 摘要

## 六、数据层
公开页以 `data/latest/*.json` 为统一事实源：
- `run_manifest.json`
- `market_state.json`
- `strategy_state.json`
- `candidate_state.json`
- `review_state.json`
- `research_state.json`

## 七、发布链
- 生成 latest state 的脚本能成功运行
- `market_morning_brief` / `midday_analysis` / `unified_decision_payload` / `data/latest/*` 可被 git add / commit / push
- 页面不再依赖各自零散拼 JSON 的旧路径作为主事实源
