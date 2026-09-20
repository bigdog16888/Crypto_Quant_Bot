# Track B Finding 2 — Side Inference Gap in Adoption/Healing: Proposed Fix

## Summary
Two call sites omit the `side=` parameter when calling `credit_fill()`, causing `credit_fill` to infer side from bot direction. If the physical exchange position has opposite direction to bot config (e.g., hedge child, orphan adoption), the inferred side is wrong → wrong sign in `exchange_fills` → position computation error.

## Affected Call Sites

### 1. Orphan Adoption (parity_gates.py:1135)
```python
if credit_fill(
    bot_id=bot_id,
    order_id=oid,
    cumulative_qty=filled,
    avg_price=avg,
    order_type='adoption',
    is_cumulative=True,
):
```
**No `side=` passed.** `credit_fill` infers from bot direction (line 616-623 in ledger.py):
```python
_bot_dir = conn.execute("SELECT direction FROM bots WHERE id = ?", (bot_id,)).fetchone()
_is_long = _bot_dir and 'long' in str(_bot_dir[0]).lower()
if _otype_lower in _ENTRY_TYPES:
    fill_side = 'BUY' if _is_long else 'SELL'
else:
    fill_side = 'SELL' if _is_long else 'BUY'
```
**Problem:** Orphan adoption may be crediting a fill that physically went opposite to bot's configured direction (e.g., hedge child fill on parent pair, or manual exchange action).

### 2. Race Guard (database.py:2173)
```python
from engine.ledger import credit_fill as _cf_race
_cf_race(
    bot_id=bot_id,
    order_id=str(_ex_oid),
    cumulative_qty=_filled,
    avg_price=float(_detail.get('average', 0) or 0),
    order_type=str(_otype or 'grid').lower(),
    is_cumulative=True,
    caller='race_guard'
)
```
**No `side=` passed.** The exchange response `_detail` HAS the real side (`_detail.get('side')`) but it's not passed.

## Proposed Fixes

### Fix 1: parity_gates.py:1135 (Orphan Adoption)
```diff
--- a/engine/parity_gates.py
+++ b/engine/parity_gates.py
@@ -1132,6 +1132,7 @@ async def adopt_orphan_fills_from_exchange_history(
         if credit_fill(
             bot_id=bot_id,
             order_id=oid,
             cumulative_qty=filled,
             avg_price=avg,
             order_type='adoption',
             is_cumulative=True,
+            side=exch_order.get('side', ''),  # REAL EXCHANGE SIDE
         ):
             credited_total += filled
             credited_cids.append(cid)
```

### Fix 2: database.py:2173 (Race Guard)
```diff
--- a/engine/database.py
+++ b/engine/database.py
@@ -2170,6 +2170,7 @@ def reset_bot_after_tp(
                                 _cf_race(
                                     bot_id=bot_id,
                                     order_id=str(_ex_oid),
                                     cumulative_qty=_filled,
                                     avg_price=float(_detail.get('average', 0) or 0),
                                     order_type=str(_otype or 'grid').lower(),
                                     is_cumulative=True,
                                     caller='race_guard'
+                                    side=_detail.get('side', ''),  # REAL EXCHANGE SIDE
                                 )
```

## Why This Matters
- `exchange_fills` is the **immutable position source** (position_ledger reads from it)
- Wrong `side` = wrong algebraic sign (BUY=+, SELL=-) = position computation error
- The dual-write guard (ledger.py:631) only prevents double-counting, not sign errors
- One-way mode (RULE #0): position = net algebraic sum. Sign error = position error.

## Evidence of Risk
- Bot 100315 (SOL hedge) and 100324 (SOL hedge) have positions that may have been adopted
- XAU duplicate AP row (0.201 SHORT) may stem from side inference mismatch
- No test currently covers adoption with opposite-direction physical fills

## Test Plan
1. Add test: adoption with `side='SELL'` on LONG bot → verify `exchange_fills.side='SELL'`
2. Add test: race guard with `side='BUY'` on SHORT bot → verify `exchange_fills.side='BUY'`
3. Verify `compute_pair_position` returns correct net for mixed-side fills
4. Full regression suite pass

## Priority
**MEDIUM** — Defense-in-depth. Current bots mostly match config direction, but hedge children and manual actions create risk. Fix before enabling live reconciliation.