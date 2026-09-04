# PROJECT_STATUS.md — Crypto_Quant_Bot

## MODEL ALERT
- MODEL ALERT: poolside/laguna-m.1:free (DELEGATION) is MISSING from live list — needs replacement


Read this FIRST at the start of every task. Update it at the END of every task.
NEVER mark anything "done" / "closed" without proof-before-claiming evidence
attached inline or linked. If something is open, this file says IN PROGRESS
with the exact open items.

## Bot health
- Status: AMBER (engine DOWN since 09:08:46; O-3 false-fired and emergency-cancelled BNB orders; restart blocked pending O-3 fix — TESTNET/DEMO, no live funds at risk)
- Last verified: 2026-09-04 10:35 (O-3 root cause CONFIRMED — see docs/O3_FALSE_FIRE_ROOT_CAUSE_20260904.md). Engine DOWN since 09:08:46, engine-initiated graceful shutdown via O-3 emergency path (NOT platform-reaped — earlier handoff claim corrected: WriteQueue flush + shutdown-seal + last_shutdown.ts logged by the engine itself). O-3 fired 09:08:29 at 37.14%: NOT an algorithm bug — replay reproduces 37.14% bit-for-bit on the live series; f2ecee7 gap-isolation + median-of-top-3 both worked. Root cause: equity read (Cash+Cost+uPnL) raced the engine's own SOL flatten — Cost 5951.43→13.72 between 09:08:22→09:08:29 while cash caught up later; plus post-gap baseline contains pre-flatten SOL Cost, so a restart now re-fires O-3 even with perfect timing (~40% steady-state vs baseline). Consequences verified live: BNB 10007 TP+GRID emergency-cancelled (position 0.02 BNB SHORT alive, NO TP protection), SOL flattened to 0 on exchange (exchange GET confirmed; DB active_positions row 100324 stale 58.16), emergency path dead-on-arrival (-2015 storm, REL-1). ETH/LINK frozen + untouched; 0.901 ETH orphan untouched. Fix design proposed in doc §5 (exclude Cost from O-3 input + median-of-last-3 current + consecutive-confirm + start-of-run rebase) — NOTHING IMPLEMENTED, awaiting operator review. DRAWDOWN_PCT=20 (operator decision, unchanged).
- Last known mismatch: NONE at verify time. Bot 10016 BTC was deflated 0.284->0.007 via run_startup_heal.py --execute (A7 fix, commit 72ba4be); 5 rows terminal-statused reset_cleared (A7 resurrection prevented).
- Test gate: 2026-09-03 (t_801582a5, at tip 37058b7): literal no-args `pytest --collect-only -q` (Py310, clean worktree) = 582 collected / 0 errors / exit 0 — first time the LITERAL no-args invocation collects clean: scripts/test_gate_vs_exchange.py (phantom import _verify_fill_on_exchange from engine.ledger, symbol never existed in any commit) retired at 25f9f8c. Full no-args suite at 37058b7: 577 passed / 13 failed / 2 warnings in 112.83s. All 13 failures are members of the pre-existing-at-4d3ba20 set (adopt_fill_guard x4, auto_repair_guards, ghost_clearing x2, inv38, parity_gates_retry, require_proof_writers x2, seal_short_phantom, snap_allocate_gate, streamlit_smoke test_database_views). The freeze-guard implementation (Config.is_bot_frozen across 12 runtime paths + scenario test test_freeze_guard_scenario.py) passed 9/9, no regressions introduced.
- Ongoing job (Track A, Hermes): Engine DOWN (09:08:46, O-3 emergency shutdown — see docs/O3_FALSE_FIRE_ROOT_CAUSE_20260904.md). NO RESTART until O-3 fix design is approved + implemented + scenario-tested (restart now would re-fire O-3 at ~40% due to post-flatten baseline).

## Open items
- [PARKED 2026-09-03 / DATA BUG] flatten write path stores price=0.0 on flatten_close rows in bot_orders (100316's 3.07 ETH flatten @ ~2406 has price=0). Realized P&L on forced closes is therefore NOT computable from bot_orders — must be derived from exchange fills. Fix the write path when next touching bot_executor/ledger flatten code.
- [PARKED 2026-09-03 / RELIABILITY] -2015 "Invalid API-key, IP, or permissions" burst during emergency-liquidation path (2026-09-03 13:57 + 14:41 runs): mass fetch_positions calls got rate-limited/permission-blocked moments after O-3 trip. If a REAL emergency ever fires, the liquidation path must not be dead-on-arrival. Investigate whether the -2015 burst is rate-limit (429 shadow) or genuine permission scope, and add backoff.
- [NOTE 2026-09-03 / ACCOUNTING] The 0.904 ETH orphan (exchange-held, no trades row owns it) makes DB-reported equity UNDERSTATE true equity by its value (~$2,267 at entry 2405.95). Live exchange pull 2026-09-03: true equity 17,112.21 vs DB 14,845.53. This gap is expected while the orphan exists — do not read it as a loss. Resolves when the orphan decision is made.
- [URGENT / OPERATOR] Engine DOWN since 14:11 (crashed mid-startup-barrier); restart attempt at 15:20 (PID 7000, post-fix code) FATAL'd at startup parity gate: ETH/USDC:USDC ledger=0.079 vs exchange=3.076 (delta 2.997 ETH, ~$7,412) — manual proof required per docs/OPERATOR_MISMATCH_RUNBOOK.md. Run scripts/run_startup_heal.py or resolve manually. Pre-existing divergence (first visible 2026-08-28 DNA-B4-STALE), NOT caused by a54daf1.
- [TRACK A / Hermes] RESTART IN PROGRESS — engine start from 37058b7 with ETH/LINK excluded. Watching for: clean startup barrier, TRADING MODE ACTIVE, cycle increments, FREEZE-GUARD live logs on ETH/LINK bots, SOL/XAU first reconcile cycle clean.
- [TRACK A / Hermes] auth2015: -2015 on fapiPrivateGetIncome. NARROWED to income/read-permission scope disabled on the API key (NOT IP, NOT invalid key — fetch_positions/fetch_ticker/fetch_my_trades all succeed from same IP+key). Bot NEVER calls it in production. PARKED — no action; operator's call whether to enable the permission in Binance UI. Tagged: opened 2026-07-17 by Hermes.
- [TEST DEBT] COLLECTION RESOLVED 2026-09-03 (t_801582a5): literal no-args bare pytest at tip 37058b7 collects 582 / 0 errors / exit 0 — scripts/test_gate_vs_exchange.py phantom-import error (last non-tests/ offender, zero references anywhere in repo) retired at 25f9f8c. History: collection errors were fixed for real via t_d0a8526f commits b2af64f + f79ea2a (the earlier t_33caaa9d "RESOLVED" claim was premature — deletions were uncommitted), but "bare pytest 582 / 0 errors" at f79ea2a was true only for `pytest tests/`; literal no-args still had 1 error (exit 2) until 25f9f8c. REMAINING (open, NOT collection) at 37058b7: 13 failures, ALL members of the pre-existing-at-4d3ba20 set (adopt_fill_guard x4, auto_repair_guards, ghost_clearing x2, inv38, parity_gates_retry, require_proof_writers x2, seal_short_phantom, snap_allocate_gate, streamlit test_database_views). The 4 GTR merge-introduced (7766bf3) failures + inv31 closure-signature x4 + hedge_lifecycle now pass at 37058b7 (consistent with 3b4b1b1 / ed1d8cd; not per-commit re-verified).
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
- laguna-m.1 expires 2026-07-28 (DELEGATION). Before that, benchmark 1-2 replacement candidates for delegation so a replacement is ready.

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