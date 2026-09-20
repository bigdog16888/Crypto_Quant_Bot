# Track C Scope — Verification of Remaining P1/P2 Anomalies (Items 3, 6, 8)

## Backlog Items from PROJECT_STATUS.md:104
| # | Anomaly | Classification | Status |
|---|---------|----------------|--------|
| 1 | XAU ORDER-SYNC loop | P1 | OPEN (Item 1 analysis) |
| 2 | Stale-cycle_id dedup wedge | P1 | OPEN (Item 2 analysis) |
| 3 | INV30 double-count | P1 | **Verify only** |
| 4 | Hedge-child is_active check | P1 | ✅ RESOLVED (commit 2b94ecb) |
| 5 | audit_bot_wipes signature | P1 | OPEN (Item 5 analysis) |
| 6 | GTR lock display | P2 | **Verify only** |
| 7 | Retry-queue false alarm | P2 | OPEN (Item 7 analysis) |
| 8 | Flatten price=0.0 | P2 | **Verify only** |

---

## Item 3: INV30 Double-Count — Verification

### What It Is
INV-30 is the hedge child step saturation guard (ledger.py:467-524). When multiple orders exist for the same step/cycle (GTX chase retries), it prevents double-counting by marking later orders `auto_closed`.

### Current Implementation (ledger.py:488-542)
```python
already_credited = conn.execute(
    "SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders "
    "WHERE bot_id = ? AND step = ? AND cycle_id = ? "
    "AND order_type IN ('entry','grid','adoption_add','adoption','forensic_adoption_add') "
    "AND filled_amount > 0 "
    "AND status NOT IN ('reset_cleared','auto_closed','cancelled','canceled','failed','rejected') "
    "AND id != ?",
    (bot_id, row_step, row_cycle, db_id)
).fetchone()[0] or 0.0

capacity_limit = order_amount * 1.05
if already_credited > 0 and (already_credited + delta_proposed) > capacity_limit:
    # Mark current order auto_closed, return False (no credit)
```

### Verification
- **Logic sound**: Sums fills from OTHER orders in same step/cycle, excludes current order, compares against capacity
- **Catchup exemption** (lines 515-523): `_CATCHUP_` orders are exempt — they're additive by design
- **Test coverage**: `test_ledger_integrity.py::test_hedge_child_order_virtual_net`, `test_inv29_saturated_step_not_re_signaled`, `test_inv29_no_double_signal_when_child_open` all PASS
- **No double-count observed**: Exchange fills for hedge children match virtual net (bot 100315 SOL 0.23, bot 100324 SOL 0.23, bot 100319 XAU 1.681)

### Conclusion
**VERIFIED RESOLVED** — No action needed. The guard works correctly. The "double-count" label was likely a pre-fix concern.

---

## Item 6: GTR Lock Display — Verification

### What It Is
Display of bots locked to `REQUIRE_MANUAL_PROOF` status in UI/logs, including lock duration.

### Current Implementation

**Engine log (ground_truth_reconciler.py:132-138):**
```python
logger.critical(
    f"[GTR-INV31] REQUIRE_MANUAL_PROOF: Bot {name} ({bot_id}) "
    f"has been locked for {age//3600}h {(age%3600)//60}m. "
    f"Virtual open_qty={open_qty:.6f} "
    f"(~${usd_value:.2f} USD unresolved). "
    f"Human intervention required..."
)
```

**UI display (monitor.py:544-561):**
```python
for _bot_name in _hd.get("manual_proof_bots", []):
    # Checks if pair has netting mismatch
    if is_netting_mismatch:
        _banner_parts.append(f"🔴 REQUIRE_MANUAL_PROOF: {_bot_name} — ⚠️ Netting Mismatch")
    else:
        _banner_parts.append(f"🔴 REQUIRE_MANUAL_PROOF: {_bot_name} — human intervention needed")
```

**System health (health.py:27):**
```python
manual_proof_bots: list - bot names locked to REQUIRE_MANUAL_PROOF status
```

### Verification
- **Log output**: Shows lock duration in hours/minutes, virtual qty, USD value ✅
- **UI banner**: Shows bot name + whether netting mismatch is the cause ✅
- **System health**: Returns list for programmatic access ✅
- **No missing fields**: All required info (bot name, duration, qty, USD, reason) present

### Conclusion
**VERIFIED RESOLVED** — Display is functional. The "lock display" label was likely a pre-implementation concern.

---

## Item 8: Flatten Price=0.0 — Verification

### What It Is
Concern that `price=0.0` is passed to exchange for flatten close orders.

### Current Implementation

**Market close (ledger.py:1866-1869):**
```python
close_order = exchange.create_order(
    norm_pair, 'market', close_side, qty,  # market order — no price param
    params=_flatten_params
)
```
Market orders don't take a price parameter — exchange fills at best available.

**Audit row (bot_executor.py:672-678):**
```python
save_bot_order(
    child_bot_id, 'drift_note', drift_cid,
    price=0.0, amount=0.0, step=0, status='audit',  # Intentional: audit row, no price/qty
    ...
)
```
This is a `drift_note` audit entry — price=0, amount=0 is correct for metadata-only row.

**Pending close fallback (bot_executor.py:4010-4017):**
```python
_sbo_partial(
    bot_id, 'close', close_cid,
    price=0.0, amount=current_open_qty, step=0,  # Market order placeholder
    status='pending_placement',
    ...
)
# Then exchange.create_order(pair, 'market', close_side, current_open_qty) — no price
```
The `price=0.0` in `save_bot_order` is a placeholder for the pending_placement row. The actual exchange call is `market` with no price.

### Verification
- **No limit/stop orders with price=0.0**: All flatten paths use `market` order type
- **Market orders**: Correctly omit price (exchange ignores price for market)
- **Audit rows**: Correctly use 0.0 for metadata-only entries
- **Actual fill price**: Retrieved from exchange response (`close_order.get('average')` or `get('price')`) and used for `reset_price`

### Conclusion
**VERIFIED RESOLVED** — No bug. The `price=0.0` occurrences are either:
1. Market order placeholder (correct — market orders don't use price)
2. Audit/metadata rows (correct — no price/qty applicable)

---

## Track C Summary

| Item | Verdict | Action |
|------|---------|--------|
| 3. INV30 double-count | ✅ Verified resolved | None |
| 6. GTR lock display | ✅ Verified resolved | None |
| 8. Flatten price=0.0 | ✅ Verified resolved | None |

**Track C complete — no fixes needed for items 3, 6, 8.**