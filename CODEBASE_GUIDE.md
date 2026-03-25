# Crypto Quant Bot — AI Agent Codebase Guide
**Version: 1.4.3 | Last Updated: 2026-03-13**

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

### 2.1. Proof-Only Consensus (V1.4.4 Core Rule)
- **Do NOT trust aggregate exchange notional sums** to make decisions about individual bots.
- The Engine purely trusts **explicit Order ID proofs (Client Order IDs)** tied to bot executions.
- If a bot's `total_invested` is out of sync, it MUST be corrected via direct CID `bot_orders` match in the `reconciler.py`. Heuristic-based tracking causes UI ghost position inflation.

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

---

## 3. Key File Deep-Dives

### `engine/ws_cache.py` (V1.4.3)
In-memory singleton for positions and open orders. Populated by:
- `populate_from_rest()` — called after CCXT REST fetch on startup or when WS cache is stale (>30s)
- `update_position()` — called on every WS `ACCOUNT_UPDATE`

**Critical:** Both methods normalize symbol keys via `normalize_symbol()`. Do not remove this or you'll re-introduce the split-brain phantom position bug.

### `engine/reconciler.py`
Multi-phase offline fill detector. Runs on startup (48h window) and every 10 cycles (2h window).
- **Phase: Offline fill detection** — Scans `bot_orders` for open orders that are now filled on the exchange.
- **Phase: Position anchor** — Re-reads exchange position after any fill and overwrites `avg_entry_price`/`total_invested`.
- **DO NOT** add any position math that uses `total_physical_notional - sum(sister_bots)`. This causes ghost injection. Use strict CID matching only.

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
| `ADOPTION_BLOCKED` logs everywhere | Sibling bots already claimed the full position value | Expected — prevents double-counting on shared pairs |

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
1. Run `check_and_fix_integrity()` (DB startup sanitizer)
2. Run `reconstruct_offline_fills(48h)` (offline TP/Grid crediting)
3. Scan open exchange orders and tag stray CQB orders for recovery
4. Run `run_cycle()` once to populate snapshots
5. Begin normal polling loop

---

## 7. Testing Checklist Before Any Engine PR

- [ ] No `SIZE DISCREPANCY` in engine.log after 5+ cycles
- [ ] `active_positions` table count matches Binance open positions count
- [ ] `bot_orders` table has no `open` status entries older than 10 minutes without exchange confirmation
- [ ] No `GHOST_RESET` or `PHANTOM_RESET` in `reconciliation_logs` for legitimate positions
- [ ] Bot TP correctly resets to Step 0 / `total_invested = 0` after exchange position closes
- [ ] Multi-bot pairs: each bot's `total_invested` sums to the approximate notional of the shared position
