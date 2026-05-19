# Deadlock & Gap Prevention — Root Cause Analysis & Definitive Fix
**For: Implementing LLM Agent**  
**Version: 1.0 | Based on full review of bot_executor.py, runner.py, reconciler.py, database.py**

> This document explains WHY gaps are created in the first place, and provides
> the architectural fixes to prevent them permanently. Read every section.

---

## 0. The Core Problem in One Paragraph

When a bot is IN TRADE, it must always have exactly 2 active orders on the exchange: one TP and one Grid. If either goes missing and the engine fails to replace it — for any reason — the bot enters a **NO ORDERS** deadlock. During a deadlock, the engine loop keeps running, market moves, grid orders on the exchange fill at new prices, but the bot's ledger never records those fills because the WebSocket handler has no `bot_orders` row to match them against. When the deadlock breaks (usually only on engine restart), the position on the exchange no longer matches what the ledger thinks it is. That is every gap you have ever seen.

There are **four separate deadlock causes** in the current code, each producing gaps differently. All four must be fixed.

---

## 1. Deadlock Cause #1: The `EE_WAIT` Timer Blocks Order Placement

### Where it is
`bot_executor.py` — the `maintain_orders` function, around the EE (Early Exit) timer check.

### What happens
When a bot's basket age exceeds `EE_MaxTime`, the engine sets the bot into a wait state and returns early from `maintain_orders` **without placing any orders**. If the bot's TP or Grid order gets cancelled by the exchange during this wait period (e.g. GTX maker order rejected by price movement), the engine loop keeps returning early because the EE timer says "wait". The bot has zero orders on the exchange for the entire wait duration. Any grid fills during this time are anonymous — no `bot_orders` row exists to receive the WebSocket event.

### The Fix
The EE wait must **never block order maintenance**. Orders must always be present on the exchange regardless of EE state. The EE timer should only affect the TP *price*, not whether a TP order *exists*.

In `maintain_orders`, find the EE wait early-return block. Replace any `return None` that happens because of EE timer with a pass-through that still executes the TP and Grid maintenance below it, just at the current (non-decayed) TP price.

```python
# WRONG — returns early, leaving bot with no orders
if ee_wait_active:
    logger.info(f"[EE-WAIT] {name}: Waiting for next interval. Skipping maintain.")
    return None

# CORRECT — EE affects price only, not order existence  
if ee_wait_active:
    logger.debug(f"[EE-WAIT] {name}: Using current TP price (no decay this cycle).")
    # Fall through — maintain orders normally at current price
    tp_price = raw_db_tp  # use undecayed price
```

---

## 2. Deadlock Cause #2: `entry_confirmed = 0` Ghost Lock

### Where it is
`bot_executor.py` `maintain_orders` — the STEP-PROGRESSION-PROOF block (lines ~2778–2840).

### What happens
The engine checks `entry_confirmed` before placing Grid orders. If `entry_confirmed = 0`, it runs a 3-tier proof check. Tier 1 is a DB flag. Tier 2 queries `bot_orders` for a filled row at `current_step`. Tier 3 is a math check.

The deadlock occurs when:
1. Bot is at Step N, has a real position, but `entry_confirmed` was cleared by a restart or integrity check
2. Tier 2 looks for a `bot_orders` row for `step=N` — but the row exists for a DIFFERENT step numbering (e.g. grid filled as step 6 but DB thinks current_step=7 due to carry arithmetic)
3. Tier 3 math check passes but does NOT place the grid — it just heals the flag
4. Next cycle: Tier 1 passes, grid is placed — but **an entire cycle elapsed with no grid order**

If this coincides with an exchange fill, that fill has no bot_orders row to match.

### The Fix
Tier 3 must also **immediately trigger `maintain_orders` re-entry** after healing, not wait for the next cycle. Add a `healed_this_cycle` flag and bypass the return:

```python
# After Tier 3 heal:
logger.warning(f"[T3-HEAL] {name}: Auto-healed entry_confirmed. Continuing to place orders this cycle.")
bot_status['entry_confirmed'] = 1  # Update local state
# Do NOT return — fall through to order placement immediately
healed_this_cycle = True
```

---

## 3. Deadlock Cause #3: GTX Rejection Loop with No Recovery

### Where it is
`bot_executor.py` `_place_gtx_order_with_retry` and `maintain_orders` grid/TP placement blocks.

### What happens
The engine places GTX (Post-Only) orders. Binance rejects them if the price would cross the spread (-50004 or -2010). The retry logic re-fetches bid/ask and tries once more. If the retry ALSO fails (market is moving fast), it falls back to a plain limit order.

The bug: the fallback plain limit order **uses the same `clientOrderId`** with `_F` suffix. If the exchange accepts this order and it fills immediately (taker), the WebSocket event fires with the `_F` order ID. But the `bot_orders` row was saved with the original `clientOrderId` (no `_F` suffix) — `update_bot_order_exchange_id` is never called for the fallback. The WS event has no matching row. Fill is lost.

### The Fix
In `_place_gtx_order_with_retry`, when the fallback fires, save the fallback order ID to `bot_orders` immediately:

```python
# After fallback order placement succeeds:
fallback_order = exchange.create_order(...)
if fallback_order:
    # CRITICAL: Update bot_orders with the fallback order ID
    # so the WebSocket fill handler can match it
    try:
        from engine.database import update_bot_order_exchange_id
        # Find the bot_orders row by the original CID and update it
        original_cid = params.get('clientOrderId', '')
        fallback_id = fallback_order.get('id')
        conn = get_connection()
        conn.execute(
            "UPDATE bot_orders SET order_id=?, client_order_id=? WHERE client_order_id=?",
            (fallback_id, fallback_params.get('clientOrderId', original_cid), original_cid)
        )
        conn.commit()
        logger.info(f"[GTX-FALLBACK] Fallback order {fallback_id} linked to bot_orders row.")
    except Exception as e_link:
        logger.error(f"[GTX-FALLBACK] Failed to link fallback order to bot_orders: {e_link}")
return fallback_order
```

---

## 4. Deadlock Cause #4: `adopt_from_physical_positions` Only Runs Every 5 Minutes

### Where it is
`runner.py` — the periodic reconciler call at line 832: `if self.cycle_count % 10 == 0`.

### What happens
The reconciler's `adopt_from_physical_positions` (which detects and closes gaps between virtual and physical) only runs every 10 cycles (~5 minutes). If a bot deadlocks for 4 minutes and the deadlock breaks, the gap exists for up to 5 additional minutes before the reconciler even notices. During those 5 minutes, new grid orders are placed based on the wrong position size, potentially at wrong levels.

This is not itself a deadlock cause, but it **extends gap duration** from seconds to minutes.

### The Fix
After any order placement failure in `maintain_orders`, immediately trigger a targeted reconciliation for that bot's pair instead of waiting for the periodic sweep:

```python
# In maintain_orders, after ANY order placement failure:
except Exception as e:
    logger.error(f"❌ {name}: Error placing order: {e}")
    # Trigger immediate targeted reconciliation for this bot
    try:
        if self.runner._reconciler:
            self.runner._reconciler.adopt_from_physical_positions(
                target_pair=normalize_symbol(pair)
            )
    except Exception as recon_err:
        logger.debug(f"[IMMEDIATE-RECON] Failed: {recon_err}")
```

Also change the periodic sweep frequency from every 10 cycles to every 5 cycles for bots currently flagged with order health issues:

```python
# In runner.py run_cycle:
# Current:
if self.cycle_count % 10 == 0 and self._reconciler:

# Better:
has_order_health_issues = any(
    get_bot_status(b[0]).get('pos_limit_hit') or 
    str(get_bot_status(b[0]).get('cycle_phase', '')).upper() == 'MARGIN_HELD'
    for b in bots if b[9] == 1
)
recon_frequency = 5 if has_order_health_issues else 10
if self.cycle_count % recon_frequency == 0 and self._reconciler:
```

---

## 5. The Fill Attribution Gap — The Direct Cause of Ledger Mismatches

### Where it is
`ws_event_handlers.py` — the WebSocket fill handler that calls `credit_fill`.

### What happens
When a grid order fills on the exchange, Binance sends a WebSocket event with the order ID. The handler looks up `bot_orders` by `order_id` to find which bot owns it. If no matching row exists (because the bot was in a deadlock and never recorded the grid order in `bot_orders`), the fill is logged as "unattributed" and discarded. The position exists on the exchange but the ledger never knew about it.

This is the **direct mechanism** that creates every gap you have seen. Cause #1 through #4 above create the conditions. This is where the accounting loss actually occurs.

### The Fix — Real-Time Orphan Attribution
In the WebSocket fill handler, when a fill arrives with no matching `bot_orders` row, don't discard it. Instead, run immediate attribution:

```python
# In ws_event_handlers.py, in the fill handler:
# CURRENT (fill is lost):
if not bot_orders_row:
    logger.warning(f"[WS-FILL] No bot_orders row for order {order_id}. Fill unattributed.")
    return

# BETTER — real-time attribution:
if not bot_orders_row:
    logger.warning(f"[WS-FILL] No bot_orders row for order {order_id}. Attempting real-time attribution...")
    try:
        attributed_bot_id = _attribute_anonymous_fill(
            order_id=order_id,
            symbol=symbol,
            side=side,  # 'buy' or 'sell'
            qty=filled_qty,
            price=avg_price,
            timestamp=fill_timestamp
        )
        if attributed_bot_id:
            # Create the missing bot_orders row retroactively
            from engine.database import save_bot_order, get_bot_status
            status = get_bot_status(attributed_bot_id)
            cycle_id = status.get('cycle_id', 1) if status else 1
            order_type = 'grid'  # anonymous fills during deadlock are almost always grids
            save_bot_order(
                bot_id=attributed_bot_id,
                order_type=order_type,
                exchange_order_id=order_id,
                price=avg_price,
                amount=filled_qty,
                step=status.get('current_step', 0) + 1 if status else 1,
                status='filled',
                client_order_id=f"CQB_{attributed_bot_id}_RETROATTR_{order_id}",
                notes=f"Retroactive attribution: fill {order_id} matched to bot {attributed_bot_id}"
            )
            # Now credit the fill normally
            from engine.ledger import credit_fill, seal_trade_state
            credit_fill(
                bot_id=attributed_bot_id,
                order_id=order_id,
                cumulative_qty=filled_qty,
                avg_price=avg_price,
                order_type=order_type,
                is_cumulative=True
            )
            seal_trade_state(attributed_bot_id)
            logger.info(f"✅ [RETROATTR] Fill {order_id} ({qty} @ {price}) attributed to bot {attributed_bot_id}")
        else:
            logger.warning(f"[WS-FILL] Could not attribute fill {order_id}. Will be caught by periodic reconciler.")
    except Exception as attr_err:
        logger.error(f"[RETROATTR] Attribution failed for {order_id}: {attr_err}")
    return
```

Add the `_attribute_anonymous_fill` helper:

```python
def _attribute_anonymous_fill(order_id: str, symbol: str, side: str, 
                               qty: float, price: float, timestamp: int) -> Optional[int]:
    """
    Attempt to attribute an anonymous fill (no bot_orders row) to the correct bot.
    
    Strategy:
    1. Find all active bots on this symbol
    2. Determine direction from fill side: 'buy' = LONG entry or SHORT exit, 'sell' = SHORT entry or LONG exit
    3. Filter to bots that are IN TRADE and in the correct direction
    4. If exactly one bot matches: attribute to it
    5. If multiple match: attribute to the one whose avg_entry_price is closest to the fill price
    
    Returns bot_id or None if attribution is impossible.
    """
    from engine.database import get_connection
    from engine.exchange_interface import normalize_symbol
    
    try:
        norm_sym = normalize_symbol(symbol)
        conn = get_connection()
        cursor = conn.cursor()
        
        # Determine likely direction: buy = entry for LONG bot or exit for SHORT bot
        # During deadlock, grid fills are entries (deepening the position)
        # So: buy fill → LONG bot grid, sell fill → SHORT bot grid
        likely_direction = 'LONG' if side.lower() == 'buy' else 'SHORT'
        
        cursor.execute("""
            SELECT b.id, b.direction, t.total_invested, t.avg_entry_price, t.current_step
            FROM bots b
            JOIN trades t ON t.bot_id = b.id
            WHERE b.is_active = 1
              AND (b.normalized_pair = ? OR REPLACE(REPLACE(b.pair,'/',''  ),':USDC','') = ?)
              AND t.total_invested > 0
              AND b.direction = ?
        """, (norm_sym, norm_sym, likely_direction))
        
        candidates = cursor.fetchall()
        
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][0]
        
        # Multiple candidates: pick closest avg_entry_price to fill price
        best_bot_id = min(candidates, key=lambda r: abs(float(r[3] or 0) - price))[0]
        return best_bot_id
        
    except Exception as e:
        logger.error(f"[ATTR-HELPER] Failed: {e}")
        return None
```

---

## 6. The Startup Gap — Fills During Engine-Off Period

### Where it is
`runner.py` `startup_sync` → `reconciler.py` `reconstruct_offline_fills`.

### What happens
When the engine restarts after being down (planned or crash), `reconstruct_offline_fills` scans exchange order history to find fills that happened while the engine was offline. It uses a `since_hours` window (default 2h, periodic 24h).

The bug: this scan uses `since_hours` relative to `time.time()` — the current time. But if the engine was down for 30 hours, fills from 25 hours ago are outside the 24h window and are permanently missed. They never get attributed.

### The Fix
Calculate `since_hours` dynamically from the last engine shutdown timestamp:

```python
# In runner.py startup_sync:
# Determine how long the engine was offline
try:
    pid_file = config.PATHS.get("PID_FILE", "engine.pid")
    shutdown_ts_file = config.PATHS.get("SHUTDOWN_TS_FILE", "last_shutdown.ts")
    if os.path.exists(shutdown_ts_file):
        with open(shutdown_ts_file) as f:
            last_shutdown = int(f.read().strip())
        offline_hours = max(2, (time.time() - last_shutdown) / 3600 + 1)  # +1 hour buffer
    else:
        offline_hours = 48  # Conservative: scan 48h if no shutdown record
except Exception:
    offline_hours = 24
    
logger.info(f"[STARTUP] Engine was offline ~{offline_hours:.1f}h. Scanning fill history...")
self._reconciler.reconstruct_offline_fills(since_hours=min(offline_hours, 168))  # cap at 7 days
```

Write the shutdown timestamp on clean exit:

```python
# In runner.py, in the shutdown/finally block:
try:
    shutdown_ts_file = config.PATHS.get("SHUTDOWN_TS_FILE", "last_shutdown.ts")
    with open(shutdown_ts_file, 'w') as f:
        f.write(str(int(time.time())))
    logger.info(f"[SHUTDOWN] Wrote shutdown timestamp.")
except Exception as e:
    logger.warning(f"[SHUTDOWN] Failed to write shutdown timestamp: {e}")
```

---

## 7. Implementation Order

Do these in strict order. Test after each phase.

### Phase 1 — Real-time fill attribution (highest impact, prevents all future gaps)
File: `engine/ws_event_handlers.py`
- Add `_attribute_anonymous_fill()` helper function
- Modify the fill handler's "no matching bot_orders row" branch to call it
- Test: trigger a deadlock intentionally by pausing a bot, let a grid fill, confirm the fill is attributed in real-time

### Phase 2 — EE timer must never block order maintenance
File: `engine/bot_executor.py` `maintain_orders`
- Find every `return None` that fires because of EE wait state
- Replace with `tp_price = raw_db_tp; # fall through` pattern
- Test: enable EE on a bot, wait for EE interval, confirm TP and Grid orders still exist

### Phase 3 — Tier 3 heal must not skip a cycle
File: `engine/bot_executor.py` `maintain_orders` STEP-PROGRESSION-PROOF block
- After Tier 3 auto-heal, set local `bot_status['entry_confirmed'] = 1` and continue
- Test: manually clear `entry_confirmed` in DB, confirm orders are placed same cycle

### Phase 4 — GTX fallback order must be linked in bot_orders
File: `engine/bot_executor.py` `_place_gtx_order_with_retry`
- After fallback placement, update `bot_orders.order_id` with the fallback exchange ID
- Test: force a GTX rejection by placing at taker price, confirm fallback ID is in bot_orders

### Phase 5 — Startup fill scan uses offline duration
File: `engine/runner.py` `startup_sync`
- Write `last_shutdown.ts` on clean exit
- Read it on startup, compute `offline_hours`, pass to `reconstruct_offline_fills`
- Test: stop engine, wait 30 minutes, let exchange fills happen, restart — confirm fills are found

### Phase 6 — Immediate reconciliation after order failures
File: `engine/bot_executor.py` all order placement exception handlers
File: `engine/runner.py` `run_cycle` periodic reconciler frequency
- Add immediate targeted reconcile call after order failures
- Increase reconciler frequency when bots have order health issues
- Test: cause a margin rejection, confirm reconciler fires within 1 cycle not 5 minutes

---

## 8. Files to Modify

| File | Change |
|------|--------|
| `engine/ws_event_handlers.py` | Add `_attribute_anonymous_fill`, modify unattributed fill handler |
| `engine/bot_executor.py` | EE wait passthrough, Tier 3 same-cycle heal, GTX fallback linking, immediate recon on failure |
| `engine/runner.py` | Shutdown timestamp write, dynamic offline_hours, adaptive recon frequency |

---

## 9. What This Achieves

After these fixes:

1. **Anonymous fills during deadlocks are attributed in real-time** — no more gaps from deadlock periods
2. **EE timer cannot create order vacuums** — bots always have TP and Grid on exchange
3. **Entry confirmation heals without losing a cycle** — no gap window from restart races
4. **GTX fallback fills are tracked** — no more lost fills from fast markets
5. **Startup scan covers the full offline period** — no more permanent gaps from long downtime
6. **Reconciler reacts within seconds of order failures** — not minutes

The netting mismatch alerts should go permanently dark within 2 run cycles after these fixes are deployed, and stay dark indefinitely.
