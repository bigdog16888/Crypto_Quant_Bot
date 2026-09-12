# Architectural Root Cause Analysis & Structural Fix Proposal

**Status**: Analysis Complete | **Date**: 2026-09-12 | **Author**: Hermes Agent (Crypto Quant Bot Operator)

---

## Executive Summary

Every bug discovered this week — **startup-wipe**, **SUI cycle-sweep**, **SNAP-ALLOCATE missing guard**, **entry_confirmed loop**, **ETH/LINK fill-credit race**, **balance-fetch key mismatch**, **get_pair_virtual_net sign bug** — shares the **same architectural root cause**:

> **Mutable `bot_orders.status` serves dual conflicting purposes:**
> 1. **Immutable fact**: "This fill actually happened on the exchange at timestamp T" (should never change)
> 2. **Mutable accounting lever**: "This cycle is closed, exclude from ledger" (changed by sweeps, wipes, reconciliations)

When different processes flip the same status field for different reasons, **real fills get destroyed** or **phantom positions get fabricated**. The status field becomes a **contested mutable variable** rather than an **append-only fact**.

---

## Part 1: Complete Audit of State-Mutating Functions

### A. Functions That Mutate `bot_orders.status`

| Function | File:Line | Trigger | What It Assumes | Stale Context Risk |
|----------|-----------|---------|-----------------|-------------------|
| **`credit_fill()`** | `ledger.py:231` | WS fill arrives | Fill is new; order exists in `open`/`new` state; client_order_id unique | **HIGH** — Fill may arrive AFTER cycle reset/wipe already ran (ETH/LINK race). No idempotency guard. |
| **`sync_stale_open_orders()`** | `ledger.py:430+` | Periodic / startup | Exchange `fetch_open_orders` + `fetch_my_trades` is complete; fills not yet credited | **HIGH** — Exchange API limit (200 fills); fills beyond limit silently dropped. Timing vs `credit_fill` unclear. |
| **`_cycle_sweep()`** | `database.py:1750` | Every TP reset | `status='filled'` means "confirmed fill"; per-cycle net=0 → cycle done | **CRITICAL** — Filters by `status='filled'` (ignores `reset_cleared` fills corrupted by startup wipe); ignores cross-cycle hedge attribution. **Destroys real fill history.** |
| **`safe_mark_reset_cleared()` / `_apply_wipe_internal()`** | `wipe_proof.py:56/68` | TP hit, manual wipe, startup seal | Exchange position flat (verified) OR action in `excluded_carry_labels` | **CRITICAL** — Startup calls with `allow_nonzero_wipe=True` for `TP_HIT` on bots that NEVER had TP (startup-wipe bug). Wipes valid fills. |
| **`_reset_bot_after_tp_internal()`** | `database.py:1623` | TP hit, cycle advance, startup seal | `action_label='TP_HIT'` justifies `allow_nonzero_wipe=True` | **CRITICAL** — Called from `seal_all_active_bots()` during startup on hedge children with no TP. **Root cause of SOL bug.** |
| **`seal_all_active_bots()`** | `ledger.py:1132` | `startup_sync()` step 8 | All active bots need ledger reconciliation | **HIGH** — Calls `seal_trade_state()` on EVERY bot including hedge children, which triggers `_reset_bot_after_tp_internal()` → wipe. |
| **`reconstruct_offline_fills()`** | `reconciler.py:2372` | Startup barrier | Exchange fill history complete; client_order_id match works | **HIGH** — Fills beyond API limit (200) missed; CID format changes break matching. |
| **SNAP-ALLOCATE** | `database.py:3059` | Periodic parity check | `get_exchange_signed_net()` correct; `trades.open_qty` fresh | **HIGH** — Runs against stale DB cache; missing guard on key mismatch (O-9). |
| **One-Way Netting Repair** | `oneway_netting.py:809` | Orphan detection | Exchange positions ground truth; bot ownership via `normalized_pair` | **HIGH** — Key format mismatch (`BNB/USDC` vs `BNB/USDC:USDC`) causes false orphans. |
| **Hedge Watchdog / Parity Gates** | `parity_gates.py` | Cycle reset, orphan detection | `pair_parity_ok()` tolerance correct; `get_exchange_signed_net()` sign correct | **HIGH** — Multiple independent implementations of "virtual vs physical" with different sign conventions. |
| **`recompute_invested_from_orders()`** | `database.py:4123` | Seal, sync, health checks | `status IN ('filled','closed',...)` correctly identifies real fills | **HIGH** — Excludes `reset_cleared` (correct) but status may be corrupted by startup wipe/sweep. |

### B. Functions That Mutate `trades.open_qty` (Derived State)

| Function | File:Line | Trigger | What It Computes |
|----------|-----------|---------|-----------------|
| `_seal_trade_state_internal()` | `ledger.py:717` | TP, startup, manual, periodic | `recompute_invested_from_orders()` → writes `open_qty`, `total_invested`, `avg_entry_price` |
| `sync_trades_from_orders()` | `database.py:4422` | Periodic health check | Compares cached vs recomputed; writes if delta > 1e-6 |
| `startup_sync()` Scenario 2/3/4/5 | `database.py:162+` | Engine start | Heals phantom `open_qty`, zeroes stale accumulators |
| `SNAP-ALLOCATE` | `database.py:3059` | Periodic | Splits pair net across bots; writes `open_qty` per bot |

---

## Part 2: Core Structural Fix Proposal

### The Problem: Dual-Purpose Status Field

```
┌─────────────────────────────────────────────────────────────────────┐
│  bot_orders.status  ◄── CONFLICTING WRITERS                         │
│       │                                                                │
│       ├─► credit_fill()          ──► "This fill happened" (FACT)    │
│       ├─► _cycle_sweep()         ──► "Cycle closed, exclude" (LEDGER)│
│       ├─► safe_mark_reset_cleared() ──► "Wiped, exclude" (LEDGER)   │
│       └─► SNAP-ALLOCATE / parity  ──► "Include/exclude" (ACCOUNTING)│
│                                                                      │
│  RESULT: A real fill can be "erased" by a sweep/wipe that            │
│          only meant "this cycle is done for accounting purposes"     │
└─────────────────────────────────────────────────────────────────────┘
```

### The Fix: Separate Immutable Fact from Derived State

#### 1. **Immutable Fill Log** (Append-Only, Never Edited)

```sql
CREATE TABLE exchange_fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange_order_id  TEXT NOT NULL,          -- Binance orderId
    client_order_id    TEXT,                    -- CQB_... CID
    bot_id             INTEGER NOT NULL,
    symbol             TEXT NOT NULL,           -- normalized (BTCUSDC)
    side               TEXT NOT NULL,           -- BUY / SELL
    qty                REAL NOT NULL,           -- filled amount
    price              REAL NOT NULL,           -- fill price
    timestamp          INTEGER NOT NULL,       -- exchange fill time (ms)
    fee                REAL DEFAULT 0,
    fee_asset          TEXT,
    source             TEXT DEFAULT 'ws',       -- 'ws' / 'rest' / 'restore'
    ingested_at        INTEGER NOT NULL,       -- when we recorded it
    UNIQUE(exchange_order_id, bot_id, timestamp, side)  -- dedup
);

-- Index for fast bot+time queries
CREATE INDEX idx_exchange_fills_bot_time ON exchange_fills(bot_id, timestamp);
```

**Rules**:
- **INSERT only** — never UPDATE, never DELETE
- Written by: `credit_fill()`, `sync_stale_open_orders()`, `reconstruct_offline_fills()`
- **Single writer principle**: One canonical ingestion path per fill source
- **Dedup**: Unique constraint prevents double-crediting (solves ETH/LINK race)

#### 2. **Derived State = Single Canonical Reconciliation Function**

```python
# engine/reconciler.py — THE ONLY source of truth for "what is this bot/pair's position?"

def compute_bot_position(bot_id: int, as_of_ts: int = None) -> PositionSnapshot:
    """
    Compute bot's true position from IMMUTABLE fill history.
    Never reads bot_orders.status, never reads trades.open_qty.
    """
    fills = fetch_exchange_fills(bot_id, as_of_ts)
    # FIFO match BUY vs SELL per bot direction
    # Return: net_qty, avg_entry_price, total_invested, unrealized_pnl
    
def compute_pair_position(symbol: str, as_of_ts: int = None) -> PositionSnapshot:
    """Aggregate across all bots for a symbol."""
    bots = get_active_bots_for_symbol(symbol)
    return sum(compute_bot_position(b) for b in bots)

def reconcile_bot(bot_id: int) -> ReconciliationResult:
    """Compare derived position vs exchange position vs trades cache."""
    derived = compute_bot_position(bot_id)
    exchange = fetch_exchange_position(bot_id)
    cached = read_trades_cache(bot_id)
    # ... diff logic with tolerances
```

**All call sites migrate to this ONE function**:
- Startup barrier → `reconcile_bot(bot_id)`
- Periodic O-10 → `reconcile_pair(symbol)`
- Cycle sweep → `compute_bot_position(bot_id)` (read-only)
- SNAP-ALLOCATE → `compute_pair_position(symbol)`
- Hedge watchdog → `compute_bot_position(bot_id)`
- UI / Telegram → `compute_pair_position(symbol)`

#### 3. **bot_orders.status Becomes Read-Only Audit Trail**

| Status | Meaning | Mutability |
|--------|---------|------------|
| `filled` | Fill confirmed (historical) | **IMMUTABLE** once set |
| `reset_cleared` | Accounting: "excluded from cycle N ledger" | **IMMUTABLE** once set (audit only) |
| `cancelled` | Order cancelled (may have partial fills) | IMMUTABLE |
| `open`/`placing`/`cancelling` | In-flight order | MUTABLE (transient) |

**No process ever flips `filled` → `reset_cleared` again**. The sweep/wipe logic **computes** which cycles are "closed" at query time, doesn't mutate history.

#### 4. **Idempotent Fill Crediting**

```python
def credit_fill_idempotent(exchange_order_id, bot_id, side, qty, price, ts, cid):
    """
    Safe to call N times, safe to call out of order.
    Never loses a real exchange fill regardless of timing.
    """
    # 1. Try to insert into exchange_fills (unique constraint = dedup)
    try:
        insert_exchange_fill(...)
    except IntegrityError:
        return "already_recorded"
    
    # 2. Trigger derived state recompute (async or sync)
    schedule_reconciliation(bot_id)
    
    return "credited"
```

**No more**: "fill arrived after wipe → silently dropped" (ETH/LINK race fixed).

---

## Part 3: Migration Plan (Incremental, Zero Big-Bang)

### Phase 1: Add Immutable Fill Log (Week 1) — **Zero Behavior Change**
```bash
# 1. Create exchange_fills table + indexes
# 2. Modify credit_fill() to ALSO write to exchange_fills (dual-write)
# 3. Modify sync_stale_open_orders() to ALSO write to exchange_fills
# 4. Modify reconstruct_offline_fills() to ALSO write to exchange_fills
# 5. Run for 3-5 days — verify exchange_fills matches exchange history exactly
#    (count, qty, price, timestamp per bot per day)
```
**Risk**: Near zero. Only adds writes, doesn't change reads.

### Phase 2: Build Canonical Reconciliation Function (Week 2)
```bash
# 1. Implement compute_bot_position(), compute_pair_position(), reconcile_bot()
# 2. Unit test against REAL historical data (SUI 10018, SOL 100315, etc.)
# 3. Verify: output matches current `get_pair_virtual_net()` + `seal_trade_state()` for healthy bots
# 4. Verify: output correctly handles edge cases (cross-cycle, hedge, wipe, restart)
```
**Risk**: Low. Pure read-only function, tested against production data.

### Phase 3: Migrate Call Sites One-by-One (Week 3-4)
| Call Site | Migration | Test |
|-----------|-----------|------|
| `get_pair_virtual_net()` | → `compute_pair_position()` | Compare output for 30 days |
| Startup barrier | → `reconcile_bot()` per bot | Verify no new gaps |
| Cycle sweep | → `compute_bot_position()` (read-only, no status mutation) | Verify cycles 6/10 correctly NOT swept |
| SNAP-ALLOCATE | → `compute_pair_position()` | Verify allocation matches |
| Parity gates / hedge watchdog | → `compute_bot_position()` | Verify alerts match |
| UI / Telegram commands | → `compute_pair_position()` | Verify numbers match |

**Each migration**: Deploy behind feature flag → run in shadow mode → compare → cutover.

### Phase 4: Deprecate Mutable Status Mutations (Week 5)
```bash
# 1. Remove status='reset_cleared' writes from _cycle_sweep, safe_mark_reset_cleared
# 2. Replace with "cycle_is_closed(bot_id, cycle_id)" query function
# 3. bot_orders.status becomes: filled/cancelled/open only (immutable after filled)
# 4. Remove excluded_carry_labels / allow_nonzero_wipe complexity
```

### Phase 5: Cleanup (Week 6)
- Remove `trades.open_qty` as authoritative (keep as cache with TTL)
- Remove `wipe_wall_ts`, `cycle_phase` complexity
- Simplify `_reset_bot_after_tp_internal` to just "increment cycle_id"

---

## Part 4: Effort & Risk Estimate

| Phase | Files Touched | Call Sites | Est. Days | Risk |
|-------|---------------|------------|-----------|------|
| **1. Immutable Fill Log** | 4 (`ledger.py`, `reconciler.py`, `database.py`, `schema`) | 3 ingestion paths | 3-5 | 🟢 Very Low |
| **2. Canonical Reconciliation** | 2 (new `reconciler.py` functions, tests) | 1 new module | 5-7 | 🟢 Low |
| **3. Migrate Call Sites** | 8-10 (`database.py`, `ledger.py`, `parity_gates.py`, `oneway_netting.py`, `reconciler.py`, `startup.py`, `bot_executor.py`, UI) | ~15 call sites | 10-14 | 🟡 Medium (shadow mode mitigates) |
| **4. Deprecate Status Mutations** | 3 (`database.py`, `wipe_proof.py`, `ledger.py`) | 5 mutation sites | 3-5 | 🟡 Medium |
| **5. Cleanup** | 5+ | - | 3-5 | 🟢 Low |
| **TOTAL** | ~25 files | ~25 call sites | **24-36 days** | **Overall: 🟡 Medium** |

### Riskiest Parts
1. **Shadow mode divergence** — `compute_pair_position()` might differ from `get_pair_virtual_net()` in edge cases (cross-cycle, hedge, wipe). Mitigation: run both in parallel for 2 weeks, log diffs, fix before cutover.
2. **Fill ingestion completeness** — Exchange API limits, WS disconnections, CID format changes. Mitigation: reconciliation function must detect gaps and alert, not silently drift.
3. **Performance** — `exchange_fills` grows unbounded. Mitigation: partition by month, archive >90 days, keep hot 30 days indexed.
4. **Migration of `trades.open_qty` consumers** — Many places read `trades.open_qty` directly. Must audit all readers before deprecation.

### What This Migration Breaks If Done Carelessly
- **Silent drift** if `exchange_fills` misses fills (API limit, WS gap) and no fallback
- **Double-counting** if dual-write in Phase 1 isn't idempotent (same fill written twice)
- **Startup failures** if reconciliation function has a bug and startup barrier blocks
- **SNAP-ALLOCATE allocating wrong shares** if pair position wrong → bots trade wrong sizes

---

## Part 5: Immediate Next Steps (This Sprint)

Given the analysis, **do NOT implement the full migration yet**. Instead:

1. **Document this analysis** as `docs/architecture/IMMUTABLE_FILLS_ARCHITECTURE.md` ✅
2. **Apply minimal guards** already done (startup-wipe, cross-cycle sweep, virtual net sign) ✅
3. **Create JIRA/GitHub issues** for each Phase 1-5 task with owners
4. **Start Phase 1** (immutable fill log) in next sprint — it's the only zero-risk, high-value step
5. **Keep current fixes live** — they work and buy time for the structural fix

---

## Appendix: Bug-to-Root-Cause Mapping

| Bug | Surface Symptom | Root Cause (This Analysis) |
|-----|----------------|---------------------------|
| **Startup Wipe (SOL)** | Hedge child's entry fill → `reset_cleared` | `seal_all_active_bots()` → `_reset_bot_after_tp_internal()` → `safe_mark_reset_cleared(allow_nonzero_wipe=True)` unconditionally for `TP_HIT` |
| **SUI Cycle Sweep** | 93 real fills → `reset_cleared` | `_cycle_sweep()` filters `status='filled'`, ignores cross-cycle hedge, sweeps per-cycle net=0 |
|| **SNAP-ALLOCATE Missing Guard** | Allocation runs with stale key | **UNCONFIRMED — NEEDS INVESTIGATION**: Claim of `BNB/USDC` vs `BNB/USDC:USDC` key mismatch not verified in codebase. `normalize_symbol()` strips `:QUOTE` suffix identically for both formats. Root cause likely same as others (reads corrupted derived state), but specific key mismatch unproven. |
| **Entry Confirmed Loop** | Bot stuck with `entry_confirmed=0` but position>0 | `trades.open_qty` stale vs `bot_orders`; pre-flight guard added but root is dual-write |
| **ETH/LINK Fill-Credit Race** | Real fill credited, then wipe runs, fill lost | `credit_fill()` not idempotent; `sync_stale_open_orders()` timing vs wipe |
| **Balance Fetch Key Mismatch** | BNBUSDC virtual +0.04 vs exchange −0.04 | `get_pair_virtual_net()` sums raw `open_qty` without direction sign |
| **Cycles 6/10 Unresolved** | 80.9 units `reset_cleared` but shouldn't be | Sweep logic can't distinguish "truly balanced" vs "cross-cycle unbalanced" without immutable fill log |

---

**All roads lead to the same fix**: Immutable facts + single canonical reconciliation.