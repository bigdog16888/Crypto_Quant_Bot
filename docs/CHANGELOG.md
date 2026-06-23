# Changelog — Crypto Quant Bot

All notable **architecture** changes are documented here. Version numbers match `CODEBASE_GUIDE.md` and `docs/ARCHITECTURE_v3.x.md`.

## v4.1.4 — 2026-06-22 — Pre-Advance Invariant Check

- **engine/database.py**:
  - **Pre-Advance Invariant Check**: Closes the structural root cause of the recurring cycle-abandon-with-unaccounted-fill bug.
  - Before advancing `trades.cycle_id`, compares `bot_orders` ground-truth net qty against `trades.open_qty`. If they diverge by > 1e-6, forces `seal_trade_state(force_recompute=True)`.
  - In `check_and_repair_inconsistent_state`, queries `bot_orders` net qty for the current cycle before setting `cycle_id=NULL` or wiping phantom-invested; blocks the wipe if fills exist.
- **tests/test_v414_pre_advance_invariant.py**:
  - Created unit tests verifying the pre-advance invariant, ghost-step wipe blocking, and phantom-invested wipe blocking.

## v4.1.3 — 2026-06-22 — Phase 2 Exchange-Authoritative Position Sync & Stale Trades Recovery

- **engine/oneway_netting.py**:
  - Added `_attempt_drift_correction()` to perform a FIFO reseal of all active bots on the pair via `seal_trade_state()` on drift detection.
  - If drift persists, writes `[MANUAL-REVIEW]` flag to `bots.notes` and `exchange_sync_diagnostics.json`.
- **engine/reconciler.py**:
  - Added `[OFFLINE-STALE-TRADES]` path in `_reconstruct_offline_fills_internal`. Rolls `trades.cycle_id` back and force-reseals when `bot_orders.filled_amount` exists but `trades.open_qty` is zero.

## v4.1.2 — 2026-06-22 — Reconciler Write Path Serialization

- **engine/reconciler.py**:
  - Wrapped key reconciler functions (`reconcile_all`, `reconstruct_offline_fills`, `_fix_ghost_bot`, `_align_memory_to_ledger`, `adopt_from_physical_positions`) entirely in `WriteQueue` to resolve write race conditions.
  - Parameterized task timeouts in `WriteQueue` via `_wq_timeout` (increased to 120s).

## v4.1.1 — 2026-06-22 — Write Queue Timeout & Thread Self-Healing

- **engine/write_queue.py**:
  - Added 30s timeout to `task.event.wait()` and auto-restart dead worker thread.

## v4.1.0 — 2026-06-22 — Write Serialization (INV-31)

- **engine/write_queue.py**:
  - Implemented thread-safe `WriteQueue` singleton class to serialize all writes targeting `trades` and `bot_orders` tables.
- **engine/ledger.py**:
  - Wrapped `credit_fill()` and `seal_trade_state()` to execute via the write queue.
- **engine/database.py**:
  - Wrapped `reset_bot_after_tp()` to execute via the write queue.
- **engine/oneway_netting.py**:
  - Wrapped `apply_oneway_entry_cross_reduction()` to execute via the write queue.

## v4.0.5 — 2026-06-22 — Parent-Child Handoff Status Gates & BE-Only Freeze (INV-29)

- **engine/ledger.py**:
  - Implemented parent `pending_hedge_close` gate in `handle_tp_completion` when its child is active, and added parent unblock callback `complete_parent_cycle_after_hedge()` when child closes.
- **engine/bot_executor.py**:
  - Added child `'be_only'` state to freeze grid order placement and cancel active grids when the parent has exited.
- **engine/runner.py**:
  - Excluded bots in `'pending_hedge_close'` status from execution runs.
- **ui/views/monitor.py**:
  - Extracted notification rendering to an asynchronous `@st.fragment` (`_notifications_fragment`) to prevent full view load blocks.

## v4.0.4 — 2026-06-12 — Skip/Consolidated Release

- Skipped / Consolidated release. Combined refinements directly into v4.0.5.

## v4.0.1 — 2026-06-12 — Sibling TP Cancel & Physical Orphan Check (INV-28A / INV-28B)

- **engine/oneway_netting.py**:
  - Implemented sibling TP order cancellation and filling bot physical orphan check to resolve netting race conditions.

## v4.0.0 — 2026-06-11 — Database Schema Standardization & Hedge Qty Deprecation (ADR-004)

- **engine/database.py**:
  - Removed the deprecated `hedge_qty` column from `trades` table. All queries updated to use signed net virtual calculations.

## v3.9.19 — 2026-06-08 — Hedge Child Ghost Detection & Missed BE TP Self-Healing (INV-26)

- **engine/oneway_netting.py**:
  - **Hedge Child Ghost Detection**: Added `detect_hedge_child_ghost()` to precisely verify if only the child bot's portion of a hedged position is gone (by comparing expected parent-only net with actual signed exchange net). Added `wipe_hedge_child_ghost()` to safely cancel orders, zero child trade metrics, set status to `hedge_standby`, log a critical error, and record a `drift_note` audit row.
- **engine/runner.py**:
  - Run the hedge child ghost detection check on startup sync right after the global wipe check.
- **engine/reconciler.py**:
  - Run the hedge child ghost detection check during every reconciler pass cycle.
- **engine/bot_executor.py**:
  - **Missed BE TP Self-Healing (INV-26)**: Added self-healing logic inside `maintain_orders()` to detect if a parent bot has completed its cycle while its hedge child still has `open_qty > 0` and no active TP order exists. Instantly registers and places a break-even TP order.
- **CODEBASE_GUIDE.md**:
  - Documented invariant `INV-26`.

## v3.9.18 — 2026-06-08 — Precise DNA-WIPE Wall & Hedge Child Standby Status

- **engine/database.py**:
  - **Precise Wipe Wall (INV-25)**: Updated `[DNA-WIPE]` self-healing routine to query the most recent filled order timestamp (`status IN ('filled','partially_filled') AND filled_amount > 0`) for the wiped cycle and set `trades.wipe_wall_ts` and `trades.cycle_start_time` to it. Falls back to current system time if no fills exist. This prevents incorrect post-wipe forensic adoptions of historical orders.
  - **Hedge Child Status Preservation**: Set resting status to `'hedge_standby'` instead of `'Scanning'` if the bot type is `'hedge_child'` during a DNA-wipe.
- **CODEBASE_GUIDE.md**:
  - Documented invariant `INV-25`.

## v3.9.17 — 2026-06-08 — Hedge-Aware Residue Bypass & Signed Exposure

- **engine/reconciler.py**:
  - **Wrong-Side Residue Bypass**: Skip the `len(pair_positions) <= 1` wrong-side residue check for hedged bots, as hedge child bots are designed to hold opposite positions to parents and are not trapped residuals.
  - **Direction-Signed Exposure**: Ensure `pair_net_virtual` uses signed quantities based on `BotState.direction` (`parent_qty - child_qty` if parent is `LONG`, else `-parent_qty + child_qty`).

## v3.9.16 — 2026-06-08 — Hedge-Aware Reconciler & Child Cycle ID Repair

- **engine/reconciler.py**:
  - **Hedge-Aware Reconciler**: Skip `UNAUTHORIZED_LOSS` gate for parent-child hedge pairs when pair-level virtual net matches signed exchange net within tolerance. For non-hedged bots, use the clamped `unrelated_opposite_virtual` formula.
- **engine/bot_executor.py**:
  - **Stale Cycle ID Repair**: Update `trades.cycle_id` of the hedge child from `bot_orders` filled entries if the child holds a position but has a stale cycle ID, triggering `seal_trade_state()` and returning `'active'`.
- **tests/test_hedge_lifecycle.py**:
  - Added `TestV3916ReconcilerFixes` verifying the stale cycle ID repair and hedge-aware reconciler.

## v3.9.11 / v3.9.12 / v3.9.13 / v3.9.14 / v3.9.15 — 2026-06-05

- **General**: Incremental versions documenting intermediate fixes.

## v3.6.1 — 2026-06-03 — Reset hedge child to hedge_standby after TP

- **engine/database.py** (`_reset_bot_after_tp_internal`):
  - Reset status of `hedge_child` bots to `'hedge_standby'` instead of `'Scanning'` after a TP cycle completes.
- **engine/oneway_netting.py** (`apply_oneway_entry_cross_reduction`):
  - Skip bots with status `'hedge_standby'` in the opposite-direction netting cross-reduction guard.

## v3.6.2 — 2026-05-28 — Direction-Aware TP Capacity Clip & Stale Sibling Guard

- **engine/bot_executor.py**:
  - **Fix 1 (`_prepare_tp_order_params`)**: Clip now checks physical position SIDE against bot's closing direction. SHORT bot BUY TP on net-LONG pair correctly gets capacity=0 and falls to GTX instead of firing reduceOnly into a -4118 rejection.
  - **Fix 2 (`_is_order_net_reducing`)**: Sole-bot override now verifies physical net direction matches before returning True, preventing stale sibling count (sibling just reset) from triggering false reduceOnly on opposite-side physical net.
- **CODEBASE_GUIDE.md**:
  - Added invariant 3.21 (TP Capacity is Direction-Aware).

## v3.6.1 — 2026-05-28 — Permanent fix for hedge child cycle_id desync

- **engine/bot_executor.py** (`_signal_hedge_child_entry`):
  - Added invariant sync: immediately after `save_bot_order` for the child bot, update `trades.cycle_id = parent_cycle_id` for the child bot so that subsequent cost/open_qty recomputations filter by the correct cycle ID.
  - Added `[HEDGE-CYCLE-SYNC]` log on success, ERROR log on failure.
- **engine/database.py** (`heal_zombie_bots`):
  - **Scenario 1 Guard**: Query for open/placing orders in `bot_orders` before wiping the cycle_id/step. If open orders exist, skip wiping to avoid deleting cycle information of resting orders.
- **CODEBASE_GUIDE.md**:
  - Incremented version to `3.6.1`.
  - Added invariant `3.20. Hedge Child cycle_id Sync`.
- **One-off DB recovery (2026-05-28)**:
  - sol_hedge (`100315`): trades.cycle_id updated from `1` to `48`.
  - Executed `seal_all_active_bots()` to sync trades from orders.

## v3.6.0 — 2026-05-27 — Global Flatten safety guards, forensic proof gate fix, audit fill receipts, and manual database repairs

- **engine/reconciler.py** (`resolve_net_mismatch`):
  - **Candidate Gating Check**: Filter candidate bots (`suspects`) to exclude those with status `REQUIRE_MANUAL_PROOF`, `MANUAL_GATE`, `FLATTENING`, `HEDGE_STANDBY`, or `STOPPED`, and inactive bots (`is_active = 0`). If all suspects are gated, the reconciler blocks the global flatten order, flags all candidate bots as `REQUIRE_MANUAL_PROOF`, and continues to the next pair.
  - **Forensic DNA Gate Fix**: Added missing `b4_ran = True` assignment at the end of the B.4 claimant block. This prevents the reconciler from incorrectly falling through to the Aggressive Market Flatten protocol when valid forensic DNA/TP proofs exist.
  - **Auditable Close Fill Receipts**: Captured the CCXT market order result and wrote a closing order receipt to `bot_orders` + called `credit_fill` to cleanly decrement `open_qty` before resetting the bot's virtual state, preventing post-flatten ledger corruption.
- **tests/test_reconciler_manual_gate.py**: Added integration tests `test_global_flatten_skips_gated_bots` and `test_b4_forensic_proof_prevents_flatten` to verify these safety behaviors.
- **tests/test_global_flatten_writes_bot_orders_row.py**: Added tests for verifying the flatten fill receipt creation.
- **One-off DB recovery (2026-05-27)**:
  - Recovered BTC bot `10016` (status `IN TRADE`, `open_qty = 0.006`, `total_invested = 455.097`) and ETH bot `10011` (removed stale `tp_order_id` block).

## v3.5.8 — 2026-05-26 — Canonical dedup ranking fix, consolidate post-seal expansion, one-way netting inactive-bot guard, and surgical DB repairs

- **engine/database.py** (`_BOT_ORDERS_CANONICAL_SUBSELECT`):
  - **Canonical Ranking for Canceled+Filled Rows**: `canceled`/`cancelled` rows with `filled_amount > 0` now rank equally to `filled`/`auto_closed` rows in the dedup ORDER BY. Previously, a canceled TP with a real fill (e.g., `fill=0.97` for SOL, `fill=22.4` for SUI) lost rank to an `auto_closed` zero-fill duplicate. `recompute_invested_from_orders` then selected the zero-fill canonical row, making `sold_qty=0` → `open_qty` never decremented after the TP. This was the single root cause of all SOL/SUI persistent open_qty inflation.
- **engine/database.py** (`consolidate_duplicate_bot_orders`):
  - **Remove 'filled' from status NOT IN**: Consolidator now catches groups where the WS already set one duplicate to `'filled'` while partial/open retries remain.
  - **All-row seal detection**: Seal check now inspects all rows in a consolidated group (including the keeper, which is typically the canonical filled TP row) rather than only non-keepers. Extended EXIT_TYPES to include `'forensic_adoption_reduce'`.
  - **Improved seal logging**: Single `logger.warning` reports how many bots were sealed after the commit, rather than per-bot `logger.info`.
- **engine/oneway_netting.py** (`apply_oneway_entry_cross_reduction`):
  - **Inactive-bot status guard**: Fetches `b.status` in the neighbors SQL query. `SCANNING`/`Scanning`/`REQUIRE_MANUAL_PROOF`/`STOPPED` bots are skipped. A bot with a stale `open_qty` residual that is not actively in trade would otherwise receive phantom `virtual_netting` reductions, creating a false impression that the SHORT bot's cross-reduction was consumed by an already-flat sibling.
- **One-off DB recovery (2026-05-26)**:
  - Fixed `short sui` (100000) and `short sol` (100001) MANUAL_PROOF gates: returned to `Scanning` after confirming `open_qty=0` and `total_invested=0`.
  - `sui long` (10018) `open_qty` force-corrected `580.3 → 557.9` (accumulator was stale; recompute after Fix 5 confirmed `557.9` = exchange physical).
  - `sol` (10008) `open_qty` sealed to `0.42` via `seal_trade_state` (was `1.39`; Fix 5 correctly accounted for the 0.97 canceled TP fill).

## v3.5.6 — 2026-05-26 — Drift check, OWAY_REPAIR ledger-neutral rows, and exponential grid placement backoff

- **engine/bot_executor.py**:
  - **Drift Alert Sibling Check**: Added checks for active sibling bots sharing the same pair. If active sibling bots exist, the bot-level warning drift alert is suppressed, leaving pair-level parity audits to the reconciler.
  - **Grid Placement Backoff**: Implemented exponential backoff for grid order placement. When CCXT throws a network timeout / connection error or Binance returns a 408, grid placement is deferred per-bot (delay scales from 2 to 60 seconds). Successfully placing a grid order resets backoff.

- **engine/oneway_netting.py**:
  - **Ledger-Neutral OWAY_REPAIR**: Modified `reconcile_oneway_pair_open_qty` to write `'drift_note'` rows with `amount=0.0` and `status='audit'` instead of `'virtual_netting'` exit fills. This ensures that startup alignments document physical discrepancies without faking ledger fills and corrupting subsequent recomputations.

- **config/settings.py**:
  - **Version Bump**: Incremented `VERSION` to `"3.5.6"`.

## v3.5.5 — 2026-05-25 — Reconciler CID Dedup Fix & BTC/USDC Parity Restoration

**Root cause (BTC/USDC parity mismatch, sys=-0.046 vs ex=-0.090):**
`reconstruct_offline_fills` used `OR client_order_id=?` in its CID lookup query **without** a cycle restriction. A historical `virtual_netting` row (`CQB_10022_OWAY_REPAIR_1779752456`, filled_amount=0.044) inserted by an earlier OWAY_REPAIR path was matched across cycles and caused `sync_trades_from_orders` to compute `open_qty=0.046` instead of the correct `0.090`. Additionally, two `UPDATE bot_orders` statements used `OR client_order_id=?` which could corrupt sibling rows that happened to share the same CID prefix.

- **engine/reconciler.py** (`reconstruct_offline_fills`):
  - **CID Cycle Guard**: CID lookup now uses `(order_id=? OR (client_order_id=? AND cycle_id=?))` so only rows from the bot's current cycle are matched. Historical rows from prior cycles with the same CID prefix are ignored.
  - **New-Physical-Order Guard**: If the DB row matched by CID has a different real exchange `order_id` from the fill being processed, the match is treated as a brand-new physical order (`row=None`) instead of being silently skipped.
  - **Surgical UPDATEs (×2)**: The two `UPDATE bot_orders SET status='filled'` statements that previously used `OR client_order_id=?` have been narrowed to `WHERE order_id=?` only, preventing collateral mutation of sibling rows.

- **engine/runner.py**:
  - **Sync Barrier**: `sync_trades_from_orders` runs exactly once for all active bots at startup before any exchange parity audit begins. Barrier polls for count stability (up to 10 s, 1 s sleep) and logs a WARNING then proceeds if not stabilized—preventing indefinite startup blocks.

- **One-off DB recovery (2026-05-25)**:
  - Bot 10022 (`short btc`): Deleted erroneous `virtual_netting` row id=103359 (`CQB_10022_OWAY_REPAIR_1779752456`, filled_amount=0.044). Called `sync_trades_from_orders(10022)`. `trades.open_qty` corrected 0.046 → **0.090** (matches exchange). Bot reset `REQUIRE_MANUAL_PROOF` → `IN TRADE`.

## v3.5.4 — 2026-05-25 — Consolidated Core Stability Fixes

Consolidated stability fixes across NameError handling, WS warmup startup timing, oneway opposite gate, and an adoption circuit breaker.

- **Fix 1 — `phys_net_signed` NameError (`engine/bot_executor.py`):**
  - Replaced undefined reference `phys_net_signed` with `phys_net_qty` in the drift-alert logic within `maintain_orders`.

- **Fix 2 — WS Warmup Timing (`engine/runner.py`):**
  - Increased `WS_WARMUP_SECONDS` from 8 to 20 to prevent startup race conditions where reconciliation starts before WS cache trades sync finishes.

- **Fix 3 — Oneway Opposite Gate Bypass (`engine/bot_executor.py`):**
  - Skip the `gate_oneway_opposite_entry` check during grid maintenance if the bot is already in-trade (`total_invested > 0.01` and `current_step > 0`). The gate now only blocks new entries for scanning bots.

- **Fix 4 — Adoption Circuit Breaker (`engine/reconciler.py`):**
  - Track cumulative quantity adopted per bot per pass inside `adopt_from_physical_positions`.
  - Abort adoption, log an `ERROR`, and set status to `REQUIRE_MANUAL_PROOF` if the cumulative quantity exceeds the `MAX_ADOPTION_QTY_PER_CYCLE` limit (default `0.5`, configurable via `.env`).

- **Config — `config/settings.py`:**
  - Added `MAX_ADOPTION_QTY_PER_CYCLE` to config settings, reading from `.env` (defaulting to `0.5`).

## v3.5.3 — 2026-05-22 — DNA-WIPE client order ID deadlock

**Root cause:** When no ledger fills were found, the `DNA-WIPE` protocol reset the bot's trade stats and phase to `IDLE`/`Scanning`, but left stale `entry_order_id` / `tp_order_id` in the `trades` table and failed to increment the `cycle_id`. Consequently, the bot remained in the same cycle and when placing a new entry order, used the same `client_order_id` as the previous cycle, which triggered `DEDUP-GUARD` and locked the bot in a permanent `🟢 SCANNING` state.

- **Fix — `engine/database.py`:**
  - Updated the `DNA-WIPE` UPDATE query to set `entry_order_id = NULL`, `tp_order_id = NULL`, `open_qty = 0`, and increment `cycle_id = COALESCE(cycle_id, 1) + 1`.
- **Verified:**
  - Ran the database healing script to resolve active bot deadlocks (XRP, LINK, SUI).
  - Confirmed the bots resumed correct behavior (XRP long successfully placed its entry order, and LINK/SUI short are scanning correctly).

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
