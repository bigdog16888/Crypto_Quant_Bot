# Architecture — Proof Ledger Enforcement (v3.5.0)

**Status:** Current production design. **Supersedes** `ARCHITECTURE_v3.4.md` for all parity, healing, maintenance, and shutdown behavior.

**Last updated:** 2026-05-20

---

## 0. What “correct down to the cent” means in this codebase

| Term | Definition |
|------|------------|
| **Proof** | Every unit of virtual position is backed by a `bot_orders` row with a `CQB_{bot_id}_…` clientOrderId whose fill came from (or was verified against) Binance order/trade history. Writes go only through `ledger.credit_fill()`. |
| **Parity (PASS)** | For each traded pair: `abs(get_pair_virtual_net(pair) - exchange_signed_net(pair)) <= PAIR_PARITY_QTY_TOLERANCE` (default **0.002** contracts). |
| **Parity (FAIL)** | Above tolerance → UI **SYSTEM MISMATCH**, bots on that pair → `REQUIRE_MANUAL_PROOF`, **no new entries**. |
| **Repair** | Startup action that forces ledger ↔ exchange alignment using exchange as authority when proof paths already failed (deflate / orphan flatten). Logged; not a substitute for proof on new fills. |
| **Patch (anti-pattern)** | Raw SQL `UPDATE bot_orders SET filled_amount=…`, forensic adopt, or UI reset without exchange flat. **Removed or gated in v3.5.** |

**UI dollar amounts** (`System $613` vs `Exchange $460`) are `qty × mark price` for display. **Pass/fail is decided in quantity space**, not USD rounding.

---

## 1. Version lineage (no whack-a-mole)

| Version | What changed | Type |
|---------|----------------|------|
| **v3.4.0** | Introduced `parity_gates.py`: cycle-reset gate, entry gate, forensic adopt off, proof flatten UI, phantom purge when exchange=0 | **Architecture** |
| **v3.4.1** | Startup pair repair, `startup_repair_mismatched_pairs`, testnet phantom purge | **Architecture** |
| **v3.4.2** | Monitor UI columns (Total Invested, Open Qty) | UI |
| **v3.5.0** | **Closes all known bypasses of the v3.4 proof model** (see §3). Split maintain vs entry gates. Cooperative shutdown. Deterministic startup repair. | **Architecture enforcement** |

v3.5 does **not** replace v3.4 — it **enforces** it in code paths that were still violating it.

---

## 2. Core invariants (must always hold)

### I1 — Single write path for fills

```
Binance fill → credit_fill() → bot_orders.filled_amount → seal_trade_state() → trades.*
```

**Forbidden:** `UPDATE bot_orders SET filled_amount=…` in reconciler/heal except inside `credit_fill` / explicit capped repair (deflate).

### I2 — Pair virtual net is the ledger sum

`database.get_pair_virtual_net(symbol)`:

- Sums entry/grid/adoption/carry minus tp/close/sl per bot, signed by direction.
- **v3.5:** `sold_qty` capped to `bought_qty` per bot per cycle (no orphan exit inflation).
- Same buckets as `recompute_invested_from_orders`.

### I3 — Exchange is physical authority

`parity_gates.get_exchange_signed_net()` from live `fetch_positions()` (one-way signed contracts).

### I4 — Parity tolerance is explicit

Config: `PAIR_PARITY_QTY_TOLERANCE` (env, default `0.002`).  
**HEALTHY** iff `audit_pair_ledger_vs_exchange()` returns **zero** rows.

### I5 — No silent ledger inflation on startup

Offline/history heal:

1. `gate_heal_fill_qty(pair, qty)` — cannot credit more than `abs(exchange) - abs(virtual)` for same sign.
2. `gate_heal_exit_without_entry(bot_id, type, qty)` — no TP/close credit without entry/grid in cycle.
3. Uses `credit_fill()`, not raw SQL.

### I6 — In-trade maintenance is not blocked by ledger over-count

- **New entries:** `gate_trading_allowed()` — strict parity.
- **TP/Grid for open positions:** `gate_maintain_orders_allowed()` — allows maintenance when `total_invested > 0` and signs are not opposite (ledger/exchange). Prevents “MISSING GRIDS” while repair runs.

### I7 — Grid placement is exchange-backed

DB row alone does **not** block grid placement. Block only if grid is **open on exchange**. Stale DB grid → cancelled → new grid placed.

### I8 — Shutdown releases SocketLock immediately

`engine/shutdown_control.py`: stop file → runner releases port **19888** before long flush; UI waits on port, then PID terminate if needed.

---

## 3. v3.5.0 modules and behavior

### 3.1 `engine/parity_gates.py` (extended)

| Function | Role |
|----------|------|
| `pair_heal_budget` / `gate_heal_fill_qty` | Max qty startup heal may add per pair |
| `gate_heal_exit_without_entry` | Blocks orphan TP credits (e.g. short bot scanning + TP fill) |
| `deflate_pair_ledger_overcount` | **Repair:** ledger > exchange (same sign) → trim entry/grid `filled_amount` newest-first until parity |
| `repair_exchange_orphan_when_ledger_flat` | **Repair:** ledger ≈ 0, exchange ≠ 0 → CQB proof adopt OR `AUTO_REPAIR_ORPHAN_EXCHANGE` reduceOnly flatten |
| `gate_maintain_orders_allowed` | **§I6** — split from entry gate |
| `gate_trading_allowed` | **§I4** — new entries only |

### 3.2 `engine/reconciler.py` — `[HEALING]` block

**Before v3.5:** Raw SQL update on `filled_amount` → double-count (BTC 0.004 vs 0.002).  
**v3.5:** `credit_fill` + heal gates only.

### 3.3 `engine/database.py`

| Change | Role |
|--------|------|
| `get_pair_virtual_net` sold cap | **I2** |
| `consolidate_duplicate_bot_orders` | Keeper = best fill + real `order_id`, not arbitrary row |
| `verify_filled_orders_against_exchange` | Delta-only `credit_fill` + heal gates |
| WIPE-AUDIT | `debug` for legacy `reset_cleared` without snapshot (not active gap) |

### 3.4 `engine/bot_executor.py` — `maintain_orders`

| Change | Role |
|--------|------|
| `gate_maintain_orders_allowed` | **I6** |
| Grid idempotency | **I7** — verify on exchange |

### 3.5 `engine/shutdown_control.py` (new)

Cooperative stop: PID file, port 19888 check, interruptible sleep, UI terminate fallback.

### 3.6 `config/settings.py`

| Flag | Default (testnet) | Meaning |
|------|-------------------|---------|
| `AUTO_REPAIR_ORPHAN_EXCHANGE` | True | Ledger flat, exchange has size → proof adopt or flatten |
| `TESTNET_PURGE_PHANTOM_LEDGER` | True | Exchange flat, ledger not → safe_wipe |
| `ALLOW_FORENSIC_ADOPT` | False | No invented fills |
| `PAIR_PARITY_QTY_TOLERANCE` | 0.002 | Parity pass/fail |

---

## 4. Startup sequence (deterministic outcomes)

After **Start Monitoring**, `startup_sync()` runs in order:

1. Prime `active_positions`
2. `reconstruct_offline_fills` (CQB only, heal gates)
3. `heal_inflated_filled_amounts` / `consolidate_duplicate_bot_orders`
4. `verify_filled_orders_against_exchange` (delta + gates)
5. `audit_pair_ledger_vs_exchange` → if fail: `REQUIRE_MANUAL_PROOF`
6. Per-pair CQB history repair (forensic off)
7. `startup_repair_mismatched_pairs`:
   - exchange ≈ 0, ledger ≠ 0 → phantom purge (testnet)
   - ledger > exchange (same sign) → **deflate** (deterministic trim)
   - ledger ≈ 0, exchange ≠ 0 → **orphan repair** (proof adopt or flatten)
8. Re-audit → **either 0 mismatches or mismatch remains flagged**
9. `seal_all_active_bots`

**Pass criterion:** Step 8 reports **0** mismatched pairs.  
**Not acceptable:** “might improve” — if mismatch remains, UI stays **MISMATCH** until operator resolves or repair logs show success.

---

## 5. Operator-visible symptoms → cause → v3.5 fix

| Symptom | Root cause (proven in logs) | v3.5 fix |
|---------|------------------------------|----------|
| BTC virtual 2× exchange | `[HEALING]` raw SQL double-credit | credit_fill + heal budget |
| SOL virtual inflated, short scanning | Orphan TP credit, no entry | exit-without-entry gate + sold cap |
| XAU ledger 0, exchange short | Wiped ledger, residual exchange | orphan repair (proof or flatten) |
| MISSING GRIDS (1/2) | Grid idempotency + parity blocked maintain | I6 + I7 |
| Grids “disappeared” | DB ghost row blocked placement | I7 stale grid eviction |
| Stop Monitoring hangs | Stop only between cycles; lock held | shutdown_control |
| WIPE-AUDIT spam | Legacy rows, every recompute | debug only |

---

## 6. What still requires human action

| Situation | Why | Action |
|-----------|-----|--------|
| Mismatch after startup repair | Repair failed or opposite-sign gap | `💥 Close` proof flatten on pair (logged) |
| `REQUIRE_MANUAL_PROOF` | Parity fail or orphan no proof | Resolve pair, then reset status |
| Opposite-sign virtual vs exchange | Cannot auto-heal safely | Proof flatten |

**Close button** is not the primary fix path — it is the **escalation** when automated repair cannot prove or align.

---

## 7. Proof vs repair (honest boundary)

| Mechanism | Class | Proof level |
|-----------|-------|-------------|
| WS / REST fill → `credit_fill` | **Proof** | Order ID + cumulative qty |
| CQB history heal with heal budget | **Proof-limited** | Per-order, capped by exchange net |
| `deflate_pair_ledger_overcount` | **Repair** | Exchange authority; trims DB to match |
| `repair_exchange_orphan_when_ledger_flat` | **Repair** | Adopt if CQB match; else flatten |
| `purge_phantom_ledger_when_exchange_flat` | **Repair** | Exchange flat proof |

**Goal for v3.6+ (not yet done):** order-level proof for every repair trim (audit trail per reduced row). v3.5 stops inflation and restores parity; it does not yet re-prove every historical row.

---

## 8. Files to read (in order)

1. `docs/ARCHITECTURE_v3.5.md` (this file)
2. `docs/OPERATOR_MISMATCH_RUNBOOK.md` (updated for v3.5)
3. `engine/parity_gates.py`
4. `engine/ledger.py` — `credit_fill`
5. `engine/database.py` — `get_pair_virtual_net`
6. `tests/test_parity_gates.py`

---

## 9. Regression checklist (prove v3.5, 7 days testnet)

1. `audit_pair_ledger_vs_exchange` → **0 rows** after each cold start (or logged repair success then 0).
2. No log line `UPDATE bot_orders SET filled_amount` in `[HEALING]` path.
3. In-trade bots show **2** open CQB orders (TP + grid) when step ≥ 1.
4. Stop Monitoring → port 19888 free within 60s without Force Kill.
5. No new `forensic_adoption_*` rows.
6. After TP cycle, parity still 0 within tolerance.

If any fail → treat as **regression**, fix in parity/ledger layer, update this doc.
