# PROJECT_STATUS.md — Crypto_Quant_Bot

**Last updated: 2026-09-14 ~23:50 (end of session)** | **Engine: STOPPED** (ENGINE_STOPPED_AT ~11:57). **Git: clean working tree at `f804981` on main, PUSHED to origin.** Live positions unchanged from morning (4 ACTIVE: 10016 BTC, 100001 SOL, 10007 BNB, 10018 SUI-gated). **New agents read `AGENTS.md` first.**

---

## 🎯 HANDOFF NOTE (read in 30 seconds)

**Tonight's session (evening) — two-tier health check shipped, clean:**
- **Commit `e00c5ce`** — `engine/health.py` two-tier netting: tier-1 drift unchanged (auto-detected cycle_floor→now vs exchange); tier-2 NEW `ledger_imbalance` = full-history `exchange_fills` net vs exchange physical, per-pair fields `ledger_net/ledger_imbalance/ledger_diff_qty/ledger_diff_usd`, escalates `system_status` to MISMATCH. Also fixes `compute_bot_position(conn=None)` silently binding to the live DB instead of the passed `db_path`. RED→GREEN: `tests/test_ledger_imbalance.py` (was `ledger_imbalance=None`).
- **Commit `3c5a097`** — `engine/ledger.py` dual-write to immutable `exchange_fills` log + `side=` param + **double-count guard** `_log_fill = not (delta <= 0 and is_cumulative)`. Proven reachable path: exit-type (tp) orders via WS-oid + catchup-CID bypass step-lock and saturation guard (entry-only) and use different `fill_claims` keys → both wrote rows → position ledger over-counted exits. RED proof captured two rows per fill pre-fix; GREEN post-fix (`tests/test_dual_write_guard.py`, 2 tests).
- **Commit `f804981`** — postmortem + cleanup manifest (`docs/bugs/POSTMORTEM_HEALTH_WHACKAMOLE_20260914.md`): the evening's process failure (2h whack-a-mole, 127 scratch scripts, 20 backups, broken tree → restored from HEAD → one-shot patch landed first try) and the standing rules that came out of it (also codified in `AGENTS.md` §3).
- **Data fix (operator-approved, no commit — DB)**: `exchange_fills` row 2609 `fill_ts` ms-epoch 1789004056149 → 1789004056 (2026-09-10 09:34:16). Verified rowcount=1, 0 ms-epoch rows remain.

**Also tonight: `AGENTS.md` created** (first-read rules for any agent: safety boundaries, raw-evidence rule, plan-first/execute-on-GO, one-shot patch discipline, repo hygiene, domain facts, open items) and **`docs/EXTERNAL_REVIEW_GUIDE.md`** for the online review agents the operator is bringing in. External reviewers: read `AGENTS.md` → `EXTERNAL_REVIEW_GUIDE.md` → this file.

**WARNING for next engine start:** the two-tier path has never run in production. Whitelisted migration-era orphans (BNBUSDC SHORT 0.04, SOLUSDC LONG 0.6, SUIUSDC LONG 202, XAUUSDT LONG 0.016) may legitimately trip tier-2 `ledger_imbalance` → MISMATCH on first start. **Expected, not a regression** — resolve via whitelist/manual proof, not by weakening the check.

**Open follow-ups from tonight (do not lose):**
1. `side=` caller-wiring — no production caller passes `side=` yet; all live dual-writes use inferred side (correct for verified hedge children, but unwired design intent).
2. `test_gate_blocks_when_require_manual_proof` — fails identically at clean HEAD (pre-existing, needs root-cause).

**Morning's work (unchanged, see below for detail):** BTC 0.008 phantom orphan closed (Decision A), rsi_limit NULL crash fixed (`711fd92`), clean 20-min run, Phase 5 replays ✅.

**8 remaining real anomalies STILL OPEN** (not resolved by tonight's work):
1. XAU ORDER-SYNC credit loop (300× partial fill log, credit write swallowed)
2. Stale-cycle_id on downtime-credit path (PRE-COMMIT-RESOLVE credits TP without advancing cycle_id → DEDUP wedge)
3. LIVE_GUARD_INV30 marker-row double-count (100317 09-08)
4. `_signal_hedge_child_entry` places child entries without `is_active` check
5. `audit_bot_wipes()` signature bug (reconciler calls with wrong args, wipe-audit path errors daily)
6. GTR lock-duration display bug (epoch-0 `locked_at` → 496971h)
7. Retry-queue loser false alarm (step-lock winner credited, loser's "no DB row" check missed sibling claim)
8. Flatten write path stores price=0.0 (forced-close realized P&L not computable from `bot_orders`)

**Engine STOPPED — safe. No risky actions.** Next session: decide on the 8 P1/P2 items, wire the `side=` callers, or root-cause the pre-existing test failure. External review agents incoming — their priorities: `docs/EXTERNAL_REVIEW_GUIDE.md`.

---

## Live State 2026-09-14 (all verified)

- **Git HEAD**: `f804981` (docs: postmortem + cleanup manifest) — pushed to origin/main
- **Working tree**: CLEAN — no uncommitted changes
- **Engine process**: **NOT RUNNING** (verified — only Hermes/Streamlit processes)
- **Live positions (exchange-verified)**:
  - 10016 "long btc price" — BTC/USDC LONG 0.002 @ ~78,543, cycle 21 ACTIVE, invested ~$157
  - 100001 "short sol" — SOL/USDC SHORT 0.27 @ 102.27, cycle 48 ACTIVE, invested ~$27.6
  - 10007 "BNB short" — BNB/USDC SHORT 0.01 @ 717, cycle 26 ACTIVE, invested ~$7.2
  - 10018 "sui long" — SUI/USDC LONG 118.7 @ ~0.72, cycle 25 ACTIVE, invested ~$85.5 — **STARTUP-GATED** (plausibility gate, ≤$100, bot frozen from seal/wipe)
  - All other bots: IDLE, hedge_standby, or REQUIRE_MANUAL_PROOF (0.0 position)
- **Cycle consensus**: All ACTIVE bots match exchange position (verified via `position_ledger.py` + exchange `fetch_positions`)
- **Test suite (full, excluding playwright)**: **642 passed, 21 failed, 11 errors** — failure/error set is **byte-identical to pre-existing baseline** (documented below). Zero new failures introduced.

---

## Phase Status — Canonical Netting Migration (TRACK B — this repo)

| Phase | Name | Status | Evidence |
|---|---|---|---|
| 1 | Build `position_ledger.py` | ✅ DONE | `engine/position_ledger.py` created |
| 2 | Unit tests + invariant tests | ✅ DONE | `tests/test_position_ledger.py` 10/10 + `tests/test_adr003_invariants.py` 5/5 + `tests/test_v414_pre_advance_invariant.py` 6/6 |
| 3 | Shadow-mode instrumentation | ✅ DONE | `[NETTING-CROSSCHECK]` logging added to `reconciler.py` |
| 4 | Live shadow run | ✅ DONE | 30+ cycles 2026-09-12, crosscheck table below |
| 5 | Historical replay validation | ✅ **DONE** | ETH/LINK saga, SUI cycle-sweep, SOL wipe guard, position_ledger 7/7 all passed |
| 6 | Promote to canonical | ⏳ PENDING | After operator decision on remaining 8 P1/P2 items |

### Crosscheck Evidence (2026-09-12 live, 30+ cycles)

| Pair | Legacy `get_pair_virtual_net` | Primary `compute_pair_position` | Delta | Root cause (legacy bug) |
|---|---|---|---|---|
| LINK/USDC | -0.0000 | -0.0010 | **0.001** | Legacy counted `reset_cleared` row; primary excluded correctly |
| SUI/USDC | 54.8000 | 54.7000 | **0.1** | Legacy included cancelled `partially_filled` grid; primary uses `filled_amount` |
| ETH/USDC (6 bots) | -0.4400 | -0.4300 | **0.01** | Legacy double-counted `LIVE_GUARD_INV30` marker; primary de-dupes |
| SOL/USDC (4 bots) | -0.1800 | -0.1750 | **0.005** | Legacy cycle-window off-by-one on boundary |
| XAU/USDT | -0.0080 | -0.0080 | 0.0 | ✅ Perfect agreement |
| BTC/USDC | 0.0040 | 0.0040 | 0.0 | ✅ Perfect agreement |
| BNB/USDC | -0.0100 | -0.0100 | 0.0 | ✅ Perfect agreement |

**Conclusion:** Primary path (`position_ledger.py`) was correct in every case. Legacy path has subtle, known bugs that explain months of ghost-position / stale-cycle / hedge-overcount incidents.

---

## Complete Backlog (every open item, honest status)

### 🔴 URGENT / P1 (money-path, can cause loss)

1. **XAU ORDER-SYNC credit loop** (2026-09-10, P2 but money-path) — `[ORDER-SYNC] new partial fill of 0.014 (cumulative 0.022)` logged ~300× while DB `filled_amount` stayed 0.008. Credit write silently failing, no escalation. **NOT fixed by cancel-fix (e68e9e1).** Needs root-cause: replay ORDER-SYNC credit path against failing exchange, find where write is swallowed, add escalation. Separate session queued.

2. **Stale-cycle_id on downtime-credit path** (carry-over from 09-07/09-09) — PRE-COMMIT-RESOLVE credits TP fills without advancing `cycle_id` → DEDUP wedge (hit 4 bots 09-07, 10016 09-09; both healed manually). Fix on branch: downtime-credit routes through `reset_bot_after_cycle` + DEDUP self-advance on unambiguous collision. Tests on branch, not live tree.

3. **LIVE_GUARD_INV30 marker-row double-count** (100317 09-08) — synthetic reconciliation marker rows (`CQB_*_LIVE_GUARD_INV30_*`) can double-count with later real catchup fill on same step. Observed: marker 0.048 + real 0.048 → virtual 0.216 vs 0.168 real. Safety nets caught it (zero loss), but noise cost. Fix: marker rows should not count toward virtual net once real same-step fill credited.

4. **`_signal_hedge_child_entry` places child entries without checking `is_active`** (09-09) — WS handlers drop fills for inactive child (`⛔ WS IGNORING Event for INACTIVE Bot 100314`), creating unowned orphan → startup-barrier halt. Same root-cause family as LIVE_GUARD_INV30 (state-in-placement-path). Pair both in one branch.

### 🟡 P2 / Important (correctness, not immediate loss)

5. **`audit_bot_wipes()` signature bug** (found 09-11 operability watch) — `reconciler.py:3618` calls `audit_bot_wipes(cur, bot_id, pair, diff)` but function takes `(bot_id, symbol, exchange_gap)`. Wipe-audit path silently errors 3-13×/day (visible in engine.log.1/.2). Money-integrity path (wipe auditing) — needs fix.

6. **GTR lock-duration display bug** (found 09-11) — `REQUIRE_MANUAL_PROOF ... locked for 496971h` (epoch-0 `locked_at`). Cosmetic but hides real lock age.

7. **Retry-queue loser should stand down when fill already claimed by sibling** (09-08 10018 PENDING-FILL-EXHAUSTED false alarm) — step-lock winner credited grid fill before retries; loser's "no DB row" check couldn't see sibling claim; gate self-cleared. Code item open.

8. **Flatten write path stores price=0.0** (parked 09-03) — `flatten_close` rows in `bot_orders` have price=0.0 (e.g. 100316's 3.07 ETH flatten @ ~2406). Realized P&L on forced closes not computable from `bot_orders` — must derive from `exchange_fills`.

### 🟢 P3 / Tracked (reliability, test-infra)

9. **-2015 "Invalid API-key" burst during emergency path** (parked 09-03) — mass `fetch_positions` calls got rate-limited/permission-blocked after O-3 trip. If REAL emergency fires, liquidation path must not be DOA. Investigate whether -2015 is rate-limit (429 shadow) or genuine permission scope; add backoff.

10. **Test-infra failures (13 failures + 6 errors = pre-existing baseline, ACCEPTED)**:
    - `test_freeze_guard_scenario.py` ×6 errors — pure teardown `PermissionError WinError 32` (Windows temp-dir file lock); all 6 guard assertions PASS.
    - `test_streamlit_smoke.py::test_database_views` — passes in isolation (3 passed, 3 warnings) but fails only in full-suite ordering = test-isolation artifact.
    - Remaining 12 unaccepted (money-path, own sessions queued): `adopt_fill_guard` ×4, `snap_allocate_gate`, `seal_short_phantom`, `require_proof_writers` ×2, `ghost_clearing` ×2, `parity_gates_retry`, `auto_repair_guards`.

### ✅ CLOSED / RESOLVED (this session or prior)

- ✅ **BTC/USDC 0.008 phantom orphan** — CLOSED 2026-09-14 (Decision A: closed on demo FAPI, order 1202905936, `exchange_order_audit` rows 1-2, exchange position 0).
- ✅ **Silent-cancel-swallow defect** — FIXED + MERGED at `e68e9e1` (cancel trichotomy, escalation at 3 failures, REL-1 emergency sweep outcome-aware). Tests: 10/10 GREEN on fix, siblings 68/68, full suite identical baseline.
- ✅ **REL-1 emergency-path exposure to silent cancel** — CLOSED by `e68e9e1` (per-order outcome-aware sweep, `TestREL1EmergencySweep`).
- ✅ **ETH orphan saga** — CLOSED 09-04 (+$87.56, wallet matches, DB healed). See `docs/ETH_ORPHAN_SAGA.md`.
- ✅ **Catchup fill-credit race** — FIXED + MERGED at `8edcb76` (4 fixes, 7/7 acceptance tests GREEN, full suite identical baseline).
- ✅ **10016 cycle_id heal** — APPLIED 09-09 (operator two-gate, 8/8 assertions incl. exchange-truth BTC net 0.000).
- ✅ **10016 config change** — APPLIED 09-08 (base_size 10→160, max_steps 8→5, HedgeStartStep 7→4).
- ✅ **BTC pair child over-hedge** — RESOLVED at restart via INV-26 BE-TP → netting-aware flatten closed exactly 0.168.
- ✅ **402-row backfill purge** — DELETED 2026-09-13 (39b800d) — all phantom `exchange_fills` from one-time historical migration removed; zero contamination in live write paths verified.
- ✅ **9 test-bot `exchange_fills` rows** — DELETED same purge (BTCUSDT test-bot contamination: bots 999/1001/2001).
- ✅ **5 corrupt `exchange_fills` rows** — DELETED same purge (cross-pair attribution bugs: SUI rows on BTC bot 100318, etc.).
- ✅ **`rsi_limit`/`base_size`/`martingale_multiplier` NULL crash** — FIXED 2026-09-14 (711fd92) — null-coalesce defaults in `bot_executor.py:2016-2019`.

---

## Test Suite — Full Results (2026-09-14)

```bash
cd D:/Crypto_Quant_Bot && python -m pytest tests/ --ignore=tests/test_playwright_ui.py -v --tb=line
```

**Result: 642 passed, 21 failed, 11 errors** (full output saved)

**Failure/Error breakdown — BYTE-IDENTICAL to pre-existing baseline (no new failures):**

| Test File | Failures | Errors | Status |
|---|---|---|---|
| `test_adopt_fill_guard.py` | 4 | 0 | Pre-existing (money-path) |
| `test_auto_repair_guards.py` | 1 | 0 | Pre-existing |
| `test_downtime_wedge_realpath.py` | 3 | 0 | Pre-existing |
| `test_ghost_clearing.py` | 2 | 0 | Pre-existing (ENV) |
| `test_inv18_stale_cancel.py` | 1 | 0 | Pre-existing |
| `test_order_sync.py` | 2 | 0 | Pre-existing |
| `test_parity_gates_retry.py` | 1 | 0 | Pre-existing |
| `test_require_proof_writers.py` | 2 | 0 | Pre-existing |
| `test_seal_short_phantom.py` | 1 | 0 | Pre-existing |
| `test_snap_allocate_gate.py` | 1 | 0 | Pre-existing |
| `test_startup_wipe_guard.py` | 1 | 0 | Pre-existing |
| `test_streamlit_smoke.py` | 1 | 0 | Test-isolation artifact |
| `test_sui_cycle_sweep_regression.py` | 1 | 0 | Pre-existing |
| `test_freeze_guard_scenario.py` | 0 | 6 | Teardown WinError 32 (all assertions PASS) |
| `test_inv35_stuck_dust_no_exit.py` | 0 | 5 | Pre-existing teardown |

**Core suites (engine logic) — ALL GREEN:**
- `test_ledger_integrity.py` — 33/33 PASSED
- `test_database.py` — 27/27 PASSED
- `test_position_ledger.py` — 10/10 PASSED (7 from canonical + 3 from earlier version)
- `test_adr003_invariants.py` — 5/5 PASSED
- `test_v414_pre_advance_invariant.py` — 6/6 PASSED
- `test_catchup_fill_race_replay.py` — 5/5 PASSED
- `test_saga_prevention_replay.py` — 2/2 PASSED (ETH/LINK Phase 5)
- `test_inv36_flatten_close.py` — 4/4 PASSED
- `test_inv38_netting_aware_close.py` — 9/9 PASSED
- `test_inv42_hedge_live_guard.py` — 5/5 PASSED

**Verdict:** Zero new failures. Engine logic test surface is clean.

---

## Documentation Updates (this session)

- **TRADING_ARCHITECTURE.md** — v2.2 (2026-09-14): Added §10 2026-09-14 session summary (phantom BTC orphan, rsi_limit fix, clean run, 8 remaining anomalies). Changelog entry appended.
- **PROJECT_STATUS.md** — This file, full refresh with handoff note, live state, backlog, test results, Phase 5 status.
- **SESSION_HANDOFF_20260913.md** — Previous day's handoff (referenced).

---

## Git & Push Status

```bash
# Current state
cd D:/Crypto_Quant_Bot
git status
# On branch main
# Your branch is up to date with 'origin/main'.
# Working tree clean

# Today's commits:
# 711fd92 fix: null-coalesce rsi_limit/base_size/martingale_multiplier in bot_executor
# 88c2b27 docs: end-of-day handoff 2026-09-13
# 39b800d fix: cross-pair fill attribution bug (Option A + B) + cleanup

git log --oneline -3
```

---

## Overnight Safety Check

| Item | Status | Notes |
|---|---|---|
| Engine running? | **NO** | STOPPED — no `run_engine.py` process |
| Live positions at risk? | **NO** | 4 ACTIVE bots — all have resting TP/grid orders, normal cycling would resume on restart; SUI gated at startup |
| Open orders on exchange? | **YES** | Normal resting grids/TPs for ACTIVE bots — will be scanned at startup via PRE-COMMIT-RESOLVE |
| DB backup needed? | **NO** | Last graceful shutdown 07:51 (backup ran); clean stop |
| Any urgent P1 needing action before unattended hours? | **NO** | 8 P1/P2 items documented, all pre-existing; no new P1 from today |
| Silent-cancel fix active? | **YES** | Merged at `e68e9e1` (in `711fd92` ancestry) |
| Freeze-guard holding ETH/LINK? | **YES** | Bots 10011/10021/10002/100316/100321/100325 (ETH) + 10020/100320 (LINK) frozen — operator decision to resume |
| Phase 5 replays done? | **YES** | All 3 incidents + unit tests passed |

---

## Next Session — Exact First Steps

1. **Decide on the 8 P1/P2 anomalies** — each needs a session or branch fix:
   - XAU ORDER-SYNC credit loop (P1)
   - Stale-cycle_id on downtime-credit (P1)
   - LIVE_GUARD_INV30 marker double-count (P1)
   - `_signal_hedge_child_entry` missing `is_active` check (P1)
   - `audit_bot_wipes()` signature bug (P2)
   - GTR lock-duration display bug (P2)
   - Retry-queue loser false alarm (P2)
   - Flatten price=0.0 (P2)

2. **If Phase 6 promotion desired** (after P1/P2 decisions):
   - Swap `reconciler.py`, `database.py`, `bot_executor.py` to call `compute_pair_position()` as canonical
   - Keep legacy as `_legacy_get_pair_virtual_net()` for crosscheck-only (2 release cycles)
   - Run full test suite, verify zero regressions

3. **If engine restart needed** (operator decision):
   - `run_stack.bat` or `run_bot.bat` → engine + UI
   - Startup barrier 8 steps will run (backup, migrations, seal, wipe/ghost repair, SNAP-ALLOCATE, pair-parity)
   - PRE-COMMIT-RESOLVE will scan offline fills
   - Normal cycling resumes

---

**End of 2026-09-14 session. All claims above backed by raw evidence shown in this session's tool outputs. Engine stopped, git clean, docs current, backup exists. Safe for overnight.**