# Stage Contracts

Define each stage as a contract with explicit inputs, outputs, and pass/fail semantics.

## Stage 1: Data Preparation

### Inputs
- provider configuration
- trade-date policy
- existing cache paths

### Outputs
- effective trade date
- provider usage report
- cache file list
- `health/data_ready_YYYYMMDD.json`

### Success
- trade date resolved
- required files exist for expected date
- degraded flags recorded if fallbacks were used

### Fail
- no valid trade date
- no daily quotes
- required provider data missing with no allowed downgrade

## Stage 2: Core Stock Selection

### Inputs
- Stage 1 success marker
- effective trade date
- quote/factor datasets

### Outputs
- selection history
- verification output
- summary output
- optional tracker sync report

### Success
- market scan completed
- top results generated
- selection history written

### Fail
- no valid scan results
- output files missing

## Stage 3: Strategy Suite

### Inputs
- Stage 1 success marker
- factor caches across lookback window

### Outputs
- `data/strategy_backtests.json`
- summary stats for each retained strategy
- latest top20 for each retained strategy

### Success
- retained strategies are present (`baseline_v4`, `prebreakout_v41` by default)
- each retained strategy has expected topN length
- `latest_trade_date` matches policy
- no unexpected extra strategies remain in the output

### Fail
- strategy count mismatch
- topN mismatch
- date mismatch

## Stage 4: AI Enrichment and Publish

### Inputs
- strategy suite output
- configured target strategy and target topN
- model chain
- publish policy

### Outputs
- enriched target items
- validation report
- publication result
- git evidence if publish is enabled

### Success
- required AI fields present in full mode
- validation passes
- publish succeeds if required

### Fail
- validation fails in required mode
- publish fails when publish is required
- target strategy missing

## Operational Rule

Never mark the whole system successful just because an early stage succeeded.
A run is only fully successful if every enabled downstream stage either:
- succeeds, or
- downgrades in a way explicitly allowed by config.
