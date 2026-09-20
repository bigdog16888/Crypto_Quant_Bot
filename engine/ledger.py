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
  - mark_order_filled() is the ONLY path to set status='filled' without credit_fill().
    REQUIRES exchange_fill_id (exchange order ID) as proof of real fill.
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


def mark_order_filled(
    bot_id: int,
    order_id: str,
    exchange_fill_id: str,
    filled_qty: float,
    avg_price: float,
    order_type: str = 'entry',
    caller: str = ''
) -> bool:
    """
    Mark an order as FILLED with exchange proof.
    
    This is the ONLY function allowed to set bot_orders.status = 'filled'
    without going through credit_fill(). It requires a real exchange fill ID
    as evidence that the fill actually occurred on the exchange.
    
    Args:
        bot_id: The bot that owns this order.
        order_id: The client_order_id or order_id of our bot_orders row.
        exchange_fill_id: The REAL exchange order_id (from exchange API) that filled.
                         This is the proof that prevents phantom fills.
        filled_qty: Quantity filled.
        avg_price: Average fill price.
        order_type: For audit logging (entry/grid/tp/close/sl/etc).
        caller: Debug label for audit trail.
    
    Returns:
        True if successfully marked filled, False if not found or already filled.
    
    Raises:
        ValueError: If exchange_fill_id is missing or empty.
    
    Usage:
        # When we have a real exchange fill from REST fetch
        mark_order_filled(bot_id, client_order_id, exchange_order_id, qty, price, 'tp')
    """
    if not exchange_fill_id or not str(exchange_fill_id).strip():
        raise ValueError(
            f"[MARK-FILLED-REJECTED] Bot {bot_id} order {order_id}: "
            f"exchange_fill_id is REQUIRED. Cannot mark filled without exchange proof. "
            f"Caller: {caller}"
        )
    
    from engine.database import get_connection
    
    conn = get_connection()
    
    try:
        # Find the bot_orders row
        row = conn.execute(
            "SELECT id, filled_amount, amount, status, step, cycle_id, filled_at FROM bot_orders "
            "WHERE (order_id = ? OR client_order_id = ?) AND bot_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (order_id, order_id, bot_id)
        ).fetchone()
        
        if not row:
            logger.warning(
                f"[MARK-FILLED] Bot {bot_id} order {order_id}: no bot_orders row found. "
                f"Cannot mark filled. Caller: {caller}"
            )
            return False
        
        db_id, existing_fill, order_amount, current_status, row_step, row_cycle, existing_filled_at = row
        existing_fill = float(existing_fill or 0)
        order_amount = float(order_amount or 0)
        
        # Check if already filled
        if current_status in ('filled', 'closed'):
            logger.debug(
                f"[MARK-FILLED] Bot {bot_id} order {order_id}: already status={current_status}. "
                f"Caller: {caller}"
            )
            return True  # Idempotent - already filled
        
        # Prevent marking cancelled/rejected orders as filled (state machine blocks this)
        if current_status in ('cancelled', 'canceled', 'rejected', 'failed', 'expired'):
            logger.warning(
                f"[MARK-FILLED-REJECTED] Bot {bot_id} order {order_id}: status={current_status} "
                f"cannot transition to filled. Caller: {caller}"
            )
            return False
        
        # Record the fill with exchange_fill_id as proof
        # We use filled_amount as the evidence and set filled_at if not set
        actual_fill_ts = int(time.time())
        
        conn.execute(
            "UPDATE bot_orders SET "
            "filled_amount = ?, "
            "price = ?, "
            "status = 'filled', "
            "filled_at = CASE WHEN filled_at = 0 THEN ? ELSE filled_at END, "
            "updated_at = ?, "
            "notes = COALESCE(notes, '') || ? "
            "WHERE id = ?",
            (
                filled_qty,
                avg_price if avg_price > 0 else 0.0,
                actual_fill_ts,
                actual_fill_ts,
                f" | mark_order_filled: exchange_fill_id={exchange_fill_id}, caller={caller}, ts={actual_fill_ts}",
                db_id
            )
        )
        conn.commit()
        
        logger.info(
            f"[MARK-FILLED] Bot {bot_id} order {order_id} (db_id={db_id}): "
            f"marked FILLED with exchange_fill_id={exchange_fill_id}, "
            f"qty={filled_qty:.6f} @ {avg_price:.6f}, caller={caller}"
        )
        
        # Also increment open_qty for entry-type orders (same logic as credit_fill)
        _ENTRY_TYPES = ('entry', 'grid', 'adoption_add', 'adoption', 'forensic_adoption_add')
        if order_type in _ENTRY_TYPES:
            delta = filled_qty - existing_fill
            if delta > 0:
                conn.execute(
                    "UPDATE trades SET open_qty = ROUND(COALESCE(open_qty, 0) + ?, 8) WHERE bot_id = ?",
                    (delta, bot_id)
                )
                conn.commit()
        
        # Trigger seal_trade_state to sync trades table
        from engine.ledger import seal_trade_state
        seal_trade_state(bot_id)
        
        return True
        
    except ValueError:
        raise
    except Exception as e:
        logger.error(
            f"[MARK-FILLED-ERROR] Bot {bot_id} order {order_id}: {e}. Caller: {caller}"
        )
        try:
            conn.rollback()
        except Exception:
            pass
        return False


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
    from engine.database import get_connection
    conn = get_connection()
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
    exchange = None,
    suppress_cascade: bool = False,
    caller: str = '',
    side: str = '',  # REAL EXCHANGE SIDE ('BUY' or 'SELL')
) -> bool:
    from engine.write_queue import WriteQueue
    return WriteQueue().put_and_wait(
        _credit_fill_internal,
        bot_id,
        order_id,
        cumulative_qty,
        avg_price,
        order_type=order_type,
        is_cumulative=is_cumulative,
        fill_ts=fill_ts,
        sync_to_exchange=sync_to_exchange,
        exchange=exchange,
        suppress_cascade=suppress_cascade,
        caller=caller,
        side=side,
    )

def _credit_fill_internal(
    bot_id: int,
    order_id: str,
    cumulative_qty: float,
    avg_price: float,
    order_type: str = 'grid',
    is_cumulative: bool = True,
    fill_ts: int = 0,
    sync_to_exchange: bool = False,
    exchange = None,
    suppress_cascade: bool = False,
    caller: str = '',
    side: str = '',  # REAL EXCHANGE SIDE ('BUY' or 'SELL')
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

        # Get bot status to enforce REQUIRE_MANUAL_PROOF safety gates
        _b_row = conn.execute("SELECT status FROM bots WHERE id = ?", (bot_id,)).fetchone()
        bot_status = _b_row[0] if _b_row else ""
        is_gated = (bot_status == 'REQUIRE_MANUAL_PROOF')
        # Gated status blocks entry signals but NOT fill recording
        # Fill recording always proceeds regardless of status

        # Find the bot_orders row — try order_id first, then client_order_id
        row = conn.execute(
            "SELECT id, filled_amount, amount, status, step, cycle_id, client_order_id FROM bot_orders "
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

        db_id, existing_fill, order_amount, current_status, row_step, row_cycle, row_cid = row
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
        new_status = 'filled' if (fully_filled and not suppress_cascade) else 'partially_filled'

        # ── Fix 1 of the catchup fill-credit race (2026-09-04): record-on-terminal ──
        # A late WS fill on a terminal row is REAL exposure: the physical position
        # grew on the exchange, so the ledger row must carry the truth even though
        # the row was administratively cleared. We record the fill data on the row
        # WITHOUT un-terminalizing it and WITHOUT touching trades.open_qty — the
        # virtual-net contribution stays excluded (every reader filters on terminal
        # status), and attribution/adoption of the qty remains GTR/SNAP's audited
        # job. This mirrors the FILL-RESURRECTION precedent for 'cancelled' rows,
        # extended to administrative terminals with the safety of no re-credit.
        if current_status in ('reset_cleared', 'auto_closed'):
            _terminal_fill = float(cumulative_qty or 0)
            if _terminal_fill <= existing_fill:
                # Already recorded (idempotent MAX() semantics) — nothing new.
                logger.debug(
                    f"[CREDIT-FILL] Bot {bot_id} order {order_id}: status is {current_status} "
                    f"and cumulative {_terminal_fill:.6f} <= recorded {existing_fill:.6f} — skip."
                )
                return False
            logger.warning(
                f"📝 [FILL-LATE-TERMINAL] Bot {bot_id} order {order_id} was {current_status} "
                f"but WS delivered fill ({cumulative_qty:.6f} @ {avg_price:.6f}). "
                f"Recording fill on row for exchange truth; status stays {current_status}; "
                f"open_qty NOT incremented (adoption is GTR/SNAP's audited path)."
            )
            conn.execute(
                "UPDATE bot_orders SET filled_amount = ?, price = ?, "
                "filled_at = CASE WHEN filled_at = 0 THEN ? ELSE filled_at END, "
                "updated_at = ? WHERE id = ?",
                (cumulative_qty, avg_price if avg_price > 0 else None,
                 int(fill_ts if fill_ts > 0 else time.time()), int(time.time()), db_id)
            )
            conn.commit()
            return 'recorded_terminal'

        # ── INV-20: fill_claims singleton guard (post-status-check position —
        # Fix 2 of the catchup fill-credit race, 2026-09-04: claims must NOT burn
        # on refused fills; only a real credit attempt claims the slot) ────────
        # Atomically claim this (bot_id, order_id) slot. INSERT OR IGNORE ensures
        # only the first concurrent caller proceeds; all subsequent callers for the
        # same fill event see 0 rows_affected and early-return.
        # This eliminates the TOCTOU race where WS + REST both observe 0 filled_amount
        # and both attempt to credit the same fill.
        _claim_caller = caller or 'credit_fill'
        try:
            _claim_result = conn.execute(
                "INSERT OR IGNORE INTO fill_claims (bot_id, order_id, caller, claimed_at) "
                "VALUES (?, ?, ?, ?)",
                (bot_id, str(order_id), _claim_caller, int(time.time()))
            )
            conn.commit()
            if _claim_result.rowcount == 0:
                # Another caller already claimed this fill — this is a benign duplicate.
                logger.debug(
                    f"[FILL-CLAIM] Bot {bot_id} order {order_id}: already claimed "
                    f"(caller={_claim_caller}). Skipping duplicate credit."
                )
                return False
            logger.debug(
                f"[FILL-CLAIM] Bot {bot_id} order {order_id}: claimed by {_claim_caller}."
            )
        except Exception as _claim_err:
            # fill_claims table not yet created (first-run race) — fail-open to preserve fills.
            logger.warning(
                f"[FILL-CLAIM] Guard check failed for bot {bot_id} order {order_id}: "
                f"{_claim_err}. Proceeding without claim (fill_claims may not exist yet)."
            )
        # ────────────────────────────────────────────────────────────────────────

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
                        'forensic_adoption_reduce', 'flatten_close')

        if order_type in _ENTRY_TYPES and row_step is not None and row_cycle is not None and order_amount > 0:
            # Before the existing step saturation check, insert a step-level claim:
            try:
                _step_claim = conn.execute(
                    "INSERT OR IGNORE INTO fill_claims (bot_id, order_id, caller, claimed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (bot_id, f"STEP_{row_step}_{row_cycle}", f"step_lock_{_claim_caller}", int(time.time()))
                )
                conn.commit()
                if _step_claim.rowcount == 0:
                    logger.warning(
                        f"[STEP-LOCK] Bot {bot_id} step={row_step} cycle={row_cycle}: "
                        f"already being credited by another caller. Skipping."
                    )
                    return False
            except Exception as _sl_err:
                logger.warning(
                    f"[STEP-LOCK] Failed checking step lock for bot {bot_id} step={row_step}: "
                    f"{_sl_err}. Proceeding without lock."
                )

            try:
                already_credited = conn.execute(
                    "SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders "
                    "WHERE bot_id = ? AND step = ? AND cycle_id = ? "
                    "AND order_type IN ('entry','grid','adoption_add','adoption','forensic_adoption_add') "
                    "AND filled_amount > 0 "
                    "AND status NOT IN ('reset_cleared','auto_closed','cancelled','canceled','failed','rejected','reconciliation') "
                    "AND id != ?",
                    (bot_id, row_step, row_cycle, db_id)
                ).fetchone()[0] or 0.0

                delta_proposed = cumulative_qty - existing_fill
                capacity_limit = order_amount * 1.05  # 5% tolerance for rounding

                if already_credited > 0 and (already_credited + delta_proposed) > capacity_limit:
                    # Step is already saturated by another order_id. Normally this is
                    # a GTX chase duplicate: mark the row auto_closed and do not
                    # credit open_qty.
                    # ── Fix 3a of the catchup fill-credit race (2026-09-04) ──
                    # INV-30 catchup entries are NOT chase retries. They are NEW
                    # exchange orders placed because the child step is UNDER-covered,
                    # and INV-30 already counts inflight sibling amounts in its delta
                    # at placement time (status IN open/new/placing/cancelling), so a
                    # second catchup for the same step only exists when coverage was
                    # genuinely still short. Their fills are additive-by-design
                    # exposure: crediting them keeps virtual == physical. Refusing
                    # them (the pre-fix behavior) lost the fill and froze the pair.
                    _is_catchup = '_CATCHUP_' in str(row_cid or '') or '_CATCHUP_' in str(order_id or '')
                    if _is_catchup:
                        logger.warning(
                            f"🛡️ [STEP-SATURATED-CATCHUP-EXEMPT] Bot {bot_id} {order_type} "
                            f"step={row_step} cycle={row_cycle}: already credited "
                            f"{already_credited:.6f} via other order_id(s), but {order_id} is a "
                            f"catchup order (additive-by-design exposure). CREDITING its fill "
                            f"normally — virtual follows physical; no auto_close."
                        )
                        # Fall through to the normal credit path below.
                    else:
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

        # One-way shared book netting logic removed under ADR-006. Independent accounting active.

        conn.commit()

        # ── DUAL-WRITE TO IMMUTABLE EXCHANGE_FILLS LOG ──────────────────────────
        # Write the confirmed fill to the append-only exchange_fills log for
        # canonical position computation (position_ledger). Uses real exchange
        # side if provided, otherwise infers from bot direction + order_type.
        try:
            if side and side.upper() in ('BUY', 'SELL'):
                fill_side = side.upper()
            else:
                # Infer side: LONG bots BUY entries, SELL exits; SHORT bots SELL entries, BUY exits
                _bot_dir = conn.execute(
                    "SELECT direction FROM bots WHERE id = ?", (bot_id,)
                ).fetchone()
                _is_long = _bot_dir and 'long' in str(_bot_dir[0]).lower()
                if _otype_lower in _ENTRY_TYPES:
                    fill_side = 'BUY' if _is_long else 'SELL'
                else:
                    fill_side = 'SELL' if _is_long else 'BUY'

            # Guard (2026-09-14): delta<=0 on a cumulative call = replay or
            # sync-reduction — nothing NEW to log (writing cumulative_qty would
            # double-count the earlier delta row; UNIQUE(exchange_order_id,
            # fill_ts, qty, price) does not dedupe it — qty differs).
            # Incremental calls (is_cumulative=False) are exempt: their
            # cumulative_qty parameter IS the new increment.
            _log_fill = not (delta <= 0 and is_cumulative)

            from engine.database import record_exchange_fill
            _pair = conn.execute(
                "SELECT pair FROM bots WHERE id = ?", (bot_id,)
            ).fetchone()
            _symbol = _pair[0] if _pair else 'UNKNOWN'
            
            if _log_fill:
                record_exchange_fill(
                    conn=conn,
                    exchange_order_id=order_id,
                    client_order_id=row_cid or '',
                    symbol=_symbol,
                    side=fill_side,
                    qty=delta if delta > 0 else cumulative_qty,  # delta for new cumulative credit; cumulative_qty for incremental calls
                    price=avg_price if avg_price > 0 else 0.0,
                    fill_ts=actual_fill_ts,
                    source=caller or 'unknown',
                    bot_id=bot_id,
                    order_type=order_type,
                    step=row_step,
                    cycle_id=row_cycle,
                )
        except Exception as _ef_err:
            logger.warning(f"[EXCHANGE-FILLS] Dual-write failed for bot {bot_id} order {order_id}: {_ef_err}")
        # ─────────────────────────────────────────────────────────────────────────

        # Trigger hedge child signal if parent bot grid/entry fill is credited
        # This is for Fix 3A (real-time/online path)
        if _otype_lower in _ENTRY_TYPES and delta > 1e-12 and bot_status != 'REQUIRE_MANUAL_PROOF':
            try:
                _bot_row = conn.execute(
                    "SELECT name, pair, direction, hedge_child_bot_id, hedge_trigger_step FROM bots WHERE id = ?",
                    (bot_id,)
                ).fetchone()
                if _bot_row:
                    _bname, _bpair, _bdir, _child_id, _trigger = _bot_row
                    if _child_id and _trigger and _trigger > 0:
                        if row_step is not None and row_step >= _trigger:
                            if exchange is None:
                                _cfg_row = conn.execute("SELECT config FROM bots WHERE id = ?", (bot_id,)).fetchone()
                                _mtype = 'future'
                                if _cfg_row and _cfg_row[0]:
                                    try:
                                        import json
                                        _cfg = json.loads(_cfg_row[0])
                                        _mtype = _cfg.get('market_type', 'future')
                                    except Exception:
                                        pass
                                from engine.exchange_interface import ExchangeInterface
                                exchange = ExchangeInterface(market_type=_mtype)

                            # Get parent's actual fill price for this step (historical weighted avg)
                            _fill_price_row = conn.execute(
                                """SELECT ROUND(SUM(filled_amount * price) / NULLIF(SUM(filled_amount), 0), 8)
                                   FROM bot_orders
                                   WHERE bot_id = ? AND step = ? AND cycle_id = ?
                                   AND order_type IN ('entry', 'grid')
                                   AND status IN ('filled', 'partially_filled', 'closed')
                                   AND filled_amount > 0""",
                                (bot_id, row_step, row_cycle)
                            ).fetchone()
                            actual_fill_price = float(_fill_price_row[0] or 0) if _fill_price_row else 0.0
                            step_fill_price = actual_fill_price if actual_fill_price > 0 else avg_price

                            # Get the step quantity
                            _step_qty_row = conn.execute(
                                """SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders
                                   WHERE bot_id = ? AND step = ? AND cycle_id = ?
                                   AND order_type IN ('entry', 'grid')
                                   AND status IN ('filled', 'partially_filled', 'closed')
                                   AND filled_amount > 0""",
                                (bot_id, row_step, row_cycle)
                            ).fetchone()
                            _step_qty = float(_step_qty_row[0] or 0.0) if _step_qty_row else cumulative_qty

                            from engine.bot_executor import BotExecutor
                            executor = BotExecutor(runner=None)
                            
                            logger.info(
                                f"[HEDGE-REALTIME] Parent bot {bot_id} step {row_step} fill credited. "
                                f"Signaling hedge child {_child_id} synchronously. Price={step_fill_price:.4f}"
                            )
                            executor._signal_hedge_child_entry(
                                parent_bot_id=bot_id,
                                parent_name=_bname,
                                parent_step=row_step,
                                pair=_bpair,
                                direction=_bdir,
                                step_qty=_step_qty,
                                step_fill_price=step_fill_price,
                                exchange=exchange,
                                parent_cycle_id=row_cycle,
                                current_price=avg_price
                            )
            except Exception as _hedge_err:
                logger.error(f"❌ Failed to evaluate online hedge child signal for bot {bot_id}: {_hedge_err}")

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

def seal_trade_state(
    bot_id: int,
    force_recompute: bool = False,
    *,
    cycle_floor: int = None,
    exchange=None,
) -> Dict[str, Any]:
    """Public entry-point for sealing bot trade state.

    Args:
        cycle_floor: Optional[int]. When provided, passes straight through to
                 recompute_invested_from_orders(cycle_floor=...) so the FIFO
                 computation spans [cycle_floor, trades.cycle_id] instead of
                 just trades.cycle_id.  Used exclusively by the cross-cycle
                 orphan healer in the reconciler (v4.1.5).  All existing
                 callers omit this — behaviour is identical to before.
    """
    from engine.write_queue import WriteQueue
    return WriteQueue().put_and_wait(
        _seal_trade_state_internal,
        bot_id,
        force_recompute=force_recompute,
        cycle_floor=cycle_floor,
        exchange=exchange,
    )

def _seal_trade_state_internal(
    bot_id: int,
    force_recompute: bool = False,
    *,
    cycle_floor: int = None,
    exchange=None,
) -> Dict[str, Any]:
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

        # ── is_active guard: skip sealing for inactive bots ─────────────────────
        # Bots with is_active=0 are explicitly stopped by the operator and must not
        # have their status flipped to 'IN TRADE' or trades table modified by seal.
        # This prevents the pre-snapshot seal loop (cycle_loop.py:572) from
        # reactivating paused bots that have residual active_positions rows.
        # FAIL CLOSED: if the is_active lookup itself fails, skip the seal — do NOT
        # fall through to unguarded behavior.
        try:
            is_active_row = conn.execute(
                "SELECT is_active FROM bots WHERE id = ?", (bot_id,)
            ).fetchone()
            if is_active_row and is_active_row[0] == 0:
                logger.info(
                    f"[SEAL] Bot {bot_id}: is_active=0 — skipping seal (bot is explicitly stopped). "
                    f"Preserving existing status and trades state."
                )
                return {}
        except Exception as _ia_err:
            logger.warning(f"[SEAL] Bot {bot_id}: is_active check failed (non-fatal) — skipping seal: {_ia_err}")
            return {}
        # ────────────────────────────────────────────────────────────────────────


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
            conn.execute("UPDATE trades SET entry_confirmed = 1 WHERE bot_id = ?", (bot_id,))
            conn.commit()
    except Exception as _pf_err:
        logger.warning(f"[LEDGER-PREFLIGHT] Bot {bot_id}: pre-flight guard failed (non-fatal): {_pf_err}")
    # ────────────────────────────────────────────────────────────────────────

    # MECHANISM B FIX: For idle bots transitioning to Scanning, force recompute to only
    # scan the CURRENT cycle (not older cycles polluted by Mechanism A drift).
    # Detect this early by checking current cycle_id and whether it has fills.
    try:
        conn = get_connection()
        row_cycle = conn.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
        current_cycle_id = int(row_cycle[0]) if row_cycle and row_cycle[0] is not None else 1
        
        # Check if bot has fills in current cycle
        curr_cycle_fills = conn.execute(
            "SELECT COUNT(*) FROM bot_orders "
            "WHERE bot_id = ? AND cycle_id = ? "
            "AND order_type IN ('entry','grid','adoption','adoption_add','carry') "
            "AND status IN ('filled','partially_filled','closed','auto_closed','hedge_exited') "
            "AND filled_amount > 0",
            (bot_id, current_cycle_id)
        ).fetchone()
        has_current_cycle_fills = curr_cycle_fills and curr_cycle_fills[0] > 0
        
        # If bot has NO current-cycle fills and will end up flat (idle), 
        # restrict recompute to current cycle only
        if not has_current_cycle_fills and cycle_floor is None:
            # Bot appears idle - force recompute to only scan current cycle
            cycle_floor = current_cycle_id
            logger.debug(f"[SEAL] Bot {bot_id}: Idle bot detected (no cycle {current_cycle_id} fills). Restricting recompute to current cycle only.")
    except Exception as _cf_err:
        logger.warning(f"[SEAL] Bot {bot_id}: cycle_floor detection warning (non-fatal): {_cf_err}")

    try:
        cost, avg, qty, step = recompute_invested_from_orders(bot_id, cycle_floor=cycle_floor)
    except Exception as e:
        logger.error(f"[SEAL] recompute_invested_from_orders failed for bot {bot_id}: {e}")
        return {}
    main_open_qty = max(0.0, qty)
    has_real_position = abs(qty) > 1e-8

    # ── A2 FLAG: Detect active_positions / bot_orders mismatch ────────────────
    # Scenario: offline fill reconstruction updated active_positions (correct exchange position)
    # but bot_orders has no fills (wiped/reset). seal_trade_state would write 0 to trades.
    # FIX: Do NOT auto-write. Instead, flag for manual review via bots.notes.
    if main_open_qty <= 1e-8:
        try:
            conn_ap = get_connection()
            row_ap = conn_ap.execute(
                "SELECT size, entry_price, side FROM active_positions WHERE bot_id = ?",
                (bot_id,)
            ).fetchone()
            if row_ap:
                ap_size = float(row_ap[0] or 0)
                ap_entry = float(row_ap[1] or 0)
                ap_side = str(row_ap[2] or '').upper()
                if ap_size > 1e-8:
                    # MISMATCH DETECTED: active_positions has position, bot_orders has no fills
                    # Flag for manual review — do NOT auto-adopt
                    existing_notes = conn_ap.execute(
                        "SELECT COALESCE(notes, '') FROM bots WHERE id = ?", (bot_id,)
                    ).fetchone()
                    existing = existing_notes[0] if existing_notes else ''
                    flag = f"[MANUAL-REVIEW] A2 mismatch: active_positions has {ap_size:.6f} @ {ap_entry:.6f} ({ap_side}) but bot_orders has no fills. Review required."
                    new_notes = (existing + ' ' + flag).strip() if existing else flag
                    conn_ap.execute("UPDATE bots SET notes = ? WHERE id = ?", (new_notes, bot_id))
                    conn_ap.commit()
                    logger.warning(
                        f"[SEAL-A2-FLAG] Bot {bot_id}: Mismatch flagged for manual review — "
                        f"active_positions={ap_size:.6f} @ {ap_entry:.6f} ({ap_side}), "
                        f"bot_orders empty. NOT auto-adopting."
                    )
        except Exception as _a2_err:
            logger.warning(f"[SEAL-A2-FLAG] Bot {bot_id}: mismatch check failed (non-fatal): {_a2_err}")
    # ──────────────────────────────────────────────────────────────────────────────




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

        # Recalculate target_tp_price based on the new avg_entry_price
        new_tp = 0.0
        if main_open_qty > 1e-8 and avg > 0:
            try:
                row_bot = conn.execute("SELECT config, direction FROM bots WHERE id = ?", (bot_id,)).fetchone()
                if row_bot:
                    config_json, direction = row_bot
                    from engine.runner import BotRunner
                    runner_instance = BotRunner.get_instance()
                    
                    row_t = conn.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
                    cycle_id = row_t[0] if row_t else 1
                    
                    bot_status = {
                        'avg_entry_price': avg,
                        'total_invested': cost,
                        'current_step': step,
                        'cycle_id': cycle_id,
                        'direction': direction,
                        'open_qty': main_open_qty
                    }
                    
                    if runner_instance:
                        import json
                        bot_params = json.loads(config_json) if config_json else {}
                        strategy = runner_instance.get_strategy(bot_id, bot_params)
                        new_tp = strategy.calculate_take_profit_price(bot_status, avg)
                    else:
                        new_tp = avg * 1.015 if str(direction).upper() == 'LONG' else avg * 0.985
            except Exception as e_tp:
                logger.warning(f"[SEAL] Recalculate TP price failed for bot {bot_id}: {e_tp}")
                row_curr = conn.execute("SELECT target_tp_price FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
                new_tp = float(row_curr[0] or 0) if row_curr else 0.0

        # Write to trades
        conn.execute("""
            UPDATE trades SET
                total_invested   = ?,
                avg_entry_price  = ?,
                target_tp_price  = ?,
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
        """, (cost, avg, new_tp, step, main_open_qty, cost, basket_time_update, basket_time_update, cost, bot_id))


        # Derive and update bot status
        # A bot is IN TRADE if it has: cost > 0, OR an outstanding position (main_open_qty > 0).
        # A hedge_child bot with a migrated position will have main_open_qty > 0 but cost=0 (audit entry has price=0), so we check both.
        if cost > 0.01 or main_open_qty > 1e-8:
            new_status = 'IN TRADE'
        else:
            # Preserve pending_hedge_close status if it has an active hedge child
            has_active_child = False
            try:
                row_child = conn.execute(
                    "SELECT hedge_child_bot_id FROM bots WHERE id = ?", (bot_id,)
                ).fetchone()
                if row_child and row_child[0]:
                    child_id = row_child[0]
                    row_child_trade = conn.execute(
                        "SELECT open_qty FROM trades WHERE bot_id = ?", (child_id,)
                    ).fetchone()
                    if row_child_trade and float(row_child_trade[0] or 0) > 1e-8:
                        has_active_child = True
            except Exception as _hc_err:
                logger.warning(f"[SEAL] Bot {bot_id} active child check failed: {_hc_err}")

            if has_active_child:
                new_status = 'pending_hedge_close'
            else:
                new_status = 'Scanning'

        # Preserve REQUIRE_MANUAL_PROOF status if currently gated
        row_curr_status = conn.execute("SELECT status FROM bots WHERE id = ?", (bot_id,)).fetchone()
        curr_status = row_curr_status[0] if row_curr_status else ""
        if curr_status == 'REQUIRE_MANUAL_PROOF':
            # Only preserve gate if position is still open (entry fill)
            # If position is now flat (TP completed), clear the gate
            step_size_tolerance = 1e-5
            try:
                # Attempt to determine actual step size from exchange if runner exists
                row_pair = conn.execute("SELECT pair FROM bots WHERE id = ?", (bot_id,)).fetchone()
                if row_pair and row_pair[0]:
                    pair = row_pair[0]
                    from engine.runner import BotRunner
                    runner = BotRunner.get_instance()
                    if runner and runner.exchange:
                        prec = runner.exchange.get_symbol_precision(pair)
                        if prec and 'step_size' in prec:
                            step_size_tolerance = float(prec['step_size']) * 0.5
            except Exception:
                pass

            if main_open_qty <= step_size_tolerance:
                # Loophole fix: verify physical exchange parity before clearing gate
                has_parity = False
                active_exchange = exchange
                if not active_exchange:
                    try:
                        from engine.runner import BotRunner
                        runner = BotRunner.get_instance()
                        if runner and runner.exchange:
                            active_exchange = runner.exchange
                    except Exception:
                        pass

                if active_exchange:
                    try:
                        from engine.parity_gates import pair_parity_ok
                        ok, virt_net, phys_net, delta = pair_parity_ok(pair, active_exchange)
                        if ok:
                            has_parity = True
                            logger.info(f"[SEAL] Bot {bot_id}: Parity verified with exchange (delta={delta:.6f}).")
                        else:
                            logger.warning(
                                f"[SEAL] Bot {bot_id}: REQUIRE_MANUAL_PROOF NOT cleared. "
                                f"Pair {pair} is NOT in parity: virtual={virt_net:.6f}, "
                                f"physical={phys_net:.6f}, delta={delta:.6f}."
                            )
                    except Exception as e_parity:
                        logger.error(f"[SEAL] Bot {bot_id} parity check failed: {e_parity}")
                else:
                    logger.warning(f"[SEAL] Bot {bot_id}: REQUIRE_MANUAL_PROOF NOT cleared. Exchange is unavailable and cannot verify parity.")

                if has_parity:
                    new_status = 'Scanning'
                    logger.info(f"[SEAL] Bot {bot_id}: REQUIRE_MANUAL_PROOF cleared — position is now flat after TP.")
                    cascade_statuses = ('pending_close', 'FLATTENING', 'pending_flatten', 'pending_hedge_close')
                    cascade_ts = int(time.time()) if new_status in cascade_statuses else 0
                    conn.execute("UPDATE bots SET status = ?, cascade_started_at = ? WHERE id = ?", (new_status, cascade_ts, bot_id))
                else:
                    logger.info(f"[SEAL] Bot {bot_id}: REQUIRE_MANUAL_PROOF preserved — physical exchange position mismatch or unavailable")
            else:
                logger.info(f"[SEAL] Bot {bot_id}: REQUIRE_MANUAL_PROOF preserved — position still open (open_qty={main_open_qty:.6f})")
        else:
            cascade_statuses = ('pending_close', 'FLATTENING', 'pending_flatten', 'pending_hedge_close')
            cascade_ts = int(time.time()) if new_status in cascade_statuses else 0
            conn.execute("UPDATE bots SET status = ?, cascade_started_at = ? WHERE id = ?", (new_status, cascade_ts, bot_id))

        if new_status in ('Scanning', 'hedge_standby'):
            # Retrieve last fill timestamp for wipe wall and cycle start
            last_fill_row = conn.execute(
                "SELECT MAX(created_at) FROM bot_orders "
                "WHERE bot_id = ? AND status IN ('filled','partially_filled') "
                "AND filled_amount > 0",
                (bot_id,)
            ).fetchone()
            now_ts = int(last_fill_row[0]) if (last_fill_row and last_fill_row[0] is not None) else int(time.time())
            
            # Get the CURRENT cycle_id from trades (the cycle we just sealed)
            row_cycle = conn.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,)).fetchone()
            current_cycle_id = int(row_cycle[0]) if row_cycle and row_cycle[0] is not None else 1

            # MECHANISM B FIX: Only increment cycle_id if there are ACTUAL fills in the CURRENT cycle.
            # Check if there are any filled entry/grid orders in the current cycle_id.
            # If zero current-cycle fills, the bot is idle - do NOT increment cycle_id.
            # This prevents drift when Mechanism A put fills in wrong old cycles.
            curr_cycle_fills = conn.execute(
                "SELECT COUNT(*) FROM bot_orders "
                "WHERE bot_id = ? AND cycle_id = ? "
                "AND order_type IN ('entry','grid','adoption','adoption_add','carry') "
                "AND status IN ('filled','partially_filled','closed','auto_closed','hedge_exited') "
                "AND filled_amount > 0",
                (bot_id, current_cycle_id)
            ).fetchone()
            has_current_cycle_fills = curr_cycle_fills and curr_cycle_fills[0] > 0

            # Determine if the last filled order in the CURRENT cycle was a TP
            last_exit_row = conn.execute(
                "SELECT order_type FROM bot_orders "
                "WHERE bot_id = ? AND cycle_id = ? "
                "AND status IN ('filled','partially_filled') "
                "AND filled_amount > 0 AND order_type IS NOT NULL "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (bot_id, current_cycle_id)
            ).fetchone()
            last_exit_type = str(last_exit_row[0]).lower() if last_exit_row else None

            # Increment cycle_id ONLY if:
            # 1. Bot is actually transitioning from active state (IN TRADE or REQUIRE_MANUAL_PROOF)
            # 2. There ARE fills in the current cycle (bot was actually trading this cycle)
            # 3. The last exit in current cycle was NOT a TP (TP handled by reset_bot_after_tp)
            is_transitioning = curr_status in ('IN TRADE', 'REQUIRE_MANUAL_PROOF')
            should_increment = is_transitioning and has_current_cycle_fills and last_exit_type != 'tp'

            if should_increment:
                conn.execute("""
                    UPDATE trades 
                    SET total_invested = 0, avg_entry_price = 0, current_step = 0, 
                        entry_confirmed = 0, cycle_phase = 'IDLE',
                        cycle_id = COALESCE(cycle_id, 1) + 1,
                        entry_order_id = NULL, tp_order_id = NULL, open_qty = 0,
                        wipe_wall_ts = ?, cycle_start_time = ?
                    WHERE bot_id = ?
                """, (now_ts, now_ts, bot_id))
                logger.info(f"🌉 [SEAL-CYCLE-RESET] Bot {bot_id}: Transitioned to flat. Cycle incremented (had {curr_cycle_fills[0]} current-cycle fills).")
            else:
                # Ensure wipe_wall_ts and cycle_start_time are set even if not incrementing
                # Preserve real position if it exists (has_real_position)
                if has_real_position:
                    conn.execute("""
                        UPDATE trades 
                        SET total_invested = ?, avg_entry_price = ?, current_step = ?, 
                            entry_confirmed = CASE WHEN ? > 0.01 THEN 1 ELSE 0 END, cycle_phase = 'IDLE',
                            entry_order_id = NULL, tp_order_id = NULL, open_qty = ?,
                            wipe_wall_ts = ?, cycle_start_time = ?
                        WHERE bot_id = ?
                    """, (cost, avg, step, cost, qty, now_ts, now_ts, bot_id))
                else:
                    conn.execute("""
                        UPDATE trades 
                        SET total_invested = 0, avg_entry_price = 0, current_step = 0, 
                            entry_confirmed = 0, cycle_phase = 'IDLE',
                            entry_order_id = NULL, tp_order_id = NULL, open_qty = 0
                        WHERE bot_id = ?
                    """, (bot_id,))
                if not has_current_cycle_fills:
                    logger.info(f"🌉 [SEAL-CYCLE-SKIP] Bot {bot_id}: No current-cycle fills (cycle={current_cycle_id}). Cycle NOT incremented.")
                elif last_exit_type == 'tp':
                    logger.info(f"🌉 [SEAL-CYCLE-SKIP] Bot {bot_id}: Last exit was TP in current cycle. Cycle NOT incremented (reset_bot_after_tp handles).")
                else:
                    logger.info(f"🌉 [SEAL-CYCLE-SKIP] Bot {bot_id}: Not transitioning from active state (status={curr_status}). Cycle NOT incremented.")
            
            # Fix 4 (catchup fill-credit race, 2026-09-04): fill_claims are kept —
            # dedup history must survive seals so late/re-delivered fills can't
            # double-credit. Pruning is handled by 30-day retention.

        conn.commit()


        logger.debug(
            f"[SEAL] ✅ Bot {bot_id}: cost=${cost:.4f} avg={avg:.4f} "
            f"qty={qty:.6f} step={step} → {new_status}"
        )

        return {'qty': qty, 'cost': cost, 'avg': avg, 'step': step, 'status': new_status}

    except Exception as e:
        logger.error(f"[SEAL] Failed to write trades for bot {bot_id}: {e}")
        return {}


def seal_all_active_bots(skip_bot_ids: set = None) -> int:
    """
    Run seal_trade_state() for all active bots.
    Used at startup to ensure trades table is consistent before any trading.
    Returns the count of bots that had their state corrected.
    If skip_bot_ids is provided, bots in that set are NOT sealed
    (used by the O-9 startup plausibility gate to skip implausible pairs).
    """
    from engine.database import get_connection

    corrected = 0
    skip_bot_ids = skip_bot_ids or set()
    try:
        conn = get_connection()
        bots = conn.execute(
            "SELECT id, name FROM bots WHERE is_active = 1"
        ).fetchall()

        for bot_id, bot_name in bots:
            if bot_id in skip_bot_ids:
                logger.warning(f"[SEAL-ALL] Skipping bot {bot_name} (id={bot_id}): plausibility-gated.")
                continue
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
                "WHERE bot_id=? AND status IN ('open', 'new', 'placing', 'cancelling')",
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

        has_hedge_child_open = False
        child_id = None
        child_open_qty_val = 0.0
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
                    child_open_qty_val = float(child_open_qty or 0)
                    child_avg = float(child_avg or 0)
                    if child_open_qty_val > 0.0001 and child_avg > 0:
                        has_hedge_child_open = True
                        child_direction = _hc_conn.execute(
                            "SELECT direction FROM bots WHERE id=?", (child_id,)
                        ).fetchone()[0]
                        # The hedge child TP price is: current_price if the position is already profitable, otherwise avg_entry_price.
                        be_price = _calc_hedge_tp_price(child_direction, child_avg, exit_price)
                        be_cid = f"CQB_{child_id}_TP_{child_cycle}_BE"

                        # Check if active BE TP already exists
                        existing_be = _hc_conn.execute(
                            "SELECT id FROM bot_orders WHERE bot_id=? AND client_order_id LIKE ? "
                            "AND status IN ('pending_placement', 'open', 'new', 'partially_filled', 'pending', 'placing', 'cancelling')",
                            (child_id, f"{be_cid}%")
                        ).fetchone()
                        if not existing_be:
                            # Register intent — actual order placed by bot_executor on next cycle
                            from engine.database import save_bot_order
                            save_bot_order(
                                child_id, 'tp', f'PENDING_BE_{child_id}_{child_cycle}',
                                be_price, child_open_qty_val, step=0,
                                status='pending_placement',
                                client_order_id=be_cid,
                                notes=f"Break-even TP pending placement: parent {bot_id} TP hit",
                                cycle_id=child_cycle,
                            )
                            logger.info(
                                f"[HEDGE-BE-TP] Child {child_id}: break-even TP registered "
                                f"@ {be_price:.4f} for {child_open_qty_val:.6f} {child_direction}. "
                                f"Will be placed by bot_executor on next cycle."
                            )
        except Exception as _hc_err:
            logger.warning(
                f"⚠️ [HANDLE-TP-COMPLETION] Failed to register BE TP for hedge child "
                f"bot_id={locals().get('child_id', '?')}: {_hc_err}",
                exc_info=True
            )

        if has_hedge_child_open and child_id:
            logger.info(f"[HEDGE-GATE] Parent {bot_id}: active hedge child {child_id} has position {child_open_qty_val:.6f}. Placing parent in pending_hedge_close.")
            try:
                # Seal parent trade state first to ensure trades.open_qty matches ledger truth
                from engine.ledger import seal_trade_state
                seal_trade_state(bot_id)

                reset_bot_after_tp(
                    bot_id=bot_id,
                    exit_price=exit_price,
                    action_label='TP_HIT',
                    notes=f'Hedge gate pending parent close. Child open qty: {child_open_qty_val:.6f}',
                    exit_fill_ts=exit_fill_ts,
                    exchange=exchange,
                    skip_cycle_increment=True
                )
                
                # Cancel parent exchange orders (stray grids, etc.)
                if exchange and pair:
                    try:
                        exchange.cancel_orders_by_bot_id(bot_id, pair)
                    except Exception as e_cancel:
                        logger.error(f"[HEDGE-GATE] Failed to cancel parent exchange orders: {e_cancel}")
                
                return True
            except Exception as e_gate:
                logger.error(f"[HEDGE-GATE] Failed to transition parent to pending_hedge_close: {e_gate}")
                return False

        # --- Step 3: Full atomic reset via existing reset_bot_after_tp ---
        # This handles: mark reset_cleared, increment cycle_id, zero trades row
        # Pass the exchange fill timestamp so cycle_start_time is anchored to
        # the actual TP execution moment, not the engine processing time.
        try:
            # Seal trade state first to ensure trades.open_qty matches the ledger truth after the TP fill.
            from engine.ledger import seal_trade_state
            seal_trade_state(bot_id)

            _row_qty = conn.execute(
                "SELECT open_qty, direction, cycle_id FROM trades t JOIN bots b ON b.id = t.bot_id WHERE t.bot_id = ?",
                (bot_id,)
            ).fetchone()
            _open_qty = float(_row_qty[0] or 0) if _row_qty else 0.0
            _direction = str(_row_qty[1] or 'LONG').upper() if _row_qty else 'LONG'
            _cycle_id = int(_row_qty[2] or 1) if _row_qty else 1

            if _open_qty > 0.0001:
                logger.warning(
                    f"[TP-CASCADE] 🛑 Bot {bot_id}: open_qty={_open_qty:.6f} > 0 after TP fill. "
                    f"Grid orders filled after TP placed. Transitioning to PARTIAL_CLOSE_PENDING."
                )
                conn.execute(
                    "UPDATE trades SET cycle_phase = 'PARTIAL_CLOSE_PENDING' WHERE bot_id = ?",
                    (bot_id,)
                )
                conn.commit()

                # Place a reduce-only close order for the remaining open_qty
                close_side = 'sell' if _direction == 'LONG' else 'buy'
                close_cid = f"CQB_{bot_id}_CLOSE_{_cycle_id}_{int(time.time())}"
                from engine.database import save_bot_order
                save_bot_order(
                    bot_id, 'close', close_cid,
                    price=0.0, amount=_open_qty, step=0,
                    status='pending_placement',
                    client_order_id=close_cid,
                    notes=f"PARTIAL_CLOSE_PENDING: Close remaining position of {_open_qty:.6f}",
                    cycle_id=_cycle_id
                )
                try:
                    _testnet = bool(getattr(exchange, 'is_testnet', False) or
                                    getattr(getattr(exchange, 'exchange', None), 'sandbox', False))
                    _params = {
                        'reduceOnly': True,
                        'newClientOrderId': close_cid,
                    }
                    from engine.bot_executor import BotExecutor
                    _params = BotExecutor._resolve_position_side_param(_params, _testnet)
                    close_order = exchange.create_order(
                        normalize_symbol(pair), 'market', close_side, _open_qty,
                        params=_params
                    )
                    if close_order and isinstance(close_order, dict):
                        conn.execute(
                            "UPDATE bot_orders SET order_id = ?, status = ?, updated_at = ? WHERE client_order_id = ?",
                            (close_order['id'], close_order.get('status', 'open'), int(time.time()), close_cid)
                        )
                        conn.commit()
                        logger.info(f"[TP-CASCADE] Placed close order {close_order['id']} for remaining {_open_qty:.6f}")
                except Exception as e_close:
                    logger.error(f"[TP-CASCADE] Failed to place close order: {e_close}")
                return False

            reset_bot_after_tp(
                bot_id=bot_id,
                exit_price=exit_price,
                action_label='TP_HIT',
                notes=f'Cascade via ledger.handle_tp_completion @ {exit_price:.6f}',
                exit_fill_ts=exit_fill_ts,
                exchange=exchange,
            )
            logger.info(f"[TP-CASCADE] ✅ Bot {bot_id}: Reset to Scanning. Cycle {cycle_id} → {cycle_id + 1} (cst={exit_fill_ts}).")

            # Hook for hedge child TP completion
            try:
                _bt_row = conn.execute(
                    "SELECT bot_type, parent_bot_id FROM bots WHERE id = ?", (bot_id,)
                ).fetchone()
                if _bt_row and _bt_row[0] == 'hedge_child' and _bt_row[1]:
                    parent_bot_id = _bt_row[1]
                    logger.info(f"[HEDGE-COMPLETE] Bot {bot_id} is a hedge child. Unblocking parent {parent_bot_id}.")
                    complete_parent_cycle_after_hedge(parent_bot_id, exchange)
            except Exception as e_hook:
                logger.error(f"[HEDGE-COMPLETE] Failed to trigger parent unblock hook: {e_hook}")

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


def complete_parent_cycle_after_hedge(parent_bot_id: int, exchange=None):
    """
    Called when a hedge child's BE TP fills. Idempotently completes the parent
    cycle that was waiting in pending_hedge_close.
    """
    from engine.database import get_connection, reset_bot_after_tp
    conn = get_connection()
    try:
        # Check if parent is in pending_hedge_close
        parent_row = conn.execute(
            "SELECT status FROM bots WHERE id = ?", (parent_bot_id,)
        ).fetchone()
        if not parent_row or parent_row[0] != 'pending_hedge_close':
            return
            
        logger.info(f"[HEDGE-UNBLOCK] Parent bot {parent_bot_id} in pending_hedge_close. Completing cycle reset.")
        reset_bot_after_tp(
            bot_id=parent_bot_id,
            exit_price=0.0,
            action_label='HEDGE_UNBLOCK',
            notes='Hedge child TP complete, parent cycle incremented.',
            exchange=exchange,
            skip_cycle_increment=False
        )
    except Exception as e:
        logger.error(f"❌ [HEDGE-UNBLOCK] Failed to complete parent cycle for bot {parent_bot_id}: {e}")


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

    # 🛡️ FREEZE-GUARD: frozen bots must not be flattened (config + status)
    _fg_conn = get_connection()
    _fg_row = _fg_conn.execute("SELECT status, name FROM bots WHERE id=?", (bot_id,)).fetchone()
    _fg_status = _fg_row[0] if _fg_row else None
    _fg_name = _fg_row[1] if _fg_row else str(bot_id)
    from config.settings import config as _fg_config
    if _fg_config.is_bot_frozen(bot_id, _fg_status):
        logger.critical(
            f"🛑 [FREEZE-GUARD] Bot {bot_id} ({_fg_name}) blocked from handle_flatten ({reason}): frozen "
            f"(excluded={bot_id in _fg_config.STARTUP_EXCLUDED_BOT_IDS}, "
            f"manual_proof={_fg_status == 'REQUIRE_MANUAL_PROOF'})"
        )
        return False

    logger.warning(f"[FLATTEN] ▶ Starting {reason} flatten for Bot {bot_id} {pair}")

    try:
        conn = get_connection()
        norm_pair = normalize_symbol(pair)

        # --- Step 1: Set FLATTENING status to block new orders ---
        conn.execute("UPDATE bots SET status='FLATTENING', cascade_started_at=? WHERE id=?", (int(time.time()), bot_id))
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
                        # Rule #0: positionSide must NEVER be sent to mainnet (causes -4061).
                        # Testnet requires positionSide='BOTH'; mainnet must omit it entirely.
                        _flatten_is_testnet = bool(getattr(exchange, 'is_testnet', False) or
                                                   getattr(getattr(exchange, 'exchange', None), 'sandbox', False))
                        _flatten_params: dict = {
                            'reduceOnly': True,
                            'newClientOrderId': cid,
                        }
                        if _flatten_is_testnet:
                            _flatten_params['positionSide'] = 'BOTH'
                        # mainnet: positionSide absent — Binance One-Way mode requirement
                        close_order = exchange.create_order(
                            norm_pair, 'market', close_side, qty,
                            params=_flatten_params
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

def _calc_hedge_tp_price(direction: str, avg_entry_price: float, current_price: float) -> float:
    """
    Calculate the TP price for a hedge child bot.
    - For SHORT:
      - If current_price < avg_entry_price -> profitable -> close at current_price immediately.
      - Otherwise -> losing/break-even -> close at avg_entry_price (resting limit order).
    - For LONG:
      - If current_price > avg_entry_price -> profitable -> close at current_price immediately.
      - Otherwise -> losing/break-even -> close at avg_entry_price (resting limit order).
    """
    if direction.upper() == 'SHORT':
        if current_price < avg_entry_price:
            return current_price
        else:
            return avg_entry_price
    else:  # LONG child
        if current_price > avg_entry_price:
            return current_price
        else:
            return avg_entry_price

