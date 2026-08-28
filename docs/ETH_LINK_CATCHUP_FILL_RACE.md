# ETH/LINK Catchup Fill-Credit Race — Root Cause & Fix Design

**Date:** 2026-08-28
**Status:** DESIGN ONLY — not implemented. Awaiting operator review.
**Severity:** Recurring architectural bug. Affects any pair with hedge-child catchup entries.

---

## Summary

During live trading (not downtime), the engine's hedge-child catchup entry
lifecycle has a fill-credit race: the engine marks a catchup order as
`reset_cleared` or `auto_closed` **before** the exchange fill credit arrives.
The fill is lost from the DB, the virtual net diverges from the exchange, and
O-10 correctly freezes the pair.

This is **not** a downtime-fill issue. It is a live-trading fill-credit race
specific to the catchup/re-entry pattern.

---

## Evidence (real numbers)

### LINK (virtual -102.65 vs exchange -87.46, Δ=15.19)

| CID | Bot | DB status | DB filled | Exchange filled | Gap |
|-----|-----|-----------|-----------|-----------------|-----|
| CQB_10020_GRID_1_9 | 10020 (parent) | **open** | 0.0 | **140.33** | 140.33 |
| CQB_100320_ENTRY_1_2_CATCHUP | 100320 (child) | **partially_filled** | 0.51 | **69.22** | 68.71 |
| CQB_100320_ENTRY_1_8_R | 100320 (child) | **open** | 0.0 | **68.72** | 68.72 |

The parent's GRID_1_9 (140.33 SHORT) filled on exchange but DB says `open`.
The child's catchup entries filled on exchange but DB says `partially_filled`/`open`.

### ETH (virtual -1.557 vs exchange 0.0, Δ=1.557)

| CID | Bot | DB status | DB filled | Exchange filled | Gap |
|-----|-----|-----------|-----------|-----------------|-----|
| CQB_100002_GRID_5_9 | 100002 (parent) | **open** | 0.0 | **1.244** | 1.244 |
| CQB_100325_ENTRY_5_2_CATCHUP | 100325 (child) | **auto_closed** | 0.0 | **0.467** | 0.467 |
| CQB_100325_ENTRY_5_1_CATCHUP | 100325 (child) | **reset_cleared** | 0.352 | **0.352** | 0 |
| CQB_100325_ENTRY_5_8_R | 100325 (child) | **reset_cleared** | 0.408 | **0.408** | 0 |

Same pattern: parent grid filled on exchange, DB says `open`. Child catchup
entries got `reset_cleared` or `auto_closed` in DB (engine thought they were
superseded/cancelled), but they **actually filled on exchange**.

---

## The Mechanism

1. Parent's grid order fills → engine places child catchup entry
2. Child catchup fills on exchange
3. Engine's reset/reconciliation logic marks the catchup as `reset_cleared`
   or `auto_closed` **before the fill credit arrives** (perhaps because a
   newer entry superseded it, or the reset logic ran before the WS fill event)
4. Fill is lost from DB → virtual net diverges from exchange → O-10 correctly freezes

The root cause is in the **ordering** of two operations:
- **Fill credit** (WS event → `credit_fill()` → `seal_trade_state()`)
- **Reset/clear** (reconciliation → `reset_cleared` / `auto_closed`)

When reset/clear runs before fill credit, the fill is lost.

---

## Is This Recurring?

**Yes, but specific to the catchup/re-entry pattern.** Any pair where:
- A parent bot's grid fills rapidly (volatile market)
- The hedge child's catchup entry is placed and fills
- But the engine's reset logic clears the catchup before the fill is credited

...will hit this same divergence. It is **not** a general netting bug — it is
a **fill-credit race condition** in the catchup entry lifecycle.

The O-10 freeze is working correctly (detecting the divergence and locking
bots). The root cause is upstream: the fill-credit path for catchup entries.

---

## Proposed Fix Design (NOT IMPLEMENTED)

### Option A: Don't reset_cleared/auto_close until fill status is confirmed final

Before marking an order as `reset_cleared` or `auto_closed`, check whether
the exchange reports it as filled. If the exchange says filled, credit the
fill first, then proceed with the reset/clear.

```python
# In the reset/clear path:
def _safe_reset_order(order_id, bot_id):
    # 1. Check exchange status first
    exchange_status = fetch_order_status(order_id)
    if exchange_status == 'filled':
        # Credit the fill before clearing
        credit_fill(order_id, bot_id)
        seal_trade_state(bot_id)
    # 2. Now safe to reset/clear
    mark_reset_cleared(order_id)
```

### Option B: Check for a fill one more time before clearing

Add a final exchange check in the reset/clear path. If the order filled
between the last known state and now, credit it before clearing.

```python
# In the reset/clear path:
def _safe_reset_order(order_id, bot_id):
    # Final check: did it fill since we last looked?
    detail = exchange.fetch_order(order_id, pair)
    if detail.get('filled', 0) > 0:
        credit_fill(order_id, bot_id)
        seal_trade_state(bot_id)
    mark_reset_cleared(order_id)
```

### Option C: Make reset/clear aware of pending fills

Track a "pending fill" flag on orders that have been placed but not yet
confirmed. The reset/clear logic should not clear orders with pending fills.

```python
# In bot_orders schema:
ALTER TABLE bot_orders ADD COLUMN fill_pending INTEGER DEFAULT 0;

# When placing an order:
UPDATE bot_orders SET fill_pending=1 WHERE client_order_id=?;

# When fill credit arrives:
UPDATE bot_orders SET fill_pending=0 WHERE client_order_id=?;

# In reset/clear path:
if order.fill_pending:
    # Don't clear yet — wait for fill confirmation
    return
```

### Recommendation

**Option A** is the simplest and most robust. It adds one exchange lookup
per reset/clear operation, which is acceptable given the low frequency of
these operations. It also handles the case where the fill arrived but the
WS event was missed.

**Option C** is the most thorough but requires a schema change and careful
state management. It's worth considering if Option A proves insufficient.

---

## What NOT to Do

- **Do not auto-heal ETH/LINK now.** The divergence is real and the bots are
  correctly frozen. Manual review is needed to understand the full scope.
- **Do not rush this fix.** It's an architectural change to the fill-credit
  lifecycle. It deserves proper design time and testing.
- **Do not assume O-10 is wrong.** O-10 is working correctly — it detected
  the divergence and locked the bots. The bug is upstream.

---

## Next Steps

1. Operator reviews this design
2. Choose Option A, B, or C (or a combination)
3. Implement with full test coverage
4. Verify on a test pair before deploying to live pairs
5. Manually resolve ETH/LINK frozen state after the fix is in place
