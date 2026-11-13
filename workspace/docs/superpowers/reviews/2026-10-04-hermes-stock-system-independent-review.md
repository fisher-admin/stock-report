# Hermes stock-selection system: independent operational review (2026-10-04)

Reviewer: Claude Code on the Mac. Scope: whether the system runs smoothly and what keeps it from doing so. This review does not change the strategy, does not trade and does not modify production. Every finding below was re-derived from live evidence on 2026-10-04, not taken from earlier reports.

Evidence used:
- `~/.hermes/cron/executions.db`: 328 executions, 2026-08-17 to 2026-10-04, plus `cron_incidents`.
- `~/.hermes/cron/jobs.json`.
- `stock_data/03-working/health/` (`runs/`, run-state files).
- `df`/`du` output.
- The wrapper scripts, `ops/backup_databases.sh`, `scripts/run_ai_summary_and_publish.py`, `skills/stock-analyzer/backtest.py` and the Hermes scheduler source.
- The published strategy statistics in `evidence/20261003-hermes-reliability/`.

## 1. Headline numbers (Hermes history, 2026-08-17 to 2026-10-04)

| Job | Completed | Failed | Unknown (orphaned) | Notes |
|---|---|---|---|---|
| stock_pipeline_gate (19:30, main selection) | 15 | 15 | 5 | p50 816 s, max 3626 s (hit the 3600 s Hermes kill) |
| daily_ai_publish (20:00) | 17 | 17 | 2 | since September every failure is a downstream block of a failed pipeline |
| post_publish_research (22:15) | 4 | 22 | 4 | **15 failures in a row**, 0 successes since early September |
| event_quality_drift (08:15) | 19 | 13 | 7 | |
| morning_brief (08:00) | 24 | 9 | 1 | |
| midday_analysis (12:00) | 26 | 6 | 1 | |
| data_freshness_watchdog (21:05) | 19 | 15 | 1 | "failed" here usually means "alert raised", not "job broke" |
| daily_closure (20:45) | 33 | 0 | 2 | |
| backup_databases (21:30) | 43 | 4 | 2 | **failed 2026-10-03 and 2026-10-04** |

The main selection succeeded on 15 of 35 scheduled runs (43%). The causes are concentrated: one week-long credential outage, data-vendor 5xx errors, and stage-timing problems. The selection logic itself is rarely what fails.

What is working:
- Orphaned "owner exited" runs stopped after 2026-09-04.
- Failure is fail-closed. Stage 1 refuses previous-day data, and publish refuses unmatched receipts.
- The calendar gate and the final publication gate are now deployed and tested on this Mac.

## 2. P0: fix before the first post-holiday trading day (Thu 2026-10-08)

### P0-1. The disk is full and backups have stopped

**Evidence**
- The data volume is 228 GiB, with 200 GiB used and **2.6 GiB free (99%)**.
- Backups logged "磁盘可用 4G/2G <8G" (4 GB / 2 GB free, under 8 GB) on Oct 3 and Oct 4, and exited 1.
- The last backup of `recommendations.db` (1.8 GB) is from 2026-10-02.
- A disk-full (ENOSPC) outage already happened on Jul 8–9, according to the comment in `ops/backup_databases.sh`.
- Production writes need space: SQLite WAL files, parquet caches, git pushes. A full disk will fail Stage 1 or the publisher in ways that look like unrelated errors.

**Defect found in the guard** (`ops/backup_databases.sh` line 22)
- The low-disk "extra prune" step runs `ls -1t "$DEST"/*.????????.db`.
- Every backup is stored gzipped as `*.db.gz`, so the glob matches nothing and the emergency prune has never pruned anything.
- Even if it matched, `tail -n +16` would keep 15 files, not the 5 described in the comment.
- Separately, `sqlite3 .backup` first writes an **uncompressed** 1.8 GB copy before gzip. At 2–4 GB free, the backup itself can push the disk to ENOSPC.

**Actions**
1. **You** free at least 20 GB. These are the largest candidates I found. I have not deleted anything; review each one yourself.
   - `~/.hermes/state.db.pre-update-emergency-2026-08-2{3,5,6}*.bak`: 3 × 1.5 GB (4.5 GB) from August.
   - `~/.hermes/audits`: 4.6 GB.
   - `~/.openclaw/workspace/.worktrees`: 1.9 GB, with four worktrees from 09-10 to 09-21.
   - `~/.openclaw/backups/workspace-20260726-post-cleanup.bundle`: 786 MB.
   - `docs/superpowers`: 3.5 GB, mostly evidence.
   - Outside this system: `~/Library` (38 GB), `~/Documents` (20 GB), `~/.cache` (5.9 GB).
2. Fix the prune glob to `*.????????.db.gz` and make the retention count match the stated policy. Add a regression test that runs against a temp directory.
3. Check that free space is at least 1.2 × the database size before `.backup`. Then either write the uncompressed copy to a location with room, or stream it with `sqlite3 src .dump | gzip`, accepting the longer run time.
4. Add a free-disk check to the 19:30 preflight and to the 21:05 watchdog: warn below 15 GB and fail closed with a clear "disk" reason below 5 GB. That way a full disk produces a single, unambiguous alert.

### P0-2. The 20:00 publish wait equals the Hermes hard kill

**Evidence**
- `run_ai_summary_and_publish.py` (`wait_for_completed_release`) waits up to `OPENCLAW_PRODUCTION_RELEASE_TIMEOUT_SECONDS`, which defaults to 3600 s. Nothing in the wrapper or `jobs.json` overrides it.
- Hermes kills script jobs at `_DEFAULT_SCRIPT_TIMEOUT = 3600` s.
- Observed: daily_ai_publish has run for 3602 s.

**Why it matters now**
- The Oct 3 change caps the Stage 1 wait at 2220 s. On a slow-data day the pipeline can therefore finish around 20:20–20:40.
- Publish then either catches the receipt late, or is killed by Hermes before it can print an honest "not ready" message.
- Meanwhile the 20:45 closure job and the 21:05 watchdog run while publish is still waiting, so the closure status can be written before the publish outcome exists.
- Six pipeline runs in August and early September already finished after 20:00.

**Actions**
1. Set the publish wait explicitly in `daily_ai_publish.sh` with margin below the kill. For example, 2700 s gives the job time to log and alert before 3600.
2. Make `daily_closure` refuse to finalize while the publish job is still inside its wait window. Otherwise, move closure to 21:10 and the watchdog to 21:20.
3. Write down the full time budget as one table and keep it under the 3600 s cap with at least 10% margin:
   - main pipeline: Stage 1 2220 s plus about 1000 s for the remaining stages;
   - publish;
   - closure.

   Today the main pipeline budget (about 3200 s) sits within about 10% of the cap.

### P0-3. One expired credential took the system down for a week, and the fallback never worked

**Evidence**
- From 09-14 to 09-21, Stage 1 failed within about 15 s every day with `您的token已过期，请联系续费` ("your token has expired, please renew"). That was 6 trading days with no selection.
- An earlier expiry around late July is recorded on the task board.
- The vendor setup is fragile in several ways:
  - Tushare goes through a third-party proxy (`tushare_api_url`).
  - RDS and Promax keys were added on 09-21.
  - 09-25 shows `rds stock_st HTTP 400; promax stock_st HTTP 503`.
  - 09-29 shows "configured provider unavailable for daily".
- The same-day market-snapshot fallback failed every time with `market snapshot python missing: no usable interpreter found`. In the committed `backtest.py`, `_resolve_external_data_python` listed only interpreters that don't exist on this Mac.
- That was fixed in the working tree on 2026-09-29 (it now includes `sys.executable` and the workspace venvs). The fix is **uncommitted**, and the fallback has **never succeeded in production**.

**Actions**
1. **Daily credential canary at about 07:45**, read-only:
   - Call `trade_cal` on each configured provider: RDS, Promax and Tushare.
   - Classify the result as expired, quota, HTTP 4xx, HTTP 5xx or network.
   - Send one alert that names the provider and the renewal step.
   - Record each provider's known expiry date in a non-secret file, with a reminder 7 days before.
2. Run the market-snapshot fallback end to end in an isolated fixture, with no publishing, and confirm it produces a same-day `daily` cache that passes the Stage 1 quality checks. Until that has been shown, treat the fallback as unavailable in runbooks.
3. Commit the 09-29 interpreter-resolution fix. A `git checkout`/`stash` today would silently undo it (see P2-1).

## 3. P1: stability and alert quality

### P1-1. Post-publish research is permanently red

**Evidence**
- 15 consecutive failures.
- The 10-02 run took 22:15:12 to 23:15:24, which is the full 3600 s cap.
- The three heavy stages succeed (factorAttribution about 1030 s, prebreakoutDailyPan about 1670 s, prebreakoutShadowResearch about 900 s).
- The job is red because one optional stage, `shortTrackGeneration`, fails, each day for a different reason:
  - 09-25: vendor `stock_st` 400/503;
  - 09-28 and 09-30: the 600 s timeout;
  - 10-01: `immutable parquet mismatch`;
  - 10-02: `frozen materialized input disagrees with common PIT copy`.

**Actions**
1. Report each research track separately: success, degraded, or failed with a reason. Turn the job red only when a required track fails. Report optional-track failures as one line each, not as a job failure.
2. Give each stage its own budget and keep the total under about 3000 s, or split the work into two Hermes jobs (factor/prebreakout at 22:15, short track at 23:20). Right now the job only works if nothing is slow.
3. The frozen-input mismatches are the open "pre-staging bundle" item from the Oct 3 plan. Measure the cold-start time first and keep the 600 s budget, as that plan specifies.

### P1-2. Alerts conflate "found a problem", "blocked by upstream" and "broke"

**Evidence**
- The watchdog exits non-zero when it detects stale data, which accounts for 15 "failed" runs.
- Every pipeline failure also produces a publish failure, so one root cause becomes two red alerts.
- 61 rows in `cron_incidents` are still `alerted`/`detected`, and none has ever been closed, even after the jobs recovered.

**Actions**
1. Use distinct exit and alert classes:
   - exit 0 with a structured "finding" when the job ran and found a problem;
   - a dedicated code with one line referencing the root run_id when the job is blocked upstream;
   - non-zero only when the job itself broke.
2. Close or recover `cron_incidents` when a job succeeds. This is a demonstrated observability defect, so it falls within the allowed scope for Hermes changes. `stock_alert_policy.py` already handles new-failure, repeat and recovery messages; the incident table should follow it.

### P1-3. Stage timing is volatile and close to the cap

**Evidence**
- `dataPreparation` ranges from 2 s to 2400 s (killed on 09-22) and took 723 s on 09-28.
- The strategy stage ranges from 78 s to 1960 s.
- The main pipeline has hit 3626 s, i.e. the kill.

**Actions**
1. Add per-stage elapsed time from `health/runs/*.json` to the daily status and alert on drift, for example a stage taking more than twice its 20-day median.
2. Record why Stage 1 waited (vendor not ready, retries, or rate limit) so slow days can be explained afterwards.

## 4. P2: maintainability and hygiene

1. **Version control is not a reliable record of production.**
   - About 3 weeks of work is uncommitted: 125 modified and 1,479 untracked files across several tasks.
   - Production fixes exist only in the working tree.
   - 8,582 data files are tracked, 6,269 of them parquet, giving a 709 MiB pack.

   Commit each task when it closes. Stop tracking runtime data (`stock_data/03-working/**` caches, health files and backups) with `.gitignore` plus `git rm --cached`, which keeps the files on disk. Prune the four stale worktrees once their work is merged or recorded.
2. **The health directory grows forever.** It holds 311 files, including one `data_ready_*` and one `market_snapshot_*` per day since March. Move files older than 30 days into `health/archive/YYYYMM/`. Archive them; never delete them.
3. **Two clones with confusing roles.**
   - `stock_data/03-working/stock-report-repo` is a working/staging copy, last at HEAD 09-22. `orchestrator_run.json` records it as `repo`.
   - The publisher pushes from `workspace/stock-report`.

   Rename the field (for example `working_repo`), record `published_repo`, and document both.
4. **Runtime pinning.** Python 3.14.3 and pandas 3.0.0 are very new, and there are three venvs (3.14, AI 3.11, qlib 3.9). Commit a `pip freeze` lock for each venv, and keep `env_drift_probe.json` comparing against the locks.
5. **A known failing test.** `test_backfill_targeting…fresh_same_day_ai_replaces_nonempty_generated_fallback` fails on both original and modified code. It covers whether fresh same-day AI analysis replaces fallback text, which affects the AI-value accounting below.

## 5. Strategy outcome (context only; no changes proposed)

From the published CSV (2026-02-24 to 2026-09-30, 2,558 picks with a 5-day return, using the system's own T close / T+1 open rule):
- mean 5-day return −0.80%;
- win rate 41.2%;
- daily cohort Newey–West t = −2.24;
- non-overlapping cohorts mean −1.29%.

The AI score has no measurable within-day ranking power (mean rank IC −0.039, t = −1.55).

There is no benchmark yet, so this does **not** prove the strategy has negative relative value. It does mean the reliability work makes the system *run*; it does not make the picks *good*. Before spending more on AI analysis cost or research tracks, compute excess returns against equal-weight all-A, CSI 1000 and CSI 300 (open item 3 of the Oct 3 plan).

## 6. Suggested order

| When | Item |
|---|---|
| Now (user) | Free ≥ 20 GB of disk (P0-1, action 1) |
| Before 10-08 | P0-1 prune fix and disk guard; P0-2 publish timeout and closure ordering; P0-3 canary and commit of the interpreter fix |
| During the 10-08 acceptance window | Only defect fixes; log any change that touches gated jobs in the acceptance log so the count stays honest |
| After acceptance | P1-1 to P1-3, then P2, then the strategy benchmark |
