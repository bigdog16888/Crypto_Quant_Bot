# Architecture Decisions — Crypto_Quant_Bot

## 1. One-Way Mode (Immutable Design Choice)

**This system is explicitly designed for Binance One-Way Mode (`dualSidePosition: false`).**

### What One-Way Mode means

| Fact | Detail |
|------|--------|
| Single net position per symbol | The exchange keeps ONE position per symbol. Positive = LONG, negative = SHORT. |
| `positionSide` is always `BOTH` | The raw API response always has `positionSide: "BOTH"`. It carries zero directional information. |
| No `positionSide` on orders | Sending `positionSide=LONG/SHORT` → Binance 400 error. |
| Bots net internally | Multiple bots on the same pair share the single exchange position. Their internal positions sum to the exchange net. |

### Why One-Way (not Hedge Mode)

| Reason | Detail |
|--------|--------|
| **Margin efficiency** | Long and short bots offset each other. A $100 LONG bot and $80 SHORT bot only require margin for the $20 net, not $180. |
| **Simpler exchange interface** | No need to manage separate LONG/SHORT legs, position mode switches, or dual-side margin calculations. |
| **Natural risk distribution** | The hedge feature (parent/child bot pairs) provides risk management internally without exchange complexity. |
| **Testnet compatibility** | Binance testnet demo accounts default to One-Way. No mode switching required. |

### What this means for code

| Rule | Implementation |
|------|----------------|
| Never send `positionSide` | `exchange_interface.py` strips it from all order params |
| Direction from sign | `sign(positionAmt)`: + = LONG, − = SHORT |
| Close via `reduceOnly` | `side=sell, reduceOnly=True` for LONG close; `side=buy, reduceOnly=True` for SHORT close |
| Testnet exception | Demo FAPI requires `positionSide='BOTH'` on orders (handled in `exchange_interface.py`) |

---

## 2. Hedge Feature — Bot-Level Risk Management

**The hedge feature is a BOT-LEVEL risk tool, NOT exchange Hedge Mode.**

### How it works

```
Parent bot (standard, LONG)
  Entry → Step 1 → ... → Step hedge_trigger_step
                                      ↓ spawn hedge child
                                Child (hedge_child, SHORT)
                                  accumulates SHORT grids internally

  Parent TP fills
    ├─ Register child BE TP intent
    ├─ Cancel parent exchange orders
    ├─ Zero parent trade row
    ├─ Set parent → pending_hedge_close
    └─ cycle_id NOT incremented

  Child now in 'be_only' mode:
    ├─ Cancel all grid/entry orders
    └─ Resting limit BE TP at avg_entry_price

  Child BE TP fills
    ├─ Child → hedge_standby / reset
    └─ complete_parent_cycle_after_hedge()
         ├─ Increment parent cycle_id
         └─ Parent → Scanning
```

### Internal accounting vs exchange reality

| Layer | Parent (LONG) | Child (SHORT) | Net |
|-------|---------------|---------------|-----|
| Internal DB | open_qty = +100 | open_qty = -80 | +20 |
| Exchange | — | — | +20 (single position) |

The child does NOT create a separate exchange position. In One-Way mode, everything nets. The child is an INTERNAL ACCOUNTING concept for risk distribution.

### Hedge lifecycle states

| State | Meaning |
|-------|---------|
| `hedge_standby` | Child armed, waiting for parent trigger |
| `HEDGE_ACTIVE` | Child accumulating SHORT grids |
| `be_only` | Parent exited, child only maintains BE TP |
| `pending_hedge_close` | Parent TP'd, waiting for child BE TP |

---

## 3. Proof-Only Reconciliation Architecture

**The system never invents fills, never guesses, never forensically adopts without evidence.**

### Three-layer state model

| Layer | Source of Truth | Authority |
|-------|-----------------|-----------|
| 1. Exchange Physical Position | Binance REST/WS | Reality |
| 2. Bot Orders Ledger (`bot_orders`) | `credit_fill()` only | History |
| 3. Trades Cache (`trades`) | `seal_trade_state()` recompute | Eventually consistent |

### Key invariants

| Invariant | Enforcement |
|-----------|-------------|
| INV-19 | Unique `(bot_id, client_order_id)` — no duplicate orders |
| INV-20 | `fill_claims UNIQUE(order_id, bot_id)` — no double fill credit |
| INV-27 | Only `credit_fill()` and `seal_trade_state()` write `open_qty` |
| INV-31 | `WriteQueue` singleton — single-threaded DB writes |
| INV-34 | `credit_fill()` processes fills regardless of bot status |

### Parity discipline

For every pair: `abs(virtual_qty - exchange_qty) <= PAIR_PARITY_QTY_TOLERANCE` (default 0.002).

UI **HEALTHY** only when `audit_pair_ledger_vs_exchange()` returns zero rows. No "close enough."

---

## 4. Write Serialization (INV-31)

**All database writes targeting `trades` and `bot_orders` tables run through the `WriteQueue` singleton.**

### Why

| Problem | Solution |
|---------|----------|
| WS fill + REST stale sync both credit same fill | `fill_claims` UNIQUE constraint |
| Concurrent `seal_trade_state` corrupts `open_qty` | Single worker thread |
| Nested `put_and_wait` deadlocks | Worker thread identity check → execute inline |

### Bypass rules

| Condition | Behavior |
|-----------|----------|
| `_bypass = True` (pytest) | Execute directly, no queue |
| Called from worker thread | Execute directry, no queue |
| Normal operation | Enqueue, wait for worker |

---

## 5. Safety Layers (O-1 through O-10)

| Layer | Name | Trigger | Action |
|-------|------|---------|--------|
| O-1 | Position-size circuit breaker | Single order > threshold | Block order |
| O-2 | UI status unification | Status mismatch | Alert operator |
| O-3 | Rolling drawdown breaker | 20% drop over 24h | Emergency liquidation |
| O-9 | Startup plausibility gate | Implausible exchange data | Block destructive reconciliation |
| O-10 | Hedge-engagement watchdog | Child fails to engage within timeout | Freeze parent to REQUIRE_MANUAL_PROOF |

---

## 6. Documentation Map

| Document | Purpose |
|----------|---------|
| `CODEBASE_GUIDE.md` | Single authoritative codebase guide (read first) |
| `ARCHITECTURE_v3.5.md` | Current architecture reference |
| `CHANGELOG.md` | Version history |
| `OPERATOR_MISMATCH_RUNBOOK.md` | Step-by-step repair procedures |
| `PROJECT_STATUS.md` | Current state, open items, standing rules |
| `docs/adr/` | Architecture decision records |

---

## 7. Key Design Principles

| Principle | Meaning |
|-----------|---------|
| **Proof-only** | Never invent fills. Every ledger entry has exchange evidence. |
| **Idempotent** | `seal_trade_state()` can be called N times safely. |
| **Fail-open to preserve fills** | If a guard check fails, log and continue (don't drop legitimate fills). |
| **Exchange first, DB second** | INV-15: never write "flat" to DB before confirming exchange is flat. |
| **One-Way forever** | The system is designed for One-Way mode. Hedge Mode is not supported. |

---

*Last updated: 2026-08-31 | Version: 5.3.7*
