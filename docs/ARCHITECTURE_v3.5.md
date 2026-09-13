# Architecture v3.5 — Crypto Quant Bot

> **Current architecture reference.** Read this for the big picture.
> **Version:** 5.4.0 | **Last updated:** 2026-09-13

---

## 1. System Overview

A multi-bot, per-directional grid trading engine for Binance Futures USDC, designed for **One-Way mode** with **proof-only reconciliation**.

### Core design principles

| Principle | Meaning |
|-----------|---------|
| **One-Way mode** | Single net position per symbol on exchange. Multiple bots share it. |
| **Proof-only** | Every ledger entry has exchange evidence. No invented fills. |
| **Idempotent** | `seal_trade_state()` can be called N times safely. |
| **Independent accounting** | Each bot tracks its own position. Pair net is emergent, not enforced. |
| **Write serialization** | All `trades`/`bot_orders` writes go through `WriteQueue` singleton. |
| **Immutable fill log** | `exchange_fills` append-only log is the single source of truth for position computation (Phase 4+). |

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        EXCHANGE (Binance)                        │
│  One-Way Mode: single net position per symbol                    │
│  REST API + WebSocket streams                                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ fetch_positions(), create_order()
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     ENGINE (run_engine.py)                        │
│  SocketLock → init_db → preflight → BotRunner → main loop        │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              BotRunner (runner/__init__.py)              │    │
│  │  Composes 4 mixins:                                      │    │
│  │  - StartupMixin: __init__, startup_sync, exchanges       │    │
│  │  - ShutdownMixin: graceful stop, SocketLock, PID         │    │
│  │  - WebSocketLifecycleMixin: WS connection management     │    │
│  │  - CycleLoopMixin: run_cycle, pending_flatten/close      │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │
│                              │ per-bot cycle
│                              ▼
│  ┌─────────────────────────────────────────────────────────┐    │
│  │           BotExecutor (bot_executor.py)                  │    │
│  │  maintain_orders() → entry, grid, TP, hedge logic        │    │
│  │  enforce_hedge_child_state() → hedge lifecycle           │    │
│  │  _signal_hedge_child_entry() → spawn child bot           │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │
│                              │ fills
│                              ▼
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              Ledger (ledger.py)                          │    │
│  │  credit_fill() → bot_orders (INV-20 fill_claims guard)   │    │
│  │  seal_trade_state() → trades (idempotent recompute)      │    │
│  │  handle_tp_completion() → atomic TP cascade              │    │
│  │  handle_flatten() → atomic force close cascade           │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │
│                              │ writes
│                              ▼
│  ┌─────────────────────────────────────────────────────────┐    │
│  │           WriteQueue (write_queue.py)                    │    │
│  │  Single worker thread → serialized DB writes (INV-31)    │    │
│  │  Bypass under pytest, bypass from worker thread          │    │
│  └─────────────────────────────────────────────────────────┘    │
│                              │
│                              │
│                              ▼
│  ┌─────────────────────────────────────────────────────────┐    │
│  │           SQLite (crypto_bot.db, WAL mode)               │    │
│  │  bots, trades, bot_orders, fill_claims, active_positions │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Three-Layer State Model

| Layer | Table | Source of Truth | Authority |
|-------|-------|-----------------|-----------|
| 1. Exchange Physical Position | Binance REST/WS | Actual contracts held | **Reality** |
| 2. Bot Orders Ledger | `bot_orders` | `credit_fill()` only | **History** |
| 3. Trades Cache | `trades` | `seal_trade_state()` recompute | **Eventually consistent** |

### Synchronization flow

```
WS fill event → credit_fill(bot_id, order_id, qty, price)
             → fill_claims INSERT OR IGNORE (INV-20)
             → bot_orders.filled_amount updated
             → trades.open_qty accumulator updated
             → seal_trade_state() enqueued (idempotent)
             → trades.total_invested, avg_entry_price, current_step recomputed

runner.run_cycle():
  → drain_tp_cascade() → handle_tp_completion()
  → GTR every 10 cycles → pair-level physical-vs-virtual comparison
  → Parity gates → cycle reset gate, entry gate, heal gates
```

---

## 3.5. Immutable Fill Log & Canonical Reconciliation (Phase 4+)

### New authoritative layer

```
┌─────────────────────────────────────────────────────────────────┐
│  0. Immutable Fill Log: exchange_fills (append-only)           │
│     credit_fill() → exchange_fills INSERT (unique on           │
│     exchange_order_id + bot_id + side + cycle_id)              │
│     → NEVER loses a real exchange fill regardless of timing    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Canonical Reconciliation (engine/position_ledger.py)          │
│  compute_bot_position(), compute_pair_position()               │
│  Pure, read-only, deterministic — same input → same output     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
All call sites now PRIMARY on position_ledger:
  - compute_system_health()        ← engine/health.py
  - audit_pair_ledger_vs_exchange() ← engine/database.py
  - SNAP-ALLOCATE / UI / Telegram  ← position_ledger direct
  - Cycle sweep / Preflight check  ← position_ledger direct
```

### Migration status (v5.4.0)

| Call Site | Migration | Test |
|-----------|-----------|------|
| `get_pair_virtual_net()` | → `compute_pair_position()` | Compare output for 30 days |
| Startup barrier | → `reconcile_bot()` per bot | Verify no new gaps |
| Cycle sweep | → `compute_bot_position()` (read-only) | Verify cycles 6/10 correctly NOT swept |
| SNAP-ALLOCATE | → `compute_pair_position()` | Verify allocation matches |
| Parity gates / hedge watchdog | → `compute_bot_position()` | Verify alerts match |
| UI / Telegram commands | → `compute_pair_position()` | Verify numbers match |

**Each migration**: Deployed behind feature flag → run in shadow mode → compare → cutover.

### Key invariant: Proof-only holds

The `exchange_fills` log is written by:
1. `credit_fill()` — WS fill events
2. `sync_stale_open_orders()` — REST poll reconciliation  
3. `reconstruct_offline_fills()` — gap detection (INV-31 GTR)

Every row has `exchange_order_id` (unique on exchange) — **no invented fills possible**.

### What this fixes

| Old Problem | New Behavior |
|-------------|--------------|
| Phantom fills in `bot_orders` with `status='reset_cleared'` | Ignored — `position_ledger` reads `exchange_fills` only |
| Hedge bots invisible in `trades.open_qty` | Visible — all bots with fills in `exchange_fills` computed |
| `trades.total_invested` drift after wipe/restart | Impossible — recomputed from immutable log |
| Cross-cycle sweep double-counting | Impossible — `cycle_ceiling` bounds exact cycle |

---

## 4. Hedge Architecture

### Parent/child bot pairs

| Bot | Type | Direction | Role |
|-----|------|-----------|------|
| Parent | `standard` | LONG | Main grid strategy |
| Child | `hedge_child` | SHORT | Risk offset |

### Lifecycle

```
Parent (LONG) accumulates grids
  → reaches hedge_trigger_step
  → Child (SHORT) spawns, starts accumulating opposite grids
  → Parent TP fills → parent → pending_hedge_close
  → Child BE TP fills → child → hedge_standby
  → Parent cycle_id incremented → parent → Scanning
```

### Internal accounting

| Bot | Internal open_qty | Exchange effect |
|-----|-------------------|-----------------|
| Parent | +100 LONG | +100 |
| Child | -80 SHORT | -80 |
| **Net** | **+20 LONG** | **+20** (single position) |

The child does NOT create a separate exchange position. In One-Way mode, everything nets.

---

## 5. Write Serialization (INV-31)

### WriteQueue singleton

```python
class WriteQueue:
    _instance = None
    _worker_thread = None
    _bypass = True if pytest else False

    def put_and_wait(fn, *args, **kwargs):
        if _bypass or current_thread == _worker_thread:
            return fn(*args, **kwargs)  # Execute inline
        enqueue(task)
        task.event.wait(timeout=30)
```

### What goes through WriteQueue

| Function | Table | Why |
|----------|-------|-----|
| `credit_fill()` | `bot_orders`, `trades` | Prevent double-credit races |
| `seal_trade_state()` | `trades` | Prevent concurrent recompute corruption |
| `reset_bot_after_tp()` | `trades`, `bots` | Atomic reset |
| `safe_wipe_bot()` | `trades`, `bots`, `bot_orders` | Atomic wipe |

### Deadlock prevention

The worker thread identity check (`current_thread == _worker_thread`) allows nested calls to execute inline. This is safe because:
1. The worker thread has no DB lock when idle
2. Nested calls are always leaf operations (no further nesting)

---

## 6. Parity & Reconciliation

### Pair-level parity

For every active pair: `abs(virtual_net - exchange_net) <= PAIR_PARITY_QTY_TOLERANCE` (default 0.002).

| Component | Frequency | Action |
|-----------|-----------|--------|
| `parity_gates.py` | Every cycle | Block reset if parity would widen |
| `ground_truth_reconciler.py` | Every 10 cycles | Full pair-level comparison |
| `integrity.py` | Every 30 cycles | FLAG-ONLY direction/side consistency |
| `reconciler.py` | Every 10 cycles | Offline fill detection (2h window) |

### Self-healing actions

| Condition | Action |
|-----------|--------|
| GHOST_VIRTUAL (DB > 0, exchange = 0) | safe_wipe_bot |
| ORPHAN_PHYSICAL (DB = 0, exchange > 0) | Log + alert operator |
| STUCK_CASCADE (> 300s in transitional state) | Re-trigger cascade |
| HEDGE_DRIFT (child offset mismatch) | Live guard correction |

---

## 7. Safety Layers

| Layer | Name | Trigger | Action |
|-------|------|---------|--------|
| O-1 | Position-size circuit breaker | Single order > threshold | Block order |
| O-3 | Rolling drawdown breaker | 20% drop over 24h | Emergency liquidation |
| O-9 | Startup plausibility gate | Implausible exchange data | Block destructive reconciliation |
| O-10 | Hedge-engagement watchdog | Child fails to engage within 300s | Freeze parent to REQUIRE_MANUAL_PROOF |

---

## 8. Project Structure

```
Crypto_Quant_Bot/
├── engine/
│   ├── runner/
│   │   ├── __init__.py          # BotRunner class (composes mixins)
│   │   ├── startup.py           # StartupMixin
│   │   ├── shutdown.py          # ShutdownMixin, SocketLock
│   │   ├── cycle_loop.py        # CycleLoopMixin (run_cycle)
│   │   └── websocket_lifecycle.py
│   ├── bot_executor.py          # Per-bot execution (entry, grid, TP, hedge)
│   ├── ledger.py                # Single source of truth for trades table
│   ├── database.py              # All SQLite operations
│   ├── exchange_interface.py    # CCXT + raw Binance FAPI wrapper
│   ├── write_queue.py           # WriteQueue singleton (INV-31)
│   ├── reconciler.py            # Offline fill detection & state recovery
│   ├── ground_truth_reconciler.py # GTR (INV-31)
│   ├── parity_gates.py          # Pair parity, heal gates, proof flatten
│   ├── integrity.py             # FLAG-ONLY mismatch detection
│   ├── recovery.py              # Universal gated bot resolution
│   ├── hedge_watchdog.py        # O-10 hedge-engagement watchdog
│   ├── wipe_proof.py            # Wipe-proof ledger reset
│   ├── health.py                # System health computation
│   ├── preflight.py             # Startup gate
│   ├── metrics.py               # Prometheus metrics server
│   ├── ws_event_handlers.py     # WebSocket fill processing
│   ├── ws_cache.py              # In-memory position/order cache
│   ├── websocket_handler.py     # Binance user data stream
│   ├── oneway_netting.py        # One-way netting (legacy, mostly retired)
│   ├── strategies/
│   │   ├── base.py              # BaseStrategy ABC
│   │   └── martingale_strategy.py # Martingale + Grid strategy
│   └── migrations/              # SafeMigration + 001-010
├── ui/
│   ├── app.py                   # Streamlit entry
│   └── views/
│       ├── monitor.py           # Live monitor
│       ├── bot_creator.py       # Bot creation UI
│       ├── bot_manager.py       # Bot management UI
│       └── analytics.py         # Analytics UI
├── config/
│   ├── settings.py              # Config loader
│   └── constants.py             # Magic numbers
├── scripts/
│   ├── run_startup_heal.py      # One-shot ledger heal
│   ├── diag_live_state.py       # Live parity diagnostic
│   ├── check_model_health.py    # OpenRouter model health
│   └── session_start_check.py   # Session-start date check
├── docs/
│   ├── ARCHITECTURE_v3.5.md     # This file
│   ├── ARCHITECTURE_DECISIONS.md # Design decisions
│   ├── CHANGELOG.md             # Version history
│   ├── OPERATOR_MISMATCH_RUNBOOK.md # Repair procedures
│   └── adr/                     # Architecture decision records
├── tests/                       # 530 tests
├── crypto_bot.db                # Live SQLite (WAL mode)
├── engine.log                   # Rotating log (10MB, 5 backups)
├── run_bot.bat                  # Start engine
└── run_stack.bat                # Start engine + UI
```

---

## 9. Key Invariants (INV-1 through INV-42)

See `CODEBASE_GUIDE.md §3` for the full invariant dependency graph. The most critical:

| Invariant | What it prevents |
|-----------|------------------|
| INV-19 | Duplicate client_order_id → double orders |
| INV-20 | Double fill credit via fill_claims UNIQUE |
| INV-27 | Only credit_fill/seal write open_qty |
| INV-31 | WriteQueue serializes all trades/bot_orders writes |
| INV-34 | credit_fill processes fills regardless of bot status |
| INV-35 | Active TP must exist for in-trade bots |

---

## 10. Operational Procedures

### Startup sequence

```
run_bot.bat → run_engine.py
  → SocketLock.acquire() (port 19888)
  → init_db()
  → preflight_check() (position match, order integrity, step consistency)
  → BotRunner()
  → startup_sync() (blocking parity barrier)
  → main loop (run_cycle every 15s)
```

### Shutdown sequence

```
Stop file written (engine.stop)
  → runner detects is_stop_requested()
  → interruptible_sleep returns True
  → metrics_server.stop()
  → stop_db_worker() (flush WriteQueue)
  → seal_all_active_bots()
  → remove_pid()
  → clear_stop_signal()
  → SocketLock.release()
```

### Emergency stop

```
engine.emergency file written
  → runner detects in run_cycle
  → handle_emergency_liquidation()
  → Flatten all positions
  → Process exits
```

---

## 11. Configuration

### Environment variables (.env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `TESTNET` | True | Use Binance testnet |
| `TRADING_ENABLED` | True | Allow order placement |
| `DEMO_TRADING` | True | Simulate fills |
| `LOG_LEVEL` | INFO | Logging verbosity |
| `METRICS_PORT` | 9090 | Prometheus metrics port |
| `PAIR_PARITY_QTY_TOLERANCE` | 0.002 | Parity tolerance |
| `ALLOW_FORENSIC_ADOPT` | False | Block forensic adoption |

### Config object (config/settings.py)

```python
class Config:
    VERSION = "5.3.7"
    TESTNET = True
    TRADING_ENABLED = True
    METRICS_PORT = 9090
    GLOBAL_STOP_LOSS_PCT = 50.0
    DRAWDOWN_WINDOW_HOURS = 24
    DRAWDOWN_PCT = 20.0
```

---

## 12. Testing

### Test structure

| Category | Count | Coverage |
|----------|-------|----------|
| INV-xx invariant tests | ~40 | INV-19 through INV-42 |
| Version fix tests | ~15 | v3.9.0 through v4.1.4 |
| Hedge lifecycle | ~10 | Parent/child state machine |
| WriteQueue | ~5 | Serialization, bypass, deadlock |
| Parity gates | ~5 | Cycle reset, heal gates |
| UI smoke | ~3 | Streamlit import, DB views |

### Running tests

```bash
# Full suite (ignore env-dependent tests)
python -m pytest tests/ -v --tb=short \
  --ignore=tests/test_playwright_ui.py \
  --ignore=tests/test_verify_fill_on_exchange.py

# Targeted
python -m pytest tests/test_inv31_ground_truth_reconciler.py -v
```

### WriteQueue bypass under pytest

`conftest.py` forces `WriteQueue._bypass = True` so tests run synchronously.

---

## 13. Monitoring & Observability

### Prometheus metrics (port 9090)

| Metric | Type | Purpose |
|--------|------|---------|
| `bot_cycle_time_seconds` | Gauge | Cycle duration |
| `bot_active_count` | Gauge | Active bots |
| `account_equity_usd` | Gauge | Total equity |
| `account_drawdown_percent` | Gauge | Current drawdown |

### Health check (ui/views/monitor.py)

```python
health = compute_system_health()
# Returns: system_status, worst_gap_usd, mismatched_pair_count,
#          orphan_positions, stuck_cascade_bots, manual_proof_bots
```

### Log files

| File | Purpose | Rotation |
|------|---------|----------|
| `engine.log` | Main engine log | 10MB, 5 backups |
| `engine.log.1-5` | Rotated logs | - |
| `session_start_task.log` | Session-start check | Append-only |

---

## 14. Common Operations

### Check engine status

```bash
# Is the engine running?
netstat -an | grep 19888

# Last log entries
tail -20 engine.log

# Process check
cat engine.pid | xargs ps -p
```

### Restart engine

```bash
# Stop
echo "stop" > engine.stop

# Wait for clean shutdown
# Then restart
./run_bot.bat
```

### Manual heal

```bash
python scripts/run_startup_heal.py --execute
```

### Check parity

```bash
python scripts/diag_live_state.py
```

---

## 15. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `WRITE-QUEUE TIMEOUT` | Deadlock in worker thread | Check nested put_and_wait calls |
| `REQUIRE_MANUAL_PROOF` | Parity gate failed | Run diag_live_state.py, verify exchange |
| `PREFLIGHT FAILED` | Position mismatch | Run startup_heal.py |
| `DEPLOY-OUTDATED` | Code modified while running | Restart engine |
| `strategy_config` error | Column renamed | Patch database.py:3685 |

---

*This document is the authoritative architecture reference. For implementation details, see `CODEBASE_GUIDE.md`. For design decisions, see `ARCHITECTURE_DECISIONS.md`.*
