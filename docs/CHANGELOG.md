# Changelog — Crypto Quant Bot

All notable **architecture** changes are documented here. Version numbers match `CODEBASE_GUIDE.md` and `docs/ARCHITECTURE_v3.x.md`.

---

## v3.5.2 — 2026-05-21 — Orphan-repair stale-snapshot deadlock

**Root cause:** `repair_exchange_orphan_when_ledger_flat` placed a `reduceOnly` market order to flatten
orphan exchange positions, then immediately called `reset_bot_after_tp`. The `_fetch_pos_wrapper`
closure inside `safe_mark_reset_cleared` reads `active_positions` (cached snapshot, NOT yet updated
from the WS fill event). It still saw the pre-flatten qty → raised `WipeBlockedError` → bot ledger
stayed stuck with phantom `open_qty`. This repeated every ~7 s as a `-2022 ReduceOnly Order rejected`
loop (engine tried to TP-maintain a phantom position with no exchange backing).

- **Fix — `engine/parity_gates.py`:**
  - `repair_exchange_orphan_when_ledger_flat`: DELETE `active_positions` for the pair immediately after
    confirmed flatten, before calling reset. Eliminates the stale-snapshot false positive.
  - Sleep increased 0.5 s → 1.0 s.
  - `'ORPHAN_EXCHANGE_REPAIR'` added to `CYCLE_RESET_CARRY_LABELS` so `assert_cycle_reset_allowed`
    bypasses the parity gate (caller already verified exchange flat).
- **Fix — `engine/database.py`:**
  - `_reset_bot_after_tp_internal`: `'ORPHAN_EXCHANGE_REPAIR'` added to `excluded_carry_labels`
    → `safe_mark_reset_cleared` uses `allow_nonzero_wipe=True` for this path only.
- **Verified:** All 8 pairs = 0 mismatches. Only 4 genuinely in-trade bots appear in
  `SELECT … WHERE open_qty > 0`.

## v3.5.1 — 2026-05-20 — Parity repair correctness

- **Fix:** `deflate_pair_ledger_overcount` now runs when `excess > tolerance` (was wrongly skipping when excess == tolerance, e.g. BTC 0.008 vs 0.006).
- **Fix:** `audit_pair_ledger_vs_exchange` uses `PAIR_PARITY_QTY_TOLERANCE` (was hardcoded 0.0001).
- **Fix:** Orphan repair credits all matching CQB closed orders, then flattens remainder if still not in parity.
- **Fix:** Startup logs `[STARTUP-DEFLATE]` / `[STARTUP-ORPHAN-REPAIR]` / `[STARTUP-PARITY-REMAINING]`.

## v3.5.0 — 2026-05-20 — Proof ledger enforcement

**Theme:** Close every bypass of the v3.4 proof-only model. Deterministic parity; no “maybe improves.”

### Architecture (fundamental)

- **Heal write path:** `reconciler` `[HEALING]` and `verify_filled_orders_against_exchange` use `credit_fill` + `gate_heal_fill_qty` / `gate_heal_exit_without_entry` only.
- **Pair heal budget:** Startup cannot credit more qty than `abs(exchange_net) - abs(virtual_net)` (same sign).
- **Virtual net integrity:** `sold_qty <= bought_qty` per bot/cycle in `get_pair_virtual_net`.
- **Startup repair (deterministic):**
  - `deflate_pair_ledger_overcount` — ledger > exchange
  - `repair_exchange_orphan_when_ledger_flat` — ledger ≈ 0, exchange ≠ 0 (`AUTO_REPAIR_ORPHAN_EXCHANGE`)
- **Split trading gates:**
  - `gate_trading_allowed` — new entries (strict parity)
  - `gate_maintain_orders_allowed` — TP/grid for in-trade bots (parity warn, not block on over-count)
- **Grid idempotency:** Block only if grid is live on exchange; evict stale DB rows.
- **CID dedup:** Keep row with best fill + real exchange `order_id`.
- **Shutdown:** `engine/shutdown_control.py` — cooperative stop, early SocketLock release.

### Config

- `AUTO_REPAIR_ORPHAN_EXCHANGE` (default True on testnet)

### Docs

- `docs/ARCHITECTURE_v3.5.md` (authoritative for parity/heal/maintain)
- `docs/CHANGELOG.md` (this file)

### Tests

- `tests/test_parity_gates.py` — heal gates, deflate, maintain gate

---

## v3.4.2 — 2026-05-19

- Monitor: Total Invested row, Open Qty column

## v3.4.1 — 2026-05-19

- `startup_repair_mismatched_pairs`, testnet phantom purge

## v3.4.0 — 2026-05-19

- `parity_gates.py`, cycle reset gate, proof flatten, forensic adopt disabled

See `docs/ARCHITECTURE_v3.4.md` for v3.4 design (historical).
