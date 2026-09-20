# Item 7 — Retry-Queue False Alarm: Root-Cause Analysis + Proposed Fix

## Current State (Verified)
- File: `engine/ws_event_handlers.py` lines 149-181 (`_fill_credited_by_sibling`)
- Trigger: 2026-09-09 bot 10018 incident — gate fired 26s after step-lock winner credited; pair parity green 3s later
- The stand-down check failed to detect sibling credit

## Root Cause Trace

### The Sibling Check Logic (lines 161-181)
```python
def _fill_credited_by_sibling(bot_id, order_id, client_id, qty):
    # 1. Check fill_claims for exchange_order_id OR client_order_id
    claimed = conn.execute(
        "SELECT caller FROM fill_claims WHERE bot_id=? AND (order_id=? OR order_id=?) ...",
        (bot_id, str(order_id), str(client_id) if client_id else str(order_id))
    ).fetchone()
    if not claimed:
        return False
    # 2. Check bot_orders for filled_amount or terminal status
    row = conn.execute(
        "SELECT filled_amount, status FROM bot_orders WHERE bot_id=? AND (order_id=? OR client_order_id=?)",
        (bot_id, str(order_id), str(client_id) if client_id else str(order_id))
    ).fetchone()
    filled_amount = float(row[0] or 0)
    return filled_amount >= qty * 0.99 or str(row[1]) in ('filled', 'partially_filled', 'closed')
```

### The Step-Lock Claim Key (ledger.py:473)
```python
# INV-30 step saturation guard claims a SYNTHETIC key
_step_claim = conn.execute(
    "INSERT OR IGNORE INTO fill_claims (bot_id, order_id, caller, claimed_at) VALUES (?, ?, ?, ?)",
    (bot_id, f"STEP_{row_step}_{row_cycle}", f"step_lock_{_claim_caller}", int(time.time()))
)
```

### The Mismatch
| Path | fill_claims Key | bot_orders Key |
|------|-----------------|----------------|
| **WS retry queue** | `exchange_order_id` (or `client_order_id`) | `exchange_order_id` / `client_order_id` |
| **Step-lock winner (INV-30)** | `STEP_{step}_{cycle}` (synthetic) | Same `bot_orders` row (by `db_id`) |

**The stand-down check queries `fill_claims` with `exchange_order_id`/`client_order_id` — it NEVER sees the `STEP_{step}_{cycle}` claim.**

### Why the False Alarm Occurred (2026-09-09 bot 10018)
1. Hedge child fill arrives via WS
2. WS retry queue tries `credit_fill(exchange_order_id)` → no bot_orders row yet → returns False → parks in retry queue
3. Step-lock path (INV-30) runs, finds the bot_orders row, credits it using `STEP_{step}_{cycle}` claim key
4. Fill is properly credited to bot_orders, seal_trade_state runs, pair parity green
5. 26s later: retry queue exhausts retries (3 retries × ~1s + 30s age ceiling)
6. Stand-down check runs: queries `fill_claims` for `exchange_order_id` → **no claim found** (claim is under `STEP_...`)
7. Stand-down check queries `bot_orders` for `exchange_order_id` → **row found with filled_amount > 0** → SHOULD return True
8. **But**: the bot_orders lookup uses `order_id` OR `client_order_id` — if the step-lock credited via a DIFFERENT order_id (e.g., the exchange assigned a different ID), the lookup fails

Wait — the step-lock credits the SAME bot_orders row (by `db_id`), so the exchange_order_id should be the same. Let me check...

Actually, looking at the credit_fill flow: the step-lock check happens INSIDE `credit_fill` (ledger.py:469-481). It checks for OTHER orders in the same step/cycle that already have fills. If found, it marks the current order `auto_closed` and returns False (doesn't credit). The "winner" is the FIRST order that gets credited for that step.

So the scenario is:
1. Multiple orders for same step (GTX chase retries)
2. First order fills → WS credits it via exchange_order_id → claim key = exchange_order_id
3. Second order (chase) fills → WS tries to credit → credit_fill sees step already saturated by first order → marks second order auto_closed
4. But the retry queue might have the SECOND order parked
5. Stand-down check for second order: looks for exchange_order_id of second order in fill_claims → not claimed (first order has different exchange_order_id)
6. bot_orders lookup for second order: status = auto_closed, filled_amount = 0 → returns False
7. Escalates to REQUIRE_MANUAL_PROOF — FALSE ALARM

**But the comment says "step-lock winner had already credited"** — meaning the first order WAS credited. The false alarm is for a DIFFERENT order (the chase retry) that was correctly auto_closed.

### The Real False Alarm Pattern
The retry queue parks fills for orders that are still "live" (not yet in bot_orders). If a sibling credits a DIFFERENT order for the same step (GTX chase), the step-lock marks our order auto_closed. Our retry queue then escalates because:
- fill_claims has no claim for our exchange_order_id
- bot_orders has our order as auto_closed with filled_amount=0
- Stand-down returns False → escalation

This is a **correct auto_close** being misclassified as a failed credit.

## Proposed Fix

### Fix 1: Check for Auto-Closed Step Siblings
In `_fill_credited_by_sibling`, also check if there's ANOTHER order in the same step/cycle that was credited.

```diff
--- a/engine/ws_event_handlers.py
+++ b/engine/ws_event_handlers.py
@@ -149,7 +149,25 @@ def _fill_credited_by_sibling(bot_id: int, order_id: str, client_id: str, qty: float) -> bool:
     """
     Fix (retry-queue sibling stand-down, 2026-09-09): True when this fill was
     claimed by a caller other than this queue's attempts AND the ledger row
     already carries the fill. In that state the claim guard (8edcb76 Fix 2,
     working as designed) makes every retry return False — which the OLD code
     read as "still no DB row", burned all retries, and escalated a FALSE
     REQUIRE_MANUAL_PROOF (2026-09-09 bot 10018: gate fired 26s after the
     step-lock winner had already credited; pair parity was green 3s later).
     Only returns True when the credit ACTUALLY landed on the row — a claim
     without the fill (crash mid-commit) keeps retrying and escalates normally.
+    Extended: Also check for step-lock auto_closed siblings — if another order
+    in the same step/cycle was credited and this order was auto_closed as a
+    result, this is NOT a failed credit — it's correct GTX chase handling.
     """
     try:
         from engine.database import get_connection
         conn = get_connection()
         claimed = conn.execute(
             "SELECT caller FROM fill_claims WHERE bot_id=? AND (order_id=? OR order_id=?) "
             "ORDER BY rowid DESC LIMIT 1",
             (bot_id, str(order_id), str(client_id) if client_id else str(order_id)),
         ).fetchone()
         if not claimed:
+            # Check for step-lock auto_closed sibling: another order in same step/cycle
+            # that was credited, causing this order to be auto_closed
+            our_row = conn.execute(
+                "SELECT step, cycle_id FROM bot_orders WHERE bot_id=? AND (order_id=? OR client_order_id=?)",
+                (bot_id, str(order_id), str(client_id) if client_id else str(order_id))
+            ).fetchone()
+            if our_row and our_row[0] is not None and our_row[1] is not None:
+                step, cycle_id = our_row
+                sibling = conn.execute(
+                    "SELECT filled_amount FROM bot_orders WHERE bot_id=? AND step=? AND cycle_id=? "
+                    "AND id != (SELECT id FROM bot_orders WHERE bot_id=? AND (order_id=? OR client_order_id=?)) "
+                    "AND filled_amount > 0 AND status NOT IN ('auto_closed','reset_cleared','cancelled','canceled','failed','rejected')",
+                    (bot_id, step, cycle_id, bot_id, str(order_id), str(client_id) if client_id else str(order_id))
+                ).fetchone()
+                if sibling and float(sibling[0] or 0) >= qty * 0.99:
+                    return True
             return False
         row = conn.execute(
             "SELECT filled_amount, status FROM bot_orders "
@@ -175,7 +193,7 @@ def _fill_credited_by_sibling(bot_id: int, order_id: str, client_id: str, qty: float) -> bool:
         )
         if not row:
             return False
         filled_amount = float(row[0] or 0)
-        return filled_amount >= qty * 0.99 or str(row[1]) in ('filled', 'partially_filled', 'closed')
+        return filled_amount >= qty * 0.99 or str(row[1]) in ('filled', 'partially_filled', 'closed', 'auto_closed')
     except Exception:
         return False
```

### Fix 2: Also Treat Auto-Closed as "Credited" in Stand-Down
If our order was auto_closed due to step saturation, the fill was already credited to the sibling. This is a successful outcome, not a failure.

## Test Plan
1. Simulate GTX chase: two orders for same step, first fills and credited, second fills and auto_closed
2. Park second order in retry queue
3. Verify `_fill_credited_by_sibling` returns True for second order
4. Verify no REQUIRE_MANUAL_PROOF escalation
5. Full regression suite pass

## Proposed Diff (Minimal)

```diff
--- a/engine/ws_event_handlers.py
+++ b/engine/ws_event_handlers.py
@@ -169,11 +169,25 @@ def _fill_credited_by_sibling(bot_id: int, order_id: str, client_id: str, qty: float) -> bool:
         if not claimed:
+            # Check for step-lock auto_closed sibling (GTX chase handling)
+            our_row = conn.execute(
+                "SELECT step, cycle_id, status FROM bot_orders WHERE bot_id=? AND (order_id=? OR client_order_id=?)",
+                (bot_id, str(order_id), str(client_id) if client_id else str(order_id))
+            ).fetchone()
+            if our_row and our_row[0] is not None and our_row[1] is not None:
+                step, cycle_id, our_status = our_row
+                if str(our_status) == 'auto_closed':
+                    # We were auto_closed by step-lock — sibling got the credit
+                    return True
+                # Also check if another order in same step/cycle was credited
+                sibling = conn.execute(
+                    "SELECT filled_amount FROM bot_orders WHERE bot_id=? AND step=? AND cycle_id=? "
+                    "AND id != (SELECT id FROM bot_orders WHERE bot_id=? AND (order_id=? OR client_order_id=?)) "
+                    "AND filled_amount > 0 AND status NOT IN ('auto_closed','reset_cleared','cancelled','canceled','failed','rejected')",
+                    (bot_id, step, cycle_id, bot_id, str(order_id), str(client_id) if client_id else str(order_id))
+                ).fetchone()
+                if sibling and float(sibling[0] or 0) >= qty * 0.99:
+                    return True
             return False
         row = conn.execute(
             "SELECT filled_amount, status FROM bot_orders "
             "WHERE bot_id=? AND (order_id=? OR client_order_id=?) LIMIT 1",
             (bot_id, str(order_id), str(client_id) if client_id else str(order_id)),
         ).fetchone()
         if not row:
             return False
         filled_amount = float(row[0] or 0)
-        return filled_amount >= qty * 0.99 or str(row[1]) in ('filled', 'partially_filled', 'closed')
+        return filled_amount >= qty * 0.99 or str(row[1]) in ('filled', 'partially_filled', 'closed', 'auto_closed')
     except Exception:
         return False
```

## Priority
**MEDIUM** — False positive escalation, not a money-path bug. Fix after P1 items.