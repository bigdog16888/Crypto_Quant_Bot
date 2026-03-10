# Database Schema — crypto_bot.db

SQLite database. All timestamps are **Unix epoch integers** (seconds). All monetary values are **USDC** unless noted.

---

## `bots` — Bot Configuration (9 rows)

The master config table. One row per bot. Never deleted — bots are deactivated via `is_active`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Bot ID (e.g. 10016, 10020). Hardcoded/assigned at creation. |
| `name` | TEXT | Human label (e.g. "long btc price", "short link") |
| `pair` | TEXT | Exchange pair (e.g. `BTC/USDC:USDC`, `LINK/USDC:USDC`) |
| `direction` | TEXT | `LONG` or `SHORT` |
| `rsi_limit` | REAL | RSI threshold for entry signal (legacy, may be unused) |
| `martingale_multiplier` | REAL | Grid multiplier (legacy, superseded by `config`) |
| `base_size` | REAL | Base USDC per step (legacy, superseded by `config`) |
| `strategy_type` | TEXT | Strategy class name (default `Martingale`) |
| `config` | TEXT | **JSON blob** — all live strategy params (see below) |
| `is_active` | BOOLEAN | `1` = bot runs, `0` = disabled |
| `status` | TEXT | Current state: `Scanning`, `IN TRADE`, `Stopped`, `WAITING FOR FILL` |
| `manual_close_pct` | REAL | % of position to close on manual exit command (default 100) |
| `last_error` | TEXT | Last exception message for diagnostics |
| `last_error_time` | INTEGER | Epoch of last error |

### `config` JSON Keys (important ones)
| Key | Description |
|-----|-------------|
| `max_steps` | Max grid steps before TP-only mode |
| `base_order_size` | USD per step 1 |
| `UseEarlyExit` | Bool — enables EE decay |
| `EEStartHours` | Hours before EE starts decaying TP |
| `EEEndHours` | Hours at which TP reaches break-even |
| `EEAllowLoss` | Bool — allow EE to go below break-even |
| `grid_spacing_pct` | % gap between grid levels |
| `tp_pct` | Take-profit % above avg entry |
| `locked_atr` | Persisted ATR used for grid spacing |

---

## `trades` — Live Trade State (9 rows, one per bot)

One row per bot — **always exists**, even when bot is scanning. Resets on TP/exit but row is never deleted.

| Column | Type | Description |
|--------|------|-------------|
| `bot_id` | INTEGER PK | FK → `bots.id` |
| `current_step` | INTEGER | Grid step count (0 = no position, 1 = entry filled, 2 = first grid filled…) |
| `total_invested` | REAL | Virtual total invested USD in current cycle |
| `avg_entry_price` | REAL | Volume-weighted average entry price |
| `target_tp_price` | REAL | Active TP price (may be EE-decayed from original) |
| `last_exit_price` | REAL | Price of last TP/exit for reference |
| `last_exit_time` | INTEGER | Epoch of last exit |
| `basket_start_time` | INTEGER | Epoch when current cycle began. **Must be > 0 for EE to work.** Set on entry, reset to `time.time()` on resets — NEVER to 0. |
| `entry_confirmed` | BOOLEAN | `1` = entry order confirmed filled by WS |
| `entry_order_id` | TEXT | Exchange order ID of the active entry order |
| `tp_order_id` | TEXT | Exchange order ID of the active TP order |
| `bot_position_id` | TEXT | Internal position linking ID (for multi-bot adoption) |
| `close_type` | TEXT | How last cycle closed: `TP`, `EE`, `SL`, `MANUAL`, etc. |
| `cycle_id` | INTEGER | Increments on each TP/reset. Used to fence `bot_orders` by cycle so old fills aren't re-adopted. |

> **⚠️ Key Invariant (v1.4.0):** `avg_entry_price` and `total_invested` are always authoritative from the exchange. The `_sync_positions_to_exchange()` reconciler preflight overwrites DB values if drift exceeds 0.5%. Never compute position size from `total_invested` without also checking `active_positions`.

---

## `bot_orders` — Order Ledger (1,425 rows)

Every order placed by every bot. Old cycle orders are marked `reset_cleared` or `auto_closed` — never deleted. The reconciler filters by `cycle_id` to avoid re-adopting stale history.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `bot_id` | INTEGER | FK → `bots.id` |
| `step` | INTEGER | Grid step this order belongs to |
| `order_type` | TEXT | `entry`, `grid`, `tp` |
| `order_id` | TEXT | Exchange order ID |
| `price` | REAL | Limit price of the order |
| `amount` | REAL | Quantity in base asset (e.g. BTC, LINK) |
| `status` | TEXT | `open`, `filled`, `closed`, `cancelled`, `auto_closed`, `reset_cleared`, `missing` |
| `created_at` | INTEGER | Epoch when order was placed/recorded |
| `client_order_id` | TEXT | CQB deterministic ID: `CQB_{bot_id}_{TYPE}_{step}_{timestamp}` |
| `updated_at` | INTEGER | Epoch of last status change |
| `notes` | TEXT | Diagnostic notes (e.g. rejection reason) |
| `cycle_id` | INTEGER | Matches `trades.cycle_id` at time of placement — used to fence order adoption |
| `filled_amount` | REAL | Partial fill amount (base asset) |

### `client_order_id` Format
```
CQB_{bot_id}_{TYPE}_{step}_{timestamp_ms}
Example: CQB_10020_GRID_4_1772778234982
```
This deterministic ID is used for idempotent order placement and ghost detection.

### Status Lifecycle
```
open → filled        (WS fill event)
open → cancelled     (manual cancel or SYNC-DRIFT replacement)
open → missing       (reconciler found no exchange order)
filled → reset_cleared  (cycle reset after TP)
open → auto_closed   (cycle reset — order was still open at TP)
```

---

## `active_positions` — WS Position Snapshot (4 rows)

Written by the WebSocket handler on every position update. Reflects **exchange truth** in near-real-time. Used by reconciler for `NOTIONAL-GAP` checks and by the UI for mismatch display.

| Column | Type | Description |
|--------|------|-------------|
| `bot_id` | INTEGER PK | FK → `bots.id` (0 = unassigned/rogue) |
| `pair` | TEXT PK | Short pair name (e.g. `LINK/USDC`) |
| `side` | TEXT PK | `LONG` or `SHORT` |
| `size` | REAL | Position size in **base asset** (e.g. 869.87 LINK) |
| `entry_price` | REAL | Exchange average entry price |
| `last_checked` | INTEGER | Epoch of last reconciler check |
| `last_updated` | INTEGER | Epoch of last WS update |

> **Note:** `size × entry_price = notional (USD)`. The `bot_id=0` case means the reconciler hasn't matched it to a bot yet (potential orphan).

---

## `trade_history` — Immutable Audit Log (797 rows)

Append-only log of every significant trade event. Never updated, never deleted.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `bot_id` | INTEGER | FK → `bots.id` |
| `timestamp` | INTEGER | Epoch |
| `action` | TEXT | Event type: `ENTRY`, `GRID_FILL`, `TP`, `EE`, `GHOST_RESET`, `PHANTOM_RESET`, `SYNC_DOWN`, `POSITION_SYNC`, `OFFLINE_GRID`, `OFFLINE_TP`, `OFFLINE_ENTRY` |
| `step` | INTEGER | Grid step at time of event |
| `pnl` | REAL | Realised PnL for this event (0 for entries) |
| `symbol` | TEXT | Trading pair |
| `price` | REAL | Price at event |
| `amount` | REAL | Quantity |
| `cost_usdc` | REAL | USD value |
| `order_id` | TEXT | Exchange or internal order ID |
| `step` | INTEGER | Grid step at time of event |
| `pnl` | REAL | Realised PnL for this event (0 for entries) |
| `notes` | TEXT | Human-readable context |

---

## `reconciliation_logs` — Reconciler Action Log (5,891 rows)

Every action the reconciler takes. Useful for diagnosing why a bot was reset or repaired.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `timestamp` | INTEGER | Epoch |
| `bot_id` | INTEGER | FK → `bots.id` |
| `pair` | TEXT | Trading pair |
| `action` | TEXT | e.g. `GHOST_RESET`, `PHANTOM_RESET`, `RECON_REPAIR`, `SYNC_TO_REALITY`, `POSITION_SYNC`, `RECON_HEAL` |
| `details` | TEXT | Full description of what was done and why. For `POSITION_SYNC`: shows drift % before anchoring. |
| `proof_order_id` | TEXT | Exchange order ID used as evidence for the action |

---

## `notifications` — UI Alert Queue (1,606 rows)

Notifications shown in the UI. Marked `is_read` when dismissed.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `timestamp` | INTEGER | Epoch |
| `type` | TEXT | `INFO`, `WARNING`, `ERROR`, `SUCCESS` |
| `message` | TEXT | Display text |
| `bot_id` | INTEGER | Related bot (nullable) |
| `is_read` | BOOLEAN | `0` = unread, `1` = dismissed |

---

## `system_equity` — Global Metrics (2 rows)

Simple key-value store for system-wide metrics.

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT PK | Metric name (`peak_equity`, `starting_equity`, etc.) |
| `value` | REAL | Metric value |

---

## Key Relationships

```
bots (id)
  ├── trades (bot_id) — 1:1, always exists
  ├── bot_orders (bot_id) — 1:many, lifecycle via status + cycle_id
  ├── active_positions (bot_id) — 1:1, WS truth
  ├── trade_history (bot_id) — 1:many, immutable log
  └── reconciliation_logs (bot_id) — 1:many, reconciler audit
```

## Common Diagnostic Queries

```sql
-- All active bots with position state
SELECT b.name, b.status, t.current_step, t.total_invested, t.avg_entry_price, t.basket_start_time
FROM bots b JOIN trades t ON b.id = t.bot_id WHERE b.is_active = 1;

-- Open orders by bot
SELECT b.name, bo.order_type, bo.price, bo.amount, bo.client_order_id, bo.status
FROM bot_orders bo JOIN bots b ON bo.bot_id = b.id WHERE bo.status = 'open';

-- Physical vs virtual mismatch check
SELECT b.name, t.total_invested AS virtual, ap.size * ap.entry_price AS physical
FROM bots b
JOIN trades t ON b.id = t.bot_id
JOIN active_positions ap ON b.id = ap.bot_id
WHERE t.total_invested > 0;

-- Recent reconciler actions
SELECT datetime(timestamp,'unixepoch'), b.name, action, details
FROM reconciliation_logs r JOIN bots b ON r.bot_id = b.id
ORDER BY timestamp DESC LIMIT 20;

-- Bots with basket_start_time = 0 while in trade (EE broken)
SELECT b.name, t.total_invested, t.basket_start_time
FROM bots b JOIN trades t ON b.id = t.bot_id
WHERE t.total_invested > 0 AND t.basket_start_time = 0;
```
