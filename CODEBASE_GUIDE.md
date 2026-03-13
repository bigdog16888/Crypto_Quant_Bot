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

### 2.1. Symbol Normalization
- **Always** use `normalize_symbol(sym)` from `exchange_interface.py` before comparing or storing symbols.
- Binance REST/CCXT returns `"BTC/USDC"` (slash). Binance WebSocket pushes `"BTCUSDC"` (no slash).
- **The ws_cache (V1.4.3 fix) normalizes all keys.** If you bypass this, you'll get duplicate position entries and false Size Discrepancy alerts. This was the V1.4.3 bug.

### 2.2. Order Isolation (Multi-Bot Core Rule)
- **NEVER call `cancel_all_orders(pair)`** in bot logic. Always use `cancel_orders_by_bot_id(bot_id, pair)`.
- Every order is tagged with `CQB_{bot_id}_{type}_{step}_{uuid}` as `clientOrderId`.
- The reconciler and integrity checks use this CID fingerprint as the *only* proof of bot ownership.

### 2.3. Database Is Source of Truth — But Exchange Anchors It
- The `trades` table tracks each bot's virtual position independently.
- The exchange snapshot anchors the DB: if `trades.avg_entry_price` drifts >0.5% from Binance's live data, the reconciler overwrites the DB with exchange truth.
- **Do NOT trust aggregate exchange notional sums** to make decisions about individual bots. Always validate against `bot_orders.client_order_id`.

### 2.4. TP Reset Synchronization
- When a TP fills offline, `reconciler.py` calls `reset_bot_after_tp()` which wipes all `bot_orders` for that cycle to `reset_cleared`.
- **The TP fill record itself must also be saved as `reset_cleared`**, not `'filled'`. Otherwise the reconciler math loop sees a dangling negative quantity and injects phantom "adoption_add" ghosts. This was the V1.4.2 bug.

### 2.5. reduceOnly Logic
- `reduceOnly=True` on TP orders is ONLY safe when exactly **1 bot** is active on a pair.
- With >1 bots on a pair, Binance will reject the TP with `-2022 ReduceOnly Order is rejected` if the net aggregate position points the wrong way.
- `bot_executor.py` dynamically counts sibling bots per pair and chooses accordingly.

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
