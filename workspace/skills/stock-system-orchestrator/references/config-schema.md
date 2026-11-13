# Config Schema

Use a single config file to control provider priority, model fallback, stage schedules, and downgrade behavior.

## Suggested shape

```yaml
system:
  name: fisher-stock-system
  timezone: Asia/Shanghai
  publishChannel: discord:#fisher

tradeDate:
  requireSameDayOnTradeDay: true
  allowLatestTradeDayOnNonTradeDay: true

providers:
  tradeCalendar:
    - tushare
    - local-calendar-cache
  dailyQuotes:
    - tushare
    - akshare-eastmoney
    - local-cache
  technicalFactors:
    - tushare-stk-factor
    - local-derived
  chipFactors:
    - tushare-cyq-perf
    - previous-trade-cache
  newsEvents:
    - primary-news-pipeline
    - web_search

models:
  orchestrator:
    primary: openai-codex/gpt-5.4
    fallbacks:
      - minimax/MiniMax-M2.5
  enrichment:
    primary: minimax/MiniMax-M2.7
    fallbacks:
      - minimax/MiniMax-M2.5
  summary:
    primary: minimax/MiniMax-M2.5
    fallbacks:
      - openai-codex/gpt-5.4

stages:
  dataPreparation:
    enabled: true
    degradeIfChipFactorMissing: true
    requireFiles:
      - daily
      - stk_factor
  coreSelection:
    enabled: true
    requireStage: dataPreparation
  strategySuite:
    enabled: true
    requireStage: dataPreparation
    retainedStrategies:
      - baseline_v4
      - prebreakout_v41
    requiredStrategyCount: 2
    requiredTopN: 20
  aiEnrichment:
    enabled: true
    requireStage: strategySuite
    targetStrategy: prebreakout_v41
    targetTopN: 20
    allowPublishWithoutAI: true

publish:
  requireGitPush: true
  requireValidationPass: true
  degradedLabel: "[DEGRADED]"

qualityGates:
  ai:
    requireFields:
      - ai_score
      - ai_advice
      - ai_conclusion
      - ai_points
      - ai_trend
      - ai_ma
      - ai_volume
      - ai_confidence
      - ai_summary
      - ai_risk_warning
    explicitLabels:
      - "触发条件："
      - "失效条件："
      - "板块定位："
      - "筹码判断："
      - "事件催化："
```

## Notes

### Trade-date policy

Use strict rules for production:
- If today is a trade day, require same-day data for full success.
- If today is not a trade day, allow latest trade day.

### Chip-factor policy

Treat chip factors as special:
- If unavailable, either:
  - downgrade and reduce/remove chip-dependent weights, or
  - fail closed
- Never silently pretend same-quality output when chip data is stale.

### Model policy

Validate output after each model run.
A fallback model is not just a backup for API failure; it is also a backup for **quality failure**.
