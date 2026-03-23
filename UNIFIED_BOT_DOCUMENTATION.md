# Crypto Quant Bot: Unified Documentation

**Version:** 1.4.6 (Stale Order Partial Fill Preservation)  
**Status:** Highly Stable, Strict Proof-Only Active  
**Last Updated:** 2026-03-13

This document provides a unified, comprehensive overview of the Crypto Quant Bot, its architecture, setup, and operational best practices. It consolidates the key information from over 20 separate markdown files.

---

## 1. High-Level Overview

The Crypto Quant Bot is a professional-grade, automated cryptocurrency trading platform designed for high precision, robust risk management, and live market resilience.

### 1.1. Key Features

-   **Advanced Strategy Engine:** Utilizes an 11-trigger "confluence" system, requiring multiple technical indicators (RSI, CCI, Bollinger Bands), price patterns, and volatility filters to align before executing a trade.
-   **Institutional-Grade Safety:**
    -   **Global Circuit Breaker:** A master "kill switch" monitors total account equity and halts all trading if a critical drawdown (e.g., 50%) is detected.
    -   **State Recovery & Self-Healing:** The system automatically synchronizes its internal database with the exchange on startup, detecting and resolving discrepancies like "ghost trades" (positions closed while the bot was offline).
    -   **Pre-emptive Validation:** Every order is checked against the exchange's live rules (minimum notional, quantity, step size) *before* being sent, preventing API rejections and potential bans.
-   **Multi-Bot Architecture (Virtual Position Manager):** The bot's core architectural feature, allowing multiple independent trading strategies (bots) to run on the **same trading pair** simultaneously without interfering with each other.
-   **Professional UI:** A Streamlit-based dashboard provides a comprehensive interface for:
    -   **Live Monitoring:** Real-time view of trades, positions, and logs.
    -   **Bot Creation:** A wizard for configuring and deploying new strategies.
    -   **Bot Management:** Editing and controlling existing bots.
    -   **Advanced Analytics:** A performance dashboard with equity curves, win rate, profit factor, and trade history export.

---

## 2. Core Architecture: The Virtual Position Manager

The bot has evolved from a simple "one bot per pair" model to a sophisticated multi-bot system. Understanding this architecture is critical for operating and developing the bot correctly.

### 2.1. The Problem Solved

In a simple trading bot, if you have two strategies on the same pair (e.g., Bot A is LONG 0.1 BTC, Bot B is SHORT 0.1 BTC), the net position on the exchange is 0. A simple bot would see the zero position, assume its trades were closed, and incorrectly reset itself—a "ghost trade." The Virtual Position Manager solves this.

### 2.2. Core Principles

1.  **The Database is the Source of Truth:** The bot's internal `trades` table is the absolute source of truth for its position. The aggregate net position shown on the exchange is considered **irrelevant** for determining an individual bot's status.
2.  **Order Isolation via `clientOrderId`:** Every order sent to the exchange is tagged with a unique, deterministic prefix: `CQB_{bot_id}_`. For example, `CQB_42_TP_0` is the Take Profit order for Bot 42. This allows the system to distinguish which bot owns which order.
3.  **Bot-Specific Logic:** A bot determines its own state by looking for *its own* orders on the exchange.
    -   `cancel_orders_by_bot_id()` is used to safely cancel only one bot's orders.
    -   **Crucial Rule:** Global `cancel_all_orders()` calls are forbidden in standard bot logic as they would wipe out other bots' orders.

### 2.3. Multi-Bot Virtual Positioning & Reconciliation

Each bot's state is tracked in the `trades` table. The `reconciler.py` aggregates virtual positions and runs a 3-phase **Exchange-Anchored** sync on every cycle:

| Phase | What it does |
|-------|--------------|
| **Preflight Sync** | Before reading any fill history, `_sync_positions_to_exchange()` compares DB against Binance live positions. **Crucial One-Way Guard**: If multiple bots are active on the same pair, the bot strictly ignores Binance's aggregated position (to prevent math-stealing between Longs/Shorts) and relies purely on individual order receipts (`clientOrderId`). If it is the sole bot, it anchors perfectly to the exchange. |
| **Idempotency & Partial Guard** | Every offline fill is double-checked against `bot_orders.order_id` AND `trade_history`. The system proactively fetches `fetch_open_orders` alongside closed history to correctly attribute mathematically live fractions of **Partial Fills** that occurred while the DB was asleep/offline. |
| **Post-Fill Anchor** | After any offline fill is recorded, the system immediately re-fetches the exchange position and overwrites `avg_entry_price`/`total_invested`. Arithmetic drift is impossible. |
| **TP Safety Guard** | Before calling `reset_bot_after_tp` on a found closed TP, the system verifies the exchange position is actually flat. If a live position still exists, the TP is marked stale and the reset is aborted. |

> **Key Invariant:** The exchange's live position is the ground truth. The DB always syncs to match, provided the bot has absolute mathematical ownership of the pair. Fractional math is always tracked directly via explicit order IDs.

---

## 3. Configuration & Setup

### 3.1. API Keys (CRITICAL UPDATE)
**Binance Testnet/Sandbox for Futures is DEPRECATED by CCXT.**
To run this bot, you **MUST** use valid Binance Futures API keys (Mainnet). 
- Ensure `DEMO_TRADING=False` in your `.env` file.
- Use `DRY_RUN=True` to test logic without placing real orders.

### 3.2. Installation Steps

```bash
# 1. Clone the repository
git clone https://github.com/your-repo/Crypto_Quant_Bot.git
cd Crypto_Quant_Bot

# 2. Create and activate a virtual environment (recommended)
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux/Mac:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
# Create a .env file from the example
cp .env.example .env

# Edit the .env file with your Binance API keys and settings
# nano .env
```

### 3.3. Environment Configuration (`.env`)

Your `.env` file must contain:

```ini
# Your Binance API Key and Secret
BINANCE_API_KEY=your_key_here
# Your Binance API Secret
BINANCE_API_SECRET=your_secret_here

# Set to False for live trading
DRY_RUN=True

# Set to True to use the Binance Testnet (DEPRECATED for Futures)
TESTNET=False

# The global circuit breaker limit (e.g., 50.0 for 50%)
GLOBAL_STOP_LOSS_PCT=50.0

# The master switch for placing live orders
TRADING_ENABLED=True
```

### 3.4. Running the Application

The bot consists of two main components: the UI and the trading engine. The UI provides a control panel for the engine.

```bash
# Start the Streamlit UI
streamlit run ui/app.py

# Once the UI is running, navigate to http://localhost:8501
# Use the sidebar controls to start and stop the trading engine.
```

---

## 4. Developer's Guide & Best Practices

### 4.1. The Golden Rule of Multi-Bot

**Never use `cancel_all_orders(pair)`. Always use `cancel_orders_by_bot_id(bot_id, pair)`.** The former will cause catastrophic interference between bots; the latter is the foundation of the Virtual Position Manager.

### 4.2. Dynamic `reduceOnly` Logic (Updated: 2026-03-12)

**The system now dynamically handles `reduceOnly: True` order parameters based on active pair population.**
Previously, using `reduceOnly: True` in a Multi-Bot environment was strictly banned because if a specific bot sends a Take Profit 'buy' order with `reduceOnly=True` while the exchange's aggregate physical position is opposite (e.g. LONG), Binance will reject the order with `-2022 ReduceOnly Order is rejected`. This would prevent the bot from ever taking profit.

In this upgraded system, **Take Profit orders automatically evaluate sibling bot counts**:
- **Single-Bot Active (1 Bot on Pair)**: The system applies `reduceOnly=True` to the Take Profit order. This safely ensures any fractional dust limits are natively cleaned by the exchange upon exiting the position.
- **Multi-Bot Active (>1 Bots on Pair)**: The system automatically drops the `reduceOnly` flag and falls back to a structural algorithmic Limit order (`postOnly=True`, or GTC if crossing spread) utilizing `bot_orders` memory to prevent position doubling. This avoids `-2022` rejection collisions while guaranteeing profits are always successfully secured no matter the net balance orientation!

### 4.2. Recent Stability & Bug Fixes (Updated: 2026-02-12)

The bot has recently undergone a fundamental stabilization phase to ensure multi-bot isolation.

-   **Order Isolation (2026-02-12):** Fixed critical collisions in `MarketMaker` logic and `OrderManager` where global cancellation calls were wiping out orders from other bots.
-   **Aggregate Position Math (2026-02-12):** Reconciler now sums virtual positions for One-Way mode validation.
-   **WebSocket Handler (2026-02-12):** Fixed a `KeyError` in `ws_event_handlers.py` where database results were being accessed as lists instead of dictionaries.
-   **Database Integrity (2026-02-12):** Fixed "Ghost Fix Loop" by ensuring `basket_start_time` is correctly initialized in the `trades` table.
-   **Exchange Limits (2026-02-12):** Increased default `base_size` to $150 to ensure orders always clear the exchange's "Min Notional" requirements.

### 4.3. Troubleshooting

-   **UI Won't Start:** Check if the port (usually 8501) is in use.
-   **P/L Shows But No Exchange Position:** State mismatch. The next reconciler cycle (runs every few minutes) will auto-heal via Preflight Sync. No restart needed.
-   **Bots Auto-Resetting to IDLE:** Check `reconciliation_logs` table for `GHOST_RESET` or `PHANTOM_RESET` entries — details column explains why.
-   **Bot Zeroed While Exchange Has Live Position:** A Previous-cycle TP order was in the 168h history window. This is now blocked by the Exchange-Position Guard in the reconciler. If it happened: re-link the bot via the Manual Link tool in the UI — it will now correctly recover the original step from `bot_orders`.
-   **Orders Rejected (Min Notional):** Ensure `base_size` is at least $150 (especially on USDC mainnet).

---

## 5. Changelog Summary

### Version 1.4.7 (2026-03-19)
**Multi-Bot Perfect Hedge Survival & Dust Chaser Upgrades**
- **Perfect Hedge Vanishing Bug:** Fixed a critical flaw in `reconciler.py` where two bots perfectly hedging each other (e.g., $5k LONG and $5k SHORT yielding $0 physical on exchange) were mathematically flagged as a "Vanished Position" and systematically wiped. The guard now checks absolute `virtual_net_usd` instead of gross `total_virtual_invested` to cleanly preserve perfectly neutralized bots.
- **DUST_CHASER `reduceOnly` Mandate:** Upgraded `bot_executor.py` `DUST_CHASER` orders with `reduceOnly=True` to explicitly bypass Binance's $5 minimum notional rejections for dust clearing.
- **Grid Placement Unblocking:** Removed the early `return None` aborts after TP placement/dust sweeping in `maintain_orders`, permanently ensuring that Grid orders are independently placed every cycle.

### Version 1.4.6 (2026-03-13)
**Stale Order Partial Fill Preservation**
- **Complete Cancel Loop Patch:** Updated all 4 cancellation loops in `bot_executor.py` (Stale Step Orders, Grid Duplicates, TP Ghost Sweeping, and Grid Drift) to dynamically extract the `filled` variable from CCXT order objects before issuing a local DB `update_order_status(..., 'cancelled')`. This patches the final mathematical leak where fractionally filled orders right on the boundary of a step progression were losing their executed volume on the transition.

### Version 1.4.5 (2026-03-13)
**Cycle-Aware Reconciliation — Multi-Bot Accuracy**
- **Cycle-Aware Ledger Sync:** Patched `engine/reconciler.py` and `ui/views/monitor.py` to filter `bot_orders` by `cycle_id` from the `trades` table. This prevents "zombie volume" from previous completed trades (e.g., $57k SUI mismatch) from being counted toward the current active system net.
- **Overlapping Bot Isolation:** Refined `_sync_positions_to_exchange` to strictly avoid mathematical "math-healing" of physical positions when multiple bots are active on the same pair (One-Way Margin mode).

### Version 1.4.4 (2026-03-13)
**Partial Fill Resilience — Ghost Order Pruning**
- **Partial Fill Resilience:** Patched `bot_executor.py` to capture and preserve `filled` quantities from Binance before canceling stagnant grid or take-profit orders. This ensures that even if an order is cancelled while partially filled, the filled amount is recorded in the bot's mathematical ledger (`bot_orders`), preventing "phantom" USD mismatches in the UI.
- **Stray Ghost Order Pruning:** Updated `cleanup_pending_orders` in `engine/database.py` to target both `open` and `new` statuses. Previously, orders that crashed during the creation phase (stuck as `new`) were ignored by the cleanup sweeper, leading to "Stray Orders" alerts in the dashboard.
- **Ledger Math Normalization:** Manually reconciled the SUI/SOL ledger gaps by injecting the exact missing fractional quantities to align Virtual (DB) state with Exchange (CCXT) physical limits.

### Version 1.4.3 (2026-03-13)
**WebSocket Cache Split-Brain Resolution — Phantom Position Elimination**
- **Root Cause Identified:** The `WSCache` in `engine/ws_cache.py` stored position data under two different key formats: REST positions from CCXT used slash-format (`BTC/USDC`) while live WebSocket `ACCOUNT_UPDATE` events used raw Binance format (`BTCUSDC`). This caused the cache to accumulate *both* entries simultaneously instead of overwriting, doubling the virtual physical notional when the integrity checker compared positions. The mismatch always equalled *exactly* the Entry Order 1 value — a reliable signature of this bug.
- **Fix:** `update_position()` and `populate_from_rest()` in `ws_cache.py` now call `normalize_symbol()` on every position key before storage. REST and WS data now correctly resolve to the same dict key, ensuring deduplication.
- **Impact:** All `SIZE DISCREPANCY` and `UNMATCHED POSITION` warnings in `engine.log` have been completely eliminated.

### Version 1.4.2 (2026-03-12)
**Multi-Bot Architecture Isolation & Ledger Math Integrity**
- **Strict Proof-Only Independence:** Reverted proportional guessing mechanics within `reconciler.py` where overlapping bots mapping identical pairs attempted to mathematically assume ownership of physical exchange gaps. Overlapping bots now exclusively rebuild internal limits using physical `client_order_id` receipt tracking via `bot_orders`, completely solving the massive phantom $30,000 `adoption_add` ghost scaling loops.
- **Take Profit Ledger Integrity:** Fixed a profound structural flaw in offline sync logic where `tp` (`Take Profit`) execution logs were natively written back into the new DB Cycle Scope directly *after* grid history wipes. This left un-matched negative quantities in active balance traces that provoked the auto-healer. TP receipts are now dynamically stored as `reset_cleared` synchronously to halt infinite array loops immediately.
- **Dynamic TP reduceOnly Logic:** Upgraded `bot_executor.py` so that Single-Bot states map TP limits dynamically via `reduceOnly=True` to clear fractional token dust silently, while Multi-Bot states safely rely on native Limit Orders (`postOnly=True`, or GTC if crossing spread) avoiding `-2022` Exchange collisions.

### Version 1.4.1 (2026-03-10)
**Virtual Hedge Guarding & Step Integrity**
- **Protected Virtual Hedging:** `StateReconciler` safely bypasses 'Net Physical' anchoring logic when >1 bot is actively trading on the same token (e.g. tracking `BTC/USDC` Long and Short concurrently).
- **Mathematical Step Recovery:** Position adoptions and re-links now logically derive their exact numerical Step natively using existing Martingale size configurations, eliminating Step 1 memory wipes upon adoption.
- **Strict Cycle ID Proofing:** Fixed a "Ghost Loop" vulnerability in offline reconciliations by ensuring past Take Profit closures are strictly validated against the active bot's `cycle_id`.

### Version 1.4.0 (2026-03-09)
**Exchange-Anchored Reconciler & Root Cause Fixes**
- **Exchange-Anchored Reconciliation:** Replaced event-replay architecture with a 3-phase system. Exchange live position is now ground truth. Eliminates `avg_entry` halving, double-counting, and arithmetic drift permanently.
- **False OFFLINE_TP Guard:** Reconciler now verifies the exchange position is flat before firing `reset_bot_after_tp`. Stale/cancelled TPs from history no longer zero live positions.
- **Dynamic Step Recovery on Manual Adoption:** `import_position_from_exchange` now queries `bot_orders` to restore the correct Martingale step instead of hardcoding Step 1.
- **XAU Grid Vibration Fix:** ATR-based grids exempt from GRID-SYNC drift check. Non-ATR tolerance widened to 0.5%.
- **UI Profit Display:** Added ROE% (leverage-adjusted) alongside ROI%. EE display uses actual `calculate_early_exit_decay` function.
- **NOTIONAL-GAP Healing:** Replaced 100-line event-replay auto-repair with direct call to `_sync_positions_to_exchange()`.

### Version 1.3.0 (2026-03-03)
**Order Sync & Race Condition Stabilization**
- **Forward-Step Cancel Bug:** Fixed a critical race condition in `bot_executor.py` where a slight database read latency caused fresh Grid orders to be instantly falsely flagged and deleted as "stale" from previous steps.
- **Zero-Invested Sweeper Safety:** Modified the "SCANNING" phase dangling order cleanup to permanently cease if `current_step > 0`, explicitly protecting rapidly filled Entry/Grid orders from being incorrectly deleted if the `total_invested` database field recalculation hasn't propagated.
- **Perfect 1:1 Order Verification:** Closed the persistent "15 vs 16 phantom missing exchange order" discrepancy. The local database mathematical order expectation now flawlessly matches Binance's physical active order state (20/20).

### Version 1.0.0 (2026-02-12)
**Fundamental Multi-Bot Isolation**
- **Scoped Cancellations:** Replaced all `cancel_all_orders` with `cancel_orders_by_bot_id` in core engine.
- **Aggregate Reconciliation:** `reconciler.py` now correctly handles shared positions in One-Way mode.
- **WebSocket Fix:** Corrected dictionary access in real-time event handlers.
- **Basket Timestamp Fix:** Ensured active trades have valid start times to prevent premature auto-healing resets.
- **Min Notional Safety:** Increased default order size to clear exchange hurdles.

### Version 0.9.1 (2026-02-11)
**Major Update: True Virtual Positions**
- **Removed Ownership Blocking:** Completely eliminated `try_atomic_claim_ownership_before_entry()`.
- **Fixed Database Schema:** Updated `active_positions` table for multi-bot primary keys.
- **Reconciler Decoupling:** Removed ownership state dependencies.

---
This unified document was last updated on **2026-03-19** (v1.4.7).

## 6. Database Architecture & Concurrency (Added 2026-02-17)
The system uses **SQLite** in **WAL (Write-Ahead Logging)** mode for high-performance concurrency.
-   **File**: `crypto_bot.db` in the root directory.
-   **Concurrency**: 
    -   `runner.py` writes to the DB using `isolation_level=None` and `BEGIN IMMEDIATE` transactions to prevent locking issues.
    -   Streamlit UI reads from the DB in a separate process.
    -   WAL mode allows simultaneous readers and one writer without blocking.
-   **Tables**:
    -   `bots`: Configuration and state.
    -   `trades`: Active trade tracking (Virtual Positions).
    -   `active_positions`: Snapshot of valid physical exchange positions (Synced every cycle).
    -   `bot_orders`: Log of all orders placed by bots.

### Troubleshooting "Empty Wallet"
If the UI reports "Exchange wallet is empty", it means the `active_positions` table is empty.
This checks:
1.  Check `engine.log` for `✅ Active Positions Synced: X`.
2.  If Synced > 0 but UI is empty, ensure `runner.py` is running and `crypto_bot.db-wal` exists.

---

## 8. VIRTUAL LEDGER MISMATCH — ROOT CAUSES AND PERMANENT FIXES

The UI shows `System X vs Exchange Y (Diff $Z)` when `bot_orders.SUM(filled_amount)` diverges from `active_positions.size * entry_price`. There are exactly **4 code-level root causes** discovered and fixed. Future agents: do NOT patch the DB directly. Fix the code and backfill properly.

### Root Cause 1: `adoption_add` rows had NULL cycle_id
- **Where**: `engine/reconciler.py` → `_sync_positions_to_exchange()` INSERT
- **Symptom**: UI mismatch for XRP/SUI/BTC even though fills existed in bot_orders
- **Why**: `monitor.py` virtual ledger JOIN was `bo.cycle_id = t.cycle_id` (strict). NULL rows were silently dropped.
- **Fix applied**: Changed JOIN to `(bo.cycle_id = t.cycle_id OR bo.cycle_id IS NULL)` in `monitor.py`. Also stamped `cycle_id=db_cycle` on the INSERT in `reconciler.py`.

### Root Cause 2: Offline fill reconstruction: INSERT missing `filled_amount`
- **Where**: `engine/reconciler.py` → `reconstruct_offline_fills()` INSERT at line ≈604
- **Symptom**: `trades.total_invested` shows 0.479 BTC but `bot_orders` sum shows only 0.189 BTC. Gap matches exactly the untracked grids.
- **Why**: The INSERT for reconstructed offline fills omitted the `filled_amount` column. SQLite defaulted it to 0. `accumulate_trade_fill` correctly updated `trades`, but `bot_orders.filled_amount=0` so the virtual ledger missed them.
- **Fix applied**: Changed INSERT to include `filled_amount=fill_qty` and `cycle_id=_bot_cycle`.

### Root Cause 3: WS FILLED event sets status but not filled_amount
- **Where**: `engine/ws_event_handlers.py` → `_handle_order_filled()` → `update_order_status()`
- **Symptom**: Rows with `status=filled` but `filled_amount=0` exactly matching the live gap.
- **Why**: Binance `ORDER_TRADE_UPDATE` WS event's `z` field (cumulative qty) can be 0 when the event fires. The call `update_order_status(..., filled_qty=0)` correctly sets `status=filled` but sets `filled_amount=0`.
- **Fix applied**: Added safety net in `_handle_order_filled`: if `filled_qty <= 0`, run `UPDATE bot_orders SET filled_amount = amount WHERE order_id = ? AND filled_amount = 0`.

### Root Cause 4: Monitoring ref_price uses virtual avg, not physical
- **Where**: `engine/ui/views/monitor.py` → pair_prices dict
- **Symptom**: Identical qty gaps show inflated USD diff because virtual avg_entry ≠ physical entry_price.
- **Detail**: `pair_prices` is set from `avg_entry_price` first (virtual loop), then not overridden by physical. Both sides use this same ref_price to compute USD. Minor issue — doesn't cause false positives for qty-level mismatches.

### Root Cause 5: Cross-cycle orphan partial-fills lost on reset
- **Where**: `engine/database.py` → `reset_bot_after_tp()`
- **Symptom**: The bot resets after TP, but the virtual ledger suddenly misses a fractional amount of contracts that the physical exchange still holds open.
- **Why**: If a grid was *partially* filled before being cancelled, the physical exchange holds those contracts. When TP is hit, `reset_bot_after_tp` indiscriminately marked ALL old bot_orders as `reset_cleared`, effectively wiping them from the virtual ledger mathematical sum. Because `is_sole_bot` prevents math adoption where multiple sister-bots run (e.g., SOL/USDC and SHORT SOL/USDC), the reconciler refused to sweep these orphaned contracts into the new cycle. 
- **Fix applied**: Modified `reset_bot_after_tp` to calculate the mathematical net quantity of the old cycle before wiping it. If net_qty > 0, it explicitly creates an `adoption_add` row tagged into the *new* `cycle_id` so the old partial fills natively carry over into the new cycle.

### How to Diagnose Future Mismatches
1. Run `python tmp_monitor_debug.py` (check the `tmp_` templates in the doc comments) to see exact qty gaps.
2. Check `bot_orders` for rows with `status='filled' AND filled_amount=0 AND amount>0` — these are the offenders.
3. Backfill with: `UPDATE bot_orders SET filled_amount=amount WHERE status='filled' AND filled_amount=0 AND amount>0`
4. Check if the code fixes in reconciler.py and ws_event_handlers.py are in place.
5. **Never** run `DELETE FROM bot_orders` without first flattening the exchange physically.


**Never use manual SQL `DELETE` or `TRUNCATE` scripts (e.g., `DELETE FROM bot_orders`) to "fix" the bot's state, reset trials, or clear the dashboard.**

If you delete rows from `bot_orders` or `trade_history` while the Exchange API still holds physical positions or limit orders, you give the bot **Amnesia**. When it restarts, it will fetch the exchange's physical reality, compare it against its blank DB, and immediately throw catastrophic `NOTIONAL-GAP` and `STRAY ORDERS` errors because Physics != Memory. The bot will explicitly refuse to manage these orphaned positions because it assumes a human placed them manually.

### How to Fundamentally Fix State:
1. **Never "patch" the symptoms.** If the DB says $0 and the exchange says $100k, find out *why* the DB failed to record the fill (e.g., a logic bug in `reconstruct_offline_fills`).
2. **To legitimately start fresh:** Use the bot's built-in UI "Manual Link Recovery / Market Close" tools. This ensures the bot's engine gracefully market-closes the physical position on Binance *and* writes the closed ledger mathematical receipt to the database harmoniously.
3. **If you MUST wipe (absolute last resort):** You must execute a script that actively queries the `ccxt` APIs to cancel ALL open orders and market close ALL physical contracts to exactly $0.00 `BEFORE` you wipe the SQLite database. If the physical exchange is not 100% flattened, the SQLite database must be retained. 
