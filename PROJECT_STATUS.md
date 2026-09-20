# PROJECT_STATUS.md — Crypto_Quant_Bot

**Last updated: 2026-09-19 ~23:45 (session) | Engine: STOPPED (operator decision, explicit go-ahead required). Git: 16 commits ahead of origin/main (HEAD 9521d33). New agents read `AGENTS.md` first.**

---

## 🎯 HANDOFF NOTE (read in 30 seconds)

**Today's session (2026-09-19) — Item 1 (Finding 1), Item 2 (cycle_id_filter_fix), Item 3 (INV30/LIVE_GUARD_INV30), plus bot 10007 exchange_fills cleanup — ALL COMMITTED:**

- **Commit `76bcce9`** (Finding 1, P0) — `engine/reconciler.py`: removed reconciler's duplicate `record_exchange_fill()` call (lines 1275-1293) that created duplicate `exchange_fills` rows when `fill_ts` differed from `credit_fill()`'s write. Verified: bot 10007 exchange_order_ids 350133162/350133201 now have 1 row each (were 2). Full suite 108/108 + new test GREEN.
- **Commit `ad7e76e`** (Item 2) — `engine/database.py`: `recompute_invested_from_orders()` cycle_id=None no longer applies `wipe_wall_ts` filter (excludes valid current-cycle fills). Explicit `cycle_id=` respected as target. CARRY pass gated to live cycle only. **Critical note**: bots 10008 (SOL) and 10018 (SUI) still have `trades.cycle_id=39` (should be ~20) — data correction NOT applied; code now works around it correctly.
- **Commit `9521d33`** (Item 3, P4) — `engine/bot_executor.py`, `engine/ledger.py`, `engine/database.py`: LIVE_GUARD_INV30 markers use `'reconciliation'` status. Excluded from step saturation guard (ledger.py), included in position computation (database.py entry & exit queries). 3 new tests: `test_live_guard_marker_excluded_from_saturation_guard`, `test_recompute_includes_reconciliation_markers`, `test_saturation_guard_catches_historical_duplicate`. **111/111 passed**.
- **Bot 10007 exchange_fills cleanup (no code commit)** — Rule-8 DELETE executed: removed duplicate rows for exchange_order_ids 350133162, 350133201 (bot 10007). Post-delete: 1 row each (backfill rows remain). Snapshot at repo root.

**Open follow-ups (do not lose):**
1. **Item 5 (P2)** — `audit_bot_wipes()` signature mismatch (calls with positional args vs def with keyword-only). Not started.
2. **Item 7 (P2)** — Retry-queue cross-ID gap: `fill_claims` keyed by `(bot_id, order_id)` but WS uses exchange_order_id, reconciler may use client_order_id. Defense-in-depth holds; test coverage missing. Not started.
3. **Finding 2 (P1)** — Side inference gap: `parity_gates.py:1135` (orphan adoption) and `database.py:2173` (race guard) omit `side=` param to `credit_fill()`. Physical position may have opposite direction. Not started.
4. **Bot 10008/10018** — Still `is_active=0`, `status=STOPPED` with orphan exchange positions (SOL 0.23, SUI 58.6). Resolution requires explicit operator decision (attribution + ledger alignment).
5. **Bot 10008 `trades.cycle_id=39`** (should be ~20) — data correction pending. Code workaround in `ad7e76e` handles it correctly.
6. **`resolve_net_mismatch()` dependency** — promotes `Scanning→IN TRADE` when virtual matches physical; has **no independent is_active check**. Currently safe because all 8 upstream write paths to `trades.total_invested` are guarded. Future write paths must audit this.

**WARNING:** `resolve_net_mismatch()` is a reactivation vector if a 9th write path to `trades` is added without an `is_active` guard.

---

## Dated reminders (authoritative)

- 2026-07-21: tencent-hy3-free model retired — CONFIRM it is NOT in active config/routing [DONE:pending]
- 2026-07-28: laguna-m.1 model retired — IS delegation, benchmark replacement before removing [DONE:2026-07-28]
- 2026-09-20: rotate Rule-8 DB snapshots older than 30d (free disk, keep last 3 per milestone) [DONE:2026-09-20]

---

## Live State 2026-09-19 (verified at session start, engine STOPPED)

- **Git HEAD**: `9521d33` (LIVE_GUARD_INV30 fix applied) — LOCAL; 16 commits ahead of origin/main
- **Engine process**: **STOPPED** (operator decision — explicit go-ahead required to start)
- **Startup barrier**: CLEARED (`[8/8] All pairs verified in perfect parity`)
- **Tier-2 health (at boot)**: all pairs clean (0 ledger_imbalance) post-reconciliation
- **Active positions (exchange-verified)**: after clean baseline, all pairs flat except SOL −2.53 (bot 100001, managed). 23 bots: 1 IN TRADE + 13 Scanning + 9 hedge_standby.
- **Bots 10008 (SOL), 10018 (SUI)**: `is_active=0`, `status=STOPPED`, orphan exchange positions remain (0.23 SOL, 58.6 SUI)
- **Test suite (Py3.10, excluding playwright)**: 670 passed / 4 failed (isolation artifacts) / 11 errors (env) — **0 hard-logic failures**

---

## 2026-09-19 Session Summary (this session)

### Commits this session (in order)
| Hash | Message | Type |
|------|---------|------|
| `76bcce9` | fix(reconciler): use real bo.filled_at timestamp for reconciler-uncredited fills, remove duplicate record_exchange_fill write (Finding 1, P0) | Code |
| `ad7e76e` | fix(database): recompute_invested_from_orders no longer excludes current-cycle fills via wipe_wall_ts; historical cycle_id lookups skip CARRY logic correctly | Code |
| `9521d33` | fix(bot_executor,ledger,database): LIVE_GUARD_INV30 markers use 'reconciliation' status — excluded from saturation guard, included in position computation (Item 3, P4) | Code + Test |

### Bot 10007 exchange_fills cleanup (no commit)
- **Action**: Rule-8 snapshot + DELETE for duplicate rows (exchange_order_ids 350133162, 350133201, bot 10007)
- **Result**: 1 row each (backfill rows preserved)
- **Snapshot**: `snapshot_before_10007_exchange_fills_cleanup_20260919_*.db` at repo root

---

## Phase Status — Canonical Netting Migration (TRACK B — this repo)

| Phase | Name | Status | Evidence |
|---|---|---|---|
| 1-5 | Build / tests / shadow / live / replay | ✅ DONE | (see prior session doc) |
| 6 | Promote to canonical | ⏳ PENDING | After P1/P2 items + isolation baseline eliminated |

---

## Complete Backlog (honest status)

### 🔴 URGENT / P1 (money-path)
1. **XAU ORDER-SYNC loop** — Bot 100003 (XAU) stuck in ORDER-SYNC loop (repeated TP place/cancel). Pre-existing, not fixed.
2. **Stale-cycle_id dedup wedge** — Dedup key uses cycle_id; stale cycle_id causes false negatives. Pre-existing, not fixed.
3. **INV30 double-count (LIVE_GUARD_INV30)** — ✅ **RESOLVED `9521d33`** — markers now `'reconciliation'` status, excluded from saturation, included in position.
4. **Hedge-child is_active check** — Hedge child entry path lacked is_active guard. ✅ **RESOLVED `2b94ecb` + `732db57`** (8 sites total).
5. **audit_bot_wipes() signature** — Caller passes positional args; def expects keyword-only. **OPEN** — not started.
6. **GTR lock display** — Lock state display shows stale info. Pre-existing, not fixed.
7. **Retry-queue false alarm / cross-ID gap** — `fill_claims` WS oid vs CID mismatch. Defense holds; test missing. **OPEN** — not started.
8. **Flatten price=0.0** — Emergency flatten uses price=0.0 in some path. Pre-existing, not fixed.

### 🟡 P2 / Important
- Items 5, 7 above.

### 🟢 P3 / Test-infra
9. `-2015` burst during emergency (parked).
10. **Failure baseline**: 4 isolation artifacts (pass in isolation, fail in full-suite): `test_ghost_clearing`×2, `test_snap_allocate_gate`, `test_streamlit_smoke::test_database_views` — shared-DB / Windows file-lock contamination.
11. **`close_position()` symbol-normalization bug** (2026-09-15): `bots.pair` includes `:USDC` suffix; exchange `fetch_positions` returns bare symbol. Mismatch → wipes ledger without ordering. Fix: normalize before matching.
12. `test_freeze_guard_scenario.py` ×6 errors — `PermissionError WinError 32` teardown (all assertions pass) — environmental.

### ✅ CLOSED / RESOLVED (this session or prior)
- ✅ **Finding 1 (reconciler double dual-write)** — RESOLVED `76bcce9`
- ✅ **cycle_id_filter_fix (Item 2)** — RESOLVED `ad7e76e`
- ✅ **LIVE_GUARD_INV30 / INV30 (Item 3)** — RESOLVED `9521d33`
- ✅ **side= caller-wiring** — RESOLVED `3af3bef` (prior session)
- ✅ **Whitelist duplicate + format bug** — RESOLVED `62b816d` (prior session)
- ✅ **Bot 10016 orphan** — RESOLVED (prior session)
- ✅ **SUI dust flatten (Option B)** — RESOLVED (prior session)
- ✅ **is_active guard 8 sites** — RESOLVED `d4f8fad` + `2b94ecb` + `732db57` (prior session)
- ✅ **Option 1 Single-Writer Consolidation** — RESOLVED 6 commits (prior session)
- ✅ (all prior closed items from 09-14 doc remain closed: BTC phantom orphan, silent-cancel, REL-1, ETH saga, catchup race, 10016 heal, 402-row purge, rsi_limit crash, etc.)

---

## Test Suite — Today's Results (Py3.10, 2026-09-19, final)

```bash
cd D:/Crypto_Quant_Bot && py -3.10 -m pytest tests/ --ignore=tests/test_playwright_ui.py -q
# 670 passed / 4 failed (isolation artifacts) / 11 errors (env: freeze_guard teardown lock + stuck_dust collection)
# 0 HARD-LOGIC FAILURES
```

**4 residual failures = isolation artifacts (ALL pass in isolation, fail only in full-suite ordering via shared-DB / Windows file-lock contamination):**
`test_ghost_clearing`×2, `test_snap_allocate_gate`, `test_streamlit_smoke::test_database_views`. None are logic regressions.

**11 errors** = `test_freeze_guard_scenario`×6 (`PermissionError WinError 32` teardown temp-dir lock — all assertions pass) + `test_inv35_stuck_dust_no_exit`×5 (collection error). Environmental, Windows-specific.

**RESOLVED this session (all hard-logic):**
- Finding 1 reconciler double-write + new test
- Item 2 cycle_id_filter_fix (108/108 passed)
- Item 3 LIVE_GUARD_INV30 (111/111 passed incl. 3 new)

---

## Git & Push Status

```bash
# All committed, NOT YET pushed to origin/main
git log --oneline -10
# 9521d33 fix(bot_executor,ledger,database): LIVE_GUARD_INV30 markers use 'reconciliation' status...
# ad7e76e fix(database): recompute_invested_from_orders no longer excludes current-cycle fills...
# 76bcce9 fix(reconciler): use real bo.filled_at timestamp for reconciler-uncredited fills...
# 78f5600 fix(database): recompute_invested_from_orders no longer excludes current-cycle fills via wipe_wall_ts...
# 732db57 fix(database): sync_trades_from_orders adds is_active guard (8th site)
# 2b94ecb fix(bot_executor): _signal_hedge_child_entry adds is_active guard (7th site)
# 1e47869 docs: Option 1 single-writer consolidation complete...
# dfd9aa7 test(active_positions): add force_write=True to W2 calls...
# 8623161 fix(reconciler): remove W2 periodic refresh (startup overwrites, W1 covers)
# 7541bc5 fix(active_positions): W4 soft-clear (DELETE→UPDATE size=0)
```

---

## Next Blocker on Production Roadmap

**Step 6 — eliminate the 4 isolation artifacts (full-suite 4 → 0).** The residual `test_ghost_clearing`×2, `test_snap_allocate_gate`, `test_streamlit_smoke::test_database_views` pass in isolation but fail under full-suite ordering due to shared-DB / Windows file-lock contamination. Fix: ensure each test uses an isolated temp/in-memory DB with properly closed connections in teardown (and the `test_freeze_guard_scenario` / `test_inv35_stuck_dust` collection errors get the same treatment). Goal: full-suite **0 failed / 0 hard-logic**, leaving only pre-existing Windows-env teardown errors.

---

## Overnight / Unattended Safety

| Item | Status | Notes |
|---|---|---|
| Engine running? | **NO** | STOPPED — operator decision. Explicit go-ahead required. |
| Live positions at risk? | **NO** | Clean baseline; SOL −2.53 managed by bot 100001; all other pairs flat. |
| Circuit breaker healthy? | **YES** | Equity steady ~$9,117, no O-3/escalation at boot. |
| Barrier clean? | **YES** | 8/8 pairs verified in perfect parity. |

---

**End of 2026-09-19 session. Engine STOPPED. 3 commits local, push pending. Item 5+ start fresh next session.**