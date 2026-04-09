# Crypto Quant Bot — AI Agent Codebase Guide
**Version: 1.5.0 | Last Updated: 2026-04-07**

> **READ THIS FIRST** before touching any code. This guide documents the critical architectural invariants, known failure patterns, and correct debugging procedures for the system.

---

## 1. Project Layout

```
Crypto_Quant_Bot/
├── engine/               ← Core trading engine (DO NOT TOUCH lightly)
│   ├── runner.py         ← Main bot loop, cycle orchestration, snapshot mgmt
│   ├── bot_executor.py   ← Per-bot order execution (Entry, Grid, TP logic)
│   ├── reconciler.py     ← Offline fill detection & state recovery
│   ├── integrity.py      ← FLAG-ONLY mismatch detection (does NOT mutate state)
│   ├── database.py       ← All SQLite operations (single source of truth layer)
│   ├── exchange_interface.py ← CCXT + raw Binance FAPI wrapper
│   ├── ws_cache.py       ← In-memory position/order snapshot (WS + REST merged)
│   ├── ws_event_handlers.py  ← Real-time WebSocket fill processing
│   ├── websocket_handler.py  ← WS connection manager
│   └── strategies/
│       └── martingale_strategy.py ← Entry/Grid signal logic
├── ui/app.py             ← Streamlit dashboard
├── config/
│   ├── settings.py       ← Config loader (.env → config object)
│   └── constants.py      ← System-wide constants (MIN_ORDER_USD, etc.)
├── crypto_bot.db         ← Live SQLite database (WAL mode)
├── engine.log            ← Rotating log (10MB limit, 5 backups)
├── run_bot.bat           ← Starts the engine
├── restart_runner.bat    ← Kills + restarts the engine
├── SESSION_HANDOFF.md    ← Bug history & session context
└── UNIFIED_BOT_DOCUMENTATION.md ← Full system documentation
```

---

## 2. Critical Architectural Invariants

**NEVER violate these. Each one was added to fix a real catastrophic bug.**

### 2.1. Rejection of Heuristics and Artificial Masking (V1.5.1)
- In **V1.5.1**, all "heuristic-based" guessing, float-drift tolerances, and artificial "gap adoption" rows have been **strictly eliminated**.
- A professional quant system requires $0.00 difference between its internal ledger and verified exchange proofs. If a gap emerges between the physical exchange mass and the database virtual mass, the engine will search Binance's trade history for exact `CQB_` ClientOrderID proofs.
- **If no exact proof is found, the system completely refuses to synthetically mask the gap.** The discrepancy is openly warned as `[FLOAT-DRIFT]` or `[NET-MISMATCH]`, forcing organic resolution (manual user closure or natural 0-state wiping upon TP fill). The bot safely caps its TP orders to `min(virtual_qty, physical_qty)` to prevent ghost direction flips.

### 2.2. Gross-Directional Tracking (V1.4.4)
- **DO NOT** use signed net math for comparison (e.g. `$0 - (-$143k) = $143k mismatch`).
- The system groups positions by direction (LONG vs SHORT). If checking a SHORT, we compare the absolute physical exchange value against the absolute sum of all SHORT `trades.total_invested`.
- This ensures multi-bot strategies covering opposite directions on the same pair do not cannibalize each other safely.

### 2.3. Symbol Normalization
- **Always** use `normalize_symbol(sym)` from `exchange_interface.py` before comparing or storing symbols.
- Binance REST/CCXT returns `"BTC/USDC"` (slash). Binance WebSocket pushes `"BTCUSDC"` (no slash).
- **The ws_cache normalizes all keys.** Bypassing this duplicates positions and triggers Size Discrepancy alerts.

### 2.4. Order Isolation (Multi-Bot Core Rule)
- **NEVER call `cancel_all_orders(pair)`** in bot logic. Always use `cancel_orders_by_bot_id(bot_id, pair)`.
- Every order is tagged with `CQB_{bot_id}_{type}_{step}_{uuid}` as `clientOrderId`.
- Database SQLite indexes on `idx_bot_orders_status`, `idx_bot_orders_type`, `idx_bots_pair` are paramount for the 1s sub-loop performance.

### 2.5. TP Reset Synchronization
- When a TP fills offline, `reconciler.py` calls `reset_bot_after_tp()`.
- **The action_label MUST be precise** (e.g., `'SYSTEM_WIPE'`, `'ENTRY_TIMEOUT'`). Otherwise, the UI history logs phantom "$0.00 profit TP_HIT" events, polluting the trade journal.

### 2.6. reduceOnly Logic & OHLCV Caching
- `reduceOnly=True` on TP orders is ONLY safe when exactly **1 bot** is active on a pair.
- With >1 bots on a pair, Binance rejects TP with `-2022 ReduceOnly Order is rejected` if the net aggregate position points the wrong way.
- **OHLCV Caching**: The engine caches 1m candles for ~25s in `runner.py`. NEVER fetch 1m candles un-cached inside `run_cycle` or it will burn through Binance rate limits.

### 2.7. Carry-Over Ghost Mass Protection
- When a bot is aggressively wiped by the Reconciler (`SYSTEM_WIPE`), `reset_bot_after_tp` **MUST NOT** carry over the mathematical deficit into the next cycle. 
- Administrative actions (`SYSTEM_WIPE`, `MANUAL_CLOSE`, `STOP_LOSS_EXIT`) are permanently blacklisted from generating `_CARRY_` orders. Breaking this rule causes infinite UI inflation loops.

### 2.8. TP Reset Double-Execution Guard
- The REST API polling loop in `bot_executor.execute_tp` can and will race against the `ws_event_handlers`.
- You MUST wrap any manual TP reset with `if total_invested > 0:` to prevent duplicating the reset. Without this limit, the REST loop hits the bot milliseconds after the WS zeroed it, generating false `$0.00 TP_HIT` log entries in the UI.

### 2.9. Pre-Reset Exchange Purge
- Before calling `reset_bot_after_tp()`, the system MUST physically cancel all remaining open orders for that bot on the exchange.
- This prevents orphaned Grid orders from filling "after the bot has moved on", which historically created unowned positions (e.g. XRP 6430 unit orphan).
- Handles both synchronous REST exits and asynchronous WebSocket fills (via `_pending_cancel_after_tp` registry).

### 2.10. Unified Ledger Mathematics
- All position calculations MUST use the definitive ledger sum logic:
    - **Entries:** `('entry', 'grid', 'adoption_add', 'adoption')`
    - **Exits:** `('tp', 'close', 'exit', 'adoption_reduce', 'dust_close', 'sl')`
- Any deviation creates "ledger gaps" where the UI reports a mismatch despite the bot having all necessary data in `bot_orders`.

### 2.11. `safe_wipe_bot()` Is the ONLY Authorized Reset Path (V1.5.0)
- **NEVER** call `reset_bot_after_tp(bot_id, ..., action_label='SYSTEM_WIPE')` directly.
- ALL destructive resets go through `safe_wipe_bot(bot_id, pair, direction, reason, exit_price)` in `database.py`.
- 3 guards enforced before any wipe:
  - **Guard 1:** Blocked if `trades.cycle_phase == 'CARRY_PENDING'`
  - **Guard 2:** Blocked if exchange physical qty > 0.0005
  - **Guard 3:** Blocked if `bot_orders` ledger net qty > 0.0005
- **⚠️ Python scoping trap:** NEVER put `from .database import safe_wipe_bot` inside a function body. It's imported at the TOP of `reconciler.py` (L12). An inline import makes Python treat `safe_wipe_bot` as a local variable for the ENTIRE function, causing `UnboundLocalError` at every earlier call site. This was the root cause of the periodic reconciliation crash (2026-04-07).

### 2.12. `cycle_phase` State Machine (V1.5.0)
- Column: `trades.cycle_phase` TEXT, DEFAULT `'ACTIVE'`
- Transitions:
  - `ACTIVE` → `CARRY_PENDING`: `reset_bot_after_tp()` when carry qty > 0.0001
  - `ACTIVE` → `IDLE`: `reset_bot_after_tp()` clean zero reset
  - `CARRY_PENDING` → `ACTIVE`: `_align_memory_to_ledger()` at startup, or `_update_trade_state_from_fill()` on live WS fill
- A `CARRY_PENDING` bot looks like a ghost but is a real position. Do NOT ghost-detect it.

### 2.13. Single Exchange Snapshot Per Startup (V1.5.0)
- `StateReconciler.prime_startup_snapshot()` fetches ALL positions once, writes `active_positions`, stores in `self._startup_snapshot`.
- **One fetch only.** Do NOT add any `fetch_positions()` call in `_initialize_exchanges()` — it was removed intentionally.
- Periodic reconciler in `run_cycle()` uses `self._reconciler.reconcile_all()` (persistent instance). Do NOT instantiate `StateReconciler(self.exchanges)` inside `run_cycle`.


## 3. Key File Deep-Dives

### `engine/ws_cache.py` (V1.4.3)
In-memory singleton for positions and open orders. Populated by:
- `populate_from_rest()` — called after CCXT REST fetch on startup or when WS cache is stale (>30s)
- `update_position()` — called on every WS `ACCOUNT_UPDATE`

**Critical:** Both methods normalize symbol keys via `normalize_symbol()`. Do not remove this or you'll re-introduce the split-brain phantom position bug.

### `engine/reconciler.py` (V1.5.0)
Multi-phase offline fill detector and position consensus engine.
- **Startup:** `prime_startup_snapshot()` → `reconstruct_offline_fills(48h)` → `_align_memory_to_ledger()` → `resolve_net_mismatch()`
- **Periodic (every 60 cycles):** `self._reconciler.reconcile_all()` — uses SAME persistent instance as startup to preserve CARRY_PENDING awareness.
- **Ghost detection guard:** `resolve_net_mismatch()` skips any bot with `cycle_phase == 'CARRY_PENDING'`.
- **DO NOT** add any `from .database import safe_wipe_bot` inside function bodies — see invariant 2.11.
- **DO NOT** add position math using `total_physical_notional - sum(sister_bots)` — use strict CID matching only.

### `engine/integrity.py`
FLAG-ONLY module. It logs mismatches (`SIZE DISCREPANCY`, `UNMATCHED POSITION`) but **does not mutate state**. The reconciler handles corrections. If you see these warnings in the log, investigate the reconciler and ws_cache.

### `engine/runner.py`
- `run_cycle()` is the main loop. Called every `POLL_INTERVAL_SECONDS` (config).
- Snapshot priority: **WS cache** (if <30s old) → **REST fetch** (fallback).
- The integrity check throttles to every 10 cycles.
- `startup_sync()` → calls `reconstruct_offline_fills(48h)` first, then `run_cycle()` once.

---

## 4. Database Tables (Quick Reference)

| Table | Purpose |
|-------|---------|
| `bots` | Bot config, pair, direction, active flag, status display |
| `trades` | Per-bot virtual position (step, invested, avg_entry, tp_price) |
| `bot_orders` | Full order ledger with CID, status, filled_amount |
| `active_positions` | Exchange position snapshot for UI display (refreshed each cycle) |
| `trade_history` | Immutable closed trade archive |
| `notifications` | UI notification queue |
| `reconciliation_logs` | Audit log of all reconciler actions |

**DB Path:** `crypto_bot.db` in project root. WAL mode enabled.

---

## 5. Common Failure Patterns & Fixes

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `SIZE DISCREPANCY` in logs | ws_cache has duplicate symbol keys (REST+WS) | Check `ws_cache.py` uses `normalize_symbol` |
| Bot resets after TP while still having Binance position | Reconciler treating stale TP fills as active | Check Phase: TP Safety Guard in reconciler |
| `GHOST_RESET` in reconciliation_logs | Bot has `total_invested > 0` but no valid CID evidence | Happens after crash/wipe — use Manual Link in UI |
| Orders rejected `-2022` | `reduceOnly=True` used on multi-bot pair | Check `bot_executor.py` sibling count logic |
| BotExecutor skips all grids | `entry_confirmed = 0` in trades | Reconciler needs to process the entry fill first |
| `ADOPTION_BLOCKED` logs everywhere | Sibling bots already claimed full position value | Expected — prevents double-counting on shared pairs |
| `UnboundLocalError: safe_wipe_bot referenced before assignment` | Inline `import safe_wipe_bot` inside a function body | Remove it — `safe_wipe_bot` is at file top of `reconciler.py` L12 |
| Bot wiped immediately after TP (CARRY position lost) | `safe_wipe_bot()` guard 1 not firing | Check `trades.cycle_phase` is correctly set to `CARRY_PENDING` by `reset_bot_after_tp()` |
| `[SNAPSHOT]` appears multiple times at startup | Duplicate `fetch_positions` call still present | Check `_initialize_exchanges()` — only `prime_startup_snapshot()` should fetch |

---

## 6. How to Restart Safely

```powershell
# Stop and restart the engine (preserves DB state)
.\restart_runner.bat

# Start the UI (separate terminal)
streamlit run ui/app.py

# Watch the log in real time
Get-Content engine.log -Wait -Tail 30
```

**After restart**, the engine will:
1. `check_and_fix_integrity()` — DB startup sanitizer
2. `prime_startup_snapshot()` — fetches ALL exchange positions **once**, writes `active_positions` table
3. `reconstruct_offline_fills(48h)` — credits any fills that happened while offline
4. `_align_memory_to_ledger()` — promotes CARRY_PENDING bots to ACTIVE if fills confirmed
5. `run_cycle()` — begins normal polling using `self._reconciler` (persistent instance)

---

## 7. Testing Checklist Before Any Engine PR

- [ ] No `SIZE DISCREPANCY` in engine.log after 5+ cycles
- [ ] `active_positions` table count matches Binance open positions count
- [ ] `bot_orders` table has no `open` status entries older than 10 minutes without exchange confirmation
- [ ] No `GHOST_RESET` or `PHANTOM_RESET` in `reconciliation_logs` for legitimate positions
- [ ] Bot TP correctly resets to Step 0 / `total_invested = 0` after exchange position closes
- [ ] Multi-bot pairs: each bot's `total_invested` sums to the approximate notional of the shared position
