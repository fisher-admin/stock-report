# Hermes stock-selection system: optimization plan (2026-10-04)

Basis: `docs/superpowers/reviews/2026-10-04-hermes-stock-system-independent-review.md`. Every work item below maps to a finding there (P0-x, P1-x, P2-x).

## 0. Goal and rules

**Goal.** On every eligible trading day the system should either:
- publish a correct batch with a remote-confirmed receipt, or
- block honestly with one clear alert naming the root cause.

It should not be stopped by avoidable environment failures: a full disk, an expired credential, timeouts that collide, or noisy alerts.

**Unchanged limits** (from the Oct 3 authorization):
- observe-only; no trading;
- no strategy change;
- no deleting evidence, backups or failed-run records;
- no change to SSH, TLS or proxy settings, and no change to the Discord target;
- no force-push;
- failure injection only in temp fixtures;
- Hermes platform changes only for demonstrated defects.

**How every item is delivered**
1. Write a failing test first, then make the smallest change.
2. Record each changed file in an evidence package with original and proposed copies, `manifest.json`, `apply.sh` and `rollback.sh`. **Both scripts must preserve file modes**, a lesson from 10-04.
3. Read every file back after deploy and check its SHA256 and mode.
4. Make one commit per item, containing only that item's files.
5. Report in this order: what was checked, what passed, what failed.

**Acceptance window** (10 natural trading days from 10-08):
- Phase 1 must be deployed by **Wed 10-07, 18:00 Shanghai**, so the window starts on the final configuration.
- During the window only defect fixes are allowed. Each one is logged in `evidence/20261003-hermes-reliability/acceptance/` with date, item and reason.
- A change to a gated job's behaviour during the window restarts the count, unless the change fixes a defect the window itself exposed. Whether the count restarts is decided and written down before the change is deployed.

## 1. Phase 1: before the first trading day (deploy by 10-07)

### 1.1 Disk headroom (P0-1). Owner: **you**, then me

| Step | Who | Detail |
|---|---|---|
| 1.1a Free ≥ 20 GB | You | Candidates I measured: `~/.hermes/state.db.pre-update-emergency-2026-08-2{3,5,6}*.bak` (4.5 GB), `~/.hermes/audits` (4.6 GB), `workspace/.worktrees` (1.9 GB, four from Sept), `~/.openclaw/backups/workspace-20260726-post-cleanup.bundle` (786 MB), plus `~/Library`, `~/Documents` and `~/.cache` outside the system. I will not delete these. If you want, I can move chosen items to an external disk you name. |
| 1.1b Fix backup prune | Me | `ops/backup_databases.sh`: the glob becomes `*.????????.db.gz`, and retention is applied per database prefix, keeping the newest 5 as the comment states. Also check that free space is ≥ 1.2 × the source database size before `sqlite3 .backup`; if not, skip with a 🚨 "disk" alert instead of risking ENOSPC. |
| 1.1c Disk guard in production | Me | A shared `disk_guard.py` (standard library only). It is called by the 19:30 wrapper preflight and the 21:05 watchdog: warn below 15 GB, and fail closed below 5 GB with the reason `DISK_LOW`. |

**Tests (temp directories only)**
- The prune keeps 5 per prefix and never touches non-matching files.
- Low disk produces a skip with exit 1 and the 🚨 text. The available-space function is stubbed.
- The guard's thresholds and output format.

**Verify**
- The 21:30 backup on 10-05 completes; the backup is not calendar-gated, so it runs on holidays.
- A new `recommendations.YYYYMMDD.db.gz` exists, and `gzip -t` passes on it.

### 1.2 Time budget that fits inside Hermes's 3600 s kill (P0-2). Owner: me

Hermes's script timeout is **global** (`HERMES_CRON_SCRIPT_TIMEOUT`, default 3600 s). I will not raise it. Every job fits under it instead:

| Job | Starts | Hard budget inside the job | Latest finish |
|---|---|---|---|
| stock_pipeline_gate | 19:30 | Stage 1 2400 s kill (internal wait 2220 s) plus later stages ≤ 900 s = **3300 s** | 20:25 |
| daily_ai_publish | 20:00 | release wait **2400 s** (today 3600 s), then summary and hand-off ≤ 300 s | 20:45 |
| daily_closure | 20:45 | must not finalize while publish is still running (see below) | ~20:50 |
| data_freshness_watchdog | 21:05 | unchanged | |

**Changes**
1. `daily_ai_publish.sh`: export `OPENCLAW_PRODUCTION_RELEASE_TIMEOUT_SECONDS=2400`. When the wait expires, the job prints an honest "主管线未在 20:40 前完成" ("main pipeline did not finish by 20:40") with the run_id, and exits with the blocked-upstream code (see 2.2).
2. `run_pipeline.py`: a total budget guard. If elapsed time plus the next stage's budget would pass 3300 s, stop with `BUDGET_EXHAUSTED` instead of being killed silently by Hermes.
3. `daily_business_status.py`: if publish is still running (Hermes `executions` row in `running` for daily_ai_publish), poll for up to 10 minutes. If it is still running after that, write `publish: pending` instead of a final verdict. Hermes `executions` is read-only from here.
4. `test_time_budget.py`: asserts the table above from the code constants, so a future change to any timeout that breaks the chain fails a test.

**Tests**
- A fixture where the release appears at 20:30 simulated time: publish succeeds.
- A fixture where it never appears: an honest blocked message before the kill.
- A closure fixture with publish still running gives `pending`.
- The pipeline budget guard stops before 3300 s.

### 1.3 Data-vendor credential and fallback reliability (P0-3). Owner: me, plus **you** for expiry dates

1. **Commit** the 09-29 interpreter-resolution fix in `skills/stock-analyzer/backtest.py`. It currently exists only in the working tree. Commit only that hunk, with a regression test that `_resolve_external_data_python()` returns an existing interpreter on this Mac.
2. **Credential canary**: a new no-agent job `provider_canary.sh` at **07:45 Mon–Fri**, calendar-gated, delivering to the existing Discord target.
   - For each configured provider (RDS, Promax, Tushare proxy), make one read-only `trade_cal` call over a 5-day window.
   - Classify the result as `ok`, `expired`, `quota`, `http_4xx`, `http_5xx`, `network` or `bad_payload`.
   - Write the result to `health/provider_canary.json`.
   - Alert only on a change, using `stock_alert_policy.py` (one full message, then one line per repeat, then "已恢复", i.e. "recovered").
   - Credentials are never printed or logged.
3. **Expiry reminder.** You provide each provider's renewal date. I store the dates in the non-secret `health/provider_expiry.json`, and the canary warns starting 7 days before.
4. **Prove the fallback.** Run the same-day market-snapshot fallback end to end in a temp workspace, never against the live health directory or publishing, for the most recent open date. Check that the resulting `daily` cache passes the Stage 1 quality checks. If it passes, it is listed as a proven fallback; otherwise the gap is documented and it is treated as unavailable.

**Tests**
- Canary classification with stubbed HTTP responses, one per class.
- A secret-redaction test.
- The calendar gate is respected.

**Decision needed from you:** adding one Hermes job (the canary) to `jobs.json`. Same Discord target, no other change.

### 1.4 Phase 1 exit check (10-07 evening)

- Every Phase 1 test passes under `/bin/bash` 3.2 and Python 3.14.3.
- The full orchestrator suite shows no new failures against the 10-04 baseline (`evidence/…/test_results/mac_20261004/mac.lst`).
- The holiday checks for 10-05 to 10-07 hold: skip records, no business messages, the backup ran, nothing new in `stock-report`.
- Status report sent to you. The acceptance count starts on 10-08.

## 2. Phase 2: stability and alert quality (after acceptance day 10, or earlier only as a logged defect fix)

### 2.1 Post-publish research no longer permanently red (P1-1)

- Stage results become `required` / `optional`. The job is red only when a required track fails. Each optional track reports one line: `ok` / `degraded: <reason>`.
- Per-stage budgets: factorAttribution 1200 s, prebreakoutDailyPan 1500 s (needs profiling; today about 1670 s), shadow 900 s, shortTrack 600 s. That sums above 3600 s, so:
  - **Option A (recommended)**: split into two Hermes jobs, *research-core* at 22:15 and *research-short-track* at 23:20.
  - **Option B**: keep one job and profile prebreakoutDailyPan down first.

  **Decision needed from you.**
- The shortTrack input mismatches (`immutable parquet mismatch`, `frozen materialized input disagrees`) are addressed by the pre-staging bundle from the Oct 3 plan. Measure the Mac cold-start time first, keep the 600 s budget, and add no blanket tolerance.

### 2.2 One root cause, one alert (P1-2)

- Exit classes for all stock wrappers:

  | Exit | Meaning | Alert |
  |---|---|---|
  | 0 | ran | finding text if any |
  | 10 | blocked by upstream | one line referencing the root run_id |
  | anything else | broke | full alert |

  Hermes still records exit 10 as failed. The alert *text* changes; the exit code stays honest.
- Watchdog: exit 0 with a structured finding when it detects stale data. Non-zero only when the watchdog itself fails.
- `cron_incidents` never closes, which is a demonstrated Hermes observability defect. Close an incident after the job's next success. This is the smallest Hermes change possible and gets its own test in the Hermes repo. **Decision needed from you**, because it touches the Hermes platform.

### 2.3 Stage-timing telemetry (P1-3)

- `daily_business_status` adds per-stage elapsed time from `health/runs/<run>.json`, compared with the 20-day median. It flags a stage taking more than twice its median, or more than 80% of its budget.
- Stage 1 records why it waited: vendor not ready, retry, rate limit or fallback.

## 3. Phase 3: maintainability (after acceptance)

| Item | Change | Needs you |
|---|---|---|
| 3.1 Git as the record of production (P2-1) | (a) Commit the outstanding uncommitted work task by task, after showing you the grouping. (b) `.gitignore` runtime data (`stock_data/03-working/**` caches, health, backups) and `git rm --cached` it; files stay on disk and history is not rewritten. (c) Remove merged worktrees with `git worktree remove`; unmerged ones are left alone. | Approve the grouping in (a) and the scope of (b) |
| 3.2 Health directory retention (P2-2) | Move `data_ready_*` and `market_snapshot_*` older than 30 days to `health/archive/YYYYMM/` (move, never delete); readers updated and tested | — |
| 3.3 Repo naming (P2-3) | `orchestrator_run.json` records `working_repo` and `published_repo`; README section | — |
| 3.4 Environment locks (P2-4) | `pip freeze` lock for each venv committed; `env_drift_probe` compares against them | — |
| 3.5 Failing test (P2-5) | Fix `test_backfill_targeting … fresh_same_day…` at its root (`stage4_ai_backfill_loop.py`) | — |

## 4. Phase 4: strategy evaluation (analysis only; no strategy change)

- Benchmarks: equal-weight all-A, CSI 1000 and CSI 300, using the system's own rule (T close signal, T+1 open qfq entry, 0.3% round trip, 5-day hold). Results split by version (before and after 08-11).
- AI value computed only on `fresh_same_day` rows from `recommendations.db`.
- Output: a new review document, which feeds your decision on whether AI analysis and the research tracks are worth their run time and cost.

## 5. Timeline

| Date (Shanghai) | Work |
|---|---|
| 10-04 to 10-05 | You: disk cleanup. Me: 1.1b, 1.1c, 1.2 and 1.3 items 1–2 implemented with tests, deployed as packages |
| 10-05 to 10-07 | Holiday verification each evening; 1.3 item 4 fallback proof; Phase 1 exit check on 10-07 |
| 10-08 to ~10-21 | Acceptance window: daily log, defect fixes only |
| After acceptance | Phase 2, then Phase 3, then Phase 4 |

## 6. Decisions needed from you

1. Disk cleanup: which items to remove or move (1.1a). This is the only blocker for Phase 1.
2. Add the 07:45 provider canary job to Hermes (1.3), and give me provider renewal dates if you have them.
3. Post-publish research: split into two jobs (A) or profile first (B) (2.1).
4. Allow the minimal Hermes fix that closes `cron_incidents` on recovery (2.2).
5. Git cleanup scope (3.1).
