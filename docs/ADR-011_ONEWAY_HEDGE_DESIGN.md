# ADR-011: One-Way Mode Hedge Bot Design — Structural Conflict Analysis

**Status:** Draft  
**Date:** 2025-07-29  
**Context:** ADR-011 Mechanism A (TP cascade) and Mechanism B (cycle drift) fixes revealed a deeper architectural conflict: hedge-child bots designed for independent virtual positions share one exchange position in One-Way Mode.

---

## Problem Statement

The account operates in **Binance One-Way Mode** (confirmed: `positionSide=None` on all positions). In One-Way Mode:
- One net position per symbol (BTCUSDC)
- `side='buy'` opens/increases LONG; `side='sell'` opens/increases SHORT
- Opposite-side orders **net against the single position** — they cannot coexist independently

**Current design:** 12 hedge pairs (24 bots) where:
- Parent bot (e.g., 10016 LONG) places BUY entries, SELL TPs
- Hedge child (e.g., 100317 SHORT) places SELL entries, BUY flattens
- Both bots' orders hit the **same BTCUSDC symbol on the same account**

**Result:** Every real fill by one bot mathematically cancels part of the other bot's virtual position. The exchange position (−0.018 BTC) is the algebraic sum of all bot activity — not attributable to either bot independently.

---

## Evidence (from `scripts/verify_fills_vs_exchange.py`)

| Bot | Exchange-verified BUY | Exchange-verified SELL | Net |
|-----|----------------------|------------------------|-----|
| 10016 (LONG) | 0.284 BTC | 0.281 BTC | +0.003 LONG |
| 100317 (SHORT hedge) | 0.050 BTC | 0.071 BTC | −0.021 SHORT |
| **Combined (one position)** | **0.334 BTC** | **0.352 BTC** | **−0.018 SHORT** |

**Phantom fills (DB only, no exchange match):** 0.131 BTC SHORT (100317's `LIVE_GUARD_*` orders)

---

## Options

### Option 1: Switch Account to Hedge Mode
**What changes:** Binance account setting flip → `positionSide` becomes required/functional.

| Pros | Cons |
|------|------|
| True independent LONG+SHORT per symbol | **Requires Binance account migration** (cannot change with open positions) |
| Hedge children hold real independent positions | **Invasive codebase changes:** |
| Parity gates track per-side correctly | • `engine/exchange_interface.py`: stop stripping `positionSide` for hedge children |
| | • `bot_executor.py:_resolve_position_side_param()`: return `LONG`/`SHORT` not `BOTH` |
| | • `engine/parity_gates.py`: rewrite for per-side position tracking |
| | • `active_positions` table: split from 1 row/pair → per-side rows |
| | • `get_pair_virtual_net()`: sum by `positionSide` not net |
| | • All 24 active bots must be re-validated |
| | **Highest risk:** live account change mid-session |

**Verdict:** Possible but high-risk, high-effort. Requires exchange-side coordination.

---

### Option 2: Separate Sub-Accounts per Hedge Pair
**What changes:** New API keys, new bot configs, each pair on isolated sub-account.

| Pros | Cons |
|------|------|
| Clean isolation — no netting | **Requires Binance sub-account setup** |
| Existing One-Way code works unchanged | • 12 hedge pairs → 24+ API key sets |
| Parity gates per sub-account — unchanged | • New bot configs, redeploy |
| Cycle tracking per bot — unchanged | • Operational overhead (monitoring, funding) |
| No codebase changes to order placement | |

**Verdict:** Operationally heavy, but codebase-safe. No Binance mode change needed.

---

### Option 3: Virtual Hedging Only (Fix Phantom-Fill Gate, Accept One-Way Netting)
**What changes:** Keep One-Way Mode. Add exchange-confirmation gate in `engine/ledger.py:_credit_fill_internal()` — fail-closed, only record fills verified on exchange.

| Pros | Cons |
|------|------|
| **Single code change** (gate in ledger) | **Does NOT stop real netting conflict** |
| No exchange config change | 10016 BUY and 100317 SELL still fight on exchange |
| No new API keys | Virtual positions never independently match exchange |
| No parity gate rewrite (already One-Way correct) | Cycle tracking: bots share one position |
| No bot config changes | Parity gate sees net −0.018 — cannot attribute to either bot |
| Surgical data fix for 10016/100317 using verified order_ids only | **Silent drift eliminated; structural conflict remains** |

**What Option 3 actually fixes:**
- ✅ Phantom fills (0.131 BTC) never enter DB again
- ✅ `trades` table stays in sync with exchange reality
- ✅ Parity gate passes (virtual net = physical net)

**What Option 3 does NOT fix:**
- ❌ Two bots' real orders netting against each other on every fill
- ❌ Virtual `open_qty` per bot diverges from attributable exchange fills
- ❌ Cycle tracking: `cycle_id` advance depends on net fills, not bot-specific fills

---

## Recommendation

**Option 3 is the least destructive immediate fix** — it stops the data corruption (phantom fills) with one gate and one surgical diff.

**However:** It leaves the structural conflict intact. As long as hedge children exist on One-Way Mode:
- Every real fill by one bot reduces the other's attributable position
- Virtual positions are accounting fictions; only the combined net is real
- Long-term: the design is incompatible with One-Way Mode

**Path forward:**
1. **Immediate:** Apply Option 3 (gate + surgical diff) — stops bleeding
2. **Decide separately:** Migrate to Hedge Mode (Option 1) or Sub-accounts (Option 2) — or redesign to single net-position bots (Option 3b)

---

## Files to Change (Option 3)

1. `engine/ledger.py` — add `_verify_fill_on_exchange()` gate in `_credit_fill_internal()`
2. `scripts/verify_fills_vs_exchange.py` — keep as audit tool
3. Surgical SQL diff for 10016/100317 (exchange-verified order_ids only)

---

## Decision Required

This doc is for review. No code changes, no gate clearing, no apply until explicit approval.