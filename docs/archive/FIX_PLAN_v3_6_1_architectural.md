# Root Cause: cycle_id Desync — The Recurring Architecture Bug

## What keeps breaking

Every few sessions you see a variant of this:
- SUI hedge mismatch
- XRP CID bug
- SOL $423 mismatch

They are all the SAME bug. One root cause.

---

## The Root Cause

When a hedge child bot places its SHORT entry, `_signal_hedge_child_entry`
in `bot_executor.py` does this:

1. Saves a `bot_orders` row with `cycle_id = parent.cycle_id` (e.g. 48)
2. Does NOT update the child's `trades.cycle_id` to match

The child's `trades` row still has `cycle_id=1` (initialization default).

When the fill arrives and `credit_fill` runs:
- It finds the `bot_orders` row (cycle_id=48) ✓
- It calls `UPDATE trades SET open_qty = open_qty + delta WHERE bot_id = child_id` ✓
- open_qty IS incremented correctly ✓

Wait — so why is `sol_hedge.open_qty = 0.0`?

Because `seal_trade_state` runs AFTER credit_fill. It calls
`recompute_invested_from_orders(child_id)` which filters:
```sql
WHERE bot_id = ? AND cycle_id = ?
```
The `?` for cycle_id comes from `trades.cycle_id` = 1.
The filled row has `cycle_id=48`.
**The fill is invisible to recompute. It returns qty=0.**
`seal_trade_state` then writes `open_qty=0` over the correct value
that `credit_fill` just set.

This is the kill chain:
```
credit_fill increments open_qty → seal_trade_state overwrites with 0
because recompute_invested_from_orders uses wrong cycle_id
```

---

## Why It Keeps Recurring

`_signal_hedge_child_entry` saves the order but never syncs
the child's `trades.cycle_id`. This has been partially patched
(v3.5.5 CID cycle guard) but the underlying sync was never done.

Every time someone touches `recompute_invested_from_orders`,
`seal_trade_state`, or `_signal_hedge_child_entry` without knowing
this invariant, the same desync reappears in a new form.

---

## The Permanent Architectural Fix

**One rule, enforced in one place:**

When `_signal_hedge_child_entry` saves the child's `bot_orders` row,
it MUST also update the child's `trades.cycle_id` to match the parent.

This is a 3-line addition to `_signal_hedge_child_entry` in
`bot_executor.py` and must be documented as an invariant in
CODEBASE_GUIDE so it is never removed.

### Code fix — `bot_executor.py`, function `_signal_hedge_child_entry`

Find the block that saves the child's bot_orders entry row.
It will look something like:

```python
save_bot_order(
    child_bot_id, 'entry', order_id,
    price, qty, step=child_step,
    cycle_id=parent_cycle_id,
    ...
)
```

Immediately AFTER that `save_bot_order` call, add:

```python
# INVARIANT: child trades.cycle_id MUST match the cycle_id used in
# its bot_orders rows. Without this, recompute_invested_from_orders
# filters by the wrong cycle and returns qty=0, causing seal_trade_state
# to overwrite the correct open_qty with 0.
# This is the permanent fix for the SUI/XRP/SOL hedge desync bug.
try:
    from engine.database import get_connection as _gc_sync
    with _gc_sync() as _sc:
        _sc.execute(
            "UPDATE trades SET cycle_id = ? WHERE bot_id = ?",
            (parent_cycle_id, child_bot_id)
        )
    logger.info(
        f"[HEDGE-CYCLE-SYNC] Child {child_bot_id} trades.cycle_id "
        f"synced to parent cycle {parent_cycle_id}"
    )
except Exception as _sync_err:
    logger.error(
        f"[HEDGE-CYCLE-SYNC] FAILED for child {child_bot_id}: {_sync_err}. "
        f"open_qty WILL be wrong after next seal. Manual fix required."
    )
```

### CODEBASE_GUIDE addition — add to Section 3 as invariant 3.20

```
### 3.20. Hedge Child cycle_id Sync — MANDATORY (v3.6.1)

INVARIANT: A hedge child bot's trades.cycle_id MUST always equal
the cycle_id used in its bot_orders filled rows.

Owner: _signal_hedge_child_entry (bot_executor.py) is the ONLY
function that initializes a child's position. It MUST update
trades.cycle_id immediately after saving the bot_orders row.

Why: recompute_invested_from_orders filters by trades.cycle_id.
If the child's trades.cycle_id differs from its bot_orders rows,
recompute returns qty=0. seal_trade_state then overwrites the
correct credit_fill increment with 0. open_qty becomes 0.
get_pair_virtual_net sees 0. Mismatch alert fires.

This was the root cause of SUI (v3.5.4), XRP (v3.5.5),
and SOL (v3.6.1) hedge mismatch bugs.

NO other writer may change a child bot's cycle_id except:
- _signal_hedge_child_entry (on entry)
- safe_wipe_bot (on full wipe, sets to NULL)
```

---

## Immediate DB fix for current SOL situation

The code fix above prevents recurrence. For the current SOL state,
one targeted DB update is needed:

```sql
-- Step 1: sync sol_hedge trades.cycle_id to match its bot_orders
UPDATE trades SET cycle_id = 48 WHERE bot_id = 100315;

-- Step 2: recompute open_qty from the now-visible fill
-- (run seal_all_active_bots() after this, or have LLM call it)
```

Then call `seal_all_active_bots()`. This will:
- recompute_invested_from_orders for sol_hedge with cycle_id=48
- find the 3.18 filled entry row
- set open_qty=3.18, total_invested=~$257, status=IN TRADE

After that:
- get_pair_virtual_net('SOLUSDC') will include sol_hedge's -3.18 SHORT
- Virtual net = sol(+3.18) + sol_hedge(-3.18) = 0 net
- Exchange net = -2.07 (there may still be a residual from grid fills)
- Mismatch narrows dramatically or clears

The MARGIN HELD on sol will also resolve once sol_hedge is visible
and the TP capacity clip sees the correct physical net.

---

## What this fix does NOT cover

The `short sol` bot (100001) is at REQUIRE_MANUAL_PROOF with
open_qty=0 and invested=0 — it is genuinely flat. That is correct.
Leave it alone.

The `sol_hedge` short_sol_hedge (100324) is also flat — correct.

---

## Version bump

This is v3.6.1. Update CODEBASE_GUIDE header.
Add invariant 3.20 to Section 3.
Add to changelog:

```
### v3.6.1 — YYYY-MM-DD
**Permanent fix for hedge child cycle_id desync (root cause of
SUI/XRP/SOL recurring mismatch bugs).**

engine/bot_executor.py (_signal_hedge_child_entry):
- After save_bot_order for hedge child entry, immediately update
  trades.cycle_id = parent_cycle_id for the child bot.
- Added [HEDGE-CYCLE-SYNC] log on success, ERROR log on failure.
- This is the architectural fix. Without it, seal_trade_state
  overwrites credit_fill's correct open_qty with 0 because
  recompute_invested_from_orders filters by the wrong cycle_id.

CODEBASE_GUIDE:
- Added invariant 3.20: Hedge Child cycle_id Sync
- Documents the kill chain, ownership rule, and history of this bug.

DB recovery (SOL):
- sol_hedge (100315) trades.cycle_id corrected 1 → 48
- seal_all_active_bots() run to recompute from confirmed fills
```
