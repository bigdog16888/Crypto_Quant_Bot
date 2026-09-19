# Track B Fill-Crediting Reliability Audit
**Date:** 2026-09-19  
**Context:** Post Part 1 (is_active guards) — read-only investigation, no code changes

---

## Executive Summary

The fill-crediting pipeline is **fundamentally sound** with three layers of idempotency:
1. **fill_claims singleton guard** (INV-20) — prevents same (bot_id, order_id) double credit
2. **Step saturation guard** (INV-30) — prevents same step/cycle double credit across order_ids
3. **Dual-write guard** (ledger.py:631) — prevents cumulative replay from double-logging to exchange_fills

**Two actionable findings** requiring fixes:
| # | Finding | Severity | File:Line |
|---|---------|----------|-----------|
| 1 | Reconciler double dual-write to exchange_fills | MEDIUM | reconciler.py:1279 |
| 2 | Adoption/healing paths infer side instead of using exchange side | LOW | parity_gates.py:1135, database.py:2173 |

---

## Fill-Crediting Pipeline Map

### Core Function: `engine/ledger.py::credit_fill()`

**Three guards executed in order:**

| Guard | Location | Mechanism | What It Prevents |
|-------|----------|-----------|------------------|
| fill_claims singleton | lines 405-436 | `INSERT OR IGNORE INTO fill_claims (bot_id, order_id, ...)` | Same fill credited twice via same order_id |
| Step saturation | lines 467-550 | `INSERT OR IGNORE INTO fill_claims (bot_id, 'STEP_{step}_{cycle}', ...)` + capacity check | Same step/cycle credited via different order_ids (GTX chase, catchup) |
| Dual-write to exchange_fills | lines 607-656 | `if not (delta <= 0 and is_cumulative): record_exchange_fill(...)` | Cumulative replay/sync from double-counting in immutable fill log |

**Delta calculation (line 568):**
```python
delta = cumulative_qty - existing_fill  # net NEW qty this call
```

**open_qty accumulator (lines 571-597):**
- Entry types: `open_qty += delta` (floored at 0 for negative delta)
- Exit types: `open_qty -= delta` (floored at 0 for positive delta)
- Reversals (delta < 0 on entry, delta > 0 on exit) correctly handled

---

### Call Sites (8 Call Stacks)

| # | Call Site | File:Line | order_id Source | side Source | is_cumulative | caller | Notes |
|---|-----------|-----------|-----------------|-------------|---------------|--------|-------|
| 1 | WS live fills | ws_event_handlers.py:125, 136 | exch_oid → fallback client_oid | **Real exchange side** ✓ | True | 'ws' | Tries exch_oid first, then client_oid |
| 2 | Reconciler uncredited | reconciler.py:1254 | bot_orders order_id | **Inferred from bot direction** ⚠️ | True | 'reconciler-uncredited' | **Double dual-write** (see Finding 1) |
| 3 | TP-SYNC partial fill | bot_executor.py:1862 | tp_order_id (exch) | **Real exchange side** ✓ | True | 'maintain_orders' | Only on cancelled TP with partial fill |
| 4 | ENTRY-RETRO (fill heal) | bot_executor.py:2850 | order_id (exch) | **Real exchange side** ✓ | True | 'maintain_orders' | |
| 5 | CANCEL-SWEEP | exchange_interface.py:1370 | order_id (exch) | **Real exchange side** ✓ | True | 'cancel_sweep' | suppress_cascade=True |
| 6 | Parity gates adoption | parity_gates.py:1135 | exchange order_id | **Inferred from bot direction** ⚠️ | True | 'orphan_adopt' | Adoption = orphan physical position |
| 7 | FILL-HEAL (sync down/up) | database.py:4438, 4478 | exch_oid | **Real exchange side** ✓ | True | 'exchange_fill_heal' | Two calls: sync-down (delta≤0) & sync-up |
| 8 | Race guard (heal_zombies) | database.py:2173 | exch_oid | **Inferred from bot direction** ⚠️ | True | 'race_guard' | Pre-reset fill check |

---

## Detailed Findings

### Finding 1: Reconciler Double Dual-Write ⚠️ MEDIUM
**Location:** `engine/reconciler.py:1254-1293`

**Code flow:**
```python
# Line 1254-1263: credit_fill() already does dual-write internally (ledger.py:639-654)
credit_fill(
    bot_id=bot_id,
    order_id=str(order_id),
    cumulative_qty=filled_qty,
    avg_price=avg_price,
    order_type=order_type,
    is_cumulative=True,
    caller='reconciler-uncredited',
    side=fill_side
)

# Line 1279-1293: SECOND direct record_exchange_fill() with SAME parameters
record_exchange_fill(
    conn=_credit_conn,
    exchange_order_id=str(order_id),
    client_order_id=client_cid or '',
    symbol=_symbol,
    side=fill_side,
    qty=filled_qty,          # Same as credit_fill's delta (since first credit, delta = filled_qty)
    price=avg_price,
    fill_ts=_fill_ts,        # Uses time.time() vs credit_fill's actual_fill_ts
    source='reconciler-uncredited',
    bot_id=bot_id,
    order_type=order_type,
    step=step,
    cycle_id=cycle_id,
)
```

**Why it's a problem:**
- `exchange_fills` has UNIQUE constraint on `(exchange_order_id, fill_ts, qty, price)`
- If `fill_ts` differs (credit_fill uses `actual_fill_ts` = `fill_ts` or `time.time()`; reconciler uses `time.time()`), both inserts succeed → **duplicate fill log entries**
- Position computation (`position_ledger` → `compute_pair_position`) reads from `exchange_fills` → **double-counted position**

**Fix:** Remove the direct `record_exchange_fill` call at lines 1275-1293. The `credit_fill` dual-write is sufficient and uses the correct `actual_fill_ts`.

**Test impact:** No existing test covers this double-write. Need to add test verifying single exchange_fills entry per reconciler credit.

---

### Finding 2: Side Inference in Adoption/Healing Paths ⚠️ LOW
**Locations:**
- `engine/parity_gates.py:1135` (orphan adoption)
- `engine/database.py:2173` (heal_zombie_bots race guard)

**Problem:** These calls omit the `side` parameter. `credit_fill` infers:
```python
# ledger.py:612-623
if side and side.upper() in ('BUY', 'SELL'):
    fill_side = side.upper()
else:
    _bot_dir = conn.execute("SELECT direction FROM bots WHERE id = ?", (bot_id,)).fetchone()
    _is_long = _bot_dir and 'long' in str(_bot_dir[0]).lower()
    if _otype_lower in _ENTRY_TYPES:
        fill_side = 'BUY' if _is_long else 'SELL'
    else:
        fill_side = 'SELL' if _is_long else 'BUY'
```

**Risk scenarios:**
1. **Adoption** (parity_gates.py): Orphan physical position may have opposite direction to bot's configured direction. Example: Bot is LONG, but exchange has SHORT residual (from manual trade or another bot). Adoption infers BUY, but real fill was SELL → wrong side in exchange_fills → position computation error.
2. **Race guard** (database.py): Healing a zombie bot before reset. If the pending order was an exit order on a LONG bot, inference gives SELL (correct). But if order_type is misclassified, wrong side.

**Mitigation:** Both call sites have access to exchange order detail (`exch_order.get('side')` or `_detail.get('side')`). Pass it explicitly.

**Fix:** Add `side=exch_order.get('side', '')` to both calls.

---

### Finding 3: fill_claims Cross-ID Gap (Known, Accepted Risk)
**Mechanism:** fill_claims PK is `(bot_id, order_id)`. If WS credits with `exchange_order_id` and reconciler credits with `client_order_id` for the SAME fill:
- fill_claims sees two different keys → both proceed
- BUT: credit_fill's bot_orders lookup uses `WHERE order_id=? OR client_order_id=?`
- Second call finds the row, computes `delta = cumulative_qty - existing_fill = 0`
- Dual-write guard: `delta <= 0 and is_cumulative=True` → `_log_fill = False`
- open_qty delta = 0 → no double-count

**Residual risk:** If the two calls use different `fill_ts`, the dual-write guard might not suppress the second exchange_fills log (since `delta=0` but `is_cumulative=True` → `_log_fill=False` anyway). The exchange_fills UNIQUE constraint on `(exchange_order_id, fill_ts, qty, price)` would only catch if all 4 match.

**Verdict:** Accepted. Defense-in-depth (step lock, dual-write guard, MAX protection) makes double-credit practically impossible. Not worth complicating fill_claims with composite keys.

---

## Test Coverage Status

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `test_ledger_integrity.py` | 33 | Core credit_fill logic, dual-write guard, step saturation, MAX protection, FIFO |
| `test_hedge_lifecycle.py` | 55 | Hedge child fill crediting, TP cascade, INV-29 gates |
| `test_adopt_fill_guard.py` | 5 | Adoption gate, REQUIRE_MANUAL_PROOF |
| `test_explicit_side_wiring.py` | 1 | Explicit side beats inference |
| `test_bot_lifecycle.py` | 1 | Full lifecycle integration |
| `test_database.py` | 20 | Schema, recompute, wipe wall, migrations |

**All 115 tests PASS** (verified fresh subprocess).

**Missing test coverage:**
- Reconciler double dual-write (Finding 1)
- Adoption side inference with opposite-direction physical position
- Cross-ID fill_claims scenario (WS exch_oid vs reconciler client_oid)

---

## Recommendations

### Immediate (before next engine start)
1. **Fix Finding 1** — Remove reconciler.py lines 1275-1293 (direct record_exchange_fill)
2. **Fix Finding 2** — Add `side=` parameter to parity_gates.py:1135 and database.py:2173

### Next Session (Part 3 / Track C)
3. Add test for reconciler single exchange_fills entry
4. Add test for adoption with opposite-direction physical position
5. Consider extending fill_claims PK to `(bot_id, exchange_order_id, client_order_id)` or add a composite unique index — but only if cross-ID double-credit ever observed in prod

---

## Verification Commands

```bash
# Run full test suite (Python 3.10 target)
cd D:/Crypto_Quant_Bot && python -m pytest tests/ -v

# Verify no double exchange_fills for same order
sqlite3 crypto_bot.db "SELECT exchange_order_id, fill_ts, COUNT(*) FROM exchange_fills GROUP BY exchange_order_id, fill_ts HAVING COUNT(*) > 1;"

# Check fill_claims cross-ID
sqlite3 crypto_bot.db "SELECT bot_id, order_id, COUNT(*) FROM fill_claims GROUP BY bot_id, order_id HAVING COUNT(*) > 1;"
```

---

## Sign-off

- [x] Pipeline traced end-to-end
- [x] All 8 call sites categorized
- [x] Three guards verified
- [x] Two findings documented with fixes
- [x] Test coverage mapped
- [ ] Findings 1 & 2 fixed (next session)
- [ ] Tests for findings added (next session)