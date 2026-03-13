import logging
import time
import sqlite3
from typing import Dict, List, Optional, Tuple, Any, Set
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

from .database import (
    get_connection, get_bot_status, get_all_bots, reset_bot_after_tp,
    log_trade, get_bot_order_ids, save_bot_order, update_order_status,
    update_martingale_step, log_reconciliation,
    DB_PATH
)
from .exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

logger = logging.getLogger("StateReconciliation")

# 🕐 SESSION GUARD: Record the exact moment this engine session started.
# Any fill from the exchange that occurred BEFORE this timestamp is rejected from adoption.
# This is the fundamental guard against Binance's 48h history being re-ingested on every restart.
ENGINE_START_TIME = time.time()


class ReconciliationAction(Enum):
    """Actions to take during reconciliation"""
    NO_ACTION = "no_action"
    RESET_TO_IDLE = "reset_to_idle"
    MARK_TP_HIT = "mark_tp_hit"
    REPAIR_ORDERS = "repair_orders"
    REQUIRE_MANUAL = "require_manual"
    SYSTEM_FIX_ZOMBIE = "system_fix_zombie"
    ALERT_ONLY = "alert_only"
    ROGUE_POSITION = "rogue_position"


@dataclass
class BotState:
    """Represents a bot's current state"""
    bot_id: int
    name: str
    pair: str
    direction: str
    is_active: bool
    # Trade state
    in_trade: bool
    total_invested: float
    avg_entry_price: float
    target_tp_price: float
    current_step: int
    basket_start_time: int
    # Order tracking
    entry_order_id: Optional[str]
    tp_order_id: Optional[str]
    has_confirmed_entry: bool
    cycle_id: int = 1  # Current trading cycle. Increments on each TP reset.


@dataclass
class ExchangePosition:
    """Represents position data from exchange"""
    symbol: str
    side: str  # 'LONG' or 'SHORT'
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float


@dataclass
class ExchangeOrder:
    """Represents order data from exchange"""
    order_id: str
    symbol: str
    side: str  # 'buy', 'sell'
    order_type: str  # 'limit', 'market'
    price: float
    amount: float
    status: str  # 'open', 'filled', 'cancelled'
    client_order_id: Optional[str]


@dataclass
class ReconciliationResult:
    """Result of reconciliation for a bot"""
    bot_id: int
    bot_name: str
    pair: str
    action_taken: ReconciliationAction
    details: str
    requires_manual_intervention: bool


class StateReconciler:
    """
    Refactored State Reconciler (v2.0)
    
    Philosophy:
    1. Bot-Centric: The Bot DB is the source of truth for "Intent".
    2. Verification: The Exchange is the source of truth for "Reality".
    3. Net-Sum: We validate the Aggregate Virtual Position against the Net Physical Position.
    """
    
    def __init__(self, exchanges: Optional[Dict[str, ExchangeInterface]] = None):
        if exchanges:
            self.exchanges = exchanges
        else:
            if getattr(config, 'FUTURES_ONLY_MODE', False):
                self.exchanges = {
                    'future': ExchangeInterface(market_type='future')
                }
            else:
                self.exchanges = {
                    'spot': ExchangeInterface(market_type='spot'),
                    'future': ExchangeInterface(market_type='future')
                }
        self.results: List[ReconciliationResult] = []
        
    def get_exchange(self, market_type: str):
        if market_type in self.exchanges:
            return self.exchanges[market_type]
        if not self.exchanges:
            return None
        return list(self.exchanges.values())[0]

    # ------------------------------------------------------------------
    # STEP 1: OFFLINE FILL DETECTION
    # ------------------------------------------------------------------
    def _sync_positions_to_exchange(self, exchange: ExchangeInterface) -> None:
        """
        Preflight: fetch live exchange positions and sync DB for any diverging bot.
        Runs BEFORE fill history replay. This is the primary guard against double-counting and math bugs.
        If the exchange says we have 100 contracts @ $1.50, we force the DB to match, ignoring local history math.
        """
        try:
            positions = exchange.fetch_positions()
            # Map by normalized symbol
            pos_map = {normalize_symbol(p['symbol']): p for p in positions if abs(p.get('contracts', 0)) > 0}
            
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("SELECT b.id, b.pair, b.direction, t.total_invested, t.avg_entry_price "
                        "FROM bots b JOIN trades t ON b.id=t.bot_id WHERE b.is_active=1")
            
            bot_records = cur.fetchall()
            
            # Count ALL active trades per pair to prevent Double Adoption and Anchor Stealing
            # in One-Way Margin Mode.
            pair_counts = {}
            for r in bot_records:
                norm_p = normalize_symbol(r[1])
                pair_counts[norm_p] = pair_counts.get(norm_p, 0) + 1

            synced_count = 0
            for bot_id, pair, direction, db_inv, db_entry in bot_records:
                norm = normalize_symbol(pair)
                ex_pos = pos_map.get(norm)
                if not ex_pos:
                    continue  # Exchange flat — let existing zombie/reset logic handle it
                
                ex_entry = float(ex_pos.get('entryPrice') or 0)
                ex_qty   = abs(float(ex_pos.get('contracts') or 0))
                ex_inv   = ex_entry * ex_qty
                ex_side  = ex_pos.get('side', '').upper()
                bot_dir  = direction.upper()
                
                if ex_entry <= 0 or ex_qty <= 0:
                    continue

                # 🛡️ DIRECTION GUARD: Never anchor a bot to a position of the opposite side.
                if ex_side != bot_dir:
                    continue
                    
                # 🚀 SOLE-OWNERSHIP ISOLATION:
                # If there is exactly ONE bot trading THIS pair globally, it has sole ownership
                # and can safely adopt orphaned mismatch positions or physically grouped Binance one-way numbers.
                is_sole_bot = pair_counts.get(norm, 0) == 1

                # 🛡️ STRICT ID-BASED RULES:
                # 1. Never blindly adopt unseen positions. If we missed it, `reconstruct_offline_fills` 
                #    will find the exact ID receipt. If it doesn't, it's a manual position or ghost.
                is_empty_bot = db_inv <= 0.01

                entry_drift  = abs(ex_entry - db_entry) / max(db_entry, 1e-8)
                inv_drift    = abs(ex_inv   - db_inv)   / max(db_inv,   1e-8)
                
                # 2. Prevent active bots from eating each other's trades. 
                # If a bot is NOT EMPTY, and NOT the SOLE bot, and has a huge drift, it MUST NOT adopt.
                if not is_empty_bot and not is_sole_bot and inv_drift > 0.05:
                    logger.warning(f"🛡️ [MANUAL-GUARD] Bot {bot_id} (Active) physical size=${ex_inv:.2f} differs immensely from DB=${db_inv:.2f}. Assuming manual interference or belonging to sibling bot; avoiding raw adoption.")
                    continue
                    
                # 3. Orphan Sweeper: If bot IS empty, and isn't the sole bot, we ONLY let it adopt 
                # if the residual unowned physical position matches ex_inv.
                if is_empty_bot and not is_sole_bot:
                    # STRICT PROOF-ONLY ISOLATION (Per User Architecture Request)
                    # We absolutely do not share, guess, or interpolate multi-bot adoptions.
                    # If multiple bots are active on this pair, they MUST rebuild exclusively 
                    # via physical `client_order_id` receipt tracking via `reconstruct_offline_fills`.
                    # Math-heal guessing is strictly forbidden for overlapping active bots.
                    logger.warning(f"🛡️ [MULTI-BOT ISOLATION] Bot {bot_id} detects physical vs logical gap, but CANNOT adopt mathematically because another bot is active on this pair. Strict Proof-Only mode active. Ignoring math gap.")
                    continue
                
                # 🛡️ TRUE-MATH LEDGER SYNC:
                # We align the Balance Sheet (trades) to the Ledger (bot_orders),
                # and then align the Ledger to the Exchange via a Delta Adjustment.
                # 🚀 SIGN-AWARE FIX: Short bots have negative inventory (Sell > Buy).
                if bot_dir == 'LONG':
                    cur.execute("""
                        SELECT COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add') THEN filled_amount ELSE 0 END), 0) -
                               COALESCE(SUM(CASE WHEN order_type IN ('tp', 'close', 'adoption_reduce') THEN filled_amount ELSE 0 END), 0)
                        FROM bot_orders WHERE bot_id = ? AND filled_amount > 0
                    """, (bot_id,))
                else: # SHORT
                    cur.execute("""
                        SELECT COALESCE(SUM(CASE WHEN order_type IN ('tp', 'close', 'adoption_reduce') THEN filled_amount ELSE 0 END), 0) -
                               COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add') THEN filled_amount ELSE 0 END), 0)
                        FROM bot_orders WHERE bot_id = ? AND filled_amount > 0
                    """, (bot_id,))
                
                ledger_qty = float(cur.fetchone()[0] or 0.0)
                
                # Check for significant drift between Ledger and Exchange
                # (Note: ex_qty is absolute, target_qty must be signed)
                target_qty = ex_qty if bot_dir == 'LONG' else -ex_qty
                
                # 🚀 STRICT MULTI-BOT REJECTION
                # If there are sister bots running simultaneously, this specific bot CANNOT assume
                # mathematical ownership of the exchange gaps. It must rely solely on receipt string proofs later.
                if not is_sole_bot:
                    continue
                    
                adj_qty = target_qty - ledger_qty

                if abs(adj_qty) > 0.0001:
                    logger.warning(f"🏗️ [LEDGER-SYNC] Bot {bot_id} ledger={ledger_qty:.4f} vs Exchange={target_qty:.4f}. "
                                   f"Inserting Delta Adjustment of {adj_qty:.4f} units.")
                    
                    # 🚀 DIRECTIONAL INSERT BUGFIX
                    # Determine if we are ADDING or REDUCING the positional stack.
                    is_adding = True
                    if bot_dir == 'LONG':
                        is_adding = (adj_qty > 0)
                    else: # SHORT
                        is_adding = (adj_qty < 0)
                        
                    sync_otype = 'adoption_add' if is_adding else 'adoption_reduce'
                    
                    sync_cid = f"CQB_{bot_id}_SYNC_{int(time.time() * 1000)}"
                    cur.execute("""
                        INSERT INTO bot_orders (
                            bot_id, step, order_type, order_id, price, amount, filled_amount,
                            status, created_at, updated_at, client_order_id, notes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?)
                    """, (
                        bot_id, 0, sync_otype, sync_cid, ex_entry, abs(adj_qty), abs(adj_qty),
                        int(time.time()), int(time.time()), sync_cid, 
                        f"Physical alignment adjustment ({adj_qty:+.4f} units)"
                    ))
                    
                    # Log the drift event
                    log_trade(bot_id, 'POSITION_SYNC', pair, ex_entry, adj_qty, abs(adj_qty)*ex_entry,
                              'RECON_SYNC', 0, f'Ledger drifted by {adj_qty:.4f}. Anchored to exchange.', 0)
                    synced_count += 1

                # ⚓ ALWAYS update balance sheet to match current physical truth + any new adjustment
                # This ensures immediate UI refresh even if rebuilder hasn't fired yet.
                # Use CASE to elegantly set current_step to 1 if it was 0 when adopting new positions bridging the gap.
                bot_owned_inv = abs(target_qty) * ex_entry
                cur.execute("UPDATE trades SET avg_entry_price=?, total_invested=?, current_step=CASE WHEN current_step=0 THEN 1 ELSE current_step END WHERE bot_id=?",
                            (ex_entry, bot_owned_inv, bot_id))
                    
            if synced_count > 0:
                conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error in _sync_positions_to_exchange: {e}")

    def reconstruct_offline_fills(self, since_hours: int = 168) -> Dict[str, int]:
        """
        Scans exchange history for orders that filled while we were offline.
        Updates the DB immediately so subsequent checks see the correct state.
        Refactored to be ROBUST: Uses Client Order ID parsing to reconstruct state.
        """
        stats = {'grid_fills': 0, 'tp_fills': 0, 'entry_fills': 0, 'total': 0}
        
        # 🔑 PREFLIGHT: Sync DB positions to exchange before looking at any single fill
        # This prevents fragmented/slow fills from corrupting a position that already matches
        try:
            ex_fut = self.exchanges.get('future')
            if ex_fut:
                self._sync_positions_to_exchange(ex_fut)
        except Exception as e:
            logger.error(f"Preflight sync failed: {e}")

        # 1. Identify pairs to scan (Active Bots + Open Orders)
        conn = get_connection()
        cursor = conn.cursor()
        
        # Get all bots to know what pairs to check
        cursor.execute("SELECT id, pair, name, status FROM bots WHERE is_active=1")
        active_bots = cursor.fetchall() # [(id, pair, name, status), ...]
        
        # Get pairs from open orders too
        cursor.execute("SELECT DISTINCT pair from bots WHERE id IN (SELECT DISTINCT bot_id FROM bot_orders WHERE status='open')")
        order_pairs = [r[0] for r in cursor.fetchall()]
        
        pairs_to_check = set([b[1] for b in active_bots] + order_pairs)
        
        # Pre-fetch Bot States for fast lookups
        # Map: bot_id -> {current_step, total_invested, avg_entry, basket_start_time}
        bot_states = {}
        cursor.execute("SELECT bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time, COALESCE(cycle_id, 1) FROM trades")
        for row in cursor.fetchall():
            bot_states[row[0]] = {
                'current_step': row[1], 'total_invested': row[2], 
                'avg_entry': row[3], 'entry_confirmed': row[4],
                'basket_start_time': row[5] or 0,
                'cycle_id': row[6]
            }

        conn.close() # Close mainly to keep scope clean, we'll reopen for updates

        # 🚀 SELF-HEALING: basket_start_time Recovery
        # If a bot is actively IN TRADE (total_invested > 0) but basket_start_time=0,
        # EE decay is permanently silent. Recover the timestamp from the oldest filled order
        # in this session — this runs every reconciler pass so no restart is ever needed.
        _heal_conn = get_connection()
        _heal_cur = _heal_conn.cursor()
        _healed = 0
        for _bot_id, _state in bot_states.items():
            if _state['total_invested'] > 0 and _state['basket_start_time'] == 0:
                # Try to recover from oldest filled entry order in bot_orders
                _heal_cur.execute("""
                    SELECT MIN(created_at) FROM bot_orders 
                    WHERE bot_id=? AND order_type='entry' AND status IN ('filled','closed')
                """, (_bot_id,))
                _row = _heal_cur.fetchone()
                recovered_ts = (_row[0] if _row and _row[0] else None)
                if not recovered_ts:
                    # Fallback: use current time so EE at least starts from now
                    recovered_ts = int(time.time())
                _heal_cur.execute(
                    "UPDATE trades SET basket_start_time=? WHERE bot_id=? AND basket_start_time=0",
                    (int(recovered_ts), _bot_id)
                )
                _state['basket_start_time'] = int(recovered_ts)  # update in-memory too
                _healed += 1
                logger.info(f"🩹 [SELF-HEAL] Bot {_bot_id}: basket_start_time recovered to {recovered_ts} (was 0 while in trade — EE now active).")
        if _healed:
            _heal_conn.commit()
        _heal_conn.close()


        # 2. Scan History per Pair
        since_ts = int((time.time() - (since_hours * 3600)) * 1000)
        
        for pair in pairs_to_check:
            # Find which exchange handles this pair (usually 'future')
            # For now assume 'future' or check config. 
            # In a mixed setup, we might need to check both if not sure.
            # But usually pair names are unique enough or we iterate exchanges.
            
            for mt, ex in self.exchanges.items():
                if not ex: continue
                try:
                    # Fetch History (Limit 1000 to ensure high-spam bots don't push fills off-page)
                    history = ex.fetch_closed_orders(pair, since=since_ts, limit=1000)
                    if not isinstance(history, list):
                        history = []
                        
                    # 🚀 PARTIAL FILL RECOVERY FIX:
                    # Also fetch open orders to catch active grid ladders that might have received partial fills while offline.
                    try:
                        open_orders = ex.fetch_open_orders(pair)
                        if isinstance(open_orders, list):
                            history.extend(open_orders)
                    except Exception as eo_e:
                        logger.error(f"Failed to fetch open matching orders for {pair}: {eo_e}")
                    
                    if not history:
                        continue
                        
                    # Sort by time (Oldest first) to replay history
                    history.sort(key=lambda x: x.get('timestamp', 0) or 0)
                    
                    conn = get_connection()
                    cursor = conn.cursor()
                    
                    for order in history:
                        cid = order.get('clientOrderId', '')
                        if not cid.startswith('CQB_'): continue
                        
                        # Parse CID: CQB_{bot_id}_{type}_{step}
                        # Examples: CQB_1_ENTRY_0, CQB_1_GRID_1, CQB_1_TP_5
                        try:
                            parts = cid.split('_')
                            if len(parts) < 4: continue
                            
                            bot_id = int(parts[1])
                            otype = parts[2] # ENTRY, GRID, TP
                            step = int(parts[3])
                            
                            current_state = bot_states.get(bot_id, {})
                            
                            # 🔑 CYCLE-ID GUARD: The definitive, time-free fill isolation check.
                            # An offline fill is ONLY adopted if it belongs to the bot's CURRENT cycle.
                            # A fill from a previous cycle will have no matching bot_orders row with the
                            # current cycle_id, OR will have a 'reset_cleared' status. Both are rejected.
                            cursor.execute("""
                                SELECT status, cycle_id FROM bot_orders 
                                WHERE (order_id=? OR client_order_id=?)
                            """, (order['id'], cid))
                            row = cursor.fetchone()

                            logger.info(f"🔍 [OFFLINE-SYNC] Checking Order {order['id']} (CID: {cid}) | InDB: {'Yes' if row else 'No'} | DBStatus: {row[0] if row else 'N/A'} | DBCycle: {row[1] if row else 'N/A'}")

                            if row:
                                # Known order: reject if already processed or from old cycle
                                if row[0] in ['filled', 'closed', 'reset_cleared', 'auto_closed']:
                                    continue
                                # Also reject if it has a different cycle_id than the bot's current cycle
                                bot_current_cycle = current_state.get('cycle_id', 1)
                                if row[1] is not None and row[1] != bot_current_cycle:
                                    logger.debug(f"🛑 [CYCLE-GUARD] Rejecting fill {cid}: belongs to cycle {row[1]}, bot is on cycle {bot_current_cycle}.")
                                    continue
                            else:
                                # Order NOT in our DB at all — it was placed in a previous life / manually.
                                # Unconditionally reject to prevent adopting unknown history.
                                logger.debug(f"🛑 [CYCLE-GUARD] Rejecting unknown fill {cid}: not in bot_orders DB (not from this system's current cycle).")
                                continue

                            # Update Bot State
                            order_status = order.get('status', '').lower()
                            
                            # GRACE PERIOD GUARD for Filled/Closed/Open orders
                            # If an order just finished filling, the WebSocket might still be processing its fragmented partial fills.
                            # Give the WS 60 seconds to finish. We only reconstruct offline history if the order is truly 'cold'
                            # OR if the order is currently OPEN but older than 10 seconds.
                            finish_ts = order.get('lastTradeTimestamp') or order.get('timestamp') or 0
                            if order_status in ['closed', 'filled']:
                                if (time.time() * 1000 - finish_ts) < 60000:
                                    logger.debug(f"⏳ [GRACE PERIOD] Skipping recently filled order {cid} (Age: {int((time.time()*1000 - finish_ts)/1000)}s); giving WS time to process.")
                                    continue
                            elif order_status == 'open':
                                if (time.time() * 1000 - finish_ts) < 10000:
                                    continue
                                
                                # Skip open orders that have no partial fills yet
                                if not order.get('filled') or float(order.get('filled')) <= 0:
                                    continue
                            
                            # GUARD: Only process actually-filled orders.
                            # However, if the order was cancelled/expired/rejected on the exchange,
                            # we MUST update the local DB to reflect that so it doesn't stay 'open' forever.
                            # 🛡️ FUNDAMENTAL FIX: Remove "Optimistic Hallucinations".
                            # Never default to target 'amount' if exchange fill is missing.
                            # That is the source of the $53k SOL drift. Fill is 0 until proven otherwise.
                            fill_price = order.get('average') or order.get('price') or 0.0
                            fill_qty = order.get('filled') or 0.0

                            # Update local DB if cancelled, but DO NOT skip if there was a partial fill
                            if order_status in ['canceled', 'cancelled', 'expired', 'rejected']:
                                logger.debug(f"🧹 [OFFLINE-SYNC] Syncing cancelled order {cid} (status={order_status}) to DB.")
                                cursor.execute("UPDATE bot_orders SET status=?, updated_at=? WHERE order_id=?",
                                               (order_status, int(time.time()), order['id']))
                                # ONLY skip if it wasn't partially filled.
                                # If fill_qty > 0, it was partially filled before dying, so WE MUST PROCESS IT!
                                if fill_qty <= 0:
                                    continue
                            elif order_status not in ('filled', 'closed'):
                                logger.debug(f"Skipping non-filled order {cid} (status={order_status})")
                                continue
                            fill_symbol = order.get('symbol', pair)
                            bot_name = f"Bot-{bot_id}" # Placeholder, ideally fetch name
                            
                            curr_step = current_state.get('current_step', 0)
                            
                            logger.info(f"🕵️ RECONSTRUCTING: Found Fill for Bot {bot_id} {otype} {step} @ {fill_price} for {fill_symbol}")

                            if otype == 'TP':
                                # GUARD: Never reset with price=0 — that corrupts trade history
                                if fill_price <= 0:
                                    logger.warning(f"⚠️ Skipping OFFLINE_TP for Bot {bot_id}: fill_price={fill_price} is invalid.")
                                    continue
                                
                                # 🔑 EXCHANGE-POSITION GUARD: The most critical safety check.
                                # If the exchange STILL has an open position for this symbol, this TP
                                # either failed, was cancelled, or belongs to a previous cycle.
                                # We MUST NOT reset the bot in this case — it would zero a live position.
                                try:
                                    ex_for_guard = self.exchanges.get('future') or self.exchanges.get('spot')
                                    if ex_for_guard:
                                        live_positions = ex_for_guard.fetch_positions()
                                        norm_fill_sym = normalize_symbol(fill_symbol)
                                        still_has_position = any(
                                            normalize_symbol(p['symbol']) == norm_fill_sym and abs(float(p.get('contracts', 0))) > 0.001
                                            for p in live_positions
                                        )
                                        if still_has_position:
                                            logger.warning(
                                                f"🛡️ [TP-GUARD] Skipping OFFLINE_TP reset for Bot {bot_id} ({fill_symbol}): "
                                                f"Exchange STILL has an open position. This TP is stale/cancelled/from prior cycle."
                                            )
                                            # Mark the TP order as cancelled in our DB so we don't keep checking it
                                            cursor.execute("UPDATE bot_orders SET status='auto_closed' WHERE (order_id=? OR client_order_id=?)", (order['id'], cid))
                                            continue
                                except Exception as guard_err:
                                    logger.error(f"TP position guard failed for Bot {bot_id}: {guard_err}. Skipping reset to be safe.")
                                    continue
                                
                                self._handle_offline_tp_fill(bot_id, bot_name, fill_price, fill_symbol)
                                stats['tp_fills'] += 1
                                
                            elif otype == 'GRID':
                                # MODIFIED: Allow step >= curr_step to recover from "Alzheimer's" repeat-fills
                                # If the order ID wasn't already marked closed/filled in DB, we MUST credit it.
                                if step >= curr_step:
                                    # 🔑 IDEMPOTENCY CHECK v2: Two-layer guard.
                                    # Layer 1: Check by exchange order_id in bot_orders (fastest, most accurate)
                                    cursor.execute(
                                        "SELECT COUNT(*) FROM bot_orders WHERE order_id=? AND status IN ('filled','closed')",
                                        (order['id'],)
                                    )
                                    already_in_orders = cursor.fetchone()[0] > 0
                                    
                                    # Layer 2: Check trade_history by price+qty (catches WS fills logged
                                    # under different order_ids due to partial fill fragmentation)
                                    cursor.execute(
                                        "SELECT COUNT(*) FROM trade_history "
                                        "WHERE bot_id=? AND ABS(price - ?) < ? AND ABS(amount - ?) < ? "
                                        "AND action IN ('WS_GRID_FILL','GRID_FILL','WS_ENTRY_FILL')",
                                        (bot_id, fill_price, fill_price * 0.001, fill_qty, fill_qty * 0.001)
                                    )
                                    already_in_history = cursor.fetchone()[0] > 0
                                    
                                    already_logged = already_in_orders or already_in_history
                                    if already_logged:
                                        logger.info(f"⏭️ [OFFLINE-DEDUP] Skipping fill for Bot {bot_id} Step {step} @ {fill_price} x{fill_qty} — already logged (orders={already_in_orders}, history={already_in_history}).")
                                        # Mark bot_order as filled so reconciler doesn't re-check on next cycle
                                        cursor.execute("UPDATE bot_orders SET status='filled' WHERE (order_id=? OR client_order_id=?)", (order['id'], cid))
                                        continue
                                    
                                    logger.info(f"✅ Re-playing OFFLINE_GRID for Bot {bot_id} (Step {step})")
                                    self._handle_offline_grid_fill(cursor, bot_id, bot_name, fill_price, fill_qty, curr_step, current_state.get('total_invested',0), current_state.get('avg_entry',0), fill_symbol)
                                    stats['grid_fills'] += 1
                                    # ✅ FIX: Update the LOCAL cache so subsequent fills in THIS SAME SWEEP
                                    # use the correct post-fill avg_entry and total_invested.
                                    # Previously, 'avg_entry update omitted for local cache brevity' meant the
                                    # second offline fill in a sweep used stale state, corrupting the average.
                                    old_inv = bot_states[bot_id].get('total_invested', 0)
                                    old_avg = bot_states[bot_id].get('avg_entry', 0)
                                    fill_cost = fill_price * fill_qty
                                    new_inv = old_inv + fill_cost
                                    new_avg = ((old_inv * old_avg) + fill_cost) / new_inv if new_inv > 0 else fill_price
                                    bot_states[bot_id]['current_step'] = step
                                    bot_states[bot_id]['total_invested'] = new_inv
                                    bot_states[bot_id]['avg_entry'] = new_avg
                                    
                            elif otype == 'ENTRY':
                                if curr_step == 0:
                                    logger.info(f"✅ Re-playing OFFLINE_ENTRY for Bot {bot_id}")
                                    order_time_sec = int(order.get('timestamp', time.time() * 1000) / 1000)
                                    self._handle_offline_entry_fill(cursor, bot_id, bot_name, fill_price, fill_qty, fill_symbol, order_time_sec)
                                    stats['entry_fills'] += 1
                                    bot_states[bot_id]['current_step'] = 1
                                    bot_states[bot_id]['entry_confirmed'] = 1
                                    bot_states[bot_id]['total_invested'] = fill_price * fill_qty

                            elif otype == 'HEDGETP':
                                logger.info(f"🛡️ ✅ [OFFLINE-HEDGETP] Re-playing HEDGETP for Bot {bot_id}")
                                # Mark as filled in DB to trigger retirement in next executor cycle
                                cursor.execute("UPDATE bot_orders SET status='filled', updated_at=? WHERE order_id=?", 
                                               (int(time.time()), order['id']))
                                stats['tp_fills'] += 1 # Count as a TP fill for stats

                            elif otype == 'HEDGE':
                                logger.info(f"🛡️ ✅ [OFFLINE-HEDGE] Re-playing HEDGE for Bot {bot_id}")
                                # Mark as filled in DB
                                cursor.execute("UPDATE bot_orders SET status='filled', updated_at=? WHERE order_id=?", 
                                               (int(time.time()), order['id']))
                                # Note: We don't update total_invested/avg_entry here as hedge is separate
                                stats['grid_fills'] += 1 # Count as a grid fill for stats
                            
                            # Update/Insert Order Record
                            # bot_orders has no UNIQUE on order_id, so check first.
                            cursor.execute("SELECT id FROM bot_orders WHERE order_id=?", (order['id'],))
                            existing = cursor.fetchone()
                            
                            # 🚀 LEDGER INTEGRITY FIX:
                            # If this was a TP order, we already called reset_bot_after_tp which wiped previous grids.
                            # We MUST mark this TP order as `reset_cleared` immediately so it doesn't leave an unbalanced 
                            # negative ledger chunk floating around in the new clean cycle.
                            final_status = 'reset_cleared' if otype == 'TP' else 'filled'
                            
                            if existing:
                                cursor.execute("UPDATE bot_orders SET status=?, updated_at=? WHERE order_id=?",
                                               (final_status, int(time.time()), order['id']))
                            else:
                                cursor.execute("""
                                    INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at, updated_at, client_order_id, notes)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (bot_id, step, otype.lower(), order['id'], fill_price, fill_qty, final_status,
                                      int(order['timestamp']/1000), int(time.time()), cid, 'Reconstructed from History'))
                                    
                        except Exception as e:
                            logger.debug(f"Error parsing/processing CID {cid}: {e}")
                            continue

                    conn.commit()
                    conn.close()

                except Exception as e:
                    logger.error(f"Error scanning pair {pair} on {mt}: {e}")

        stats['total'] = stats['grid_fills'] + stats['tp_fills'] + stats['entry_fills']
        return stats

    def _mark_order_filled(self, cursor, order_id, fill_price):
        cursor.execute("""
            UPDATE bot_orders SET status='filled', filled_at=?, price=?, updated_at=?
            WHERE order_id = ?
        """, (int(time.time()), fill_price, int(time.time()), order_id))

    def _handle_offline_tp_fill(self, bot_id, bot_name, fill_price, symbol):
        # GUARD: Never call reset_bot_after_tp with an invalid price (belt-and-suspenders)
        # The caller already guards, but this function is the final safety net.
        if not fill_price or float(fill_price) <= 0:
            logger.warning(
                f"⚠️ [OFFLINE-TP-GUARD] Skipping reset for Bot {bot_id} ({bot_name}): "
                f"fill_price={fill_price} is invalid (0 or None). Will not corrupt trade history."
            )
            return
        reset_bot_after_tp(bot_id, exit_price=fill_price, action_label='OFFLINE_TP')
        # Note: reset_bot_after_tp handles DB commits internaly, but here we are in a transaction from caller...
        # Ideally reset_bot_after_tp should be transaction-aware or we use a separate connection. 
        # Using a separate connection inside reset_bot_after_tp is fine as sqlite handles concurrency (WAL).

    def _handle_offline_grid_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount, current_step, total_invested, avg_entry, symbol):
        new_step = (current_step or 0) + 1
        fill_cost = fill_price * fill_amount
        new_invested = (total_invested or 0) + fill_cost
        new_avg = ((total_invested or 0) * (avg_entry or 0) + fill_cost) / new_invested if new_invested > 0 else fill_price
        
        # UPSERT trade (Temporary local math — will be anchored to exchange immediately below)
        cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE trades SET current_step=?, total_invested=?, avg_entry_price=? WHERE bot_id=?", 
                           (new_step, new_invested, new_avg, bot_id))
        else:
            cursor.execute("INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price) VALUES (?,?,?,?)",
                           (bot_id, new_step, new_invested, new_avg))
        
        log_trade(bot_id, 'OFFLINE_GRID', symbol, fill_price, fill_amount, fill_cost, f"GRID_{new_step}", new_step, "Offline Grid Fill", 0)
        
        # (POST-FILL ANCHOR removed: Exact math via the receipt footprint is correct; don't blindly snap to physical size)

    def _handle_offline_entry_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount, symbol, timestamp_sec):
        fill_cost = fill_price * fill_amount
        # UPSERT trade (Temporary local math — will be anchored to exchange immediately below)
        cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE trades SET current_step=1, total_invested=?, avg_entry_price=?, entry_confirmed=1, basket_start_time=? WHERE bot_id=?",
                           (fill_cost, fill_price, timestamp_sec, bot_id))
        else:
            cursor.execute("INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time) VALUES (?,1,?,?,1,?)",
                           (bot_id, fill_cost, fill_price, timestamp_sec))
        
        log_trade(bot_id, 'OFFLINE_ENTRY', symbol, fill_price, fill_amount, fill_cost, "ENTRY", 1, "Offline Entry Fill", 0)
        
        # (POST-FILL ANCHOR removed: Exact math via the receipt footprint is correct; don't blindly snap to physical size)

    # ------------------------------------------------------------------
    # STEP 2: BOT-CENTRIC VALIDATION
    # ------------------------------------------------------------------
    def validate_individual_bots(self, bot_states: List[BotState], all_orders: Dict[str, List[ExchangeOrder]]) -> List[ReconciliationResult]:
        """
        Ask each bot: "You say you are in trade. Do your orders exist on the exchange?"
        If not -> Zombie -> Reset.
        """
        results = []
        
        for bot in bot_states:
            # Common: Check propagation delay
            if (time.time() - bot.basket_start_time) < 60:
                continue

            pair_orders = all_orders.get(bot.pair, [])
            
            # -----------------------------------------------------------
            # CASE A: BOT IS IN TRADE (Zombie Check)
            # -----------------------------------------------------------
            if bot.in_trade:
                # Check if this bot's orders exist in the exchange list
                has_orders = False
                for order in pair_orders:
                    # Check Client Order ID (Best way)
                    if order.client_order_id and f"CQB_{bot.bot_id}_" in order.client_order_id:
                        has_orders = True
                        break
                    # Check stored Exchange IDs
                    if str(order.order_id) == str(bot.entry_order_id) or str(order.order_id) == str(bot.tp_order_id):
                        has_orders = True
                        break
                
                if not has_orders:
                    # --- PHANTOM BOT CORRECTION ---
                    # If invested > 0 but NO orders and NO physical position (checked in resolve_net_mismatch)
                    # For now, just log and let resolve_net_mismatch handle the structural lock.
                    logger.warning(f"⚠️ [RECON] Bot {bot.name} (ID {bot.bot_id}) is IN TRADE but has NO orders. Deferring to Global Net Resolution.")


            # -----------------------------------------------------------
            # CASE B: BOT IS IDLE (Orphan Order Check)
            # -----------------------------------------------------------
            else:
                # Bot is IDLE/Scanning. It should have NO open orders.
                my_orders = [o for o in pair_orders if (o.client_order_id and f"CQB_{bot.bot_id}_" in o.client_order_id)]
                
                if my_orders:
                    # ORPHAN DETECTED
                    # 🚀 SIGNATURE-BASED ACCURACY FIX:
                    # An IDLE bot with orders is a discrepancy, but we should NOT force-cancel
                    # if they have the system's DNA (CQB prefix). 
                    # Instead, we report them as requiring intervention.
                    logger.warning(f"⚠️ ORPHAN DETECTED: Bot {bot.name} (ID {bot.bot_id}) is IDLE but has {len(my_orders)} open CQB orders. PRESERVING for forensic link.")
                    
                    res = ReconciliationResult(
                        bot_id=bot.bot_id,
                        bot_name=bot.name,
                        pair=bot.pair,
                        action_taken=ReconciliationAction.ROGUE_POSITION, # Use Rogue to trigger Wizard
                        details=f"Orphan Bot Orders Found ({len(my_orders)}) - Manual Link Suggested",
                        requires_manual_intervention=True
                    )
                    results.append(res)
                    
                    # 🗑️ NO AUTO-CLEAN: We wait for the user to use the "Manual Link Recovery Tool"
                    # or force an aggressive cleanup explicitly.
                    
                    # Also mark them closed in DB to stop the cycle
                    conn = get_connection()
                    for o in my_orders:
                        conn.execute("UPDATE bot_orders SET status='cancelled', updated_at=? WHERE order_id=?", (int(time.time()), o.order_id))
                    conn.commit()
        
        return results

    # ------------------------------------------------------------------
    # STEP 3: NET-SUM VERIFICATION
    # ------------------------------------------------------------------
    def resolve_net_mismatch(self, bot_states: List[BotState], positions: Dict[str, List[ExchangePosition]], all_orders: Dict[str, List[ExchangeOrder]] = {}, force_adoption: bool = False) -> List[ReconciliationResult]:
        """
        Calculates Net Virtual Position and compares with Net Physical Position.
        If significant deviation exists, identifies 'Ghost' bots and resets them using SYSTEM_FIX.
        """
        results = []
        
        # Group bots by pair (normalized)
        bots_by_pair = {}
        for bot in bot_states:
            norm_pair = normalize_symbol(bot.pair)
            if norm_pair not in bots_by_pair: bots_by_pair[norm_pair] = []
            bots_by_pair[norm_pair].append(bot)
            
        
        # FUNDAMENTAL FIX: Normalize the incoming CCXT positions dictionary keys
        # The exchange keys are like "BTC/USDC" but we must process them as "BTCUSDC"
        normalized_positions = {}
        for raw_pair, pos_list in positions.items():
            norm_p = normalize_symbol(raw_pair)
            normalized_positions[norm_p] = pos_list
            
        # FUNDAMENTAL FIX: Include pairs that have positions but no bots (Rogue Positions)
        # We must ensure that EVERY pair in normalized_positions is a key in bots_by_pair
        all_exchange_pairs = list(normalized_positions.keys())
        for norm_p in all_exchange_pairs:
            if norm_p not in bots_by_pair:
                bots_by_pair[norm_p] = [] # Empty list of bots for this pair

        for pair, bots in bots_by_pair.items():
            # 'pair' here is normalized
            # 1. Calc Virtual Net
            virtual_net = 0.0
            total_virtual_invested = 0.0
            virtual_net_usd = 0.0  # Signed USD value
            
            for b in bots:
                if b.in_trade:
                    qty = b.total_invested / b.avg_entry_price if b.avg_entry_price > 0 else 0
                    total_virtual_invested += b.total_invested
                    if b.direction.upper() == 'LONG':
                        virtual_net += qty
                        virtual_net_usd += b.total_invested
                    else:
                        virtual_net -= qty
                        virtual_net_usd -= b.total_invested
            
            # 2. Get Physical Net
            # 3. Compare with Tolerance
            # Ensure we are comparing normalized symbols
            pair_normalized = normalize_symbol(pair)
            pair_positions = normalized_positions.get(pair_normalized, [])
            
            total_physical_notional = 0.0
            physical_net = 0.0
            physical_net_usd = 0.0 # Signed USD value
            rep_side = "N/A"
            
            for p in pair_positions:
                val = abs(p.size) * p.entry_price
                total_physical_notional += val
                rep_side = p.side # Used for reporting
                if p.side == 'LONG': 
                     physical_net += abs(p.size)
                     physical_net_usd += val
                else: 
                     physical_net -= abs(p.size)
                     physical_net_usd -= val
                
            logger.info(f"⚖️ RECON AUDIT [{pair_normalized}]: Virtual=${total_virtual_invested:.2f} vs Physical=${total_physical_notional:.2f}")
            
            # Case A: Total System Investment > 10.0 but Physical = 0
            if total_virtual_invested > 10.0 and total_physical_notional < 1.0:
                logger.critical(f"👻 SYSTEM MISMATCH on {pair}: Virtual=${total_virtual_invested:.2f} vs Physical=$0.00.")
                
                # --- FUNDAMENTAL ARCHITECTURE: EVIDENCE-BASED RECONSTRUCTION ---
                for b in bots:
                    if b.in_trade:
                        proof = self._find_proof_of_exit(b)
                        if proof:
                            logger.info(f"✅ PROOF FOUND: Bot {b.name} exited via order {proof.get('id')} ({proof.get('clientOrderId')}). Resetting safely.")
                            from .database import log_reconciliation
                            log_reconciliation(
                                bot_id=b.bot_id,
                                pair=b.pair,
                                action="RESET_WITH_PROOF",
                                details=f"Found exit fill {proof.get('id')} in exchange history.",
                                proof_order_id=proof.get('id')
                            )
                            res = ReconciliationResult(
                                bot_id=b.bot_id,
                                bot_name=b.name,
                                pair=b.pair,
                                action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                details=f"Reconciled: Exit proof found in history ({proof.get('clientOrderId')}).",
                                requires_manual_intervention=False
                            )
                            results.append(res)
                            self._fix_ghost_bot(b, proof_order_id=proof.get('id'))
                        else:
                            # --- FUNDAMENTAL FIX: VERIFY ENTRY EXISTENCE ---
                            # If no exit proof, maybe the entry itself never happened (Phantom Entry)?
                            
                            # SKIP IF AUDITED/MANUAL
                            # If we manually confirmed it, don't delete it just because the order is old/missing.
                            if b.has_confirmed_entry:
                                logger.info(f"🛡️ SKIPPING Entry Verification for Bot {b.name}: Entry already confirmed (Manual/Audit).")
                                continue

                            entry_proof = self._verify_entry_existence(b)
                            if not entry_proof:
                                logger.info(f"✅ INVALID ENTRY DETECTED: Bot {b.name} Entry Order {b.entry_order_id} not found/filled on exchange. Resetting Phantom State.")
                                from .database import log_reconciliation
                                log_reconciliation(
                                    bot_id=b.bot_id,
                                    pair=b.pair,
                                    action="RESET_PHANTOM_ENTRY",
                                    details="Entry order not found or not filled on exchange. Bot state was phantom.",
                                )
                                res = ReconciliationResult(
                                    bot_id=b.bot_id,
                                    bot_name=b.name,
                                    pair=b.pair,
                                    action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                    details=f"Reset Phantom Entry (Order {b.entry_order_id} invalid)",
                                    requires_manual_intervention=False
                                )
                                results.append(res)
                                self._fix_ghost_bot(b, proof_order_id="PHANTOM_ENTRY")
                                # FORCE RESET: If the exchange says $0, the position is gone (e.g. user manually closed it on exchange).
                                # We MUST adopt reality rather than stalling the bot indefinitely.
                                logger.critical(
                                    f"💥 [MANUAL-INTERVENTION DETECTED] Bot {b.name}: Virtual claims ${b.total_invested:.2f} but Physical is $0.00! "
                                    f"The position vanished without bot logic (user flattened). Forcing memory wipe to sync with reality!"
                                )
                                from .database import reset_bot_after_tp, log_reconciliation
                                reset_bot_after_tp(b.bot_id, 0.0) # Reset to 0 since it vanished
                                
                                log_reconciliation(
                                    bot_id=b.bot_id,
                                    pair=b.pair,
                                    action="RESET_VANISHED_POSITION",
                                    details="Mismatch: Virtual>0, Physical=0. Position vanished (likely user closed). Resetting bot."
                                )
                                results.append(ReconciliationResult(
                                    bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                                    action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                    details="Reset Vanished Position", requires_manual_intervention=False
                                ))
            
            # --- CASE B.2: STRUCTURAL GHOST DETECTION ---
            # Even if net-notional is close, if a bot claims high step but lacks history, it's a structural ghost (legacy state).
            for b in bots:
                # 🚀 ARCHITECTURAL FIX: 
                # Do not execute Structural Ghost destruction if the bot's entry is explicitly confirmed. 
                # 'entry_confirmed=1' indicates the position was successfully spawned via WebSocket fill, 
                # or was manually adopted/audited by the user/system.
                # NEW FIX: Also EXEMPT bots that were just anchored from physical exchange data (Step 0 but invested > 1) 
                if b.in_trade and b.total_invested > 1.0 and not b.has_confirmed_entry and b.current_step > 0:
                    try:
                        from .database import get_connection
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT COUNT(*), SUM(amount * price) FROM bot_orders 
                            WHERE bot_id=? AND status IN ('filled', 'closed') AND created_at >= (? - 120)
                        """, (b.bot_id, b.basket_start_time))
                        row = cursor.fetchone()
                        
                        # If bot has money invested, but ZERO filled orders in the current basket session
                        if not row or row[0] == 0:
                            logger.critical(f"👻 [STRUCTURAL-GHOST] Bot {b.name} claims ${b.total_invested:.2f} (Step {b.current_step}) but has NO filled orders since basket start ({b.basket_start_time}). Resetting to truth.")
                            from .database import reset_bot_after_tp, log_reconciliation
                            reset_bot_after_tp(b.bot_id, 0.0) # Force reset
                            log_reconciliation(
                                bot_id=b.bot_id, pair=b.pair, action="RESET_STRUCTURAL_GHOST",
                                details=f"Structural Ghost: Claimed ${b.total_invested:.2f} but 0 Session Fills."
                            )
                            results.append(ReconciliationResult(
                                bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                                action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                details="Reset Structural Ghost (No verifiable history)", requires_manual_intervention=False
                            ))
                    except Exception as e:
                        logger.error(f"Error checking structural ghost for {b.name}: {e}")
                    finally:
                        if 'conn' in locals(): conn.close()

            # --- CASE C: SIGNIFICANT NOTIONAL DEVIATION (AUTO-REPAIR) ---
            # 🚀 ARCHITECTURAL FIX: Use Signed USD Net instead of absolute magnitude.
            # This accounts for virtual hedging (Long + Short) correctly.
            delta_notional = abs(virtual_net_usd - physical_net_usd)
            
            # 🚀 SCANNING-BOT GUARD: Only run NOTIONAL-GAP repair if at least one bot
            # on this pair is GENUINELY in trade (status=IN TRADE with real invested amount > $50).
            # Without this, dust residuals after a clean TP exit trigger AUTO-REPAIR and write
            # the dust back into the bot's virtual position, resurrecting a closed cycle.
            genuine_in_trade_bots = [b for b in bots if b.in_trade]
            
            if delta_notional > 50.0 and (abs(virtual_net_usd) > 10.0 or abs(physical_net_usd) > 10.0) and genuine_in_trade_bots:
                 logger.error(
                     f"🚨 [NOTIONAL-GAP] {pair}: Virtual=${total_virtual_invested:.2f} vs "
                     f"Physical=${total_physical_notional:.2f} (Delta=${delta_notional:.2f}). "
                     f"System explicitly refuses to blindly alter bot math without algorithmic ID receipts. This implies manual interference or exchange desync. Retaining algorithmic state."
                 )
                 
                 results.append(ReconciliationResult(
                     bot_id=genuine_in_trade_bots[0].bot_id,
                     bot_name=genuine_in_trade_bots[0].name,
                     pair=pair,
                     action_taken=ReconciliationAction.NO_ACTION,
                     details=f"Severe Notional Gap (${delta_notional:.2f}) observed on {pair}. Math is preserved. Check for manual tampering.",
                     requires_manual_intervention=True
                 ))
            
            # ------------------------------------------------------------------
            # CASE D: NET-SUM GHOST DETECTION (Revised Fix #1)
            # ------------------------------------------------------------------
            # Instead of strict direction checks (which break hedging), we check
            # the Net Error. If Virtual is too LONG vs Physical, we check LONG bots.
            # If a suspect bot has NO open orders, it's a ghost.
            # ------------------------------------------------------------------
            
            # 1. Calculate Net Mismatch
            # Virtual Net = (Longs) - (Shorts) in USD
            # Physical Net = (Longs) - (Shorts) in USD
            virt_net = virtual_net_usd
            phys_net = physical_net_usd
            net_error = virt_net - phys_net
            
            # 2. Identify Direction of Error
            if abs(net_error) > 50.0:
                error_side = 'LONG' if net_error > 0 else 'SHORT'
                logger.warning(
                    f"⚠️ [NET-MISMATCH] {pair_normalized}: Virtual Net=${virt_net:.2f}, Physical Net=${phys_net:.2f}. "
                    f"Diff=${net_error:.2f} (Virtual is too {error_side}). Checking for ghosts..."
                )
                
                # 3. Identify Suspects (Bots contributing to the error)
                suspects = [b for b in bots if b.in_trade and b.direction.upper() == error_side]
                
                for b in suspects:
                    # Get orders for THIS specific bot using normalized pair string
                    b_norm_pair = normalize_symbol(b.pair)
                    pair_orders = all_orders.get(b_norm_pair, [])
                    
                    # 4. Proof of Life Check: Does this bot have open orders?
                    bot_orders = [o for o in pair_orders if o.client_order_id and f"CQB_{b.bot_id}_" in o.client_order_id]
                    
                    if not bot_orders:
                        # We suspect this is a bot that hit TP while offline, but total pair position didn't drop to 0
                        # Check exchange history for proof of exit
                        exit_proof = self._find_proof_of_exit(b)
                        if exit_proof:
                            logger.info(f"✅ GHOST EXIT PROOF FOUND: Bot {b.name} (ID {b.bot_id}) exited via {exit_proof.get('clientOrderId')} at {exit_proof.get('timestamp')}.")
                            self._fix_ghost_bot(b, proof_order_id=f"GHOST_EXIT_{exit_proof.get('id')}")
                            results.append(ReconciliationResult(
                                bot_id=b.bot_id, bot_name=b.name, pair=b.pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                details="Reset Ghost Bot (Found exit fill in exchange history)", requires_manual_intervention=False
                            ))
                        else:
                            # FLAG ONLY — do not reset without proof. Bot may be between order placement cycles.
                            logger.warning(
                                f"⚠️ [NET-SUM-FLAG] Bot {b.name} (ID {b.bot_id}) is {b.direction} "
                                f"${b.total_invested:.2f} with no open orders. Contributes to net error. "
                                f"Flagging only — NO RESET."
                            )
                    else:
                         logger.info(f"🛡️ [GHOST-SAFE] Bot {b.name} has {len(bot_orders)} open orders.")

            # Case B / Case C: True Math Structural Memory Rebuilding
            # If there is an unexplained physical gap, we recalculate the absolute raw DB sum for active bots
            # natively spanning their entire ID history (ignoring ephemeral cycle boundaries).
            if abs(net_error) > 50.0:
                error_side = 'LONG' if net_error > 0 else 'SHORT'
                logger.critical(f"🛑 [NOTIONAL-GAP] {pair}: Mathematical Gap=${net_error:.2f}. Executing True Math ID-Based Historical Rebuild...")
                
                from .database import get_connection
                try:
                    conn = get_connection()
                    cursor = conn.cursor()
                    
                    for b in bots:
                        if not b.is_active: continue
                        
                        # Mathematically reconstruct position directly from bot_orders ID footprint
                        # 🚀 FUNDAMENTAL FIX: Sum 'filled_amount' instead of 'amount'
                        # We use COALESCE/CASE to support legacy fills where filled_amount might be 0 
                        # but status is 'filled' (treating those as 100% to avoid data loss, 
                        # while new orders follow strict receipt math).
                        # 🚀 FUNDAMENTAL ARCHITECTURE FIX: ONLY sum 'filled_amount'.
                        # The 'ELSE amount' fallback was the engine of hallucination.
                        # We also strictly filter for status IN ('filled', 'closed').
                        cursor.execute("""
                            SELECT 
                                SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption') 
                                    THEN filled_amount ELSE 0 END) as total_entries,
                                SUM(CASE WHEN order_type IN ('tp', 'close') 
                                    THEN filled_amount ELSE 0 END) as total_exits,
                                SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption') 
                                    THEN filled_amount * price ELSE 0 END) as total_notional
                            FROM bot_orders 
                            WHERE bot_id = ? AND status IN ('filled', 'closed')
                        """, (b.bot_id,))
                        
                        row = cursor.fetchone()
                        if row and row[0] is not None:
                            total_entries = float(row[0] or 0)
                            total_exits = float(row[1] or 0)
                            total_notional = float(row[2] or 0)
                            
                            active_qty = total_entries - total_exits
                            logger.info(f"📊 [MATH-TRACE] Bot {b.name}: Entries={total_entries:.6f}, Exits={total_exits:.6f}, Active={active_qty:.6f}, TotalNotional=${total_notional:.2f}")
                            
                            # If mathematically long on inventory within the db footprint
                            if active_qty > 0.0001:
                                avg_entry = total_notional / total_entries if total_entries > 0 else 0
                                expected_usd = active_qty * avg_entry
                                
                                # Compare true math footprint to its broken trades table memory
                                if abs(expected_usd - b.total_invested) > 10.0:
                                    logger.warning(f"🏗️ [MATH-HEAL] Bot {b.name} (ID {b.bot_id}): Memory=${b.total_invested:.2f}, True ID-Math=${expected_usd:.2f}. Overwriting DB memory with explicit native ID sum!")
                                    
                                    # Recover step from actual physical record
                                    cursor.execute("SELECT MAX(step) FROM bot_orders WHERE bot_id=? AND status IN ('filled','closed') AND order_type IN ('entry','grid')", (b.bot_id,))
                                    step_row = cursor.fetchone()
                                    derived_step = int(step_row[0]) if step_row and step_row[0] else 1
                                    
                                    import time
                                    # Overwrite existing trade table state (Upsert)
                                    cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (b.bot_id,))
                                    if cursor.fetchone():
                                        cursor.execute("""
                                            UPDATE trades 
                                            SET total_invested = ?, avg_entry_price = ?, current_step = ?
                                            WHERE bot_id = ?
                                        """, (expected_usd, avg_entry, derived_step, b.bot_id))
                                    else:
                                        cursor.execute("""
                                            INSERT INTO trades (bot_id, total_invested, avg_entry_price, current_step, entry_confirmed, basket_start_time)
                                            VALUES (?, ?, ?, ?, 1, ?)
                                        """, (b.bot_id, expected_usd, avg_entry, derived_step, int(time.time())))
                                        
                                    cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (b.bot_id,))
                                    conn.commit()
                                    
                                    results.append(ReconciliationResult(
                                        bot_id=b.bot_id, bot_name=b.name, pair=b.pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                        details=f"True Math Recovery: Restored ${expected_usd:.2f} via pure ID footprint.",
                                        requires_manual_intervention=False
                                    ))
                except Exception as e:
                    logger.error(f"Error during True Math Reconstruction: {e}")
                finally:
                    if 'conn' in locals(): conn.close()
                    
                # Deduplicate: only emit NET-GAP if we didn't already emit a Notional Gap or Zombie fix
                if not any(r.pair == pair for r in results):
                    res = ReconciliationResult(
                        bot_id=0, bot_name="NET-GAP", pair=pair, action_taken=ReconciliationAction.REQUIRE_MANUAL,
                        details=f"Net Error=${net_error:.2f}. Math Heal executed. If physically rogue, intervene manually.",
                        requires_manual_intervention=not any("Restored" in r.details for r in results if r.pair == pair)
                    )
                    results.append(res)
            else:
                # 🚀 MATH COMPILES PERFECTLY.
                # If there are bots in 'Scanning' state with real invested money,
                # they were stranded by safety locks during an offline fill.
                # Since the net error is near zero, it proves their positions are REAL.
                # We auto-promote them to 'IN TRADE' and assert 'entry_confirmed=1' so BotExecutor generates Grids.
                for b in bots:
                    # if a bot is actively in a trade (invested > 1.0) mathematically
                    if b.in_trade and b.total_invested > 1.0:
                        from .database import get_connection
                        try:
                            conn = get_connection()
                            cursor = conn.cursor()
                            cursor.execute("SELECT status, entry_confirmed FROM bots b LEFT JOIN trades t ON b.id=t.bot_id WHERE b.id=?", (b.bot_id,))
                            row = cursor.fetchone()
                            if row:
                                db_status, db_confirmed = row
                                if db_status and db_status.upper() == 'SCANNING':
                                    logger.warning(f"🔧 [RECON-HEAL] Perfect Physical Match. Unblocking Bot {b.name} from 'Scanning' to 'IN TRADE'.")
                                    cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (b.bot_id,))
                                    if not db_confirmed:
                                        cursor.execute("UPDATE trades SET entry_confirmed=1 WHERE bot_id=?", (b.bot_id,))
                                    conn.commit()
                        except Exception as e:
                            logger.error(f"Error healing Scanning lock: {e}")
                        finally:
                            if 'conn' in locals(): conn.close()

        return results

    def _find_proof_of_exit(self, bot: BotState) -> Optional[Dict]:
        """
        Searches exchange history for proof that the bot's position was closed.
        Uses the clientOrderId DNA (CQB_BOTID_TP/SL/...) AND verifies the cycle_id against bot_orders.
        """
        for mt, ex in self.exchanges.items():
            if not ex: continue
            try:
                # Fetch history for the specific pair (High limit to catch old exits)
                history = ex.fetch_closed_orders(bot.pair, limit=1000)
                if not isinstance(history, list):
                    continue
                
                conn = get_connection()
                cur = conn.cursor()
                
                expected_cycle = bot.cycle_id
                
                for order in history:
                    cid = order.get('clientOrderId', '')
                    # We look for any exit order from this bot
                    if cid.startswith(f"CQB_{bot.bot_id}_TP_") or cid.startswith(f"CQB_{bot.bot_id}_SL_"):
                        if order.get('status') in ['closed', 'filled']:
                            # 🛡️ CYCLE ID GUARD: Ask DB if this order belongs to the CURRENT cycle
                            cur.execute("SELECT cycle_id, status FROM bot_orders WHERE order_id=? OR client_order_id=?", 
                                        (order.get('id'), cid))
                            row = cur.fetchone()
                            
                            if row:
                                db_cycle = row[0]
                                db_status = row[1]
                                if db_cycle == expected_cycle and db_status not in ['reset_cleared', 'auto_closed']:
                                    conn.close()
                                    return order
                                else:
                                    logger.debug(f"🛑 [GHOST-EXIT-GUARD] Found TP {cid} but rejected: Belongs to cycle {db_cycle} (Current: {expected_cycle}) or status={db_status}")
                            else:
                                logger.debug(f"🛑 [GHOST-EXIT-GUARD] Found TP {cid} but rejected: Not found in bot_orders (Historical Run)")
                
                conn.close()
            except Exception as e:
                logger.error(f"Failed to fetch exit proof for Bot {bot.bot_id}: {e}")
        return None

    def _verify_entry_existence(self, bot: BotState) -> bool:
        """
        Verifies if the bot's supposed Entry Order actually exists and is filled.
        Returns True if valid entry found, False if missing/cancelled (Phantom).
        """
        if not bot.entry_order_id:
            return False # No Order ID = Phantom State

        for mt, ex in self.exchanges.items():
            if not ex: continue
            try:
                # remove prefix if needed, though fetch_order handles ID usually
                # We need to use the exchange interface wrapper
                order = ex.fetch_order(bot.entry_order_id, bot.pair)
                if order:
                    status = order.get('status', '').lower()
                    if status in ['filled', 'closed']:
                        return True
                    if status in ['canceled', 'rejected', 'expired']:
                        return False # Explicitly failed
                
            except Exception as e:
                logger.warning(f"Entry verification failed for {bot.entry_order_id}: {e}")
                # If 404/Not Found, usually raises exception. 
                # CCXT usually raises OrderNotFound.
                if "OrderNotFound" in str(e) or "not found" in str(e).lower():
                    return False
        
        # If we checked all exchanges and found nothing... assume False (Phantom)?
        # Or be conservative?
        # If ID format looks real, returning False is checking "Reality".
        return False

    def _lock_irreconcilable_bot(self, bot: BotState):
        """NEUTERED: Warn only — never deactivate bots automatically."""
        logger.warning(
            f"⚠️ Bot {bot.name} (ID {bot.bot_id}) has irreconcilable state. "
            f"Flagging only — NO LOCK, NO DEACTIVATION."
        )


    # ------------------------------------------------------------------
    # MAIN ENTRY POINT
    # ------------------------------------------------------------------
    def reconcile_all(self, force_adoption: bool = False):
        logger.info("🔄 STARTING RECONCILIATION CYCLE")
        
        # 0. Phantom Entry Cleanup (FIX #2)
        # Detects bots with total_invested > 0 but entry_confirmed=0 and avg_entry=0
        # These are phantom entries that were never actually filled.
        try:
            self._cleanup_phantom_entries()
        except Exception as e:
            logger.error(f"Phantom entry cleanup failed: {e}")
        
        # 1. Offline Fills (Updates DB)
        self.reconstruct_offline_fills()
        
        # 2. Fetch Fresh State
        bot_states = self.get_bot_states()
        success, all_positions = self.fetch_all_exchange_positions()
        
        if not success:
            logger.error("🛑 [RECON-ABORT] Exchange position fetch failed. Skipping net-sum check to prevent false resets.")
            return results

        # Flatten orders
        all_pairs = list(set([b.pair for b in bot_states]))
        all_orders = self.fetch_all_exchange_orders(all_pairs)
        
        results = []
        
        # 3. Individual Bot Validation (Zombies)
        zombie_results = self.validate_individual_bots(bot_states, all_orders)
        results.extend(zombie_results)
        
        # 4. Global Net Check & Resolution (now includes Net-Sum ghost detection)
        net_results = self.resolve_net_mismatch(bot_states, all_positions, all_orders, force_adoption=force_adoption)
        results.extend(net_results)
        
        logger.info(f"✅ RECONCILIATION COMPLETE. {len(results)} actions taken.")
        return results

    def _cleanup_phantom_entries(self):
        """
        FIX #2: Detects and resets phantom entries.
        A phantom entry has total_invested > 0 but entry_confirmed = 0 AND avg_entry_price = 0.
        This means an entry order was recorded in DB but never actually filled.
        """
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.bot_id, b.name, b.pair, t.total_invested
            FROM trades t
            JOIN bots b ON t.bot_id = b.id
            WHERE t.total_invested > 0 
              AND t.entry_confirmed = 0 
              AND (t.avg_entry_price IS NULL OR t.avg_entry_price = 0)
        """)
        phantoms = cursor.fetchall()
        
        for bot_id, bot_name, pair, invested in phantoms:
            logger.warning(
                f"👻 [PHANTOM-ENTRY] Bot {bot_name} (ID {bot_id}): "
                f"invested=${invested:.2f} but entry_confirmed=0 and avg_entry=0. Auto-resetting."
            )
            cursor.execute("""
                UPDATE trades 
                SET total_invested=0, current_step=0, avg_entry_price=0, 
                    entry_confirmed=0, basket_start_time=?
                WHERE bot_id=?
            """, (int(time.time()), bot_id))  # 🚀 FUNDAMENTAL FIX: Never set basket_start_time=0. Always stamp reset time so EE fires on next cycle.
            cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot_id,))
            
            from .database import log_reconciliation
            log_reconciliation(
                bot_id=bot_id,
                pair=pair,
                action="RESET_PHANTOM_ENTRY",
                details=f"Phantom entry reset: invested=${invested:.2f}, confirmed=0, avg_entry=0"
            )
            log_trade(
                bot_id=bot_id, action='PHANTOM_RESET', symbol=pair,
                price=0, amount=0, cost_usdc=0,
                order_id=f"PHANTOM_{int(time.time())}",
                step=0, notes="Auto-reset phantom entry (never filled)", pnl=0
            )
        
        if phantoms:
            conn.commit()
            logger.info(f"✅ Cleaned up {len(phantoms)} phantom entries.")
        conn.close()

    def _fix_ghost_bot(self, bot: BotState, proof_order_id: str = "MANUAL_FIX"):
        """
        Resets a ghost bot to IDLE state.
        This is used when a bot claims a position but verification proves it doesn't exist.
        """
        try:
            logger.info(f"🔧 Fixing Ghost Bot {bot.name} (ID {bot.bot_id})...")
            
            # Log the fix to trade history
            log_trade(
                bot_id=bot.bot_id,
                action='GHOST_RESET',
                symbol=bot.pair,
                price=0,
                amount=0,
                cost_usdc=0,
                order_id=f"GHOST_{int(time.time())}",
                step=bot.current_step,
                notes=f"System reset: {proof_order_id}",
                pnl=0
            )
            
            # Reset State in DB
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades 
                SET total_invested=0, current_step=0, entry_confirmed=0, basket_start_time=?,
                    avg_entry_price=0, target_tp_price=0
                WHERE bot_id=?
            """, (int(time.time()), bot.bot_id))  # 🚀 FUNDAMENTAL FIX: Never set basket_start_time=0. Always stamp reset time so EE fires on next cycle.
            cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (bot.bot_id,))
            
            # Also cancel any open internal orders to prevent zombie revival
            cursor.execute("UPDATE bot_orders SET status='cancelled' WHERE bot_id=? AND status='open'", (bot.bot_id,))
            
            conn.commit()
            conn.close()
            logger.info(f"✅ Ghost Bot {bot.name} successfully reset to Scanning.")
            
        except Exception as e:
            logger.error(f"Failed to fix ghost bot {bot.name}: {e}")

    # ------------------------------------------------------------------
    # HELPER METHODS (Preserved/Adapted)
    # ------------------------------------------------------------------
    def fetch_all_exchange_positions(self) -> Tuple[bool, Dict[str, List[ExchangePosition]]]:
        all_pos = {}
        success = True
        seen_positions = set()
        for mt, ex in self.exchanges.items():
            if not ex: continue
            try:
                raw = ex.fetch_positions()
                if not isinstance(raw, list):
                    logger.warning(f"⚠️ fetch_positions returned non-list for exchange '{mt}': {raw}. Skipping.")
                    continue
                for p in raw:
                    sym = normalize_symbol(p.get('symbol', ''))
                    side = 'LONG' if float(p.get('contracts',0) or p.get('size',0)) > 0 else 'SHORT'
                    
                    # Deduplicate globally using symbol and side to prevent double-counting if endpoints overlap
                    pos_id = f"{sym}_{side}"
                    if pos_id in seen_positions:
                        continue
                    seen_positions.add(pos_id)
                    
                    pos = ExchangePosition(
                        symbol=sym,
                        side=side,
                        size=abs(float(p.get('contracts',0) or p.get('size',0))),
                        entry_price=float(p.get('entryPrice',0)),
                        mark_price=float(p.get('markPrice',0)),
                        unrealized_pnl=float(p.get('unrealizedPnl',0))
                    )
                    if sym not in all_pos: all_pos[sym] = []
                    all_pos[sym].append(pos)
            except Exception as e:
                logger.error(f"Pos fetch failed {mt}: {e}")
                success = False
        return success, all_pos

    def fetch_all_exchange_orders(self, pairs: List[str]) -> Dict[str, List[ExchangeOrder]]:
        orders_by_pair = {}
        seen_order_ids = set() # Prevent duplicated orders from overlapping API endpoints
        for pair in pairs:
            norm_pair = normalize_symbol(pair)
            if norm_pair not in orders_by_pair:
                orders_by_pair[norm_pair] = []
            for mt, ex in self.exchanges.items():
                if not ex: continue
                try:
                    raw = ex.fetch_open_orders(pair)
                    if not isinstance(raw, list):
                        logger.warning(f"⚠️ fetch_open_orders returned non-list for exchange '{mt}', pair '{pair}': {raw}. Skipping.")
                        continue
                    for o in raw:
                        oid = str(o.get('id', ''))
                        if oid in seen_order_ids:
                            continue
                        seen_order_ids.add(oid)
                        
                        orders_by_pair[norm_pair].append(ExchangeOrder(
                            order_id=oid,
                            symbol=pair,
                            side=o.get('side',''),
                            order_type=o.get('type','limit'),
                            price=float(o.get('price',0) or 0),
                            amount=float(o.get('amount',0) or 0),
                            status=o.get('status','open'),
                            client_order_id=o.get('clientOrderId')
                        ))
                except: pass
        return orders_by_pair

    def get_bot_states(self) -> List[BotState]:
        bots = get_all_bots()
        states = []
        conn = get_connection()
        cursor = conn.cursor()
        
        for b in bots:
            bot_id, name, pair, is_active = b[0], b[1], b[2], b[3]
            logger.debug(f"🔍 [GET-STATE] Examining Bot {bot_id} (Active={is_active})")
            
            # The get_bot_status function is imported from .database,
            # so we assume it's modified to return 'cycle_id' at index 13.
            # The instruction implies modifying the *source* of get_bot_status.
            # For this file, we just use the updated return value.
            status = get_bot_status(bot_id)
            if not status: 
                logger.warning(f"⚠️ [GET-STATE] No status found for Bot {bot_id}")
                continue
            
            # Read the actual entry_confirmed flag from the database
            order_ids = get_bot_order_ids(bot_id)
            confirmed = bool(status.get('entry_confirmed', False))
            
            states.append(BotState(
                bot_id=bot_id,
                name=name,
                pair=pair,
                direction=status['direction'],
                is_active=bool(is_active),
                in_trade=status['total_invested'] > 0,
                total_invested=status['total_invested'],
                avg_entry_price=status['avg_entry_price'],
                target_tp_price=status['target_tp_price'],
                current_step=status['current_step'],
                basket_start_time=status['basket_start_time'],
                entry_order_id=str(order_ids.get('entry_order_id')) if order_ids.get('entry_order_id') else None,
                tp_order_id=str(order_ids.get('tp_order_id')) if order_ids.get('tp_order_id') else None,
                has_confirmed_entry=confirmed,
                cycle_id=status.get('cycle_id', 1)
            ))
        conn.close()
        return states

# Alias for backward compatibility if needed
DeepReconciler = StateReconciler
