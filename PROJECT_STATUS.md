# PROJECT_STATUS.md — Crypto_Quant_Bot

## MODEL ALERT
- MODEL ALERT: poolside/laguna-m.1:free (DELEGATION) is MISSING from live list — needs replacement


Read this FIRST at the start of every task. Update it at the END of every task.
NEVER mark anything "done" / "closed" without proof-before-claiming evidence
attached inline or linked. If something is open, this file says IN PROGRESS
with the exact open items.

## Bot health
- Status: GREEN (forward-testing in progress, TESTNET/DEMO)
- Last verified: 2026-07-17 (full four-way pair reconciliation: BTC +0.007, SUI -79.4, XRP +14.7 — all matched exchange via get_pair_virtual_net / bot_orders / fetch_positions / active_positions)
- Last known mismatch: NONE at verify time. Bot 10016 BTC was deflated 0.284->0.007 via run_startup_heal.py --execute (A7 fix, commit 72ba4be); 5 rows terminal-statused reset_cleared (A7 resurrection prevented).
- Test gate: 2026-09-02 (this cleanup t_33caaa9d): full suite via Py310 = 516 passed / 19 failed. All 19 failures pre-exist at 4d3ba20 (verified by baseline worktree run); a54daf1 healed exactly 2 (test_inv32_safe_wipe_not_blocked_by_sibling, test_inv35 manual_close_succeeds_when_exchange_flat — the safe_wipe_bot external-cursor UnboundLocalError) and added 1 passing regression test. Collection errors FIXED: test_inv36_flatten_close.py (removed dead start_db_worker/stop_db_worker import), test_verify_fill_on_exchange.py (retired — tested phantom _verify_fill_on_exchange function that never existed; real API verify_filled_orders_against_exchange covered by test_verify_filled_orders_timeout.py), test_inv34.py (dead import committed).
- Ongoing job (Track A, Hermes): continue watching engine.log / active_positions for drift, ghost positions, mismatches. This is continuous, not one-time.

## Open items
- [URGENT / OPERATOR] Engine DOWN since 14:11 (crashed mid-startup-barrier); restart attempt at 15:20 (PID 7000, post-fix code) FATAL'd at startup parity gate: ETH/USDC:USDC ledger=0.079 vs exchange=3.076 (delta 2.997 ETH, ~$7,412) — manual proof required per docs/OPERATOR_MISMATCH_RUNBOOK.md. Run scripts/run_startup_heal.py or resolve manually. Pre-existing divergence (first visible 2026-08-28 DNA-B4-STALE), NOT caused by a54daf1.
- [TRACK A / Hermes] auth2015: -2015 on fapiPrivateGetIncome. NARROWED to income/read-permission scope disabled on the API key (NOT IP, NOT invalid key — fetch_positions/fetch_ticker/fetch_my_trades all succeed from same IP+key). Bot NEVER calls it in production. PARKED — no action; operator's call whether to enable the permission in Binance UI. Tagged: opened 2026-07-17 by Hermes.
- [TEST DEBT] RESOLVED 2026-09-02 via t_33caaa9d: test_inv36_flatten_close.py + test_verify_fill_on_exchange.py collection errors fixed; test_inv34.py dead import committed. Bare pytest now collects cleanly.
- [Memory store] was in fail-loop 2026-07-17; junk entry cleaned 2026-07-17 (attempt 2). Store now 2,080/2,200, 8 valid entries. No open memory item.

## OS-level redundancy (ACTIVE)
- Hermes cron = InProcessCronScheduler (60s in-process ticker), NOT Windows Task Scheduler. One-time jobs silently never fire if laptop off.
- NET 1 (authoritative): session-start date-check — scripts/session_start_check.py + tests/test_session_start_check.py (committed, 4 PASS). Run by Hermes at session start.
- NET 2 (redundant, OS-level): Windows Task Scheduler task "CQB_SessionStartCheck" (DAILY 09:05, StartWhenAvailable=TRUE, DisallowStartIfOnBatteries=FALSE). Runs scripts/run_session_start_check.bat -> session_start_check.py -> appends to session_start_task.log. Registered OS-level (C:/Windows/System32/Tasks/CQB_SessionStartCheck), fires on wake if laptop was off. This is the belt-and-suspenders layer independent of the Hermes process.
- Both nets verified 2026-07-17. auth2015 stays parked (no action).

## In-progress structural work (TRACK B — SEPARATE TOOL: Cline/Windsurf)
- engine/runner.py mid-migration to mixin-based package engine/runner/.
- Module 1 (ShutdownMixin): COMPLETE, tested, committed (commit 748ba15).
- Module 2 (WebSocketLifecycleMixin): COMPLETE, tested, committed.
- Module 3 (StartupMixin): NEXT. NOT done. Those methods still live directly in engine/runner/__init__.py BotRunner class body.
- Module 4 (CycleLoopMixin): NOT done.
- Module 5 (SharedMixin): NOT done.
- RULE: Do NOT modify engine/runner/ files without checking this status first — the other tool (Cline/Windsurf) owns that migration and a conflicting edit will clash.

## Standing rules (binding, see CODEBASE_GUIDE.md §12 for full text)
- Closed-language ban: never say "closed"/"done" while any item is open.
- Proof before claiming a fix: show before/after diff + raw verify output + full-vs-targeted suite disclosure.
- Stop after 2 tool failures on same action; report exact error; ask for different approach.
- Clean junk memory same session it's noticed.
- Two-tool-loop in one session = possible env/session-state issue; consider fresh session.
- Memory-vs-Repo precedence: CODEBASE_GUIDE.md §8/§12 is SOURCE OF TRUTH; if memory and repo disagree, repo wins.

## Adversarial Self-Review (2026-07-17 session — see §12 for full)
1. Unverified claims: none — BTC/Sui/XRP all raw-verified; auth2015 narrowed to permission-scope (3/3 other calls succeed same IP+key); SUI "−31.8 vs −79.4" was a FALSE ALARM corrected plainly.
2. Internal contradictions: none current (earlier SUI alarm retracted).
3. Silent scope narrowing: heal "all pairs match" banner re-pulled independently — disclosed.
4. Mechanism honesty: fetch_ticker diff + 6 call sites + smoke shown; A7 deflate code + 5 reset_cleared rows shown.
5. Test coverage gaps: fetch_ticker ran TARGETED only (455 passed subset, NOT full suite); no dedicated regression test added for the wrapper. 2 test_ghost_clearing.py fails = ENVIRONMENTAL, pre-existing.
6. Reversibility: fetch_ticker on branch 35caa9c (git-reversible); heal --execute proof-only but DID mutate DB (reset_cleared written; pre-exec snapshot outside scratch/).

## 3 invariants most prone to careless future violation (from §3 read)
- INV-19 / INV-20 (Unique (bot_id, client_order_id) / fill_claims UNIQUE): any new order-path code that doesn't reuse the CQB_ client_order_id scheme silently double-credits. The DEDUP-GUARD we verified is the only guard — bypassing it = double-fill.
- INV-15 (Two-phase atomic reset: exchange first, DB second): if a future heal/reset writes DB before confirming exchange, you get phantom-closed positions (exactly the A7 class we fixed). Highest stakes.
- INV-31 (GroundTruthReconciler reads exchange directly, fail-fast sentinel, write-through refreshes active_positions): if someone makes it "forgiving" (swallow exchange-read errors), you get the SUI-stale-banner class (banner green, persisted state wrong).

## Open architectural debt (from §6/§9 read — NOT yet audited this session)
- DEBT-004 (cache thread-safety), DEBT-005 (fragment netting duplication), DEBT-006 (headless ENGINE_STARTED_AT gap), DEBT-007 (stale pending_placement deadlock) referenced as OPEN in §9/§10. DEBT-001/002/003 marked RESOLVED.

## Model chain (authoritative)
DEFAULT: nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free
FALLBACK: nvidia/nemotron-3-ultra-550b-a55b:free
DELEGATION: poolside/laguna-m.1:free
- Selected 2026-07-17 via 3-incident benchmark (SOL CID / ADR-006 / runner __init__ mixin). All 3 candidates scored 3/3. nano=fastest+complete; ultra=stopgap fallback (3/3 but 12x slower); laguna=delegation (unchanged). hy3:free was benchmarked 3/3 but EXPIRED 2026-07-21 — excluded.
- RULE (free-tier churn is structural): every model here must have expiry checked automatically (scripts/check_model_health.py), not remembered manually. Degrade gracefully (auto-promote benchmarked fallback) but NEVER silently substitute an unbenchmarked model into DEFAULT without explicit approval.

## Standing model-health rule
- Free models on OpenRouter rotate (hy3 2026-07-21, laguna-m.1 2026-07-28 — confirmed pattern). Any model in DEFAULT/FALLBACK/DELEGATION must have expiry auto-checked. Auto-promote pre-benchmarked fallbacks only. If fallback chain empty → STOP and alert operator to run fresh benchmark; never grab untested :free model.
- laguna-m.1 expires 2026-07-28 (DELEGATION). Before that, benchmark 1-2 replacement candidates for delegation so a replacement is ready.

## Known model dates
- 2026-07-21: hy3 retirement (NOT in our config — no action, but confirmed gone).
- 2026-07-28: laguna-m.1 retirement (IS our DELEGATION — must have replacement benchmarked before this date).

## Dated reminders (authoritative)
- 2026-07-21: hy3 retirement (NOT in config — confirm gone, no action) [DONE:pending]
- 2026-07-24: laguna-m.1 replacement benchmark due (DELEGATION expires 2026-07-28 — benchmark 1-2 candidates via 3-incident test before then) [DONE:pending]
- 2026-07-28: laguna-m.1 retirement (IS our DELEGATION — replacement must be benchmarked+ready) [DONE:pending]

## Session-start safety net (REPLACES unreliable cron for dated reminders)
- Hermes cron = IN-PROCESS 60s ticker thread (InProcessCronScheduler), NOT Windows Task Scheduler. If laptop off at scheduled time, ONE-TIME jobs SILENTLY NEVER FIRE (no OS make-up). Therefore dated reminders CANNOT depend on cron.
- REAL guarantee: at the START of every session, run `python scripts/session_start_check.py`. It compares today vs every dated reminder in the block above; if today >= date and not [DONE:...], it prints a TOP-OF-FILE alert. This works regardless of laptop uptime.
- Cron jobs are a nice-to-have for same-day awareness only. Session-start date check is the authoritative trigger.
- To mark a reminder done: change `[DONE:pending]` to `[DONE:YYYY-MM-DD]` after the action is taken.


## Model benchmark — 2026-07-17 (live OpenRouter, NOT cached config)

Live free-tier (`*:free`) count: 20 models (re-fetched; my cached config missed
`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` — validates "don't trust cache").

3 real incidents as known-answer cases (grounded in CODEBASE_GUIDE §3/§6):
- CASE1 SOL CID-collision / ETH $45 orphan = virtual-netting synthetic cross-bot rows.
- CASE2 ADR-006 hedge-child math (INV-3 + INV-29, per-bot accounting).
- CASE3 runner.py `__init__.py` mixin-placement contradiction (Track B).

Scored (pass = correct mechanism trace, no self-contradiction):
- incumbent nemotron-3-ultra-550b-a55b:free (ctx 1M): 3/3. Longest/highest rate-limit exposure.
- A nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free (ctx 256k): 3/3. Purpose-built reasoning, smallest/fastest, lowest rate-limit risk. NEW live (not in cached config).
- B tencent/hy3:free (ctx 262k): 3/3. Cleanest CASE3. Proven 200 earlier.

NOTE: naive "contradiction_signal" fired on all 3 for CASE3 — FALSE POSITIVE
(word "contradict" appeared in their valid reasoning). Manual read: all 3 passed
CASE3 correctly. Disclosed per proof-before-claim rule.

RECOMMENDATION (operator picks): promote A (nano-omni-reasoning:free) to
DEFAULT reasoning — 3/3 correct, purpose-built reasoning, lowest rate-limit risk,
newly-live. Keep B (hy3:free) as fallback. Keep laguna-m.1:free delegation
(no failure evidence). Incumbent demoted to secondary.

## Model health log
- [2026-08-07 10:00] ALERTS=1; MODEL ALERT: poolside/laguna-m.1:free (DELEGATION) is MISSING from live list — needs replacement
- [2026-07-17 13:19] PASS — all configured models present, free, no imminent expiry
