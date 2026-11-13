# September 7 trading-day acceptance — OPEN / NOT PASSED

Checked from 23:16 UTC+08. Do not count this day as clean acceptance.

## Latest check — September 9 night

See `2026-09-09-hermes-stock-recovery.md`. September8 research ended failed: short-track600.05s timeout, other stages1181.591/2247.427/1549.426s completed. September9 main publication timed out on a GitHub HTTP/2 connection; broadcast and research were correctly blocked. The existing publisher was hardened and manually recovered in16.949s; Pages34369782348 and CI34369782354 succeeded for ed9984bc, actual HTTPS shows September9 and AI20/20. Original failure records were preserved unchanged. Stage-progress persistence and publisher diagnostics have passing regressions; final339 script tests plus20 end-to-end tests pass. Full new-day research is still unverified: zero clean actual days. All old process/session handles below are historical, not active.

## Historical check — September 8 night

See `2026-09-08-hermes-stock-immediate-audit.md`. September8 actual production/AI/Pages and live identity pass. Actual research execution6c1f061f99854d469efb7f775360e9e7 remains running; parent6556, final factory7565 from23:22:15. No September8 short-track artifacts were found; reason awaits final stage summary. Do not count September8 as clean. Newly verified cost-label inconsistency and progress visibility gap are recorded in the immediate audit. Existing heartbeat temporarily adds00:15/01:15; preserve and follow the same execution, do not rerun finished session43120 or child6920.

## Latest verified state — September 8, 08:16

The corrective implementation and complete historical replay are verified. Actual trading-day acceptance remains open, targeting September8 and9 (both open in the local exchange calendar). September7 remains failed; no historical record was relabelled.

- Replay session43120 exited0 and PID82195 is absent. Completed07:18:45, total871.709s: short track5.229s, real factor attribution215.167s, daily search400.481s, standalone factory250.826s. All four stages returned0 within their existing deadlines.
- Harness `verification_passed=true`: four healthy current tracking reports; newly generated factor/search/factory artifacts; all4 frozen snapshot hashes unchanged; original scheduled failure-summary hash unchanged. An additional08:15 readback confirmed matching factory run/date, `production_frozen=true`, current diagnostic-only attribution, and the original23:40:53 failed summary still present.
- Day-search runtime improved from the actual2400-second timeout to400.481s in this replay; standalone factory from2454.642s to250.826s. These are measured replay results, not a guarantee of future daily timings. New-day short-track generation previously283.601s differs from a5-second frozen-day resume.
- Historical research coverage warnings remain: proxy-chip dates are excluded and some older dates lack usable features. These do not authorize strategy promotion or establish investment effectiveness; no missing data was fabricated to make the run pass.
- Temporary08:15/09:15 continuation checks have been removed using the existing automation. Heartbeat `hermes` is ACTIVE at weekdays23:15, now instructed to inspect September8/9 actual scheduled results, avoid replaying the completed session, and pause only after two clean trading days pass.
- Source repair remains8971563; reproducible replay harness and this verification record are saved separately from generated market artifacts. No public push was needed for these local research fixes.

## September 8 follow-up

- At08:26 verified the first actual scheduled component after the repair: morning event execution `56bb38fa7f504861ab26515d17a5a632` completed08:15:00.887945–08:15:15.853207, error=null (14.965s). The daily tracking artifact has signal/valuation date20260907 (prior-day cutoff), operational status `healthy_no_eligible_candidates`,1 new announcement and0 eligible events. Decision remains `observe_only`, authority `observe_only_no_auto_order`; promotion gates did not pass and were not relaxed. Scheduler log08:15:15 records empty output and skipped delivery, matching the quiet-success contract. This verifies the morning component only; September8 main/AI/Pages/post-publish acceptance is still pending tonight. Existing weekday23:15 heartbeat remains ACTIVE; no duplicate job or replay was started.

- At07:02 verified original ledger terminal `failed`, finished September7 23:40:55.261231+08, and old processes absent. Final stages: short track283.601s success; old helper-library attribution2.353s empty success (invalid evidence); daily search2400.056s timeout; standalone factory2454.642s success. Original summary is retained unchanged.
- Actual scheduler log `agent.log` at23:40:55.230 records delivery to the existing stock Discord channel; job `last_delivery_error=null`. This proves the recorded transport success, not a human reading the alert.
- Repair checkpoint: local commit `8971563`,8 exact source/test/verification files, no market-data commit.
- Corrected full historical replay started September8 07:04:14: session43120/PID82195. Follow that handle only. Live progress/result: `research-replay-20260908.json`, harness `verify_research_replay_20260908.py` in this directory. Historical clock confined to harness; live release identity checks retained, production gate unchanged; original failure-summary digest and4 frozen snapshot hashes checked on completion. Short track passed5.229s; other stages pending. This replay cannot count as actual trading-day acceptance.
- At07:11 confirmed factor attribution also passed215.167s, with actual result generated07:07:54, as_of20260907 and90 valid days. Replay remains active at daily search; no terminal success claim. Existing heartbeat now checks08:15/09:15/23:15 and follows session43120/PID82195. Restore evening-only after this verification closes.

## Confirmed production evidence

- Scheduled main execution `7e854fd7c0d447d58a067f6be93754f1`: completed, 19:30:11–19:52:34. Run `stock-20260907T113012Z`, trade date `20260907`; stages completed and final AI/validation ready.
- Scheduled AI publication execution `344c3bb81ba3497d86b57e0290cc7ef6`: completed 20:00:20.
- Pages run `34118862444` and CI run `34118862451`: success for `0b044c3f35053256028a8ee178bf022c99fc328e`.
- Actual HTTPS readback: matching run/date, `published=true`, `publish_ready=true`, `ai_complete=true`, `publish_mode=full`, both verdict `publish_ok=true`.
- Scheduled morning event execution `38f27867543c4392bee4591a4f0888f6`: completed 08:15:01–08:15:32; output silent. Tracking uses prior calendar-day event cutoff `20260906`, operational status healthy and no new announcement events. Promotion gates remain closed.
- Four current short-track reports dated `20260907` were written 22:19:45–22:19:55 and report healthy. New-day generation is not equivalent to the 4-second frozen-day resume benchmark.

## Findings and corrective work

1. `factorAttribution` launched `factor_attribution.py`, a helper library without a CLI. It returned exit 0 without producing analysis. The actual CLI is `factor_attribution_report.py`; its public result was still as of August 21. The previous exit-code-based replay verification missed this defect. Corrected the stage to invoke the real CLI with matching `--as-of` and `--refresh-days 1`, preserving full-history aggregation. Two new tests failed before correction; 32 focused tests passed afterward. The real September 7 attribution supplement (session 20605) completed exit 0 at 23:26:11, with `as_of=20260907`, full-history aggregation, 90 valid signal days out of 145 considered, `promotion_eligible=false` and no automatic Phase2. Read back both working and local-public state. This is manual supplemental evidence, not scheduled acceptance.
2. Daily search wrote its September 7 search report at 22:31:41 (699.8 seconds), but its nested factory evaluation did not finish before the 2400-second parent-stage boundary: the next standalone factory began 23:00:01. Final parent summary still pending; do not invent its terminal result.
3. Factory backtests recomputed the same raw scores for every experiment (7 active experiments, with 30/70/100-day windows). Added a run-local score cache; no persistent cache and no selection/return formula changes. Regression compares cached and uncached full results and verifies input rows unchanged. Real-data September 2–7 parity passed exactly: uncached 6.030s, first cached pass 1.533s, reused pass 0.271s, 3 signal dates. The helper `verify_factory_cache_20260907.py` is read-only with respect to experiment/public artifacts.
4. Full script regression suite after these changes: 336 tests passed in 16.731 seconds. This is not a substitute for pending real pipeline verification.

## Active process / safety record

- Original scheduled research execution `b272eebc3b124403b4b388fde9303810` was confirmed running via SQLite ledger AND live processes: parent 70456, factory 70958. No duplicate whole-pipeline job, restart or forced termination was performed.
- Native sample confirmed factory CPU-bound; Python remote stack access required unavailable privileged access, so no credential prompt or bypass was attempted. A separate read-only cached-panel profile completed (five evaluations, 60 days, 0.863 seconds), and actual cached/uncached factory comparison supplied direct performance evidence.
- Original process already loaded old code. Source edits apply to future invocations; its result must be preserved honestly, including any failure and notification.
- Remaining: observe original terminal result/delivery; verify corrected complete research after original process exits; retain automatic next-trading-day acceptance. No automatic orders, frozen-list changes, or publication of raw data.
- Existing heartbeat `hermes` updated to add 00:15 and 01:15 checks alongside 23:15 on weekdays, with instructions to follow the same process, remain quiet unless meaningful change, and restore evening-only cadence after the in-flight repair verification finishes. No duplicate monitor was created.
- Four standalone recipe parity checks also passed; only scoped source whitespace check passed (unrelated pre-existing generated CSV whitespace was left untouched). Runtime has no pytest installed, so used the existing standalone test entrypoint instead of installing dependencies.
- Independent bounded review of the four changed source/test files: approved, no findings; reviewer reran 9 gate tests and 1 cache test and checked call-site wiring and row immutability. This approval covers the patch, not the still-pending full production run.
- Last in-flight check at about 23:33: original factory PID70958 remained CPU-active after33 minutes, with original research ledger still `running`. Only that same job will be followed; next heartbeat00:15.
