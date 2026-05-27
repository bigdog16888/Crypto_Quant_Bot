# Implementation Tickets — ADR-002 Hedge Child Bot

Complete tickets in order. Do not start ticket N+1 until ticket N passes all tests and is committed.

---

## TICKET-1: Schema Migration

**File:** `engine/database.py`  
**Commit message:** `feat(hedge-refactor): ticket-1 — schema migration, new bots columns`

### What to add

In `init_db()`, after existing `bots` table migrations, add:

```python
# ADR-002: hedge child bot columns
for col, definition in [
    ('bot_type',           "TEXT DEFAULT 'standard'"),
    ('parent_bot_id',      "INTEGER DEFAULT NULL"),
    ('hedge_child_bot_id', "INTEGER DEFAULT NULL"),
    ('hedge_trigger_step', "INTEGER DEFAULT NULL"),
]:
    try:
        cursor.execute(f'SELECT {col} FROM bots LIMIT 1')
    except sqlite3.OperationalError:
        cursor.execute(f'ALTER TABLE bots ADD COLUMN {col} {definition}')
        conn.commit()
        logger.info(f"🛠️ DB Migration ADR-002: Added {col} to bots table.")
```

### What NOT to change in this ticket

Nothing else. No function logic changes. Schema only.

### Tests

```python
def test_ticket1_schema_columns_exist():
    """All four new columns exist in bots table."""
    conn = get_connection()
    row = conn.execute("SELECT bot_type, parent_bot_id, hedge_child_bot_id, hedge_trigger_step FROM bots LIMIT 1").fetchone()
    # If no bots exist, fetchone() returns None — that's fine, column existence is proven by no exception
    # If bots exist, values should be default
    bots = conn.execute("SELECT bot_type FROM bots").fetchall()
    for b in bots:
        assert b[0] in ('standard', 'hedge_child', None)

def test_ticket1_default_values():
    """Existing bots default to bot_type='standard', NULLs for new FK columns."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT bot_type, parent_bot_id, hedge_child_bot_id, hedge_trigger_step FROM bots WHERE id IS NOT NULL LIMIT 5"
    ).fetchall()
    for bot_type, parent_id, child_id, trigger_step in rows:
        assert bot_type in ('standard', None)  # NULL acceptable for pre-migration rows
        assert parent_id is None
        assert child_id is None
        # trigger_step may be set by existing config — NULL is correct default
```

---

## TICKET-2: Migration Script for Bot 10017

**File:** `scripts/migrate_hedge_to_child_bot.py` (new file)  
**Commit message:** `feat(hedge-refactor): ticket-2 — migration script for existing hedge state`

### What this script does

Run once, manually, after Ticket-1 is deployed. Creates the hedge child bot for any existing bot that has `hedge_qty > 0` in `trades`.

```python
#!/usr/bin/env python3
"""
One-time migration: converts existing hedge_qty state to hedge child bot rows.
Run ONCE after deploying Ticket-1 schema changes.
Usage: python scripts/migrate_hedge_to_child_bot.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from engine.database import get_connection
from engine.ledger import seal_trade_state

def migrate():
    conn = get_connection()
    cursor = conn.cursor()

    # Find all bots with outstanding hedge_qty
    rows = cursor.execute("""
        SELECT b.id, b.name, b.pair, b.normalized_pair, b.direction,
               b.config, COALESCE(t.hedge_qty, 0) as hedge_qty,
               COALESCE(t.cycle_id, 1) as cycle_id
        FROM bots b
        JOIN trades t ON t.bot_id = b.id
        WHERE t.hedge_qty > 0.0001
          AND b.bot_type = 'standard'
          AND b.is_active = 1
    """).fetchall()

    if not rows:
        print("No bots with outstanding hedge_qty found. Nothing to migrate.")
        return

    for bot_id, name, pair, norm_pair, direction, config_json, hedge_qty, cycle_id in rows:
        print(f"\nMigrating bot {bot_id} ({name}): hedge_qty={hedge_qty:.6f}")

        # Hedge child direction is opposite to parent
        child_direction = 'SHORT' if direction.upper() == 'LONG' else 'LONG'
        child_name = f"{name}_hedge"

        # Check if child already exists (idempotent)
        existing_child = cursor.execute(
            "SELECT id FROM bots WHERE parent_bot_id = ? AND bot_type = 'hedge_child'",
            (bot_id,)
        ).fetchone()

        if existing_child:
            print(f"  ✅ Child bot already exists (id={existing_child[0]}). Skipping creation.")
            child_id = existing_child[0]
        else:
            # Create child bot
            cursor.execute("""
                INSERT INTO bots (name, pair, normalized_pair, direction, bot_type,
                                  parent_bot_id, is_active, status, config,
                                  rsi_limit, martingale_multiplier, base_size, strategy_type)
                VALUES (?, ?, ?, ?, 'hedge_child', ?, 1, 'IN TRADE', ?, 0, 1.0, 0, 'Martingale')
            """, (child_name, pair, norm_pair, child_direction, bot_id, config_json))
            child_id = cursor.lastrowid
            print(f"  ✅ Created child bot id={child_id} ({child_name}, {child_direction})")

            # Create trades row for child
            child_position_side = child_direction
            cursor.execute("""
                INSERT INTO trades (bot_id, open_qty, hedge_qty, cycle_id, position_side,
                                    total_invested, avg_entry_price, current_step, entry_confirmed)
                VALUES (?, ?, 0, 1, ?, 0, 0, 1, 1)
            """, (child_id, hedge_qty, child_position_side))

            # Create audit entry in bot_orders representing net inherited position
            audit_cid = f"CQB_{child_id}_HEDGE_MIGRATE_{int(time.time())}"
            cursor.execute("""
                INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id,
                                        price, amount, filled_amount, status, step, cycle_id,
                                        created_at, notes, position_side)
                VALUES (?, 'entry', ?, ?, 0, ?, ?, 'filled', 1, 1, ?, ?, ?)
            """, (
                child_id,
                audit_cid, audit_cid,
                hedge_qty, hedge_qty,
                int(time.time()),
                f"HEDGE_MIGRATION: inherited {hedge_qty:.6f} {child_direction} from parent bot {bot_id} ({name})",
                child_position_side
            ))

        # Link parent to child
        cursor.execute(
            "UPDATE bots SET hedge_child_bot_id = ? WHERE id = ?",
            (child_id, bot_id)
        )

        # Zero hedge_qty on parent
        cursor.execute(
            "UPDATE trades SET hedge_qty = 0 WHERE bot_id = ?",
            (bot_id,)
        )

        # Update active_positions: reassign orphan SHORT to child bot
        updated = cursor.execute("""
            UPDATE active_positions
            SET bot_id = ?
            WHERE pair = ? AND side = ? AND bot_id = 0
        """, (child_id, norm_pair, child_direction)).rowcount
        print(f"  ✅ Reassigned {updated} active_positions row(s) to child bot.")

        conn.commit()
        print(f"  ✅ Parent bot {bot_id} linked to child {child_id}. hedge_qty zeroed.")

        # Seal both bots
        seal_trade_state(bot_id)
        seal_trade_state(child_id)
        print(f"  ✅ Sealed both bots.")

    print(f"\n✅ Migration complete. {len(rows)} parent bot(s) migrated.")
    print("Next step: deploy Ticket-3 (recompute cleanup).")

if __name__ == '__main__':
    migrate()
```

### Tests

```python
def test_ticket2_migration_idempotent():
    """Running migration twice produces identical state."""
    # Run once
    migrate()
    child_id_1 = get_connection().execute(
        "SELECT id FROM bots WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
    ).fetchone()
    # Run again
    migrate()
    child_id_2 = get_connection().execute(
        "SELECT id FROM bots WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
    ).fetchone()
    assert child_id_1 == child_id_2  # Same child, not duplicated

def test_ticket2_parent_hedge_qty_zeroed():
    migrate()
    row = get_connection().execute(
        "SELECT hedge_qty FROM trades WHERE bot_id=10017"
    ).fetchone()
    assert float(row[0] or 0) < 0.0001

def test_ticket2_child_open_qty_matches_former_hedge():
    migrate()
    row = get_connection().execute(
        "SELECT open_qty FROM trades t JOIN bots b ON b.id=t.bot_id "
        "WHERE b.parent_bot_id=10017 AND b.bot_type='hedge_child'"
    ).fetchone()
    assert row is not None
    assert abs(float(row[0]) - 44.7) < 0.01

def test_ticket2_active_positions_no_orphan():
    migrate()
    orphan = get_connection().execute(
        "SELECT COUNT(*) FROM active_positions WHERE bot_id=0 AND pair='XRPUSDC'"
    ).fetchone()[0]
    assert orphan == 0
```

---

## TICKET-3: Remove `h_qty` from `recompute_invested_from_orders`

**File:** `engine/database.py`  
**Commit message:** `feat(hedge-refactor): ticket-3 — recompute returns 4-tuple, remove h_qty`

### What to change

`recompute_invested_from_orders()` currently returns `(cost, avg, qty, step, h_qty)`.

**Change to return `(cost, avg, qty, step)`** — 4-tuple.

Steps:
1. Remove all hedge-specific SQL CASE blocks from the main query (lines referencing `order_type LIKE 'hedge%'`)
2. Remove the `NULL cycle_id` hedge-only path (lines 3310-3330) — replace with simply `return 0.0, 0.0, 0.0, 0`
3. Remove `hedge_qty` from the SELECT result row unpacking (line 3404)
4. Remove `total_net_qty = round(total_qty - hedge_qty, 8)` — `total_net_qty` is just `total_qty` now
5. Remove all `return ..., hedge_qty` tail returns — replace with `return ..., 0` temporarily while callers are being updated, then remove entirely once all callers updated in this ticket
6. Delete `get_bot_hedge_qty()` function entirely (lines 3577-3583)
7. Delete `basket_open_qty_from_recompute()` function entirely (lines 3278-3286)

**Update all callers in `database.py`:**
- Line 1624: `cost, avg, qty, step, h_qty = recompute_...` → `cost, avg, qty, step = recompute_...`
- Line 2404: `_, _, net_qty, _, hedge_qty = recompute_...` → `_, _, net_qty, _ = recompute_...`
- Line 2405: `share_qty = basket_open_qty_from_recompute(net_qty, hedge_qty)` → `share_qty = max(0.0, net_qty)`
- Line 3562: `hedge_qty = get_bot_hedge_qty(bot_id)` block — remove entirely; use `open_qty` directly from trades
- Line 3608: `main_open_qty = basket_open_qty_from_recompute(recomputed_qty, recomputed_hedge)` → `main_open_qty = max(0.0, recomputed_qty)`

### Tests

```python
def test_ticket3_recompute_returns_4_tuple():
    from engine.database import recompute_invested_from_orders
    result = recompute_invested_from_orders(bot_id=1)  # any valid bot_id
    assert len(result) == 4, f"Expected 4-tuple, got {len(result)}-tuple"

def test_ticket3_get_bot_hedge_qty_deleted():
    import engine.database as db
    assert not hasattr(db, 'get_bot_hedge_qty'), "get_bot_hedge_qty should be deleted"

def test_ticket3_basket_open_qty_deleted():
    import engine.database as db
    assert not hasattr(db, 'basket_open_qty_from_recompute'), "basket_open_qty_from_recompute should be deleted"

def test_ticket3_recompute_not_affected_by_old_hedge_orders():
    """Bot 10017 recompute should return open_qty=0 (basket), ignoring any legacy hedge rows."""
    from engine.database import recompute_invested_from_orders
    cost, avg, qty, step = recompute_invested_from_orders(10017)
    assert qty < 0.001, f"Parent bot should have 0 basket qty after migration, got {qty}"
```

---

## TICKET-4: Remove `h_qty` from `ledger.py`

**File:** `engine/ledger.py`  
**Commit message:** `feat(hedge-refactor): ticket-4 — remove h_qty threading from ledger`

### What to change

`seal_trade_state()`:
1. Line 427: `cost, avg, qty, step, h_qty = recompute_...` → `cost, avg, qty, step = recompute_...`
2. Line 432-433: Remove `basket_open_qty_from_recompute` import and call → `main_open_qty = max(0.0, qty)`
3. Lines 460-461: Remove `hedge_qty = ?` from the UPDATE statement (keep `open_qty = ?`)
4. Line 517: `if main_open_qty <= 1e-8 and abs(h_qty) <= 1e-8:` → `if main_open_qty <= 1e-8:`
5. Lines 532, 543: Remove `hedge_qty = ?` parameter from UPDATE SQL and its binding
6. Line 548: `if cost > 0.01 or abs(h_qty) > 1e-8:` → `if cost > 0.01:`

`handle_tp_completion()`:
1. Line 816: `invested, avg_entry, qty, current_step, h_qty = recompute_...` → `invested, avg_entry, qty, current_step = recompute_...`

`credit_fill()` (lines 210-213):
- `_EXIT_TYPES` tuple: remove `'hedge'` and `'hedge_tp'` — these order types no longer exist

Line 791:
- `AND order_type NOT IN ('hedge', 'hedge_tp')` guard: remove this exclusion

### Tests

```python
def test_ticket4_seal_trade_state_no_hedge_qty_write():
    """seal_trade_state does not write hedge_qty to trades."""
    from engine.ledger import seal_trade_state
    from engine.database import get_connection
    # Get initial hedge_qty (should be 0 after ticket-2 migration)
    before = get_connection().execute(
        "SELECT hedge_qty FROM trades WHERE bot_id=10017"
    ).fetchone()[0]
    seal_trade_state(10017)
    after = get_connection().execute(
        "SELECT hedge_qty FROM trades WHERE bot_id=10017"
    ).fetchone()[0]
    # hedge_qty must not have changed (column deprecated, not written)
    assert before == after

def test_ticket4_seal_trade_state_correct_open_qty():
    """seal_trade_state writes correct open_qty for a standard bot."""
    from engine.ledger import seal_trade_state
    result = seal_trade_state(10017)
    assert result.get('qty', -1) >= 0

def test_ticket4_exit_types_no_hedge():
    """'hedge' and 'hedge_tp' are not in credit_fill EXIT_TYPES."""
    # Inspect the source — if the constants are importable:
    import inspect
    from engine import ledger
    src = inspect.getsource(ledger.credit_fill)
    assert "'hedge'" not in src or "DEPRECATED" in src
    assert "'hedge_tp'" not in src or "DEPRECATED" in src
```

---

## TICKET-5: Remove `h_qty` from `reconciler.py`

**File:** `engine/reconciler.py`  
**Commit message:** `feat(hedge-refactor): ticket-5 — remove h_qty from reconciler`

### What to change

All sites where `recompute_invested_from_orders` is called:

- Line 2193: `true_cost, true_avg, true_qty, true_step, h_qty = recompute_...` → 4-tuple
- Line 2231: same
- Line 6336: `true_cost, true_avg_price, true_qty, true_step, hedge_qty = recompute_...` → 4-tuple
- Line 7091: `_, _, true_qty, _, h_qty = recompute_...` → `_, _, true_qty, _ = recompute_...`
- Line 7669: `_, _, b_qty, _, h_qty = recompute_...` → `_, _, b_qty, _ = recompute_...`

Remove hedge-specific net calculations:
- Lines 2983-3019: remove `internal_hedge_qty` fetch and the `+ float(internal_hedge_qty)` additions to `max_possible_qty` — hedge child's own `open_qty` is already counted correctly in pair virtual net
- Lines 3451-3475: remove the per-bot outstanding hedge calculation block
- Lines 3885: remove hedge order type fetch for global flatten check
- Lines 5334-5346: replace `WIPE-ABORT` hedge guard (`float(b_status.get('hedge_qty', 0)) > 1e-8`) with check on hedge child status: `get_connection().execute("SELECT status FROM bots WHERE parent_bot_id=? AND bot_type='hedge_child'", (b.bot_id,)).fetchone()` — abort wipe if child is `IN TRADE`

Remove from hist_net calculation (lines 7117-7173):
- Remove `- float(h_qty)` from `hist_net` formula: `hist_net = float(hist_opened) - float(hist_closed)`
- Remove the comment block explaining the h_qty deduction
- Line 7671: `if b_qty <= 0 and abs(h_qty) <= 1e-8` → `if b_qty <= 0`

`adopt_from_physical_positions()` global netting (line 3929):
- Remove: `logger.info(f"🛡️ [GLOBAL-FLATTEN SKIPPED] {b.name} is legally HEDGED...")` — no longer needed, hedge child is a proper SHORT bot

### Tests

```python
def test_ticket5_recompute_callers_use_4_tuple():
    """Reconciler does not unpack 5 values from recompute."""
    import inspect
    from engine import reconciler
    src = inspect.getsource(reconciler.StateReconciler)
    # No 5-element unpack of recompute result
    import re
    five_tuple_unpacks = re.findall(
        r'[\w]+,\s*[\w]+,\s*[\w]+,\s*[\w]+,\s*[\w]+\s*=\s*recompute_invested_from_orders',
        src
    )
    assert len(five_tuple_unpacks) == 0, f"Found 5-tuple unpacks: {five_tuple_unpacks}"

def test_ticket5_global_netting_no_mismatch_after_migration():
    """After migration, pair virtual net matches exchange for XRP."""
    from engine.database import get_pair_virtual_net
    # With hedge child owning 44.7 SHORT and parent owning 0 basket,
    # virtual net for XRP = 0 (parent) + (-44.7) (child) = -44.7
    # This should match exchange physical
    net = get_pair_virtual_net('XRP/USDC:USDC')
    assert abs(net - (-44.7)) < 0.01, f"Expected -44.7, got {net}"
```

---

## TICKET-6: Fix `apply_oneway_entry_cross_reduction` — Parent/Child Suppression

**File:** `engine/oneway_netting.py`  
**Commit message:** `feat(hedge-refactor): ticket-6 — suppress cross-reduction between parent and hedge child`

### What to change

In `apply_oneway_entry_cross_reduction()`, update the neighbor SQL query to exclude the filling bot's hedge child:

```python
# BEFORE (line 128-136):
for bid, bdir, raw_pair, bot_norm, oq in conn.execute(
    """
    SELECT b.id, b.direction, b.pair, b.normalized_pair, COALESCE(t.open_qty, 0)
    FROM bots b
    JOIN trades t ON t.bot_id = b.id
    WHERE b.is_active = 1 AND b.id != ?
    """,
    (filling_bot_id,),
).fetchall():

# AFTER:
for bid, bdir, raw_pair, bot_norm, oq, b_status in conn.execute(
    """
    SELECT b.id, b.direction, b.pair, b.normalized_pair,
           COALESCE(t.open_qty, 0), b.status
    FROM bots b
    JOIN trades t ON t.bot_id = b.id
    WHERE b.is_active = 1
      AND b.id != ?
      AND b.id != COALESCE(
          (SELECT hedge_child_bot_id FROM bots WHERE id = ?), -1
      )
    """,
    (filling_bot_id, filling_bot_id),
).fetchall():
    # Also skip SCANNING and REQUIRE_MANUAL_PROOF bots (Fix-4 from v3.5.8)
    if str(b_status).upper() in ('SCANNING', '🟢 SCANNING', 'STOPPED',
                                  'REQUIRE_MANUAL_PROOF', 'HEDGE_STANDBY'):
        continue
```

Also update `gate_oneway_opposite_entry()` similarly — the hedge child should not block the parent's entries:

```python
# In gate_oneway_opposite_entry, existing neighbor query:
# Add same exclusion:
AND b.id != COALESCE((SELECT hedge_child_bot_id FROM bots WHERE id = ?), -1)
# and add ? binding for filling_bot_id
```

### Tests

```python
def test_ticket6_parent_fill_does_not_reduce_hedge_child():
    """When parent (LONG) fills an entry, hedge child open_qty is not touched."""
    from engine.database import get_connection
    from engine.oneway_netting import apply_oneway_entry_cross_reduction

    conn = get_connection()
    # Get hedge child id and its current open_qty
    child_row = conn.execute(
        "SELECT id FROM bots WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
    ).fetchone()
    assert child_row, "Hedge child must exist (run ticket-2 migration first)"
    child_id = child_row[0]

    before_qty = float(conn.execute(
        "SELECT open_qty FROM trades WHERE bot_id=?", (child_id,)
    ).fetchone()[0] or 0)

    # Simulate parent filling 1.0 unit LONG
    apply_oneway_entry_cross_reduction(
        filling_bot_id=10017,
        pair='XRP/USDC:USDC',
        direction='LONG',
        delta=1.0,
        source_order_id='TEST_ORDER_001',
    )

    after_qty = float(conn.execute(
        "SELECT open_qty FROM trades WHERE bot_id=?", (child_id,)
    ).fetchone()[0] or 0)

    assert abs(before_qty - after_qty) < 0.0001, (
        f"Hedge child open_qty changed: {before_qty} -> {after_qty}. "
        f"Cross-reduction must be suppressed between parent and child."
    )

def test_ticket6_parent_fill_still_reduces_unrelated_short():
    """Cross-reduction still applies between unrelated bots on same pair."""
    # This test requires an unrelated SHORT bot on the same pair
    # If none exists, skip with pytest.skip
    pass  # Implement with test fixtures if available

def test_ticket6_scanning_bot_not_reduced():
    """SCANNING bots are skipped by cross-reduction."""
    from engine.oneway_netting import apply_oneway_entry_cross_reduction
    from engine.database import get_connection
    # Get short_sol bot (100001, status=Scanning, open_qty=0)
    conn = get_connection()
    before = float(conn.execute(
        "SELECT open_qty FROM trades WHERE bot_id=100001"
    ).fetchone()[0] or 0)
    apply_oneway_entry_cross_reduction(
        filling_bot_id=10008,  # sol long
        pair='SOL/USDC:USDC',
        direction='LONG',
        delta=0.5,
        source_order_id='TEST_ORDER_002',
    )
    after = float(conn.execute(
        "SELECT open_qty FROM trades WHERE bot_id=100001"
    ).fetchone()[0] or 0)
    assert abs(before - after) < 0.0001
```

---

## TICKET-7: Hedge Child Entry Signal in `bot_executor.py`

**File:** `engine/bot_executor.py`  
**Commit message:** `feat(hedge-refactor): ticket-7 — parent signals hedge child entry on trigger step`

### What to change

**Replace** the `execute_hedge_lock()` call site (lines 3356-3383) with a new function `_signal_hedge_child_entry()`.

**Remove:**
- `execute_hedge_lock()` method entirely (lines 3388-3641)
- `_hedge_cooldown_ts` dict from `__init__` (line 84)
- `_HEDGE_COOLDOWN_SECS` constant (line 85)
- Import of `check_hedge_entry`, `calculate_hedge_lot` from `engine.manager`

**Add** new method `_signal_hedge_child_entry()`:

```python
def _signal_hedge_child_entry(
    self,
    parent_bot_id: int,
    parent_name: str,
    parent_step: int,
    pair: str,
    direction: str,
    step_qty: float,       # qty that filled on this step
    step_fill_price: float,
    exchange: ExchangeInterface,
    cycle_id: int,
) -> bool:
    """
    Signal the hedge child bot to place a SHORT entry mirroring the parent's
    filled step. Called when parent_step >= hedge_trigger_step.

    Returns True if entry was placed or already exists for this step.
    """
    from engine.database import get_connection, save_bot_order
    from engine.ledger import credit_fill

    conn = get_connection()

    # Fetch hedge child bot id
    row = conn.execute(
        "SELECT hedge_child_bot_id FROM bots WHERE id = ?", (parent_bot_id,)
    ).fetchone()
    if not row or not row[0]:
        logger.warning(
            f"[HEDGE-SIGNAL] Parent {parent_name} has no hedge_child_bot_id configured. "
            f"Cannot signal hedge entry."
        )
        return False

    child_bot_id = row[0]

    # Idempotency: check if this step already has a hedge entry for this cycle
    existing = conn.execute(
        "SELECT id FROM bot_orders WHERE bot_id=? AND step=? AND cycle_id=? "
        "AND order_type='entry' AND status NOT IN ('cancelled','failed','reset_cleared')",
        (child_bot_id, parent_step, cycle_id)
    ).fetchone()
    if existing:
        logger.debug(
            f"[HEDGE-SIGNAL] Child {child_bot_id}: entry for step {parent_step} "
            f"cycle {cycle_id} already exists. Skipping."
        )
        return True

    # Determine child direction (opposite to parent)
    child_direction = 'SHORT' if direction.upper() == 'LONG' else 'LONG'
    child_side = 'sell' if child_direction == 'SHORT' else 'buy'

    # Round qty to exchange precision
    prec = exchange.get_symbol_precision(pair)
    qty_step = float(prec.get('step_size', 0.001) or 0.001)
    entry_qty = exchange.round_to_step(step_qty, qty_step)
    if entry_qty <= 0:
        logger.warning(f"[HEDGE-SIGNAL] Entry qty rounded to 0 for step {parent_step}. Skipping.")
        return False

    # Place limit order on exchange at parent's fill price (post-only GTX)
    cid = f"CQB_{child_bot_id}_ENTRY_{cycle_id}_{parent_step}"
    is_testnet = getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)
    params = {'postOnly': True, 'timeInForce': 'GTX', 'newClientOrderId': cid}
    if is_testnet:
        params['positionSide'] = 'BOTH'

    try:
        order = self._place_gtx_order_with_retry(
            exchange, pair, child_side, entry_qty, step_fill_price,
            params=params, label=f"HEDGE-ENTRY-{parent_name}-step{parent_step}"
        )
    except Exception as e:
        logger.error(
            f"[HEDGE-SIGNAL] Child {child_bot_id}: entry placement failed: {e}. "
            f"Will retry next cycle."
        )
        return False

    exchange_order_id = str(order['id'])
    actual_cid = order.get('_fallback_cid') or cid

    save_bot_order(
        child_bot_id, 'entry', exchange_order_id, step_fill_price, entry_qty,
        step=parent_step, status=order.get('status', 'open'),
        client_order_id=actual_cid,
        notes=f"Hedge entry mirroring parent {parent_bot_id} step {parent_step}",
        cycle_id=cycle_id,
    )

    logger.info(
        f"✅ [HEDGE-SIGNAL] Child {child_bot_id}: entry placed "
        f"{entry_qty:.6f} {child_direction} @ {step_fill_price:.4f} "
        f"(parent step {parent_step}, cid={actual_cid})"
    )
    return True
```

**Update trigger call site** (replaces lines 3356-3383):

```python
# 🛡️ HEDGE CHILD SIGNAL: if parent has a hedge child and step >= trigger, signal entry
hedge_child_id = bot_config.get('hedge_child_bot_id') or bot_status.get('hedge_child_bot_id')
hedge_trigger = bot_config.get('hedge_trigger_step') or conn.execute(
    "SELECT hedge_trigger_step FROM bots WHERE id=?", (bot_id,)
).fetchone()
hedge_trigger = int(hedge_trigger[0] or 0) if hedge_trigger else 0

if hedge_child_id and hedge_trigger > 0:
    current_step = int(bot_status.get('current_step', 0))
    if current_step >= hedge_trigger:
        # Signal child for this step if not already done
        step_qty = float(bot_status.get('open_qty', 0))  # filled qty this step
        self._signal_hedge_child_entry(
            parent_bot_id=bot_id,
            parent_name=name,
            parent_step=current_step,
            pair=pair,
            direction=direction,
            step_qty=step_qty,
            step_fill_price=current_price,
            exchange=exchange,
            cycle_id=int(bot_status.get('cycle_id', 1)),
        )
```

### Tests

```python
def test_ticket7_signal_hedge_child_entry_idempotent():
    """Calling signal twice for same step does not create duplicate entries."""
    # Requires mock exchange and a configured parent/child pair
    pass  # Implement with pytest fixtures and mock exchange

def test_ticket7_no_hedge_child_no_error():
    """Bot without hedge_child_bot_id configured does not error."""
    # Standard bot should complete maintain_orders without hedge signal
    pass
```

---

## TICKET-8: Break-Even TP Signal on Parent TP Completion

**File:** `engine/ledger.py`  
**Commit message:** `feat(hedge-refactor): ticket-8 — signal hedge child break-even TP when parent TP fires`

### What to add

In `handle_tp_completion()`, after the parent's `reset_bot_after_tp()` call succeeds, add:

```python
# 🛡️ HEDGE CHILD: place break-even TP if parent has an active hedge child
try:
    from engine.database import get_connection as _gc_hc
    _hc_conn = _gc_hc()
    _hc_row = _hc_conn.execute(
        "SELECT hedge_child_bot_id FROM bots WHERE id=?", (bot_id,)
    ).fetchone()
    if _hc_row and _hc_row[0]:
        child_id = _hc_row[0]
        child_state = _hc_conn.execute(
            "SELECT open_qty, avg_entry_price, cycle_id, status FROM trades t "
            "JOIN bots b ON b.id=t.bot_id WHERE t.bot_id=?", (child_id,)
        ).fetchone()
        if child_state:
            child_open_qty, child_avg, child_cycle, child_status = child_state
            child_open_qty = float(child_open_qty or 0)
            child_avg = float(child_avg or 0)
            if child_open_qty > 0.0001 and child_avg > 0:
                # Break-even TP = avg_entry_price of the hedge child
                be_price = child_avg
                child_direction = _hc_conn.execute(
                    "SELECT direction FROM bots WHERE id=?", (child_id,)
                ).fetchone()[0]
                # TP side is opposite to child's position direction
                tp_side = 'buy' if child_direction == 'SHORT' else 'sell'
                be_cid = f"CQB_{child_id}_TP_{child_cycle}_BE"

                # Check if BE TP already exists
                existing_be = _hc_conn.execute(
                    "SELECT id FROM bot_orders WHERE bot_id=? AND client_order_id=?",
                    (child_id, be_cid)
                ).fetchone()
                if not existing_be:
                    # Register intent — actual order placed by bot_executor on next cycle
                    # (exchange object not available here in ledger context)
                    from engine.database import save_bot_order
                    save_bot_order(
                        child_id, 'tp', f'PENDING_BE_{child_id}_{child_cycle}',
                        be_price, child_open_qty, step=0,
                        status='pending_placement',
                        client_order_id=be_cid,
                        notes=f"Break-even TP pending placement: parent {bot_id} TP hit",
                        cycle_id=child_cycle,
                    )
                    logger.info(
                        f"[HEDGE-BE-TP] Child {child_id}: break-even TP registered "
                        f"@ {be_price:.4f} for {child_open_qty:.6f} {child_direction}. "
                        f"Will be placed by bot_executor on next cycle."
                    )
except Exception as _hc_err:
    logger.warning(f"[HEDGE-BE-TP] Failed to register child TP (non-fatal): {_hc_err}")
```

**In `bot_executor.maintain_orders()`**, add handling for hedge child bots (`bot_type='hedge_child'`):
- Check for `pending_placement` TP orders and place them on the exchange
- Otherwise: do not scan for entries, do not place grids
- Only manage existing open TP orders (cancel/replace if price drifted, which it won't for BE TP)

### Tests

```python
def test_ticket8_parent_tp_registers_child_be_tp():
    """When parent TP fires, a pending_placement TP is created for hedge child."""
    from engine.ledger import handle_tp_completion
    # Requires mock exchange and configured parent/child
    pass

def test_ticket8_be_tp_price_equals_child_avg_entry():
    """Break-even TP price = child's avg_entry_price."""
    from engine.database import get_connection
    conn = get_connection()
    # After parent TP fires, check pending TP price
    child_id = conn.execute(
        "SELECT id FROM bots WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
    ).fetchone()[0]
    child_avg = float(conn.execute(
        "SELECT avg_entry_price FROM trades WHERE bot_id=?", (child_id,)
    ).fetchone()[0] or 0)
    pending_tp = conn.execute(
        "SELECT price FROM bot_orders WHERE bot_id=? AND status='pending_placement' AND order_type='tp'",
        (child_id,)
    ).fetchone()
    if pending_tp:
        assert abs(float(pending_tp[0]) - child_avg) < 0.0001
```

---

## TICKET-9: Snapshot Writer Fix

**File:** `engine/database.py` — `update_active_positions_snapshot()`  
**Commit message:** `feat(hedge-refactor): ticket-9 — snapshot writer assigns hedge child positions correctly`

### What to change

In `update_active_positions_snapshot()`, the owner lookup currently queries:
```python
cursor.execute(
    "SELECT id FROM bots WHERE normalized_pair = ? AND direction = ? AND is_active = 1 LIMIT 1",
    (symbol, side)
)
```

This already works correctly after Ticket-2 migration — the hedge child IS a SHORT bot with the correct `direction` and `is_active=1`. The snapshot writer will find it naturally.

**Verify and remove** the special-case comment at line 2369:
```python
# "Drift" on SHORT bots that are part of a healthy hedge.
```
This comment and any associated logic that suppresses drift warnings for hedged pairs can be removed.

**Verify** `bot_id=0` no longer occurs for XRP SHORT after migration. Add a guard log:

```python
if owner_id == 0:
    logger.warning(
        f"⚠️ [BRIDGE-MISS] No bot owner found for {symbol} {side} (Qty: {amount}). "
        f"If this is a hedge position, ensure the hedge child bot is created via "
        f"scripts/migrate_hedge_to_child_bot.py (ADR-002)."
    )
```

### Tests

```python
def test_ticket9_xrp_short_assigned_to_child_not_zero():
    """XRPUSDC SHORT position is assigned to hedge child bot, not bot_id=0."""
    from engine.database import get_connection
    # Simulate a snapshot update with a SHORT XRP position
    from engine.database import update_active_positions_snapshot
    mock_positions = [{
        'symbol': 'XRP/USDC:USDC',
        'side': 'short',
        'contracts': 44.7,
        'entryPrice': 2.20,
    }]
    update_active_positions_snapshot(mock_positions)
    conn = get_connection()
    row = conn.execute(
        "SELECT bot_id FROM active_positions WHERE pair='XRPUSDC' AND side='SHORT'"
    ).fetchone()
    assert row is not None
    assert row[0] != 0, f"bot_id should not be 0 after migration. Got: {row[0]}"

def test_ticket9_no_orphan_positions():
    """No active_positions rows have bot_id=0 after a clean snapshot."""
    from engine.database import get_connection
    conn = get_connection()
    orphans = conn.execute(
        "SELECT COUNT(*) FROM active_positions WHERE bot_id=0"
    ).fetchone()[0]
    assert orphans == 0, f"Found {orphans} orphan position(s) with bot_id=0"
```

---

## TICKET-10: Deprecation Sweep

**Files:** All  
**Commit message:** `feat(hedge-refactor): ticket-10 — remove deprecated hedge code`

### What to remove

Only after tickets 1-9 all pass and are committed.

1. **`database.py`**: Remove `order_type LIKE 'hedge%'` from `recompute_invested_from_orders` (already done in Ticket-3 but confirm no remnants)
2. **`database.py`**: Remove `hedge_qty` from `get_bot_status()` return dict (keep column, remove from dict)
3. **`database.py`**: Remove `cycle_phase='HEDGED'` assignment (line 3627)
4. **`database.py`**: Remove `cycle_phase='HEDGE_EXIT_PENDING'` assignment (line 1766)
5. **`bot_executor.py`**: Confirm `execute_hedge_lock()` is fully removed
6. **`bot_executor.py`**: Remove `cycle_phase in ('HEDGED', 'HEDGE_EXIT_PENDING')` check (line 2185)
7. **`bot_executor.py`**: Remove `MARGIN_HELD` hedge guard (line 2717)
8. **`reconciler.py`**: Remove `[GLOBAL-FLATTEN SKIPPED] ... is legally HEDGED` branch (line 3929)
9. **`parity_gates.py`**: Remove `get_bot_hedge_qty` import and usage — `get_bot_signed_contribution()` should use `open_qty` directly from `trades`

Updated `get_bot_signed_contribution()` in `parity_gates.py`:
```python
def get_bot_signed_contribution(bot_id: int) -> float:
    """Signed virtual qty this bot contributes to pair netting."""
    from engine.database import get_connection
    conn = get_connection()
    row = conn.execute(
        "SELECT b.direction, COALESCE(t.open_qty, 0) FROM bots b "
        "JOIN trades t ON t.bot_id = b.id WHERE b.id = ?",
        (bot_id,),
    ).fetchone()
    if not row:
        return 0.0
    direction = str(row[0] or 'LONG').upper()
    open_qty = float(row[1] or 0)
    # Hedge child bots have direction='SHORT' and their own open_qty.
    # No special hedge_qty subtraction needed — they are proper bots.
    return round(open_qty if direction == 'LONG' else -open_qty, 8)
```

### Tests

```python
def test_ticket10_full_test_suite():
    """All existing tests pass. pytest exit code 0."""
    pass  # Run: pytest --tb=short -q

def test_ticket10_no_hedge_order_types_written():
    """New bot_orders rows do not use order_type='hedge' or 'hedge_tp'."""
    # After running a full cycle simulation, check bot_orders
    from engine.database import get_connection
    conn = get_connection()
    new_hedge_rows = conn.execute(
        "SELECT COUNT(*) FROM bot_orders WHERE order_type IN ('hedge', 'hedge_tp') "
        "AND created_at > ?", (int(time.time()) - 3600,)
    ).fetchone()[0]
    assert new_hedge_rows == 0

def test_ticket10_no_hedged_status_in_bots():
    """No bot has status='HEDGED' or 'HEDGE_EXIT_PENDING'."""
    from engine.database import get_connection
    conn = get_connection()
    hedged = conn.execute(
        "SELECT COUNT(*) FROM bots WHERE status IN ('HEDGED', 'HEDGE_EXIT_PENDING')"
    ).fetchone()[0]
    assert hedged == 0
```
