# ADR-003: Hedge Child Exchange-Authoritative Execution

**Status:** Required
**Version:** v4.0.0
**Addresses:** Infinite accumulation loop, TP rejection loop, DB/exchange drift

---

## The Two Fundamental Flaws

### Flaw 1 — Timestamp CIDs bypass idempotency

`_signal_hedge_child_entry` generates CIDs like:
`CQB_100315_ENTRY_57_10_1780644877`
`CQB_100315_ENTRY_57_10_1780644889`

The timestamp suffix makes every CID unique. The idempotency check
queries by CID — finds nothing — places another entry. Result:
54 duplicate 3.0 SOL entries in 2 minutes, all filled.

### Flaw 2 — TP sized from DB, not exchange

`maintain_orders` for hedge child reads `trades.open_qty` to size the TP.
When DB drifts above physical (inevitable given Flaw 1), the TP order
is larger than the exchange position. Exchange rejects with -4118 or
silently expires. TP never fills. Parent triggers again. New entries
added. Drift grows. Loop repeats forever.

### Why these combine into an infinite loop

```
Parent hits trigger step
  → _signal_hedge_child_entry fires
  → CID has timestamp → idempotency misses → entry placed
  → Entry fills on exchange (sometimes partially, sometimes not)
  → DB records it as filled regardless
  → DB open_qty grows beyond exchange position
  → Parent TPs → hedge child BE TP placed at DB qty
  → Exchange rejects (qty > physical position)
  → Hedge child never closes
  → Parent re-enters on next cycle
  → _signal_hedge_child_entry fires again
  → Adds ANOTHER entry on top of existing position
  → DB drift grows further
  → Repeat
```

---

## The Architectural Fix: Exchange-First Hedge Execution

### Invariant A — CIDs must be deterministic, not timestamp-based

A hedge child entry CID must be fully determined by:
- child bot_id
- parent cycle_id  
- parent step number

Format: `CQB_{child_id}_ENTRY_{parent_cycle}_{parent_step}`

No timestamp. No suffix. One CID per (cycle, step) combination.
If that CID exists on the exchange OR in the DB with any non-failure
status, skip placement entirely.

**File: `bot_executor.py`, `_signal_hedge_child_entry`**

Remove the timestamp from all CID generation:
```python
# WRONG — timestamp makes CID unique every call
cid = f"CQB_{child_id}_ENTRY_{cycle_id}_{parent_step}_{int(time.time())}"

# CORRECT — deterministic per (cycle, step)
cid = f"CQB_{child_id}_ENTRY_{cycle_id}_{parent_step}"
```

Idempotency check must use the deterministic CID:
```python
existing = conn.execute(
    "SELECT id FROM bot_orders WHERE client_order_id = ? "
    "AND status NOT IN ('cancelled','failed','reset_cleared','rejected')",
    (cid,)
).fetchone()
if existing:
    return  # Already placed for this cycle+step
```

Also check the exchange directly before placing:
```python
try:
    exch_order = exchange.fetch_order_by_client_id(cid)
    if exch_order and exch_order.get('status') not in ('cancelled','rejected','expired'):
        return  # Live on exchange already
except Exception:
    pass  # Not found = safe to place
```

### Invariant B — Hedge child TP must use exchange position size, not DB

Before placing or replacing a hedge child TP, fetch the actual
exchange position for the pair. Size the TP from the exchange,
not from `trades.open_qty`.

**File: `bot_executor.py`, hedge child TP placement block**

```python
# WRONG — uses DB open_qty which may be inflated
tp_qty = child_state['open_qty']

# CORRECT — authoritative from exchange
_phys = self._get_phys_pos(pair, direction=child_direction)
if not _phys or _phys['size'] < 0.0001:
    logger.warning(f"[HEDGE-TP] {name}: Exchange flat, skipping TP placement")
    return
tp_qty = _phys['size']  # What the exchange actually holds
```

This means the TP is always sized to close exactly what the exchange
holds — never more, never less. DB drift becomes irrelevant to TP sizing.

### Invariant C — Entry qty must be verified against exchange capacity

Before placing a hedge child entry, check that the exchange can
accept the order without exceeding position limits:

```python
_current_phys = self._get_phys_pos(pair, direction=child_direction)
_current_phys_qty = _current_phys['size'] if _current_phys else 0.0
_expected_after = _current_phys_qty + entry_qty
if _expected_after > MAX_HEDGE_POSITION_LIMIT:
    logger.error(f"[HEDGE-ENTRY] Would exceed position limit. Skipping.")
    return
```

`MAX_HEDGE_POSITION_LIMIT` should be configurable per bot.

### Invariant D — DB open_qty must be reconciled to exchange on seal

`seal_trade_state` for hedge child bots must cross-check the
recomputed qty against the exchange position. If drift exceeds 5%:

```python
_phys = self._get_phys_pos(pair, direction=direction)
_phys_qty = _phys['size'] if _phys else 0.0
_db_qty = recomputed_qty
if _phys_qty > 0 and abs(_db_qty - _phys_qty) / _phys_qty > 0.05:
    logger.warning(
        f"[SEAL-DRIFT] {bot_id}: DB={_db_qty:.4f} vs "
        f"Exchange={_phys_qty:.4f} drift>{5}%. Using exchange qty."
    )
    _db_qty = _phys_qty  # Exchange wins
```

---

## Immediate DB Recovery (before code fix)

These DB fixes must run BEFORE the code fix and engine restart.
They bring the DB into alignment with what the exchange actually holds.

### SOL hedge recovery

Exchange holds: 159.71 SOL SHORT
DB claims: 167.56 SOL SHORT
Drift: 7.85 SOL phantom

```sql
-- 1. Mark phantom duplicate entries as phantom_duplicate
UPDATE bot_orders 
SET status = 'phantom_duplicate', notes = 'Duplicate entry storm - exchange never held'
WHERE bot_id = 100315 
AND cycle_id = 57 
AND order_type = 'entry' 
AND status = 'filled'
AND id NOT IN (
    -- Keep entries that sum to actual exchange qty (159.71)
    -- Keep the earliest entries until cumulative sum reaches 159.71
    SELECT id FROM (
        SELECT id, 
               SUM(filled_amount) OVER (ORDER BY created_at ASC) as cumsum
        FROM bot_orders
        WHERE bot_id = 100315 AND cycle_id = 57 
        AND order_type = 'entry' AND status = 'filled'
    ) WHERE cumsum <= 159.72
);

-- 2. Reseal to recompute from remaining valid entries
-- (run seal_trade_state(100315) via Python after)
```

### XRP hedge recovery

Exchange holds: 2872.1 XRP SHORT
DB claims: 2886.1 XRP SHORT  
Drift: 14.0 XRP phantom

The BE TP is currently at $1.1541 with XRP at $1.1496.
The hedge IS profitable (SHORT, price below entry). 
The TP will fill if XRP drops another ~0.4%.

```sql
-- 1. Update open_qty to match exchange
UPDATE trades SET open_qty = 2872.1 WHERE bot_id = 100313;

-- 2. Cancel the oversized TP that keeps getting rejected
UPDATE bot_orders SET status = 'cancelled'
WHERE bot_id = 100313 
AND order_type = 'tp' 
AND status IN ('open','new','pending_placement');

-- 3. Reseal
-- (run seal_trade_state(100313) via Python after)
-- The next cycle will place a correctly-sized TP at exchange qty
```

---

## Implementation Order

1. Run DB recovery SQL above
2. Run seal_trade_state for both bots
3. Verify exchange alignment with check_state.py
4. Implement Invariant A (deterministic CIDs) — highest priority
5. Implement Invariant B (exchange-authoritative TP sizing)
6. Implement Invariant C (entry capacity check)
7. Implement Invariant D (seal drift correction)
8. Run full test suite
9. Add tests specifically for:
   - Duplicate CID idempotency (same cycle+step called twice)
   - TP sizing matches exchange not DB
   - Entry rejected when at capacity limit
10. Restart engine

---

## CODEBASE_GUIDE additions

Add as INV-19 through INV-22:

**INV-19:** Hedge child entry CIDs are deterministic:
`CQB_{child_id}_ENTRY_{parent_cycle}_{parent_step}`.
No timestamp suffix. Ever. Violation causes duplicate entries.

**INV-20:** Hedge child TP qty comes from exchange position size,
not `trades.open_qty`. DB is a hint. Exchange is authoritative.

**INV-21:** Before every hedge child entry, verify exchange
position + entry_qty does not exceed configured position limit.

**INV-22:** `seal_trade_state` for hedge child bots cross-checks
recomputed qty against exchange. If drift > 5%, exchange wins.

---

## Version

This is v4.0.0. The timestamp CID and DB-authoritative TP sizing
are architectural decisions that have caused every recurring hedge
bug since v3.5.x. These four invariants close the loop permanently.
