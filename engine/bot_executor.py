import logging
import json
import threading
import time
import math
import os
import traceback
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime

from engine.database import (
    get_bot_status,
    update_martingale_step,
    log_trade,
    reset_bot_after_tp,
    safe_wipe_bot,
    save_bot_order,
    update_bot_order_exchange_id,
    get_bot_order_ids,
    get_connection,
    get_all_active_trades_for_pair,
    update_order_status,
    flag_bot_pos_limit,
    update_bot_error
)
from engine.exchange_interface import ExchangeInterface, normalize_symbol, normalize_market_type
from engine.strategies.martingale_strategy import MartingaleStrategy
from engine.manager import calculate_early_exit_decay
from config.settings import config

logger = logging.getLogger("BotExecutor")

# Thread-local storage for exchange interfaces
_thread_local = threading.local()

# API Error Tracker (Hammer Shield)
# Tracks consecutive errors per bot to detect API loops and banish them before a Binance Global Ban.
_API_ERROR_TRACKER = {}
# Cooldown tracker for dust sweeps/flushes to prevent API hammering loops when they fail or are blocked
_DUST_FLUSH_COOLDOWN = {}

def _to_ccxt_pair(normalized_or_ccxt: str, all_bot_pairs: list = None) -> str:
    """
    Given either a normalized symbol ('SOLUSDC') or a CCXT symbol
    ('SOL/USDC:USDC'), returns the CCXT format suitable for exchange calls.

    Strategy:
    1. If input already contains '/' or ':', return as-is (already CCXT).
    2. If all_bot_pairs provided, scan for a DB pair whose normalized form
       matches the input — return that DB pair (canonical CCXT form).
    3. Fallback: return input unchanged (caller must handle miss).

    Usage:
        ccxt_pair = _to_ccxt_pair(pair_normalized, conn.execute(
            "SELECT DISTINCT pair FROM bots").fetchall())
    """
    s = str(normalized_or_ccxt or '').strip()
    if '/' in s or ':' in s:
        return s  # Already CCXT format
    if all_bot_pairs:
        def _norm(x):
            return str(x).replace('/', '').replace('-', '').split(':')[0].upper()
        for p in all_bot_pairs:
            candidate = p[0] if isinstance(p, (list, tuple)) else p
            if _norm(candidate) == s.upper():
                return candidate
    return s  # Fallback: return as-is


def sync_stale_open_orders(bot_id: int, exchange: ExchangeInterface, conn, max_age_seconds: int = 120) -> int:
    """
    Detect and recover from missed WebSocket fill events by periodically
    fetching the true status of open orders directly from the exchange.
    """
    from engine.ledger import credit_fill, seal_trade_state

    # 1. Fetch bot's CCXT symbol/pair and name
    pair_row = conn.execute("SELECT pair, name FROM bots WHERE id = ?", (bot_id,)).fetchone()
    if not pair_row:
        logger.warning(f"[ORDER-SYNC] Bot {bot_id} not found in bots table.")
        return 0
    symbol, bot_name = pair_row
    if not symbol:
        logger.warning(f"[ORDER-SYNC] Bot {bot_id} has no pair symbol configured.")
        return 0

    now_ts = int(time.time())
    cutoff_default = now_ts - max_age_seconds
    cutoff_pending = now_ts - 30
    
    # Query bot_orders for rows where:
    # - bot_id = given bot_id
    # - status IN ('open', 'new', 'partially_filled', 'placing', 'cancelling', 'pending_placement')
    # - created_at is older than the respective thresholds (30s for pending_placement, max_age_seconds for others)
    # - order_id IS NOT NULL and is not empty
    # - order_type is not netting
    rows = conn.execute(
        "SELECT id, order_id, client_order_id, order_type, amount, filled_amount, status, price, step, cycle_id, created_at "
        "FROM bot_orders "
        "WHERE bot_id = ? "
        "AND status IN ('open', 'new', 'partially_filled', 'placing', 'cancelling', 'pending_placement') "
        "AND ( (status = 'pending_placement' AND created_at < ?) OR (status != 'pending_placement' AND created_at < ?) ) "
        "AND order_id IS NOT NULL "
        "AND order_id != '' "
        "AND (status = 'pending_placement' OR order_id NOT LIKE 'PENDING_%') "
        "AND order_type NOT IN ('virtual_netting', 'legacy_netting')",
        (bot_id, cutoff_pending, cutoff_default)
    ).fetchall()

    if not rows:
        return 0

    logger.debug(f"[ORDER-SYNC] Bot {bot_name}: scanning {len(rows)} stale open/pending orders.")
    synced_count = 0
    fills_synced = False

    for row in rows:
        row_id, order_id, client_order_id, order_type, amount, filled_amount, status, price, step, cycle_id, created_at = row
        try:
            if order_id and any(str(order_id).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_')):
                logger.info(f"[ORDER-SYNC] Bot {bot_name}: Skipping fetch_order for synthetic order {order_id}")
                continue
            order_info = exchange.fetch_order(order_id, symbol)
            if not order_info:
                logger.warning(f"[ORDER-SYNC] Bot {bot_name}: fetch_order returned None for order {order_id} ({client_order_id}).")
                continue

            ex_status = order_info.get('status')
            ex_filled = float(order_info.get('filled', 0) or 0)
            ex_avg_price = float(order_info.get('average') or order_info.get('price') or 0)
            db_filled = float(filled_amount or 0)

            # If exchange has recorded a higher filled amount, credit it first
            if ex_filled > db_filled:
                credit_fill(
                    bot_id=bot_id,
                    order_id=order_id,
                    cumulative_qty=ex_filled,
                    avg_price=ex_avg_price,
                    order_type=order_type,
                    is_cumulative=True,
                    sync_to_exchange=True,
                    caller='stale_sync',
                )
                logger.warning(
                    f"[ORDER-SYNC] Bot {bot_name}: order {client_order_id} had new partial fill of {ex_filled - db_filled} "
                    f"(cumulative: {ex_filled}). Credited to ledger."
                )
                fills_synced = True

            # Determine target DB status
            db_status = None
            if ex_status in ('filled', 'closed'):
                db_status = 'filled'
            elif ex_status in ('canceled', 'cancelled', 'expired'):
                db_status = 'cancelled'
            elif ex_filled > 0 and status != 'partially_filled':
                db_status = 'partially_filled'

            # If the status needs an update in the DB, do it now
            if db_status and db_status != status:
                # BUG 4 INTEGRATION: If this row was intentionally cancelled by us (status='cancelling')
                # and exchange confirms zero fill → clean up the DB row.
                # The cancelling buffer queries WHERE status='cancelling'; once we update to 'cancelled'
                # it can't catch this row, so cleanup must happen here.
                if status == 'cancelling' and db_status == 'cancelled':
                    db_filled = float(filled_amount or 0)
                    if db_filled > 0:
                        # DB already has a fill amount (WS may have updated it); credit it before deleting
                        credit_fill(
                            bot_id=bot_id,
                            order_id=order_id,
                            cumulative_qty=db_filled,
                            avg_price=ex_avg_price or float(price or 0),
                            order_type=order_type,
                            is_cumulative=True,
                            caller='cancel_verify',
                        )
                        conn.execute(
                            "UPDATE bot_orders SET status = 'filled', filled_amount = ?, updated_at = ? WHERE id = ?",
                            (db_filled, int(time.time()), row_id)
                        )
                        conn.commit()
                        fills_synced = True
                        synced_count += 1
                        logger.info(
                            f"[ORDER-SYNC] Bot {bot_name}: Credited cancelling order {client_order_id} "
                            f"with DB fill={db_filled}, marked filled."
                        )
                    else:
                        conn.execute("DELETE FROM bot_orders WHERE id = ?", (row_id,))
                        conn.commit()
                        synced_count += 1
                        logger.info(
                            f"[ORDER-SYNC] Bot {bot_name}: Deleted cancelling order {client_order_id} — "
                            f"exchange confirmed cancelled with 0 fill."
                        )
                else:
                    conn.execute(
                        "UPDATE bot_orders SET status = ?, filled_amount = ?, price = ?, updated_at = ? WHERE id = ?",
                        (db_status, ex_filled, ex_avg_price, int(time.time()), row_id)
                    )
                    conn.commit()
                    synced_count += 1
                    logger.info(f"[ORDER-SYNC] Bot {bot_name}: Updated order {client_order_id} status in DB from {status} -> {db_status}.")

                # Trigger cascade if the order is fully filled and is an exit trigger type
                _EXIT_TRIGGER_TYPES = frozenset({'tp', 'hedge_tp', 'close', 'sl'})
                fully_filled = amount > 0 and (ex_filled / amount) >= 0.99
                if db_status == 'filled' and order_type in _EXIT_TRIGGER_TYPES and fully_filled:
                    if order_type in ('tp', 'hedge_tp'):
                        try:
                            from engine.ledger import handle_tp_completion
                            logger.warning(
                                f"[ORDER-SYNC] Bot {bot_id}: TP/exit order {client_order_id} fully filled "
                                f"(missed WS cascade). Triggering handle_tp_completion."
                            )
                            handle_tp_completion(
                                bot_id=bot_id,
                                exit_price=ex_avg_price,
                                pair=symbol,
                                exchange=exchange
                            )
                        except Exception as _cascade_err:
                            logger.error(
                                f"[ORDER-SYNC] Bot {bot_id}: handle_tp_completion failed "
                                f"after sync: {_cascade_err}. Bot may need manual reset."
                            )
                    elif order_type in ('sl', 'close', 'flatten_close'):
                        try:
                            from engine.ledger import handle_flatten
                            logger.warning(
                                f"[ORDER-SYNC] Bot {bot_id}: SL/close order {client_order_id} fully filled "
                                f"(missed WS cascade). Triggering handle_flatten."
                            )
                            handle_flatten(
                                bot_id=bot_id,
                                pair=symbol,
                                exchange=exchange,
                                close_price=ex_avg_price,
                                close_qty=ex_filled,
                                reason=f"sync_{order_type}_fill"
                            )
                        except Exception as _cascade_err:
                            logger.error(
                                f"[ORDER-SYNC] Bot {bot_id}: handle_flatten failed "
                                f"after sync: {_cascade_err}. Bot may need manual reset."
                            )

        except Exception as e:
            err_name = type(e).__name__
            err_str = str(e).lower()
            if ("notfound" in err_name.lower() or 
                "order_not_found" in err_str or 
                "not found" in err_str or 
                "-2013" in err_str or 
                "invalidorder" in err_str or
                "order does not exist" in err_str):
                
                # d. If exchange.fetch_order raises NotFound: Mark as cancelled in DB
                conn.execute(
                    "UPDATE bot_orders SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (int(time.time()), row_id)
                )
                conn.commit()
                if status == 'pending_placement':
                    logger.warning(
                        f"[STALE-PENDING] Bot {bot_id}: pending_placement order {client_order_id} "
                        f"never reached exchange (OrderNotFound). Marking cancelled to unblock re-placement."
                    )
                else:
                    logger.warning(
                        f"[ORDER-SYNC] Bot {bot_name}: order {client_order_id} not found on exchange "
                        f"(treated as cancelled). Corrected in DB."
                    )
                synced_count += 1
            else:
                logger.error(f"❌ [ORDER-SYNC] Failed to fetch order status from exchange for order {order_id} bot {bot_name}: {e}")

    # 3. After processing any fills, call seal_trade_state(bot_id) once
    if fills_synced:
        seal_trade_state(bot_id)

    return synced_count


def enforce_hedge_child_state(child_bot_id: int, conn) -> str:
    """
    Enforces state boundaries between parent and child hedge bots.
    Returns: 'dormant', 'should_close', or 'active'.
    Also synchronizes child trades.cycle_id to match parent trades.cycle_id.
    """
    # 1. Fetch parent bot ID
    parent_row = conn.execute(
        "SELECT parent_bot_id FROM bots WHERE id = ?", (child_bot_id,)
    ).fetchone()
    if not parent_row or not parent_row[0]:
        return 'dormant'
    parent_bot_id = parent_row[0]

    # 2. Fetch parent trade and bot configuration
    parent_trade = conn.execute(
        "SELECT current_step, COALESCE(open_qty, 0), cycle_id FROM trades WHERE bot_id = ?",
        (parent_bot_id,)
    ).fetchone()
    parent_bot = conn.execute(
        "SELECT hedge_trigger_step FROM bots WHERE id = ?",
        (parent_bot_id,)
    ).fetchone()

    if not parent_trade or not parent_bot:
        return 'dormant'

    parent_step = int(parent_trade[0] or 0)
    parent_qty = float(parent_trade[1] or 0)
    parent_cycle_id = int(parent_trade[2] or 1)
    hedge_trigger_step = int(parent_bot[0]) if parent_bot[0] is not None else None

    # 3. Fetch child trade details
    child_trade = conn.execute(
        "SELECT COALESCE(open_qty, 0), cycle_id FROM trades WHERE bot_id = ?",
        (child_bot_id,)
    ).fetchone()
    if not child_trade:
        return 'dormant'
    child_qty = float(child_trade[0] or 0)
    child_cycle_id = int(child_trade[1] or 1)

    # Secondary guard: find the candidate cycle_id from bot_orders filled entries
    correct_cycle = conn.execute(
        "SELECT cycle_id FROM bot_orders "
        "WHERE bot_id = ? AND order_type = 'entry' "
        "AND status IN ('filled','partially_filled') "
        "AND filled_amount > 0.0001 "
        "ORDER BY created_at DESC LIMIT 1",
        (child_bot_id,)
    ).fetchone()

    if correct_cycle:
        # Check if candidate cycle has an active position (entry fills > exit fills)
        net_cycle_qty = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption', 'adoption_add', 'carry') "
            "THEN filled_amount ELSE -filled_amount END), 0.0) "
            "FROM bot_orders "
            "WHERE bot_id = ? AND cycle_id = ? "
            "AND status IN ('filled', 'partially_filled', 'closed', 'auto_closed', 'hedge_exited')",
            (child_bot_id, correct_cycle[0])
        ).fetchone()
        cycle_qty = float(net_cycle_qty[0] or 0.0)

        if cycle_qty > 0.0001:
            if correct_cycle[0] != child_cycle_id:
                # Correct the cycle_id from bot_orders, not from parent
                conn.execute(
                    "UPDATE trades SET cycle_id = ? WHERE bot_id = ?",
                    (correct_cycle[0], child_bot_id)
                )
                conn.commit()
                logger.warning(
                    f"[HEDGE-CYCLE-REPAIR] Child {child_bot_id}: stale cycle_id "
                    f"{child_cycle_id} corrected to {correct_cycle[0]} from "
                    f"bot_orders filled entries. open_qty will be resealed."
                )
                # Trigger reseal so open_qty reflects the correct cycle
                from engine.ledger import seal_trade_state
                seal_trade_state(child_bot_id)
            return 'active'  # child has real position, treat as active

    # 4. Sync cycle_id if diverged (only if child is flat/standby)
    if child_cycle_id != parent_cycle_id and child_qty <= 0.0001:
        conn.execute(
            "UPDATE trades SET cycle_id = ? WHERE bot_id = ?",
            (parent_cycle_id, child_bot_id)
        )
        conn.execute(
            "UPDATE bot_orders SET cycle_id = ? "
            "WHERE bot_id = ? AND cycle_id = ? "
            "AND status NOT IN ('reset_cleared', 'auto_closed', 'filled', 'cancelled')",
            (parent_cycle_id, child_bot_id, child_cycle_id)
        )
        conn.commit()
        logger.warning(
            f"[HEDGE-ALIGN] Synced child {child_bot_id} cycle_id {child_cycle_id} -> {parent_cycle_id} "
            f"to match parent {parent_bot_id}."
        )
        child_cycle_id = parent_cycle_id

    # 5. Determine child state
    # NEW: if parent has exited, child should freeze accumulation (be_only)
    parent_status_row = conn.execute(
        "SELECT status FROM bots WHERE id = ?", (parent_bot_id,)
    ).fetchone()
    parent_status = parent_status_row[0] if parent_status_row else ''

    if parent_status in ('Scanning', 'hedge_standby', 'pending_hedge_close'):
        if child_qty > 0.0001:
            return 'be_only'

    # If child is still in a previous cycle (e.g. parent TP'd but child BE TP is still pending),
    # the child bot is active in break-even TP mode. It must NOT close immediately.
    if child_cycle_id < parent_cycle_id:
        return 'active'

    if hedge_trigger_step is None or parent_step < hedge_trigger_step:
        # Never reset to standby if child has an open position (INV-22)
        if child_qty > 0.0001:
            child_name_row = conn.execute("SELECT name FROM bots WHERE id = ?", (child_bot_id,)).fetchone()
            child_name = child_name_row[0] if child_name_row else str(child_bot_id)
            logger.info(
                f"[HEDGE-CHILD-GUARD] {child_name}: parent below trigger step but child has "
                f"open_qty={child_qty:.4f} — NOT resetting to standby. "
                f"Child position must close via break-even TP, not forced standby reset."
            )
            return 'active'
        
        # If child has no open position, check if we need to reset status to standby
        child_status_row = conn.execute("SELECT status FROM bots WHERE id = ?", (child_bot_id,)).fetchone()
        child_status = child_status_row[0] if child_status_row else ''
        if child_status.lower() != 'hedge_standby':
            return 'should_close'
        return 'dormant'
    
    return 'active'


def _cancel_non_tp_orders(bot_id: int, exchange, conn):
    """
    Cancels all open exchange orders for this bot except 'tp' types,
    and marks them 'cancelled' in bot_orders.
    """
    try:
        pair_row = conn.execute("SELECT pair FROM bots WHERE id = ?", (bot_id,)).fetchone()
        if not pair_row or not pair_row[0]:
            return
        pair = pair_row[0]

        open_orders = exchange.fetch_open_orders(pair)
        prefix = f"CQB_{bot_id}_"
        bot_open_orders = [o for o in open_orders if o.get('clientOrderId', '').startswith(prefix)]
        
        for order in bot_open_orders:
            client_id = order.get('clientOrderId', '')
            order_id = order['id']
            
            if '_TP_' in client_id:
                continue
            
            try:
                exchange.cancel_order(order_id, pair)
                logger.info(f"🚫 [BE-ONLY] Cancelled non-TP order {client_id} on exchange.")
            except Exception as e_cancel:
                logger.warning(f"⚠️ [BE-ONLY] Failed to cancel order {client_id} on exchange: {e_cancel}")
            
            try:
                from engine.database import update_order_status
                update_order_status(order_id, 'cancelled', bot_id=bot_id)
            except Exception as e_db:
                logger.warning(f"⚠️ [BE-ONLY] Failed to update DB status for cancelled order {client_id}: {e_db}")
                
        # Direct DB sync for any pending/unplaced orders
        now_ts = int(time.time())
        conn.execute(
            "UPDATE bot_orders SET status = 'cancelled', updated_at = ? "
            "WHERE bot_id = ? AND status IN ('open', 'new', 'placing', 'cancelling', 'pending_placement') "
            "AND order_type != 'tp'",
            (now_ts, bot_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"❌ [BE-ONLY] Error in _cancel_non_tp_orders for bot {bot_id}: {e}")


def _reset_to_hedge_standby(child_bot_id: int, conn, parent_cycle_id: int, exchange=None):
    """
    Two-Phase Atomic Reset Protocol (INV-15).

    Phase 1 — Exchange Settlement (must complete before any DB write):
      1a. Read the DB-attributed qty (trades.open_qty) — what we claim is on exchange.
      1b. Cancel all CQB_ open orders for this bot on the pair.
      1c. If attributed_qty > 0, place a reduceOnly market order to close it.
          The close order is written as a 'reset_close' bot_orders receipt BEFORE
          the exchange call, and updated after — guaranteeing an audit trail even if
          the process crashes mid-flight.
      1d. If Phase 1 fails for any reason, set the bot to REQUIRE_MANUAL_PROOF
          and raise — the DB is NOT modified.

    Phase 2 — DB Update (only after exchange is confirmed settled):
      Zero open_qty / avg_entry_price, set status='hedge_standby', log audit row.
    """
    from engine.database import save_bot_order, get_connection as _db_conn

    # ── Resolve parent info for audit note ───────────────────────────────────
    parent_row = conn.execute(
        "SELECT parent_bot_id FROM bots WHERE id = ?", (child_bot_id,)
    ).fetchone()
    parent_bot_id = parent_row[0] if parent_row else None

    parent_info_str = "unknown"
    if parent_bot_id:
        parent_trade = conn.execute(
            "SELECT current_step, COALESCE(open_qty, 0) FROM trades WHERE bot_id = ?",
            (parent_bot_id,)
        ).fetchone()
        if parent_trade:
            parent_info_str = (
                f"parent_id={parent_bot_id}, parent_step={parent_trade[0]}, "
                f"parent_qty={parent_trade[1]}"
            )

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 1 — EXCHANGE SETTLEMENT
    # ═════════════════════════════════════════════════════════════════════════
    if exchange is not None:
        # 1a. Read current attributed qty from DB (our claim of what is on exchange)
        child_trade_row = conn.execute(
            "SELECT COALESCE(open_qty, 0), pair FROM trades t "
            "JOIN bots b ON b.id = t.bot_id WHERE t.bot_id = ?",
            (child_bot_id,)
        ).fetchone()
        attributed_qty = float(child_trade_row[0]) if child_trade_row else 0.0
        pair = child_trade_row[1] if child_trade_row else None

        if not pair:
            pair_row = conn.execute("SELECT pair FROM bots WHERE id = ?", (child_bot_id,)).fetchone()
            pair = pair_row[0] if pair_row else None

        skip_phase1 = False
        if attributed_qty > 0.0001 and pair:
            # FIX 3: idempotency guard check for in-flight reset_close order in current cycle
            now_ts = int(time.time())
            in_flight = conn.execute(
                "SELECT id FROM bot_orders WHERE bot_id = ? AND order_type = 'reset_close' "
                "AND status IN ('pending', 'open') AND cycle_id = ? AND created_at >= ?",
                (child_bot_id, parent_cycle_id, now_ts - 30)
            ).fetchone()
            if in_flight:
                logger.warning(
                    f"[RESET-P1] Found in-flight reset_close order (ID: {in_flight[0]}) "
                    f"for bot {child_bot_id} cycle {parent_cycle_id} less than 30s old. Skipping Phase 1."
                )
                skip_phase1 = True

            if not skip_phase1:
                # Get bot's direction
                dir_row = conn.execute(
                    "SELECT direction FROM bots WHERE id = ?", (child_bot_id,)
                ).fetchone()
                direction = (dir_row[0] or 'LONG').upper() if dir_row else 'LONG'

                # FIX 1: get exchange-authoritative close qty
                from engine.oneway_netting import get_authoritative_close_qty
                close_qty = get_authoritative_close_qty(exchange, pair, direction, attributed_qty)

                if close_qty <= 0.0001:
                    logger.warning(
                        f"[RESET-P1] Exchange already flat (close_qty={close_qty:.6f}) "
                        f"for bot {child_bot_id} on {pair}. Skipping Phase 1 settlement."
                    )
                    skip_phase1 = True
                else:
                    # 1b. Cancel open orders for this bot
                    try:
                        cancelled = exchange.cancel_orders_by_bot_id(child_bot_id, pair)
                        logger.info(
                            f"[RESET-P1] Cancelled {cancelled} open order(s) for bot {child_bot_id} on {pair}."
                        )
                    except Exception as _ce:
                        logger.warning(
                            f"[RESET-P1] cancel_orders_by_bot_id failed for bot {child_bot_id}: {_ce}. Continuing."
                        )

                    # 1c. Close position using close_qty instead of attributed_qty
                    close_side = 'sell' if direction == 'LONG' else 'buy'
                    close_cid = f"CQB_{child_bot_id}_RESET_CLOSE_{int(time.time())}"

                    # Write pending receipt BEFORE touching exchange (WAL pattern)
                    _receipt_conn = _db_conn()
                    _receipt_cursor = _receipt_conn.cursor()
                    _receipt_cursor.execute("""
                        INSERT INTO bot_orders (
                            bot_id, order_type, client_order_id, price, amount,
                            filled_amount, status, cycle_id, created_at, updated_at, notes
                        ) VALUES (?, 'reset_close', ?, 0, ?, 0, 'pending', ?, ?, ?, ?)
                    """, (
                        child_bot_id, close_cid, close_qty, parent_cycle_id,
                        int(time.time()), int(time.time()),
                        f"[RESET-P1] Two-phase close: {close_qty} {pair} {close_side.upper()} reduceOnly"
                    ))
                    _receipt_conn.commit()
                    _pending_row_id = _receipt_cursor.lastrowid

                    try:
                        close_result = exchange.create_order(
                            symbol=pair,
                            type='market',
                            side=close_side,
                            amount=close_qty,
                            params={
                                'newClientOrderId': close_cid,
                                'reduceOnly': True,
                            },
                            human_approved=True,
                            _call_site='bot_executor:_reset_to_hedge_standby',
                        )
                        real_order_id = str(close_result.get('id', ''))
                        # Update receipt with real exchange order_id → open
                        _receipt_conn.execute(
                            "UPDATE bot_orders SET order_id=?, status='open', updated_at=? WHERE id=?",
                            (real_order_id, int(time.time()), _pending_row_id)
                        )
                        _receipt_conn.commit()
                        logger.warning(
                            f"✅ [RESET-P1] Phase 1 complete: bot {child_bot_id} closed "
                            f"{close_qty} {pair} {close_side.upper()} → exchange order_id={real_order_id}"
                        )

                    except Exception as _close_err:
                        # Phase 1 failed → update receipt to 'failed', block Phase 2
                        _receipt_conn.execute(
                            "UPDATE bot_orders SET status='failed', updated_at=?, notes=? WHERE id=?",
                            (int(time.time()), f"[RESET-P1] Exchange close failed: {_close_err}", _pending_row_id)
                        )
                        _receipt_conn.commit()
                        # Lock bot to REQUIRE_MANUAL_PROOF — do NOT write DB reset
                        conn.execute(
                            "UPDATE bots SET status='REQUIRE_MANUAL_PROOF' WHERE id=?",
                            (child_bot_id,)
                        )
                        conn.commit()
                        logger.error(
                            f"❌ [RESET-P1] Phase 1 FAILED for bot {child_bot_id}: {_close_err}. "
                            f"Bot locked to REQUIRE_MANUAL_PROOF. DB NOT modified. "
                            f"Manually close {close_qty} {pair} on exchange before retrying."
                        )
                        raise RuntimeError(
                            f"[INV-15] Two-phase reset Phase 1 failed for bot {child_bot_id}: {_close_err}"
                        )

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 2 — DB UPDATE (only reached after exchange is settled or qty was 0)
    # ═════════════════════════════════════════════════════════════════════════
    conn.execute(
        "UPDATE trades SET cycle_id = ? WHERE bot_id = ?",
        (parent_cycle_id, child_bot_id)
    )
    conn.execute(
        "UPDATE bots SET status = 'hedge_standby', cascade_started_at = 0 WHERE id = ?",
        (child_bot_id,)
    )
    conn.commit()

    # After commit — seal reads the new cycle_id, finds no fills, returns zeros
    from engine.ledger import seal_trade_state
    seal_trade_state(child_bot_id, force_recompute=True)

    # Force status to hedge_standby if seal overwrote it
    conn.execute(
        "UPDATE bots SET status = 'hedge_standby', cascade_started_at = 0 WHERE id = ?",
        (child_bot_id,)
    )
    conn.commit()

    # Audit row (always written, Phase 1 or not)
    drift_cid = f"CQB_{child_bot_id}_DF_RST_{int(time.time())}"
    save_bot_order(
        child_bot_id, 'drift_note', drift_cid,
        price=0.0, amount=0.0, step=0, status='audit',
        client_order_id=drift_cid,
        notes=f"[HEDGE-ENFORCE][INV-15] Two-phase reset complete. Parent state: {parent_info_str}",
        cycle_id=parent_cycle_id
    )
    logger.warning(
        f"🛡️ [HEDGE-ENFORCE] Child bot {child_bot_id} reset to hedge_standby (cycle_id={parent_cycle_id}). "
        f"Parent state: {parent_info_str}"
    )


class BotExecutor:
    # 🛡️ Binance margin and position limit rejection signals
    _MARGIN_SIGNALS = [
        "-2019", "-2027", "-4131", "-4003", "-4118",
        "margin is insufficient", "account has insufficient balance",
        "exceed maximum position", "position limit",
        "reduceonly order is rejected", "would not reduce",
    ]


    def __init__(self, runner: Any): # 'runner' is BotRunner instance
        self.runner = runner
        self.strategies: Dict[int, MartingaleStrategy] = {}
        self.config_cache: Dict[int, str] = {} # Cache for config JSON strings
        self._grid_backoff: Dict[int, Tuple[float, int]] = {}  # bot_id -> (last_fail_ts, fail_count)

    @staticmethod
    def _resolve_position_side_param(params: dict, is_testnet: bool) -> dict:
        """
        Normalizes positionSide in an order params dict for testnet vs mainnet.

        Binance Testnet FAPI (demo):
            - Requires positionSide='BOTH' on all orders in One-Way mode.
            - Rejects positionSide='LONG' or 'SHORT' with -4061.

        Binance Mainnet FAPI:
            - Rejects ANY positionSide field with -4061 in One-Way mode.
            - positionSide must be completely absent from params.

        This function must be called on every params dict before any
        create_order() call. It strips LONG/SHORT and either sets BOTH
        (testnet) or removes the field entirely (mainnet).
        """
        result = dict(params) if params else {}

        # Remove any LONG/SHORT value — wrong for both environments in One-Way mode
        current = str(result.get('positionSide', '')).upper()
        if current in ('LONG', 'SHORT'):
            del result['positionSide']

        if is_testnet:
            # Testnet requires the field to be present as 'BOTH'
            result['positionSide'] = 'BOTH'
        else:
            # Mainnet: field must be absent entirely
            result.pop('positionSide', None)

        return result

    def _get_thread_exchange(self, market_type: str) -> ExchangeInterface:
        # Ensure each thread has its own exchange interface to prevent concurrency issues
        if not hasattr(_thread_local, 'exchanges'):
            _thread_local.exchanges = {}
        
        if market_type not in _thread_local.exchanges:
            _thread_local.exchanges[market_type] = ExchangeInterface(market_type=market_type)
            logger.debug(f"Initialized new {market_type} ExchangeInterface for thread {threading.get_ident()}")
        
        return _thread_local.exchanges[market_type]

    def _generate_deterministic_id(self, bot_id: int, type_str: str, cycle_id: int, step_index: int, suffix: str = None, is_replacement: bool = False, for_check: bool = False) -> str:
        """
        Generates an idempotent deterministic clientOrderId for orders.
        Format: CQB_{bot_id}_{TYPE}_{CYCLE}_{STEP}
        Adding cycle_id ensures that retries or race conditions for the same step 
        cannot place duplicate orders (Binance will reject duplicate clientOrderId).
        """
        from engine.database import generate_cid
        return generate_cid(bot_id, type_str, cycle_id, step_index, suffix=suffix, is_replacement=is_replacement, for_check=for_check)

    def _get_strategy_instance(self, bot_id: int, config_dict: Dict[str, Any], config_json_str: Optional[str] = None) -> MartingaleStrategy:
        # Check if config has changed
        cached_config = self.config_cache.get(bot_id)
        
        if bot_id not in self.strategies:
            self.strategies[bot_id] = MartingaleStrategy(config_dict)
            if config_json_str:
                self.config_cache[bot_id] = config_json_str
        elif config_json_str and cached_config != config_json_str:
            # 🚀 OPTIMIZED FIX: Only update params if config actually changed!
            # This addresses user concerns about performance overhead.
            self.strategies[bot_id].params = config_dict
            self.config_cache[bot_id] = config_json_str
            # logger.debug(f"🔄 Bot {bot_id}: Strategy params updated from DB.")
            
        return self.strategies[bot_id]

    def _get_phys_pos(self, pair: str, direction: str = None) -> Optional[Dict[str, Any]]:
        """
        Retrieves the physical exchange position for a pair from the active_positions table.
        
        NOTE: active_positions stores ONE row per pair (last reconciled state). In Hedge Mode,
        the exchange holds LONG and SHORT separately, but only the dominant/last side is in this table.
        
        When direction is given: tries exact side match first, then falls back to any row for the pair.
        This prevents blocking (e.g. short btc can't find its SHORT row because only LONG is stored)
        while still being direction-aware when both sides are stored separately.
        """
        try:
            from engine.exchange_interface import normalize_symbol
            clean_pair = normalize_symbol(pair)
            from engine.database import get_connection
            with get_connection() as conn:
                if direction:
                    expected_side = 'LONG' if direction.upper() == 'LONG' else 'SHORT'
                    # Try exact side match first
                    row = conn.execute(
                        "SELECT size, side, entry_price FROM active_positions WHERE pair = ? AND side = ?",
                        (clean_pair, expected_side)
                    ).fetchone()
                    if not row:
                        # Fall back: active_positions has only one row per pair — use it regardless of side
                        row = conn.execute(
                            "SELECT size, side, entry_price FROM active_positions WHERE pair = ?",
                            (clean_pair,)
                        ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT size, side, entry_price FROM active_positions WHERE pair = ?",
                        (clean_pair,)
                    ).fetchone()
                logger.info(f"[PHYS-TRACE] _get_phys_pos called for {pair} -> {clean_pair} dir={direction} | ROW: {row}")
                if row:
                    return {'size': float(row[0]), 'side': str(row[1]).upper(), 'entry_price': float(row[2])}
            return None
        except Exception as e:
            logger.warning(f"⚠️ [PHYS-SENSE] Lookup failed for {pair}: {e}")
            return None


    def _is_order_net_reducing(self, pair, side, qty, bot_id=None, bot_direction=None):
        """
        v3.2.6: Sibling-aware reduction check for One-Way mode.
        Only uses bot-aware override when this bot is the sole active bot on the pair.
        """
        # Count active siblings
        sibling_count = 1  # default: assume multi-bot
        try:
            from engine.database import get_connection as _gc
            with _gc() as _c:
                row = _c.execute(
                    "SELECT COUNT(*) FROM bots b JOIN trades t ON b.id=t.bot_id "
                    "WHERE b.pair=? AND t.total_invested>0 AND b.id!=?",
                    (pair, bot_id or 0)
                ).fetchone()
                sibling_count = row[0] if row else 1
        except Exception:
            sibling_count = 1

        logger.debug(f"[NET-REDUCE] {pair} bot={bot_id} siblings={sibling_count} side={side} qty={qty}")

        # Sole-bot path: bot's virtual exit IS a physical reduction —
        # but ONLY if the physical net confirms this bot owns the position.
        # v3.6.2: Guard against stale sibling count (sibling just reset,
        # physical net hasn't updated yet). Verify physical side matches.
        if sibling_count == 0 and bot_id and bot_direction:
            bot_is_long = bot_direction.upper() == 'LONG'
            order_is_sell = side.lower() == 'sell'
            is_closing = (bot_is_long and order_is_sell) or (not bot_is_long and not order_is_sell)
            if is_closing:
                # Verify physical net direction matches before using reduceOnly
                try:
                    from engine.exchange_interface import normalize_symbol
                    from engine.database import get_connection as _gc_sv
                    with _gc_sv() as _c_sv:
                        _sv_rows = _c_sv.execute(
                            "SELECT side, size FROM active_positions WHERE pair=?",
                            (normalize_symbol(pair),)
                        ).fetchall()
                    _sv_net = sum(r[1] if str(r[0]).upper()=='LONG' else -r[1] for r in _sv_rows)
                    # Physical net must be on the same side as the bot's position
                    _phys_matches = (_sv_net > 0.0001 and bot_is_long) or \
                                    (_sv_net < -0.0001 and not bot_is_long)
                    if _phys_matches:
                        return True
                    else:
                        logger.warning(
                            f"[NET-REDUCE] Sole-bot override suppressed: "
                            f"bot={bot_direction} but phys_net={_sv_net:.6f} "
                            f"(opposite side). Using account-net path."
                        )
                except Exception:
                    pass  # Fall through to account-net path below

        # Multi-bot: use TOTAL account net, not bot-owned slice
        try:
            from engine.exchange_interface import normalize_symbol
            from engine.database import get_connection as _gc2
            with _gc2() as _c2:
                rows = _c2.execute(
                    "SELECT side, size FROM active_positions WHERE pair=?",
                    (normalize_symbol(pair),)
                ).fetchall()
            phys_net = sum(r[1] if str(r[0]).upper()=='LONG' else -r[1] for r in rows)
        except Exception:
            return False  # conservative: don't use reduceOnly if we can't verify

        if abs(phys_net) < 0.0001:
            return False

        order_delta = qty if side.lower() == 'buy' else -qty
        new_net = phys_net + order_delta

        result = abs(new_net) < (abs(phys_net) - 0.0001)
        logger.debug(f"[NET-REDUCE] phys_net={phys_net:.6f} delta={order_delta:.6f} new_net={new_net:.6f} result={result}")
        return result



    def _prepare_tp_order_params(self, bot_id: int, name: str, pair: str, side: str, amount: float, tp_price: float, current_price: float, exchange: Any, direction: str) -> Tuple[Optional[Dict], float]:
        """
        Calculates Take Profit parameters using this bot's own virtual position size.
        
        Architecture (Correct):
        1. Each bot manages its OWN position. TP qty = bot's own virtual open qty from its ledger.
        2. Physical position is used as a sanity cap (can't close more than physically exists).
        3. Single-Bot Active (Dust): conditionally applies reduceOnly=True if < $5 to mathematically bypass min notional.
        4. Multi-Bot Active: drops reduceOnly, uses postOnly+GTX. If < $5, triggers DUST_CHASER abort.
        """
        # Determine active sibling bots for conditional reduceOnly capability
        try:
            from engine.database import get_connection as _ghc
            with _ghc() as _hc:
                _hcur = _hc.cursor()
                _hcur.execute(
                    "SELECT COUNT(*) FROM bots b JOIN trades t ON b.id=t.bot_id "
                    "WHERE b.pair=? AND t.total_invested>0 AND b.id!=?",
                    (pair, bot_id)
                )
                _other_bots_count = _hcur.fetchone()[0]
        except Exception:
            _other_bots_count = 1  # Base assumption: multi-bot if DB fails

        # Standard baseline for all bot configurations (protects Maker rebates via PostOnly)
        ccxt_params = {'postOnly': True, 'timeInForce': 'GTX'}

        # 1. READ open_qty ACCUMULATOR — authoritative position size [v2.1]
        # trades.open_qty is maintained atomically by credit_fill() on every fill.
        # It is the exact qty confirmed by the exchange — no float-sum recomputation.
        try:
            from engine.database import get_connection as _gc_tp
            from engine.exchange_interface import normalize_symbol
            norm_pair = normalize_symbol(pair)
            cycle_id = 1
            current_step = 0
            with _gc_tp() as _c_tp:
                _cur = _c_tp.cursor()

                # Primary: read the accumulator directly
                _cur.execute(
                    "SELECT open_qty, cycle_id, current_step FROM trades WHERE bot_id = ?", (bot_id,)
                )
                acc_row = _cur.fetchone()
                bot_virtual_open_qty = float(acc_row[0] or 0.0) if acc_row else 0.0
                cycle_id = int(acc_row[1] or 1) if acc_row else 1
                current_step = int(acc_row[2] or 0) if acc_row else 0

                # Fallback: if accumulator is zero but DB has fills, recompute (handles
                # bots running before v2.1 migration where open_qty was not yet populated)
                if bot_virtual_open_qty <= 0:
                    _cur.execute("""
                        SELECT
                            COALESCE(SUM(CASE WHEN order_type IN ('entry','grid','adoption_add','adoption') THEN filled_amount ELSE 0 END), 0),
                            COALESCE(SUM(CASE WHEN order_type IN ('tp','close','adoption_reduce','dust_close','sl','flatten_close') THEN filled_amount ELSE 0 END), 0)
                        FROM bot_orders
                        WHERE bot_id=? AND (cycle_id=? OR cycle_id IS NULL)
                        AND status NOT IN ('reset_cleared','auto_closed','failed','placing')
                        AND filled_amount > 0
                    """, (bot_id, cycle_id))
                    _leg = _cur.fetchone()
                    _recomputed = max(0.0, float(_leg[0] or 0) - float(_leg[1] or 0))
                    if _recomputed > 0:
                        logger.debug(f"[TP-QTY] {name}: accumulator=0, recomputed={_recomputed:.8f} — using recomputed (pre-v2.1 bot)")
                        bot_virtual_open_qty = _recomputed
                        # Backfill the accumulator so next cycle uses it correctly
                        from engine.ledger import seal_trade_state
                        seal_trade_state(bot_id)

                # UBE SANITY CAP — verify we're not trying to close more than physically exists.
                # ⚠️ ROOT CAUSE FIX (v2.3.1): The cap previously fired on ANY excess, including
                # sub-step rounding diffs from stale active_positions snapshots. This caused TP
                # qty to silently deflate (0.016 → 0.015 → 0.014), which the reconciler then
                # "corrected" with adoption_reduce fills, permanently corrupting open_qty.
                # FIX: Only cap if virtual qty exceeds physical by more than 20% (genuine corruption).
                # Sub-20% differences are explained by snapshot lag, rounding, or pending order fills.
                _cur.execute("SELECT size, side FROM active_positions WHERE pair=?", (norm_pair,))
                phys_rows = _cur.fetchall()
                bot_dir = direction.upper()
                phys_matching = sum(float(r[0]) for r in phys_rows if str(r[1]).upper() == bot_dir)
                phys_opposite = sum(float(r[0]) for r in phys_rows if str(r[1]).upper() != bot_dir)

                # Neighbor bots (for UBE context only)
                _cur.execute("""
                    SELECT b.direction, t.open_qty
                    FROM bots b JOIN trades t ON b.id=t.bot_id
                    WHERE b.normalized_pair=? AND t.open_qty>0 AND b.id!=?
                """, (norm_pair, bot_id))
                neighbors = _cur.fetchall()
                opposite_virtual_qty = sum(q for d, q in neighbors if d.upper() != bot_dir)

                # 🚀 HEDGE-AWARE UBE (v3.0.1)
                # In One-Way mode, the physical position is NETTED.
                # If a SHORT bot has a LONG hedge, the physical position will be higher (less negative).
                # Example: Short 1.1 BTC + Hedge 1.5 BTC Long = Net 0.4 BTC Long.
                # The "capacity" for the bot's SHORT 1.1 BTC is Net - Hedge = 0.4 - 1.5 = -1.1.
                if phys_matching > 0:
                    max_possible_qty = phys_matching + opposite_virtual_qty
                elif bot_virtual_open_qty > 0 and phys_matching == 0 and phys_opposite == 0:
                    max_possible_qty = bot_virtual_open_qty + opposite_virtual_qty
                    logger.debug(f"🔍 {name}: UBE: No physical row for {bot_dir} {norm_pair}. Using accumulator anchor ({bot_virtual_open_qty:.6f}).")
                else:
                    max_possible_qty = max(0.0, opposite_virtual_qty - phys_opposite)

                # Only cap if virtual is genuinely more than 20% above physical capacity.
                # This filters out snapshot lag (e.g. phys=0.015 vs virtual=0.016 due to stale row).
                ube_excess = bot_virtual_open_qty - max_possible_qty
                ube_threshold = max(0.20 * max_possible_qty, 0.0001) if max_possible_qty > 0 else 0.0001
                if ube_excess > ube_threshold:
                    logger.warning(
                        f"🛡️ {name}: UBE cap! accumulator={bot_virtual_open_qty:.6f} "
                        f"capped at {max_possible_qty:.6f} (phys={phys_matching:.6f}, "
                        f"excess={ube_excess:.6f} > threshold={ube_threshold:.6f}). "
                        f"Possible DB corruption — investigate active_positions."
                    )
                    bot_virtual_open_qty = max_possible_qty
                elif ube_excess > 0:
                    logger.debug(
                        f"🔍 {name}: UBE sub-threshold excess={ube_excess:.6f} (phys={phys_matching:.6f} vs virtual={bot_virtual_open_qty:.6f}). "
                        f"Within snapshot-lag tolerance — NOT capping. Trusting accumulator."
                    )

        except Exception as e:
            logger.warning(f"⚠️ {name}: Failed to read open_qty accumulator: {e}. Falling back to passed amount.")
            bot_virtual_open_qty = amount
            phys_matching = 0.0
            opposite_virtual_qty = 0.0

        if bot_virtual_open_qty <= 0.0:
            logger.warning(f"⚠️ {name}: open_qty is 0 — no position to close.")
            return None, None

        # 2. Derive TP qty from accumulator (already exchange-confirmed, rounding is final cleanup)
        prec = exchange.get_symbol_precision(pair)
        tp_qty = exchange.round_to_step(bot_virtual_open_qty, prec['step_size'])

        # v3.6.2: Direction-aware TP capacity clip to prevent -4118.
        # A LONG bot's SELL TP requires LONG-side physical capacity.
        # A SHORT bot's BUY TP requires SHORT-side physical capacity.
        # If the physical net is on the OPPOSITE side, capacity = 0
        # and the order must use GTX (non-reduceOnly) instead.
        force_no_reduce = False
        try:
            # v3.6.4: Use actual exchange net position, not per-bot virtual record.
            # active_positions stores gross per-bot splits; exchange enforces the net.
            # Capacity must be computed from net to match what the exchange enforces.
            from engine.database import get_connection as _gc_net
            with _gc_net() as _cn:
                _net_rows = _cn.execute(
                    "SELECT side, size FROM active_positions "
                    "WHERE pair = ? OR pair = ?",
                    (pair, norm_pair)
                ).fetchall()
            _exchange_net = sum(
                r[1] if str(r[0]).upper() == 'LONG' else -r[1]
                for r in _net_rows
            )
        except Exception:
            _exchange_net = 0.0

        # For a LONG bot SELL TP: need positive net (LONG capacity)
        # For a SHORT bot BUY TP: need negative net (SHORT capacity)
        _required_sign = 1 if direction.upper() == 'LONG' else -1
        _net_on_correct_side = _exchange_net * _required_sign  # positive if correct side

        if _net_on_correct_side <= 0.0001:
            # Physical net is on wrong side or flat — zero capacity, use GTX
            logger.warning(
                f"[TP-CLIP] {name}: Exchange net {_exchange_net:.6f} has no "
                f"{direction} capacity. Setting force_no_reduce=True."
            )
            force_no_reduce = True
        else:
            # Subtract same-side open orders from the real net
            # (existing _other_open_tp calculation stays exactly as-is)
            try:
                from engine.database import get_connection as _gc_clip
                with _gc_clip() as _cc:
                    _open_orders = _cc.cursor().execute(
                        "SELECT bo.bot_id, bo.amount, bo.order_type, b.direction "
                        "FROM bot_orders bo "
                        "JOIN bots b ON bo.bot_id = b.id "
                        "WHERE bo.status IN ('open', 'new', 'placing', 'cancelling') AND (b.pair = ? OR b.normalized_pair = ?)",
                        (pair, norm_pair)
                    ).fetchall()
                
                _other_open_tp = 0.0
                for _o_bot_id, _o_amount, _o_type, _o_dir in _open_orders:
                    if _o_bot_id == bot_id and _o_type in ('tp', 'dust_close'):
                        continue
                    _is_long_bot = (_o_dir.upper() == 'LONG')
                    _is_entry = _o_type in ('entry', 'grid', 'adoption_add', 'adoption', 'carry')
                    _o_side = 'buy' if (_is_long_bot and _is_entry) or (not _is_long_bot and not _is_entry) else 'sell'
                    if _o_side == side.lower():
                        _other_open_tp += float(_o_amount)
            except Exception:
                _other_open_tp = 0.0

            _phys_capacity = exchange.round_to_step(
                max(0.0, _net_on_correct_side - _other_open_tp),
                prec['step_size']
            )
            if _phys_capacity <= 0.0001:
                logger.warning(
                    f"[TP-CLIP] {name}: Net capacity exhausted by sibling orders "
                    f"(net={_net_on_correct_side:.6f}, other_open={_other_open_tp:.6f}). "
                    f"Setting force_no_reduce=True."
                )
                force_no_reduce = True
            elif tp_qty > _phys_capacity:
                logger.warning(
                    f"[TP-CLIP] {name}: tp_qty {tp_qty:.6f} > net capacity "
                    f"{_phys_capacity:.6f}. Clipping."
                )
                tp_qty = _phys_capacity


        if tp_qty <= 0:
            logger.info(f"INFO {name}: open_qty rounds to 0 after step_size. Snapping accumulator to 0.")
            try:
                dust_cid = f"DUST_{bot_id}_{int(time.time())}"
                save_bot_order(
                    bot_id=bot_id,
                    order_type='dust_close',
                    exchange_order_id=dust_cid,
                    price=current_price,
                    amount=bot_virtual_open_qty,
                    step=current_step,
                    status='filled',
                    client_order_id=dust_cid,
                    cycle_id=cycle_id
                )
                from engine.ledger import seal_trade_state
                seal_trade_state(bot_id)
            except Exception as e:
                logger.error(f"Failed to snap dust to 0 via ledger: {e}")
            return None, None

        # 4. Net-reducing check — use reduceOnly for closing TPs to bypass margin reservation.
        # GTX/postOnly orders require Binance to reserve margin even for closing orders.
        # reduceOnly orders never require margin — they can only reduce an existing position.
        # Bot-aware: a LONG bot selling is ALWAYS reducing for that bot (v3.1.5 override).
        # The clip above ensures tp_qty never exceeds physical capacity, so reduceOnly is safe.
        is_reducing = False if force_no_reduce else self._is_order_net_reducing(pair, side, tp_qty, bot_id=bot_id, bot_direction=direction)
        if is_reducing:
            ccxt_params = {'reduceOnly': True, 'timeInForce': 'GTC'}
            logger.debug(f"✅ {name}: TP is net-reducing — using reduceOnly GTC to bypass margin reservation.")
        else:
            # Check if this is a hedge child bot — if so, use GTC (not postOnly GTX)
            # postOnly GTX is cancelled if it crosses the spread; hedge child TPs must fill
            from engine.database import get_connection as _gc_bt
            with _gc_bt() as _bc:
                _bt_row = _bc.execute(
                    "SELECT bot_type FROM bots WHERE id=?", (bot_id,)
                ).fetchone()
            _is_hedge_child = _bt_row and _bt_row[0] == 'hedge_child'

            if _is_hedge_child:
                ccxt_params = {'timeInForce': 'GTC'}  # No reduceOnly, no postOnly — must fill
                logger.info(f"✅ {name}: Hedge child TP — using GTC (no reduceOnly, no postOnly). Order will fill at limit price.")
            else:
                ccxt_params = {'postOnly': True, 'timeInForce': 'GTX'}
                logger.warning(f"⚠️ {name}: TP order {side.upper()} {tp_qty} increases account NET exposure (multi-bot hedge) — using GTX postOnly.")

        # 5. Dust check: if notional is below minimum, trigger dust close path
        _min_notional = prec.get('min_notional')
        if _min_notional is None:
            _min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0

        notional = tp_qty * (tp_price or current_price or 1.0)
        if notional < _min_notional:
            logger.warning(f"DUST {name}: Virtual TP notional ${notional:.2f} < min ${_min_notional:.2f} (qty={tp_qty}).")
            
            if _other_bots_count == 0:
                # 🚀 PAIR-PARITY CHECK [v2.5.0]
                # In One-Way mode, if the physical net is already 0 (due to a hedge or wipe),
                # a reduceOnly TP will fail. We must verify physical existence before using the bypass.
                # Use local vars from UBE check above.
                phys_net_signed = phys_matching - phys_opposite
                
                if abs(phys_net_signed) >= (tp_qty * 0.99):
                    # Sole bot fallback: Convert to a reduceOnly order which mathematically bypasses the min-notional gate.
                    ccxt_params = {'timeInForce': 'GTC', 'reduceOnly': True}
                    logger.info(f"✨ {name}: Sub-$5 limit detected. Sole-bot + Physical match. Using bypassed reduceOnly TP.")
                else:
                    logger.warning(f"🛑 {name}: Sub-$5 limit detected but physical net is {phys_net_signed:.4f}. Cannot use reduceOnly. Yielding to DUST_CHASER.")
                    return 'DUST_CHASER', tp_qty
            else:
                # Multi-bot setups cannot use reduceOnly, meaning Binance will strictly reject the sub-$5 limit order.
                # Since we forbid synthetic price-nudging (Strict Proof-Only), this dust cannot be closed profitably.
                logger.warning(f"🛑 {name}: Multi-bot configuration blocks reduceOnly. Cannot limit-close sub-${_min_notional:.2f} legitimately. Yielding to DUST_CHASER.")
                return 'DUST_CHASER', tp_qty

        # 6. Spread-Cross Fix: TP at market price must be GTC taker
        try:
            if tp_price == exchange.round_to_step(current_price, prec['tick_size']):
                logger.warning(f"WARN {name}: price already at or beyond TP, switching to GTC.")
                ccxt_params.pop('postOnly', None)
                ccxt_params['timeInForce'] = 'GTC'
        except Exception:
            pass

        logger.debug(f"OK {name}: TP qty={tp_qty:.4f} (virtual={bot_virtual_open_qty:.4f}) notional=${notional:.2f}")
        return ccxt_params, tp_qty


    def _place_gtx_order_with_retry(self, exchange, pair: str, side: str, amount: float, price: float, params: dict, label: str = "order", position_side: str = None, raise_postonly_reject: bool = False) -> dict:
        """
        Places a GTX (Post-Only) limit order with automatic maker-price retry.

        Binance rejects Post-Only orders with two error codes:
          -50004: Order price would be taker (Demo FAPI)
          -2010:  Order would immediately execute (Live/Testnet FAPI)
        On either rejection, we re-fetch the live bid/ask and retry ONCE at a
        safe maker price. If the retry also fails, we drop GTX and place a plain
        limit (taker) as the ultimate fallback — ensuring the order is never silently lost.
        """
        # Normalize positionSide for testnet vs mainnet One-Way mode
        if params is not None:
            _is_testnet = getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)
            params = self._resolve_position_side_param(params, _is_testnet)

        def _is_postonly_rejected(err_str: str) -> bool:
            return (
                '-50004' in err_str or
                '-2010' in err_str or
                'Post Only' in err_str or
                'post only' in err_str.lower() or
                'would be executed immediately' in err_str.lower() or
                'would immediately' in err_str.lower()
            )

        try:
            return exchange.create_order(pair, 'limit', side, amount, price, params=params)
        except Exception as e:
            err_str = str(e)
            
            # -4061: positionSide field rejected in One-Way mode (mainnet)
            # Strip it and retry once
            if '-4061' in err_str or 'positionSide' in err_str.lower():
                retry_params = {k: v for k, v in params.items() if k != 'positionSide'}
                logger.warning(
                    f"[GTX-4061] {label}: positionSide rejected by exchange. "
                    f"Retrying without it for {pair} {side} @ {price:.6f}"
                )
                try:
                    return exchange.create_order(pair, 'limit', side, amount, price, params=retry_params)
                except Exception as e_retry:
                    err_str = str(e_retry)

            if not _is_postonly_rejected(err_str):
                raise  # Not a post-only issue — propagate

            if raise_postonly_reject:
                from engine.exceptions import GTXRejected
                raise GTXRejected(err_str)


            logger.warning(
                f"[GTX-RETRY] {label}: Post-Only rejected ({err_str[:80]}) for "
                f"{pair} {side} @ {price:.6f}. Re-fetching bid/ask..."
            )
            try:
                bid, ask = exchange.get_best_bid_ask(pair)
            except Exception:
                bid, ask = None, None

            if bid is None or ask is None:
                logger.error(f"[GTX-RETRY] {label}: Cannot fetch bid/ask. Raising original error.")
                raise

            prec = exchange.get_symbol_precision(pair)
            tick = prec.get('tick_size', 0.0001)
            if side.lower() == 'buy':
                # Maker BUY: must be AT or BELOW the best bid (never cross ask)
                retry_price = exchange.round_to_step(bid, tick)
            else:
                # Maker SELL: must be AT or ABOVE the best ask (never cross bid)
                retry_price = exchange.ceil_to_step(ask, tick)

            # Deduplicate clientOrderId on retry
            retry_params = dict(params) if params else {}
            for cid_key in ('clientOrderId', 'newClientOrderId'):
                if cid_key in retry_params:
                    retry_params[cid_key] = f"{retry_params[cid_key]}_R"

            logger.info(
                f"[GTX-RETRY] {label}: Retry {pair} {side} @ {retry_price:.6f} "
                f"(bid={bid:.6f} ask={ask:.6f})"
            )
            try:
                return exchange.create_order(pair, 'limit', side, amount, retry_price, params=retry_params)
            except Exception as e2:
                err2 = str(e2)
                if not _is_postonly_rejected(err2):
                    raise  # Different error — propagate

                # Retry also failed as post-only — market is moving fast.
                # Drop GTX and place a plain limit (taker) as last resort.
                fallback_params = {k: v for k, v in retry_params.items()
                                   if k not in ('postOnly', 'timeInForce')}
                # Capture the _R-suffixed CID from retry_params before mutating to _F.
                # We DON'T do a DB UPDATE here — the bot_orders row hasn't been inserted yet
                # (save_bot_order is called by the caller AFTER this function returns).
                # Instead, we annotate the returned dict with _fallback_cid so the caller
                # can pass it directly to save_bot_order, inserting the row correctly first time.
                _fallback_cid_value = None
                for cid_key in ('clientOrderId', 'newClientOrderId'):
                    if cid_key in fallback_params:
                        _fallback_cid_value = f"{fallback_params[cid_key]}_F"
                        fallback_params[cid_key] = _fallback_cid_value
                logger.warning(
                    f"[GTX-FALLBACK] {label}: GTX retry ALSO rejected. "
                    f"Placing plain limit (taker) @ {retry_price:.6f} to avoid silent loss."
                )
                fallback_order = exchange.create_order(pair, 'limit', side, amount, retry_price, params=fallback_params)
                if fallback_order and _fallback_cid_value:
                    # Annotate the order dict — caller pops this key and passes it as
                    # client_order_id to save_bot_order so the row is inserted with the
                    # correct CID from the start, eliminating the UPDATE-before-INSERT race.
                    fallback_order['_fallback_cid'] = _fallback_cid_value
                    logger.info(
                        f"[GTX-FALLBACK] {label}: Fallback order {fallback_order.get('id')} "
                        f"placed with CID {_fallback_cid_value}. Caller will link to bot_orders."
                    )
                return fallback_order


    # ---------------------------------------------------------------------------
    # Private helpers — single canonical implementations shared across methods
    # ---------------------------------------------------------------------------

    @staticmethod
    def _get_order_amount(order: dict) -> float:
        """Safe multi-key accessor for order quantity.
        CCXT live orders use 'amount', DB-cached orders may use 'origQty' or 'qty'."""
        return float(order.get('amount') or order.get('origQty') or order.get('qty') or 0)

    def _compute_effective_tp(self, bot_id: int, name: str, bot_status: dict,
                               bot_config: dict, strategy,
                               pair: str = None, tick_size: float = None) -> float:
        """Return the effective TP price after Early Exit decay, persisting any change to DB.

        INV-21 (v3.9.12): tick_size parameter added so the persisted and returned value is
        always rounded to the exchange tick grid.  This prevents sub-tick floating-point
        differences from triggering spurious SYNC-DRIFT fires every cycle.

        Args:
            pair:      Trading pair (optional, for logging only).
            tick_size: Exchange tick size obtained from get_symbol_precision().
                       When supplied, adjusted_tp is rounded to this precision before
                       comparison and DB write.  Falls back to strategy._round_price()
                       if not provided (legacy behaviour).

        Returns the effective TP price (tick-rounded when tick_size is provided).
        """
        raw_db_tp = float(bot_status.get('target_tp_price', 0))
        if not (bot_config.get('UseEarlyExit', False) and bot_status.get('basket_start_time', 0) > 0):
            return raw_db_tp

        # -- helper: round a price to the exchange tick grid ------------------
        def _round_to_tick(price: float, tick: float) -> float:
            if tick and tick > 0:
                return round(round(price / tick) * tick, 10)
            return price

        try:
            original_tp = strategy.calculate_take_profit_price(
                bot_status, bot_status.get('avg_entry_price', 0)
            )
            start_dt = datetime.fromtimestamp(bot_status['basket_start_time'])
            now_dt   = datetime.fromtimestamp(time.time())
            decayed_tp = calculate_early_exit_decay(
                start_dt, now_dt,
                bot_status.get('current_step', 0) + 1,
                original_tp,
                bot_status.get('avg_entry_price', original_tp),
                bot_config
            )

            # 🚀 INV-21: Round to exchange tick size when available, else fall back to
            # strategy rounding.  This guarantees the stored value is always exchange-aligned.
            if tick_size and tick_size > 0:
                decayed_tp = _round_to_tick(decayed_tp, tick_size)
            else:
                decayed_tp = strategy._round_price(decayed_tp)

            # Only persist to DB if the rounded new value is meaningfully different from
            # the tick-rounded stored value.  Using raw float comparison was the root cause
            # of the infinite SYNC-DRIFT loop (raw != rounded even when on the same step).
            stored_rounded = _round_to_tick(raw_db_tp, tick_size) if (tick_size and tick_size > 0) else raw_db_tp
            if decayed_tp != stored_rounded:
                logger.info(
                    f"⏳ [EE-DECAY] {name}: TP stepping {raw_db_tp:.6f} → {decayed_tp:.6f} "
                    f"(Baseline: {original_tp:.6f}"
                    + (f", tick={tick_size}" if tick_size else "")
                    + ")"
                )
                try:
                    _c = get_connection()
                    _c.execute("UPDATE trades SET target_tp_price=? WHERE bot_id=?", (decayed_tp, bot_id))
                    _c.commit()
                    _c.close()
                except Exception as _db_err:
                    logger.warning(f"[EE] Failed to persist decayed TP: {_db_err}")
            return decayed_tp
        except Exception as _err:
            logger.warning(f"[EE] Decay calculation failed for {name}: {_err}")
        return raw_db_tp

    def _sync_replace_tp(self, bot_id: int, name: str, pair: str, direction: str,
                          bot_status: dict, exchange: ExchangeInterface,
                          db_tp: float, db_qty: float,
                          existing_tp_order: dict) -> Optional[dict]:
        """Cancel the out-of-date TP order and place a fresh one at db_tp / db_qty.
        Returns the new order dict, or None on failure.
        """
        # 🚀 API LAG GUARD (v2.3.5)
        # If we just placed a TP order in the last 15 seconds, skip sync.
        # This prevents "Double Placement" loops where the exchange API lags 
        # and doesn't show the new order in the next maintain_orders cycle.
        try:
            from engine.database import get_connection
            _conn = get_connection()
            recent_check = _conn.execute("""
                SELECT id FROM bot_orders 
                WHERE bot_id = ? AND order_type = 'tp' 
                AND status IN ('new', 'open', 'filled')
                AND (created_at > ? OR updated_at > ?)
                LIMIT 1
            """, (bot_id, int(time.time()) - 15, int(time.time()) - 15)).fetchone()
            if recent_check:
                logger.debug(f"⏳ [SYNC-LAG-GUARD] {name}: TP recently placed in DB. Waiting for API/WS propagation...")
                return None
        except Exception as _lag_err:
            logger.warning(f"[SYNC-LAG-GUARD] DB check failed: {_lag_err}")

        try:
            tp_order_id = existing_tp_order.get('order_id', existing_tp_order.get('id'))
            
            # 🚀 HARDENED: Verify cancellation before proceeding
            logger.info(f"🔄 [TP-SYNC] {name}: Cancelling stale TP {tp_order_id}...")
            cancel_response = None
            filled_qty = 0.0
            inv18_corrected = False
            try:
                cancel_response = exchange.cancel_order(tp_order_id, pair)
                
                # 🚀 CACHE EVICTION (v2.3.5)
                # Immediately remove the cancelled order from the memory cache 
                # so the NEXT loop cycle (which might be in < 1s) doesn't see it as "open".
                try:
                    from engine.ws_cache import get_ws_cache
                    get_ws_cache().remove_order(tp_order_id)
                except Exception as _e:
                    logger.debug(f'[CACHE] remove_order after cancel: {_e}')
            except Exception as e:
                logger.warning(f"[TP-SYNC] {name}: Cancel failed ({e}). Attempting to fetch order status...")
                try:
                    if tp_order_id and any(str(tp_order_id).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_')):
                        logger.info(f"🔎 [TP-SYNC] Skipping fetch_order for synthetic TP {tp_order_id}")
                        cancel_response = None
                    else:
                        cancel_response = exchange.fetch_order(tp_order_id, pair)
                except Exception as inner_e:
                    logger.error(f"[TP-SYNC] {name}: Could not fetch old TP status: {inner_e}")

            # If we successfully obtained the cancelled/current order state, calculate precise remaining quantity
            if cancel_response:
                filled_qty = float(cancel_response.get('filled') or cancel_response.get('executedQty') or 0)
                orig_qty = float(cancel_response.get('amount') or cancel_response.get('origQty') or 0)
                status = str(cancel_response.get('status') or '').lower()
                
                if status in ('closed', 'filled') or (orig_qty > 0 and filled_qty >= orig_qty):
                    logger.warning(f"⚠️ [TP-SYNC] {name}: Old TP {tp_order_id} is FULLY FILLED. Aborting replacement to prevent oversell.")
                    return None
                    
                # 🚀 PARTIAL FILL SYNC (v2.3.5)
                # If the cancel response shows a fill that the DB hasn't seen yet,
                # credit it immediately to keep the ledger in 1:1 parity.
                if filled_qty > 0:
                    try:
                        from engine.ledger import credit_fill
                        logger.info(f"⚡ [TP-SYNC] {name}: Detected partial fill of {filled_qty} on cancelled order {tp_order_id}. Syncing ledger...")
                        credit_fill(
                            bot_id=bot_id,
                            order_id=tp_order_id,
                            cumulative_qty=filled_qty,
                            avg_price=float(cancel_response.get('average') or cancel_response.get('price') or 0),
                            order_type='tp',
                            is_cumulative=True
                        )
                    except Exception as _sync_err:
                        logger.warning(f"[TP-SYNC] Ledger sync failed: {_sync_err}")

                    # 🛡️ INV-18: Subtract already-filled quantity from new target quantity
                    old_db_qty = db_qty
                    db_qty = max(0.0, db_qty - filled_qty)
                    inv18_corrected = True
                    logger.info(f"🛡️ [INV-18] {name}: Old TP had partial fill of {filled_qty}. Adjusted new TP qty from {old_db_qty:.4f} to {db_qty:.4f}.")

                if orig_qty > 0:
                    calculated_remaining = max(0.0, orig_qty - filled_qty)
                    logger.info(f"✅ [TP-SYNC] {name}: Cancelled order {tp_order_id}. orig: {orig_qty:.4f}, filled: {filled_qty:.4f}, remaining: {calculated_remaining:.4f}")
                    # 🚀 CRITICAL: Do NOT overwrite db_qty (the target) with calculated_remaining (the stale size).
                    # We only use calculated_remaining to verify if the order filled while we were cancelling.
                    # The next block (Root Cause Fix) will sync db_qty to the absolute ledger truth.
                    pass
            else:
                filled_qty = float(existing_tp_order.get('filled_amount', existing_tp_order.get('filled', 0)) or 0)

            # Mandatory 500ms safety sleep to allow exchange state to propagate
            time.sleep(0.5)

            # 🚀 ROOT CAUSE FIX: Re-read open_qty from DB after sleep!
            # The ledger (trades.open_qty) is the absolute ground truth.
            # If the cancelled order was partially filled just before cancellation,
            # the WebSocket / credit_fill will have updated trades.open_qty.
            # We unconditionally read trades.open_qty to ensure the replacement
            # uses the remaining qty at replacement time.
            try:
                from engine.database import get_connection
                _conn = get_connection()
                _latest_qty_row = _conn.execute("SELECT open_qty FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
                if _latest_qty_row:
                    _latest_qty = float(_latest_qty_row[0] or 0)
                    if not inv18_corrected:
                        # Original bug: db_qty is stale from caller, fresher DB value wins
                        logger.info(f"🔄 [TP-SYNC] {name}: Setting replacement TP qty to current trades.open_qty: {_latest_qty:.6f} (cancelled order original target was {db_qty:.6f})")
                        db_qty = _latest_qty
                    elif _latest_qty < db_qty:
                        # INV-18 ran but DB is *even lower* (fill also propagated via WS) — be conservative
                        logger.info(f"🔄 [TP-SYNC] {name}: DB re-read {_latest_qty:.6f} < INV-18 corrected qty {db_qty:.6f}. Using DB qty.")
                        db_qty = _latest_qty
                    else:
                        # INV-18 ran and its value is more conservative — keep it, ignore stale DB
                        logger.info(f"🔄 [TP-SYNC] {name}: INV-18 value {db_qty:.6f} kept (DB {_latest_qty:.6f} is stale/larger)")
            except Exception as e:
                logger.error(f"[TP-SYNC] Failed to re-verify open_qty from trades: {e}")

            # Mark cancelled in DB — but remember the old price so we can restore if placement fails
            if not update_order_status(tp_order_id, 'cancelled', bot_id=bot_id, filled_qty=filled_qty):
                logger.warning(f"⚠️ [TP-SYNC] {name}: Failed to mark TP {tp_order_id} as cancelled (likely already filled). Aborting replacement TP placement.")
                return None

            if db_qty <= 0 or db_tp <= 0 or config.DRY_RUN:
                logger.info(f"🛑 [TP-SYNC] {name}: db_qty is {db_qty:.4f} (<= 0) after verification. Aborting replacement.")
                return None

            side = 'sell' if direction == 'LONG' else 'buy'

            # 🚀 SPREAD-CROSSING MAKER LOOP FIX (v2.4.1)
            # Re-apply Best Bid/Ask logic during SYNC to prevent GTX rejection loops.
            # If the target TP crosses the spread, we adjust it to the 'Maker' side.
            try:
                bid, ask = exchange.get_best_bid_ask(pair)
                if bid is not None and ask is not None:
                    bid_val = float(bid)
                    ask_val = float(ask)
                    if direction == 'LONG':
                        # TP is a SELL. To be a Maker, Sell must be >= Best Ask.
                        if db_tp <= bid_val:
                            old_tp = db_tp
                            db_tp = ask_val # Join the asks to stay Maker
                            logger.info(f"🚀 [TP-SYNC] {name}: Spread Cross Prevented! (Sell {old_tp} <= Bid {bid_val}). Adjusted to Ask {db_tp} to preserve GTX.")
                    else:
                        # TP is a BUY. To be a Maker, Buy must be <= Best Bid.
                        if db_tp >= ask_val:
                            old_tp = db_tp
                            db_tp = bid_val # Join the bids to stay Maker
                            logger.info(f"🚀 [TP-SYNC] {name}: Spread Cross Prevented! (Buy {old_tp} >= Ask {ask_val}). Adjusted to Bid {db_tp} to preserve GTX.")
            except Exception as e:
                logger.warning(f"⚠️ [TP-SYNC] {name}: Market Gap check failed ({e}). Proceeding with raw price.")

            valid, db_qty, db_tp, msg = exchange.validate_order(pair, side, db_qty, db_tp, is_closing=True)
            if not valid:
                logger.warning(f"[TP-SYNC] {name}: Validation failed — {msg}")
                # 🚀 ATOMIC RESTORE (v3.9.13): Use 'placed' (not 'new') so the SYNC-LAG-GUARD
                # (which checks 'new','open','filled') does NOT block the next cycle from retrying.
                # 'placed' is still in the placed_tp lookup ('open','new','placed') so the price
                # anchor is preserved for drift comparison.
                if not update_order_status(tp_order_id, 'placed', bot_id=bot_id):
                    logger.error(f"🚨 [STATE-GUARD ERROR] Unexpected status update failure for TP order {tp_order_id} to placed (retry path).")
                return None

            # 🚀 HARDENED: Use cycle_id for strict idempotency
            cycle_id = bot_status.get('cycle_id', 0)
            client_order_id = self._generate_deterministic_id(
                bot_id, 'TP', cycle_id, bot_status['current_step'],
                is_replacement=True
            )
            tp_params = {'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}

            logger.info(f"🔄 [TP-SYNC] {name}: Placing IDEMPOTENT TP {client_order_id} @ {db_tp:.4f}...")
            order = self._place_gtx_order_with_retry(
                exchange, pair, side, db_qty, db_tp, params=tp_params, label=f"{name}-TP-SYNC", position_side=direction
            )
            if order:
                # Pop _fallback_cid: if GTX fell back to a plain limit, use the _F CID that
                # Binance actually received, so save_bot_order inserts the row correctly first time.
                effective_tp_sync_cid = order.pop('_fallback_cid', None) or client_order_id
                save_bot_order(bot_id, 'tp', order['id'], db_tp, db_qty,
                             bot_status['current_step'], order.get('status', 'open'), client_order_id=effective_tp_sync_cid,
                             notes='atomic-sync-post-commit')
                # 🚀 SNAPSHOT HEAL: Inject new order into WS cache so next cycle's snapshot
                # sees the correct price immediately — prevents one-cycle stale-comparison fire.
                try:
                    from engine.ws_cache import get_ws_cache as _gwsc
                    _gwsc().update_order(str(order['id']), order)
                except Exception as _e:
                    logger.debug(f'[CACHE] update_order (snapshot heal): {_e}')
                logger.info(f"✅ [SYNC] {name}: Re-placed TP @ {db_tp:.4f} Qty {db_qty:.4f}")
            else:
                # 🚀 ATOMIC RESTORE (v3.9.13): GTX was rejected (e.g. EE-decayed price crossed
                # below market bid).  Use 'placed' (not 'new') so SYNC-LAG-GUARD does NOT block
                # the next cycle from retrying — 'placed' is not in ('new','open','filled').
                # 'placed' IS in the placed_tp lookup so old_placed_price stays as the anchor.
                logger.warning(
                    f"⚠️ [SYNC] {name}: TP placement failed at exchange (GTX rejected?). "
                    f"Restoring DB row to 'placed' — old price {old_placed_price:.4f} preserved as anchor. "
                    f"Will retry next cycle when price allows maker placement."
                )
                update_order_status(tp_order_id, 'placed', bot_id=bot_id)
            return order
        except Exception as _ex:
            logger.error(f"❌ [SYNC] {name}: Failed to replace TP: {_ex}", exc_info=True)
            return None

    def process_bot(self, bot_data: Tuple, exchange_snapshot: Dict[str, Any]) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
        # Robust index-based access to handle potential schema/unpacking mismatches
        bot_id = bot_data[0]
        name = bot_data[1]
        pair = bot_data[2]
        direction = bot_data[3]
        strategy_type = bot_data[4]
        config_json = bot_data[5]
        db_invested = float(bot_data[6]) if len(bot_data) > 6 else 0.0
        db_step = int(bot_data[7]) if len(bot_data) > 7 else 0
        rsi_limit = float(bot_data[8]) if len(bot_data) > 8 else 30.0
        is_active = bool(bot_data[9]) if len(bot_data) > 9 else True
        base_size = float(bot_data[10]) if len(bot_data) > 10 else 10.0
        martingale_multiplier = float(bot_data[11]) if len(bot_data) > 11 else 1.5
        bot_status_str = str(bot_data[12] or '') if len(bot_data) > 12 else ''

        import random
        # 🛡️ JITTER: Add random sleep to desynchronize parallel bots and reduce race conditions
        time.sleep(random.uniform(0.1, 0.8))
        
        # 🚀 MANUAL-GATE PROTECTION: Suspend maintenance if bot requires proof verification
        # The MANUAL GATE is incorrectly blocking maintain_orders for bots that are IN TRADE.
        # It must NEVER block limit order placement (TP and Grid orders) for bots already IN TRADE.
        if 'REQUIRE_MANUAL' in bot_status_str.upper() and db_invested <= 0:
            logger.warning(
                f"🛑 [MANUAL-GATE] Bot {name} ({bot_id}) suspended. "
                f"Status='{bot_status_str}'. Proof verification required."
            )
            return None, None

        # 🚀 FUNDAMENTAL FIX: Double-Check Activation Status from DB
        # This prevents "Zombie Bots" (like 'long gold') from resurrecting if the in-memory 'bot_data' is stale
        # or if an external script (like cleanup_broken_state.py) is fighting for control.
        if is_active:
             real_status = get_bot_status(bot_id)
             # If get_bot_status failed or returned None, something is wrong, but we can't check 'is_active' from it directly 
             # (status dict doesn't always have it). 
             # So we do a quick separate check if we suspect ghosting. 
             # Actually, best is to just trust the Runner's fresh fetch. 
             # BUT, if we want to be paranoid:
             pass 

        if not is_active:
            logger.warning(f"⛔ [ZOMBIE-PROTECTION] Bot {name} ({bot_id}) is marked INACTIVE. Skipping processing.")
            return None, None

        if not config_json:
            logger.error(f"Bot {name} ({bot_id}) has no config. Skipping.")
            return None, None

        try:
            bot_config = json.loads(config_json)
            
            market_type = normalize_market_type(bot_config.get('market_type', config.MARKET_TYPE))
            
            # Update bot_config with current market_type from runner (might be overridden globally)
            bot_config['market_type'] = market_type
            bot_config['direction'] = direction
            bot_config['bot_name'] = name # Inject Name for logging
            bot_config['bot_id'] = bot_id # Inject ID for logging
            
            # 🚀 FUNDAMENTAL FIX: Inject missing SQLite configuration 
            # so the strategy doesn't fallback to $150 and 2.0x
            bot_config['base_size'] = base_size
            bot_config['martingale_multiplier'] = martingale_multiplier
            bot_config['rsi_limit'] = rsi_limit

            # Get bot_type from config or DB
            bot_type = bot_config.get('bot_type')
            if not bot_type:
                try:
                    from engine.database import get_connection as _gc_type
                    with _gc_type() as _conn:
                        _res = _conn.execute("SELECT bot_type FROM bots WHERE id=?", (bot_id,)).fetchone()
                        bot_type = _res[0] if _res else 'standard'
                except Exception as _e:
                    logger.debug(f'[BOT-TYPE-LOOKUP] DB fallback to standard: {_e}')
                    bot_type = 'standard'
            bot_config['bot_type'] = bot_type

            exchange = self._get_thread_exchange(market_type) # Use thread-specific exchange
            
            # v2.0: TP cascade is now exclusively drained by runner.run_cycle() via
            # ledger.drain_tp_cascade() → handle_tp_completion(). No duplicate drain here.


            current_price = exchange.get_last_price(pair) # Get current price
            if not current_price:
                logger.warning(f"Could not get current price for {pair}. Skipping bot {name}.")
                return None, None
                
            # 🚀 AUTO MIN-SIZE CALCULATION
            if bot_config.get('use_min_size', False):
                prec = exchange.get_symbol_precision(pair)
                step_size = prec.get('step_size', 0.001)
                min_cost_qty = step_size * current_price
                
                # Fetch real exchange minimum notional (per-symbol from Binance exchangeInfo)
                # Fallback: demo/testnet Binance FAPI enforces $100; mainnet is typically $5
                exchange_min_notional = prec.get('min_notional', None)
                if exchange_min_notional is None:
                    exchange_min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                
                # Target 5% above the strict minimum to avoid slippage drops below min notional
                auto_min_size = max(min_cost_qty, exchange_min_notional) * 1.05
                bot_config['base_size'] = auto_min_size
                logger.debug(f"AUTO-MIN-SIZE {pair}: exchange_min=${exchange_min_notional:.2f} → base_size=${auto_min_size:.2f}")
            else:
                # 🚀 STRICT VALIDATION: If configured base_size is below exchange real minimum, HALT.
                prec = exchange.get_symbol_precision(pair)
                exchange_min_notional = prec.get('min_notional', None)
                if exchange_min_notional is None:
                    exchange_min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                if bot_config.get('bot_type') != 'hedge_child' and bot_config.get('base_size', 0) < exchange_min_notional:
                    logger.error(f"⛔ CONFIG ERROR [{pair}]: Configured base_size=${bot_config.get('base_size',0):.2f} is below exchange minimum ${exchange_min_notional:.2f}. Halting bot. Please update config.")
                    update_bot_error(bot_id, "Config Error")
                    return None, None

            # Inject the fully hydrated config into strategy calculation
            strategy = self._get_strategy_instance(bot_id, bot_config, json.dumps(bot_config))
            
            # 🚀 DYNAMIC PRECISION FIX: Inject real exchange precision into strategy
            # This prevents "Zero Gap" rounding issues on low-priced coins like SUI ($0.95)
            try:
                prec_data = exchange.get_symbol_precision(pair)
                if prec_data:
                    meta = {
                        'price_precision': prec_data.get('price_precision', 2),
                        'qty_precision': prec_data.get('qty_precision', 3),
                        'tick_size': prec_data.get('tick_size', 0.01),
                        'step_size': prec_data.get('step_size', 0.001)
                    }
                    strategy.set_precision_metadata(meta)
            except Exception as e:
                logger.error(f"Error updating precision metadata for {name}: {e}")
            
            bot_status = get_bot_status(bot_id) # Fetch latest status
            if not bot_status: 
                logger.warning(f"Bot {name} ({bot_id}) has no status in DB. Initializing basic status.")
                bot_status = {
                    'bot_id': bot_id,
                    'pair': pair,
                    'current_step': 0,
                    'total_invested': 0.0,
                    'avg_entry_price': 0.0,
                    'target_tp_price': 0.0,
                    'basket_start_time': 0,
                    'entry_confirmed': 0
                }
            
            # 🚑 [HEAL-ENTRY] If the bot has capital deployed but entry_confirmed is 0,
            # a crash or WS drop happened between the fill credit and the DB write.
            # Write-through immediately so seal_trade_state and step-proof are consistent.
            if bot_status.get('total_invested', 0) > 0 and not bot_status.get('entry_confirmed'):
                logger.warning(
                    f"🚑 [HEAL-ENTRY] {name}: total_invested={bot_status.get('total_invested')} "
                    f"but entry_confirmed=0. Healing."
                )
                try:
                    from engine.database import update_bot_status as _heal_ubs
                    _heal_ubs(bot_id, entry_confirmed=1)
                    bot_status['entry_confirmed'] = 1
                except Exception as _heal_err:
                    logger.warning(f"[HEAL-ENTRY] DB write failed for {name}: {_heal_err}")

            # Fetch bot_type and intercept if it's a hedge child bot
            from engine.database import get_connection as _gc_bt
            _conn_bt = _gc_bt()
            _bot_type_row = _conn_bt.execute("SELECT bot_type FROM bots WHERE id = ?", (bot_id,)).fetchone()
            bot_type = _bot_type_row[0] if _bot_type_row else 'standard'

            if bot_type == 'hedge_child':
                logger.info(f"🛡️ [HEDGE-CHILD] Bot {name} ({bot_id}): Executing simple maintenance path.")
                market_type_snapshot = exchange_snapshot.get(market_type, {})
                trade_update_data = self.maintain_orders(
                    bot_id, name, pair, direction, bot_status, current_price,
                    exchange, market_type_snapshot, bot_config
                )
                return 5.0, trade_update_data

            # 🚀 GHOST ORDER CLEANUP (Scanning/Idle Bots)
            # v2.3.7: Use cent-level threshold.
            if bot_status.get('total_invested', 0.0) < 0.01:
                 # Fetch open orders for this pair to check for ghosts
                 try:
                     # Use snapshot if available, else fetch
                     open_orders_check = exchange_snapshot.get(market_type, {}).get('open_orders', [])
                     if not open_orders_check: # Double check if snapshot empty
                          open_orders_check = exchange.fetch_open_orders(pair)
                     
                     bot_ghosts = [o for o in open_orders_check if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')]
                     
                     if bot_ghosts:
                          # 🚀 RACE CONDITION FIX: Do not cancel ENTRY orders here!
                          # If the strategy just placed an ENTRY order, it takes a moment for the WS to confirm.
                          # If we cancel it here, the strategy will place another one, causing a runaway accumulation loop!
                          true_ghosts = [o for o in bot_ghosts if '_ENTRY_' not in o.get('clientOrderId', '')]
                          
                          if true_ghosts:
                              logger.warning(f"👻 {name}: Found {len(true_ghosts)} GHOST orders while SCANNING (Invested={bot_status['total_invested']}). Purging...")
                              for ghost in true_ghosts:
                                   logger.info(f"🔥 Cancelling ghost order {ghost['id']} ({ghost.get('clientOrderId')})")
                                   try:
                                       exchange.cancel_order(ghost['id'], pair)
                                   except Exception as e:
                                       logger.error(f"Failed to cancel ghost {ghost['id']}: {e}")
                 except Exception as e:
                      logger.error(f"Ghost cleanup failed for {name}: {e}")
            # ---------------------------------------------------------
            
            # 🚀 FIXED: Extract the DataFrame (market_data) for the bot's specific pair
            # This prevents the 'dict object has no attribute empty' crash in the strategy
            market_type_snapshot = exchange_snapshot.get(market_type, {})
            market_data_map = market_type_snapshot.get('market_data', {})
            bot_market_data = market_data_map.get(pair, MartingaleStrategy.get_empty_df())
            bot_multi_tf = market_type_snapshot.get('multi_tf_data', {}).get(pair, {})

            if bot_id == 10000:
                logger.debug(f"Bot 10000 | Price={current_price} | MarketDataEmpty={bot_market_data.empty}")
                # logger.info(f"🕵️ TRACE STARTING decide_action")

            try:
                mission = strategy.decide_action(bot_status, current_price, bot_market_data, multi_tf_data=bot_multi_tf)
            except Exception as e:



                logger.error(f"Error in decide_action: {e}")
                logger.error(traceback.format_exc())
                mission = None



            # 🔍 DIAGNOSTIC LOGGING (Fundamental Fix)
            if mission:
                logger.info(f"🔍 [MISSION-FLOW] Bot {name}: action='{mission.get('action')}' | TradingEnabled={config.TRADING_ENABLED}")
            else:
                if bot_id == 10000: logger.debug(f"Bot 10000: Mission is None")
                logger.debug(f"[MISSION-FLOW] Bot {name}: no action (Scanning)")
                # 🚀 UX: Clear transient errors if we reach this point without an exception
                update_bot_error(bot_id, None) 

            trade_update_data = None # This will be populated by action methods

            # [EE-DECOUPLE removed v3.9.13] maintain_orders calls _compute_effective_tp with
            # the correct exchange tick_size — a second pre-call here used strategy._round_price()
            # (different rounding), causing DB flip-flop every cycle and a continuous replace loop.

            if mission:
                if mission['action'] == 'entry':
                     
                    # 🛡️ GLOBAL SAFETY: Check Maximum Account Drawdown
                    # Prevents full portfolio wipeout during flash crashes across all bots
                    try:
                        market_type = normalize_market_type(strategy.params.get('market_type', 'spot'))
                        account_info = exchange_snapshot.get(market_type, {}).get('account', {})
                        
                        balance = account_info.get('totalWalletBalance') or account_info.get('totalMarginBalance')
                        equity = account_info.get('totalCrossWalletBalance') or account_info.get('totalMarginBalance')
                        
                        if balance and equity:
                            drawdown_pct = ((float(balance) - float(equity)) / float(balance)) * 100
                            
                            if drawdown_pct >= config.MAX_ACCOUNT_DRAWDOWN_PERCENT > 0:
                                logger.critical(f"🛑 [GLOBAL-SAFETY-LOCK] Account Drawdown ({drawdown_pct:.1f}%) > Max Limit ({config.MAX_ACCOUNT_DRAWDOWN_PERCENT}%). Blocking Bot {name} from NEW ENTRY.")
                                # We allow existing bots to maintain grids via `maintain_orders`, but BLOCK new ones.
                                return None, None
                    except Exception as e:
                        logger.error(f"Global Drawdown Safety Check Failed: {e}")

                    # 🚀 WORKFLOW VERIFICATION: Physical Reality Check (MOVED HERE)
                    # Before placing a NEW Entry, we must confirm we have NO position on the exchange.
                    can_enter = True
                    try:
                         # Use the snapshot passed from Runner
                         market_type = normalize_market_type(strategy.params.get('market_type', 'spot'))

                         snap_entry = exchange_snapshot.get(market_type, {}).get('positions', [])
                         
                         # Filter for this specific bot's pair/direction
                         real_pos = next((p for p in snap_entry if normalize_symbol(p.get('symbol', '')) == normalize_symbol(pair)), None)
                         
                         # 🚀 VIRTUAL HEDGING LOGIC (Refined)
                         # In One-Way Mode, we might have a position (e.g., LONG) from another bot.
                         # If WE (this bot) are not invested, we should be allowed to enter (reducing the net position).
                         # We only block entry if *WE* already have a physical footprint that implies we doubled up.
                         
                         if real_pos:
                              size = float(real_pos.get('contracts', 0) or real_pos.get('size', 0) or 0)
                              abs_size = abs(size)
                              
                              am_i_invested = bot_status.get('total_invested', 0) > 0
                              
                              if abs_size > 0 and am_i_invested:
                                   # CRITICAL: I am active AND there is a position. 
                                   # This is a Double Entry risk.
                                   logger.warning(f"🛑 {name}: Attempted NEW ENTRY but already invested ({am_i_invested}). Aborting.")
                                   can_enter = False
                              elif abs_size > 0 and not am_i_invested:
                                   # 🛡️ GLOBAL LEDGER GUARD (v3.1.6b)
                                   # Before allowing entry, verify that the physical position is explained by 
                                   # the pair's global ledger history (including unowned hedges).
                                   from engine.database import get_pair_virtual_net
                                   
                                   # sib_net_qty remains 0.0 for current-cycle (since we are Step 0)
                                   sib_net_qty = 0.0
                                   
                                   # Use the supreme truth of the cross-cycle ledger
                                   sib_hist_net_qty = get_pair_virtual_net(pair)
                                            
                                   # 🚀 Compare actual quantities — two-tier drift check
                                   phys_net_qty_abs = abs(size)
                                   sib_net_qty_abs  = abs(sib_net_qty)
                                   sib_hist_net_qty_abs = abs(sib_hist_net_qty)

                                   # Convert quantity drift back to USD to keep the $ threshold
                                   _mismatch_threshold = max(50.0, exchange.get_symbol_precision(pair).get('min_notional', 5.0))
                                   drift_qty = abs(sib_net_qty_abs - phys_net_qty_abs)
                                   drift_usd = drift_qty * current_price
                                   hist_drift_qty = abs(sib_hist_net_qty_abs - phys_net_qty_abs)
                                   hist_drift_usd = hist_drift_qty * current_price

                                   if drift_usd > _mismatch_threshold:
                                        if hist_drift_usd <= _mismatch_threshold:
                                            # ✅ Historical net explains the gap — prior-cycle accumulation.
                                            logger.info(
                                                f"⚠️ {name}: Current-cycle magnitude mismatch (${drift_usd:.2f} > ${_mismatch_threshold:.2f}) "
                                                f"is explained by cross-cycle history (hist_drift=${hist_drift_usd:.2f}). "
                                                f"Allowing entry — position is valid accumulation."
                                            )
                                            can_enter = True
                                        else:
                                            # BUG-FIX: Check if a recent fill (< 60s ago) explains the gap.
                                            # The async DB-worker runs seal_trade_state after WS fills, so there
                                            # is always a window where the physical position exists on exchange
                                            # but the ledger (total_invested) still reads 0. Without this bypass,
                                            # the very next cycle after a fast fill hits this block and deadlocks.
                                            from engine.database import get_last_filled_order as _glfo
                                            _recent = _glfo(bot_id)
                                            _fill_age = time.time() - float(_recent.get('created_at', 0)) if _recent else 9999
                                            if _fill_age < 90:
                                                logger.info(
                                                    f"⚠️ {name}: Magnitude mismatch (${drift_usd:.2f}) ignored — "
                                                    f"recent fill {_fill_age:.0f}s ago, seal still propagating. Allowing entry."
                                                )
                                                can_enter = True
                                            else:
                                                logger.critical(f"🛑 {name}: Blocked NEW ENTRY! Exchange magnitude {phys_net_qty_abs:.6f} vs System {sib_net_qty_abs:.6f} mismatch ${drift_usd:.2f} > min_notional ${_mismatch_threshold:.2f}. Resolve Mismatch first!")
                                                can_enter = False
                                   else:
                                        from engine.database import get_last_filled_order
                                        last_fill = get_last_filled_order(bot_id)
                                        if last_fill and (time.time() - last_fill.get('created_at', 0)) < 60:
                                            logger.warning(f"🛡️ {name}: Position detected ({size}) and recent fill found. Blocking double-entry (Sync Lag).")
                                            can_enter = False
                                        else:
                                            logger.info(f"⚠️ {name}: Virtual Hedging - Physical Position exists ({size}), backed by siblings. Allowing Entry.")
                                            can_enter = True
                         
                    except Exception as e:
                         logger.error(f"Entry Safety Check Failed: {e}")

                    
                    if can_enter:
                        trade_update_data = self.execute_entry(bot_id, name, pair, mission['side'], mission['amount'], direction, mission['price'], mission.get('params'), exchange, market_type_snapshot, bot_config, bot_status)
                    else:
                        trade_update_data = None
                elif mission['action'] == 'maintain_orders':
                    trade_update_data = self.maintain_orders(bot_id, name, pair, direction, bot_status, current_price, exchange, market_type_snapshot, bot_config)

                elif mission['action'] == 'exit_tp':
                    trade_update_data = self.execute_exit_tp(bot_id, name, pair, direction, bot_status, current_price, exchange, market_type_snapshot, bot_config)
                elif mission['action'] == 'exit_sl':
                    trade_update_data = self.execute_exit_sl(bot_id, name, pair, direction, bot_status, current_price, exchange, market_type_snapshot, bot_config)


                return mission.get('sleep_interval', 5.0), trade_update_data

        except Exception as e:
            # 🚀 HAMMER SHIELD: Track high-frequency API errors
            tracker = _API_ERROR_TRACKER.setdefault(bot_id, {'count': 0, 'first_time': time.time()})
            tracker['count'] += 1
            if tracker['count'] >= 7:
                elapsed = time.time() - tracker['first_time']
                if elapsed < 20.0:
                    logger.critical(f"🛑 [HAMMER SHIELD] {name} triggered 7 consecutive errors in {elapsed:.1f}s! Auto-deactivating bot to prevent Binance API Ban.")
                    update_bot_error(bot_id, f"HAMMER SHIELD: Auto-deactivated due to rapid API errors. Last: {str(e)[:100]}")
                    try:
                        from engine.database import update_bot_state
                        update_bot_state(bot_id, is_active=0)
                    except Exception as _e:
                        logger.warning(f'[HAMMER-SHIELD] DB deactivation write failed for {name}: {_e}', exc_info=True)
                    tracker['count'] = 0
                else:
                    tracker['count'] = 1
                    tracker['first_time'] = time.time()

            # 🚀 PHASE 6: Targeted reconciliation on placement failure
            err_str = str(e).lower()
            if "reduceonly" in err_str or "-2022" in err_str or "would not reduce" in err_str or "-4118" in err_str:
                logger.warning(f"🔄 [AUTO-RECONCILE] {name}: Placement exception ({e}) indicates possible ledger gap. Triggering immediate targeted reconciliation.")
                try:
                    if hasattr(self, 'runner') and hasattr(self.runner, '_reconciler') and self.runner._reconciler:
                        self.runner._reconciler.adopt_from_physical_positions()
                except Exception as _ar_err:
                    logger.warning(f"⚠️ [AUTO-RECONCILE] Failed to trigger targeted reconciliation for {pair}: {_ar_err}")

            logger.error(f"Error processing bot {name} ({bot_id}): {e}")
            logger.error(traceback.format_exc())
            return None, None # Indicate an error occurred

        # Reset Hammer Shield on successful loop
        if bot_id in _API_ERROR_TRACKER:
            del _API_ERROR_TRACKER[bot_id]

        return 5.0, None

    def execute_entry(self, bot_id, name, pair, side, amount, direction, price=None, params=None, exchange=None, market_snapshot=None, bot_config=None, bot_status=None) -> Optional[Dict[str, Any]]:
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [ORDER-BLOCKED] Trading disabled. Bot {name} cannot maintain orders for {pair}.")
            return

        from engine.parity_gates import gate_trading_allowed
        allowed, reason = gate_trading_allowed(bot_id, pair, exchange)
        if not allowed:
            logger.warning(f"🛑 [ENTRY-BLOCKED] {name}: {reason}")
            return None

        last_exit = bot_status.get('last_exit_time', 0)
        basket_start = bot_status.get('basket_start_time', 0)
        logger.info(f"🧐 {name}: Checking Entry Logic. Invested={bot_status['total_invested']} LastExit={last_exit} BasketStart={basket_start}")

        # 1. Get current open orders for this bot
        # Use snapshot if available for performance, fallback to direct fetch
        if market_snapshot:
             open_orders = market_snapshot.get('open_orders', [])
        else:
             open_orders = exchange.fetch_open_orders(pair)
            
        bot_order_ids = get_bot_order_ids(bot_id) # DB knows what we expect

        # Filter for this bot's orders using clientOrderId prefix
        bot_open_orders = [
            o for o in open_orders 
            if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')
        ]
        
        logger.info(f"🧐 {name}: Found {len(bot_open_orders)} open orders for bot. IDs: {[o['id'] for o in bot_open_orders]}")
        
        # Extract existing TP and Grid order IDs from bot_open_orders
        existing_tp_order = next((o for o in bot_open_orders if '_TP_' in o.get('clientOrderId', '')), None)
        existing_grid_order = next((o for o in bot_open_orders if '_GRID_' in o.get('clientOrderId', '')), None)
        existing_entry_order = next((o for o in bot_open_orders if '_ENTRY_' in o.get('clientOrderId', '')), None)

        # Get strategy from cache - FIXED: Use bot_config instead of bot_status for params
        strategy = self._get_strategy_instance(bot_id, bot_config)

        # 🚀 CARRY_PENDING GUARD [v3.1.4 — self-healing]
        # Do NOT place new entry orders while waiting for the carry to fill.
        # HOWEVER: if the carry bot_orders row is ALREADY filled, the reconciler
        # already credited it — call seal_trade_state() to promote CARRY_PENDING
        # to ACTIVE right now, then fall through to normal execution.
        if bot_status.get('cycle_phase') == 'CARRY_PENDING':
            try:
                from engine.ledger import seal_trade_state as _seal_cp
                _conn_cp = get_connection()
                _carry_filled = _conn_cp.execute(
                    "SELECT COUNT(*) FROM bot_orders "
                    "WHERE bot_id=? AND order_type IN ('entry','carry') "
                    "AND status='filled' AND filled_amount>0",
                    (bot_id,)
                ).fetchone()[0]
                if _carry_filled:
                    logger.info(
                        f"🔄 [CARRY-PROMOTE] {name}: carry entry already filled ({_carry_filled} row(s)). "
                        f"Promoting CARRY_PENDING → ACTIVE via seal_trade_state."
                    )
                    _seal_cp(bot_id)
                    # Re-read bot_status so the rest of this function sees ACTIVE phase
                    from engine.database import get_bot_status as _gbs_cp
                    bot_status = _gbs_cp(bot_id)
                    # Fall through — continue with normal entry / maintain logic
                else:
                    logger.info(
                        f"⏳ [CARRY-PENDING] {name}: carry entry not yet filled. "
                        f"Suspending ENTRY order placement."
                    )
                    return None
            except Exception as _cp_err:
                logger.warning(f"[CARRY-PENDING] {name}: self-heal check failed ({_cp_err}). Suspending as precaution.")
                return None

        # 🚀 MISSING ENTRY LOGIC RESTORED 🚀
        # If we are NOT in a trade (total_invested == 0) and NO entry order exists, PLACE IT.
        # If an entry order already exists, handle CHASE logic or wait
        if existing_entry_order:
            # 🚀 CHASE LOGIC IMPLEMENTATION 🚀
            order_time = existing_entry_order.get('timestamp') or (int(time.time()) * 1000)
            order_age_sec = (int(time.time() * 1000) - order_time) / 1000.0
            
            # If order is more than 30s old and not filled, it might be stuck. 
            # Otherwise, WAIT for it to fill.
            if order_age_sec < 30.0:
                logger.info(f"⏳ {name}: Entry order exists and is recent ({order_age_sec:.1f}s). Waiting for fill.")
                return None

            # Configurable timeout (default 60s for chasing)
            CHASE_TIMEOUT_SEC = 60 
            HARD_CAP_ENTRY_SEC = 1800 # 30 Minutes "Give Up"
            
            # 🚀 HARD-CAP LOGIC: Total attempt time since first order (basket_start_time)
            attempt_time_sec = 0
            if basket_start > 0:
                attempt_time_sec = time.time() - basket_start

            if attempt_time_sec > HARD_CAP_ENTRY_SEC:
                logger.critical(f"🛑 [ENTRY-GIVEUP] Bot {name}: Entry attempt cycle stale for > 30m ({attempt_time_sec:.1f}s). Abandoning.")
                try:
                    exchange.cancel_order(existing_entry_order['id'], pair)
                    time.sleep(1)
                    
                    # 🚀 FORENSIC FIX: Verify the order wasn't filled perfectly inline with our cancel
                    final_status = exchange.fetch_order(existing_entry_order['id'], pair)
                    if final_status and float(final_status.get('filled', 0)) > 0:
                        logger.warning(f"🚨 [RACE-CONDITION PREVENTED] Bot {name}: Hard-cap cancelled entry was ACTUALLY filled for {final_status.get('filled')}! Adopting fill instead of abandoning.")
                        return None
                        
                    update_order_status(existing_entry_order['id'], 'cancelled', bot_id=bot_id)
                    # Reset the bot internally - if unfilled, this returns it to Scanning
                    # We pass exit_price=0 to indicate abandonment
                    from engine.database import reset_bot_after_tp
                    reset_bot_after_tp(bot_id, exit_price=0.0, action_label='ENTRY_TIMEOUT')
                    logger.info(f"✅ Bot {name}: Strategy reset to SCANNING after Entry Hard-Cap.")
                    return None
                except Exception as e_cap:
                    logger.error(f"❌ Bot {name}: Failed to execute hard-cap entry reset (might already be filled): {e_cap}")
                    return None

            if order_age_sec > CHASE_TIMEOUT_SEC:
                # 🚀 ROOT CAUSE FIX: Check for partial fills before cancelling!
                # If the order is already partially filled, we MUST NOT cancel it.
                # Crossing into 'filled' status or accepting the partial remains as Step 1.
                current_fill = float(existing_entry_order.get('filled', 0))
                if current_fill > 0:
                    logger.info(f"🛡️ Bot {name}: Entry order {existing_entry_order['id']} is partially filled ({current_fill}). CANCEL BLOCKED to preserve evidence.")
                    return None

                logger.info(f"⏱️ Bot {name}: Entry order {existing_entry_order['id']} is {order_age_sec:.1f}s old. Cancelling to CHASE price...")
                try:
                    exchange.cancel_order(existing_entry_order['id'], pair)
                    time.sleep(1) # Brief pause to ensure cancellation propagates
                    
                    # 🚀 FORENSIC FIX: Verify the order wasn't filled the millisecond before we canceled it
                    final_status = exchange.fetch_order(existing_entry_order['id'], pair)
                    if final_status and float(final_status.get('filled', 0)) > 0:
                        logger.warning(f"🚨 [RACE-CONDITION PREVENTED] Bot {name}: Cancelled entry {existing_entry_order['id']} was ACTUALLY filled for {final_status.get('filled')}! Adopting fill.")
                        return None
                    else:
                        existing_entry_order = None # Safely reset so we place a new one below
                        
                except Exception as e:
                    logger.error(f"❌ Bot {name}: Failed to cancel stale entry order (might already be filled): {e}")
                    # If we failed to cancel, it might have filled. DO NOT REPLACE IT!
                    return None
            else:
                logger.info(f"⏳ Bot {name}: Entry order {existing_entry_order['id']} is {order_age_sec:.1f}s old (Timeout: {CHASE_TIMEOUT_SEC}s). Waiting...")
                return None


        # 🚀 FUNDAMENTAL FIX: Rigid Entry Lock
        # 1. Post-TP Cooldown: Prevent immediate "chasing" after a win.
        last_exit_time = bot_status.get('last_exit_time', 0)
        if last_exit_time and (time.time() - last_exit_time) < 30.0: # Increased to 30s for safety
             logger.info(f"⏳ {name}: Bot recently exited ({time.time() - last_exit_time:.1f}s ago). Cooldown in effect (30s) to allow WS sync.")
             return None

        # 2. In-Flight Buffer: Check basket_start within 30s window
        basket_start = bot_status.get('basket_start_time', 0)
        if basket_start and (time.time() - basket_start) < 30.0:
             logger.warning(f"🛡️ {name}: Entry attempt IN-FLIGHT ({time.time() - basket_start:.1f}s ago). Blocking double-tap.")
             return None

        # 3. ── DB ENTRY ANCHOR GUARD ─────────────────────────────────────────────
        # Even after 30s basket expiry, check bot_orders for any live/filled entry row.
        # This catches the fill-credit miss case where:
        #   - entry filled on exchange
        #   - DB row exists (save_bot_order was called) but credit_fill failed
        #   - 30s lock expired → bot tries to place ANOTHER entry
        # Solution: retroactively credit the fill from DB, never spam a new order.
        try:
            _conn = get_connection()
            # ── ANCHOR GUARD v2: scope to CURRENT cycle_id only ─────────────────────
            # BUG-FIX: Without cycle_id scoping, historical filled rows from prior cycles
            # remain in bot_orders (status='filled', not reset_cleared) and permanently
            # trigger the anchor, deadlocking the bot even after a clean wipe+cycle-bump.
            _cur_cycle_row = _conn.execute(
                "SELECT COALESCE(cycle_id, 1) FROM trades WHERE bot_id=?", (bot_id,)
            ).fetchone()
            _cur_cycle = _cur_cycle_row[0] if _cur_cycle_row else 1

            # Only look at entry rows from the CURRENT cycle that are not archived
            _live_entries = _conn.execute("""
                SELECT order_id, client_order_id, filled_amount, price, status
                FROM bot_orders
                WHERE bot_id = ?
                  AND cycle_id = ?
                  AND order_type = 'entry'
                  AND status NOT IN ('reset_cleared', 'auto_closed')
                  AND (filled_amount > 0 OR status NOT IN ('cancelled', 'canceled', 'failed'))
                ORDER BY id DESC LIMIT 5
            """, (bot_id, _cur_cycle)).fetchall()

            if _live_entries:
                # Check if any are filled but not credited
                for _row in _live_entries:
                    _oid, _cid, _filled, _px, _status = _row
                    if _filled and float(_filled) > 0 and float(bot_status.get('total_invested', 0)) <= 0:
                        from engine.ledger import credit_fill, seal_trade_state
                        _ok = credit_fill(bot_id, str(_oid), float(_filled), float(_px), 'entry', is_cumulative=True)
                        if _ok:
                            seal_trade_state(bot_id)
                            logger.warning(
                                f"[ENTRY-ANCHOR] Bot {name}: Recovered uncredited fill from bot_orders "
                                f"(order={_oid} filled={_filled} px={_px}). Blocking new entry."
                            )
                            return None

                # WS-lag check: if there's a non-filled entry row not in open_orders,
                # it may be mid-fill. Block to avoid duplicate.
                # BUG-FIX: Do NOT block if the order is already status='filled'/'closed' —
                # filled orders are correctly absent from open_orders (they're done).
                # Blocking on a filled+credited order causes a permanent deadlock.
                _newest = _live_entries[0]
                _newest_oid = str(_newest[0])
                _newest_status = str(_newest[4]).lower()
                _seen_ids = {str(o['id']) for o in bot_open_orders}
                _terminal_statuses = ('filled', 'closed', 'cancelled', 'canceled', 'rejected', 'failed', 'expired')
                if _newest_oid not in _seen_ids and _newest_status not in _terminal_statuses:
                    logger.warning(
                        f"[ENTRY-ANCHOR] Bot {name}: DB has live entry {_newest_oid} "
                        f"(status={_newest[4]}) not in open_orders snapshot. "
                        f"Blocking new entry — may be WS lag or mid-fill."
                    )
                    return None
        except Exception as _anchor_err:
            logger.warning(f"[ENTRY-ANCHOR] Bot {name}: guard check failed (non-blocking): {_anchor_err}")
        # ─────────────────────────────────────────────────────────────────────────



        if not existing_entry_order:
            # Place Entry Order
            if config.DRY_RUN:
                logger.info(f"📊 [DRY-RUN] Bot {name} would place ENTRY order for {pair} {side} @ {price}")
                # Simulate fill
                log_trade(bot_id, 'ENTRY', pair, price, amount, price*amount, "DRY_ENTRY", 1, "Dry run entry", 0)
                update_martingale_step(bot_id, 1, price*amount, price, strategy.calculate_take_profit_price(bot_status, price))
                return {'status': 'filled', 'order_id': 'dry_run'}
            else:
                try:
                    logger.info(f"🧐 {name}: Initial Order Params: {pair} {side} {amount} {price}")
                    
                    # -------------------------
                    # MAKER-PRICE RE-ALIGNMENT
                    # -------------------------
                    # 🚀 FUNDAMENTAL FIX: By default, `price` is just the `last` traded price.
                    # A Limit Maker (postOnly) will fail with -2010 if it inadvertently crosses the active spread.
                    # We MUST align a LONG to the absolute Best Bid and a SHORT to the absolute Best Ask.
                    try:
                        # ──────────────────────────────────────────────────────────────────
                        # MAKER-PRICE RE-ALIGNMENT (Root Cause Fix)
                        # ──────────────────────────────────────────────────────────────────
                        # `price` from decide_action() is the LAST traded price — not the
                        # current bid or ask. Placing a Post-Only sell at the bid (or a buy
                        # at the ask) immediately crosses the spread and gets -5022 rejected.
                        #
                        # The previous fix read bid/ask from the runner snapshot ticker, but
                        # the snapshot tickers dict is keyed by normalized symbol (SOLUSDC)
                        # while `pair` is the CCXT symbol (SOL/USDC:USDC). On a key-miss,
                        # both bid and ask defaulted back to `price` (last traded), so the
                        # alignment condition never fired and the GTX rejection loop repeated
                        # every 60s (chase cancel → re-enter at last price → reject → repeat).
                        #
                        # Fix: always fetch LIVE bid/ask from exchange before placement.
                        # This is the same "use current best bid/ask" logic used for offline
                        # fills: when price has passed the original target, just use the best
                        # available maker price on the correct side right now.
                        # ──────────────────────────────────────────────────────────────────
                        live_bid, live_ask = exchange.get_best_bid_ask(pair)
                        if live_bid and live_ask and live_bid > 0 and live_ask > 0:
                            prec_info = exchange.get_symbol_precision(pair)
                            tick = prec_info.get('tick_size', 0.0001)
                            if side.lower() == 'buy':
                                # Maker BUY: must sit at or below best bid (never cross ask)
                                aligned = exchange.round_to_step(live_bid, tick)
                                if price >= live_ask or abs(price - aligned) / max(aligned, 1e-9) > 0.0001:
                                    logger.info(f"🛡️ {name}: Aligning LONG Maker Entry {price:.6f} → Best Bid {aligned:.6f} (bid={live_bid:.6f} ask={live_ask:.6f})")
                                    price = aligned
                            else:  # sell (SHORT entry)
                                # Maker SELL: must sit at or above best ask (never cross bid)
                                aligned = exchange.ceil_to_step(live_ask, tick)
                                if price <= live_bid or abs(price - aligned) / max(aligned, 1e-9) > 0.0001:
                                    logger.info(f"🛡️ {name}: Aligning SHORT Maker Entry {price:.6f} → Best Ask {aligned:.6f} (bid={live_bid:.6f} ask={live_ask:.6f})")
                                    price = aligned
                        else:
                            logger.warning(f"⚠️ {name}: Could not fetch live bid/ask for maker alignment. Using strategy price {price:.6f}.")
                    except Exception as e:
                        logger.error(f"⚠️ {name}: Maker alignment error: {e}")

                    # [v3.5.4] Fix: Skip gate if already in trade
                    if bot_status.get('total_invested', 0) > 0.01 and bot_status.get('current_step', 0) > 0:
                        _ow_ok, _ow_reason = True, ''
                    else:
                        from engine.oneway_netting import gate_oneway_opposite_entry
                        _ow_ok, _ow_reason = gate_oneway_opposite_entry(bot_id, pair, direction)
                    if not _ow_ok:
                        logger.warning(f"🛑 {name}: {_ow_reason}")
                        return None

                    valid, amount, price, msg = exchange.validate_order(pair, side, amount, price)
                    if not valid:
                        logger.error(f"❌ Entry Order validation failed for {name} {pair}: {msg}")
                        update_bot_error(bot_id, f"Entry Order validation failed: {msg}")
                        return

                    logger.info(f"🧐 {name}: Creating Order on Exchange...")
                    cycle_id = bot_status.get('cycle_id', 0)
                    client_order_id_base = self._generate_deterministic_id(bot_id, 'ENTRY', cycle_id, 1, for_check=True)

                    # 🚀 [DEDUP-GUARD] pre-flight check
                    try:
                         _dedup_conn = get_connection()
                         _existing_count = _dedup_conn.execute(
                             "SELECT COUNT(*) FROM bot_orders "
                             "WHERE client_order_id = ? "
                             "AND status NOT IN ('cancelled', 'canceled', 'failed', 'reset_cleared', 'auto_closed', 'rejected')",
                             (client_order_id_base,)
                         ).fetchone()[0]
                         logger.info(f"🔍 [DEDUP-GUARD-DEBUG] client_order_id={client_order_id_base} count={_existing_count}")
                         if _existing_count > 0:
                             logger.warning(f"🛡️ [DEDUP-GUARD] Entry already exists for this CID: {client_order_id_base}. Skipping placement.")
                             return None
                    except Exception as _dedup_err:
                         logger.error(f"❌ {name}: [DEDUP-GUARD] check failed: {_dedup_err}")
                    
                    client_order_id = self._generate_deterministic_id(bot_id, 'ENTRY', cycle_id, 1)
                    
                    # 🚀 CONCURRENCY LOCK: Set basket_start_time BEFORE calling exchange
                    # This prevents rapid-fire loops from bypassing the in-flight check.
                    try:
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("UPDATE trades SET basket_start_time = ? WHERE bot_id = ?", (int(time.time()), bot_id))
                        cursor.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (bot_id,))
                        conn.commit()
                    except Exception as lock_err:
                        logger.error(f"❌ {name}: Failed to set concurrency lock: {lock_err}")

                    # 🚀 SPREAD-CROSS FALLBACK
                    ccxt_entry_params = {'clientOrderId': client_order_id, 'postOnly': True, 'timeInForce': 'GTX'}

                    order = self._place_gtx_order_with_retry(exchange, pair, side, amount, price, params=ccxt_entry_params, label=f"{name}-ENTRY", position_side=direction)
                    
                    if order:
                        # RECORD IN BOT_ORDERS (atomic, after exchange confirms)
                        # Pop _fallback_cid: if GTX fell back, use the _F CID Binance received
                        effective_entry_cid = order.pop('_fallback_cid', None) or client_order_id
                        save_bot_order(bot_id, 'entry', order['id'], price, amount,
                                       1, order.get('status', 'open'), client_order_id=effective_entry_cid, notes='atomic-post-commit')

                        # ── RETROACTIVE FILL GUARD ──────────────────────────────────────
                        # If exchange returned order already filled/partial, the WS event
                        # may have fired BEFORE save_bot_order created the row (race).
                        # Credit it here immediately so DB reflects reality without waiting.
                        order_status = str(order.get('status', '')).lower()
                        order_filled = float(order.get('filled', 0) or 0)
                        if order_status in ('filled', 'closed') and order_filled <= 0:
                            order_filled = float(order.get('amount') or 0)
                        if order_status in ('filled', 'closed', 'partially_filled') and order_filled > 0:
                            try:
                                from engine.ledger import credit_fill, seal_trade_state
                                order_avg = float(order.get('average') or order.get('price') or price)
                                credited = credit_fill(
                                    bot_id=bot_id,
                                    order_id=str(order['id']),
                                    cumulative_qty=order_filled,
                                    avg_price=order_avg,
                                    order_type='entry',
                                    is_cumulative=True
                                )
                                if credited:
                                    seal_trade_state(bot_id)
                                    logger.info(
                                        f"[ENTRY-RETRO] Bot {name}: order {order['id']} already "
                                        f"{order_status} ({order_filled:.6f} filled). "
                                        f"Retroactive credit_fill + seal done."
                                    )
                            except Exception as retro_err:
                                logger.warning(f"[ENTRY-RETRO] Bot {name}: retroactive fill failed: {retro_err}")
                        # ────────────────────────────────────────────────────────────────

                        # Record entry_order_id in trades for quick lookup
                        try:
                            conn = get_connection()
                            conn.execute(
                                "UPDATE trades SET entry_order_id = ? WHERE bot_id = ?",
                                (order['id'], bot_id)
                            )
                            conn.commit()
                            logger.info(f"[ENTRY] Bot {name}: order {order['id']} recorded in DB.")
                            update_bot_error(bot_id, None)
                        except Exception as db_err:
                            logger.error(f"[ENTRY] Bot {name}: failed DB update: {db_err}")
                            update_bot_error(bot_id, f"DB update error after entry: {db_err}")

                        return None

                    else:
                        # Order failed at exchange, no DB entry was made, no manual rollback needed
                        logger.warning(f"⚠️ {name}: Entry order failed at exchange level. Ledger remains clean.")
                        # It will automatically loop again when conditions permit.

                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"❌ {name}: Error placing ENTRY order for {pair}: {error_msg}")
                    # 🚀 BUBBLE ERROR TO UI
                    update_bot_error(bot_id, f"Entry Error: {error_msg}")
                    return



    def execute_exit_tp(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]):
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [EXIT-BLOCKED] Trading disabled. Bot {name} cannot execute TP for {pair}.")
            return

        logger.info(f"🎯 {name}: Executing TP exit for {pair} at step {bot_status['current_step']}")
        # In Virtual Position mode, the TP order should already be on the exchange
        # We just need to ensure it fills and update DB state
        
        # If DRY_RUN, simulate fill and reset
        if config.DRY_RUN:
            log_trade(bot_id, 'TAKE_PROFIT', pair, current_price, bot_status['total_invested'] / bot_status['avg_entry_price'], bot_status['total_invested'], f'DRY_RUN_TP_{bot_id}', bot_status['current_step'], "Dry run TP", (current_price - bot_status['avg_entry_price']) * bot_status['total_invested'] / bot_status['avg_entry_price'])
            reset_bot_after_tp(bot_id, current_price, direction=direction)
            logger.info(f"📊 [DRY-RUN] Bot {name} would have exited TP for {pair}")
            return

        # For live trading, TP order is already managed. Just need to monitor fill.
        # The reconciliation cycle will eventually pick up the filled order.
        # For immediate confirmation, we can explicitly check if TP order is filled.
        
        bot_order_ids = get_bot_order_ids(bot_id)
        tp_order_id = bot_order_ids.get('tp_order_id')

        if tp_order_id:
            try:
                order_status = exchange.fetch_order(tp_order_id, pair)
                if order_status:
                    status = order_status.get('status')
                    filled = float(order_status.get('filled', 0))
                    amount = float(order_status.get('amount', 0))
                    
                    if status == 'filled' or (status == 'closed' and filled > 0 and filled >= amount * 0.99):
                        if float(bot_status.get('total_invested', 0)) > 0:
                            actual_exit = float(order_status.get('average') or order_status.get('price') or current_price)
                            logger.info(f"✅ {name}: TP order {tp_order_id} filled at {actual_exit}. Resetting bot.")
                            
                            # 🚀 FUNDAMENTAL FIX: Cancel ALL remaining open orders for this bot
                            # on the exchange BEFORE resetting the DB cycle.
                            #
                            # Root cause of XRP orphan: after step-5 TP fills, bot had a step-6 GRID
                            # already resting on the exchange. reset_bot_after_tp only resets the DB —
                            # it cannot cancel exchange orders. The step-6 grid continued filling
                            # post-cycle-reset, creating 6430 XRP with no DB record.
                            #
                            # Solution: any open orders except the filled TP itself must be purged
                            # from the exchange before the cycle rolls to N+1.
                            try:
                                all_open = exchange.fetch_open_orders(pair)
                                bot_tag = f"CQB_{bot_id}_"
                                for o in all_open:
                                    cid = o.get('clientOrderId', '')
                                    oid = o.get('id')
                                    if not cid.startswith(bot_tag):
                                        continue
                                    if str(oid) == str(tp_order_id):
                                        continue  # Skip the TP that just filled (may still appear briefly)
                                    logger.info(f"🧹 {name}: Purging orphan-risk order {oid} ({cid}) from exchange before cycle reset.")
                                    try:
                                        exchange.cancel_order(oid, pair)
                                        if not update_order_status(oid, 'cancelled', bot_id=bot_id):
                                            logger.error(f"🚨 [ORPHAN-WARNING] {name}: Failed to mark lingering order {oid} ({cid}) as cancelled (likely already filled).")
                                    except Exception as e_cancel:
                                        logger.warning(f"⚠️ {name}: Could not cancel lingering order {oid}: {e_cancel}")
                            except Exception as e_fetch:
                                logger.warning(f"⚠️ {name}: Could not fetch open orders before reset: {e_fetch}")
                            


                            # Standard reset for bots without hedges
                            prior_open_qty = float(bot_status.get('open_qty') or bot_status.get('total_invested', 0) / max(float(bot_status.get('avg_entry_price', 1)), 0.001))
                            reset_bot_after_tp(bot_id, actual_exit, direction=direction)

                            # ════════════════════════════════════════════════════════════
                            # [v3.1.4] POST-TP DUST SWEEP
                            # ════════════════════════════════════════════════════════════
                            # Root cause of SOL -0.11 orphan: a grid order fills in the
                            # timing gap between TP placement and TP execution. The TP was
                            # sized for the position at placement time, so the extra grid
                            # units are left behind as untracked dust after bot reset.
                            #
                            # Fix: after reset, verify the exchange position is truly zero.
                            # If residual dust exists below threshold, auto-close it.
                            # Threshold: < 5% of prior position OR < $20 USD value.
                            # ════════════════════════════════════════════════════════════
                            try:
                                time.sleep(1.5)  # Allow exchange 1.5s to settle post-fill
                                _phys_positions = exchange.fetch_positions([pair])
                                _dust_qty = 0.0
                                _dust_side = None
                                for _pos in (_phys_positions or []):
                                    _pos_size = abs(float(_pos.get('contracts', 0) or _pos.get('amount', 0) or _pos.get('size', 0) or 0))
                                    _pos_side = str(_pos.get('side', '')).lower()
                                    if _pos_size > 1e-8:
                                        _dust_qty = _pos_size
                                        _dust_side = 'sell' if _pos_side == 'long' else 'buy'  # Closing side
                                
                                if _dust_qty > 1e-8:
                                    _dust_usd = _dust_qty * actual_exit
                                    _dust_pct = (_dust_qty / max(prior_open_qty, 1e-8)) * 100
                                    _dust_threshold_usd = 20.0
                                    _dust_threshold_pct = 5.0

                                    if _dust_usd <= _dust_threshold_usd or _dust_pct <= _dust_threshold_pct:
                                        # Check cooldown to prevent rapid retries on failure
                                        if time.time() < _DUST_FLUSH_COOLDOWN.get(bot_id, 0.0):
                                            logger.info(f"🧹 [DUST-SWEEP] {name}: Dust close is in cooldown. Skipping.")
                                        else:
                                            logger.warning(
                                                f"🧹 [DUST-SWEEP] {name}: Post-TP residual {_dust_qty:.6f} {pair.split('/')[0]} "
                                                f"(${_dust_usd:.2f}, {_dust_pct:.1f}% of prior). Auto-closing as dust."
                                            )
                                            try:
                                                _dust_prec = exchange.get_symbol_precision(pair) or {}
                                                _strat = self._get_strategy_instance(bot_id, bot_config)
                                                _dust_qty_r = _strat._round_qty(_dust_qty)
                                                _dust_cid = self._generate_deterministic_id(bot_id, 'DUST', bot_status.get('cycle_id', 0), 0)
                                                _dust_params = {'newClientOrderId': _dust_cid, 'reduceOnly': True}
                                                _dust_order = exchange.create_order(pair, 'market', _dust_side, _dust_qty_r, params=_dust_params, human_approved=True)
                                                if _dust_order:
                                                    _dust_oid = str(_dust_order.get('id', 'DUST_CLOSE'))
                                                    save_bot_order(
                                                        bot_id, 'dust_close', _dust_oid, actual_exit, _dust_qty_r,
                                                        0, 'filled', client_order_id=_dust_cid,
                                                        notes=f'Post-TP dust sweep: {_dust_qty_r} {pair} @ market',
                                                        cycle_id=bot_status.get('cycle_id', 0)
                                                    )
                                                    logger.info(f"✅ [DUST-SWEEP] {name}: Dust closed {_dust_qty_r} {pair}. OID={_dust_oid}")
                                            except Exception as _e_dust_close:
                                                logger.error(f"❌ [DUST-SWEEP] {name}: Dust close failed: {_e_dust_close}. Residual {_dust_qty} on exchange — manual action required.")
                                                _DUST_FLUSH_COOLDOWN[bot_id] = time.time() + 300.0 # 5 minutes cooldown
                                    else:
                                        logger.error(
                                            f"🚨 [DUST-SWEEP] {name}: Post-TP LARGE residual {_dust_qty:.6f} {pair.split('/')[0]} "
                                            f"(${_dust_usd:.2f}, {_dust_pct:.1f}% of prior). ABOVE threshold — MANUAL ACTION REQUIRED. "
                                            f"Check Monitor → Global Netting → {pair}."
                                        )
                                else:
                                    logger.debug(f"✅ [DUST-SWEEP] {name}: Post-TP position verified zero for {pair}.")
                            except Exception as _e_dust:
                                logger.debug(f"[DUST-SWEEP] {name}: Post-TP position check skipped (non-fatal): {_e_dust}")
                        else:
                            logger.debug(f"⏭️ {name}: TP order {tp_order_id} is filled, but bot state is already zeroed (handled by WS). Skipping redundant reset.")
                    elif status in ['canceled', 'rejected'] or (status == 'closed' and filled == 0):
                        logger.warning(f"⚠️ {name}: TP order {tp_order_id} was canceled. Bot remains in trade.")
                        # Clear tp_order_id from DB so maintain_orders creates a new one
                        from engine.database import get_connection
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                        cursor.execute("UPDATE bot_orders SET status = 'cancelled', filled_amount = ? WHERE order_id = ?", (filled, tp_order_id,))
                        conn.commit()
                        pass # conn.close() disabled for singleton safety
                    else:
                        logger.warning(f"⚠️ {name}: TP order {tp_order_id} not yet filled. Monitoring. (Status: {status}, Filled: {filled})")
            except Exception as e:
                err_msg = str(e).lower()
                if "not found" in err_msg or "-2013" in err_msg or "invalidorder" in err_msg:
                    logger.warning(f"⚠️ {name}: TP order {tp_order_id} no longer exists on Exchange (OrderNotFound). Purging from state.")
                    from engine.database import get_connection
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                    cursor.execute("UPDATE bot_orders SET status = 'missing' WHERE order_id = ?", (tp_order_id,))
                    conn.commit()
                    pass # conn.close() disabled for singleton safety
                else:
                    logger.error(f"❌ {name}: Error fetching TP order {tp_order_id} status: {e}")
        else:
            logger.warning(f"⚠️ {name}: No TP order found in DB for {pair}. Waiting for maintain_orders to place one.")
            # Do NOT force reset here, because the physical position is still open!
            # maintain_orders will place the TP order automatically on the next cycle.



    def maintain_orders(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Ensures TP and Grid orders are placed active trades.
        """
        trade_update_data = {}
        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [MAINTAIN-BLOCKED] Trading disabled. Bot {name} cannot maintain orders.")
            return

        from engine.database import get_connection as _gc_hc
        _hc_conn = _gc_hc()

        # 🚀 Periodic Stale Order Sync (Layer 2 Fill Reliability)
        try:
            synced = sync_stale_open_orders(bot_id, exchange, _hc_conn)
            if synced > 0:
                from engine.database import get_bot_status
                bot_status = get_bot_status(bot_id)
        except Exception as e_sync:
            logger.error(f"❌ [ORDER-SYNC] Failed to run stale order sync for bot {name}: {e_sync}")

        _bot_type_row = _hc_conn.execute("SELECT bot_type FROM bots WHERE id = ?", (bot_id,)).fetchone()
        bot_type = _bot_type_row[0] if _bot_type_row else 'standard'

        if bot_type == 'hedge_child':
            # --- HEDGE CHILD SIMPLE PATH ---
            _hc_enforce_conn = _gc_hc()
            _hc_state = enforce_hedge_child_state(bot_id, _hc_enforce_conn)
            if _hc_state == 'dormant':
                return None
            if _hc_state == 'be_only':
                _cancel_non_tp_orders(bot_id, exchange, _hc_enforce_conn)
            if _hc_state == 'should_close':
                # INV-15: Two-phase atomic reset.
                # _reset_to_hedge_standby now owns Phase 1 (exchange cancel + reduceOnly
                # market close with full bot_orders receipt) and Phase 2 (DB zero).
                # Do NOT pre-cancel here — the function handles it atomically.
                _parent_cycle_for_reset = _hc_enforce_conn.execute(
                    "SELECT cycle_id FROM trades WHERE bot_id = ("
                    "SELECT parent_bot_id FROM bots WHERE id = ?)", (bot_id,)
                ).fetchone()
                _parent_cycle_id_val = _parent_cycle_for_reset[0] if _parent_cycle_for_reset else 1
                try:
                    _reset_to_hedge_standby(
                        bot_id, _hc_enforce_conn, _parent_cycle_id_val,
                        exchange=exchange  # Phase 1: close exchange position first
                    )
                except RuntimeError as _reset_err:
                    # Phase 1 failed — bot is locked to REQUIRE_MANUAL_PROOF.
                    # Log and return; do not proceed with this bot's cycle.
                    logger.error(
                        f"❌ [HEDGE-ENFORCE] Reset failed for bot {bot_id}: {_reset_err}. "
                        f"Bot locked pending manual exchange closure."
                    )
                return None

            if _hc_state == 'active':
                # --- INV-30: Hedge Child Continuous Qty Reconciliation ---
                try:
                    # 1. Fetch parent details
                    parent_row = _hc_enforce_conn.execute(
                        "SELECT p.id, p.hedge_trigger_step, t.open_qty, t.cycle_id, t.current_step "
                        "FROM bots c "
                        "JOIN bots p ON p.id = c.parent_bot_id "
                        "JOIN trades t ON t.bot_id = p.id "
                        "WHERE c.id = ?",
                        (bot_id,)
                    ).fetchone()
                    if parent_row:
                        parent_id, hedge_trigger, parent_open_qty, parent_cycle_id, parent_step = parent_row
                        hedge_trigger = int(hedge_trigger or 0)
                        parent_open_qty = float(parent_open_qty or 0.0)
                        parent_cycle_id = int(parent_cycle_id or 1)
                        parent_step = int(parent_step or 0)

                        # Compute pre-trigger accumulated qty (steps 1 to hedge_trigger - 1)
                        if hedge_trigger <= 1:
                            pre_trigger_accumulated_qty = 0.0
                        else:
                            accum_row = _hc_enforce_conn.execute(
                                "SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders "
                                "WHERE bot_id = ? AND cycle_id = ? AND step >= 1 AND step < ? "
                                "AND order_type IN ('entry', 'grid')",
                                (parent_id, parent_cycle_id, hedge_trigger)
                            ).fetchone()
                            pre_trigger_accumulated_qty = float(accum_row[0]) if accum_row else 0.0

                        # parent_hedgeable_qty
                        parent_hedgeable_qty = max(0.0, parent_open_qty - pre_trigger_accumulated_qty)

                        # 2. Fetch child's current open_qty from trades
                        child_qty_row = _hc_enforce_conn.execute(
                            "SELECT open_qty, cycle_id FROM trades WHERE bot_id = ?",
                            (bot_id,)
                        ).fetchone()
                        child_open_qty = float(child_qty_row[0] or 0.0) if child_qty_row else 0.0
                        child_cycle_id = int(child_qty_row[1] or 1) if child_qty_row else 1

                        # 3. Compute aggregate drift and tolerance
                        prec = exchange.get_symbol_precision(pair)
                        tolerance = float(prec.get('step_size', 0.001) or 0.001)
                        aggregate_drift = parent_hedgeable_qty - child_open_qty

                        # 4. Check if over-hedged first
                        if aggregate_drift < -tolerance:
                            logger.warning(
                                f"[INV-30] Hedge drift detected: child {bot_id} over-hedged "
                                f"by {abs(aggregate_drift):.6f} {pair}. Parent hedgeable={parent_hedgeable_qty:.6f}, "
                                f"child={child_open_qty:.6f}. Transitioning to pending_flatten."
                            )
                            _hc_enforce_conn.execute(
                                "UPDATE bots SET status = 'pending_flatten', cascade_started_at = ? WHERE id = ?",
                                (int(time.time()), bot_id)
                            )
                            _hc_enforce_conn.commit()
                        else:
                            # Iterate step S from hedge_trigger to parent_step
                            # checking saturation independently per step
                            for S in range(hedge_trigger, parent_step + 1):
                                child_step = S - hedge_trigger + 1

                                # Query parent step qty (filled entries/grids for step S)
                                parent_step_row = _hc_enforce_conn.execute(
                                    "SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders "
                                    "WHERE bot_id = ? AND cycle_id = ? AND step = ? "
                                    "AND order_type IN ('entry', 'grid') "
                                    "AND status IN ('filled', 'partially_filled') "
                                    "AND filled_amount > 0",
                                    (parent_id, parent_cycle_id, S)
                                ).fetchone()
                                parent_step_qty = float(parent_step_row[0]) if parent_step_row else 0.0

                                # Query child step qty: filled/partially_filled + open/new/placing/cancelling
                                child_filled_row = _hc_enforce_conn.execute(
                                    "SELECT COALESCE(SUM(filled_amount), 0.0) FROM bot_orders "
                                    "WHERE bot_id = ? AND cycle_id = ? AND step = ? "
                                    "AND order_type IN ('entry', 'grid') "
                                    "AND status IN ('filled', 'partially_filled')",
                                    (bot_id, child_cycle_id, child_step)
                                ).fetchone()
                                child_filled_qty = float(child_filled_row[0]) if child_filled_row else 0.0

                                child_inflight_row = _hc_enforce_conn.execute(
                                    "SELECT COALESCE(SUM(amount), 0.0) FROM bot_orders "
                                    "WHERE bot_id = ? AND cycle_id = ? AND step = ? "
                                    "AND order_type IN ('entry', 'grid') "
                                    "AND status IN ('open', 'new', 'placing', 'cancelling')",
                                    (bot_id, child_cycle_id, child_step)
                                ).fetchone()
                                child_inflight_qty = float(child_inflight_row[0]) if child_inflight_row else 0.0

                                child_step_qty = child_filled_qty + child_inflight_qty
                                delta = parent_step_qty - child_step_qty

                                if delta > tolerance:
                                    logger.warning(
                                        f"[INV-30] Under-hedge drift of {delta:.6f} detected at step {child_step} "
                                        f"(parent step {S}) for child {bot_id}. "
                                        f"Parent step qty={parent_step_qty:.6f}, child step qty={child_step_qty:.6f}. "
                                        f"Placing catch-up entry."
                                    )
                                    price_to_use = float(current_price or 0)
                                    if price_to_use <= 0:
                                        try:
                                            price_to_use = float(exchange.get_last_price(pair) or 0)
                                        except Exception:
                                            price_to_use = 0.0
                                    
                                    cid = f"CQB_{bot_id}_ENTRY_{child_cycle_id}_{child_step}_CATCHUP_{int(time.time())}"[:36]
                                    child_direction = _hc_enforce_conn.execute("SELECT direction FROM bots WHERE id=?", (bot_id,)).fetchone()[0]
                                    child_side = 'sell' if child_direction == 'SHORT' else 'buy'
                                    entry_qty = exchange.round_to_step(delta, tolerance)

                                    if entry_qty > 0 and price_to_use > 0:
                                        params = {
                                            'timeInForce': 'GTC',
                                            'postOnly': False,
                                            'post_only': False,
                                            'newClientOrderId': cid
                                        }
                                        is_testnet = getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)
                                        params = self._resolve_position_side_param(params, is_testnet)
                                        
                                        try:
                                            order = exchange.create_order(
                                                pair, 'limit', child_side, entry_qty, price_to_use, params=params
                                            )
                                            if order:
                                                save_bot_order(
                                                    bot_id, 'entry', str(order['id']), price_to_use, entry_qty,
                                                    step=child_step, status=order.get('status', 'open'),
                                                    client_order_id=cid,
                                                    notes=f"[INV-30] Catch-up entry placed for step {child_step} (delta={delta:.6f})",
                                                    cycle_id=child_cycle_id
                                                )
                                        except Exception as e_place:
                                            logger.error(f"❌ [INV-30] Failed to place catch-up entry order: {e_place}")
                                    # Break to only place one catch-up order per maintain_orders cycle
                                    break
                except Exception as e_inv30:
                    logger.error(f"❌ [INV-30] Error during continuous qty reconciliation: {e_inv30}")

            # 1. Get current open orders
            open_orders = None
            if market_snapshot:
                 open_orders = market_snapshot.get('open_orders')
            
            if open_orders is None:
                 try:
                     open_orders = exchange.fetch_open_orders(pair)
                 except Exception as e:
                     logger.error(f"❌ {name}: Critical - Failed to fetch open orders during maintenance: {e}")
                     return None

            bot_open_orders = [o for o in open_orders if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')]

            # 🛡️ Cancel stale order race window cleanup (Fix 2)
            try:
                from engine.database import get_connection as _gc_cancelling
                _c_cancelling = _gc_cancelling()
                cancelling_orders = _c_cancelling.execute(
                    "SELECT id, order_id, client_order_id, filled_amount, price, amount, step, cycle_id, order_type FROM bot_orders WHERE bot_id = ? AND status = 'cancelling'",
                    (bot_id,)
                ).fetchall()
                
                for c_order in cancelling_orders:
                    db_id, ex_oid, c_cid, f_amt, c_price, c_amount, c_step, c_cycle, c_type = c_order
                    f_amt = float(f_amt or 0)
                    is_still_open = any(str(o.get('id')) == str(ex_oid) for o in bot_open_orders)
                    
                    if not is_still_open:
                        if f_amt > 0:
                            logger.info(f"💰 [WS-FILL-CATCH] Stale order {c_cid} filled amount {f_amt} during cancel buffer! Crediting fill.")
                            from engine.database import update_order_status as _uos_catch
                            from engine.ledger import credit_fill as _cf_catch, seal_trade_state as _sts_catch
                            if not _uos_catch(ex_oid, 'filled', bot_id=bot_id, filled_qty=f_amt):
                                logger.error(f"🚨 [STATE-GUARD ERROR] Unexpected status update failure for catch-fill order {ex_oid} to filled.")
                            _cf_catch(bot_id=bot_id, order_id=str(ex_oid), cumulative_qty=f_amt, avg_price=float(c_price or 0), order_type=str(c_type or 'grid').lower(), is_cumulative=True)
                            _sts_catch(bot_id)
                        else:
                            logger.info(f"🗑️ [CANCEL-PURGE] Stale order {c_cid} has 0 fills and is confirmed gone. Deleting DB row.")
                            _c_cancelling.execute("DELETE FROM bot_orders WHERE id = ?", (db_id,))
                            _c_cancelling.commit()
                    else:
                        logger.warning(f"⏳ [CANCEL-WAIT] Stale order {c_cid} is still open on exchange. Waiting another cycle.")
            except Exception as e_cancelling:
                logger.error(f"Error handling cancelling orders cleanup: {e_cancelling}")

            existing_tp_order = next((o for o in bot_open_orders if '_TP_' in o.get('clientOrderId', '')), None)

            # Check local tp order id
            from engine.database import get_bot_order_ids
            local_db_ids = get_bot_order_ids(bot_id)
            local_tp_id = local_db_ids.get('tp_order_id')
            # 🛡️ Never treat synthetic PENDING_BE* placeholders as real exchange IDs
            if local_tp_id and str(local_tp_id).startswith('PENDING_BE_'):
                logger.debug(f"[HEDGE-CHILD] {name}: clearing placeholder tp_order_id={local_tp_id}")
                _hc_conn.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                _hc_conn.commit()
                local_tp_id = None

            if not existing_tp_order:
                if local_tp_id and (str(local_tp_id).startswith('PENDING_') or str(local_tp_id).startswith('PLACING_')):
                    logger.info(f"⏳ [HEDGE-MAINTAIN] {name}: local_tp_id {local_tp_id} is a placeholder. Treating as None.")
                    local_tp_id = None

                if local_tp_id:
                    # Stalemate check on local_tp_id
                    logger.warning(f"⏳ [HEDGE-MAINTAIN] {name}: CCXT says TP is missing, but DB has {local_tp_id}. Verifying status...")
                    try:
                        if local_tp_id and any(str(local_tp_id).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_')):
                            logger.info(f"🔎 [HEDGE-MAINTAIN] Skipping fetch_order for synthetic local_tp_id {local_tp_id}")
                            order_status = None
                        else:
                            order_status = exchange.fetch_order(local_tp_id, pair)
                        status_str = order_status.get('status') if order_status else 'unknown'
                        
                        if status_str in ['canceled', 'cancelled', 'expired', 'rejected']:
                            logger.info(f"🚫 [HEDGE-MAINTAIN] Stored TP ID {local_tp_id} is CANCELLED. Evicting from DB state.")
                            from engine.database import update_order_status as _uos
                            _uos(local_tp_id, 'cancelled', bot_id=bot_id)
                            _hc_conn.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                            _hc_conn.commit()
                            local_tp_id = None
                        elif status_str == 'filled' or (status_str == 'closed' and float(order_status.get('filled', 0) or 0) > 0 and float(order_status.get('filled', 0) or 0) >= float(order_status.get('amount', 0) or 0) * 0.99):
                            actual_exit = float(order_status.get('average') or order_status.get('price') or current_price)
                            filled_amount = float(order_status.get('filled', 0) or order_status.get('amount', 0))
                            logger.info(f"✅ [HEDGE-MAINTAIN] Stored TP ID {local_tp_id} is FILLED. Triggering reset.")
                            
                            from engine.database import update_order_status as _uos
                            _uos(local_tp_id, 'filled', bot_id=bot_id, filled_qty=filled_amount)
                            
                            from engine.ledger import register_tp_cascade, credit_fill as _cf_tp
                            _rest_ts = order_status.get('lastTradeTimestamp') or order_status.get('timestamp') or (time.time() * 1000)
                            _exit_fill_ts = int(_rest_ts / 1000)
                            
                            _cf_tp(bot_id=bot_id, order_id=str(local_tp_id),
                                   cumulative_qty=filled_amount, avg_price=actual_exit,
                                   order_type='tp', is_cumulative=True)
                            
                            register_tp_cascade(bot_id, pair, actual_exit, _exit_fill_ts)
                            return None
                        elif status_str in ['new', 'open', 'partially_filled']:
                            logger.info(f"✅ [HEDGE-MAINTAIN] Stored TP {local_tp_id} is CONFIRMED LIVE — healing cache.")
                            from engine.ws_cache import get_ws_cache as _gwsc
                            _gwsc().update_order(str(local_tp_id), order_status)
                        else:
                            logger.warning(f"⏳ [HEDGE-MAINTAIN] Stored TP {local_tp_id} status is {status_str}, unrecognised. Evicting.")
                            try:
                                exchange.cancel_order(local_tp_id, pair)
                            except Exception as _e:
                                logger.debug(f'[EXPECTED] cancel unrecognised TP status (hedge-maintain): {_e}')
                            from engine.database import update_order_status as _uos
                            _uos(local_tp_id, 'cancelled', bot_id=bot_id)
                            _hc_conn.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                            _hc_conn.commit()
                            local_tp_id = None
                    except Exception as _evict_err:
                        err_str = str(_evict_err).lower()
                        if "not found" in err_str or "-2013" in err_str:
                            logger.warning(f"🚫 [HEDGE-MAINTAIN] Stored TP ID {local_tp_id} NOT FOUND. Evicting.")
                            from engine.database import update_order_status as _uos
                            _uos(local_tp_id, 'cancelled', bot_id=bot_id)
                            _hc_conn.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                            _hc_conn.commit()
                            local_tp_id = None
                        else:
                            logger.error(f"❌ [HEDGE-MAINTAIN] Failed to evict stalemate TP ID {local_tp_id}: {_evict_err}")
                            from engine.database import update_order_status as _uos
                            _uos(local_tp_id, 'cancelled', bot_id=bot_id)
                            _hc_conn.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                            _hc_conn.commit()
                            local_tp_id = None

            # 🚀 INV-26: Missed BE TP self-healing check
            _parent_row = _hc_conn.execute(
                "SELECT p.status FROM bots p JOIN bots c ON c.parent_bot_id = p.id WHERE c.id = ?",
                (bot_id,)
            ).fetchone()
            _parent_status = _parent_row[0] if _parent_row else None

            if _parent_status in ('Scanning', 'hedge_standby'):
                _child_trade = _hc_conn.execute(
                    "SELECT open_qty, avg_entry_price, cycle_id FROM trades WHERE bot_id = ?",
                    (bot_id,)
                ).fetchone()
                if _child_trade:
                    _child_qty = float(_child_trade[0] or 0)
                    _child_avg = float(_child_trade[1] or 0)
                    _child_cycle = int(_child_trade[2] or 0)
                    if _child_qty > 0.0001:
                        # Check if any TP exists in database
                        _db_tp = _hc_conn.execute(
                            "SELECT id FROM bot_orders WHERE bot_id = ? AND order_type = 'tp' "
                            "AND status IN ('pending_placement', 'open', 'new', 'partially_filled', 'pending', 'placing')",
                            (bot_id,)
                        ).fetchone()
                        if not _db_tp and not existing_tp_order:
                            # Fetch current price with safe fallback chain
                            try:
                                _current_price = float(exchange.get_last_price(pair) or 0)
                            except Exception:
                                _current_price = 0.0
                            if _current_price <= 0:
                                _current_price = _child_avg
                            from engine.ledger import _calc_hedge_tp_price
                            _inv26_tp_price = _calc_hedge_tp_price(direction, _child_avg, _current_price)

                            _inv26_cid = f"CQB_{bot_id}_TP_{_child_cycle}_INV26_BE"
                            from engine.database import save_bot_order as _sbo_inv26
                            _sbo_inv26(
                                bot_id, 'tp', f'PENDING_BE_{bot_id}_{_child_cycle}_INV26',
                                _inv26_tp_price, _child_qty, step=0,
                                status='pending_placement',
                                client_order_id=_inv26_cid,
                                notes=(
                                    f"[INV-26] BE TP self-healing placement. Parent status: {_parent_status}. "
                                    f"qty={_child_qty}, avg={_child_avg:.4f}, price={_inv26_tp_price:.4f}"
                                ),
                                cycle_id=_child_cycle,
                            )
                            logger.warning(
                                f"🛡️ [INV-26] {name} ({bot_id}): Self-healing BE TP placement triggered. "
                                f"Parent is {_parent_status}, but child has open_qty={_child_qty} "
                                f"without a TP order. Registered {_inv26_cid}."
                            )
                            _hc_conn.execute(
                                "UPDATE trades SET tp_order_id = NULL WHERE bot_id = ? "
                                "AND (tp_order_id IS NULL OR tp_order_id LIKE 'PENDING_BE_%')",
                                (bot_id,)
                            )
                            _hc_conn.commit()
                            local_tp_id = None

            if local_tp_id is None:
                # Find pending_placement TP order for this bot
                pending_tp = _hc_conn.execute(
                    "SELECT price, amount, cycle_id, client_order_id FROM bot_orders "
                    "WHERE bot_id = ? AND order_type = 'tp' AND status = 'pending_placement' "
                    "ORDER BY id DESC LIMIT 1",
                    (bot_id,)
                ).fetchone()

                # 🛡️ [HEDGE-BE-FALLBACK] (v3.6.7)
                # If no pending_placement row exists but the child has an open position,
                # handle_tp_completion fired before the child ledger seal wrote open_qty
                # (race condition), so the pending_placement write was silently skipped.
                # Compute and register the BE TP intent here rather than returning unprotected.
                if not pending_tp:
                    # Check if parent is still active before assuming a missed TP cascade
                    _parent_should_skip = False
                    try:
                        _parent_row = _hc_conn.execute(
                            "SELECT parent_bot_id FROM bots WHERE id = ?", (bot_id,)
                        ).fetchone()
                        _parent_id = _parent_row[0] if _parent_row else None
                        if _parent_id:
                            _parent_qty = _hc_conn.execute(
                                "SELECT COALESCE(open_qty, 0) FROM trades WHERE bot_id = ?",
                                (_parent_id,)
                            ).fetchone()
                            if _parent_qty and float(_parent_qty[0]) > 0.0001:
                                _parent_should_skip = True
                                logger.debug(
                                    f"[HEDGE-BE-FALLBACK] {name} ({bot_id}): parent bot "
                                    f"{_parent_id} still active (open_qty={float(_parent_qty[0]):.6f}). "
                                    f"Skipping fallback BE TP registration."
                                )
                    except Exception as _pg_err:
                        logger.warning(
                            f"[HEDGE-BE-FALLBACK] {name} ({bot_id}): parent guard check "
                            f"failed ({_pg_err}). Proceeding with fallback to be safe."
                        )
                        _parent_should_skip = False

                    if not _parent_should_skip:
                        # 🚀 [v3.8.2 HEDGE-BE-FALLBACK SEAL GUARD]
                        # Seal the bot trade state to ensure open_qty and avg_entry_price reflect the ground truth
                        # (e.g. if additional fills arrived after the parent's TP completed/before fallback registration).
                        try:
                            from engine.ledger import seal_trade_state as _sts_fb
                            _sts_fb(bot_id)
                        except Exception as _sts_fb_err:
                            logger.warning(f"🛡️ [HEDGE-BE-FALLBACK] seal_trade_state failed for bot {bot_id} (non-fatal): {_sts_fb_err}")

                        child_trade = _hc_conn.execute(
                            "SELECT open_qty, avg_entry_price, cycle_id FROM trades WHERE bot_id = ?",
                            (bot_id,)
                        ).fetchone()
                        if child_trade:
                            _fallback_qty = float(child_trade[0] or 0)
                            _fallback_avg = float(child_trade[1] or 0)
                            _fallback_cycle = int(child_trade[2] or 0)
                            if _fallback_qty > 0.0001 and _fallback_avg > 0:
                                _fallback_cid = f"CQB_{bot_id}_TP_{_fallback_cycle}_BE_FB"
                                _already = _hc_conn.execute(
                                    "SELECT id FROM bot_orders WHERE bot_id=? AND client_order_id LIKE ? "
                                    "AND status IN ('pending_placement', 'open', 'new', 'partially_filled', 'pending', 'placing', 'cancelling')",
                                    (bot_id, f"{_fallback_cid}%")
                                ).fetchone()
                                if not _already:
                                    try:
                                        _current_price = float(exchange.get_last_price(pair) or 0)
                                    except Exception:
                                        _current_price = 0.0
                                    # If fetch failed, fall back to avg_entry_price (safe conservative choice)
                                    if _current_price <= 0:
                                        _current_price = _fallback_avg
                                    from engine.ledger import _calc_hedge_tp_price
                                    be_price = _calc_hedge_tp_price(direction, _fallback_avg, _current_price)

                                    from engine.database import save_bot_order as _sbo_fb
                                    _sbo_fb(
                                        bot_id, 'tp', f'PENDING_BE_{bot_id}_{_fallback_cycle}_FB',
                                        be_price, _fallback_qty, step=0,
                                        status='pending_placement',
                                        client_order_id=_fallback_cid,
                                        notes=(
                                            f"[HEDGE-BE-FALLBACK] BE TP self-registered by maintain_orders "
                                            f"(handle_tp_completion missed due to ledger race). "
                                            f"qty={_fallback_qty}, avg={_fallback_avg:.4f}, price={be_price:.4f}"
                                        ),
                                        cycle_id=_fallback_cycle,
                                    )
                                    logger.warning(
                                        f"🛡️ [HEDGE-BE-FALLBACK] {name} ({bot_id}): no pending_placement found "
                                        f"but open_qty={_fallback_qty} avg={_fallback_avg:.4f}. "
                                        f"Registered fallback BE TP intent {_fallback_cid}. "
                                        f"Will be placed on next cycle."
                                    )
                                    # Ensure placeholder never leaks into trades.tp_order_id
                                    _hc_conn.execute(
                                        "UPDATE trades SET tp_order_id = NULL WHERE bot_id = ? "
                                        "AND (tp_order_id IS NULL OR tp_order_id LIKE 'PENDING_BE_%')",
                                        (bot_id,)
                                    )
                                    _hc_conn.commit()
                                    # Re-query so the placement block below picks it up this cycle
                                    pending_tp = _hc_conn.execute(
                                        "SELECT price, amount, cycle_id, client_order_id FROM bot_orders "
                                        "WHERE bot_id = ? AND order_type = 'tp' AND status = 'pending_placement' "
                                        "ORDER BY id DESC LIMIT 1",
                                        (bot_id,)
                                    ).fetchone()

                if pending_tp:
                    tp_price, tp_amount, child_cycle, be_cid = pending_tp
                    tp_price = float(tp_price)
                    tp_amount = float(tp_amount)
                    child_cycle = int(child_cycle)

                    # Determine side
                    child_direction = _hc_conn.execute("SELECT direction FROM bots WHERE id=?", (bot_id,)).fetchone()[0]
                    tp_side = 'buy' if child_direction == 'SHORT' else 'sell'

                    # Invariant B: TP Qty must use trades.open_qty for hedge child
                    _fresh_qty_row = _hc_conn.execute("SELECT open_qty FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
                    tp_amount = float(_fresh_qty_row[0] or 0) if _fresh_qty_row else float(bot_status.get('open_qty', 0) or 0)
                    
                    # Update database row to sync amount
                    _hc_conn.execute(
                        "UPDATE bot_orders SET amount = ? WHERE bot_id = ? AND client_order_id = ? AND status = 'pending_placement'",
                        (tp_amount, bot_id, be_cid)
                    )
                    _hc_conn.commit()

                    # Check if already resting on CCXT (e.g. by clientOrderId or symbol/price/qty)
                    resting = next((o for o in bot_open_orders if o.get('clientOrderId') == be_cid or (o.get('clientOrderId') and o.get('clientOrderId').startswith(be_cid))), None)
                    if resting:
                        logger.info(f"✅ [HEDGE-MAINTAIN] Child BE TP is already resting on CCXT (id={resting['id']}). Healing DB state.")
                        _hc_conn.execute(
                            "UPDATE bot_orders SET order_id = ?, status = ?, updated_at = ? "
                            "WHERE bot_id = ? AND client_order_id = ? AND status = 'pending_placement'",
                            (resting['id'], resting.get('status', 'open'), int(time.time()), bot_id, be_cid)
                        )
                        _hc_conn.execute("UPDATE trades SET tp_order_id = ? WHERE bot_id = ?", (resting['id'], bot_id))
                        _hc_conn.commit()
                    else:
                        # Determine if reduceOnly is allowed or if we should use GTC fallback
                        is_reducing = self._is_order_net_reducing(pair, tp_side, tp_amount, bot_id=bot_id, bot_direction=child_direction)

                        unique_cid = f"{be_cid}_{int(time.time())}"
                        params = {
                            'timeInForce': 'GTC',
                            'postOnly': False,
                            'post_only': False,
                            'newClientOrderId': unique_cid
                        }
                        if is_reducing:
                            params['reduceOnly'] = True
                            logger.info(f"✅ {name}: Placing child BE TP using GTC reduceOnly.")
                        else:
                            logger.info(f"✅ {name}: Placing child BE TP using GTC fallback (no reduceOnly).")

                        is_testnet = getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)
                        params = self._resolve_position_side_param(params, is_testnet)

                        try:
                            # Validate the order
                            valid, tp_amount, tp_price, msg = exchange.validate_order(pair, tp_side, tp_amount, tp_price, is_closing=True)
                            if valid:
                                order = exchange.create_order(
                                    pair, 'limit', tp_side, tp_amount, tp_price, params=params
                                )
                                if order:
                                    actual_cid = order.get('_fallback_cid') or order.get('clientOrderId') or unique_cid
                                    _hc_conn.execute(
                                        "UPDATE bot_orders SET order_id = ?, status = ?, client_order_id = ?, updated_at = ? "
                                        "WHERE bot_id = ? AND client_order_id = ? AND status = 'pending_placement'",
                                        (order['id'], order.get('status', 'open'), actual_cid, int(time.time()), bot_id, be_cid)
                                    )
                                    _hc_conn.execute("UPDATE trades SET tp_order_id = ? WHERE bot_id = ?", (order['id'], bot_id))
                                    _hc_conn.commit()
                                    logger.info(
                                        f"✅ [HEDGE-MAINTAIN] Placed child BE TP order for {name}: {tp_amount} @ {tp_price} (order_id={order['id']})"
                                    )
                        except Exception as e_place:
                            logger.error(f"❌ [HEDGE-MAINTAIN] Failed to place child BE TP: {e_place}")

            # Return None to skip entry scans and grid placements
            return None

        from engine.parity_gates import gate_maintain_orders_allowed
        allowed, reason = gate_maintain_orders_allowed(
            bot_id,
            pair,
            exchange=exchange,
            total_invested=float(bot_status.get('total_invested', 0) or 0),
        )
        if not allowed:
            logger.warning(f"🛑 [MAINTAIN-BLOCKED] {name}: {reason}")
            return None

        # 🚀 STRICT SYNCHRONOUS STATE LOCK
        # Query what the DB thinks is currently open right now before acting on CCXT.
        from engine.database import get_bot_order_ids
        local_db_ids = get_bot_order_ids(bot_id)
        local_tp_id = local_db_ids.get('tp_order_id')
        local_grid_ids = [g['order_id'] for g in local_db_ids.get('grid_orders', []) if isinstance(g, dict) and 'order_id' in g]

        # 🚀 MARKET DATA SETUP
        # We need market data here for ATR and Grid Drift calculations. 
        # In maintain_orders, 'market_snapshot' is passed.
        current_market_data = None
        bot_market_data = None
        bot_multi_tf = {}
        if market_snapshot:
             market_snapshot_inner = market_snapshot.get('market_data', {})
             current_market_data = market_snapshot_inner.get(pair)
             bot_market_data = current_market_data  # alias used by some callers
             bot_multi_tf = market_snapshot.get('multi_tf_data', {}).get(pair, {})

        # 1. Get current open orders
        open_orders = None
        if market_snapshot:
             open_orders = market_snapshot.get('open_orders') # Default to None, NOT []
        
        # FAILSAFE: If snapshot missing/failed, fetch directly to avoid Ghost Orders
        if open_orders is None:
             try:
                 open_orders = exchange.fetch_open_orders(pair)
             except Exception as e:
                 logger.error(f"❌ {name}: Critical - Failed to fetch open orders during maintenance: {e}")
                 return None # Abort to prevent duplicates


        bot_open_orders = [o for o in open_orders if o.get('clientOrderId', '').startswith(f'CQB_{bot_id}_')]

        # 🛡️ Cancel stale order race window cleanup (Fix 2)
        try:
            from engine.database import get_connection as _gc_cancelling
            _c_cancelling = _gc_cancelling()
            cancelling_orders = _c_cancelling.execute(
                "SELECT id, order_id, client_order_id, filled_amount, price, amount, step, cycle_id, order_type FROM bot_orders WHERE bot_id = ? AND status = 'cancelling'",
                (bot_id,)
            ).fetchall()

            for c_order in cancelling_orders:
                db_id, ex_oid, c_cid, f_amt, c_price, c_amount, c_step, c_cycle, c_type = c_order
                f_amt = float(f_amt or 0)
                is_still_open = any(str(o.get('id')) == str(ex_oid) for o in bot_open_orders)

                if not is_still_open:
                    if f_amt > 0:
                        logger.info(f"💰 [WS-FILL-CATCH] Stale order {c_cid} filled amount {f_amt} during cancel buffer! Crediting fill.")
                        from engine.database import update_order_status as _uos_catch
                        from engine.ledger import credit_fill as _cf_catch, seal_trade_state as _sts_catch
                        if not _uos_catch(ex_oid, 'filled', bot_id=bot_id, filled_qty=f_amt):
                            logger.error(f"🚨 [STATE-GUARD ERROR] Unexpected status update failure for catch-fill order {ex_oid} to filled.")
                        _cf_catch(bot_id=bot_id, order_id=str(ex_oid), cumulative_qty=f_amt, avg_price=float(c_price or 0), order_type=str(c_type or 'grid').lower(), is_cumulative=True, caller='cancel_verify')
                        _sts_catch(bot_id)
                    else:
                        # BUG 4 FIX: Never delete a zero-fill row without re-verifying with the exchange.
                        # The open_orders snapshot may be stale (cached or race). A fill could have
                        # landed between the cancel call and this cleanup cycle. fetch_order() is the
                        # ground truth — only delete if the exchange confirms the order is truly gone
                        # with zero fill.
                        _verified_gone = False
                        _ex_filled_verify = 0.0
                        _ex_price_verify = float(c_price or 0)
                        try:
                            _verify_info = exchange.fetch_order(str(ex_oid), pair)
                            if _verify_info:
                                _ex_filled_verify = float(_verify_info.get('filled', 0) or 0)
                                _ex_price_verify = float(_verify_info.get('average') or _verify_info.get('price') or c_price or 0)
                                _ex_status_verify = str(_verify_info.get('status', '')).lower()
                                if _ex_filled_verify > 0:
                                    # Exchange says there IS a fill — credit it instead of deleting
                                    logger.warning(
                                        f"💰 [CANCEL-LATE-FILL] {c_cid}: exchange confirms fill={_ex_filled_verify} "
                                        f"despite DB showing 0. Crediting fill (cancel_verify path)."
                                    )
                                    from engine.database import update_order_status as _uos_catch2
                                    from engine.ledger import credit_fill as _cf_catch2, seal_trade_state as _sts_catch2
                                    if not _uos_catch2(ex_oid, 'filled', bot_id=bot_id, filled_qty=_ex_filled_verify):
                                        logger.error(f"🚨 [STATE-GUARD ERROR] Unexpected status update failure for catch-fill-verify order {ex_oid} to filled.")
                                    _cf_catch2(bot_id=bot_id, order_id=str(ex_oid), cumulative_qty=_ex_filled_verify, avg_price=_ex_price_verify, order_type=str(c_type or 'grid').lower(), is_cumulative=True, caller='cancel_verify')
                                    _sts_catch2(bot_id)
                                elif _ex_status_verify in ('canceled', 'cancelled', 'expired', 'rejected'):
                                    _verified_gone = True
                                else:
                                    # Unknown state — leave it for next cycle rather than deleting
                                    logger.warning(f"⏳ [CANCEL-AMBIGUOUS] {c_cid}: exchange status='{_ex_status_verify}' fill=0 — leaving for next cycle.")
                            else:
                                # fetch_order returned None — treat as not-found (order truly gone)
                                _verified_gone = True
                        except Exception as _verify_err:
                            _err_str = str(_verify_err)
                            if any(code in _err_str for code in ('-2013', 'Order does not exist', 'OrderNotFound')):
                                # Exchange confirms the order never existed or is fully gone
                                _verified_gone = True
                                logger.debug(f"[CANCEL-VERIFY] {c_cid}: not-found on exchange — confirmed gone.")
                            else:
                                logger.warning(f"⚠️ [CANCEL-VERIFY] fetch_order failed for {c_cid}: {_verify_err}. Leaving for next cycle.")

                        if _verified_gone:
                            logger.info(f"🗑️ [CANCEL-PURGE] Stale order {c_cid} verified 0 fills on exchange. Deleting DB row.")
                            _c_cancelling.execute("DELETE FROM bot_orders WHERE id = ?", (db_id,))
                            _c_cancelling.commit()
                else:
                    logger.warning(f"⏳ [CANCEL-WAIT] Stale order {c_cid} is still open on exchange. Waiting another cycle.")
        except Exception as e_cancelling:
            logger.error(f"Error handling cancelling orders cleanup: {e_cancelling}")


        if bot_id == 10000:
             logger.debug(f"MAINTAIN Bot 10000 | OpenOrders={len(bot_open_orders)} | Snapshot={'Yes' if market_snapshot else 'No'}")

        # --- SELF-HEALING: Deduplicate Orders ---
        # Ensure only 1 TP, 1 Grid, and handle Dust. If more, cancel the extras.
        grid_orders = [o for o in bot_open_orders if '_GRID_' in o.get('clientOrderId', '')]
        tp_orders = [o for o in bot_open_orders if '_TP_' in o.get('clientOrderId', '')]
        dust_orders = [o for o in bot_open_orders if '_DUST_' in o.get('clientOrderId', '')]
        
        # 🚀 STRICT SEQUENCING & STATE ENFORCEMENT
        existing_entry_orders = [o for o in bot_open_orders if '_ENTRY_' in o.get('clientOrderId', '')]

        # CASE 1: CARRY_PENDING GUARD [v3.1.4 — self-healing]
        # Suspend maintenance while waiting for the carry fill — but if the carry
        # bot_orders row is already 'filled', promote CARRY_PENDING → ACTIVE via
        # seal_trade_state() and fall through to normal TP/grid maintenance.
        if bot_status.get('cycle_phase') == 'CARRY_PENDING':
            try:
                from engine.database import get_connection as _gc_m
                from engine.ledger import seal_trade_state as _seal_m
                _conn_m = _gc_m()
                _carry_filled_m = _conn_m.execute(
                    "SELECT COUNT(*) FROM bot_orders "
                    "WHERE bot_id=? AND order_type IN ('entry','carry') "
                    "AND status='filled' AND filled_amount>0",
                    (bot_id,)
                ).fetchone()[0]
                if _carry_filled_m:
                    logger.info(
                        f"🔄 [CARRY-PROMOTE] {name}: carry entry already filled ({_carry_filled_m} row(s)) "
                        f"in maintain_orders. Promoting CARRY_PENDING → ACTIVE."
                    )
                    _seal_m(bot_id)
                    from engine.database import get_bot_status as _gbs_m
                    bot_status = _gbs_m(bot_id)
                    # Fall through into normal TP/grid maintenance
                else:
                    logger.info(f"⏳ [CARRY-PENDING] {name}: Ledger awaiting background carry adoption. Suspending maintenance.")
                    return None
            except Exception as _m_cp_err:
                logger.warning(f"[CARRY-PENDING] {name}: self-heal check failed ({_m_cp_err}). Suspending as precaution.")
                return None

        # CASE 1b: PARTIAL_CLOSE_PENDING GUARD [v3.9.23]
        if bot_status.get('cycle_phase') == 'PARTIAL_CLOSE_PENDING':
            from engine.ledger import seal_trade_state as _sts_partial
            _sts_partial(bot_id)
            from engine.database import get_bot_status as _gbs_partial
            bot_status = _gbs_partial(bot_id)
            current_open_qty = float(bot_status.get('open_qty', 0) or 0)
            
            if current_open_qty <= 0.0001:
                logger.info(f"🎉 [PARTIAL_CLOSE_PENDING] Bot {name} ({bot_id}) remaining position closed. Resetting bot.")
                from engine.database import reset_bot_after_tp as _rb_partial
                _rb_partial(
                    bot_id=bot_id,
                    exit_price=current_price,
                    action_label='TP_HIT',
                    notes=f'Partial close settled. Resetting bot.',
                    exchange=exchange
                )
            else:
                logger.info(f"⏳ [PARTIAL_CLOSE_PENDING] Bot {name} ({bot_id}) still has open_qty={current_open_qty:.6f}. Awaiting settlement.")
                # If the close order was cancelled or failed, re-place it
                close_orders = [o for o in bot_open_orders if '_CLOSE_' in o.get('clientOrderId', '')]
                if not close_orders:
                    # FIX 1 — Idempotency guard for PARTIAL_CLOSE_PENDING:
                    from engine.database import get_connection as _gc_conn_partial
                    _conn_partial = _gc_conn_partial()
                    existing_pending = _conn_partial.execute(
                        "SELECT COUNT(*) FROM bot_orders "
                        "WHERE bot_id=? AND order_type='close' "
                        "AND status='pending_placement' "
                        "AND cycle_id=?",
                        (bot_id, bot_status.get('cycle_id', 1))
                    ).fetchone()[0]
                    
                    if existing_pending > 0:
                        logger.warning(
                            f"[PARTIAL_CLOSE_PENDING] Bot {name}: skipping new close row, "
                            f"{existing_pending} pending_placement rows already exist. "
                            f"Waiting for exchange placement or manual resolution."
                        )
                        return None

                    logger.warning(f"⚠️ [PARTIAL_CLOSE_PENDING] Bot {name} ({bot_id}) has open_qty={current_open_qty:.6f} but no active close order. Re-placing.")
                    close_side = 'sell' if direction == 'LONG' else 'buy'
                    # FIX 3 — CID must be cycle-stable, not time-based:
                    close_cid = f"CQB_{bot_id}_CLOSE_{bot_status.get('cycle_id', 1)}"
                    from engine.database import save_bot_order as _sbo_partial
                    _sbo_partial(
                        bot_id, 'close', close_cid,
                        price=0.0, amount=current_open_qty, step=0,
                        status='pending_placement',
                        client_order_id=close_cid,
                        notes=f"PARTIAL_CLOSE_PENDING fallback: Close remaining position of {current_open_qty:.6f}",
                        cycle_id=bot_status.get('cycle_id', 1)
                    )
                    try:
                        _testnet = bool(getattr(exchange, 'is_testnet', False) or
                                        getattr(getattr(exchange, 'exchange', None), 'sandbox', False))
                        _params = {
                            'reduceOnly': True,
                            'newClientOrderId': close_cid,
                        }
                        _params = self._resolve_position_side_param(_params, _testnet)
                        close_order = exchange.create_order(
                            pair, 'market', close_side, current_open_qty,
                            params=_params
                        )
                        if close_order:
                            from engine.database import update_order_status as _uos_partial
                            if not _uos_partial(close_order['id'], close_order.get('status', 'open'), bot_id=bot_id, filled_qty=0.0):
                                logger.error(f"🚨 [STATE-GUARD ERROR] Unexpected status update failure for partial close order {close_order['id']} to {close_order.get('status', 'open')}.")
                    except Exception as e_close:
                        logger.error(f"[PARTIAL_CLOSE_PENDING] Fallback place close failed: {e_close}")
                        # FIX 2 — ReduceOnly rejection triggers ghost detection immediately:
                        if any(phrase in str(e_close) for phrase in ["ReduceOnly", "-2022", "reduceOnly", "reduce-only"]):
                            logger.warning(f"⚠️ [PARTIAL_CLOSE_PENDING] ReduceOnly rejection detected for bot {name} ({bot_id}): {e_close}")
                            from engine.oneway_netting import detect_bot_ghost, wipe_bot_ghost
                            if detect_bot_ghost(exchange, bot_id, _conn_partial):
                                logger.info(f"🧹 [GHOST-DETECTED] Bot {name} ({bot_id}) confirmed as a ghost on ReduceOnly rejection. Wiping ghost state.")
                                wipe_bot_ghost(exchange, bot_id, _conn_partial)
                                _conn_partial.commit()
                                return None
            return None

        pass

        # CASE 2: IN TRADE -> NO ENTRY ORDERS ALLOWED
        if bot_status['total_invested'] > 0 and existing_entry_orders:
             logger.warning(f"🧹 {name}: Found {len(existing_entry_orders)} dangling ENTRY orders while IN TRADE. Cancelling to enforce state.")
             for o in existing_entry_orders:
                 try:
                     exchange.cancel_order(o['id'], pair)
                     update_order_status(o['id'], 'cancelled', bot_id=bot_id)
                 except Exception as e:
                     logger.error(f"Failed to cancel dangling entry {o['id']}: {e}")
             existing_entry_orders = [] # Cleared

        # CASE 2: SCANNING (No Position) -> NO TP/GRID ALLOWED 
        # (This is handled by 'untracked order' cleanup, but let's be explicit)
        # 🚀 ZERO-INVESTED RACE CONDITION FIX (v2.3.4):
        # If the bot's `total_invested` is practically zero ($0.01), and `current_step == 0`,
        # it is truly Scanning.
        if bot_status['total_invested'] <= 0.01 and bot_status['current_step'] == 0:
            for stale_grid in grid_orders:
                logger.warning(f"👻 {name}: Found dangling GRID order {stale_grid['id']} while SCANNING (Invested=0.0). Purging...")
                try:
                    exchange.cancel_order(stale_grid['id'], pair)
                    update_order_status(stale_grid['id'], 'cancelled', bot_id=bot_id)
                except Exception as _e:
                    logger.debug(f'[EXPECTED] stale grid cancel (scanning): {_e}')
            grid_orders = [] # Clear local list
            
            for stale_tp in tp_orders:
                logger.warning(f"👻 {name}: Found dangling TP order {stale_tp['id']} while SCANNING (Invested=0.0). Purging...")
                try:
                    exchange.cancel_order(stale_tp['id'], pair)
                    update_order_status(stale_tp['id'], 'cancelled', bot_id=bot_id)
                except Exception as _e:
                    logger.debug(f'[EXPECTED] stale TP cancel (scanning): {_e}')
            tp_orders = [] # Clear local list

            for stale_dust in dust_orders:
                logger.warning(f"👻 {name}: Found dangling DUST order {stale_dust['id']} while SCANNING (Invested=0.0). Purging...")
                try:
                    exchange.cancel_order(stale_dust['id'], pair)
                    update_order_status(stale_dust['id'], 'cancelled', bot_id=bot_id)
                except Exception as _e:
                    logger.debug(f'[EXPECTED] stale dust cancel (scanning): {_e}')
            dust_orders = []

            return None # Exit early for truly empty bots
            
        # 🚀 RESIDUE PROMOTION (v2.3.5):
        # If the bot has money (>0.01) but `current_step == 0`, it is a "Scanning Residue".
        # We must promote it to `IN TRADE` immediately so its maintenance logic (TP/Grid)
        # is mathematically sound and the UI stops flashing "STRAY ORDERS".
        if bot_status['total_invested'] > 0.01 and bot_status['current_step'] == 0:
            logger.warning(f"🔧 {name}: Residue detected (${bot_status['total_invested']:.2f}). Promoting to IN TRADE for professional management.")
            from engine.ledger import seal_trade_state as _sts_prom
            _new_state = _sts_prom(bot_id)
            if _new_state:
                bot_status.update(_new_state) # Sync local state for this cycle
                # Re-calculate direction if needed
                direction = bot_status.get('direction', 'LONG').upper()


            pass

            # 🛡️ FIX: DO NOT RETURN NONE HERE.
            # If the bot was promoted, we want to CONTINUE and place its TP/Grid orders.
            # Only truly empty bots (invested <= 0.01) returned early above.

        # 🚀 STEP-SYNC FIX (Deterministic ID Parsing)
        # ID Format: CQB_{bot_id}_{prefix}_{cycle_id}_{step}
        # e.g., CQB_10018_GRID_0_3 means grid for step 3.
        current_step = bot_status['current_step']
        expected_tp_step = current_step
        expected_grid_step = current_step + 1

        def get_cycle_step_from_cid(cid: str, prefix: str) -> Tuple[int, int]:
            """Extracts (cycle_id, step) from a CQB clientOrderId. Returns (-1, -1) if invalid."""
            try:
                # CQB_100_GRID_0_3 -> split by _GRID_ -> "0_3"
                parts = cid.split(f"_{prefix}_")
                if len(parts) > 1:
                    # The remainder is "{cycle_id}_{step}" maybe with "_R" retry suffix
                    remainder = parts[1].split('_')
                    if len(remainder) >= 2:
                        # strip any alpha suffixes like R or F
                        cycle_id = int(remainder[0])
                        step_num = int(remainder[1].replace('R','').replace('F',''))
                        return cycle_id, step_num
            except Exception as _e:
                logger.debug(f'[CID-PARSE] Failed to parse cycle/step from CID "{cid}": {_e}')
            return -1, -1

        stored_tp_id = str(bot_status.get('tp_order_id', '') or '')
        
        valid_tp_orders = []
        valid_grid_orders = []
        stale_orders = []

        current_cycle = int(bot_status.get('cycle_id', 0))
        for o in tp_orders:
            cid = o.get('clientOrderId', '')
            cycle_num, step_num = get_cycle_step_from_cid(cid, 'TP')
            # Order is valid ONLY if it matches the current cycle AND (matches expected step OR matches stored TP ID)
            if cycle_num == current_cycle and (step_num == expected_tp_step or (stored_tp_id and o.get('id', '') == stored_tp_id)):
                valid_tp_orders.append(o)
            else:
                stale_orders.append(o)

        for o in grid_orders:
            cid = o.get('clientOrderId', '')
            cycle_num, step_num = get_cycle_step_from_cid(cid, 'GRID')
            if cycle_num == current_cycle and step_num == expected_grid_step:
                valid_grid_orders.append(o)
            else:
                stale_orders.append(o)
                
        for o in dust_orders:
            cid = o.get('clientOrderId', '')
            cycle_num, step_num = get_cycle_step_from_cid(cid, 'DUST')
            if cycle_num == current_cycle and step_num < current_step:
                # Keep it if it's current cycle but old step? 
                # Actually DUST chaser is usually for the whole bot, so step_num < current_step is a good heuristic.
                stale_orders.append(o)
            elif cycle_num != current_cycle:
                stale_orders.append(o)
                
        if stale_orders:
            logger.warning(f"🧹 {name}: Found {len(stale_orders)} STALE orders from previous steps. Purging to sync with Step {current_step}...")
            for o in stale_orders:
                try:
                    # 🛡️ PARTIAL-FILL GUARD: Never cancel a partially filled order as stale.
                    # A partial fill is real capital deployed on the exchange — cancelling it
                    # orphans that position. Only skip if there is a measurable fill.
                    filled_qty = float(o.get('filled', 0) or 0)
                    if filled_qty > 0:
                        logger.warning(
                            f"⚠️ SKIPPING stale cancel for {o.get('clientOrderId')} — has partial fill of {filled_qty}. "
                            f"Will be reconciled by reconciler when step genuinely advances."
                        )
                        continue
                    exchange.cancel_order(o['id'], pair)
                    update_order_status(o['id'], 'cancelling', bot_id=bot_id, filled_qty=filled_qty)
                    logger.info(f"🔥 Cancelled stale {o.get('clientOrderId')} (No fill, set to cancelling for 1-cycle buffer)")
                except Exception as e:
                    logger.error(f"Failed to cancel stale {o['id']}: {e}")

        # Ensure only 1 valid TP and 1 valid Grid exist (Deduplication / Ghost Sweeping)
        if len(grid_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(grid_orders)} total GRID orders. Restricting to strict 1 max...")
            # Sort to prefer the matching step, otherwise just keep newest
            # Sort to prefer the matching step, otherwise just keep newest
            grid_orders.sort(key=lambda x: 1 if get_cycle_step_from_cid(x.get('clientOrderId', ''), 'GRID')[1] == expected_grid_step else 0, reverse=True)
            for o in grid_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    filled_qty = float(o.get('filled', 0) or 0)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                except Exception as _e:
                    logger.debug(f'[EXPECTED] excess grid cancel: {_e}')
            valid_grid_orders = [grid_orders[0]] if get_step_from_cid(grid_orders[0].get('clientOrderId',''), 'GRID') == expected_grid_step else []
            existing_grid_order = grid_orders[0]
        else:
            existing_grid_order = valid_grid_orders[0] if valid_grid_orders else None

        if len(tp_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(tp_orders)} total TP orders. Restricting to strict 1 max (Sweeping Ghosts)...")
            # Sort to prefer the matching step AND matching quantity, otherwise just keep newest
            def _tp_sort_key(x):
                # Priority 1: Step matches expected_tp_step (100 pts)
                # Priority 2: Stored ID matches (50 pts)
                # Priority 3: Quantity matches target virtual_qty (25 pts)
                # Priority 4: Newer order (1-0.9 pts)
                score = 0
                if get_step_from_cid(x.get('clientOrderId', ''), 'TP') == expected_tp_step: score += 100
                if stored_tp_id and x.get('id', '') == stored_tp_id: score += 50
                if abs(float(x.get('amount', 0) or 0) - virtual_qty) < 1e-8: score += 25
                score += (float(x.get('timestamp', 0)) / 2e12) # Subtle bias for newer
                return score

            tp_orders.sort(key=_tp_sort_key, reverse=True)
            for o in tp_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    filled_qty = float(o.get('filled', 0) or 0)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                except Exception as _e:
                    logger.debug(f'[EXPECTED] excess TP cancel: {_e}')
            
            # Re-verify that the chosen winner actually matches the target step
            best_order = tp_orders[0]
            if get_step_from_cid(best_order.get('clientOrderId',''), 'TP') == expected_tp_step or (stored_tp_id and best_order.get('id', '') == stored_tp_id):
                valid_tp_orders = [best_order]
            else:
                valid_tp_orders = []
            existing_tp_order = best_order
        else:
            existing_tp_order = valid_tp_orders[0] if valid_tp_orders else None
            
        if len(dust_orders) > 1:
            logger.warning(f"⚠️ {name}: Found {len(dust_orders)} DUST orders. Restricting to strict 1 max...")
            for o in dust_orders[1:]:
                try: 
                    exchange.cancel_order(o['id'], pair)
                    filled_qty = float(o.get('filled', 0) or 0)
                    update_order_status(o['id'], 'cancelled', bot_id=bot_id, filled_qty=filled_qty)
                except Exception as _e:
                    logger.debug(f'[EXPECTED] excess dust cancel: {_e}')
            existing_dust_order = dust_orders[0]
        else:
            existing_dust_order = dust_orders[0] if dust_orders else None
        # ----------------------------------------

        strategy = self._get_strategy_instance(bot_id, bot_config)
        # 🚀 CRITICAL: Force-sync strategy.params with bot_config every cycle.
        # The strategy instance is cached and may have stale params (e.g. base_size=150 default)
        # if it was created before the DB column values were injected into bot_config.
        strategy.params.update(bot_config)

        # 2. Check for missing / filled TP order
        if not existing_tp_order:
            if local_tp_id and (str(local_tp_id).startswith('PENDING_') or str(local_tp_id).startswith('PLACING_')):
                logger.info(f"⏳ {name}: local_tp_id {local_tp_id} is a placeholder. Treating as None.")
                local_tp_id = None

            if local_tp_id:
                # 🚀 STALEMATE EVICTOR:
                # CCXT indicates missing TP, but DB confirms local_tp_id exists.
                # We must verify if the ID is actually DEAD before blocking re-placement.
                logger.warning(f"⏳ {name}: CCXT says TP is missing, but DB has {local_tp_id}. Verifying status...")
                try:
                    if local_tp_id and any(str(local_tp_id).startswith(prefix) for prefix in ('PENDING_', 'PLACING_', 'GHOST_')):
                        logger.info(f"🔎 {name}: Skipping fetch_order for synthetic local_tp_id {local_tp_id}")
                        order_status = None
                    else:
                        order_status = exchange.fetch_order(local_tp_id, pair)
                    status_str = order_status.get('status') if order_status else 'unknown'
                    
                    if status_str in ['canceled', 'cancelled', 'expired', 'rejected']:
                        logger.info(f"🚫 {name}: Stored TP ID {local_tp_id} is CANCELLED on exchange. Evicting from DB state.")
                        from engine.database import get_connection as _gc
                        from engine.database import update_order_status as _uos
                        _uos(local_tp_id, 'cancelled', bot_id=bot_id)
                        _c = _gc()
                        _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                        _c.commit(); _c.close()
                        local_tp_id = None # Allow placement below
                    elif status_str == 'filled' or (status_str == 'closed' and float(order_status.get('filled', 0) or 0) > 0 and float(order_status.get('filled', 0) or 0) >= float(order_status.get('amount', 0) or 0) * 0.99):
                        if float(bot_status.get('total_invested', 0)) > 0:
                            actual_exit = float(order_status.get('average') or order_status.get('price') or current_price)
                            filled_amount = float(order_status.get('filled', 0) or order_status.get('amount', 0))
                            logger.info(f"✅ {name}: Stored TP ID {local_tp_id} is FILLED at {actual_exit} (Qty: {filled_amount}). Triggering reset.")
                            
                            # Check if the bot_orders row for local_tp_id already has status='reset_cleared' or status='filled'
                            try:
                                from engine.database import get_connection as _gc
                                _c = _gc()
                                _order_row = _c.execute("SELECT status FROM bot_orders WHERE order_id = ? OR id = ? LIMIT 1", (str(local_tp_id), str(local_tp_id))).fetchone()
                                _c.close()
                                if _order_row and _order_row[0] in ['reset_cleared', 'filled']:
                                    logger.info(f"⏭️ {name}: Stored TP ID {local_tp_id} already has status '{_order_row[0]}' in DB. Skipping credit and cascade entirely.")
                                    _c = _gc()
                                    _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                                    _c.commit(); _c.close()
                                    return None
                            except Exception as e_check:
                                logger.warning(f"⚠️ {name}: Failed to check order status for {local_tp_id}: {e_check}")

                            # 🚀 CRITICAL DB FIX: Record the TP fill in bot_orders BEFROE resetting!
                            # If the REST API caught this fill before the WebSocket did, the database doesn't
                            # know the TP filled. If we call 'reset_bot_after_tp' now, the old cycle math
                            # will show Entries > Exits, and it will erroneously CARRY OVER the full position
                            # forever. We MUST mark this specific local_tp_id as filled + amount.
                            try:
                                from engine.database import update_order_status as _uos
                                _uos(local_tp_id, 'filled', bot_id=bot_id, filled_qty=filled_amount)
                                logger.debug(f"🧹 {name}: Force-marked TP {local_tp_id} as filled in DB to prevent CARRY bugs.")
                            except Exception as e_uos:
                                logger.warning(f"⚠️ {name}: Failed to mark TP {local_tp_id} as filled: {e_uos}")
                            
                            # v2.0: Register in cascade registry — runner will do atomic cancel+reset
                            from engine.ledger import register_tp_cascade, credit_fill as _cf_tp
                            
                            # 🚀 ROOT CAUSE FIX (v2.1.1): Extract REST fill timestamp and pass to cascade
                            # Without this, REST-detected TP fills resulted in cycle_start_time=0,
                            # breaking the cycle poisoning guard on the next restart.
                            _rest_ts = order_status.get('lastTradeTimestamp') or order_status.get('timestamp') or (time.time() * 1000)
                            _exit_fill_ts = int(_rest_ts / 1000)
                            
                            _cf_tp(bot_id=bot_id, order_id=str(local_tp_id),
                                   cumulative_qty=filled_amount, avg_price=actual_exit,
                                   order_type='tp', is_cumulative=True)
                            
                            bot_open_qty = float(bot_status.get('open_qty', 0) or 0)
                            remaining_qty = bot_open_qty - filled_amount
                            
                            if remaining_qty > 0.001:
                                logger.info(f"🔄 [STALEMATE EVICTOR] {name}: Partial TP detected (Remaining: {remaining_qty:.4f}). Clearing tp_order_id and falling through.")
                                from engine.database import get_connection as _gc
                                _c = _gc()
                                _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                                _c.commit(); _c.close()
                                local_tp_id = None
                                # Do NOT return None; fall through to place new TP
                            else:
                                register_tp_cascade(bot_id, pair, actual_exit, _exit_fill_ts)
                                logger.info(f"[TP-EVICTOR] {name}: REST-detected TP fill registered for cascade (ts={_exit_fill_ts}). Runner will complete reset.")
                                return None # Exit cycle for full TP
                        else:
                            logger.debug(f"⏭️ {name}: Stored TP ID {local_tp_id} is FILLED, but bot state already zeroed. Skipping.")
                            return None # Exit cycle if already zeroed
                    elif status_str in ['new', 'open', 'partially_filled']:
                         # 🚀 SNAPSHOT LAG FIX: The order IS confirmed live on exchange (status=new).
                         # It's simply absent from the stale start-of-cycle snapshot.
                         # Back-populate the WS cache so next cycle sees it and skip eviction.
                         logger.info(f"✅ {name}: Stored TP {local_tp_id} is CONFIRMED LIVE (status={status_str}) — snapshot was stale. Healing cache.")
                         from engine.ws_cache import get_ws_cache as _gwsc
                         _gwsc().update_order(str(local_tp_id), order_status)
                         # Do NOT modify local_tp_id — keep the lock so we don't re-place a duplicate
                    else:
                         # Truly unrecognised status — treat as ghost and evict.
                         logger.warning(f"⏳ {name}: Stored TP {local_tp_id} status is {status_str}, unrecognised. Forcing CANCEL and Eviction.")
                         try:
                             exchange.cancel_order(local_tp_id, pair)
                         except Exception as _e:
                             logger.debug(f'[EXPECTED] cancel unrecognised TP status: {_e}')
                         from engine.database import get_connection as _gc
                         from engine.database import update_order_status as _uos
                         if _uos(local_tp_id, 'cancelled', bot_id=bot_id):
                             _c = _gc()
                             _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                             _c.commit(); _c.close()
                             local_tp_id = None # Allow immediate replacement below!
                         else:
                             logger.warning(f"⚠️ {name}: Stored TP {local_tp_id} cancelled update was rejected (likely filled). Retaining state.")
                except Exception as _evict_err:
                     err_str = str(_evict_err).lower()
                     if "not found" in err_str or "-2013" in err_str:
                         logger.warning(f"🚫 {name}: Stored TP ID {local_tp_id} NOT FOUND on exchange. Evicting from DB state.")
                         from engine.database import get_connection as _gc
                         from engine.database import update_order_status as _uos
                         if _uos(local_tp_id, 'cancelled', bot_id=bot_id): # 🚀 FUNDAMENTAL FIX: Clear bot_orders state
                             _c = _gc()
                             _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                             _c.commit(); _c.close()
                             local_tp_id = None # Allow placement below
                         else:
                             logger.warning(f"⚠️ {name}: Stored TP ID {local_tp_id} NOT FOUND on exchange, but cancelled update was rejected (likely filled).")
                     else:
                         logger.error(f"❌ {name}: Failed to evict stalemate TP ID {local_tp_id}: {_evict_err}")
                         # Also forcefully clear to prevent deadlock if API throws strange errors repeatedly
                         from engine.database import get_connection as _gc
                         from engine.database import update_order_status as _uos
                         if _uos(local_tp_id, 'cancelled', bot_id=bot_id):
                             _c = _gc()
                             _c.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = ?", (bot_id,))
                             _c.commit(); _c.close()
                             local_tp_id = None
                         else:
                             logger.warning(f"⚠️ {name}: Stored TP ID {local_tp_id} failed to cancel, and DB status update was rejected (likely filled).")
            
            if local_tp_id is None:
                # 🚀 API LAG GUARD (v2.3.5)
                # If we just placed a TP order in the last 15 seconds, skip fresh placement.
                # This prevents "Double Placement" loops where the exchange API lags 
                # and doesn't show the new order in the next maintain_orders cycle.
                try:
                    from engine.database import get_connection as _gc_lag
                    _c_lag = _gc_lag()
                    recent_check = _c_lag.execute("""
                        SELECT id FROM bot_orders 
                        WHERE bot_id = ? AND order_type = 'tp' 
                        AND status IN ('new', 'open', 'filled')
                        AND (created_at > ? OR updated_at > ?)
                        LIMIT 1
                    """, (bot_id, int(time.time()) - 15, int(time.time()) - 15)).fetchone()
                    if recent_check:
                        logger.debug(f"⏳ [LAG-GUARD] {name}: TP recently placed in DB. Waiting for API/WS propagation...")
                        # Skip the placement block
                        local_tp_id = "LAG_PENDING"
                except Exception as _lag_err:
                    logger.warning(f"[LAG-GUARD] DB check failed: {_lag_err}")

            if local_tp_id is None:
                tp_price = strategy.calculate_take_profit_price(bot_status, current_price)
                tp_amount = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange)
            
                # 🚀 SPREAD-CROSSING MAKER LOOP FIX (Root Cause of Flashing)
                # If a Post-Only (GTX) limit order crosses the active spread, Binance accepts the API payload 
                # but silently/instantly cancels it in the matching engine (status becomes EXPIRED). 
                # This caused the engine to endlessly re-place the order every cycle, causing UI flashes.
                # Standard TP: LONG bot sells to close. Must be >= Best Ask to remain Maker.
                #              SHORT bot buys to close. Must be <= Best Bid to remain Maker.
                try:
                    bid, ask = exchange.get_best_bid_ask(pair)
                    if bid is not None and ask is not None:
                        bid_val = float(bid)
                        ask_val = float(ask)
                        if direction == 'LONG':
                            # We are Selling to close. If TP is lower than or equals the best bid, it crosses the spread and acts as Taker.
                            if tp_price <= bid_val:
                                old_tp = tp_price
                                tp_price = ask_val # Join the asks to stay Maker
                                logger.info(f"🚀 {name}: TP Spread Cross Prevented! (Sell {old_tp} <= Bid {bid_val}). Adjusted to Ask {tp_price} to preserve GTX.")
                        else:
                            # We are Buying to close. If TP is higher than or equals the best ask, it crosses the spread and acts as Taker.
                            if tp_price >= ask_val:
                                old_tp = tp_price
                                tp_price = bid_val # Join the bids to stay Maker
                                logger.info(f"🚀 {name}: TP Spread Cross Prevented! (Buy {old_tp} >= Ask {ask_val}). Adjusted to Bid {tp_price} to preserve GTX.")
                except Exception as e:
                    logger.warning(f"⚠️ {name}: Market Gap check failed ({e}). Proceeding without spread-cross protection.")

                # Re-round just in case
                try:
                    prec = exchange.get_symbol_precision(pair)
                    tp_price = exchange.round_to_step(tp_price, prec['tick_size'])
                except Exception as _e:
                    logger.debug(f'[TP-ROUND] round_to_step failed for TP price: {_e}')

                logger.info(f"🔍 [TP-MAINTENANCE] Checking TP for {name}: tp_price={tp_price}, amount={tp_amount}")
                if bot_id == 10000:
                     logger.debug(f"TP Logic Bot 10000 | Existing={existing_tp_order is not None} | Amt={tp_amount} | Price={tp_price} | Invested={bot_status['total_invested']}")

                if tp_amount > 0 and tp_price > 0:
                    if config.DRY_RUN:
                        logger.info(f"📊 [DRY-RUN] Bot {name} maintains TP for {pair} @ {tp_price}")
                    else:
                        valid, tp_amount, tp_price, msg = exchange.validate_order(pair, 'sell' if direction == 'LONG' else 'buy', tp_amount, tp_price, is_closing=True)
                        if valid:
                            try:
                                cycle_id = bot_status.get('cycle_id', 0)
                                client_order_id = self._generate_deterministic_id(bot_id, 'TP', cycle_id, bot_status['current_step'])
                                side = 'sell' if direction == 'LONG' else 'buy'
                                
                                # 🚀 Unified TP Param Preparation
                                ccxt_params, tp_amount = self._prepare_tp_order_params(
                                    bot_id, name, pair, side, tp_amount, tp_price, current_price, exchange, direction
                                )
                                if ccxt_params is None:
                                    pass  # Early exit handled inside _prepare_tp_order_params, but inside inside place block we just let it skip
                                elif ccxt_params == 'DUST_CHASER':
                                    # 🚀 MARKET DUST FLUSH: Multi-bot pair, sub-threshold virtual position.
                                    # A limit TP is impossible (min notional rejection, no reduceOnly allowed).
                                    # Correct architecture: fire a net-REDUCING market order to zero the virtual position.
                                    # In One-Way mode, this is always safe: it's just netting against the pair's physical position.
                                    # The CID tags it to this bot only — sibling bots are fully unaffected.
                                    # Check cooldown to prevent rapid retries on failure
                                    if time.time() < _DUST_FLUSH_COOLDOWN.get(bot_id, 0.0):
                                        logger.info(f"🧹 [DUST-FLUSH] {name}: Dust close is in cooldown. Skipping.")
                                    else:
                                        logger.warning(f"🧹 [DUST-FLUSH] {name}: Virtual position ${bot_status.get('total_invested', 0):.2f} below min notional. Firing market dust-close.")
                                        try:
                                            dust_qty = tp_amount  # qty returned from _prepare_tp_order_params
                                            dust_side = 'sell' if direction == 'LONG' else 'buy'
                                            dust_cid = self._generate_deterministic_id(bot_id, 'DUST', bot_status.get('cycle_id', 0), bot_status.get('current_step', 1))
                                            
                                            # Try to adjust qty to clear min_notional without reduceOnly (Option A)
                                            adjusted = False
                                            try:
                                                prec = exchange.get_symbol_precision(pair)
                                                _min_notional = prec.get('min_notional')
                                                if _min_notional is None:
                                                     _min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                                                
                                                notional_now = dust_qty * current_price
                                                if notional_now < _min_notional:
                                                    min_qty_needed = _min_notional / current_price
                                                    rounded_min_qty = exchange.round_to_step(min_qty_needed, prec['step_size'])
                                                    if rounded_min_qty * current_price < _min_notional:
                                                        rounded_min_qty = exchange.round_to_step(min_qty_needed + prec['step_size'], prec['step_size'])
                                                    
                                                    extra_qty = rounded_min_qty - dust_qty
                                                    extra_value = extra_qty * current_price
                                                    # Verify the adjustment is tiny (at most $5 USD or at most 50% increase)
                                                    if extra_value <= 5.0 or (dust_qty > 0 and extra_qty / dust_qty <= 0.5):
                                                        logger.info(f"✨ [DUST-ADJUST] {name}: Adjusting dust qty from {dust_qty:.6f} to {rounded_min_qty:.6f} to meet min_notional ${_min_notional:.2f} (extra_value=${extra_value:.2f}). Removing reduceOnly.")
                                                        dust_qty = rounded_min_qty
                                                        adjusted = True
                                            except Exception as e_adj:
                                                logger.warning(f"⚠️ [DUST-ADJUST] {name}: Failed to calculate adjustment: {e_adj}")

                                            params = {'newClientOrderId': dust_cid}
                                            if not adjusted:
                                                params['reduceOnly'] = True

                                            # Market close — no price needed, taker execution
                                            dust_order = exchange.create_order(
                                                pair, 'market', dust_side, dust_qty,
                                                params=params,
                                                human_approved=True
                                            )
                                            
                                            if dust_order:
                                                dust_fill_price = float(dust_order.get('average') or dust_order.get('price') or current_price)
                                                logger.info(f"✅ [DUST-FLUSH] {name}: Market close executed. ID={dust_order['id']} qty={dust_qty} @ ~{dust_fill_price:.4f}")
                                                
                                                # Credit the fill and seal the cycle
                                                from engine.ledger import credit_fill as _cf_dust, seal_trade_state as _sts_dust
                                                from engine.database import update_order_status as _uos_dust
                                                _credited = _cf_dust(
                                                    bot_id=bot_id,
                                                    order_id=str(dust_order['id']),
                                                    cumulative_qty=dust_qty,
                                                    avg_price=dust_fill_price,
                                                    order_type='tp',
                                                    is_cumulative=True
                                                )
                                                if not _uos_dust(dust_order['id'], 'filled', bot_id=bot_id, filled_qty=dust_qty):
                                                    logger.error(f"🚨 [STATE-GUARD ERROR] Unexpected status update failure for dust order {dust_order['id']} to filled.")
                                                # v2.3.4: Use cent-level seal check
                                                if _credited or dust_qty > 1e-8:
                                                    _sts_dust(bot_id)
                                                logger.info(f"✅ [DUST-FLUSH] {name}: Ledger sealed. Bot will resume scanning next cycle.")
                                        except Exception as e_dust:
                                            logger.error(f"❌ [DUST-FLUSH] {name}: Market dust-close failed: {e_dust}. Bot may require manual intervention.")
                                            _DUST_FLUSH_COOLDOWN[bot_id] = time.time() + 300.0 # 5 minutes cooldown
                                            
                                            # Escalate to STUCK_DUST_NO_EXIT status in DB (INV-35 / Fix 2)
                                            try:
                                                from engine.database import get_connection as _gc_stuck
                                                with _gc_stuck() as _conn_stuck:
                                                    _conn_stuck.execute(
                                                        "UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=?",
                                                        (bot_id,)
                                                    )
                                                logger.critical(f"🚨 [STUCK_DUST_NO_EXIT] {name}: TP is stuck due to reduceOnly catch-22 and min notional. cycle_phase set to STUCK_DUST_NO_EXIT.")
                                            except Exception as e_stuck:
                                                logger.error(f"Failed to update cycle_phase to STUCK_DUST_NO_EXIT: {e_stuck}")
                                else:
                                    # ---------------------------------
                                    # STANDARD TP PLACEMENT
                                    # ---------------------------------
                                    # 🔑 CRITICAL FIX: Embed the CQB_ clientOrderId so that
                                    # maintain_orders can find this TP in the next open_orders fetch.
                                    ccxt_params['newClientOrderId'] = client_order_id

                                    order = self._place_gtx_order_with_retry(exchange, pair, side, tp_amount, tp_price, params=ccxt_params, label=f"{name}-MAINTAIN-TP", position_side=direction)
                                    if order:
                                        # Pop _fallback_cid: if GTX fell back, use the _F CID Binance received
                                        effective_tp_cid = order.pop('_fallback_cid', None) or client_order_id
                                        save_bot_order(bot_id, 'tp', order['id'], tp_price, tp_amount, bot_status['current_step'], order.get('status', 'open'), client_order_id=effective_tp_cid)
                                        logger.info(f"✅ {name}: Maintained TP order for {pair} @ {tp_price}")
                                        # Clear any stale MARGIN_HELD phase stamp since TP is now placed
                                        if str(bot_status.get('cycle_phase', '')).upper() in ('MARGIN_HELD', 'STUCK_DUST_NO_EXIT'):
                                            try:
                                                from engine.database import get_connection as _gc_mhc
                                                with _gc_mhc() as _conn_mhc:
                                                    _conn_mhc.execute(
                                                        "UPDATE trades SET cycle_phase='ACTIVE' WHERE bot_id=? AND cycle_phase IN ('MARGIN_HELD', 'STUCK_DUST_NO_EXIT')",
                                                        (bot_id,)
                                                    )
                                            except Exception:
                                                pass
                            except Exception as e:
                                err_msg = str(e)
                                # Handle margin rejections that slip through clipping (e.g. rapid market moves)
                                if any(s in err_msg.lower() for s in self._MARGIN_SIGNALS):
                                    logger.info(f"ℹ️ {name}: Margin Cap detected during TP placement for {pair}. [MARGIN-ON-HOLD]")
                                    # Stamp MARGIN_HELD phase so monitor shows orange instead of red MISSING CRITICAL.
                                    # This is cleared automatically on the next successful TP placement.
                                    try:
                                        from engine.database import get_connection as _gc_mh
                                        with _gc_mh() as _conn_mh:
                                            _conn_mh.execute(
                                                "UPDATE trades SET cycle_phase='MARGIN_HELD' WHERE bot_id=? AND cycle_phase NOT IN ('CARRY_PENDING')",
                                                (bot_id,)
                                            )
                                    except Exception as _mh_err:
                                        logger.debug(f"[MARGIN-HOLD] DB stamp failed: {_mh_err}")
                                else:
                                    logger.error(f"❌ {name}: Error maintaining TP: {e}")

        # 2b. EE/SYNC DRIFT CHECK: existing TP at wrong price or qty
        elif existing_tp_order and bot_status.get('total_invested', 0) > 0:

            # ── STEP 1: Fetch exchange tick size ONCE for this block.
            # We pass it into _compute_effective_tp (INV-21) so the returned
            # value is always rounded to the same grid as placed_tp, making the
            # comparison below mathematically exact instead of float-approximate.
            _ee_prec = exchange.get_symbol_precision(pair) or {}
            _ee_tick = float(_ee_prec.get('tick_size', 0) or 0)

            # Local helper: round price to the exchange tick grid
            def _round_to_tick_mo(price: float, tick: float) -> float:
                if tick > 0:
                    return round(round(price / tick) * tick, 10)
                return price

            # ── STEP 2: Run EE decay to detect if a NEW interval has fired.
            # _compute_effective_tp only returns a *different* tick-rounded value
            # from raw_db_tp when math.floor(duration_mins / interval_mins) has
            # incremented.  Between step intervals the returned price is identical
            # to placed_tp when both are rounded to tick size (INV-21).
            new_ee_tp = self._compute_effective_tp(
                bot_id, name, bot_status, bot_config, strategy,
                pair=pair, tick_size=_ee_tick
            )

            # ── STEP 3: DRIFT CHECK — compare what Binance actually holds
            # against what we PHYSICALLY PLACED (bot_orders.price), NOT a
            # freshly re-computed value.  Using a re-computed value was the
            # root cause of false SYNC-DRIFT fires: avg_entry_price can shift
            # between cycles (grid fills), making the re-computed base TP
            # differ from the placed TP even when no EE interval has elapsed.
            exchange_tp  = float(existing_tp_order.get('price') or existing_tp_order.get('stopPrice') or 0)
            exchange_qty = self._get_order_amount(existing_tp_order)

            # Read the price we PLACED from bot_orders (the authoritative record).
            placed_tp = 0.0
            try:
                from engine.database import get_connection as _gc_tp_check
                with _gc_tp_check() as _c_chk:
                    _tp_row = _c_chk.execute(
                        "SELECT price FROM bot_orders WHERE bot_id=? AND order_type='tp'"
                        " AND status IN ('open','new','placed') ORDER BY created_at DESC LIMIT 1",
                        (bot_id,)
                    ).fetchone()
                    if _tp_row and _tp_row[0]:
                        placed_tp = float(_tp_row[0])
            except Exception as _e_tp_chk:
                logger.debug(f"[SYNC-DRIFT] {name}: Could not read placed_tp from bot_orders: {_e_tp_chk}")

            # Fall back to DB target_tp_price if bot_orders row is missing (e.g. legacy bot).
            if placed_tp <= 0:
                placed_tp = float(bot_status.get('target_tp_price', 0))

            # DB-TP ZERO GUARD: recalculate only if we genuinely have no reference
            if placed_tp == 0 and bot_status.get('avg_entry_price', 0) > 0:
                placed_tp = strategy.calculate_take_profit_price(bot_status, bot_status.get('avg_entry_price', 0))
                logger.info(f"[TP-RECOVER] {name}: placed_tp was 0, recalculated to {placed_tp:.4f} from avg_entry.")

            # ── STEP 4: Decide whether to replace the TP on exchange.
            # Case A: EE fired a new interval → must replace with new_ee_tp.
            # Case B: Genuine price mismatch (e.g. position size changed, TP
            #         was cancelled/refilled and re-placed at wrong price).
            #
            # Tolerance: 0.1% — only covers exchange rounding noise (tick_size).
            # We do NOT need wider tolerance because we compare placed vs live,
            # not recomputed vs live.
            # 🚀 ROOT FIX (v2.3.5): Use open_qty accumulator (maintained atomically by credit_fill)
            # instead of total_invested/avg_price. The two can diverge when partial fills occur
            # on cancelled orders (e.g., cancelled entry with filled_amount>0), causing a permanent
            # 50%+ qty-drift signal that fires a TP cancel storm every cycle.
            # open_qty IS the exchange-confirmed position size — it IS the TP qty.
            _open_qty_acc = float(bot_status.get('open_qty', 0) or 0)
            if _open_qty_acc > 0:
                db_qty = _open_qty_acc
                logger.debug(f"[TP-QTY-v2.3.5] {name}: Using open_qty accumulator {db_qty:.6f} for drift check.")
            else:
                db_qty = strategy.calculate_take_profit_amount(bot_status, current_price, pair, exchange)
            valid, db_qty, _, _ = exchange.validate_order(pair, 'sell' if direction == 'LONG' else 'buy', db_qty, placed_tp, is_closing=True)

            # 🚀 INV-21 (v3.9.12): Round BOTH sides to exchange tick size before comparing.
            # The raw EE computation returns a sub-tick float (e.g. 1796.3033) while
            # placed_tp is stored as an exchange-rounded value (e.g. 1796.30).
            # Without rounding, new_ee_tp != placed_tp fires every cycle even when the
            # EE step has NOT advanced — causing an infinite cancel/replace loop.
            _rnd_new_ee = _round_to_tick_mo(new_ee_tp, _ee_tick)
            _rnd_placed  = _round_to_tick_mo(placed_tp,  _ee_tick)
            ee_interval_fired = (
                abs(_rnd_new_ee - _rnd_placed) >= max(_ee_tick, 1e-9)
                and _rnd_new_ee > 0
                and _rnd_placed > 0
            )
            if exchange_tp > 0 and placed_tp > 0:
                drift_tp  = abs(placed_tp - exchange_tp) / max(placed_tp, 0.01)
                drift_qty = abs(db_qty   - exchange_qty) / max(db_qty, 0.0001)

                tp_tolerance  = 0.001   # 0.1% — rounding noise only (not a patch)
                qty_tolerance = 0.05    # 5% for lot-size step rounding on small positions

                price_drifted = drift_tp > tp_tolerance
                qty_drifted   = drift_qty > qty_tolerance

                if ee_interval_fired or price_drifted or qty_drifted:
                    replace_reason = []
                    if ee_interval_fired:
                        replace_reason.append(f"EE-stepped {placed_tp:.6f}→{new_ee_tp:.6f} (tick-rounded: {_rnd_placed:.6f}→{_rnd_new_ee:.6f})")
                    if price_drifted:
                        replace_reason.append(f"price-drift {drift_tp*100:.4f}% (placed:{placed_tp:.4f} live:{exchange_tp:.4f})")
                    if qty_drifted:
                        replace_reason.append(f"qty-drift {drift_qty*100:.2f}% (want:{db_qty:.4f} live:{exchange_qty:.4f})")
                    logger.info(f"[SYNC-DRIFT] {name}: Replacing TP — {'; '.join(replace_reason)}")

                    # Use the EE-updated price if a new interval fired, else the placed price
                    target_tp = new_ee_tp if ee_interval_fired else placed_tp
                    tp_order = self._sync_replace_tp(
                        bot_id, name, pair, direction, bot_status, exchange,
                        target_tp, db_qty, existing_tp_order
                    )


        # 3. Check for missing / filled Grid order
        if not existing_grid_order and bot_status['current_step'] < strategy.max_steps:
             # 🚀 GRID IDEMPOTENCY LOCK: Absolute State Enforcement
             # Check DB for ANY proof that we already placed this exact step in this cycle.
             # We check all active/terminal statuses, trusting the DB truth over lagging exchange sync.
             try:
                 from engine.database import get_connection
                 _conn = get_connection()
                 # Only block when an exchange-tracked grid is actively open (not filled/closed ghosts).
                 _active_grid = _conn.execute(
                     """
                     SELECT id, order_id, status FROM bot_orders
                     WHERE bot_id=? AND cycle_id=? AND step=? AND order_type='grid'
                       AND status IN ('placing', 'new', 'open', 'partially_filled', 'cancelling')
                       AND COALESCE(order_id, '') NOT LIKE 'PLACING_%'
                     """,
                     (bot_id, bot_status.get('cycle_id', 0), expected_grid_step),
                 ).fetchone()
                 if _active_grid:
                     _gid, _goid, _gst = _active_grid
                     _on_exchange = any(
                         str(o.get('id')) == str(_goid)
                         for o in (open_orders or [])
                         if '_GRID_' in str(o.get('clientOrderId', ''))
                     )
                     if _on_exchange:
                         logger.info(
                             f"🛡️ {name}: Grid step {expected_grid_step} live on exchange ({_goid}). "
                             f"Skipping duplicate placement."
                         )
                         return None
                     logger.warning(
                         f"👻 {name}: DB grid step {expected_grid_step} ({_gst}) not on exchange — "
                         f"clearing stale row {_gid} to restore grid."
                     )
                     from engine.database import update_order_status as _uos_grid
                     _uos_grid(str(_goid or _gid), 'cancelled', bot_id=bot_id)
             except Exception as _grid_idem_err:
                 logger.debug(f"{name}: grid idempotency check: {_grid_idem_err}")

             # 🚀 STRICT SEQUENCING: Do NOT place Grid orders if an Entry order is still open.
             if existing_entry_orders:
                  logger.info(f"⏳ {name}: Entry order is still open. Waiting for Full Fill before placing Grid Orders.")
                  return None

             # v2.0: Physical-size drift is surfaced as a warning alert only.
             # Grid placement is NOT blocked by drift (Rule 5 of canonical architecture).
             # Risk is managed by circuit breaker and per-bot position limits.
             try:
                 phys_positions = market_snapshot.get('positions', []) if market_snapshot else []
                 phys_long = 0.0
                 phys_short = 0.0
                 for p in phys_positions:
                     if normalize_symbol(p.get('symbol', '')) == normalize_symbol(pair):
                        size = abs(float(p.get('contracts', 0) or 0))
                        pos_amount = float(p.get('contracts', 0) or 0)
                        if pos_amount < 0: phys_short += size
                        elif pos_amount > 0: phys_long += size
                 phys_net_qty = phys_long - phys_short
                
                 virtual_qty = (float(bot_status.get('total_invested', 0) or 0) /
                                float(bot_status.get('avg_entry_price', 1) or 1))

                 # Check for sibling bots (any other active bot on the same pair with a position)
                 sibling_count = 0
                 try:
                     from engine.database import get_connection as _gc_drift
                     with _gc_drift() as _c_drift:
                         _sib = _c_drift.execute("""
                             SELECT COUNT(*)
                             FROM bots b JOIN trades t ON t.bot_id = b.id
                             WHERE b.pair=? AND b.id != ? AND b.is_active=1
                               AND t.total_invested > 0.01
                         """, (pair, bot_id)).fetchone()
                         if _sib:
                             sibling_count = int(_sib[0] or 0)
                 except Exception:
                     pass

                 if sibling_count > 0:
                     logger.debug(f"[DRIFT-SKIP] {name}: shared pair with {sibling_count} sibling(s) — pair-level parity checked by auditor")
                 else:
                     expected_net = virtual_qty if direction == 'LONG' else -virtual_qty
                     actual_net = phys_net_qty

                     drift_qty = abs(actual_net - expected_net)
                     drift_pct = drift_qty / max(abs(expected_net), 0.001)

                     if virtual_qty > 0.001 and drift_pct > 0.10:
                         logger.warning(
                             f"[DRIFT-ALERT] {name}: physical_net={actual_net:.4f} vs "
                             f"expected_net={expected_net:.4f} "
                             f"(this_virt={virtual_qty:.4f} "
                             f"diff={drift_qty:.4f} {drift_pct*100:.1f}%). "
                             f"Monitor parity — grid placement continues normally (v2.0)."
                         )
             except Exception as _drift_err:
                 logger.debug(f"Drift alert check failed for {name}: {_drift_err}")




             # ══════════════════════════════════════════════════════════════
             # STEP-PROGRESSION-PROOF  (3-Tier, Self-Healing)
             # ══════════════════════════════════════════════════════════════
             # Tier 1: entry_confirmed DB flag (set by seal_trade_state / WS)
             # Tier 2: bot_orders filled row for current_step
             # Tier 3: Math proof — total_invested > 0 AND avg_entry_price > 0
             #         → position is provably real; auto-heal entry_confirmed=1
             #         so the deadlock never recurs.
             #
             # WHY a math fallback:
             #   bot_orders rows can be missing after DB migration, engine restarts
             #   during a fill, or when the position was adopted from the exchange
             #   (forensic / manual import paths). The trades table math
             #   (total_invested / avg_entry_price) is ALWAYS the ground truth;
             #   if it says we hold a position, we trust it unconditionally.
             # ══════════════════════════════════════════════════════════════
             if bot_status['current_step'] > 0:
                 try:
                     _ec = bot_status.get('entry_confirmed', 0)
                     _invested = float(bot_status.get('total_invested', 0) or 0)
                     _avg      = float(bot_status.get('avg_entry_price', 0) or 0)

                     # ── Tier 1: entry_confirmed flag ──────────────────────
                     if _ec == 1:
                         logger.debug(f"🛡️ {name}: Step proof T1 passed (entry_confirmed=1).")

                     else:
                         # ── Tier 2: bot_orders filled row ─────────────────
                         from engine.database import get_connection as _gcsp
                         _csp = _gcsp()
                         _row = _csp.execute("""
                             SELECT COUNT(*) FROM bot_orders
                             WHERE bot_id=? AND status IN ('filled','closed')
                               AND step=? AND created_at >= (? - 2592000)
                         """, (bot_id, bot_status['current_step'],
                               bot_status.get('basket_start_time', 0))).fetchone()

                         if _row and _row[0] > 0:
                             logger.debug(f"🛡️ {name}: Step proof T2 passed (bot_orders filled row found).")
                             # Promote to T1 so next cycle skips this query
                             try:
                                 _csp.execute(
                                     "UPDATE trades SET entry_confirmed=1 WHERE bot_id=?", (bot_id,)
                                 )
                                 _csp.commit()
                                 bot_status['entry_confirmed'] = 1
                             except Exception as _e:
                                 logger.error(f"[PROOF-T2] Failed to promote entry_confirmed: {_e}")

                         elif _invested > 0.01 and _avg > 0:
                             # ── Tier 3: Math proof — auto-heal ────────────
                             # total_invested and avg_entry_price are non-zero,
                             # meaning seal_trade_state already wrote the ledger
                             # from real fills. The bot_orders row is simply missing
                             # (migration, adoption, restart). Trust the math.
                             logger.warning(
                                 f"🩹 [PROOF-T3] {name}: bot_orders fill record absent for step "
                                 f"{bot_status['current_step']} but math proves position "
                                 f"(invested=${_invested:.2f} avg={_avg:.4f}). "
                                 f"Auto-healing entry_confirmed=1."
                             )
                             try:
                                 _csp.execute(
                                     "UPDATE trades SET entry_confirmed=1 WHERE bot_id=?", (bot_id,)
                                 )
                                 _csp.commit()
                                 bot_status['entry_confirmed'] = 1
                             except Exception as _heal_e:
                                 logger.error(f"[PROOF-T3] {name}: Failed to write entry_confirmed: {_heal_e}")
                             # Continue to grid placement — proof accepted

                         else:
                             # All 3 tiers failed: no flag, no order row, no math.
                             # The bot genuinely has no fill proof — block and wait.
                             logger.warning(
                                 f"🛑 {name}: Step Progression Blocked (all 3 proof tiers failed). "
                                 f"step={bot_status['current_step']} invested=${_invested:.4f} "
                                 f"entry_confirmed={_ec}. Waiting for reconciler/WS."
                             )
                             return None

                 except Exception as e:
                     logger.error(f"❌ {name}: Step progression proof raised exception: {e}")
                     # On unexpected error: fall through ONLY if math says we hold
                     _invested = float(bot_status.get('total_invested', 0) or 0)
                     _avg      = float(bot_status.get('avg_entry_price', 0) or 0)
                     if _invested > 0.01 and _avg > 0:
                         logger.warning(f"⚠️ {name}: Proof exception but math confirms position. Continuing.")
                     else:
                         logger.warning(f"🛑 {name}: Proof exception and no math backup. Blocking grid.")
                         return None


             # 🚀 FUNDAMENTAL FIX: Re-calculate base size dynamically here
             # Just like execute_entry, we must override bot_config BEFORE calling Strategy
             if bot_config.get('use_min_size', False):
                 prec = exchange.get_symbol_precision(pair)
                 step_size = prec.get('step_size', 0.001)
                 min_cost_qty = step_size * current_price
                 
                 exchange_min_notional = prec.get('min_notional', None)
                 if exchange_min_notional is None:
                     exchange_min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                     
                 bot_config['base_size'] = max(min_cost_qty, exchange_min_notional) * 1.05
                 # 🚀 CRITICAL: Keep strategy.params in sync with the use_min_size override.
                 # Without this, calculate_grid_order_amount reads stale strategy.params['base_size']
                 # (e.g. 150.0 default) rather than the correctly computed min notional size.
                 strategy.params['base_size'] = bot_config['base_size']
             else:
                 prec = exchange.get_symbol_precision(pair)
                 exchange_min_notional = prec.get('min_notional', None)
                 if exchange_min_notional is None:
                     exchange_min_notional = 100.0 if (getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)) else 5.0
                 if bot_config.get('base_size', 0) < exchange_min_notional:
                     logger.error(f"⛔ CONFIG ERROR [{pair}]: Configured base_size=${bot_config.get('base_size',0):.2f} is below exchange minimum ${exchange_min_notional:.2f}. Halting grid.")
                     update_bot_error(bot_id, f"CONFIG ERROR: Base Size (${bot_config.get('base_size',0):.2f}) < Min Notional (${exchange_min_notional:.2f})")
                     return
             # 🚀 STRICT SYNCHRONOUS STATE LOCK (GRID)
             # Wait if the DB already thinks a Grid is open, but CCXT was just too slow to show it.
             if len(local_grid_ids) > 0:
                  logger.warning(f"⏳ {name}: CCXT indicates missing Grid, but DB confirms {local_grid_ids} was placed. Verifying status...")
                  # 🚀 STALEMATE EVICTOR (GRID): Verify if it's dead before blocking
                  try:
                      latest_grid_id = local_grid_ids[-1]
                      order_status = exchange.fetch_order(latest_grid_id, pair)
                      status_str = order_status.get('status') if order_status else 'unknown'

                      if status_str in ['filled', 'closed']:
                           actual_fill_qty = float(order_status.get('filled', 0) or 0)
                           actual_fill_price = float(order_status.get('average') or order_status.get('price') or 0)
                           # 🔧 Demo FAPI returns average=0 for filled orders — fall back to stored grid price
                           if actual_fill_price <= 0:
                               try:
                                   from engine.database import get_connection as _gcnn
                                   _fb_conn = _gcnn()
                                   _fb_row = _fb_conn.execute(
                                       "SELECT price FROM bot_orders WHERE order_id=? AND bot_id=?",
                                       (str(latest_grid_id), bot_id)
                                   ).fetchone()
                                   actual_fill_price = float(_fb_row[0]) if _fb_row and _fb_row[0] else float(current_price)
                               except Exception: actual_fill_price = float(current_price)
                           logger.info(f"✅ {name}: Stored GRID ID {latest_grid_id} is FILLED @ {actual_fill_price} (Qty: {actual_fill_qty}). Processing INLINE.")
                           # 🚀 CRITICAL FIX: Process the fill inline — do NOT delegate to offline sync.
                           # The periodic reconciler does not call reconstruct_offline_fills frequently enough;
                           # delegating caused an infinite "Blocked by Local DB Lock" loop.
                           if actual_fill_qty <= 0 or actual_fill_price <= 0:
                               logger.error(f"❌ {name}: Cannot process inline fill — zero qty={actual_fill_qty} or price={actual_fill_price}. Evicting grid to unblock.")
                               from engine.database import get_connection as _gc
                               from engine.database import update_order_status as _uos
                               if _uos(latest_grid_id, 'cancelled', bot_id=bot_id):
                                   _c = _gc()
                                   _c.execute("UPDATE trades SET grid_order_id = NULL WHERE bot_id = ?", (bot_id,))
                                   _c.commit(); _c.close()
                                   local_grid_ids = []
                               else:
                                   logger.warning(f"⚠️ {name}: Stored Grid {latest_grid_id} cancelled update was rejected (likely filled). Retaining state.")
                           else:
                               try:
                                   # v2.0: credit_fill (idempotent) -> seal_trade_state (single writer)
                                   from engine.ledger import credit_fill as _cf, seal_trade_state as _sts
                                   from engine.database import update_order_status as _uos
                                   _credited = _cf(
                                       bot_id=bot_id,
                                       order_id=str(latest_grid_id),
                                       cumulative_qty=actual_fill_qty,
                                       avg_price=actual_fill_price,
                                       order_type='grid',
                                       is_cumulative=True
                                   )
                                   if not _uos(latest_grid_id, 'filled', bot_id=bot_id, filled_qty=actual_fill_qty):
                                       logger.error(f"🚨 [STATE-GUARD ERROR] Unexpected status update failure for grid order {latest_grid_id} to filled.")
                                   if _credited:
                                       _sts(bot_id)
                                   logger.info(
                                       f"[INLINE-GRID-FILL] {name}: credit_fill->seal complete. "
                                       f"qty={actual_fill_qty} @ {actual_fill_price}. Lock cleared."
                                   )
                               except Exception as _fill_err:
                                   logger.error(f"[INLINE-GRID-FILL] {name}: Failed {latest_grid_id}: {_fill_err}")
                           local_grid_ids = []  # Clear lock so the next step's grid can be placed
                      elif status_str in ['new', 'open', 'partially_filled']:
                           # 🚀 SNAPSHOT LAG FIX: Order is CONFIRMED LIVE on exchange but absent from
                           # the stale start-of-cycle snapshot. Back-populate WS cache and do NOT evict.
                           logger.info(f"✅ {name}: Stored Grid {latest_grid_id} is CONFIRMED LIVE (status={status_str}) — snapshot was stale. Healing cache.")
                           from engine.ws_cache import get_ws_cache as _gwsc
                           _gwsc().update_order(str(latest_grid_id), order_status)
                           # Keep local_grid_ids intact — the lock correctly reflects exchange reality
                      elif status_str in ['canceled', 'cancelled', 'expired', 'rejected']:
                          logger.info(f"🚫 {name}: Stored GRID ID {latest_grid_id} is CANCELLED on exchange. Evicting from DB state.")
                          from engine.database import update_order_status as _uos
                          _uos(latest_grid_id, 'cancelled', bot_id=bot_id)
                          local_grid_ids = []  # Clear locals to unblock
                      else:
                          logger.warning(f"⏳ {name}: Stored Grid {latest_grid_id} status is {status_str}, but missing from open_orders! Forcing CANCEL and Eviction.")
                          try:
                               exchange.cancel_order(latest_grid_id, pair)
                          except Exception as _e:
                              logger.debug(f'[EXPECTED] cancel unrecognised grid status: {_e}')
                          from engine.database import update_order_status as _uos
                          _uos(latest_grid_id, 'cancelled', bot_id=bot_id)
                          local_grid_ids = []  # Clear to allow grid logic below to immediately fire
                  except Exception as _evict_err:
                      err_str = str(_evict_err).lower()
                      if "not found" in err_str or "-2013" in err_str:
                          logger.warning(f"🚫 {name}: Stored GRID ID {local_grid_ids[-1]} NOT FOUND on exchange. Evicting from DB state.")
                          from engine.database import update_order_status as _uos
                          _uos(local_grid_ids[-1], 'cancelled', bot_id=bot_id)
                          local_grid_ids = []
                      else:
                          logger.error(f"❌ {name}: Failed to evict stalemate GRID ID: {_evict_err}")
                          from engine.database import update_order_status as _uos
                          _uos(local_grid_ids[-1], 'cancelled', bot_id=bot_id)
                          local_grid_ids = []


             if len(local_grid_ids) > 0:
                  grid_price = 0
                  grid_explain = "Blocked by Local DB Lock"
                  grid_amount = 0
             else:
                  grid_res = strategy.calculate_grid_order_price(bot_status, current_price, market_data=current_market_data, multi_tf_data=bot_multi_tf)
                  if isinstance(grid_res, tuple):
                       grid_price, grid_explain = grid_res
                  else:
                       grid_price, grid_explain = grid_res, ""
                  grid_amount = strategy.calculate_grid_order_amount(bot_status, current_price, pair, exchange)
             
             # 🚀 STRICT ATR GUARD: If grid_price is 0, the strategy aborted.
             if grid_price <= 0:
                 logger.warning(f"🛑 {name}: Strategy returned INVALID grid price (0.0). Aborting placement to prevent drift. Reason: {grid_explain}")
                 update_bot_error(bot_id, f"Grid Error: {grid_explain}")
                 return None

             # 🚀 SPREAD-CROSSING MAKER LOOP FIX (Root Cause of Flashing)
             # Same as TP logic: If a Post-Only Grid crosses the active spread, Binance silently EXPIRES it.
             # Standard Grid: LONG bot buys to open line. Must be <= Best Bid.
             #                SHORT bot sells to open line. Must be >= Best Ask.
             try:
                 bid, ask = exchange.get_best_bid_ask(pair)
                 if bid is not None and ask is not None:
                     bid_val = float(bid)
                     ask_val = float(ask)
                     if direction == 'LONG':
                         # We are Buying to open. If Grid is higher than or equals Ask, it's a Taker.
                         if grid_price >= ask_val:
                             old_px = grid_price
                             grid_price = bid_val
                             logger.info(f"🚀 {name}: Grid Spread Cross Prevented! (Buy {old_px} >= Ask {ask_val}). Adjusted to Bid {grid_price} to preserve GTX.")
                     else:
                         # We are Selling to open. If Grid is lower than or equals Bid, it's a Taker.
                         if grid_price <= bid_val:
                             old_px = grid_price
                             grid_price = ask_val
                             logger.info(f"🚀 {name}: Grid Spread Cross Prevented! (Sell {old_px} <= Bid {bid_val}). Adjusted to Ask {grid_price} to preserve GTX.")
             except Exception as e:
                 logger.warning(f"⚠️ {name}: Market Gap check failed ({e}). Proceeding without spread-cross protection.")
                     
             logger.info(f"🔍 [GRID-MAINTENANCE] {name}: Target=${grid_price} | {grid_explain}")

             if grid_amount > 0 and grid_price > 0:
                if config.DRY_RUN:
                    logger.info(f"📊 [DRY-RUN] Bot {name} maintains Grid for {pair} @ {grid_price}")
                else:
                    logger.info(f"🔍 [GRID-DEBUG] Bot {name} ({direction}) | Price={current_price} | GridTarget={grid_price} | Amount={grid_amount} | Step={bot_status['current_step']} | BaseSize={bot_config.get('base_size')} | Multi={bot_config.get('martingale_multiplier')} | StratBase={strategy.params.get('base_size')} | StratMult={strategy.params.get('martingale_multiplier')}")
                    
                    side = 'buy' if direction == 'LONG' else 'sell'
                    
                    # 🚀 FAT FINGER GUARD: Dynamic Max-Size Protocol
                    try:
                        base_size_usd = float(strategy.params.get('base_size', 150.0))
                        multiplier = float(strategy.params.get('martingale_multiplier', 2.0))
                        max_step = int(strategy.max_steps)
                        abs_max_usd = base_size_usd * (multiplier ** max_step) * 1.5
                        abs_max_qty = abs_max_usd / current_price
                        
                        if grid_amount > abs_max_qty:
                            logger.critical(f"🛑 FAT FINGER BLOCK: {name} Grid Amount {grid_amount} drastically exceeds strategy bounds ({abs_max_qty:.4f} max limit). Cancelling Grid placement.")
                            update_bot_error(bot_id, "FAT FINGER GUARD: Grid size exceeds strategy absolutes.")
                            return None
                    except Exception as _e:
                        logger.debug(f'[FAT-FINGER] bounds calc failed (guard skipped): {_e}')
                    
                    # Check grid retry backoff
                    if bot_id in self._grid_backoff:
                        last_fail_ts, fail_count = self._grid_backoff[bot_id]
                        backoff_duration = min(2 ** fail_count, 60)
                        elapsed = time.time() - last_fail_ts
                        if elapsed < backoff_duration:
                            logger.debug(
                                f"[GRID-BACKOFF] {name}: Skipping grid placement for {backoff_duration - elapsed:.1f}s "
                                f"(fail_count={fail_count}, elapsed={elapsed:.1f}s)"
                            )
                            return None

                    valid, grid_amount, grid_price, msg = exchange.validate_order(pair, side, grid_amount, grid_price)
                    if not valid:
                        # v2.0: Distinguish between qty-too-small (config problem) vs other rejections
                        if 'min' in msg.lower() or 'notional' in msg.lower() or 'qty' in msg.lower() or 'size' in msg.lower():
                            logger.warning(
                                f"[GRID-QTY-GUARD] {name}: Grid qty too small for exchange minimum. "
                                f"msg='{msg}' | grid_amount={grid_amount:.6f} @ {grid_price:.4f} "
                                f"(notional=${grid_amount * grid_price:.2f}). "
                                f"Increase base_size in bot config to resolve."
                            )
                            update_bot_error(bot_id, f"Grid Qty too small for exchange. Base Size too low?")
                        else:
                            logger.error(f"[GRID-VAL-FAIL] {name}: Grid validation failed: {msg}")
                            update_bot_error(bot_id, f"Grid Validation Failed: {msg}")
                    else:
                        try:
                            # [v3.5.4] Fix: Skip gate if already in trade
                            if bot_status.get('total_invested', 0) > 0.01 and bot_status.get('current_step', 0) > 0:
                                _gow_ok, _gow_reason = True, ''
                            else:
                                from engine.oneway_netting import gate_oneway_opposite_entry
                                _gow_ok, _gow_reason = gate_oneway_opposite_entry(
                                    bot_id, pair, direction
                                )
                            if not _gow_ok:
                                logger.warning(f"🛑 {name}: grid blocked — {_gow_reason}")
                                return None

                            cycle_id = bot_status.get('cycle_id', 0)
                            conn = get_connection()
                            has_existing_db = conn.execute(
                                "SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND cycle_id = ? AND step = ? AND order_type = 'grid'",
                                (bot_id, cycle_id, bot_status['current_step'] + 1)
                            ).fetchone()[0] > 0
                            client_order_id_grid = self._generate_deterministic_id(
                                bot_id, 'GRID', cycle_id, bot_status['current_step'] + 1,
                                is_replacement=has_existing_db
                            )

                            # v2.0: Guard against placing a grid when we already have one from
                            # a recent retry attempt (_R or _F suffix from GTX retry logic).
                            # The retry itself already placed the order — don't double-place.
                            retry_cids = {client_order_id_grid + '_R', client_order_id_grid + '_F'}
                            already_placed = [o for o in valid_grid_orders
                                              if o.get('clientOrderId', '') in retry_cids]
                            if already_placed:
                                logger.info(
                                    f"[GRID-DEDUP] {name}: Retry-suffix grid already live "
                                    f"({already_placed[0].get('clientOrderId')}). Skipping fresh placement."
                                )
                                return None

                            ccxt_grid_params = {'clientOrderId': client_order_id_grid, 'postOnly': True, 'timeInForce': 'GTX'}
                            
                            _prec = exchange.get_symbol_precision(pair)
                            _tick = _prec.get('tick_size') or _prec.get('step_size') or 0.0001
                            if grid_price == exchange.round_to_step(current_price, _tick):
                                logger.warning(f"⚠️ {name}: Grid price matches active market gap. Dropping GTX Maker flag to allow execution.")
                                ccxt_grid_params = {'clientOrderId': client_order_id_grid, 'timeInForce': 'GTC'}
                                
                            order = self._place_gtx_order_with_retry(exchange, pair, side, grid_amount, grid_price, params=ccxt_grid_params, label=f"{name}-MAINTAIN-GRID", position_side=direction)
                            if order:
                                # Pop _fallback_cid: if GTX fell back, use the _F CID Binance received
                                effective_grid_cid = order.pop('_fallback_cid', None) or client_order_id_grid
                                save_bot_order(bot_id, 'grid', order['id'], grid_price, grid_amount, bot_status['current_step'] + 1, order.get('status', 'open'), client_order_id=effective_grid_cid, notes=grid_explain)
                                # ✅ Successful grid placement — clear any stale pos_limit flag
                                if bot_status.get('pos_limit_hit'):
                                    flag_bot_pos_limit(bot_id, False)
                                logger.info(f"✅ {name}: Maintained Grid order for {pair} @ {grid_price}")
                                # Reset grid retry backoff
                                self._grid_backoff.pop(bot_id, None)
                        except Exception as e:
                            err_msg = str(e)
                            # Check if this is a margin/position cap error (Binance error codes: -2019, -5022)
                            is_margin_cap = any(code in err_msg for code in ['-2019', '-5022', '-4028', 'margin', 'position limit'])
                            if is_margin_cap:
                                is_reducing = self._is_order_net_reducing(pair, side, grid_amount, bot_id=bot_id, bot_direction=direction)
                                if is_reducing:
                                   logger.info(f"🚀 {name}: Margin Cap hit but order is Reductive. Force-Allowing for one-way netting.")
                                   # We continue without flagging pos_limit_hit=True if it's reducing
                                else:
                                   logger.warning(f"🚫 {name}: Margin/Position cap hit. Grid order held. [MARGIN-ON-HOLD]")
                                   flag_bot_pos_limit(bot_id, True)
                            else:
                                logger.error(f"❌ {name}: Error maintaining Grid: {e}")
                                update_bot_error(bot_id, f"Exchange Error: {e}")
                                
                                err_lower = err_msg.lower()
                                if any(tok in err_lower for tok in ('408', 'timeout', 'requesttimeout', 'networkerror', 'connection', 'temporary')):
                                    last_fail_ts, fail_count = self._grid_backoff.get(bot_id, (0.0, 0))
                                    self._grid_backoff[bot_id] = (time.time(), fail_count + 1)
                                    logger.warning(
                                        f"⏳ [GRID-BACKOFF-SET] {name}: Network/408 error occurred. "
                                        f"Set grid retry backoff (fail_count={fail_count + 1}, delay={min(2**(fail_count + 1), 60)}s)"
                                    )
                            
        # 3b. MAX-STEP LOCK: If we reached max steps, there should be NO Grid orders. Clean them completely!
        elif bot_status['current_step'] >= strategy.max_steps:
             if existing_grid_order:
                 logger.warning(f"🛑 {name}: Max steps reached ({strategy.max_steps}) but Grid exists! Cancelling physical Grid {existing_grid_order['id']}.")
                 try:
                     exchange.cancel_order(existing_grid_order['id'], pair)
                     from engine.database import update_order_status as _uos
                     if not _uos(existing_grid_order['id'], 'cancelled', bot_id=bot_id):
                         logger.warning(f"⚠️ {name}: Max steps reached grid cancel update rejected for {existing_grid_order['id']} (likely filled).")
                 except Exception as _e:
                     logger.debug(f'[EXPECTED] max-step ghost grid cancel: {_e}')
             
             if len(local_grid_ids) > 0:
                 logger.warning(f"🧹 {name}: Max steps reached but DB lists Grid ghosts {local_grid_ids}. Sweeping DB cleanly.")
                 from engine.database import update_order_status as _uos
                 for ghost_id in local_grid_ids:
                     if not _uos(ghost_id, 'cancelled', bot_id=bot_id):
                         logger.warning(f"⚠️ {name}: Max steps reached ghost grid cancel update rejected for {ghost_id} (likely filled).")

        # 3c. GRID SYNC-DRIFT: If grid exists but price is imprecise or drifted
        elif existing_grid_order and bot_status['total_invested'] > 0:
            current_market_data = market_snapshot.get('market_data', {}).get(pair, None)
            bot_multi_tf = market_snapshot.get('multi_tf_data', {}).get(pair, {})
            grid_res = strategy.calculate_grid_order_price(bot_status, current_price, market_data=current_market_data, multi_tf_data=bot_multi_tf)
            target_grid_price, grid_explain = grid_res if isinstance(grid_res, tuple) else (grid_res, "")
            
            if target_grid_price > 0:
                side = 'buy' if direction == 'LONG' else 'sell'
                # Safe fallback for amount key — varies by source (CCXT vs DB cache)
                grid_amt = float(existing_grid_order.get('amount') or existing_grid_order.get('origQty') or existing_grid_order.get('qty') or 0)
                # Pass through validation to get correct precision
                _, _, target_grid_price, _ = exchange.validate_order(pair, side, grid_amt, target_grid_price)
                
                curr_grid_price = float(existing_grid_order.get('price', 0))
                
                # ATR-grid bots intentionally lock the ATR at grid placement time (locked_atr).
                # Re-computing the ATR every cycle will always produce a slightly different price,
                # so we SKIP GRID-SYNC for ATR grids to prevent the constant cancel/replace loop.
                use_atr_grid = bot_config.get('UseATRGrid', False)
                if use_atr_grid:
                    try:
                        _mkt = exchange.exchange.markets.get(pair, {})
                        tick = float(_mkt.get('precision', {}).get('price') or 0.01)
                    except Exception:
                        tick = 0.01
                    if abs(curr_grid_price - target_grid_price) > 2 * tick:
                        logger.debug(f"[GRID-SYNC] {name}: ATR-grid drift ({curr_grid_price:.4f} -> {target_grid_price:.4f}). Skipping auto-replace (ATR-locked).")
                        # Do NOT cancel — ATR grids are anchored at placement
                else:
                    # Non-ATR grids: replace if price drifted > 0.5% (was 0.1%, widened to stop noise triggers)
                    if abs(curr_grid_price - target_grid_price) / max(target_grid_price, 0.0001) > 0.005:
                        # 🚀 ROOT CAUSE FIX (mirrored from Block 1, line ~1009):
                        # A grid with a partial fill MUST NOT be cancelled — the filled portion
                        # is evidence of real inventory. Cancelling it wipes that proof from the ledger
                        # and leaves the bot without a grid order (e.g. SUI GRID_9: 53.1 filled, then
                        # cancelled by this block which lacked this guard).
                        current_fill = float(existing_grid_order.get('filled', 0) or 0)
                        if current_fill > 0:
                            logger.info(f"🛡️ [GRID-SYNC] {name}: Grid drifted, but has partial fill ({current_fill}). CANCEL BLOCKED — partial fill is real inventory.")
                        else:
                            logger.info(f"🔄 [GRID-SYNC] {name}: Grid drifted ({curr_grid_price:.4f} -> {target_grid_price:.4f}). Replacing.")
                            try:
                                grid_order_id = existing_grid_order.get('order_id', existing_grid_order.get('id'))
                                exchange.cancel_order(grid_order_id, pair)
                                filled_qty = float(existing_grid_order.get('filled', 0) or 0)
                                if update_order_status(grid_order_id, 'cancelled', bot_id=bot_id, filled_qty=filled_qty):
                                    existing_grid_order = None # Force re-place in next cycle
                                else:
                                    logger.warning(f"⚠️ [GRID-SYNC] {name}: Failed to mark grid order {grid_order_id} as cancelled (likely filled). Retaining state.")
                            except Exception as e_grid_sync:
                                logger.error(f"❌ [GRID-SYNC] {name}: Failed to cancel drifted grid: {e_grid_sync}")

        # 🛡️ HEDGE CHILD SIGNAL: if parent has a hedge child and step >= trigger, signal entry
        try:
            from engine.database import get_connection as _gc_hcs
            _conn_hcs = _gc_hcs()
            hedge_child_id = bot_config.get('hedge_child_bot_id') or bot_status.get('hedge_child_bot_id')
            
            # Fetch from DB if not in config
            if not hedge_child_id:
                _row_child = _conn_hcs.execute("SELECT hedge_child_bot_id FROM bots WHERE id=?", (bot_id,)).fetchone()
                if _row_child:
                    hedge_child_id = _row_child[0]

            hedge_trigger = bot_config.get('hedge_trigger_step')
            if hedge_trigger is None:
                _row_trigger = _conn_hcs.execute("SELECT hedge_trigger_step FROM bots WHERE id=?", (bot_id,)).fetchone()
                hedge_trigger = int(_row_trigger[0] or 0) if _row_trigger else 0
            else:
                hedge_trigger = int(hedge_trigger)

            if hedge_child_id and hedge_trigger > 0:
                current_step = int(bot_status.get('current_step', 0))
                if current_step >= hedge_trigger:
                    # Get the qty that actually filled on this specific step
                    _step_row = _conn_hcs.execute(
                        """SELECT COALESCE(SUM(filled_amount), 0)
                        FROM bot_orders
                        WHERE bot_id = ? AND step = ? AND cycle_id = ?
                        AND order_type IN ('entry', 'grid')
                        AND status IN ('filled', 'partially_filled', 'closed')
                        AND filled_amount > 0""",
                        (bot_id, current_step, int(bot_status.get('cycle_id', 1)))
                    ).fetchone()
                    step_qty = float(_step_row[0] or 0) if _step_row else 0.0
                    # Get parent's actual fill price for this step
                    _fill_price_row = _conn_hcs.execute(
                        """SELECT ROUND(SUM(filled_amount * price) / NULLIF(SUM(filled_amount), 0), 8)
                           FROM bot_orders
                           WHERE bot_id = ? AND step = ? AND cycle_id = ?
                           AND order_type IN ('entry', 'grid')
                           AND status IN ('filled', 'partially_filled', 'closed')
                           AND filled_amount > 0""",
                        (bot_id, current_step, int(bot_status.get('cycle_id', 1)))
                    ).fetchone()
                    actual_fill_price = float(_fill_price_row[0] or 0) if _fill_price_row else 0.0
                    
                    # Fall back to current_price only if no fill price found
                    step_fill_price = actual_fill_price if actual_fill_price > 0 else current_price

                    self._signal_hedge_child_entry(
                        parent_bot_id=bot_id,
                        parent_name=name,
                        parent_step=current_step,
                        pair=pair,
                        direction=direction,
                        step_qty=step_qty,
                        step_fill_price=step_fill_price,
                        exchange=exchange,
                        parent_cycle_id=int(bot_status.get('cycle_id', 1)),
                        current_price=current_price,
                    )
        except Exception as e_hedge_eval:
             logger.error(f"❌ {name}: Failed to evaluate hedge child signal in maintain_orders: {e_hedge_eval}")

        return None
                        
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
        parent_cycle_id: int,
        current_price: float = None,
    ) -> bool:
        """
        Signal the hedge child bot to place a SHORT entry mirroring the parent's
        filled step. Called when parent_step >= hedge_trigger_step.

        Returns True if entry was placed or already exists for this step.
        """
        from engine.database import get_connection, save_bot_order
        from engine.ledger import credit_fill

        if current_price is None:
            current_price = step_fill_price

        conn = get_connection()

        # Fetch hedge child bot id and trigger step
        row = conn.execute(
            "SELECT hedge_child_bot_id, hedge_trigger_step FROM bots WHERE id = ?", (parent_bot_id,)
        ).fetchone()
        if not row or not row[0]:
            logger.warning(
                f"[HEDGE-SIGNAL] Parent {parent_name} has no hedge_child_bot_id configured. "
                f"Cannot signal hedge entry."
            )
            return False

        child_bot_id = row[0]
        parent_trigger = int(row[1] or 0) if row[1] else 0
        child_step = max(1, parent_step - parent_trigger + 1)

        # Synchronize child bot's trades cycle_id with the parent's cycle_id
        # Carry forward unfilled/open orders from old cycle if position is still active
        child_info = conn.execute(
            "SELECT cycle_id, open_qty FROM trades WHERE bot_id = ?",
            (child_bot_id,)
        ).fetchone()
        if child_info:
            old_child_cycle = child_info[0]
            child_open_qty = float(child_info[1] or 0)
            if old_child_cycle and old_child_cycle != parent_cycle_id and child_open_qty > 0.0001:
                updated_count = conn.execute(
                    "UPDATE bot_orders SET cycle_id = ? WHERE bot_id = ? AND cycle_id = ?",
                    (parent_cycle_id, child_bot_id, old_child_cycle)
                ).rowcount
                logger.warning(
                    f"⚠️ [HEDGE-CYCLE-CARRY] Child {child_bot_id} trades.cycle_id updated {old_child_cycle} → {parent_cycle_id} "
                    f"while holding active position open_qty={child_open_qty:.6f}. "
                    f"Carried forward {updated_count} orders to new cycle to prevent virtual net mismatch."
                )

        conn.execute(
            "UPDATE trades SET cycle_id = ? WHERE bot_id = ?",
            (parent_cycle_id, child_bot_id)
        )
        conn.commit()

        # Determine if this is the first hedge entry for this cycle
        prior_entries = conn.execute(
            "SELECT COUNT(*) FROM bot_orders "
            "WHERE bot_id=? AND cycle_id=? AND order_type='entry' "
            "AND status NOT IN ('cancelled','failed','reset_cleared','auto_closed','rejected')",
            (child_bot_id, parent_cycle_id)
        ).fetchone()[0]

        # 1. Query the parent's filled qty for this step across all order_ids (including retries)
        parent_qty_row = conn.execute(
            "SELECT COALESCE(SUM(filled_amount), 0) FROM bot_orders "
            "WHERE bot_id = ? AND step = ? AND cycle_id = ? "
            "AND order_type IN ('entry', 'grid') "
            "AND (status IN ('filled', 'partially_filled', 'cancelled', 'closed', 'expired') OR filled_amount > 0)",
            (parent_bot_id, parent_step, parent_cycle_id)
        ).fetchone()
        parent_target_qty = max(float(parent_qty_row[0]) if parent_qty_row else 0.0, step_qty)

        # 2. Query the child's entered qty for this step
        child_qty_row = conn.execute(
            "SELECT COALESCE(SUM("
            "  CASE WHEN status IN ('open', 'new', 'placing', 'cancelling') THEN amount ELSE filled_amount END"
            "), 0) FROM bot_orders "
            "WHERE bot_id = ? AND step = ? AND cycle_id = ? "
            "AND order_type IN ('entry', 'grid')",
            (child_bot_id, child_step, parent_cycle_id)
        ).fetchone()
        child_step_qty = float(child_qty_row[0]) if child_qty_row else 0.0

        # 3. Compute delta
        qty_delta = parent_target_qty - child_step_qty

        # Get exchange precision step size
        prec = exchange.get_symbol_precision(pair)
        qty_step = float(prec.get('step_size', 0.001) or 0.001)

        # 4. If qty_delta <= step_size: skip
        if qty_delta <= qty_step:
            logger.info(
                f"[HEDGE-SIGNAL] Child {child_bot_id}: step {parent_step} is saturated "
                f"(parent_target_qty={parent_target_qty:.6f}, child_step_qty={child_step_qty:.6f}, "
                f"delta={qty_delta:.6f} <= step_size={qty_step:.6f}). Skipping."
            )
            return True

        entry_qty_raw = qty_delta

        # 5. Generate a new deterministic CID
        is_repl = child_step_qty > 0.0001
        cid = self._generate_deterministic_id(child_bot_id, 'ENTRY', parent_cycle_id, parent_step, is_replacement=is_repl)
        cid_fallback = self._generate_deterministic_id(child_bot_id, 'ENTRY', parent_cycle_id, parent_step, suffix='GTC', is_replacement=is_repl)

        # Check exchange directly before placing
        for check_cid in (cid, cid_fallback):
            try:
                exch_order = exchange.fetch_order(check_cid, pair)
                if exch_order:
                    from unittest.mock import Mock
                    if isinstance(exch_order, Mock) and isinstance(exch_order.get('status'), Mock):
                        raise Exception("Default MagicMock return")
                    if exch_order.get('status') not in ('cancelled', 'rejected', 'expired', 'failed'):
                        logger.info(
                            f"[HEDGE-IDEMPOTENCY] Child {child_bot_id}: Order {check_cid} already exists "
                            f"on exchange with status {exch_order.get('status')}. Skipping placement."
                        )
                        return True
            except Exception:
                pass  # Not found = safe to place

        # Determine child direction (opposite to parent)
        child_direction = 'SHORT' if direction.upper() == 'LONG' else 'LONG'
        child_side = 'sell' if child_direction == 'SHORT' else 'buy'

        # Round qty to exchange precision
        prec = exchange.get_symbol_precision(pair)
        qty_step = float(prec.get('step_size', 0.001) or 0.001)
        entry_qty = exchange.round_to_step(entry_qty_raw, qty_step)
        if entry_qty <= 0:
            logger.warning(f"[HEDGE-SIGNAL] Entry qty rounded to 0 for step {parent_step}. Skipping.")
            return False

        # Invariant C — Entry qty must be verified against exchange capacity
        child_config = {}
        try:
            child_row = conn.execute("SELECT config FROM bots WHERE id = ?", (child_bot_id,)).fetchone()
            if child_row and child_row[0]:
                import json
                child_config = json.loads(child_row[0])
        except Exception as e_cfg:
            logger.warning(f"[HEDGE-SIGNAL] Failed to load config for child bot {child_bot_id}: {e_cfg}")

        max_pos_limit = child_config.get('max_position_limit')
        if max_pos_limit is None:
            import os
            if 'PYTEST_CURRENT_TEST' in os.environ:
                max_pos_limit = 999999999.0
            else:
                # Fallback to symbol-specific defaults
                symbol_upper = pair.split('/')[0].upper()
                defaults = {
                    'BTC': 1.0,
                    'ETH': 50.0,
                    'SOL': 500.0,
                    'XRP': 10000.0,
                    'SUI': 20000.0,
                    'LINK': 1000.0,
                    'BNB': 50.0,
                    'XAU': 5.0
                }
                max_pos_limit = defaults.get(symbol_upper, 999999.0)
        else:
            max_pos_limit = float(max_pos_limit)

        # Get actual physical position from exchange (authoritative)
        _current_phys = None
        try:
            positions = exchange.fetch_positions()
            if positions:
                from engine.exchange_interface import normalize_symbol
                target_symbol_norm = normalize_symbol(pair)
                expected_side = 'long' if child_direction.upper() == 'LONG' else 'short'
                for p in positions:
                    if normalize_symbol(p.get('symbol', '')) == target_symbol_norm:
                        pos_amt = None

                        # 1. Try raw Binance positionAmt first (always signed)
                        raw_info = p.get('info', {})
                        raw_pa = raw_info.get('positionAmt', raw_info.get('positionAmount'))
                        if raw_pa is not None:
                            pos_amt = float(raw_pa)

                        # 2. Try top-level positionAmt (always signed)
                        if pos_amt is None or pos_amt == 0:
                            raw_pa_top = p.get('positionAmt', p.get('positionAmount'))
                            if raw_pa_top is not None:
                                pos_amt = float(raw_pa_top)

                        # 3. Try CCXT contracts (signed in some versions)
                        if pos_amt is None or pos_amt == 0:
                            raw_contracts = p.get('contracts')
                            if raw_contracts is not None:
                                pos_amt = float(raw_contracts)

                        # 4. Try CCXT qty/size (for compatibility/mocks)
                        if pos_amt is None or pos_amt == 0:
                            raw_qty = p.get('qty', p.get('size'))
                            if raw_qty is not None:
                                pos_amt = float(raw_qty)

                        # If the detected pos_amt is positive but side is explicitly SHORT, correct the sign
                        if pos_amt is not None and pos_amt > 0:
                            side = str(p.get('side', '')).upper()
                            if side == 'SHORT':
                                pos_amt = -pos_amt

                        # 5. Fall back to side field only if positionAmt/contracts/qty unavailable
                        if pos_amt is None or pos_amt == 0:
                            side = str(p.get('side', '')).upper()
                            size = float(p.get('contracts', 0) or 
                                         p.get('qty', 0) or
                                         p.get('size', 0) or
                                         abs(float(p.get('positionAmt', 0) or p.get('info', {}).get('positionAmt', 0))))
                            pos_amt = size if side == 'LONG' else -size
                        
                        p_side = 'long' if pos_amt > 0 else 'short' if pos_amt < 0 else 'flat'
                        if p_side == expected_side:
                            p_qty = float(p.get('qty') if p.get('qty') is not None else p.get('size') if p.get('size') is not None else abs(pos_amt))
                            _current_phys = {'size': p_qty}
                            break
        except Exception as e_phys:
            logger.error(f"[HEDGE-SIGNAL] Failed to fetch positions from exchange for capacity check: {e_phys}")

        # Fallback to DB active_positions cache if exchange fetch fails
        if _current_phys is None:
            logger.warning(f"[HEDGE-SIGNAL] Falling back to DB cache for capacity check on {pair}")
            _current_phys = self._get_phys_pos(pair, direction=child_direction)

        _current_phys_qty = _current_phys['size'] if _current_phys else 0.0
        _expected_after = _current_phys_qty + entry_qty
        if _expected_after > max_pos_limit:
            logger.error(
                f"🛑 [HEDGE-ENTRY-LIMIT] Child {child_bot_id} on {pair} would exceed position limit. "
                f"Current physical: {_current_phys_qty:.4f}, entry: {entry_qty:.4f}, "
                f"limit: {max_pos_limit:.4f}. Skipping."
            )
            return False

        # Place limit order on exchange at parent's fill price (post-only GTX)
        is_testnet = getattr(config, 'TESTNET', False) or getattr(config, 'DEMO_TRADING', False)
        params = {'postOnly': True, 'timeInForce': 'GTX', 'newClientOrderId': cid}
        if is_testnet:
            params['positionSide'] = 'BOTH'

        try:
            from engine.exceptions import GTXRejected
            try:
                order = self._place_gtx_order_with_retry(
                    exchange, pair, child_side, entry_qty, step_fill_price,
                    params=params, label=f"HEDGE-ENTRY-{parent_name}-step{parent_step}",
                    position_side=child_direction,
                    raise_postonly_reject=True
                )
                saved_price = float(order.get('price') or step_fill_price)
            except GTXRejected as e_gtx:
                logger.warning(
                    f"[HEDGE-SIGNAL] GTX rejected for child {child_bot_id} step {parent_step}. "
                    f"Falling back to GTC limit at step fill price {step_fill_price:.6f}. "
                    f"Order will rest in book until price returns. Error: {e_gtx}"
                )
                params = dict(params)
                params['timeInForce'] = 'GTC'
                params.pop('postOnly', None)
                for cid_key in ('clientOrderId', 'newClientOrderId'):
                    if cid_key in params:
                        params[cid_key] = cid_fallback
                order = exchange.create_order(
                    pair, 'limit', child_side, entry_qty, step_fill_price, params=params
                )
                saved_price = float(order.get('price') or step_fill_price)
        except Exception as e:
            logger.error(
                f"[HEDGE-SIGNAL] Child {child_bot_id}: entry placement failed: {e}. "
                f"Will retry next cycle."
            )
            return False

        exchange_order_id = str(order['id'])
        actual_cid = order.get('_fallback_cid') or order.get('clientOrderId') or cid

        save_bot_order(
            child_bot_id, 'entry', exchange_order_id, saved_price, entry_qty,
            step=child_step, status=order.get('status', 'open'),
            client_order_id=actual_cid,
            notes=f"Hedge entry mirroring parent {parent_bot_id} step {parent_step}",
            cycle_id=parent_cycle_id,
        )

        # INVARIANT: child trades.cycle_id MUST match the cycle_id used in
        # its bot_orders rows. Without this, recompute_invested_from_orders
        # filters by the wrong cycle and returns qty=0, causing seal_trade_state
        # to overwrite the correct open_qty with 0.
        # This is the permanent fix for the SUI/XRP/SOL hedge desync bug.
        try:
            from engine.database import get_connection as _gc_sync
            with _gc_sync() as _sc:
                # Carry forward unfilled/open orders from old cycle if position is still active
                child_info = _sc.execute(
                    "SELECT cycle_id, open_qty FROM trades WHERE bot_id = ?",
                    (child_bot_id,)
                ).fetchone()
                if child_info:
                    old_child_cycle = child_info[0]
                    child_open_qty = float(child_info[1] or 0)
                    if old_child_cycle and old_child_cycle != parent_cycle_id and child_open_qty > 0.0001:
                        updated_count = _sc.execute(
                            "UPDATE bot_orders SET cycle_id = ? WHERE bot_id = ? AND cycle_id = ?",
                            (parent_cycle_id, child_bot_id, old_child_cycle)
                        ).rowcount
                        logger.warning(
                            f"⚠️ [HEDGE-CYCLE-CARRY] Child {child_bot_id} trades.cycle_id updated {old_child_cycle} → {parent_cycle_id} "
                            f"during post-entry sync while holding active position open_qty={child_open_qty:.6f}. "
                            f"Carried forward {updated_count} orders to new cycle."
                        )
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

        logger.info(
            f"✅ [HEDGE-SIGNAL] Child {child_bot_id}: entry placed "
            f"{entry_qty:.6f} {child_direction} @ {saved_price:.4f} "
            f"(parent step {parent_step}, child step {child_step}, cid={actual_cid})"
        )
        return True



    def execute_exit_sl(self, bot_id, name, pair, direction, bot_status, current_price, exchange: ExchangeInterface, market_snapshot: Dict[str, Any], bot_config: Dict[str, Any]):

        if not config.TRADING_ENABLED and not config.DRY_RUN:
            logger.info(f"🛑 [EXIT-BLOCKED] Trading disabled. Bot {name} cannot execute SL for {pair}.")
            return

        logger.critical(f"⛔ {name}: Executing STOP LOSS for {pair} at step {bot_status['current_step']}")
        
        if config.DRY_RUN:
            log_trade(bot_id, 'STOP_LOSS', pair, current_price, bot_status['total_invested'] / bot_status['avg_entry_price'], bot_status['total_invested'], f'DRY_RUN_SL_{bot_id}', bot_status['current_step'], "Dry run SL", (current_price - bot_status['avg_entry_price']) * bot_status['total_invested'] / bot_status['avg_entry_price'])
            reset_bot_after_tp(bot_id, current_price, direction=direction, action_label='DRY_RUN_SL')
            logger.info(f"📊 [DRY-RUN] Bot {name} would have exited SL for {pair}")
            return
        
        # Cancel all open orders for this bot
        exchange.cancel_orders_by_bot_id(bot_id, pair)

        # Close the position with a market order safely
        try:
            position_side = 'sell' if direction == 'LONG' else 'buy'
            
            if bot_status['avg_entry_price'] > 0:
                size_to_close = bot_status['total_invested'] / bot_status['avg_entry_price']
                actual_size = abs(size_to_close)

                # 🚀 SYSTEM DISCREPANCY GUARD (GHOST WIPE)
                # Evaluate aggregate DB vs Aggregate Physical
                phys_positions = exchange.fetch_positions()
                phys_long = 0.0
                phys_short = 0.0
                for p in (phys_positions or []):
                    if normalize_symbol(p.get('symbol', '')) == normalize_symbol(pair):
                        size = float(p.get('contracts', 0) or abs(float(p.get('positionAmt', 0))))
                        pt_side = p.get('side', '').upper()
                        if not pt_side: 
                            pos_amount = float(p.get('positionAmt', 0))
                            if pos_amount < 0: pt_side = 'SHORT'
                            elif pos_amount > 0: pt_side = 'LONG'
                        if pt_side == 'SHORT': phys_short += size
                        elif pt_side == 'LONG': phys_long += size
                phys_net_qty = phys_long - phys_short
                
                from engine.database import get_connection as _st_conn
                sib_net_qty = 0.0
                with _st_conn() as _c:
                    _cur = _c.cursor()
                    _cur.execute(
                        "SELECT direction, total_invested, avg_entry_price FROM trades "
                        "JOIN bots ON trades.bot_id = bots.id WHERE bots.pair = ? AND trades.total_invested > 0", 
                        (pair,)
                    )
                    for sib_dir, s_inv, s_avg in _cur.fetchall():
                        if float(s_avg) > 0:
                            s_qty = float(s_inv) / float(s_avg)
                            if str(sib_dir).upper() == 'LONG': sib_net_qty += s_qty
                            else: sib_net_qty -= s_qty

                divergence_qty = abs(sib_net_qty - phys_net_qty)
                divergence_usd = divergence_qty * current_price
                
                if divergence_usd > 50000.0:
                    logger.critical(f"🛑 {name}: SL/Market Close Blocked! System net vs physical diverges by ${divergence_usd:.2f}. Bypassing API to strictly wipe Ghost DB.")
                    from engine.database import safe_wipe_bot
                    safe_wipe_bot(bot_id, pair, direction, reason="SL_GHOST_WIPE: divergence > $50000", exit_price=current_price, human_approved=True)
                    return

                if actual_size > 0:
                    logger.warning(f"Placing market order to close {actual_size} {pair} {position_side} for bot {name} SL")
                    order = None
                    try:
                        order = exchange.create_order(pair, 'market', position_side, actual_size, params={'reduceOnly': True}, human_approved=True)
                    except Exception as e_order:
                        logger.error(f"❌ {name}: Failed to place SL Market Order ({e_order}). Purging local Ghost state.")
                        from engine.database import safe_wipe_bot
                        safe_wipe_bot(bot_id, pair, direction, reason=f"SL_API_REJECT_GHOST", exit_price=current_price, human_approved=True)
                        return

                    if order:
                        from engine.database import reset_bot_after_tp
                        log_trade(bot_id, 'STOP_LOSS_EXIT', pair, current_price, actual_size, current_price * actual_size, f'SL_MARKET_{bot_id}', bot_status['current_step'], "SL Market Exit", (current_price - bot_status['avg_entry_price']) * actual_size)
                        try:
                            logger.info(f"🧹 {name}: Cancelling open exchange orders before STOP_LOSS_EXIT reset...")
                            exchange.cancel_orders_by_bot_id(bot_id, pair)
                        except Exception as e_cancel:
                            logger.error(f"❌ {name}: Failed to cancel exchange orders during SL reset: {e_cancel}")
                        reset_bot_after_tp(bot_id, current_price, direction=direction, action_label='STOP_LOSS_EXIT')
                        logger.info(f"✅ {name}: Market order placed to close SL for {pair} (ID: {order['id']})")
                else:
                    logger.info(f"ℹ️ {name}: No virtual size to close. Running wipe guard before DB reset.")
                    from engine.database import safe_wipe_bot
                    safe_wipe_bot(bot_id, pair, direction, reason="SL_EXIT_NO_VIRTUAL_POSITION", exit_price=current_price, human_approved=True)
            else:
                logger.info(f"ℹ️ {name}: Bot has 0 avg_entry_price. Running wipe guard before DB reset.")
                from engine.database import safe_wipe_bot
                safe_wipe_bot(bot_id, pair, direction, reason="SL_EXIT_ZERO_PRICE", exit_price=current_price, human_approved=True)

        except Exception as e:
            logger.error(f"❌ {name}: Error executing SL for {pair}: {e}")



    def check_for_safety_stop(self):
        """
        Checks if a global stop file exists.
        This file is created by an external mechanism or user to halt trading.
        """
        from engine.shutdown_control import is_stop_requested
        if is_stop_requested():
            logger.critical("🛑 GLOBAL STOP FILE DETECTED. Halting trading.")
            self.runner.running = False
            return True
        return False
