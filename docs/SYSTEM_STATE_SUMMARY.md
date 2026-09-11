# Crypto_Quant_Bot — System State Summary

**Date:** 2026-08-28  
**Session:** Single-session recovery after context compaction  
**Engine Status:** DOWN (safe state)

---

## 1. What's Been Working Long-Term (Pre-Today)

### Core Architecture — Stable Across Many Restarts
| Component | Status | Evidence |
|-----------|--------|----------|
| **Martingale-per-bot grid logic** | ✅ Stable | Bots 100001 (SOL), 10019 (XAU), 100317 (BTC hedge), 100313 (XRP hedge) cycling cleanly for weeks |
| **Multiple bots same pair + hedge for margin preservation** | ✅ Stable | SOL (100001 + 100324 hedge), XAU (10019 + 100319 hedge) — perfect parity maintained across daily shutdowns |
| **One-way netting (O-1/O-3 circuit breakers)** | ✅ Working | O-1 equity check + O-3 pair parity check firing every cycle with real `equity_snapshot` rows |
| **Offline-resilient resting orders** | ✅ Working | Grid/TP orders persist on exchange overnight; CID-verification on startup reconstructs fills |
| **Startup CID-verification barrier** | ✅ Working | SOL/XAU auto-healed on this restart (16 SOL + 0.185 XAU drift classified as reparable) |
| **Write queue (serialized DB writes)** | ✅ Stable | No deadlocks/corruption under load |
| **Wipe proof / safe_wipe_bot (Guard 2.0)** | ✅ Working | Double-verifies exchange flat before/after ledger clear |

### Pairs Running Cleanly (No Freezes, No Drift)
| Pair | Parent Bot | Hedge Child | Status |
|------|------------|-------------|--------|
| SOL/USDC | 100001 | 100324 | ✅ Parity -34.98 |
| XAU/USDC | 10019 | 100319 | ✅ Parity -0.064 |
| BTC/USDC | 100317 (hedge) | — | ✅ Cycling |
| XRP/USDC | 100313 (hedge) | — | ✅ Cycling |

---

## 2. Today's Real Fixes (Committed & Tested)

| Fix | Commit | What It Solved | Test |
|-----|--------|----------------|------|
| **GTR cursor bug** | `3c9abab` (regression from `ed1d8cd`) | `_refresh_active_positions` cursor injection bug — fixed with self-acquired connection | Unit test `test_inv31_ground_truth_reconciler.py` passes |
| **MANUAL-GATE bypass** | `5d7b09a` | `REQUIRE_MANUAL_PROOF` bots were still cycling — now blocked in `BotExecutor.process_bot` | Manual test: bot with status=REQUIRE_MANUAL_PROOF skipped |
| **Fill-credit race guard** | `86cfb0c` | Option A: check exchange fill status before `reset_cleared` clears orders | `tests/test_fill_credit_race_guard.py` — pass/fail cases for GRID_1_9 / GRID_5_9 |
| **Sign fix + CID wiring** | `8d3dfd3` | `reconcile_oneway_pair_open_qty` sign logic corrected | `tests/test_oneway_repair_sign.py` passes |

**All 4 commits on branch `refactor/inv31-bot_executor-writequeue`**

---

## 3. Currently Broken / Open

### A. Hedge-Live-Guard Corroboration Gap (CRITICAL)
- **What:** Single `fetch_positions()` read trusted → rewrites `trades.open_qty` + inserts phantom `LIVE_GUARD` rows
- **Trigger:** DNS failure at startup → testnet `fetch_positions()` returned phantom cycling quantities
- **Impact:** 12 DB rewrites in 25 min, 2 rows marked `filled` with `filled_at=0` (no real fill)
- **Design Doc:** `docs/HEDGE_LIVE_GUARD_HARDENING.md`
- **Status:** Engine DOWN, LINK bots frozen, design ready for review

### B. Filled Integrity Gap (CRITICAL)
- **What:** Code paths can write `status=filled` without exchange fill event
- **Trigger:** Hedge-live-guard "logical inference" marked phantom rows filled
- **Secondary:** `ghost_order_cancel` in `parity_gates.py` writes `status='filled'` for audit rows that never existed on exchange
- **Design Doc:** `docs/FILLED_INTEGRITY_FIX.md`
- **Status:** Design ready for review

### C. ETH/LINK Frozen (No Live Position)
| Pair | Bots | Status | Exchange Position |
|------|------|--------|-------------------|
| LINK/USDC | 10020 (parent), 100320 (hedge) | REQUIRE_MANUAL_PROOF / hedge_standby + exclusion list | 0.0 (flat) |
| ETH/USDC | 10011, 10021, 100002, 100316, 100321, 100325 | All REQUIRE_MANUAL_PROOF + exclusion list | 0.904 / 0.003 / 0.0 / 0.98 / 0.0 / 0.0 |

**No un-freeze until design docs reviewed and Phase 1 implemented.**

### D. Bot 100319 (XAU Hedge)
- **Status:** REQUIRE_MANUAL_PROOF from yesterday (unrelated to today)
- **Action:** Leave alone — separate issue

---

## 4. Autonomous Correction Paths — Load-Bearing Map

### 🏗️ CORE DAILY OPERATION (Run Every Cycle / Startup)
| Path | Frequency | Load-Bearing? | Corroboration | Risk |
|------|-----------|---------------|---------------|------|
| **Sync Stale Open Orders** | Every cycle | ✅ YES | Per-order `fetch_order()` verification | LOW |
| **Pre-Commit Resolve** | On order placement | ✅ YES | Multi-source (positions + open orders) | MEDIUM |
| **Reconstruct Offline Fills** | Startup + 15m cooldown | ✅ YES | **CID-proof required** — strongest guard | LOW |
| **Align Memory to Ledger** | After offline fills | ✅ YES | DB-only (no exchange read) | LOW |
| **Phantom Entry Cleanup** | Every reconcile | ✅ YES | DB-only | LOW |
| **GTR Reconcile All (incl. ghost detection)** | Every cycle | ✅ YES | **Single `fetch_positions()` for ghost detection** | HIGH |

### 🛡️ EDGE-CASE SAFETY NETS (Run on Anomaly / Startup Only)
| Path | Trigger | Load-Bearing? | Corroboration | Risk |
|------|---------|---------------|---------------|------|
| **Hedge-Live-Guard (INV30)** | Every cycle for hedge children | ⚠️ PARTIAL | **NONE** — single read | **CRITICAL** |
| **Filled Status Writes** | Various | ⚠️ PARTIAL | **NONE** — can fabricate fills | **CRITICAL** |
| **One-Way Repair** | Startup (sign mismatch) | ⚠️ EDGE | Single position read + guards | MEDIUM |
| **Phantom Purge** | Startup (exchange flat, ledger not) | ⚠️ EDGE | Only acts when exchange=0 (conservative) | MEDIUM |
| **Orphan Exchange Repair** | Startup (ledger flat, exchange not) | ⚠️ EDGE | `_orphan_repair_allowed` + human flags | MEDIUM |
| **Reconcile Pair to Exchange** | Startup repair | ⚠️ EDGE | Delegates to above | MEDIUM |
| **Startup Repair Mismatched** | Startup | ⚠️ EDGE | Skips if ANY gated bot | LOW-MEDIUM |
| **Safe Wipe Bot** | Manual/SL/phantom/orphan | ✅ WHEN NEEDED | **Guard 2.0: double verification** | LOW |
| **Global Wipe Detection** | Startup | ⚠️ EDGE | Requires ALL pairs flat | LOW |

### Summary: What's Actually Load-Bearing
- **6 paths run every cycle/startup** — these are the daily heartbeat
- **2 of those 6 have CRITICAL gaps** (hedge-live-guard, filled writes)
- **GTR ghost detection** (part of core reconcile) also has HIGH risk — same single-read pattern
- **11 paths are safety nets** — only trigger on anomaly, mostly conservative

---

## 5. Phase 1 Scope Decision

### Must Fix Before Any Un-freeze
1. **Hedge-Live-Guard** → Multi-read corroboration + startup cooldown
2. **Filled Integrity** → `exchange_fill_id` required for any `status=filled`

### Should Fix Before Production (Phase 2)
3. **GTR Ghost Detection** → Same multi-read pattern as hedge-live-guard

### Can Defer (Phase 3+)
4. One-Way Repair, Phantom Purge, Orphan Repair, Pre-Commit Resolve — add corroboration incrementally

---

## 6. Key Files Touched Today (For Reference)

```
/c/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/
├── engine/
│   ├── bot_executor.py           # Hedge-live-guard, MANUAL-GATE, sync_stale_open_orders
│   ├── database.py               # sync_trades_from_orders, safe_wipe_bot, reconcile_with_db
│   ├── reconciler.py             # GTR, reconstruct_offline_fills, _align_memory_to_ledger
│   ├── oneway_netting.py         # Ghost detection, one-way repair, wipe_bot_ghost
│   ├── parity_gates.py           # Phantom purge, orphan repair, startup repair
│   ├── ledger.py                 # seal_trade_state, compute_realized_pnl_fifo
│   ├── wipe_proof.py             # Guard 2.0 wipe proof
│   └── runner/startup.py         # Startup sequence
├── tests/
│   ├── test_fill_credit_race_guard.py
│   ├── test_inv31_ground_truth_reconciler.py
│   ├── test_oneway_repair_sign.py
│   └── test_balance_fetch_fix.py
├── docs/
│   ├── HEDGE_LIVE_GUARD_HARDENING.md
│   ├── FILLED_INTEGRITY_FIX.md
│   ├── AUTONOMOUS_CORRECTION_PATHS_AUDIT.md
│   └── ETH_LINK_CATCHUP_FILL_RACE.md (historical)
├── phantom_live_guard_dump_20260828.json  # Permanent trace of fabricated rows
└── config/settings.py            # STARTUP_EXCLUDED_BOT_IDS updated (LINK bots re-added)
```

---

## 7. Bottom Line

**The architecture is sound.** The martingale/hedge/one-way-netting design has proven itself across daily shutdowns for SOL/XAU/BTC/XRP. Today's failures were **implementation discipline gaps in 2-3 specific functions** that trusted noisy exchange reads without corroboration — not systemic flaws.

**Phase 1 (hedge-live-guard + filled integrity) closes the sharp edges.** After that, the 17-path audit shows the rest are either already guarded or conservative safety nets.

**Ready for your review of the 3 design docs against this picture.**