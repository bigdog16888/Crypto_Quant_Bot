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
_tp_cascade_registry: Set[Tuple] = set()  # (bot_id, pair, exit_price, exit_fill_ts)


def register_tp_cascade(bot_id: int, pair: str, exit_price: float, exit_fill_ts: int = 0) -> None:
    """
    Register a TP fill that needs the full cascade (cancel orders + reset).

    Args:
        exit_fill_ts: Unix timestamp (seconds) from the exchange TP fill event.
                      Passed through to reset_bot_after_tp to anchor cycle_start_time
                      to the actual trade-close moment on the exchange.
    """
    with _tp_cascade_lock:
        _tp_cascade_registry.add((bot_id, pair, exit_price, exit_fill_ts))
    logger.info(f"[TP-REGISTRY] Bot {bot_id} {pair} @ {exit_price:.6f} queued (fill_ts={exit_fill_ts}).")


def drain_tp_cascade() -> Set[Tuple]:
    """Pop all pending TP cascades for processing. Thread-safe.
    Returns set of (bot_id, pair, exit_price, exit_fill_ts) tuples.
    """
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
    is_cumulative: bool = True,
    fill_ts: int = 0,
    sync_to_exchange: bool = False,
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
        fill_ts: Unix timestamp (seconds) from the exchange when this fill occurred.
                 Source: order.get('lastTradeTimestamp', 0) // 1000 from Binance WS/REST.
                 Stored in bot_orders.filled_at as an immutable audit record.
                 Defaults to int(time.time()) if not provided (engine-side fallback).

    Returns:
        True if fill was credited, False if no matching order found.

    IDEMPOTENT: Calling with the same cumulative_qty twice is safe —
    the second call is a no-op because MAX(existing, same_value) = existing.

    ORDER-ID-PROOF STEP SATURATION GUARD [v2.5]:
    For entry/grid order types, before incrementing open_qty this function checks
    the TOTAL filled_amount already credited for the same (bot_id, step, cycle_id)
    across ALL other order_ids. If the step is already saturated, the row is marked
    auto_closed (preserving the audit trail) and open_qty is NOT incremented.
    This is the definitive fix for GTX chase-retry double-credit inflation.
    """
    from engine.database import get_connection
    if cumulative_qty <= 0:
        return False

    try:
        conn = get_connection()

        # Find the bot_orders row — try order_id first, then client_order_id
        row = conn.execute(
            "SELECT id, filled_amount, amount, status, step, cycle_id FROM bot_orders "
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

        db_id, existing_fill, order_amount, current_status, row_step, row_cycle = row
        existing_fill = float(existing_fill or 0)
        order_amount = float(order_amount or 0)

        # Never credit more than the order's declared size (+5% rounding tolerance).
        # Prevents unit bugs (e.g. amount=0.002 but exchange reports filled=1.0) from
        # inflating virtual net to ~1 BTC when only 0.002 was ordered.
        if order_amount > 0:
            cap = order_amount * 1.05
            if cumulative_qty > cap:
                logger.error(
                    f"[CREDIT-FILL-CAP] Bot {bot_id} order {order_id}: exchange cumulative "
                    f"{cumulative_qty:.8f} > order amount {order_amount:.8f} (cap {cap:.8f}). "
                    f"Capping to order size."
                )
                cumulative_qty = cap

        # MAX() protection: never reduce filled_amount unless syncing to exchange truth
        if is_cumulative and cumulative_qty <= existing_fill:
            if not sync_to_exchange or abs(cumulative_qty - existing_fill) <= 1e-12:
                logger.debug(
                    f"[CREDIT-FILL] Bot {bot_id} order {order_id}: "
                    f"cumulative {cumulative_qty:.6f} <= existing {existing_fill:.6f} — skip."
                )
                return False
            logger.warning(
                f"[CREDIT-FILL-SYNC] Bot {bot_id} order {order_id}: "
                f"reducing filled {existing_fill:.6f} → {cumulative_qty:.6f} (exchange truth)."
            )

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

        # Resolve actual fill timestamp: use exchange-provided if available, else engine time
        actual_fill_ts = fill_ts if fill_ts > 0 else int(time.time())

        # ── ORDER-ID-PROOF STEP SATURATION GUARD [v2.5] ──────────────────────────
        # For entry-type fills, verify that crediting this fill will not inflate the
        # step beyond its physical capacity. GTX chase retries place new exchange orders
        # (new order_id) for the same logical step — if the FIRST attempt already filled
        # and was credited, all subsequent retries must be rejected here.
        #
        # Mechanism: sum the filled_amount of ALL OTHER rows for the same
        # (bot_id, step, cycle_id) with entry-type order_types, excluding this db_id.
        # If that sum + our delta exceeds order_amount * 1.05 (5% tolerance), this is
        # a duplicate credit. We mark the row auto_closed and return False WITHOUT
        # touching open_qty. The row is preserved for audit trail.
        #
        # This guard fires on ALL fill paths: WS live, history-orphan, REST deferred.
        _ENTRY_TYPES = ('entry', 'grid', 'adoption_add', 'adoption',
                        'forensic_adoption_add')
        _EXIT_TYPES  = ('tp', 'close', 'sl', 'dust_close', 'adoption_reduce',
                        'forensic_adoption_reduce')

        if order_type in _ENTRY_TYPES and row_step is not None and row_cycle is not None and order_amount > 0:
            try:
                already_credited = conn.execute(
                    "SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders "
                    "WHERE bot_id = ? AND step = ? AND cycle_id = ? "
                    "AND order_type IN ('entry','grid','adoption_add','adoption','forensic_adoption_add') "
                    "AND filled_amount > 0 "
                    "AND status NOT IN ('reset_cleared','auto_closed','cancelled','canceled','failed','rejected') "
                    "AND id != ?",
                    (bot_id, row_step, row_cycle, db_id)
                ).fetchone()[0] or 0.0

                delta_proposed = cumulative_qty - existing_fill
                capacity_limit = order_amount * 1.05  # 5% tolerance for rounding

                if already_credited > 0 and (already_credited + delta_proposed) > capacity_limit:
                    # Step is already saturated by another order_id. This is a GTX chase
                    # duplicate. Mark this row auto_closed and do NOT credit open_qty.
                    logger.warning(
                        f"🛡️ [STEP-SATURATED] Bot {bot_id} {order_type} step={row_step} cycle={row_cycle}: "
                        f"already credited {already_credited:.6f} via other order_id(s). "
                        f"Proposed delta {delta_proposed:.6f} would exceed capacity {capacity_limit:.6f}. "
                        f"Marking order {order_id} (db_id={db_id}) as auto_closed. "
                        f"open_qty NOT incremented — ledger integrity preserved."
                    )
                    conn.execute(
                        "UPDATE bot_orders SET status='auto_closed', notes=?, updated_at=? WHERE id=?",
                        (
                            f"STEP_SATURATED:already_credited={already_credited:.6f},capacity={capacity_limit:.6f}",
                            int(time.time()),
                            db_id
                        )
                    )
                    conn.commit()
                    return False  # Do not credit open_qty
            except Exception as _sg_err:
                # Non-fatal: if the guard itself fails, log and continue with the credit
                # to avoid losing legitimate fills. The seal_trade_state cross-check will
                # catch any resulting drift on the next run.
                logger.error(
                    f"[STEP-SATURATED] Guard check failed for bot {bot_id} order {order_id}: "
                    f"{_sg_err}. Proceeding with credit (fail-open to preserve fills)."
                )
        # ─────────────────────────────────────────────────────────────────────────

        conn.execute(
            "UPDATE bot_orders SET filled_amount = ?, price = ?, status = ?, "
            "filled_at = CASE WHEN filled_at = 0 THEN ? ELSE filled_at END, "
            "updated_at = ? WHERE id = ?",
            (cumulative_qty, avg_price if avg_price > 0 else
             conn.execute("SELECT price FROM bot_orders WHERE id=?", (db_id,)).fetchone()[0],
             new_status,
             actual_fill_ts,    # filled_at — only set once (first fill wins; idempotent)
             int(time.time()),
             db_id)
        )

        # ── OPEN_QTY ACCUMULATOR [v2.1] ─────────────────────────────────────────
        # Maintain trades.open_qty as an explicit running total of confirmed fills.
        # delta = net NEW qty credited this call (cumulative_qty - prior existing_fill).
        delta = cumulative_qty - existing_fill
        _otype_lower = str(order_type).lower()
        try:
            if _otype_lower in _ENTRY_TYPES:
                if delta >= 0:
                    conn.execute(
                        "UPDATE trades SET open_qty = ROUND(COALESCE(open_qty, 0) + ?, 8) "
                        "WHERE bot_id = ?",
                        (delta, bot_id),
                    )
                else:
                    conn.execute(
                        "UPDATE trades SET open_qty = MAX(0, ROUND(COALESCE(open_qty, 0) + ?, 8)) "
                        "WHERE bot_id = ?",
                        (delta, bot_id),
                    )
                logger.debug(f"[OPEN-QTY] Bot {bot_id}: {delta:+.8f} ({_otype_lower})")
            elif _otype_lower in _EXIT_TYPES:
                if delta >= 0:
                    conn.execute(
                        "UPDATE trades SET open_qty = MAX(0, ROUND(COALESCE(open_qty, 0) - ?, 8)) "
                        "WHERE bot_id = ?",
                        (delta, bot_id),
                    )
                else:
                    conn.execute(
                        "UPDATE trades SET open_qty = ROUND(COALESCE(open_qty, 0) + ?, 8) "
                        "WHERE bot_id = ?",
                        (-delta, bot_id),
                    )
                logger.debug(f"[OPEN-QTY] Bot {bot_id}: exit delta {delta:+.8f} ({_otype_lower})")
        except Exception as _oq_err:
            # Non-fatal: open_qty will be backfilled by check_and_fix_integrity on next startup
            logger.warning(f"[OPEN-QTY] Bot {bot_id}: accumulator update failed: {_oq_err}")

        # One-way shared book: opposite-direction siblings must lose open_qty when this
        # entry/grid fill nets against their side on the exchange.
        if _otype_lower in _ENTRY_TYPES and delta > 1e-12:
            try:
                pair_row = conn.execute(
                    "SELECT pair, direction FROM bots WHERE id = ?", (bot_id,)
                ).fetchone()
                if pair_row:
                    from engine.oneway_netting import apply_oneway_entry_cross_reduction
                    apply_oneway_entry_cross_reduction(
                        bot_id,
                        pair_row[0],
                        pair_row[1],
                        delta,
                        str(order_id),
                        avg_price,
                    )
            except Exception as _ow_err:
                logger.warning(
                    f"[ONEWAY-CROSS] Bot {bot_id} post-fill netting failed: {_ow_err}"
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

    # ── PRE-FLIGHT: entry_confirmed guard ──────────────────────────────────────
    # If the bot has a physical position (total_invested > 0) but entry_confirmed
    # is still 0, a crash happened between fill credit and the DB write. Force it
    # to 1 here, BEFORE cost recomputation, so the sealed row is fully consistent.
    # Running this AFTER recompute would seal costs with entry_confirmed=0 still set.
    try:
        conn = get_connection()
        _pf_row = conn.execute(
            "SELECT total_invested, entry_confirmed FROM trades WHERE bot_id=?",
            (bot_id,)
        ).fetchone()
        if _pf_row and float(_pf_row[0] or 0) > 0 and not _pf_row[1]:
            logger.warning(
                f"[LEDGER-PREFLIGHT] Bot {bot_id}: total_invested={_pf_row[0]:.4f} "
                f"but entry_confirmed=0. Forcing entry_confirmed=1 before seal."
            )
            from engine.database import update_bot_status as _pf_ubs
            _pf_ubs(bot_id, entry_confirmed=1)
    except Exception as _pf_err:
        logger.warning(f"[LEDGER-PREFLIGHT] Bot {bot_id}: pre-flight guard failed (non-fatal): {_pf_err}")
    # ────────────────────────────────────────────────────────────────────────────

    try:
        cost, avg, qty, step = recompute_invested_from_orders(bot_id)
    except Exception as e:
        logger.error(f"[SEAL] recompute_invested_from_orders failed for bot {bot_id}: {e}")
        return {}

    main_open_qty = max(0.0, qty)

    # ── OPEN_QTY ACCUMULATOR CROSS-CHECK [v2.3.2] ──────────────────────────────
    # trades.open_qty is the authoritative running total — it was incremented/decremented
    # atomically with every exchange-confirmed fill via credit_fill().
    # For minor drifts (<20%) the accumulator wins (it reflects real-time fills).
    # For large drifts (>20%) the recompute wins — a stale accumulator from a failed
    # adoption_reduce/TP credit would otherwise cause the next TP to be oversized.
    try:
        conn = get_connection()
        _accum_row = conn.execute(
            "SELECT open_qty FROM trades WHERE bot_id=?", (bot_id,)
        ).fetchone()
        if _accum_row is not None:
            accumulator_qty = float(_accum_row[0] or 0)
            if abs(accumulator_qty) > 1e-8 or main_open_qty > 1e-8:
                drift = abs(accumulator_qty - main_open_qty)
                drift_base = max(abs(accumulator_qty), abs(main_open_qty), 1e-8)
                drift_pct = drift / drift_base
                if drift_pct > 0.05:
                    # ── LARGE-DRIFT SELF-HEAL [v2.3.2] ─────────────────────────
                    logger.warning(
                        f"[QTY-DRIFT-HEAL] Bot {bot_id}: accumulator={accumulator_qty:.8f} "
                        f"vs basket_open={main_open_qty:.8f} (drift={drift_pct:.2%} > 5%). "
                        f"Overwriting open_qty with recomputed basket truth."
                    )
                    conn.execute(
                        "UPDATE trades SET open_qty=? WHERE bot_id=?",
                        (main_open_qty, bot_id)
                    )
                    conn.commit()
                elif drift_pct > 0.001:  # 0.1%–5%: minor drift → accumulator wins
                    logger.warning(
                        f"[QTY-DRIFT] Bot {bot_id}: accumulator={accumulator_qty:.8f} "
                        f"vs basket_open={main_open_qty:.8f} (drift={drift_pct:.2%}). "
                        f"Accumulator is authoritative — using it."
                    )
                    main_open_qty = max(0.0, accumulator_qty)
                    if avg > 0:
                        cost = main_open_qty * avg
                else:
                    main_open_qty = max(0.0, accumulator_qty)
                    if avg > 0:
                        cost = main_open_qty * avg
    except Exception as _acc_err:
        logger.warning(f"[SEAL] Bot {bot_id}: accumulator read failed: {_acc_err}")
    # ────────────────────────────────────────────────────────────────────────────

    try:
        conn = get_connection()

        # Read previous qty to detect if we just entered (for basket_start_time)
        prev_row = conn.execute(
            "SELECT total_invested, avg_entry_price, basket_start_time, current_step FROM trades WHERE bot_id = ?",
            (bot_id,)
        ).fetchone()

        if prev_row:
            prev_invested = float(prev_row[0] or 0)
            prev_avg = float(prev_row[1] or 1)
            prev_basket_ts = int(prev_row[2] or 0)
            prev_step = int(prev_row[3] or 0)
            prev_qty = prev_invested / prev_avg if prev_avg > 0 else 0
        else:
            prev_qty = 0.0
            prev_basket_ts = 0
            prev_step = 0

        basket_time_update = None

        # PRIMARY: basket qty increased meaningfully → new fill, update timer
        if main_open_qty > prev_qty * 1.01 and main_open_qty > 1e-8:
            basket_time_update = int(time.time())

        # SAFETY NET: basket qty > 0 but basket_start_time is from a previous cycle
        elif main_open_qty > 1e-8 and prev_step == 0 and prev_basket_ts > 0 and prev_invested <= 0.01:
            basket_time_update = int(time.time())
            logger.info(
                f"[SEAL] Bot {bot_id}: basket_start_time reset — "
                f"prev_step=0 + prev_invested=0 indicates stale TP-reset timestamp. "
                f"Anchoring to now (basket_qty={main_open_qty:.6f})."
            )

        # EXPLICIT ZERO: flat basket → zero basket timer
        if main_open_qty <= 1e-8:
            basket_time_update = 0

        # Clamp step when basket has size (hedge-only does not advance step)
        if main_open_qty > 1e-8 and step == 0:
            step = 1
            logger.info(f"[SEAL] Bot {bot_id}: clamped step 0→1 (basket_qty={main_open_qty:.6f} > 0, position is active)")

        # Write to trades
        conn.execute("""
            UPDATE trades SET
                total_invested   = ?,
                avg_entry_price  = ?,
                current_step     = ?,
                open_qty         = ?,
                entry_confirmed  = CASE WHEN ? > 0.01 THEN 1 ELSE 0 END,
                basket_start_time = CASE 
                    WHEN ? IS NOT NULL THEN ?
                    ELSE basket_start_time 
                END,
                cycle_phase      = CASE 
                    WHEN cycle_phase = 'CARRY_PENDING' AND ? >= 0.10 THEN 'ACTIVE' 
                    ELSE cycle_phase 
                END
            WHERE bot_id = ?
        """, (cost, avg, step, main_open_qty, cost, basket_time_update, basket_time_update, cost, bot_id))


        # Derive and update bot status
        # A bot is IN TRADE if it has: cost > 0, OR an outstanding position (main_open_qty > 0).
        # A hedge_child bot with a migrated position will have main_open_qty > 0 but cost=0 (audit entry has price=0), so we check both.
        if cost > 0.01 or main_open_qty > 1e-8:
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
    cycle_id: Optional[int] = None,
    exit_fill_ts: int = 0
) -> bool:
    """
    THE complete TP workflow. Atomic. No orphan orders possible after completion.

    Args:
        exit_fill_ts: Unix timestamp (seconds) of the TP fill event from the exchange.
                      Written to trades.cycle_start_time for the new cycle, anchoring
                      the cycle boundary to the actual exchange execution time.
                      Defaults to int(time.time()) inside reset_bot_after_tp if 0.
    """
    from engine.database import (
        get_connection, reset_bot_after_tp, log_trade, get_bot_status
    )
    from engine.exchange_interface import normalize_symbol

    logger.info(f"[TP-CASCADE] ▶ Starting TP cascade for Bot {bot_id} {pair} @ {exit_price:.6f}")

    try:
        conn = get_connection()
        bot_info = get_bot_status(bot_id)
        if not bot_info:
            logger.error(f"[TP-CASCADE] Bot {bot_id} not found.")
            return False

        # --- Step 0: Exchange Truth Check (Idempotency / Overshoot Guard) ---
        try:
            norm_pair = normalize_symbol(pair)
            phys_positions = exchange.fetch_positions()
            pos = next((p for p in phys_positions if p['symbol'] == norm_pair), None)
            phys_size = pos.get('contracts', 0) if pos else 0.0
            phys_side = str(pos.get('side', '')).lower() if pos else ''
            
            bot_direction = str(bot_info.get('direction', '')).upper()
            
            # Calculate other bots' exposure on this pair
            cursor = conn.execute(
                "SELECT SUM(open_qty) FROM trades t JOIN bots b ON t.bot_id = b.id "
                "WHERE b.pair = ? AND b.id != ? AND b.is_active = 1", (pair, bot_id)
            )
            other_bots_qty = float(cursor.fetchone()[0] or 0.0)
            
            # Overshoot detection: If physical size significantly exceeds other bots,
            # or side is completely flipped, we abort cascade to prevent ghosting.
            if phys_size > (other_bots_qty + 0.001):
                # If we are the only bot (other_bots_qty == 0), phys_size should be 0.
                is_overshoot = False
                if bot_direction == 'SHORT' and phys_side == 'long':
                    is_overshoot = True
                elif bot_direction == 'LONG' and phys_side == 'short':
                    is_overshoot = True
                    
                if is_overshoot:
                    logger.error(f"[TP-CASCADE] 🛑 ABORTING: Overshoot flip detected! Bot {bot_id} {pair} is {bot_direction} but physical side is {phys_side} ({phys_size}).")
                    return False
                else:
                    # Physical size exceeds other bots but side is correct — 
                    # this is normal for partial TPs. Do NOT abort.
                    logger.warning(f"[TP-CASCADE] Physical size {phys_size} > other_bots {other_bots_qty} "
                                   f"but direction correct. Proceeding with cascade.")
                    # Fall through — only abort on actual side flip
        except Exception as e_pos:
            logger.warning(f"[TP-CASCADE] Could not verify physical position before cascade: {e_pos}")

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

        # --- Step 1b: DB-Level Blanket Auto-Close Gate (Race-Condition Guard) ---
        # Root cause of "leaked fill" orphan: an entry/grid order may be placed but not
        # yet visible in fetch_open_orders() due to API propagation lag. That order can
        # then fill minutes or hours AFTER the TP cascade resets the cycle, creating a
        # phantom exchange position with no matching system record.
        #
        # Fix: After the exchange-level cancellation sweep, DB-lock ALL remaining
        # open/new/placing orders for this bot to 'auto_closed'. credit_fill() blocks
        # auto_closed rows, so any subsequent exchange fill for these orders is silently
        # rejected at the DB level — preventing the zombie ledger entry.
        try:
            db_locked = conn.execute(
                "UPDATE bot_orders SET status='auto_closed', notes=?, updated_at=? "
                "WHERE bot_id=? AND status IN ('open', 'new', 'placing')",
                (
                    f"TP_CASCADE_RACE_GUARD: locked at tp_ts={exit_fill_ts}",
                    int(time.time()),
                    bot_id
                )
            ).rowcount
            conn.commit()
            if db_locked > 0:
                logger.warning(
                    f"[TP-CASCADE] 🔒 RACE-GUARD: Bot {bot_id}: DB-locked {db_locked} "
                    f"in-flight orders to 'auto_closed' BEFORE cycle reset. "
                    f"These orders may still fill on exchange but will be silently rejected at credit_fill."
                )
        except Exception as e_lock:
            logger.warning(f"[TP-CASCADE] Bot {bot_id}: DB race-guard lock failed (non-fatal): {e_lock}")

        # --- Step 2: Log to trade_history ---
        try:
            from engine.database import recompute_invested_from_orders, get_bot_status
            
            # 🚀 RACE-CONDITION FIX (v2.3.5)
            # Do NOT trust the trades table for the TP Hit log. 
            # If seal_trade_state hasn't run yet, total_invested might be stale.
            # Recompute from the absolute ledger truth (bot_orders).
            invested, avg_entry, qty, current_step = recompute_invested_from_orders(bot_id)
            
            bot_state = get_bot_status(bot_id)
            if bot_state:
                direction = bot_state.get('direction', 'LONG')
                basket_start = int(bot_state.get('basket_start_time', 0) or 0)

                if avg_entry > 0 and exit_price > 0:
                    qty = invested / avg_entry
                    if direction.upper() == 'LONG':
                        pnl = qty * (exit_price - avg_entry)
                    else:
                        pnl = qty * (avg_entry - exit_price)
                    duration_s = int(time.time()) - basket_start if basket_start > 0 else 0

                    cost_usdc = qty * avg_entry
                    log_trade(
                        bot_id=bot_id,
                        action='TP_HIT',
                        symbol=pair,
                        price=exit_price,
                        amount=qty,
                        cost_usdc=cost_usdc,
                        pnl=pnl,
                        notes=(
                            f"TP @ {exit_price:.6f} | entry_avg={avg_entry:.6f} | "
                            f"pnl=${pnl:.4f} | step={current_step} | duration={duration_s}s"
                        )
                    )
        except Exception as e_log:
            logger.warning(f"[TP-CASCADE] Bot {bot_id}: trade_history log failed (non-fatal): {e_log}")

        # 🛡️ HEDGE CHILD: place break-even TP if parent has an active hedge child
        # Signal child with BE TP pending_placement BEFORE wiping parent.
        # This way, even if the parent wipe is blocked, the child is already protected.
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
                        be_cid = f"CQB_{child_id}_TP_{child_cycle}_BE"

                        # Check if active BE TP already exists
                        existing_be = _hc_conn.execute(
                            "SELECT id FROM bot_orders WHERE bot_id=? AND client_order_id LIKE ? "
                            "AND status IN ('pending_placement', 'open', 'new', 'partially_filled', 'pending', 'placing')",
                            (child_id, f"{be_cid}%")
                        ).fetchone()
                        if not existing_be:
                            # Register intent — actual order placed by bot_executor on next cycle
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
            logger.warning(
                f"⚠️ [HANDLE-TP-COMPLETION] Failed to register BE TP for hedge child "
                f"bot_id={locals().get('child_id', '?')}: {_hc_err}",
                exc_info=True
            )

        # --- Step 3: Full atomic reset via existing reset_bot_after_tp ---
        # This handles: mark reset_cleared, increment cycle_id, zero trades row
        # Pass the exchange fill timestamp so cycle_start_time is anchored to
        # the actual TP execution moment, not the engine processing time.
        try:
            reset_bot_after_tp(
                bot_id=bot_id,
                exit_price=exit_price,
                action_label='TP_HIT',
                notes=f'Cascade via ledger.handle_tp_completion @ {exit_price:.6f}',
                exit_fill_ts=exit_fill_ts,
                exchange=exchange,
            )
            logger.info(f"[TP-CASCADE] ✅ Bot {bot_id}: Reset to Scanning. Cycle {cycle_id} → {cycle_id + 1} (cst={exit_fill_ts}).")
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
