"""
WebSocket Event Handlers (v2.0)

Processes real-time events from Binance WebSocket stream:
- Order updates (fill, cancel, new)
- Position updates
- Balance updates

v2.0 Architecture:
  ALL fills are recorded exclusively via ledger.credit_fill() → bot_orders.
  trades table is updated via ledger.seal_trade_state() (idempotent, enqueued).
  TP fills register in ledger._tp_cascade_registry.
  Runner.run_cycle() drains the TP registry and calls handle_tp_completion().
  No accumulate_trade_fill() calls. No upsert_active_position_for_bot() calls.
"""

import logging
import queue
import threading
import time
from typing import Dict, Callable

from engine.ws_cache import get_ws_cache

logger = logging.getLogger("WSEventHandlers")

# ---------------------------------------------------------------------------
# ⚡ ASYNC DB WRITE QUEUE
# ---------------------------------------------------------------------------
# All SQLite mutations from the WS path go through this queue/thread so the
# CCXT listener is never paused by disk I/O.
# ---------------------------------------------------------------------------
_db_write_queue: queue.Queue = queue.Queue(maxsize=2000)
_db_worker_thread: threading.Thread | None = None
_db_worker_stop = threading.Event()


def _db_worker_loop():
    """Background thread: drain the write queue and execute each task."""
    while not _db_worker_stop.is_set():
        try:
            fn, args, kwargs = _db_write_queue.get(timeout=0.5)
            try:
                fn(*args, **kwargs)
            except Exception as e:
                logger.error(f"[DB-WORKER] Task failed: {fn.__name__} — {e}")
            finally:
                _db_write_queue.task_done()
        except queue.Empty:
            continue


def _enqueue_db_write(fn: Callable, *args, **kwargs):
    """Submit a database write task to the background worker queue."""
    try:
        _db_write_queue.put_nowait((fn, args, kwargs))
    except queue.Full:
        logger.warning(f"[DB-WORKER] Queue full — executing {fn.__name__} synchronously")
        fn(*args, **kwargs)  # Fallback: execute inline rather than drop data


def start_db_worker():
    """Start the background DB worker thread (idempotent — safe to call multiple times)."""
    global _db_worker_thread
    if _db_worker_thread is None or not _db_worker_thread.is_alive():
        _db_worker_stop.clear()
        _db_worker_thread = threading.Thread(
            target=_db_worker_loop, name="WSDBWorker", daemon=True
        )
        _db_worker_thread.start()
        logger.info("[DB-WORKER] Async SQLite write worker started.")


def stop_db_worker(timeout: float = 5.0):
    """Gracefully flush the queue and stop the worker."""
    _db_worker_stop.set()
    try:
        _db_write_queue.join()  # Wait for all tasks to complete
    except Exception:
        pass
    if _db_worker_thread:
        _db_worker_thread.join(timeout=timeout)
    logger.info("[DB-WORKER] Async SQLite write worker stopped.")


# ---------------------------------------------------------------------------
# ⚡ TERMINAL ORDER CACHE
# ---------------------------------------------------------------------------
# Orders in terminal states (filled / cancelled) are immutable.  Caching
# their IDs prevents the reconciler from re-fetching them over the wire.
# ---------------------------------------------------------------------------
_terminal_order_ids: set = set()          # exchange order IDs confirmed terminal
_terminal_order_ids_lock = threading.Lock()


def mark_terminal_order(order_id) -> None:
    """Record an order ID as terminal so it is never re-fetched from the exchange."""
    with _terminal_order_ids_lock:
        _terminal_order_ids.add(str(order_id))


def is_terminal_order(order_id) -> bool:
    """Return True if the order ID is already confirmed terminal (cached)."""
    with _terminal_order_ids_lock:
        return str(order_id) in _terminal_order_ids


def get_terminal_order_cache_size() -> int:
    with _terminal_order_ids_lock:
        return len(_terminal_order_ids)


# ---------------------------------------------------------------------------
# ⚡ PENDING FILL RETRY QUEUE — credit_fill race window fix
# ---------------------------------------------------------------------------
# Problem: a taker order fills on the exchange before BotExecutor's
# save_bot_order() has committed the bot_orders row. The WS FILL event
# arrives, credit_fill() finds no matching row, returns False. The old code
# enqueued a single 0.5 s deferred retry; if that also failed it fell through
# to _attribute_orphan_fill() which — with ALLOW_FORENSIC_ADOPT=False —
# silently dropped the fill, leaving the ledger understating the position.
#
# Fix: store the full fill payload here. _drain_pending_fills() is called at
# the top of every handle_order_update() invocation so retries fire every WS
# cycle (~1 s) without blocking the listener. After PENDING_FILL_MAX_RETRIES
# exhausted we escalate to REQUIRE_MANUAL_PROOF — never silent drop.
# This queue is proof-based (real exchange order_id), not forensic invention,
# and therefore operates regardless of ALLOW_FORENSIC_ADOPT.
# ---------------------------------------------------------------------------
PENDING_FILL_MAX_RETRIES: int = 3          # ~3 WS cycles ≈ 3 s
PENDING_FILL_MAX_AGE_S: int   = 30         # Hard safety ceiling — escalate after 30 s no matter what
_pending_fills: dict = {}                  # order_id → payload dict
_pending_fills_lock = threading.Lock()


def _handle_fill_with_pending_retry(
    bot_id: int, order_id: str, client_id: str,
    qty: float, price: float, order_type: str,
    fill_ts: int, symbol: str,
) -> None:
    """
    Attempt credit_fill immediately. If no DB row exists yet (race), enqueue
    in _pending_fills for retry on the next WS cycle rather than dropping or
    invoking forensic adoption.
    """
    credited = _credit_fill_with_retry(bot_id, order_id, client_id, qty, price, order_type, fill_ts)
    if credited:
        from engine.ledger import seal_trade_state
        _enqueue_db_write(seal_trade_state, bot_id)
        logger.info(f"[WS-FILL] Bot {bot_id} {order_type}: credit_fill OK → seal enqueued.")
        return

    # Row not committed yet — park in retry queue
    with _pending_fills_lock:
        if order_id not in _pending_fills:
            _pending_fills[order_id] = {
                'bot_id':     bot_id,
                'client_id':  client_id,
                'qty':        qty,
                'price':      price,
                'order_type': order_type,
                'fill_ts':    fill_ts,
                'symbol':     symbol,
                'retries':    0,
                'first_seen': int(time.time()),
            }
    logger.warning(
        f"[WS-FILL] Bot {bot_id} {order_type}: credit_fill returned False "
        f"(race — DB row not yet committed). Parked in retry queue for order {order_id}."
    )


def _credit_fill_with_retry(bot_id: int, order_id: str, client_id: str,
                            qty: float, price: float, order_type: str,
                            fill_ts: int) -> bool:
    """
    Try credit_fill by exchange order_id first, then by client_order_id.
    Returns True on success.
    """
    try:
        from engine.ledger import credit_fill
        ok = credit_fill(
            bot_id=bot_id, order_id=order_id,
            cumulative_qty=qty, avg_price=price,
            order_type=order_type, is_cumulative=True, fill_ts=fill_ts,
        )
        if ok:
            return True
        # Fallback: try client_order_id (exchange may not have stamped order_id yet)
        if client_id and client_id != order_id:
            ok = credit_fill(
                bot_id=bot_id, order_id=client_id,
                cumulative_qty=qty, avg_price=price,
                order_type=order_type, is_cumulative=True, fill_ts=fill_ts,
            )
        return ok
    except Exception as e:
        logger.error(f"[CREDIT-FILL-RETRY] Bot {bot_id} order {order_id}: {e}")
        return False


def _drain_pending_fills() -> None:
    """
    Re-attempt credit_fill for all pending fills. Called at the top of every
    handle_order_update() so retries happen on the next WS cycle (~1 s gap).

    Retry logic:
      - Success → remove from queue, enqueue seal_trade_state.
      - retries < PENDING_FILL_MAX_RETRIES and age < PENDING_FILL_MAX_AGE_S → increment retries.
      - Exhausted → flag_orphan_fill_manual_proof + remove (no silent drop).
    """
    if not _pending_fills:
        return

    now = int(time.time())
    to_remove = []
    to_escalate = []

    with _pending_fills_lock:
        items = list(_pending_fills.items())

    for order_id, pf in items:
        bid        = pf['bot_id']
        client_id  = pf['client_id']
        qty        = pf['qty']
        price      = pf['price']
        otype      = pf['order_type']
        fill_ts    = pf['fill_ts']
        symbol     = pf['symbol']
        retries    = pf['retries']
        first_seen = pf['first_seen']
        age        = now - first_seen

        # Hard ceiling — something is very wrong if still unresolved after 30 s
        if retries >= PENDING_FILL_MAX_RETRIES or age >= PENDING_FILL_MAX_AGE_S:
            to_escalate.append((order_id, bid, client_id, qty, price, otype, fill_ts, symbol, retries, age))
            continue

        credited = _credit_fill_with_retry(bid, order_id, client_id, qty, price, otype, fill_ts)
        if credited:
            from engine.ledger import seal_trade_state
            _enqueue_db_write(seal_trade_state, bid)
            logger.info(
                f"[PENDING-FILL-RETRY] Bot {bid} {otype}: credited on retry #{retries + 1} "
                f"for order {order_id}."
            )
            to_remove.append(order_id)
        else:
            with _pending_fills_lock:
                if order_id in _pending_fills:
                    _pending_fills[order_id]['retries'] += 1
            logger.warning(
                f"[PENDING-FILL-RETRY] Bot {bid} {otype}: retry #{retries + 1} still no DB row "
                f"for order {order_id}. {PENDING_FILL_MAX_RETRIES - retries - 1} attempt(s) left."
            )

    # Remove successfully credited fills
    with _pending_fills_lock:
        for oid in to_remove:
            _pending_fills.pop(oid, None)

    # Escalate exhausted fills — REQUIRE_MANUAL_PROOF (never silent drop)
    for order_id, bid, client_id, qty, price, otype, fill_ts, symbol, retries, age in to_escalate:
        logger.error(
            f"[PENDING-FILL-EXHAUSTED] Bot {bid} {otype} order {order_id}: "
            f"credit_fill failed after {retries} retries ({age}s elapsed). "
            f"Escalating to REQUIRE_MANUAL_PROOF — fill qty={qty:.6f} @ {price:.4f} NOT dropped."
        )
        try:
            from engine.parity_gates import flag_orphan_fill_manual_proof
            flag_orphan_fill_manual_proof(bid, order_id, symbol, qty, 'pending_fill_exhausted')
        except Exception as _e:
            logger.error(f"[PENDING-FILL-ESCALATE] flag_orphan_fill_manual_proof failed: {_e}")
        with _pending_fills_lock:
            _pending_fills.pop(order_id, None)


# Auto-start the worker when this module is imported
start_db_worker()

# ---------------------------------------------------------------------------
# Deduplication set for notifications
_notified_fills = set()
_notified_fills_timestamps = {}

# ---------------------------------------------------------------------------
# Partial fill accumulator
# Tracks the cumulative filled qty seen so far per (bot_id, order_id) key.
# When a FILLED event arrives, we compute incremental_qty = filled - prev_tracked.
# Cleared on FILLED or CANCELLED (terminal events).
_partial_fill_tracker: dict = {}  # key: f"{bot_id}_{order_id}" → float cumulative_qty

# v2.0: TP cascade registry is now managed in engine.ledger
# get_pending_cancel_after_tp is kept for backward compatibility but now
# delegates to the ledger registry.
def get_pending_cancel_after_tp() -> set:
    """Return and clear the TP cascade registry (now managed by engine.ledger)."""
    try:
        from engine.ledger import drain_tp_cascade
        return drain_tp_cascade()
    except ImportError:
        return set()

_notified_fills_max_size = 10000


def _attribute_orphan_fill(bot_id: int, order_id: str, client_id: str, qty: float, price: float, order_type: str, fill_ts: int, symbol: str):
    """
    Handles fills for CQB_ orders that are missing from bot_orders DB.
    Forensic adopt is disabled by default — flags REQUIRE_MANUAL_PROOF instead.
    """
    from engine.parity_gates import forensic_adopt_allowed, flag_orphan_fill_manual_proof
    if not forensic_adopt_allowed():
        flag_orphan_fill_manual_proof(bot_id, order_id, symbol, qty, 'orphan_ws')
        return False

    logger.warning(f"🕵️ [ORPHAN-RECOVERY] Bot {bot_id}: Order {order_id}/{client_id} missing from DB. Adopting forensically.")
    try:
        from engine.database import get_connection
        from engine.ledger import seal_trade_state
        conn = get_connection()
        
        # Get bot's current cycle/step to anchor the adoption
        bot_info = conn.execute("""
            SELECT t.cycle_id, t.current_step, b.direction 
            FROM trades t 
            JOIN bots b ON b.id = t.bot_id 
            WHERE t.bot_id = ?
        """, (bot_id,)).fetchone()
        cycle_id = bot_info[0] if bot_info else -1
        step = bot_info[1] if bot_info else 0
        direction = bot_info[2] if bot_info else 'LONG'
        
        # side from direction
        side = 'LONG' if direction == 'LONG' else 'SHORT'
             
        # Insert the missing row
        conn.execute("""
            INSERT INTO bot_orders (
                bot_id, order_type, order_id, client_order_id, 
                price, amount, filled_amount, status, 
                cycle_id, step, position_side, 
                created_at, updated_at, filled_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bot_id, f"forensic_adoption_{order_type.lower()}", order_id, client_id,
            price, qty, qty, 'filled',
            cycle_id, step, side,
            int(time.time()), int(time.time()), fill_ts,
            f"Forensic Recovery: Missing {order_type} record adopted from WS."
        ))
        conn.commit()
        conn.close()
        
        # Now that the row exists, seal the trade state
        _enqueue_db_write(seal_trade_state, bot_id)
        logger.info(f"✅ [ORPHAN-RECOVERY] Bot {bot_id}: Adopted {qty:.6f} @ {price:.4f}. Ledger truth restored.")
        return True
    except Exception as e:
        logger.error(f"❌ [ORPHAN-RECOVERY] Failed to adopt orphan fill for bot {bot_id}: {e}")
        return False


def _attribute_anonymous_fill(event: Dict):
    """
    Scans active bots for matching symbol/side to adopt non-CQB fills.
    Disabled when ALLOW_FORENSIC_ADOPT=False (proof-only mode).
    """
    from engine.parity_gates import forensic_adopt_allowed
    if not forensic_adopt_allowed():
        logger.warning(
            f"[ANONYMOUS-NO-ADOPT] Non-CQB fill {event.get('order_id')} on {event.get('symbol')} "
            f"— forensic adopt disabled. Use trade-history proof or proof flatten."
        )
        return False

    symbol = event.get('symbol')
    side = event.get('side', '').upper() # BUY or SELL
    qty = float(event.get('filled_qty', 0))
    price = float(event.get('avg_price', 0) or event.get('price', 0))
    order_id = str(event.get('order_id', ''))
    client_id = str(event.get('client_order_id', ''))
    
    if qty <= 0: return

    logger.info(f"🕵️ [ANONYMOUS-SCAN] Checking bots for {symbol} {side} fill ({qty} @ {price})...")
    
    try:
        from engine.database import get_connection
        from engine.exchange_interface import normalize_symbol
        from engine.ledger import seal_trade_state
        conn = get_connection()

        # ── SYMBOL NORMALISATION FIX ────────────────────────────────────────────
        # The WebSocket delivers the raw Binance symbol (e.g. 'SOLUSDC').
        # bots.pair stores the CCXT-unified format ('SOL/USDC:USDC').
        # Match on the normalised form so the lookup never returns zero rows.
        raw_symbol = symbol  # e.g. 'SOLUSDC'
        active_bots = conn.execute("""
            SELECT b.id, b.name, b.direction, t.cycle_id, t.current_step, b.pair
            FROM bots b
            JOIN trades t ON t.bot_id = b.id
            WHERE b.is_active = 1
              AND (
                b.pair = ?                                   -- CCXT unified: 'SOL/USDC:USDC'
                OR REPLACE(REPLACE(REPLACE(b.pair,'/',''),(SELECT '' WHERE 1),':USDC'),':USDT','') = ?  -- rough strip
              )
        """, (raw_symbol, raw_symbol)).fetchall()

        # Fallback: normalise every bot pair and compare
        if not active_bots:
            all_bots = conn.execute("""
                SELECT b.id, b.name, b.direction, t.cycle_id, t.current_step, b.pair
                FROM bots b
                JOIN trades t ON t.bot_id = b.id
                WHERE b.is_active = 1
            """).fetchall()
            active_bots = [
                row for row in all_bots
                if normalize_symbol(row[5]) == normalize_symbol(raw_symbol)
            ]

        if not active_bots:
            logger.warning(f"[ANONYMOUS-ADOPT] No active bots found for symbol '{raw_symbol}' — fill {order_id} unattributed.")
            conn.close()
            return False

        for bid, name, direction, cycle_id, step, _pair in active_bots:
            # Only adopt if this bot has NO open orders (deadlock / orphan sign)
            open_orders_count = conn.execute(
                "SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status IN ('new', 'open')",
                (bid,)
            ).fetchone()[0]

            if open_orders_count == 0:
                is_entry = (direction == 'LONG' and side == 'BUY') or (direction == 'SHORT' and side == 'SELL')
                is_exit  = (direction == 'LONG' and side == 'SELL') or (direction == 'SHORT' and side == 'BUY')

                if is_entry or is_exit:
                    otype = "entry" if is_entry else "tp"
                    logger.warning(
                        f"🤝 [ANONYMOUS-ADOPT] Bot {name} ({bid}) adopting anonymous "
                        f"{side} fill {order_id} (CID={client_id}) as {otype} qty={qty} @ {price}."
                    )

                    event_ts_ms = event.get('lastTradeTimestamp') or event.get('timestamp') or 0
                    fill_ts = int(event_ts_ms / 1000) if event_ts_ms else int(time.time())

                    # ── ATOMIC WRITE WITH PROOF METADATA ────────────────────────
                    # wipe_proof_source = 'forensic_adopt' so future audits never
                    # classify this row as a SUSPECT_WIPE legacy row.
                    conn.execute("""
                        INSERT OR IGNORE INTO bot_orders (
                            bot_id, order_type, order_id, client_order_id,
                            price, amount, filled_amount, status,
                            cycle_id, step, position_side,
                            created_at, updated_at, filled_at,
                            wipe_proof_source, notes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        bid, f"forensic_adoption_{otype}", order_id, client_id,
                        price, qty, qty, 'filled',
                        cycle_id, step, 'LONG' if direction == 'LONG' else 'SHORT',
                        int(time.time()), int(time.time()), fill_ts,
                        'forensic_adopt',
                        f"Anonymous Adoption (WS): orphan {side} fill {order_id} adopted at runtime. "
                        f"CID={client_id} qty={qty} @ {price} fill_ts={fill_ts}."
                    ))
                    conn.commit()
                    conn.close()
                    _enqueue_db_write(seal_trade_state, bid)
                    logger.info(
                        f"✅ [ANONYMOUS-ADOPT] Bot {bid}: adopted {qty:.4f} @ {price:.4f} "
                        f"(order_id={order_id}) — proof=forensic_adopt."
                    )
                    return True

        conn.close()
    except Exception as e:
        logger.error(f"[ANONYMOUS-ADOPT] Error adopting fill {order_id} for symbol '{symbol}': {e}", exc_info=True)
    return False


def handle_order_update(event: Dict):
    """
    Handle real-time order update from WebSocket.
    
    Event structure:
    {
        'event': 'order_update',
        'symbol': 'BTCUSDC',
        'side': 'BUY/SELL',
        'status': 'NEW/FILLED/CANCELED/EXPIRED',
        'order_id': 123456,
        'client_order_id': 'CQB_37_GRID_abc123',
        'price': 78000.0,
        'qty': 0.002,
        'filled_qty': 0.002,
        'avg_price': 78000.5,
        'realized_pnl': 12.50,
        'timestamp': 1234567890
    }
    """
    # ── DRAIN PENDING FILL RETRY QUEUE ─────────────────────────────────────
    # Re-attempt credit_fill for any fills that raced ahead of save_bot_order().
    # This is the first thing we do on every WS cycle so retries get the full
    # remaining cycle budget without blocking the listener.
    _drain_pending_fills()
    # ────────────────────────────────────────────────────────────────────────

    try:
        status = event.get('status')
        client_id = event.get('client_order_id', '')
        order_id = event.get('order_id')
        symbol = event.get('symbol')
        
        # Only process bot orders (tagged with CQB_)
        if not client_id.startswith('CQB_'):
            # 🚀 ANONYMOUS ATTRIBUTION (Phase 1)
            # If a fill arrives without a CQB tag, it might be a manual trade
            # or a bot order that lost its tag. Attempt to attribute to a deadlocked bot.
            if status.upper() in ('FILLED', 'PARTIALLY_FILLED'):
                _attribute_anonymous_fill(event)
            else:
                logger.debug(f"⏭️ WS Ignoring non-bot order {order_id} (CID: {client_id})")
            return
            
        # Parse bot ID from clientOrderId
        # Format: CQB_{bot_id}_{type}_{uuid}
        parts = client_id.split('_')
        if len(parts) < 3:
            logger.warning(f"⚠️ WS Invalid clientOrderId format: {client_id}")
            return
            
        bot_id = int(parts[1])
        order_type = parts[2]  # ENTRY, TP, GRID
        
        logger.debug(f"📬 WS Processing {order_type} for Bot {bot_id} (Status: {status})")

        # 🕒 HISTORICAL EVENT GUARD: Reject genuinely old events from before engine startup.
        # CCXT watch_orders() replays recent history on connect. reconstruct_offline_fills()
        # already handles fills > 30 minutes old at startup. But fills that arrived DURING
        # the startup sequence (after order placement, before WS connected) fall into a gap:
        # too recent for reconstruct_offline_fills to have caught, but timestamped before
        # ENGINE_START_TIME, so the old 5s guard silently dropped them.
        #
        # FIX: Only discard fills older than 30 minutes before startup. Anything in the
        # last 30 minutes passes through — credit_fill() is idempotent so double-crediting
        # is safe (MAX-fill protection prevents quantity inflation).
        HISTORICAL_GUARD_MS = 30 * 60 * 1000  # 30 minutes in milliseconds
        event_timestamp = event.get('timestamp')
        if event_timestamp:
            try:
                from engine.reconciler import ENGINE_START_TIME
                if event_timestamp < (ENGINE_START_TIME * 1000) - HISTORICAL_GUARD_MS:
                    logger.debug(
                        f"⏭️ WS Ignoring genuinely historical order {order_id} "
                        f"(fill_ts={event_timestamp}, engine_start={ENGINE_START_TIME * 1000}, "
                        f"gap>{HISTORICAL_GUARD_MS}ms). reconstruct_offline_fills handles these."
                    )
                    return
            except ImportError:
                pass

        
        # FUNDAMENTAL SAFETY CHECK: Is Bot Active?
        # If we process a fill for an inactive bot, we might trigger new orders (Grid/TP)
        from engine.database import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM bots WHERE id = ?", (bot_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row or not row[0]:
            logger.warning(f"⛔ WS IGNORING Event for INACTIVE Bot {bot_id} (ClientID: {client_id})")
            return
        
        # Standardize status for robust matching
        status_upper = status.upper() if status else ""
        
        if status_upper in ['FILLED', 'CLOSED']:
            # Final fill — clean up partial tracker and calculate final incremental piece
            tracker_key = f"{bot_id}_{order_id}"
            cumulative_filled = float(event.get('filled_qty', 0) or 0)
            
            if tracker_key not in _partial_fill_tracker:
                try:
                    from engine.database import get_connection
                    conn = get_connection()
                    db_filled = conn.execute("SELECT filled_amount FROM bot_orders WHERE order_id = ? OR client_order_id = ?", (str(order_id), str(client_id))).fetchone()
                    if db_filled and db_filled[0] is not None:
                        _partial_fill_tracker[tracker_key] = float(db_filled[0])
                except Exception as e_pf:
                    logger.debug(f"[PF-SYNC] Failed to sync tracker for {tracker_key}: {e_pf}")
                    
            prev_filled = _partial_fill_tracker.pop(tracker_key, 0.0)
            incremental_qty = cumulative_filled - prev_filled
            
            # Pass incremental quantity so _handle_order_filled doesn't double-count
            event['incremental_qty'] = incremental_qty
            
            _handle_order_filled(bot_id, order_type, event)

        elif status_upper == 'PARTIALLY_FILLED':
            _handle_order_partial_fill(bot_id, order_type, event)

        elif status_upper in ['CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED']:
            # Order cancelled after partial fill — clean tracker, keep what was already accumulated
            tracker_key = f"{bot_id}_{order_id}"
            prev_qty = _partial_fill_tracker.pop(tracker_key, 0.0)
            if prev_qty > 0:
                logger.info(f"📋 WS Cancel after partial fill: Bot {bot_id} {order_type} had {prev_qty:.6f} already accumulated")
            _handle_order_canceled(bot_id, order_type, event)

        elif status_upper == 'NEW':
            _handle_order_new(bot_id, order_type, event)
            
        # 🚀 WS CACHING: Keep our memory snapshot alive
        ws_cache = get_ws_cache()
        
        # 🔧 CCXT COMPATIBILITY: Map WebSocket keys to CCXT format for BotExecutor
        if 'clientOrderId' not in event:
            event['clientOrderId'] = client_id
        if 'id' not in event:
            event['id'] = str(order_id)
            
        ws_cache.update_order(order_id, event)
            
    except Exception as e:
        logger.error(f"Error handling order update: {e}")


# Module-level set to track notified order fills (prevents duplicates)

def _cleanup_notified_fills():
    """Cleanup notified fills set if it grows too large."""
    global _notified_fills
    if len(_notified_fills) > _notified_fills_max_size:
        # Keep most recent 50%, clear old ones
        logger.info(f"🧹 Cleaning up notified_fills set (size: {len(_notified_fills)})")
        _notified_fills = set(list(_notified_fills)[-5000:])

def _handle_order_partial_fill(bot_id: int, order_type: str, event: Dict):
    """
    v2.0: Handle PARTIALLY_FILLED events via ledger.credit_fill() exclusively.

    credit_fill() uses MAX() protection — multiple partial events for the same
    order are idempotent. Only the highest cumulative_qty ever gets written,
    so double-processing is impossible.

    After crediting the fill, seal_trade_state() is enqueued to recompute
    the trades table from the updated bot_orders ledger.
    """
    order_id = str(event.get('order_id', ''))
    client_id = str(event.get('client_order_id', event.get('clientOrderId', '')))
    raw_avg_price = float(event.get('avg_price', 0) or 0)
    raw_limit_price = float(event.get('price', 0) or 0)
    avg_price = raw_avg_price if raw_avg_price > 0 else raw_limit_price
    cumulative_filled = float(event.get('filled_qty', 0) or 0)
    symbol = event.get('symbol', '')

    if avg_price <= 0 or cumulative_filled <= 0:
        return

    logger.info(
        f"⚡ WS PARTIAL FILL: Bot {bot_id} {order_type} "
        f"cumulative={cumulative_filled:.6f} @ {avg_price:.6f} (order={order_id})"
    )

    # --- v2.0 path: credit_fill → seal_trade_state ---
    try:
        from engine.ledger import credit_fill, seal_trade_state

        # Extract exchange fill timestamp (ms) and convert to seconds for filled_at
        event_ts_ms = event.get('lastTradeTimestamp') or event.get('timestamp') or 0
        fill_ts = int(event_ts_ms / 1000) if event_ts_ms else 0

        # Use client_order_id as lookup key (works even if exchange order_id not yet stamped)
        lookup_id = order_id if order_id else client_id
        credited = credit_fill(
            bot_id=bot_id,
            order_id=lookup_id,
            cumulative_qty=cumulative_filled,
            avg_price=avg_price,
            order_type=order_type.lower(),
            is_cumulative=True,
            fill_ts=fill_ts
        )

        if credited:
            # Track cumulative so FILLED handler computes correct incremental_qty
            tracker_key = f"{bot_id}_{order_id}"
            _partial_fill_tracker[tracker_key] = cumulative_filled
            # Enqueue idempotent state recompute (non-blocking)
            _enqueue_db_write(seal_trade_state, bot_id)
            logger.debug(f"[PARTIAL] Bot {bot_id}: credit_fill + seal_trade_state enqueued (cumulative={cumulative_filled:.6f}).")

    except Exception as e:
        logger.error(f"[PARTIAL] Failed to credit partial fill for bot {bot_id}: {e}")



def _handle_order_filled(bot_id: int, order_type: str, event: Dict):
    """
    Process a fully-filled order update.

    v2.0 Architecture:
      - credit_fill + seal_trade_state ALWAYS run (idempotent, safe to call twice)
      - Dedup gate only prevents duplicate NOTIFICATIONS, not state updates
      - This ensures WS replay / double-emit never loses a fill
    """
    from engine.database import (
        get_bot_order_ids, log_trade, add_notification
    )

    order_id = event.get('order_id')
    raw_avg_price = float(event.get('avg_price', 0) or 0)
    raw_price = float(event.get('price', 0) or 0)
    avg_price = raw_avg_price if raw_avg_price > 0 else raw_price

    # Always use total cumulative fill for credit_fill (it uses MAX protection)
    cumulative_fill_qty = float(event.get('filled_qty', 0) or 0)
    realized_pnl = float(event.get('realized_pnl', 0) or 0)
    symbol = event.get('symbol')
    client_id = str(event.get('client_order_id', event.get('clientOrderId', '')))

    logger.info(
        f"[WS-FILL] Bot {bot_id} {order_type} FILLED @ {avg_price:.6f} "
        f"qty={cumulative_fill_qty:.6f} order={order_id}"
    )

    # Mark order terminal immediately so reconciler doesn't re-fetch it
    mark_terminal_order(order_id)

    # ── STATE UPDATE (always runs — credit_fill is idempotent) ─────────────
    if order_type in ('TP',):
        # v2.0: TP hit — credit fill then register cascade for runner
        try:
            from engine.ledger import register_tp_cascade, credit_fill
            # Extract exchange fill timestamp for filled_at + cycle_start_time anchor
            event_ts_ms = event.get('lastTradeTimestamp') or event.get('timestamp') or 0
            fill_ts = int(event_ts_ms / 1000) if event_ts_ms else 0
            lookup_id = str(order_id) if order_id else client_id
            credit_fill(
                bot_id=bot_id,
                order_id=lookup_id,
                cumulative_qty=cumulative_fill_qty,
                avg_price=avg_price,
                order_type='tp',
                is_cumulative=True,
                fill_ts=fill_ts
            )
            if symbol:
                register_tp_cascade(bot_id, symbol, avg_price, exit_fill_ts=fill_ts)
                logger.info(f"[TP-CASCADE] Bot {bot_id} {symbol} @ {avg_price:.6f} queued (fill_ts={fill_ts}).")
        except Exception as e:
            logger.error(f"[WS-FILL] TP credit failed for bot {bot_id}: {e}")

    elif order_type in ('GRID', 'ENTRY'):
        # v2.0: credit_fill → seal_trade_state
        try:
            from engine.ledger import credit_fill, seal_trade_state
            # Extract exchange fill timestamp for filled_at
            event_ts_ms = event.get('lastTradeTimestamp') or event.get('timestamp') or 0
            fill_ts = int(event_ts_ms / 1000) if event_ts_ms else 0
            lookup_id = str(order_id) if order_id else client_id
            # ── RACE-SAFE CREDIT PATH [v2.1.3] ────────────────────────────
            # _handle_fill_with_pending_retry() attempts credit_fill immediately.
            # If no DB row exists yet (taker fill racing ahead of save_bot_order),
            # it parks the fill in _pending_fills for retry on the next WS cycle.
            # After PENDING_FILL_MAX_RETRIES exhausted → REQUIRE_MANUAL_PROOF.
            # Never sleeps, never silently drops, independent of ALLOW_FORENSIC_ADOPT.
            # ─────────────────────────────────────────────────────────────────
            _handle_fill_with_pending_retry(
                bot_id=bot_id,
                order_id=lookup_id,
                client_id=client_id,
                qty=cumulative_fill_qty,
                price=avg_price,
                order_type=order_type.lower(),
                fill_ts=fill_ts,
                symbol=symbol,
            )
            # Note: credit_fill OK / retry-queued logging is inside _handle_fill_with_pending_retry
        except Exception as e:
            logger.error(f"[WS-FILL] ENTRY/GRID credit failed for bot {bot_id}: {e}")

    # Queue DB status update (non-blocking)
    try:
        from engine.database import update_order_status
        _enqueue_db_write(update_order_status, order_id, 'filled', bot_id, cumulative_fill_qty)
    except Exception as e:
        logger.debug(f"[WS-FILL] order status update queued failed: {e}")

    # ── NOTIFICATION (dedup-gated — prevents spam but never blocks state) ──
    notification_key = f"{bot_id}_{order_id}_{order_type}"
    if notification_key in _notified_fills:
        logger.debug(f"[WS-FILL] Skipping duplicate notification for {notification_key}")
        return
    _notified_fills.add(notification_key)
    _cleanup_notified_fills()

    try:
        if order_type == 'TP':
            cost_val = avg_price * cumulative_fill_qty
            log_trade(bot_id, 'WS_TP_FILL', symbol, avg_price, cumulative_fill_qty, cost_val, 'TP', pnl=realized_pnl)
            add_notification('success', f"TP Hit {symbol} (PnL ${realized_pnl:.2f})", bot_id)
        elif order_type == 'ENTRY':
            cost_val = avg_price * cumulative_fill_qty
            log_trade(bot_id, 'WS_ENTRY_FILL', symbol, avg_price, cumulative_fill_qty, cost_val, 'ENTRY')
            add_notification('info', f"Entry Filled {symbol} qty={cumulative_fill_qty:.4f} @ {avg_price:.4f}", bot_id)
        elif order_type == 'GRID':
            cost_val = avg_price * cumulative_fill_qty
            log_trade(bot_id, 'WS_GRID_FILL', symbol, avg_price, cumulative_fill_qty, cost_val, 'GRID')
    except Exception as e:
        logger.debug(f"[WS-FILL] Notification/log failed (non-fatal): {e}")



def _handle_order_canceled(bot_id: int, order_type: str, event: Dict):
    """Process a canceled order - update DB, ensuring any partial fill is recorded."""
    from engine.database import update_order_status

    order_id = event.get('order_id')
    cumulative_fill = float(event.get('filled_qty', 0) or 0)

    logger.info(f"❌ WS Cancel: Bot {bot_id} {order_type} order {order_id} canceled (Partial Fill: {cumulative_fill})")

    # Mark terminal so reconciler skips re-fetching this cancelled order
    mark_terminal_order(order_id)

    try:
        # Capture partial fills before cancellation — queued so listener stays non-blocking
        _enqueue_db_write(update_order_status, order_id, 'cancelled', bot_id, cumulative_fill)
    except Exception as e:
        logger.debug(f"Could not queue cancel for order {order_id} in DB: {e}")


def _handle_order_new(bot_id: int, order_type: str, event: Dict):
    """Process a new order confirmation - can be used for logging."""
    order_id = event.get('order_id')
    price = event.get('price')
    qty = event.get('qty')
    
    logger.debug(f"📝 WS New Order: Bot {bot_id} {order_type} #{order_id} @ {price}")


def handle_position_update(event: Dict):
    """
    Handle real-time position update from WebSocket.
    
    Event structure:
    {
        'event': 'position_update',
        'symbol': 'BTCUSDC',
        'side': 'LONG/SHORT',
        'position_amt': 0.006,
        'entry_price': 78000.0,
        'unrealized_pnl': -12.50,
        'margin_type': 'cross',
        'timestamp': 1234567890
    }
    """
    try:
        symbol = event.get('symbol')
        position_amt = event.get('position_amt', 0)
        entry_price = event.get('entry_price', 0)
        unrealized_pnl = event.get('unrealized_pnl', 0)
        
        # Position amount of 0 means closed
        if position_amt == 0:
            logger.info(f"📊 WS Position Closed: {symbol}")
            # Could trigger ghost detection or cleanup here
        else:
            logger.debug(f"📊 WS Position: {symbol} {position_amt} @ {entry_price} (uPnL: ${unrealized_pnl:.2f})")
            
        # 🚀 WS CACHING: Update memory snapshot
        # Format the event to look roughly like CCXT position output
        position_data = {
            'symbol': symbol,
            'contracts': position_amt,
            'entryPrice': entry_price,
            'unrealizedPnl': unrealized_pnl,
            'marginType': event.get('margin_type', 'cross'),
            'timestamp': event.get('timestamp')
        }
        get_ws_cache().update_position(symbol, position_data)
            
    except Exception as e:
        logger.error(f"Error handling position update: {e}")
