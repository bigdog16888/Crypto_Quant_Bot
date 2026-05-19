# Architecture — Proof-Only Trading Stack (v3.4.x)

**Status:** Production design for testnet/mainnet. Supersedes ad-hoc reconciliation patches.

---

## 1. Layers

```
┌─────────────────────────────────────────────────────────────┐
│  UI (Streamlit) — monitor.py, app.py                        │
│  Display only + human actions (Close, settings, Start Monitor)│
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  Engine — runner.py (loop) + bot_executor.py (orders)         │
│  Gates: parity_gates.py (v3.4)                              │
└───────────────────────────┬─────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
   bot_orders          active_positions    Binance FAPI
   (proof ledger)      (exchange snapshot)  (source of truth)
```

| Layer | Role |
|-------|------|
| **bot_orders** | Every fill is a row keyed by `CQB_{bot_id}_…` clientOrderId |
| **trades** | Cached per-bot state (`total_invested`, `open_qty`, cycle) derived from bot_orders |
| **active_positions** | Exchange snapshot; used for reduceOnly and SNAP-ALLOCATE |
| **Exchange** | Final authority for physical net (one-way: one signed net per symbol) |

---

## 2. Startup flow (correct operator sequence)

```
run_bot.bat  →  Streamlit UI
     │
     ▼
▶ Start Monitoring  →  engine/runner.py
     │
     ▼
startup_sync()
  1. Prime active_positions (live fetch_positions)
  2. reconstruct_offline_fills (CQB-tagged history only)
  3. heal_inflated_filled_amounts / consolidate_duplicate_bot_orders
  4. verify_filled_orders_against_exchange
  5. audit_pair_ledger_vs_exchange → flag REQUIRE_MANUAL_PROOF
  6. per-pair CQB history repair
  7. phantom purge if exchange≈0 and ledger≠0 (TESTNET_PURGE_PHANTOM_LEDGER)
  8. seal_all_active_bots / adopt_from_physical_positions
     │
     ▼
Main loop: maintain_orders / execute_entry (blocked if pair mismatch)
```

**Pre-Flight Sync** in the UI only refreshes `active_positions`; it does not replace startup_sync.

---

## 3. v3.4 parity invariants (fundamental fixes)

### 3.1 Cycle reset gate (`parity_gates.assert_cycle_reset_allowed`)

Before `reset_bot_after_tp` (TP_HIT, etc.):

> After this bot’s contribution is removed from pair virtual net, does ledger still match exchange?

If not → `CycleResetBlockedError` → bot set to `REQUIRE_MANUAL_PROOF`.

Prevents LINK/SOL “2×” class: DB cycle cleared while exchange still holds size.

### 3.2 Trading gate (`parity_gates.gate_trading_allowed`)

Before `execute_entry` / `maintain_orders`:

> Does `get_pair_virtual_net(pair)` match signed exchange net?

If not → no orders; `REQUIRE_MANUAL_PROOF`.

### 3.3 Forensic adopt disabled (`ALLOW_FORENSIC_ADOPT=False`)

WS orphan fills and reconciler `forensic_mode` do not invent `forensic_adoption_*` rows.

### 3.4 Phantom ledger purge (`TESTNET_PURGE_PHANTOM_LEDGER`)

When **exchange net = 0** but **virtual net ≠ 0** (XRP class):

`safe_wipe_bot(force=True)` per bot on pair — ledger reset, **no market order**.

### 3.5 Proof flatten (UI `💥 Close`)

Cancel CQB orders → reduceOnly market → verify flat → `MANUAL_CLOSE` reset all bots on pair.

---

## 4. Single source of truth for netting UI

`database.get_pair_virtual_net(symbol)` — sums signed contributions from all active bots using the same SQL buckets as `recompute_invested_from_orders`.

Monitor **Global Netting** compares:

- **Virtual:** `get_pair_virtual_net`
- **Physical:** live `fetch_positions()` signed `net_qty` (not summed `active_positions` rows)

---

## 5. What we removed (anti-patterns)

| Removed | Why |
|---------|-----|
| Forensic WS adopt | Inflated ledger without exchange proof |
| Blind UI reset without flat exchange | Created 2× gaps |
| Summing virtual USD across symbols | Meaningless headline number |
| `reset_cleared` cycle reset without exchange check | Root cause of ghost carry |

---

## 6. Supported operator scripts

| Script | Purpose |
|--------|---------|
| `scripts/run_startup_heal.py` | Heal + parity audit without full loop |
| `scripts/repair_phantom_ledger.py` | Purge ledger when exchange flat |
| `scripts/diag_live_state.py` | Read-only parity report |

Do **not** use `scratch/*.py` (removed); those were one-off debug scripts.

---

## 7. Monitoring checklist (prove fix over time)

Watch for **7 days** on testnet:

1. Global Netting stays **0 mismatched pairs** after normal TP cycles.
2. No new `forensic_adoption_*` rows in `bot_orders`.
3. `engine.log` has no `[CYCLE-RESET-BLOCKED]` during valid TP (only when real gap).
4. After TP, `get_pair_virtual_net` ≈ exchange within `PAIR_PARITY_QTY_TOLERANCE`.

If (3) fires often on healthy TP → bug in gate or stale exchange fetch.

---

## 8. Code review notes (v3.4.1)

| Area | Assessment |
|------|------------|
| `parity_gates.py` | Core enforcement; keep all reset/trade paths wired through it |
| `safe_wipe_bot` + `REQUIRE_HUMAN_APPROVAL` | Force wipes must pass `human_approved=True` (fixed for phantom purge) |
| `reconciler` MARKET-FLATTEN | Still blocked when `REQUIRE_HUMAN_APPROVAL=True`; use UI Close or disable for testnet auto-flat |
| `SNAP-ALLOCATE` | Can log “Net matches” when virtual is wrong; parity audit is the real check |
| `scratch/` | Deleted; use `scripts/` only |

---

## 9. Version map

| Version | Change |
|---------|--------|
| v3.4.0 | Parity gates, proof flatten UI, forensic adopt off |
| v3.4.1 | Phantom ledger purge on testnet, startup pair repair |
| v3.4.2 | UI: Total Invested row + Open Qty columns in monitor |

See `CODEBASE_GUIDE.md` §7 for full changelog.
