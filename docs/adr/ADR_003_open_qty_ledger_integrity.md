# ADR-003: open_qty Ledger Integrity & Write-Isolation

**Status:** Implemented  
**Version:** 1.0  
**Implementation Date:** 2026-06-09 (v3.9.21)  
**Replaces:** Ad-hoc direct SQL updates to `trades.open_qty` across the engine

---

## 1. Context — The Problem with Direct SQL Writes

Currently, `trades.open_qty` is the single most critical accumulator in the database. It represents the net quantity that a bot believes it is holding on the exchange. If this quantity deviates from physical reality, the bot will place incorrect Take-Profit (TP) orders, causing severe exposure discrepancies or losses.

Despite its critical importance, `trades.open_qty` is currently treated as a "shared scratchpad" with **10 direct SQL update sites** across 4 different files in the core engine. These direct updates bypass the ledger/audit log (`bot_orders`), violating the fundamental principle of dual-entry bookkeeping.

The consequences observed in production and testing:
1. **Stale Cycle Restoration (Zombie Revival)**: Direct updates to `trades.open_qty` (e.g., in ghost wipes) zero the accumulator but leave matching filled orders in `bot_orders` untouched. The reconciler's subsequent alignment sweeps look at `bot_orders`, conclude the database represents a valid position, and restore the ghost quantity, resurrecting the zombie position.
2. **Attribution Collision & Lack of Audit Trail**: In one-way netting mode, multiple bots can trade on the same pair. Direct updates to `trades.open_qty` (such as oway cross-reduction) change a bot's position without writing a corresponding ledger row (`virtual_netting` or `drift_note`), leaving no audit trail to explain why `open_qty` changed.
3. **Concurrent Write Races**: If two threads run simultaneously, direct SQL updates (`SET open_qty = open_qty - ?`) can execute concurrently with a `seal_trade_state()` call. Since `seal_trade_state()` recomputes `open_qty` from `bot_orders` history, it will completely overwrite and erase the direct SQL update's changes, leading to permanent state corruption.

---

## 2. Decision — Ledger-First Integrity Principles

To enforce strict ledger integrity and eliminate write races, we define the following architectural rules:

### Principle 1: Legitimate Writers
`trades.open_qty` has exactly **two** legitimate writers:
1. `credit_fill()` (in `engine/ledger.py`): Increments or decrements `open_qty` atomically upon a verified fill.
2. `seal_trade_state()` (in `engine/ledger.py`): Performs a full recomputation of the bot's state from the `bot_orders` table and writes the definitive values back to `trades`.

All other direct SQL updates to `trades.open_qty` are technical debt to be eliminated.

### Principle 2: Seal Triggering
`seal_trade_state()` must never run on every engine loop cycle for all bots (which causes database thrashing). It is called only after:
* A confirmed fill credit.
* An explicit cycle reset.
* An explicit reconciler repair action.

### Principle 3: Reconciler Read-Only Principle
The reconciler performs audits by comparing DB state to exchange reality. When a mismatch is discovered, the reconciler **never** updates the `trades` table directly. Instead, it writes a corrective ledger order (e.g. `reconcile_note`, `drift_note`, or `reset_close`) to `bot_orders`, and then calls `seal_trade_state()` to allow the ledger to compute the correct state.

### Principle 4: Manual/Automated Repairs
All manual and automated database repairs must be transactional and go through `bot_orders` insertion followed by `seal_trade_state()`. Direct updates using `UPDATE trades SET open_qty = ...` are prohibited.

---

## 3. Violations & Remediation Plan

The following table lists every function in the codebase that historically violated these principles, along with its remediation path and actual resolution version:

| Function / File | Violation Description | Remediation Path | Status / Actual Release |
|---|---|---|---|
| `_reset_to_hedge_standby` ([engine/bot_executor.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/bot_executor.py)) | Directly sets `open_qty = 0` in `trades`. | Write a `'reset_cleared'` type order into `bot_orders` and call `seal_trade_state()`. | **FIXED in v3.9.20** |
| `_prepare_tp_order_params` ([engine/bot_executor.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/bot_executor.py)) | Backfills `trades.open_qty` with `_recomputed` for pre-v2.1 state. | Call `seal_trade_state()` instead of writing a direct SQL UPDATE. | **FIXED in v3.9.20** |
| `_prepare_tp_order_params` ([engine/bot_executor.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/bot_executor.py)) | Snaps `open_qty=0` directly in SQL if below step size. | Write a `'drift_note'` or `'dust_close'` order and call `seal_trade_state()`. | **FIXED in v3.9.20** |
| `heal_zombie_bots` ([engine/database.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/database.py)) | Directly resets `open_qty = 0` and steps on zombie/ghost step mismatches. | Write a `'drift_note'` or `'reset_cleared'` order to `bot_orders` and trigger `seal_trade_state()`. | **FIXED in v3.9.20** |
| `check_and_fix_integrity` ([engine/database.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/database.py)) | Direct `UPDATE trades SET open_qty=?` to backfill legacy states. | Trigger `seal_trade_state()` for the bot, which naturally updates `open_qty` from history. | **FIXED in v3.9.20 (Obsolete block deleted)** |
| `apply_oneway_entry_cross_reduction` ([engine/oneway_netting.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/oneway_netting.py)) | Subtracts netting quantity directly from sibling `trades.open_qty`. | Remove the direct `trades` UPDATE and call `seal_trade_state()` to apply the `'virtual_netting'` change. | **FIXED in v3.9.20 (v3.9.21 unique constraint patch)** |
| `reconcile_oneway_pair_open_qty` ([engine/oneway_netting.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/oneway_netting.py)) | Trims virtual excess directly on sibling `trades.open_qty`. | Remove the direct `trades` UPDATE and call `seal_trade_state()` to apply the `'drift_note'` change. | **FIXED in v3.9.20** |
| `wipe_hedge_child_ghost` ([engine/oneway_netting.py](file:///c:/Users/Gionie/Documents/GitHub/Crypto_Quant_Bot/engine/oneway_netting.py)) | Zeroes child bot `open_qty` directly in `trades`. | Remove the direct `trades` UPDATE and call `seal_trade_state()` to apply the `'reset_cleared'` and `'drift_note'` changes. | **FIXED in v3.9.20** |
