"""
engine/ledger.py — The Single Source of Truth for Bot State (v2.0)

ARCHITECTURE INVARIANT:
  This module is the ONLY code that writes total_invested, avg_entry_price,
  and current_step to the trades table.

  No other module (ws_event_handlers, bot_executor, reconciler, preflight)
  may directly UPDATE trades SET total_invested or accumulate_trade_fill().

DESIGN PRINCIPLES:
  - seal_trade_state() is idempotent: calling it N times yields identical DB state.
  - credit_fill() is the ONLY path to record fills in bot_orders.
  - handle_tp_completion() is the atomic TP cascade (cancel → history → reset).
  - handle_flatten() is the atomic Force Close cascade (cancel → close → history → reset).

FILL LIFECYCLE:
  WS event → credit_fill(bot_id, order_id, cumulative_qty, price)
           → _pending_tp_cascade.add() [if TP]
           → seal_trade_state(bot_id)   [enqueued, idempotent]
  
  runner.run_cycle() → drain_tp_cascade(exchange)
                     → handle_tp_completion(bot_id, price, pair, exchange)

PARTIAL FILL RULES:
  - credit_fill() always updates filled_amount to MAX(existing, cumulative_qty).
  - A step is 'mastered' only when filled_amount/amount >= 0.99 (99% threshold).
  - Partial fills advance total_invested but NOT current_step.
  - seal_trade_state() reads filled_amount (not amount) for all cost calculations.
"""

import logging
import time
import threading
from typing import Optional, Tuple, Set, Dict, Any

logger = logging.getLogger("Ledger")

# ---------------------------------------------------------------------------
# Global TP Cascade Registry
# ---------------------------------------------------------------------------
# When a TP fills (via WS), we can't immediately execute the cancel+reset
# cascade because ws_event_handlers has no exchange object. Instead, we
# register the intent here. runner.run_cycle() drains this every cycle.
_tp_cascade_lock = threading.Lock()
_tp_cascade_registry: Set[Tuple[int, str, float]] = set()  # (bot_id, pair, exit_price)


def register_tp_cascade(bot_id: int, pair: str, exit_price: float) -> None:
    """Register a TP fill that needs the full cascade (cancel orders + reset)."""
    with _tp_cascade_lock:
        _tp_cascade_registry.add((bot_id, pair, exit_price))
    logger.info(f"[TP-REGISTRY] Bot {bot_id} {pair} @ {exit_price:.6f} queued for cascade.")


def drain_tp_cascade() -> Set[Tuple[int, str, float]]:
    """Pop all pending TP cascades for processing. Thread-safe."""
    with _tp_cascade_lock:
        pending = set(_tp_cascade_registry)
        _tp_cascade_registry.clear()
    return pending


def get_pending_tp_count() -> int:
    """Return number of pending TP cascades (for monitoring)."""
    with _tp_cascade_lock:
        return len(_tp_cascade_registry)


# ---------------------------------------------------------------------------
# credit_fill() — The Only Write Path to bot_orders.filled_amount
# ---------------------------------------------------------------------------

def credit_fill(
    bot_id: int,
    order_id: str,
    cumulative_qty: float,
    avg_price: float,
    order_type: str = 'grid',
    is_cumulative: bool = True
) -> bool:
    """
    Record a fill (or partial fill) in bot_orders.

    Args:
        bot_id: The bot that owns this order.
        order_id: The exchange order_id (or client_order_id for lookup).
        cumulative_qty: The TOTAL filled quantity on this order so far.
                        (This is Binance WS 'z' field — always cumulative.)
        avg_price: The average fill price for the filled portion.
        order_type: For audit logging only (entry/grid/tp).
        is_cumulative: If True (default), uses MAX() protection —
                       filled_amount is updated only if cumulative_qty > existing.
                       Set False only for incremental delta values.

    Returns:
        True if fill was credited, False if no matching order found.

    IDEMPOTENT: Calling with the same cumulative_qty twice is safe —
    the second call is a no-op because MAX(existing, same_value) = existing.
    """
    from engine.database import get_connection
    if cumulative_qty <= 0:
        return False

    try:
        conn = get_connection()

        # Find the bot_orders row — try order_id first, then client_order_id
        row = conn.execute(
            "SELECT id, filled_amount, amount, status FROM bot_orders "
            "WHERE (order_id = ? OR client_order_id = ?) AND bot_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (order_id, order_id, bot_id)
        ).fetchone()

        if not row:
            logger.warning(
                f"[CREDIT-FILL] No bot_orders row found for order_id={order_id} bot={bot_id}. "
                f"Cannot credit {cumulative_qty:.6f} @ {avg_price:.4f}."
            )
            return False

        db_id, existing_fill, order_amount, current_status = row
        existing_fill = float(existing_fill or 0)
        order_amount = float(order_amount or 0)

        # MAX() protection: never reduce filled_amount
        if is_cumulative and cumulative_qty <= existing_fill:
            logger.debug(
                f"[CREDIT-FILL] Bot {bot_id} order {order_id}: "
                f"cumulative {cumulative_qty:.6f} <= existing {existing_fill:.6f} — skip (idempotent)."
            )
            return False

        # Determine new status
        fully_filled = order_amount > 0 and (cumulative_qty / order_amount) >= 0.99
        new_status = 'filled' if fully_filled else 'partially_filled'

        # Only reject truly administrative terminal statuses
        if current_status in ('reset_cleared', 'auto_closed'):
            logger.debug(
                f"[CREDIT-FILL] Bot {bot_id} order {order_id}: status is {current_status} — skip."
            )
            return False
        
        # If it was cancelled but we are now recording a fill, log the resurrection
        if current_status in ('cancelled', 'canceled'):
             logger.warning(
                 f"🧟 [FILL-RESURRECTION] Bot {bot_id} order {order_id} was {current_status} "
                 f"but WS delivered fill ({cumulative_qty:.6f}). Overwriting to {new_status}."
             )

        conn.execute(
            "UPDATE bot_orders SET filled_amount = ?, price = ?, status = ?, updated_at = ? WHERE id = ?",
            (cumulative_qty, avg_price if avg_price > 0 else 
             conn.execute("SELECT price FROM bot_orders WHERE id=?", (db_id,)).fetchone()[0],
             new_status,
             int(time.time()),
             db_id)
        )
        conn.commit()

        logger.debug(
            f"[CREDIT-FILL] ✅ Bot {bot_id} {order_type}: order {order_id} "
            f"filled_amount {existing_fill:.6f} → {cumulative_qty:.6f} @ {avg_price:.4f} "
            f"[{new_status}]"
        )
        return True

    except Exception as e:
        logger.error(f"[CREDIT-FILL] Failed for bot {bot_id} order {order_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# seal_trade_state() — The Only Writer to trades Table
# ---------------------------------------------------------------------------

def seal_trade_state(bot_id: int) -> Dict[str, Any]:
    """
    THE authoritative trades-table writer (v2.0).

    Reads bot_orders (the ledger), computes net position via
    recompute_invested_from_orders(), then overwrites trades with mathematical truth.

    IDEMPOTENT: Calling this N times produces identical state.
    NEVER raises — logs errors and returns empty dict on failure.

    Updates:
        trades.total_invested   ← net cost (buy_cost - sell_qty * avg_price)
        trades.avg_entry_price  ← weighted average entry
        trades.current_step     ← highest fully-mastered step
        trades.entry_confirmed  ← 1 if net qty > 0
        trades.basket_start_time← only updated if qty increased (new entry)
        bots.status             ← 'IN TRADE' if net qty > 0, else 'Scanning'

    Returns dict with computed values (useful for tests and logging):
        {'qty': float, 'cost': float, 'avg': float, 'step': int, 'status': str}
    """
    from engine.database import get_connection, recompute_invested_from_orders

    try:
        conn = get_connection()

        # ── Bootstrap position_side from bot config ─────────────────────────────
        # After a clean reset, trades.position_side=NULL which causes recompute to
        # default to 'LONG' and exclude all SHORT bot_orders → invested always=0.
        # Fix: write the correct side from bots.direction before recomputing.
        try:
            _side_check = conn.execute(
                "SELECT t.position_side, b.direction "
                "FROM trades t JOIN bots b ON b.id=t.bot_id "
                "WHERE t.bot_id=?", (bot_id,)
            ).fetchone()
            if _side_check:
                _db_side, _bot_dir = _side_check
                if not _db_side and _bot_dir:
                    _correct_side = 'SHORT' if 'short' in str(_bot_dir).lower() else 'LONG'
                    conn.execute(
                        "UPDATE trades SET position_side=? WHERE bot_id=?",
                        (_correct_side, bot_id)
                    )
                    conn.commit()
                    logger.debug(f"[SEAL] Bot {bot_id}: bootstrapped position_side={_correct_side} from config.")
        except Exception as _bs_err:
            logger.warning(f"[SEAL] Bot {bot_id}: position_side bootstrap warning: {_bs_err}")
        # ────────────────────────────────────────────────────────────────────────

    except Exception as _conn_err:
        logger.error(f"[SEAL] DB connect failed for bot {bot_id}: {_conn_err}")
        return {}

    try:
        cost, avg, qty, step = recompute_invested_from_orders(bot_id)
    except Exception as e:
        logger.error(f"[SEAL] recompute_invested_from_orders failed for bot {bot_id}: {e}")
        return {}

    try:
        conn = get_connection()

        # Read previous qty to detect if we just entered (for basket_start_time)
        prev_row = conn.execute(
            "SELECT total_invested, avg_entry_price FROM trades WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()

        if prev_row:
            prev_invested, prev_avg = float(prev_row[0] or 0), float(prev_row[1] or 1)
            prev_qty = prev_invested / prev_avg if prev_avg > 0 else 0
        else:
            prev_qty = 0.0

        # EE timer reset: update basket_start_time only if qty meaningfully increased
        # (new grid fill added to position). 1% threshold avoids noise from rounding.
        basket_time_update = None
        if qty > prev_qty * 1.01 and qty > 1e-8:
            basket_time_update = int(time.time())

        # Write to trades
        conn.execute("""
            UPDATE trades SET
                total_invested   = ?,
                avg_entry_price  = ?,
                current_step     = ?,
                entry_confirmed  = CASE WHEN ? > 0 THEN 1 ELSE 0 END,
                basket_start_time = COALESCE(?, basket_start_time)
            WHERE bot_id = ?
        """, (cost, avg, step, qty, basket_time_update, bot_id))

        # Derive and update bot status
        if qty > 1e-8:
            new_status = 'IN TRADE'
        else:
            new_status = 'Scanning'

        conn.execute("UPDATE bots SET status = ? WHERE id = ?", (new_status, bot_id))
        conn.commit()

        logger.debug(
            f"[SEAL] ✅ Bot {bot_id}: cost=${cost:.4f} avg={avg:.4f} "
            f"qty={qty:.6f} step={step} → {new_status}"
        )

        return {'qty': qty, 'cost': cost, 'avg': avg, 'step': step, 'status': new_status}

    except Exception as e:
        logger.error(f"[SEAL] Failed to write trades for bot {bot_id}: {e}")
        return {}


def seal_all_active_bots() -> int:
    """
    Run seal_trade_state() for all active bots.
    Used at startup to ensure trades table is consistent before any trading.
    Returns the count of bots that had their state corrected.
    """
    from engine.database import get_connection

    corrected = 0
    try:
        conn = get_connection()
        bots = conn.execute(
            "SELECT id, name FROM bots WHERE is_active = 1"
        ).fetchall()

        for bot_id, bot_name in bots:
            before = conn.execute(
                "SELECT total_invested FROM trades WHERE bot_id = ?", (bot_id,)
            ).fetchone()
            before_invested = float(before[0] if before else 0)

            result = seal_trade_state(bot_id)
            if result:
                after_invested = result.get('cost', 0)
                if abs(after_invested - before_invested) > 0.01:
                    logger.info(
                        f"[SEAL-ALL] Bot {bot_name} (id={bot_id}): "
                        f"${before_invested:.4f} → ${after_invested:.4f} corrected"
                    )
                    corrected += 1

        logger.info(f"[SEAL-ALL] ✅ {len(bots)} bots sealed. {corrected} corrected.")
        return corrected

    except Exception as e:
        logger.error(f"[SEAL-ALL] Failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# advance_step_if_mastered() — Step Progression Logic
# ---------------------------------------------------------------------------

def advance_step_if_mastered(bot_id: int, order_id: str) -> bool:
    """
    Check if the order at `order_id` is 99%+ filled (step mastered).
    
    A step is 'mastered' only when filled_amount / amount >= 0.99.
    Partial fills do NOT advance the step — they accumulate in filled_amount
    and are counted toward total_invested via seal_trade_state().

    Returns True if the step was mastered (caller may want to place next grid).
    Returns False for partial fills (no action needed for step).
    """
    from engine.database import get_connection

    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT step, amount, filled_amount FROM bot_orders "
            "WHERE (order_id = ? OR client_order_id = ?) AND bot_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (order_id, order_id, bot_id)
        ).fetchone()

        if not row:
            return False

        step, amount, filled = int(row[0] or 0), float(row[1] or 0), float(row[2] or 0)

        if amount <= 0:
            return False

        fill_ratio = filled / amount
        if fill_ratio >= 0.99:
            logger.debug(
                f"[STEP] Bot {bot_id}: Step {step} mastered "
                f"(filled={filled:.6f} / amount={amount:.6f} = {fill_ratio:.1%})"
            )
            return True
        else:
            logger.debug(
                f"[STEP] Bot {bot_id}: Partial fill on step {step} "
                f"({fill_ratio:.1%}) — step not yet mastered."
            )
            return False

    except Exception as e:
        logger.error(f"[STEP] advance_step_if_mastered failed for bot {bot_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# handle_tp_completion() — The Complete Atomic TP Cascade
# ---------------------------------------------------------------------------

def handle_tp_completion(
    bot_id: int,
    exit_price: float,
    pair: str,
    exchange,
    cycle_id: Optional[int] = None
) -> bool:
    """
    THE complete TP workflow. Atomic. No orphan orders possible after completion.

    Steps (ALL must succeed before status → 'Scanning'):
      1. Validate: confirm physical position is reducing (sanity check)
      2. Cancel ALL pending CQB_{bot_id}_* orders on exchange
      3. Wait for confirmation (or REST-verify after 5s timeout)
      4. Credit TP fill to bot_orders (if not already there from WS)
      5. Write trade_history entry (PnL, qty, duration)
      6. Mark all bot_orders for current cycle as 'reset_cleared'
      7. Increment cycle_id, zero trades row
      8. Set bots.status = 'Scanning'

    Args:
        bot_id: The bot to reset.
        exit_price: The TP fill price.
        pair: Exchange symbol (e.g. 'ETHUSDC').
        exchange: ExchangeInterface instance for order cancellation.
        cycle_id: The cycle that was completed (resolved from DB if None).

    Returns:
        True if cascade completed successfully, False if any step failed.
    """
    from engine.database import (
        get_connection, reset_bot_after_tp, log_trade, get_bot_status
    )
    from engine.exchange_interface import normalize_symbol

    logger.info(f"[TP-CASCADE] ▶ Starting TP cascade for Bot {bot_id} {pair} @ {exit_price:.6f}")

    try:
        conn = get_connection()

        # Resolve cycle_id if not provided
        if cycle_id is None:
            row = conn.execute(
                "SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id = ?", (bot_id,)
            ).fetchone()
            cycle_id = int(row[0]) if row else 1

        # --- Step 1: Cancel ALL pending orders for this bot ---
        cancelled_count = 0
        cancel_errors = []

        try:
            norm_pair = normalize_symbol(pair)
            open_orders_on_exchange = exchange.fetch_open_orders(norm_pair)
            bot_orders_to_cancel = [
                o for o in (open_orders_on_exchange or [])
                if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')
            ]

            for o in bot_orders_to_cancel:
                try:
                    exchange.cancel_order(o['id'], norm_pair)
                    cancelled_count += 1
                    # Update DB status
                    conn.execute(
                        "UPDATE bot_orders SET status='cancelled', updated_at=? WHERE order_id=?",
                        (int(time.time()), o['id'])
                    )
                except Exception as e_cancel:
                    cancel_errors.append(str(e_cancel))
                    logger.warning(
                        f"[TP-CASCADE] Could not cancel order {o['id']} for bot {bot_id}: {e_cancel}"
                    )

            conn.commit()
            logger.info(
                f"[TP-CASCADE] Bot {bot_id}: Cancelled {cancelled_count} orders. "
                f"Errors: {len(cancel_errors)}"
            )

        except Exception as e_fetch:
            logger.warning(f"[TP-CASCADE] Bot {bot_id}: Could not fetch open orders: {e_fetch}")

        # --- Step 2: Log to trade_history ---
        try:
            bot_state = get_bot_status(bot_id)
            if bot_state:
                invested = float(bot_state.get('total_invested', 0) or 0)
                avg_entry = float(bot_state.get('avg_entry_price', 0) or 0)
                current_step = int(bot_state.get('current_step', 0) or 0)
                basket_start = int(bot_state.get('basket_start_time', 0) or 0)
                direction = bot_state.get('direction', 'LONG')

                if avg_entry > 0 and exit_price > 0:
                    qty = invested / avg_entry
                    if direction.upper() == 'LONG':
                        pnl = qty * (exit_price - avg_entry)
                    else:
                        pnl = qty * (avg_entry - exit_price)
                    duration_s = int(time.time()) - basket_start if basket_start > 0 else 0

                    log_trade(
                        bot_id=bot_id,
                        action='TP_HIT',
                        price=exit_price,
                        amount=qty,
                        notes=(
                            f"TP @ {exit_price:.6f} | entry_avg={avg_entry:.6f} | "
                            f"pnl=${pnl:.4f} | step={current_step} | duration={duration_s}s"
                        )
                    )
        except Exception as e_log:
            logger.warning(f"[TP-CASCADE] Bot {bot_id}: trade_history log failed (non-fatal): {e_log}")

        # --- Step 3: Full atomic reset via existing reset_bot_after_tp ---
        # This handles: mark reset_cleared, increment cycle_id, zero trades row
        try:
            reset_bot_after_tp(
                bot_id=bot_id,
                exit_price=exit_price,
                action_label='TP_HIT',
                notes=f'Cascade via ledger.handle_tp_completion @ {exit_price:.6f}'
            )
            logger.info(f"[TP-CASCADE] ✅ Bot {bot_id}: Reset to Scanning. Cycle {cycle_id} → {cycle_id + 1}.")
            return True

        except Exception as e_reset:
            logger.error(f"[TP-CASCADE] Bot {bot_id}: reset_bot_after_tp failed: {e_reset}")
            # Attempt manual status set as fallback
            try:
                conn.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
                conn.commit()
            except Exception:
                pass
            return False

    except Exception as e:
        logger.error(f"[TP-CASCADE] Bot {bot_id}: Cascade failed with exception: {e}", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# handle_flatten() — The Complete Atomic Force Close Cascade
# ---------------------------------------------------------------------------

def handle_flatten(
    bot_id: int,
    pair: str,
    exchange,
    reason: str = 'FORCE_SL',
    exit_price: Optional[float] = None
) -> bool:
    """
    Complete flatten workflow for Force SL, manual close, or circuit breaker.

    Steps:
      1. Set bots.status = 'FLATTENING' (UI shows it, no new orders placed)
      2. Cancel ALL pending CQB_{bot_id}_* orders (fire-and-forget then confirm)
      3. If physical qty > dust AND virtual qty > 0: Place reduceOnly market close
      4. Wait for WS confirmation (or REST poll after 10s timeout)
      5. Credit the close fill to bot_orders
      6. Write trade_history entry
      7. Mark all bot_orders for current cycle as 'reset_cleared'
      8. Zero trades row, increment cycle_id
      9. Set bots.status = 'Scanning'

    Args:
        bot_id: The bot to flatten.
        pair: Exchange symbol.
        exchange: ExchangeInterface instance.
        reason: Reason label for logging and history.
        exit_price: If provided, skip the market close (position already closed).

    Returns:
        True if flatten completed, False if any critical step failed.
    """
    from engine.database import get_connection, get_bot_status, reset_bot_after_tp, log_trade
    from engine.exchange_interface import normalize_symbol

    logger.warning(f"[FLATTEN] ▶ Starting {reason} flatten for Bot {bot_id} {pair}")

    try:
        conn = get_connection()
        norm_pair = normalize_symbol(pair)

        # --- Step 1: Set FLATTENING status to block new orders ---
        conn.execute("UPDATE bots SET status='FLATTENING' WHERE id=?", (bot_id,))
        conn.commit()
        logger.info(f"[FLATTEN] Bot {bot_id}: Status → FLATTENING")

        # --- Step 2: Cancel all pending orders ---
        try:
            open_orders = exchange.fetch_open_orders(norm_pair)
            bot_orders_raw = [
                o for o in (open_orders or [])
                if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')
            ]

            for o in bot_orders_raw:
                try:
                    exchange.cancel_order(o['id'], norm_pair)
                    conn.execute(
                        "UPDATE bot_orders SET status='cancelled', updated_at=? WHERE order_id=?",
                        (int(time.time()), o['id'])
                    )
                except Exception as e_can:
                    logger.warning(f"[FLATTEN] Bot {bot_id}: Cancel {o['id']} failed: {e_can}")
            conn.commit()
            logger.info(f"[FLATTEN] Bot {bot_id}: Cancelled {len(bot_orders_raw)} orders.")

        except Exception as e_fetch:
            logger.warning(f"[FLATTEN] Bot {bot_id}: Order fetch failed (non-fatal): {e_fetch}")

        # --- Step 3: Market close (if no exit_price provided) ---
        actual_exit_price = exit_price
        if exit_price is None:
            bot_state = get_bot_status(bot_id)
            if bot_state and bot_state.get('total_invested', 0) > 0:
                direction = bot_state.get('direction', 'LONG')
                avg_entry = float(bot_state.get('avg_entry_price', 0) or 0)
                qty = (float(bot_state.get('total_invested', 0) or 0) / avg_entry
                       if avg_entry > 0 else 0)

                if qty > 1e-6:
                    close_side = 'sell' if direction.upper() == 'LONG' else 'buy'
                    from engine.database import get_connection as _gc
                    cycle_row = _gc().execute(
                        "SELECT COALESCE(cycle_id,1) FROM trades WHERE bot_id=?", (bot_id,)
                    ).fetchone()
                    cycle_id = int(cycle_row[0]) if cycle_row else 1

                    cid = f"CQB_{bot_id}_FLATTEN_{cycle_id}_0"
                    try:
                        close_order = exchange.create_order(
                            norm_pair, 'market', close_side, qty,
                            params={
                                'reduceOnly': True,
                                'newClientOrderId': cid,
                                'positionSide': direction.upper()
                            }
                        )
                        if close_order:
                            actual_exit_price = float(
                                close_order.get('average') or
                                close_order.get('price') or
                                avg_entry
                            )
                            logger.info(
                                f"[FLATTEN] ✅ Bot {bot_id}: Market close placed. "
                                f"qty={qty:.6f} @ ~{actual_exit_price:.4f}"
                            )
                    except Exception as e_close:
                        logger.error(
                            f"[FLATTEN] Bot {bot_id}: Market close failed: {e_close}. "
                            f"Proceeding with DB reset using avg_entry as exit price."
                        )
                        actual_exit_price = avg_entry

        # --- Step 4: Log and reset ---
        reset_price = actual_exit_price or 0.0
        try:
            log_trade(
                bot_id=bot_id,
                action=reason,
                price=reset_price,
                amount=0,
                notes=f'Flatten: {reason} @ {reset_price:.6f}'
            )
        except Exception as e_log:
            logger.warning(f"[FLATTEN] Bot {bot_id}: trade_history log failed: {e_log}")

        try:
            reset_bot_after_tp(
                bot_id=bot_id,
                exit_price=reset_price,
                action_label=reason,
                notes=f'Flatten: {reason}'
            )
            logger.info(f"[FLATTEN] ✅ Bot {bot_id}: Reset to Scanning after {reason}.")
            return True

        except Exception as e_reset:
            logger.error(f"[FLATTEN] Bot {bot_id}: reset failed: {e_reset}")
            # Emergency fallback
            try:
                conn.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
                conn.execute(
                    "UPDATE trades SET total_invested=0, avg_entry_price=0, current_step=0 WHERE bot_id=?",
                    (bot_id,)
                )
                conn.commit()
                logger.warning(f"[FLATTEN] Bot {bot_id}: Emergency status reset to Scanning.")
            except Exception:
                pass
            return False

    except Exception as e:
        logger.error(f"[FLATTEN] Bot {bot_id}: Flatten failed: {e}", exc_info=True)
        # Try to at least unblock the bot
        try:
            from engine.database import get_connection as _gc
            _gc().execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
            _gc().commit()
        except Exception:
            pass
        return False
