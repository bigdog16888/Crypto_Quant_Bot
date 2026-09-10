# PROJECT_STATUS.md — Crypto_Quant_Bot

**Last updated: 2026-09-09 09:25** | **Engine: RUNNING** from `d5b64ea` (= 7e9f3ca + tripwire provenance copy), up since 2026-09-09 08:03 headless (`python engine/run_engine.py`, log `engine_restart_20260909.log`) after overnight Event-1074 user shutdown 16:34 Sep-8 (same end-of-day pattern as Sep-4/Sep-7; see SYSTEM_UPTIME_MODEL.md). TRADING MODE ACTIVE 08:08:50.

## Live State 2026-09-09 09:25 (all verified)
- **BTC pair closed & re-opened clean**: parent 10016 cycle-19 TP #1200442005 `filled 0.231 @ 78730.9` credited by PRE-COMMIT-RESOLVE at restart; child 100317 over-hedge (0.168 SHORT, unprotected overnight) resolved at restart via INV-26 BE-TP → netting-aware PENDING-FLATTEN closed exactly 0.168 @ 78,543 → DNA-WIPE → hedge_standby.
- **10016 config change APPLIED 08:05** (operator-approved, exchange-verified flat): `base_size 10→160`, `max_steps 8→5`, `HedgeStartStep 7→4`. New O-1 threshold $15,691.51 vs real 5-step ladder ~$4,051 → never trips. Snapshot `BOT10016_CONFIG_PRE_APPLY_SNAPSHOT_20260908.json`.
- **10016 cycle_id heal APPLIED 09:06** (operator-approved two-gate, 8/8 assertions incl. exchange-truth BTC net 0.000): `trades.cycle_id 19→20` (the write reset_bot_after_cycle would have made; downtime-credit path doesn't call it). Wedge dead: next entry `CQB_10016_ENTRY_20_1` placed 09:07:20 → **FILLED 09:14:09 @ 78771.5, 0.002 BTC** → grid-1 filled 09:20:39 → TP_20_1 resting. Cycle 20 ACTIVE, step 2, open 0.004, invested $314.89 (2% of O-1 line). Snapshot `BOT10016_CYCLEID_PRE_HEAL_SNAPSHOT_20260909.json`.
- **All pairs cycling, consensus = exchange** (09:18 cycle): BTC 0.002=0.002, BNB −0.01, SOL −0.18, XAU −0.008, XRP 0, TEST 0. SUI 10018 cycle-20 step-3 open 54.8, TP_20_3 + GRID_20_4 resting (54.8=6.3+14.6+33.9 exact). ETH/LINK freeze-guard firing, O-3 silent, RMP sweep empty.
- **09:08 10018 PENDING-FILL-EXHAUSTED → gated**: FALSE ALARM from catchup-race loser side — step-lock winner credited grid fill 179393496 at 08:26:38 BEFORE retries; loser's "no DB row" check couldn't see sibling claim; gate self-cleared. Ledger exact. **Open code item: retry-queue loser should stand down when the fill is already claimed by a sibling caller.**
- **Open class item (queued for branch): stale-cycle_id on downtime-credit path** — PRE-COMMIT-RESOLVE credits TP fills without advancing cycle_id → DEDUP wedge (hit 09-07 four bots, today 10016; both healed manually). Root fix: downtime-credit routes through reset_bot_after_cycle (same as in-session TP hits), + DEDUP-GUARD could self-advance on unambiguous own-terminal-cycle collision. Tests on branch, not live tree.
- Backlog: LIVE_GUARD_INV30 marker-row double-count class (100317 09-08; bot_executor.py:691 area).
- Backlog 2026-09-09: `_signal_hedge_child_entry` places child hedge entries without checking child `is_active` — WS handlers then drop the resulting fills (`⛔ WS IGNORING Event for INACTIVE Bot 100314`; 0.11 BNB entry placed 09:38:46 for an inactive child → filled → unowned orphan → startup-barrier genuine-anomaly halt 14:57, resolved by reactivating 100314 so INV-26 BE-flattens it). Same edge-case-state-in-placement-path family as LIVE_GUARD_INV30 — pair both in one root-cause branch when there's appetite.
- Backlog 2026-09-09 URGENT-CLASS **[P1 — confirmed by operator 2026-09-10: FIRST item next fix session]**: **silent-cancel-never-lands defect** — `🔥 Cancelled stale CQB_10018_TP_20_3_R1788921114 (No fill, set to cancelling for 1-cycle buffer)` logged 300× over 50 min (13:39–14:29) while the order NEVER actually cancelled on the exchange; no error surfaced, no escalation, the loop just re-flagged "stale" and retried every ~10s. Direct cause of tonight's SUI over-sell: the oversized TP (54.8 vs 21.1 position) filled during downtime because its cancel never landed. A cancel that doesn't succeed must never log as if it did, and must escalate after N failures — not loop silently. CORRELATED SAME-WINDOW SIGNATURE: XAU GRID_14_7 partial-fill credit loop also ran ~300× in the same 13:39–14:29 window (`[ORDER-SYNC] ... new partial fill of 0.014 (cumulative 0.022)` every ~10s, DB stayed at 0.008 — the credit never landed either) while the ghost-sweep NameError was aborting maintain for 10016/10019 (300× each). Two different subsystems silently looping + one crash loop, all in one window — investigate as one systemic event, not three. Suspect same API-failure class as the parked -2015 burst item; the emergency-liquidation path (REL-1) uses the same cancel primitive and could hit the identical silent failure — verify both together when fixing.
- Backlog check-request 2026-09-09: confirm whether REL-1 emergency-liquidation path can hit the same silent-cancel-failure shape (see item above).
- ETH/LINK resumption: operator's call. ETH orphan saga closed +$87.56.
- Full suite at d5b64ea: 597p/13f/6e (identical pre-existing set).



## MODEL ALERT
- MODEL ALERT: poolside/laguna-m.1:free (DELEGATION) is MISSING from live list — needs replacement


Read this FIRST at the start of every task. Update it at the END of every task.
NEVER mark anything "done" / "closed" without proof-before-claiming evidence
attached inline or linked. If something is open, this file says IN PROGRESS
with the exact open items.

## Bot health
- Status: GREEN (engine UP from merged 7e9f3ca+ d5b64ea, PID 21164). BTC 10016/100317 MANUAL-GATE arc 2026-09-08 09:18-11:16 SELF-RESOLVED: root = pre-merge (09-07 15:42, 0531b2c) catchup race missed 0.106 SHORT on 100317 (exchange-verified real fills on reset_cleared rows); live-guard adoption + step-8 0.048 real-fill double-count inflated child virtual to 0.216 vs 0.168 real (DNS outage 08:59-09:18 blocked in-flight verification); parent TP 0.231 filled 10:23 (+$11.79), netting-aware PENDING-FLATTEN closed exactly the real 0.168 (unphysical 0.048 flagged, untouched), SYSTEM_WIPE 10:28 terminalized phantom, GTR cascade-reset parent 10:32, fresh cycle 19 entered 10:33, RMP self-cleared. Parity: VIRTUAL-CONSENSUS BTC 0.032=exchange (11:22) + independent 4-axis gap 0.000000 (11:31). O-1 POS-SIZE-CB also fired on 10016 ($18,292 invested vs $3,322 2x-max on $10 base config) — legitimate breaker, self-cleared on TP; sizing decision = operator. 8edcb76 gap check: NO — today's catchups (17_2, 17_8_R) credited exactly correct.
- Last verified: 2026-09-08 12:33.
- Last verified: 2026-09-04 15:53 (this session). ETH orphan SAGA COMPLETE: exchange flat (SELL 0.901 @ 2504.26 filled 14:30:08.994, net +$87.56, wallet delta matches to the cent), stale active_positions row DELETEd (scripts/eth_orphan_heal.py, pre-heal snapshot on repo root), engine restarted from 0531b2c (PID 8592), PREFLIGHT 3/3 + ETHUSDC parity $0.00/$-0.00 on first barrier, 6 ETH bots RMP-cleared to Scanning/hedge_standby at 15:39:42-43 by engine's own seal, GTR orphan=[] manual_proof=[] (15:52:21), FREEZE-GUARD holds ETH 6 + LINK 2 out of trading (operator decision pending on resumption). SOL downtime-fill self-healed via PRE-COMMIT-RESOLVE (entry placed 14:15:33, filled 15:08:10 while engine down, restored at 15:39:35, TP live 15:40:19; PLAUSIBILITY-BLOCKs on flat siblings 10008/100315/100324 are pair-consensus-OK). Full story: docs/ETH_ORPHAN_SAGA.md.
- Last known mismatch: NONE at verify time. Bot 10016 BTC was deflated 0.284->0.007 via run_startup_heal.py --execute (A7 fix, commit 72ba4be); 5 rows terminal-statused reset_cleared (A7 resurrection prevented).
- Test gate: 2026-09-03 (t_801582a5, at tip 37058b7): literal no-args `pytest --collect-only -q` (Py310, clean worktree) = 582 collected / 0 errors / exit 0 — first time the LITERAL no-args invocation collects clean: scripts/test_gate_vs_exchange.py (phantom import _verify_fill_on_exchange from engine.ledger, symbol never existed in any commit) retired at 25f9f8c. Full no-args suite at 37058b7: 577 passed / 13 failed / 2 warnings in 112.83s. All 13 failures are members of the pre-existing-at-4d3ba20 set (adopt_fill_guard x4, auto_repair_guards, ghost_clearing x2, inv38, parity_gates_retry, require_proof_writers x2, seal_short_phantom, snap_allocate_gate, streamlit_smoke test_database_views). The freeze-guard implementation (Config.is_bot_frozen across 12 runtime paths + scenario test test_freeze_guard_scenario.py) passed 9/9, no regressions introduced.
- Ongoing job (Track A, Hermes): REL-1 FIXED at 4fcd5da (emergency close path live for the first time — wrapper swap + 4 tests, 591 passed full suite). Engine RESTARTED 13:44 from 4fcd5da (PID 17340) after the 13:17 DEPLOY-OUTDATED self-kill (my fault: patched shutdown.py under a live engine — guard worked as designed; BNB orders verified intact, no damage). Startup verified: freeze-guard 13:46:02, TRADING MODE ACTIVE 13:46:07, SOL TP re-placed 13:46:13, cycling. 30-min stability watch NOT yet run on this instance — next session's first item. Session summary: SESSION_2_SUMMARY.md.

## Open items
- [FIXED+COMMITTED 2026-09-04 / HELD FOR RESTART] Catchup fill-credit race — the origin of the ETH orphan AND the LINK freeze. Implemented in worktree `C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot_CATCHUP` branch `fix/catchup-fill-race` @ **8edcb76** (operator-approved order 2→1→3a→4, each landed with its GREEN test passing). Fixes: Fix 2 claim-insert after status checks (ledger.py); Fix 1 record-on-terminal — late fills on terminal rows recorded on the row, status stays terminal, open_qty untouched, returns 'recorded_terminal' (ledger.py); Fix 3a catchup CIDs fully exempt from step-saturation auto_close and credit normally (DEVIATION from original refuse-only design, caught by the acceptance test mid-implementation and endorsed by operator: refuse-only would have recreated the freeze loudly via RMP escalation instead of resolving automatically — INV-30 catchups are additive-by-design exposure since the delta already counts inflight siblings); Fix 4 keep fill_claims on reset/wipe/seal (3 DELETE sites removed: database.py:1817, 4826; ledger.py:1063). Tests: tests/test_catchup_fill_race_replay.py 5 GREEN + tests/test_saga_prevention_replay.py 2 GREEN (exact 08-28 ETH/LINK incident numbers end-to-end through save_bot_order→credit_fill→seal_trade_state; every fill survives the stages that needed manual repair). Full suite in worktree: 593 passed / 13 failed / 6 errors — identical pre-existing-at-0531b2c set, no new regressions. Root cause + evidence index: docs/CATCHUP_FILL_RACE_ROOT_CAUSE_20260904.md. MERGED + RESTARTED 2026-09-08: 8edcb76 onto 395f522 -> 7e9f3ca; acceptance tests 7/7 GREEN at HEAD; engine restarted 08:03, all gates green (see Bot health). ETH/LINK trading resumption remains a SEPARATE operator decision — NOT automatic.
- [BACKLOG 2026-09-08 / NOT URGENT, TRACKED] LIVE_GUARD_INV30 marker-row double-count class: synthetic reconciliation marker rows (CQB_*_LIVE_GUARD_INV30_*, written by _maintain_live_guard_recon_internal, bot_executor.py:691 — CID used AS order_id, no exchange order exists) can double-count with a LATER REAL catchup fill on the same step. Observed 2026-09-08 on 100317: marker 0.048 + real ENTRY_17_8_R 0.048 → virtual 0.216 vs 0.168 real. Safety nets caught it (PENDING-FLATTEN excluded unphysical 0.048, SAFE-WIPE blocked, GTR reset, phantom terminalized by SYSTEM_WIPE) — cost was 1h of RMP + noise, zero loss. Fix when next touching bot_executor: marker rows should not count toward virtual net once a real same-step fill is credited (dedupe marker vs real on same step), + targeted test.
- [PARKED 2026-09-03 / DATA BUG] flatten write path stores price=0.0 on flatten_close rows in bot_orders (100316's 3.07 ETH flatten @ ~2406 has price=0). Realized P&L on forced closes is therefore NOT computable from bot_orders — must be derived from exchange fills. Fix the write path when next touching bot_executor/ledger flatten code.
- [PARKED 2026-09-03 / RELIABILITY] -2015 "Invalid API-key, IP, or permissions" burst during emergency-liquidation path (2026-09-03 13:57 + 14:41 runs): mass fetch_positions calls got rate-limited/permission-blocked moments after O-3 trip. If a REAL emergency ever fires, the liquidation path must not be dead-on-arrival. Investigate whether the -2015 burst is rate-limit (429 shadow) or genuine permission scope, and add backoff.
- [RESOLVED 2026-09-04 / ACCOUNTING] The 0.904 ETH orphan is CLOSED: SELL 0.901 @ 2504.26 filled 14:30:08.994 (order 1012525341), net +$87.56; DB healed; equity now matches wallet ($9,121 Circuit Check 15:48). See docs/ETH_ORPHAN_SAGA.md.
- [RESOLVED+SUPERSEDED 2026-09-08] Engine DOWN item (09-07): handoff claimed up-since-09-04, actually DOWN ~67h after the 09-04 16:42 Event-1074 reboot; operator restarted 11:34 via Start-Monitoring. The pending items from that window are ALL closed: 8edcb76 merged to 7e9f3ca + provenance d5b64ea, engine restarted 09-08 08:03, full suite 597p/13f/6e identical pre-existing set, four wedged bots healed + trading post-heal cycles (see Live State). Current instance: see header.
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