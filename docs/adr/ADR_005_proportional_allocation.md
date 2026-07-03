# ADR-005: Proportional Allocation — Replace Virtual Netting

**Status:** CANCELLED — incompatible with simultaneous opposite-direction bot pairs in One-Way mode
**Version:** v4.1.5
**Date:** 2026-06-23
**Authors:** Antigravity AI + operator
**Supersedes:** Virtual netting layer introduced in v2.0.0 (DEBT-001)
**Addresses:**
- ETH $45 orphan — stale TP size calculated from netted qty (INV-29 forensic report)
- SOL 0.04 drift — CID collision causing silent overwrite of `filled_amount` (v4.1.5 forensic report)

---

## 1. Current State — How Virtual Netting Works Today

### 1.1 The Problem Virtual Netting Solved

In Binance One-Way Mode, the exchange maintains a single net position per symbol. When a SHORT bot sells 0.10 BTC, the exchange reduces the net LONG position by 0.10 BTC regardless of what the LONG bot''s ledger says. The LONG bot''s `trades.open_qty` does not automatically decrease — it still claims 0.10 BTC it no longer physically holds.

This gap between virtual (per-bot DB ledger) and physical (single exchange net) is the structural problem. Virtual netting was the v2.0.0 solution.

### 1.2 The `apply_oneway_entry_cross_reduction` Flow

Called from `engine/ledger.py` `credit_fill()` (line 382) immediately after an entry/grid fill is credited to the filling bot:

```
credit_fill(bot_id, fill_qty, ...)
  └── ENTRY accumulation for filling bot (open_qty += delta)
  └── apply_oneway_entry_cross_reduction(
          filling_bot_id, pair, direction, delta, source_order_id, avg_price, exchange
      )
        └── wrapped via WriteQueue.put_and_wait (INV-31)
            └── _apply_oneway_entry_cross_reduction_internal(...)
```

**Internal logic of `_apply_oneway_entry_cross_reduction_internal`:**

1. **Neighbor scan** — Query `bots JOIN trades` for all active non-hedge-child bots on the same pair with opposite direction and `open_qty > 0`. Skip bots with status `scanning`, `stopped`, `hedge_standby`.
2. **Recency check** — Skip any neighbor whose most recent entry fill was within the last 30 seconds.
3. **Claim insert** — `INSERT OR IGNORE INTO cross_reduction_claims (source_order_id, target_bot_id, ...)`. If already exists, skip (idempotency guard, INV-21).
4. **Write two `virtual_netting` rows per reduced neighbor:**
   - **Target-side row** (on `nb_id`): EXIT-type in `get_pair_virtual_net`; decrements the neighbor''s virtual net.
   - **Source-side row** (on `filling_bot_id`): EXIT-type; signals this portion of the fill netted rather than adding new exposure.
5. **Seal both bots** — `seal_trade_state(nb_id, force_recompute=True)`.
6. **INV-28A** — Cancel stale TP orders on reduced neighbors.
7. **INV-28B** — If filling bot''s virtual `open_qty` reaches zero but exchange shows position, flag `bots.status = ''pending_flatten''`.

### 1.3 What `virtual_netting` Rows Contain

| Column | Value |
|:---|:---|
| `bot_id` | Bot whose `open_qty` is being reduced |
| `order_type` | `''virtual_netting''` |
| `order_id` | `VN_{bot_id}_{source_order_id}_{timestamp}` |
| `client_order_id` | `CQB_{bot_id}_VNET_{source_order_id}_{timestamp}` |
| `price` | avg fill price of the triggering entry |
| `amount` / `filled_amount` | qty reduced (both set to the same value) |
| `status` | `''filled''` |
| `cycle_id` | current cycle of the bot being reduced |

These rows appear in `get_pair_virtual_net` under the EXIT bucket (`order_type IN (''adoption_reduce'',''tp'',''close'',''dust_close'',''sl'',''virtual_netting'')`), reducing the bot''s virtual net.

### 1.4 What `cross_reduction_claims` Enforces

Deduplication guard (migration 004). UNIQUE constraint on `(source_order_id, target_bot_id)`. Prevents the same exchange order from generating duplicate `virtual_netting` rows for the same target bot. Uses `INSERT OR IGNORE`.

**Known limitation:** The guard correctly prevents duplicate top-level calls per `(source_order_id, target_bot_id)`. It does NOT protect the CID generation inside the function — the CID still appends `int(time.time())`, making same-second CID collisions possible for rapid partial fills.

### 1.5 Why Both Incident Types Are Structural, Not Accidental

#### ETH $45 Orphan (stale TP size from netted qty)

`get_pair_virtual_net()` sums `bought_qty - sold_qty` from `bot_orders`, including `virtual_netting` EXIT rows. If those rows are incomplete, written in a different cycle, or rolled back by a crash, TP sizing uses a corrupt basis. **No exchange-verified fallback exists.**

#### SOL 0.04 Drift (CID collision — silent `filled_amount` overwrite)

Two partial fills arriving in the same second produce identical CIDs (`CQB_{bot_id}_VNET_SRC_{nb_id}_1750000000`). `save_bot_order()` sees the same CID and silently overwrites `filled_amount`. The first partial fill''s netting quantity is permanently lost.

**Root cause of both:** The virtual netting system maintains a synthetic ledger that must stay in perfect sync with exchange fills but has no reliable ground truth to fall back to when it drifts.

---

## 2. Proposed Replacement — Proportional Allocation Model

### 2.1 Core Idea

Replace the push model (write synthetic rows on every fill) with a pull model (ask the exchange for the authoritative net every reconciler cycle, then distribute it proportionally among standard bots by their `total_invested` capital weight).

```
bot.open_qty = (pair_exchange_net - hedge_child_net) × (bot.total_invested / sum_standard_bots.total_invested)
```

No `virtual_netting` rows are written. No `cross_reduction_claims` entries are made. `open_qty` is set directly from the exchange-verified net.

### 2.2 Resolved Design Decisions

| Question | Decision |
|:---|:---|
| **Q1 — Sync frequency** | Reconciler cycle (~60s). `sync_pair_to_exchange()` runs inside `_reconcile_all_internal()` (reconciler.py line 6141), which already iterates over all active pairs. No per-loop call; no extra API calls above current volume. |
| **Q2 — Weight basis** | `trades.total_invested` confirmed. Reflects actual capital deployed; stable between fills. |
| **Q3 — Hedge child exclusion** | Hedge children excluded from proportional allocation. Exchange net used for distribution = `pair_exchange_net - sum(hedge_child.open_qty)` for all active hedge children on the pair. Hedge children continue to be managed by their own `bot_orders` fill history (ADR-004). |
| **Q4 — `cross_reduction_claims` retention** | Retain permanently as audit log. Add a `deprecated_at` INTEGER column (unix timestamp) set once on the first reconciler cycle that runs with `PROPORTIONAL_ALLOCATION=True`. No new rows written after that point. |
| **Q5 — `virtual_netting` row handling** | Mark existing rows `status=''legacy_netting''` via migration 006 (startup, one-time). Do not delete. `legacy_netting` joins `reset_cleared` in the inert set — excluded from all ENTRY and EXIT buckets. Audit trail preserved. |
| **Q6 — Deployment scope** | **Staged (two stages):** Stage A deploys behind `PROPORTIONAL_ALLOCATION=False`, runs both systems in parallel logging for 48 hours. Stage B (after 48-hour validation) flips flag, then after a further 48 stable hours deletes `apply_oneway_entry_cross_reduction` and the `cross_reduction_claims` write path. |

### 2.3 The Updated `sync_pair_to_exchange()` Function (Stage A — Write Mode)

`sync_pair_to_exchange()` already exists in `engine/oneway_netting.py` (line 770) as observation-only. Under Phase 3 it becomes write-enabled when `PROPORTIONAL_ALLOCATION=True`:

```python
def sync_pair_to_exchange(pair: str, exchange, conn) -> Optional[dict]:
    from engine.parity_gates import get_exchange_signed_net, qty_tolerance
    from engine.exchange_interface import normalize_symbol
    from config.settings import config

    norm_pair = normalize_symbol(pair).upper()
    exchange_net = get_exchange_signed_net(exchange, pair)
    if exchange_net is None:
        logger.warning(f"[PA-SYNC] API unavailable for {pair} — keeping existing open_qty")
        return None

    # Q3: Subtract hedge child contributions
    rows = conn.execute("""
        SELECT b.id, b.direction, b.bot_type, COALESCE(t.total_invested, 0), COALESCE(t.open_qty, 0)
        FROM bots b JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.normalized_pair = ?
    """, (norm_pair,)).fetchall()

    hedge_net = 0.0
    standard_rows = []
    for bot_id, direction, bot_type, invested, open_qty in rows:
        if bot_type == ''hedge_child'':
            oq = float(open_qty or 0)
            hedge_net += oq if direction.upper() == ''LONG'' else -oq
        else:
            standard_rows.append((bot_id, direction, float(invested)))

    distributable_net = round(exchange_net - hedge_net, 8)
    total_invested = sum(abs(inv) for _, _, inv in standard_rows)

    if total_invested < 0.01:
        # All standard bots flat
        for bot_id, _, _ in standard_rows:
            conn.execute("UPDATE trades SET open_qty = 0 WHERE bot_id = ?", (bot_id,))
        conn.commit()
        return {''pair'': pair, ''exchange_net'': exchange_net, ''note'': ''all_flat''}

    # Proportional allocation
    abs_net = abs(distributable_net)
    allocations = []
    for bot_id, direction, invested in sorted(standard_rows, key=lambda r: -abs(r[2])):
        weight = abs(invested) / total_invested
        if direction.upper() == ''LONG'':
            alloc = round(max(0.0, distributable_net) * weight, 8)
        else:
            alloc = round(max(0.0, -distributable_net) * weight, 8)
        allocations.append((bot_id, alloc))

    # Residual to largest bot
    alloc_sum = round(sum(a for _, a in allocations), 8)
    residual = round(abs_net - alloc_sum, 8)
    if abs(residual) > 1e-10 and allocations:
        allocations[0] = (allocations[0][0], round(allocations[0][1] + residual, 8))

    if config.PROPORTIONAL_ALLOCATION:
        for bot_id, alloc in allocations:
            conn.execute("UPDATE trades SET open_qty = ROUND(?, 8) WHERE bot_id = ?", (alloc, bot_id))
        conn.commit()

    # Always log PA result for parallel comparison (Stage A: even when flag is False)
    logger.info(
        f"[PA-SYNC] {pair}: exchange_net={exchange_net:.6f}, hedge_net={hedge_net:.6f}, "
        f"distributable={distributable_net:.6f}, allocations={allocations}, "
        f"applied={config.PROPORTIONAL_ALLOCATION}"
    )
    return {''pair'': pair, ''exchange_net'': exchange_net, ''distributable'': distributable_net, ''allocations'': allocations}
```

**Stage A parallel logging:** When `PROPORTIONAL_ALLOCATION=False`, the function computes the proportional allocation and logs the result but does NOT write `open_qty`. This produces 48 hours of log lines comparing what PA would set vs what virtual netting actually set, enabling manual validation before flipping the flag.

### 2.4 What `get_pair_virtual_net` Becomes

Under Phase 3 the function is simplified. `open_qty` is now the authoritative source:

```python
def get_pair_virtual_net(symbol: str) -> float:
    """Under PROPORTIONAL_ALLOCATION=True: sum of trades.open_qty (set by sync_pair_to_exchange).
    Under PROPORTIONAL_ALLOCATION=False: original order-history aggregation (unchanged)."""
    from config.settings import config
    if not config.PROPORTIONAL_ALLOCATION:
        # ... existing 90-line implementation unchanged ...
        pass
    conn = get_connection()
    norm = normalize_symbol(symbol).upper()
    rows = conn.execute("""
        SELECT b.direction, COALESCE(t.open_qty, 0)
        FROM bots b JOIN trades t ON t.bot_id = b.id
        WHERE b.is_active = 1 AND b.normalized_pair = ?
          AND b.bot_type != ''hedge_child''
    """, (norm,)).fetchall()
    total = 0.0
    for direction, oq in rows:
        total += float(oq) if direction.upper() == ''LONG'' else -float(oq)
    return round(total, 8)
```

Note: The existing implementation is preserved untouched behind the `if not config.PROPORTIONAL_ALLOCATION` branch. Only when the flag is `True` does the simplified path execute. This means the flag rollback restores the old implementation with zero code risk.

---

## 3. Migration Path

### 3.1 Feature Flag

```python
# config/settings.py — add to __init__
self.PROPORTIONAL_ALLOCATION = os.getenv("PROPORTIONAL_ALLOCATION", "False").lower() == "true"
self.PA_SYNC_MAX_STALE_CYCLES = int(os.getenv("PA_SYNC_MAX_STALE_CYCLES", "5"))
```

- `False` (default / Stage A): Virtual netting runs unchanged. PA logic computes and logs but does not write.
- `True` (Stage B): PA writes `open_qty` on every reconciler cycle. `credit_fill` cross-reduction call is skipped.

### 3.2 Stage A — Parallel Mode (48 hours before flag flip)

1. Deploy with `PROPORTIONAL_ALLOCATION=False`.
2. Migration 006 runs at startup (idempotent): marks existing `virtual_netting` rows as `legacy_netting`; adds `deprecated_at` column to `cross_reduction_claims`.
3. `sync_pair_to_exchange()` computes PA allocation on every reconciler cycle and logs `[PA-SYNC]` lines with `applied=False`.
4. Virtual netting continues writing `virtual_netting` rows as before.
5. Operator compares `[PA-SYNC]` log lines against actual `open_qty` in the DB for 48 hours.

### 3.3 Stage B — Flag Flip (after 48-hour validation)

1. Set `PROPORTIONAL_ALLOCATION=True` in `.env`, restart engine.
2. On first reconciler cycle: `sync_pair_to_exchange()` writes `open_qty` proportionally; logs `applied=True`.
3. `credit_fill()` skips the cross-reduction block (guarded by `if not config.PROPORTIONAL_ALLOCATION`).
4. Monitor for 48 hours. If stable, proceed to Stage B cleanup.

### 3.4 Stage B Cleanup (after 48-hour stable run)

Delete (separate commit, separate approval):
- `_apply_oneway_entry_cross_reduction_internal()` and `apply_oneway_entry_cross_reduction()` from `oneway_netting.py`
- Cross-reduction call block from `ledger.py` `credit_fill()`
- `cross_reduction_claims` INSERT logic

**This cleanup requires a separate approval. Do not delete anything until the operator explicitly approves Stage B cleanup.**

### 3.5 Rollback Path

Set `PROPORTIONAL_ALLOCATION=False`, restart. Instant. No DB repair needed. `legacy_netting` rows remain inert. Virtual netting resumes on next `credit_fill`. No manual intervention required.

---

## 4. Risk Assessment

### 4.1 Exchange API Down

`get_exchange_signed_net()` returns `None` → `sync_pair_to_exchange()` returns without modifying `open_qty`. Bots continue on previous values. Identical behaviour to current system under API failure.

After `PA_SYNC_MAX_STALE_CYCLES` consecutive failures: set all bots on the pair to `REQUIRE_MANUAL_PROOF`.

### 4.2 `total_invested` Zero Guard

All standard bots flat → `total_invested < 0.01` → set all `open_qty = 0`, no division-by-zero.

### 4.3 Stale `total_invested` at Sync Time

`sync_pair_to_exchange()` runs inside the reconciler cycle, which runs `_align_memory_to_ledger()` (reconciler line 6025) before the sync call. This ensures `total_invested` is current from `recompute_invested_from_orders()` before weights are calculated.

### 4.4 Hedge Child Isolation

Exchange net attributable to standard bots = `pair_exchange_net - sum(hedge_child.open_qty)`. Hedge children use `seal_trade_state()` / `recompute_invested_from_orders()` unchanged (ADR-004). No change to hedge child lifecycle.

### 4.5 Floating-Point Residual

Maximum residual from N bots ≈ N × 1e-8. Assigned to largest-weight bot. Sum of allocated `open_qty` equals `abs(distributable_net)` within 1e-8.

---

## 5. Impact on Dependent Systems

### 5.1 Functions That Write `virtual_netting` Rows

| File | Function | Stage A | Stage B |
|:---|:---|:---|:---|
| `engine/oneway_netting.py` | `_apply_oneway_entry_cross_reduction_internal` | Unchanged | **Delete** |
| `engine/oneway_netting.py` | `apply_oneway_entry_cross_reduction` (wrapper) | Unchanged | **Delete** |
| `engine/oneway_netting.py` | `sync_pair_to_exchange` | **Extended: PA logic + parallel logging** | Same |
| `engine/reconciler.py` | `reconcile_oneway_pair_open_qty` calls | Unchanged | Replace with `sync_pair_to_exchange()` |

### 5.2 `credit_fill()` Cross-Reduction Call

| File | Change | Stage |
|:---|:---|:---|
| `engine/ledger.py` lines 376–395 | Add `if not config.PROPORTIONAL_ALLOCATION:` guard around the `apply_oneway_entry_cross_reduction` call | **Stage A** |

### 5.3 `get_pair_virtual_net()` — Dual-Path Implementation

| File | Change | Stage |
|:---|:---|:---|
| `engine/database.py` lines 3839–3982 | Add `if config.PROPORTIONAL_ALLOCATION:` fast-path at top; preserve existing implementation as else-branch | **Stage A** |

### 5.4 `virtual_netting` Exclusions That Become Inert

When `PROPORTIONAL_ALLOCATION=True`, `virtual_netting` rows are `legacy_netting` (status-migrated at startup). However, the exclusion guards below must also be updated to include `legacy_netting` in their inert sets:

| File | Location | Change |
|:---|:---|:---|
| `engine/parity_gates.py` | Line 101: `CYCLE_RESET_CARRY_LABELS` exit_types set | Add `''legacy_netting''` |
| `engine/bot_executor.py` | Line 101: `AND order_type != ''virtual_netting''` | Add `AND order_type NOT IN (''virtual_netting'', ''legacy_netting'')` |
| `engine/database.py` | Lines 183, 221: NOT IN status lists | Add `''legacy_netting''` to inert status sets |
| `engine/database.py` | Line 3923: EXIT bucket in `get_pair_virtual_net` order-history path | Remove `''virtual_netting''` (legacy_netting already excluded by status filter) |
| `engine/database.py` | Line 3686: EXIT types in `recompute_invested_from_orders` | Remove `''virtual_netting''` |
| `engine/database.py` | Line 3509: exclusion filter in `audit_pair_ledger_vs_exchange` | Remove `!= ''virtual_netting''` |

### 5.5 `cross_reduction_claims` Table

| Component | Change |
|:---|:---|
| `migration_006_pa_legacy_netting.py` | Add `deprecated_at` INTEGER column to `cross_reduction_claims` |
| `engine/oneway_netting.py` INSERT OR IGNORE call | Guarded by `if not config.PROPORTIONAL_ALLOCATION` in Stage A; deleted in Stage B |
| `CODEBASE_GUIDE.md` INV-21 | Updated to "DEPRECATED — superseded by ADR-005 Phase 3" |

### 5.6 Callers of `get_pair_virtual_net` — No Change Required

All 9 call sites in `parity_gates.py`, `reconciler.py`, `database.py`, `wipe_proof.py`, `bot_executor.py`, `integrity.py`, `monitor.py` are unchanged. The function contract is preserved.

### 5.7 Test Files

| File | Stage A Change | Stage B Change |
|:---|:---|:---|
| `tests/test_oneway_netting.py` | Add `test_pa_*` tests (new file or section) | Replace all `apply_oneway_*` call tests |
| `tests/test_inv28.py` | Add Stage A flag-guard tests | Rewrite for PA-absorbed INV-28 equivalents |
| `tests/test_v3922_fixes.py` | Add `legacy_netting` migration test | After Stage B: replace `virtual_netting` row assertions |
| `tests/test_v3912_cross_reduction.py` | No change (still valid during Stage A) | Delete and replace |
| `tests/test_hedge_lifecycle.py` | No change (cross-reduction still active) | Update lines 84–116 |
| `tests/test_ledger_integrity.py` | Add dual-path test for `get_pair_virtual_net` | Remove EXIT-bucket assertions |
| `tests/test_write_queue.py` | No change (wrapped function still exists) | Update line 103 |

---

## 6. Test Strategy

### 6.1 Stage A Tests (added immediately, all must pass before deploy)

**`test_pa_parallel_log_no_write`**
Setup: `PROPORTIONAL_ALLOCATION=False`. Call `sync_pair_to_exchange()`. Assert: `trades.open_qty` unchanged. Assert: `[PA-SYNC]` log line emitted with `applied=False`.

**`test_pa_simple_long_short_pair`**
Setup: `PROPORTIONAL_ALLOCATION=True`. LONG bot $6,000 invested, SHORT bot $3,000 invested. Exchange net +0.05.
Expected: LONG `open_qty` ≈ 0.0333; SHORT `open_qty` ≈ 0.0167. Assert: `get_pair_virtual_net()` = +0.05.

**`test_pa_hedge_child_subtracted_from_exchange_net`**
Setup: Exchange net +0.13. Hedge child (LONG) holds `open_qty` = 0.03. Standard LONG bot, $5,000 invested.
Distributable = +0.10. Expected: standard LONG bot `open_qty` = 0.10.

**`test_pa_api_down_preserves_open_qty`**
`get_exchange_signed_net()` returns `None`. Assert: no `open_qty` changed.

**`test_pa_total_invested_zero_sets_all_to_zero`**
All `total_invested` = 0. Exchange net = 0.00001. Assert: all `open_qty` = 0. No ZeroDivisionError.

**`test_pa_residual_assigned_to_largest_bot`**
Three bots with weights producing FP residual. Assert: `sum(open_qty)` == `abs(exchange_net)` within 1e-8.

**`test_pa_stale_cycles_triggers_manual_proof`**
`get_exchange_signed_net()` returns `None` for `PA_SYNC_MAX_STALE_CYCLES` consecutive calls. Assert: all bots on pair set to `REQUIRE_MANUAL_PROOF`.

**`test_pa_legacy_netting_migration_marks_rows_inert`**
Insert 3 `virtual_netting` rows with `status=''filled''`. Run migration 006. Assert: all rows have `status=''legacy_netting''`. Assert: `get_pair_virtual_net()` value unchanged (rows excluded from EXIT bucket).

**`test_pa_credit_fill_skips_cross_reduction_when_flag_true`**
`PROPORTIONAL_ALLOCATION=True`. Call `credit_fill()` with an entry fill. Assert: `apply_oneway_entry_cross_reduction` is NOT called. Assert: no `virtual_netting` rows written.

**`test_pa_rollback_resumes_virtual_netting`**
Flip `PROPORTIONAL_ALLOCATION` True → False. Call `credit_fill()`. Assert: `apply_oneway_entry_cross_reduction` IS called.

### 6.2 Stage B Tests (added after cleanup, replacing deleted tests)

- Replace all `apply_oneway_entry_cross_reduction` call tests with `sync_pair_to_exchange` equivalents.
- Replace CID-collision tests with "CID collision impossible" structural tests.
- Replace INV-28A/28B tests with PA-absorbed equivalents (stale TP cancellation now implicit in re-sync).

### 6.3 Expected Test Count

| Milestone | Count |
|:---|:---|
| Current (v4.1.5) | 301 |
| After Stage A (new tests added) | **311** (301 + 10 new PA tests) |
| After Stage B cleanup | **≥ 311** (net-neutral: deleted tests replaced 1-for-1) |

---

## 7. Document Version

**ADR-005 v2.0** — Q1–Q6 resolved 2026-06-23. Approved for Stage A implementation.

Upon Stage A completion: CODEBASE_GUIDE.md bumped to v4.2.0.
Upon Stage B completion: DEBT-001 status → "Resolved (Phase 3 complete)". INV-21 → "DEPRECATED".
