# Filled Integrity Fix Design Doc

**Date:** 2026-08-28  
**Incident:** LINK cascade — two `LIVE_GUARD` rows marked `status=filled` with `filled_at=0` and no corresponding exchange fill event.

---

## Root Cause

### What Happened
During the hedge-live-guard cascade (14:08–14:13 UTC), the engine inserted `LIVE_GUARD` phantom rows as `status=entry` (open). Later in the same cascade, two of these rows were **internally updated to `status=filled`** with:
- `filled_amount = 68.12` and `103.16`
- `filled_at = 0` (zero = no timestamp)
- **No corresponding entry in `fetch_my_trades()`** — zero real exchange fills during this window

### Code Path That Allowed This
The `filled` status transition happened in `engine/bot_executor.py` hedge-live-guard logic when it "corrected" the child's `open_qty` to match the exchange hedge qty. The logic:
1. Computes `delta = exchange_hedge_qty - child_step_qty`
2. If delta ≈ 0 (within tolerance), marks the `LIVE_GUARD` row as `filled` to indicate "hedge is already covered"
3. **Bug:** This is a **logical inference**, not a real fill — but it writes `status=filled` without exchange verification

### Why It's an Integrity Violation
- `status=filled` in `bot_orders` **must mean** "exchange confirmed this order filled"
- `filled_at` **must be** a real Unix timestamp from the exchange
- `filled_amount` **must match** the exchange fill amount
- **Current code allows internal logic to fabricate fills** — breaks ledger trust

---

## Current Code Locations

### Primary: `engine/bot_executor.py` — Hedge-live-guard section
```python
# Approximate lines 2920-2930
if adjusted_delta <= tolerance:
    # "Hedge already covered" - but this is a LOGICAL INFERENCE, not a real fill
    cursor.execute("""
        UPDATE bot_orders SET status='filled', filled_amount=?
        WHERE client_order_id=?
    """, (live_hedge_qty, live_guard_cid))
```

### Secondary: Any path calling `save_bot_order` with `status='filled'`
Need to audit all callers.

---

## Proposed Fix: Enforced Fill Verification

### Core Principle
> **No row reaches `status=filled` without a corresponding exchange fill event.**

### Implementation Options

#### Option A: Application-Level Guard (Recommended)
Add a verification step in `save_bot_order` (or a new `mark_order_filled` wrapper) that **requires**:
1. `exchange_order_id` or `exchange_fill_id` provided
2. `filled_at` > 0 (real timestamp)
3. Optional: cross-check against `fetch_my_trades()` if `TESTING_MODE=False`

```python
# In engine/database.py or new module
def mark_order_filled(
    bot_id: int,
    client_order_id: str,
    filled_amount: float,
    filled_at: int,
    exchange_fill_id: str,  # REQUIRED
    exchange_order_id: str = None,
    ...
):
    """Mark order filled ONLY with exchange verification."""
    assert filled_at > 0, "filled_at must be > 0 (real timestamp)"
    assert exchange_fill_id, "exchange_fill_id required for filled status"
    
    # Optional: verify against exchange in non-test mode
    if not config.TESTING_MODE:
        fills = exchange.fetch_my_trades(...)
        assert any(f['id'] == exchange_fill_id for f in fills), \
            "Exchange fill not found for claimed fill"
    
    save_bot_order(..., status='filled', filled_at=filled_at, ...)
```

**Pros:** Centralized, enforceable, clear contract
**Cons:** Requires refactoring all `filled` call sites

---

#### Option B: Database Constraint + Trigger (Defense in Depth)
Add a SQLite CHECK constraint (if supported) or trigger that validates `filled_at > 0` when `status='filled'`.

```sql
-- Not directly enforceable in SQLite for cross-table checks,
-- but can add application-level CHECK:
-- (status != 'filled' OR filled_at > 0)
```

**Pros:** Catches bugs at DB level
**Cons:** SQLite CHECK constraints limited; can't verify exchange fill existence

---

#### Option C: Fill Claim Registry (Architectural)
Introduce a `fill_claims` table that **must** be populated by exchange-verified fill events before any `bot_order` can be marked `filled`.

```python
# Flow:
# 1. Exchange fill detected -> insert into fill_claims (exchange_fill_id, bot_id, client_order_id, amount, ts)
# 2. Bot logic wants to mark order filled -> must JOIN fill_claims
# 3. No fill_claim -> cannot mark filled
```

**Pros:** Complete audit trail, prevents all fabricated fills
**Cons:** Larger refactor, schema change

---

## Recommendation: Option A + Phased Rollout

**Phase 1 (Immediate):** Add `mark_order_filled()` wrapper with `exchange_fill_id` requirement. Audit all existing `filled` writes.

**Phase 2 (Soon):** Add `fill_claims` table and require join for any `filled` transition.

**Phase 3 (Future):** DB-level constraints, automated reconciliation of `bot_orders` vs `fill_claims`.

---

## Audit: All Current `status='filled'` Write Paths

| File/Function | Context | Has Exchange Fill? | Risk |
|---------------|---------|-------------------|------|
| `bot_executor.py` hedge-live-guard | Logical inference | ❌ NO | **HIGH** (today's bug) |
| `bot_executor.py` order placement flow | Real exchange fill callback | ✅ Yes | Low |
| `reconciler.py` offline-sync | CID-matched exchange fills | ✅ Yes | Low |
| `database.py` `_reset_bot_after_tp_internal` | TP hit, real fill | ✅ Yes | Low |
| `database.py` `sync_trades_from_orders` | Recompute only | N/A | N/A |
| `recovery.py` safe_wipe | Manual close | ⚠️ Conditional | Medium |
| `parity_gates.py` phantom purge | `ghost_order_cancel` audit rows | ⚠️ Fabricated | Medium |

**Critical finding:** `ghost_order_cancel` in `parity_gates.py:756` writes `status='filled'` for an audit row that **never existed on exchange**. This is the same pattern.

---

## Test Plan (Using LINK Cascade + New Fixtures)

### Test Fixture: `tests/fixtures/filled_integrity_link_cascade.json`
```json
{
  "scenario": "LINK cascade 2026-08-28",
  "hedge_live_guard_runs": 12,
  "phantom_rows_inserted": 5,
  "rows_marked_filled_by_logic": 2,
  "real_exchange_fills_during_window": 0,
  "expected_final_filled_count": 0
}
```

### Test Cases

| Test | Description | Expected |
|------|-------------|----------|
| `test_hedge_live_guard_no_fabricated_fills` | Replay LINK cascade | 0 rows `status=filled` from hedge-live-guard |
| `test_mark_filled_requires_exchange_fill` | Call `mark_order_filled()` without `exchange_fill_id` | Raises AssertionError |
| `test_ghost_order_cancel_not_filled` | `ghost_order_cancel` audit row | Must use `status='cancelled'` or new `status='ghost_cancelled'`, NOT `filled` |
| `test_real_fill_still_works` | Normal TP fill flow | Passes, `filled` written correctly |
| `test_offline_sync_preserves_fills` | CID-matched fills from exchange | Passes, real fills marked correctly |

### Test Implementation
```python
# tests/test_filled_integrity.py
def test_no_filled_without_exchange_fill():
    """Core invariant: status=filled => exchange fill exists."""
    conn = get_test_db()
    
    # Try to mark filled without exchange fill - should fail
    with pytest.raises(AssertionError, match="exchange_fill_id required"):
        mark_order_filled(
            bot_id=100320,
            client_order_id="TEST_PHANTOM",
            filled_amount=100,
            filled_at=0,  # Invalid
            exchange_fill_id=None  # Missing
        )

def test_link_cascade_no_fabricated_fills():
    """Replay LINK cascade with fix — verify zero fabricated fills."""
    with patch_fetch_positions(link_cascade_sequence):
        with patch("engine.exchange_interface.ExchangeInterface.fetch_my_trades", return_value=[]):
            run_hedge_live_guard_cycles(12)
            
            # Verify
            filled_phantoms = count_rows(
                "bot_orders", 
                bot_id=100320, 
                client_order_id_like="%LIVE_GUARD%",
                status="filled"
            )
            assert filled_phantoms == 0, f"Found {filled_phantoms} fabricated fills"

def test_ghost_order_cancel_uses_correct_status():
    """ghost_order_cancel must not use status=filled."""
    purge_phantom_ledger(...)
    
    audit_row = get_ghost_cancel_audit_row(bot_id=10020)
    assert audit_row['status'] != 'filled', "ghost cancel used filled status"
    # Should be 'cancelled' or new 'ghost_cancelled'
```

---

## Acceptance Criteria

1. **LINK cascade replay** → 0 fabricated `filled` rows
2. **All existing fill paths** (TP, offline-sync, manual close) → still work, pass tests
3. **`ghost_order_cancel`** → uses non-`filled` status (e.g., `ghost_cancelled`)
4. **Any code path** writing `status='filled'` → goes through `mark_order_filled()` with verification
5. **CI test** enforces: `grep -r "status.*filled" --include="*.py" engine/` shows only approved paths

---

## Implementation Checklist

- [ ] Add `mark_order_filled()` with `exchange_fill_id` requirement
- [ ] Refactor hedge-live-guard to use `mark_order_filled` (will fail without real fill → correct behavior)
- [ ] Refactor `ghost_order_cancel` to use `status='ghost_cancelled'` (new status)
- [ ] Audit all `save_bot_order(..., status='filled', ...)` call sites
- [ ] Add tests above
- [ ] Run full test suite
- [ ] Verify LINK cascade fixture passes