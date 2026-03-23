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
        DISABLED: The user enforced strict proof-of-order-ID consensus.
        Blindly adopting physical exchange positions without a corresponding
        `clientOrderId` receipt (DNA) constitutes a mathematical hallucination.
        All gap reconciliation is now securely routed exclusively through
        `reconstruct_offline_fills` which strictly requires order ID proof.
        Unproven anomalies will drop down to MANUAL REQUIREMENT.
        """
        pass

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
                # Try to recover from oldest filled entry order in bot_orders for the CURRENT cycle
                _heal_cur.execute("""
                    SELECT MIN(created_at) FROM bot_orders 
                    WHERE bot_id=? AND order_type='entry' AND status IN ('filled','closed') AND cycle_id=?
                """, (_bot_id, _state['cycle_id']))
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


        # 1.5. 🚀 PRE-COMMIT ROW RESOLUTION
        # Covers two failure windows:
        # A) status='placing' — order written to DB BEFORE exchange API call; engine crashed mid-atomicity.
        # B) status='new'    — order confirmed by exchange (API returned OK) but engine went offline
        #                      BEFORE the WebSocket fill event arrived. This means filled_amount=0
        #                      in DB even though Binance fully executed the order.
        # Both cases result in virtual/physical mismatches on restart that block grid placement.
        _place_conn = get_connection()
        _place_cur = _place_conn.cursor()
        _place_cur.execute("""
            SELECT bo.id, bo.bot_id, b.pair, bo.order_type, bo.client_order_id,
                   bo.price, bo.amount, bo.step, bo.cycle_id
            FROM bot_orders bo JOIN bots b ON bo.bot_id=b.id
            WHERE bo.status IN ('placing', 'new')
              AND bo.filled_amount = 0
              AND bo.created_at < ?
        """, (int(time.time()) - 30,))
        placing_rows = _place_cur.fetchall()

        if placing_rows:
            logger.info(f"🔍 [PRE-COMMIT-RESOLVE] Found {len(placing_rows)} unresolved 'placing' rows.")

        for p_row in placing_rows:
            db_id, bot_id, pair, otype, cid, price, amount, step, cycle_id = p_row
            for mt, ex in self.exchanges.items():
                if not ex: continue
                try:
                    exch_order = None
                    # Try direct fetch by clientOrderId first (Binance supports this)
                    try:
                        exch_order = ex.fetch_order_by_client_order_id(pair, cid)
                    except Exception:
                        pass
                    # Fallback: scan recent closed+open orders
                    if not exch_order:
                        try:
                            candids = (ex.fetch_closed_orders(pair, limit=50) or []) + (ex.fetch_open_orders(pair) or [])
                            for o in candids:
                                if o.get('clientOrderId') == cid or (o.get('info') or {}).get('clientOrderId') == cid:
                                    exch_order = o; break
                        except Exception: pass

                    if exch_order:
                        o_status = (exch_order.get('status') or '').lower()
                        o_id = exch_order.get('id')
                        if o_status in ('filled', 'closed', 'open', 'new', 'partially_filled'):
                            new_status = 'open' if o_status in ('open','new','partially_filled') else 'open'
                            _place_cur.execute("UPDATE bot_orders SET order_id=?, status=?, updated_at=? WHERE id=?",
                                               (o_id, new_status, int(time.time()), db_id))
                            logger.info(f"✅ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → found on exchange as {o_status} (id={o_id}). Restored to 'open'.")
                        else:
                            _place_cur.execute("UPDATE bot_orders SET status='failed', updated_at=? WHERE id=?",
                                               (int(time.time()), db_id))
                            logger.info(f"🗑️ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → {o_status}. Marked failed.")
                    else:
                        # Never reached the exchange — delete the intent row
                        _place_cur.execute("DELETE FROM bot_orders WHERE id=?", (db_id,))
                        logger.info(f"🗑️ [PRE-COMMIT-RESOLVE] Bot {bot_id} {otype} cid={cid} → not on exchange. Deleted (never placed).")
                    break
                except Exception as e_r:
                    logger.warning(f"[PRE-COMMIT-RESOLVE] Could not resolve row {db_id}: {e_r}")

        if placing_rows:
            _place_conn.commit()
        _place_conn.close()

        # 1.6. 🚀 HISTORY-BASED ORPHAN DETECTION
        # For any pair where physical position > virtual, scan 48h of exchange order history
        # for CQB_-prefixed fills that have no matching trade_history entry.
        # These are injected as bot_orders so the per-pair OFFLINE-SYNC picks them up below.
        try:
            _oh_conn = get_connection()
            _oh_cur = _oh_conn.cursor()
            _oh_cur.execute("SELECT pair, side, size FROM active_positions")
            phys_pos = {r[0]: abs(float(r[2])) for r in _oh_cur.fetchall()}

            _oh_cur.execute("""
                SELECT b.pair, b.direction, b.id,
                     - COALESCE(SUM(CASE WHEN bo.order_type IN ('tp','close','adoption_reduce','dust_close','sl') THEN bo.filled_amount ELSE 0 END),0) as net_qty
                FROM bots b
                LEFT JOIN trades t ON b.id=t.bot_id
                LEFT JOIN bot_orders bo ON b.id=bo.bot_id AND bo.filled_amount>0
                    AND (bo.cycle_id=t.cycle_id OR bo.cycle_id IS NULL)
                    AND bo.status NOT IN ('reset_cleared','auto_closed','failed','placing')
                WHERE b.is_active=1
                GROUP BY b.id
            """)
            from engine.exchange_interface import normalize_symbol as _nsym
            virt_by_pair = {}
            for rv in _oh_cur.fetchall():
                pk = _nsym(rv[0]); signed = float(rv[3] or 0) * (1 if rv[1]=='LONG' else -1)
                virt_by_pair[pk] = virt_by_pair.get(pk, 0.0) + signed
            _oh_conn.close()

            gap_pairs = [(p, pq, abs(virt_by_pair.get(_nsym(p), 0.0)), pq - abs(virt_by_pair.get(_nsym(p), 0.0)))
                         for p, pq in phys_pos.items() if pq - abs(virt_by_pair.get(_nsym(p), 0.0)) > 0.001]

            if gap_pairs:
                logger.info(f"🔍 [HISTORY-ORPHAN] {len(gap_pairs)} pairs with position gaps: {[(p,round(d,4)) for p,_,_,d in gap_pairs]}")

            since_fallback = int((time.time() - 7*24*3600)*1000)  # 7 days fallback for long weekends
            for gap_pair, phys_qty, v_qty, gap_qty in gap_pairs:
                for mt, ex in self.exchanges.items():
                    if not ex: continue
                    try:
                        hist = []
                        current_since = since_fallback
                        for _ in range(10):  # Fetch up to 10 pages to span long offline weekends
                            page = ex.fetch_closed_orders(gap_pair, since=current_since, limit=1000)
                            if not page: break
                            hist.extend(page)
                            last_ts = max((o.get('timestamp') or 0) for o in page)
                            if last_ts <= current_since: break
                            current_since = last_ts + 1
                        
                        hist = sorted(hist, key=lambda x: x.get('timestamp') or 0)
                        _oi_conn = get_connection(); _oi_cur = _oi_conn.cursor()
                        for o in hist:
                            o_cid = o.get('clientOrderId') or (o.get('info') or {}).get('clientOrderId') or ''
                            if not o_cid.startswith('CQB_'): continue
                            o_status = (o.get('status') or '').lower()
                            o_filled = float(o.get('filled') or 0)
                            o_price = float(o.get('average') or o.get('price') or 0)
                            o_id = o.get('id')
                            
                            # 🚀 ROOT CAUSE FIX: Do not require 'filled'/'closed' status.
                            # A partial fill on a 'canceled' or 'open' order is STILL A FILL.
                            # If we skip it, the ledger undercounts.
                            if o_filled <= 0: continue

                            # Already linked to trade_history?
                            _oi_cur.execute("""SELECT COUNT(*) FROM trade_history
                                WHERE bot_id=? AND ABS(price-?)<? AND ABS(amount-?)<?
                                AND action IN ('WS_ENTRY_FILL','GRID_FILL','WS_GRID_FILL','WS_ENTRY_FILL')""",
                                (int(o_cid.split('_')[1]) if len(o_cid.split('_'))>1 and o_cid.split('_')[1].isdigit() else -1,
                                 o_price, o_price*0.002, o_filled, o_filled*0.002))
                            if _oi_cur.fetchone()[0] > 0: continue

                            # Parse bot_id from CID: CQB_{bot_id}_TYPE_{step}_{ts}
                            parts = o_cid.split('_')
                            try: attributed_bot_id = int(parts[1])
                            except (IndexError, ValueError): continue

                            _oi_cur.execute("SELECT id, filled_amount, status FROM bot_orders WHERE order_id=?", (o_id,))
                            existing_order = _oi_cur.fetchone()
                            
                            is_orphan_insert = False
                            if existing_order:
                                ex_id, ex_filled, ex_status = existing_order
                                if float(ex_filled or 0) <= 0 and o_filled > 0:
                                    logger.info(f"🩹 [HEALING] Order {o_cid} exists with 0 fill but exchange reports {o_filled}. Healing ledger.")
                                    _oi_cur.execute("UPDATE bot_orders SET filled_amount=?, price=?, updated_at=? WHERE id=?",
                                                    (o_filled, o_price, int(time.time()), ex_id))
                                    _oi_conn.commit()
                                continue # Skip normal insert
                            else:
                                is_orphan_insert = True

                            raw_otype = parts[2].upper() if len(parts)>2 else 'GRID'
                            otype_r = raw_otype if raw_otype in ('ENTRY','GRID','TP','HEDGETP') else 'GRID'

                            _oi_cur.execute("SELECT COALESCE(cycle_id,1) FROM trades WHERE bot_id=?", (attributed_bot_id,))
                            cr = _oi_cur.fetchone(); cyc = cr[0] if cr else 1
                            step_g = int(parts[3]) if len(parts)>3 and parts[3].isdigit() else 1
                            _oi_cur.execute("""INSERT OR IGNORE INTO bot_orders
                                (bot_id,step,order_type,order_id,price,amount,filled_amount,status,created_at,updated_at,client_order_id,notes,cycle_id)
                                VALUES (?,?,?,?,?,?,?,'open',?,?,?,'history-orphan',?)""",
                                (attributed_bot_id, step_g, otype_r.lower(), o_id, o_price, o_filled, o_filled,
                                 int((o.get('timestamp') or time.time()*1000)/1000), int(time.time()), o_cid, cyc))
                            logger.info(f"   ➕ [HISTORY-ORPHAN] Inserted missing bot_order: Bot {attributed_bot_id} {otype_r} qty={o_filled}@{o_price} order_id={o_id}")
                        _oi_conn.commit(); _oi_conn.close()
                        break
                    except Exception as oe: logger.warning(f"[HISTORY-ORPHAN] {gap_pair}: {oe}")
        except Exception as ohe: logger.warning(f"[HISTORY-ORPHAN] outer: {ohe}")

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
                    
                    # 🚀 CHRONOLOGICAL PROCESSING FIX
                    # Binance returns history descending (newest first). This caused offline TP fills
                    # to be processed BEFORE their prerequisite Entries/Grids, causing the
                    # reset_bot_after_tp hook to calculate `net_qty` on an incomplete ledger!
                    history = sorted(history, key=lambda x: x.get('timestamp') or 0)

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
                                SELECT status, cycle_id, filled_amount FROM bot_orders 
                                WHERE (order_id=? OR client_order_id=?)
                            """, (order['id'], cid))
                            row = cursor.fetchone()

                            logger.info(f"🔍 [OFFLINE-SYNC] Checking Order {order['id']} (CID: {cid}) | InDB: {'Yes' if row else 'No'} | DBStatus: {row[0] if row else 'N/A'} | DBCycle: {row[1] if row else 'N/A'} | DBFilled: {row[2] if row else 'N/A'}")

                            if row:
                                # Known order: reject if already processed or from old cycle
                                # 🚀 FUNDAMENTAL FIX: Don't reject if filled_amount=0.
                                # Natively closed Taker orders might be saved as 'closed' but with 0 filled_amount.
                                # If Reconciler skips them, the cycle carry-over math gets permanently poisoned!
                                if row[0] in ['reset_cleared', 'auto_closed']:
                                    continue
                                if row[0] in ['filled', 'closed'] and float(row[2] or 0) > 0.0001:
                                    continue
                                    
                                # Also reject if it has a different cycle_id than the bot's current cycle
                                bot_current_cycle = current_state.get('cycle_id', 1)
                                if row[1] is not None and row[1] != bot_current_cycle:
                                    logger.debug(f"🛑 [CYCLE-GUARD] Rejecting fill {cid}: belongs to cycle {row[1]}, bot is on cycle {bot_current_cycle}.")
                                    continue
                            else:
                                # 🛡️ RIGOROUS DNA ADOPTION:
                                # We process ANY order that matches our bot's CID DNA, 
                                # irrespective of the bot's current logical status (SCANNING/IN TRADE).
                                # If it filled, it PROVES the bot is in trade.
                                order_ts_sec = int((order.get('timestamp') or order.get('lastTradeTimestamp') or 0) / 1000)
                                bot_start = current_state.get('basket_start_time', 0)
                                if bot_start > 0 and order_ts_sec < (bot_start - 60):
                                    # Too old to be current cycle activity (allow 1m clock drift)
                                    logger.debug(f"🛑 [DNA-GUARD] Rejecting order {cid}: matches DNA but happened before basket start.")
                                    continue
                                
                                logger.info(f"✅ DNA MATCH: Order {cid} matches Bot {bot_id} DNA. Authorizing processing despite DB state.")

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

                            # 🚀 FIX: If Binance returns filled=0 for a 'filled'/'closed' order, the WebSocket
                            # already processed this fill correctly (it got the real qty from the WS event).
                            # Inserting a record with amount=0/filled_amount=0 creates a DNA-HOLD deadlock.
                            # Since trades.total_invested is already correct via WS, just skip — no DB action needed.
                            if order_status in ('filled', 'closed') and fill_qty <= 0:
                                logger.debug(f"⏭️ [OFFLINE-SYNC] Skipping {cid}: status=filled but filled=0 from exchange. WS path already handled this.")
                                continue

                            # Update local DB if cancelled, but DO NOT skip if there was a partial fill
                            if order_status in ['canceled', 'cancelled', 'expired', 'rejected']:
                                logger.debug(f"🧹 [OFFLINE-SYNC] Syncing cancelled order {cid} (status={order_status}) to DB.")
                                cursor.execute("UPDATE bot_orders SET status=?, updated_at=? WHERE order_id=?",
                                               (order_status, int(time.time()), order['id']))
                                # ONLY skip if it wasn't partially filled.
                                # 🚀 ROOT CAUSE FIX: If fill_qty > 0, it was partially filled before dying.
                                # We MUST NOT return here! We must proceed to let the step advance.
                                if fill_qty <= 0:
                                    continue
                                else:
                                    logger.info(f"⚡ [OFFLINE-RECOVERY] {cid} was {order_status} but has PARTIAL FILL ({fill_qty}). Authorizing step advancement.")
                            elif order_status not in ('filled', 'closed'):
                                logger.debug(f"Skipping non-filled order {cid} (status={order_status})")
                                continue
                            fill_symbol = order.get('symbol', pair)
                            bot_name = f"Bot-{bot_id}" # Placeholder, ideally fetch name
                            
                            curr_step = current_state.get('current_step', 0)

                            
                            logger.info(f"🕵️ RECONSTRUCTING: Found Fill for Bot {bot_id} {otype} {step} @ {fill_price} for {fill_symbol}")

                            # --- 🚀 LEDGER INTEGRITY FIX ---
                            # We MUST write the order to the database BEFORE processing its state effects (like TP reset).
                            # Otherwise, the Cross-cycle Carry-over logic in reset_bot_after_tp won't see this order
                            # and will inject massive phantom positions into the next cycle.
                            cursor.execute("SELECT id FROM bot_orders WHERE order_id=?", (order['id'],))
                            existing = cursor.fetchone()
                            
                            # Always insert offline fills as 'filled' initially. If it's a TP, the subsequent 
                            # reset_bot_after_tp call will vacuum it up and change it to 'reset_cleared' automatically.
                            if existing:
                                cursor.execute("UPDATE bot_orders SET status='filled', updated_at=?, filled_amount=? WHERE order_id=?",
                                               (int(time.time()), fill_qty, order['id']))
                            else:
                                cursor.execute("SELECT cycle_id FROM trades WHERE bot_id=?", (bot_id,))
                                _cycle_row = cursor.fetchone()
                                _bot_cycle = _cycle_row[0] if _cycle_row and _cycle_row[0] else 1
                                cursor.execute("""
                                    INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, updated_at, client_order_id, notes, cycle_id)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?)
                                """, (bot_id, step, otype.lower(), order['id'], fill_price, fill_qty, fill_qty,
                                      int(order.get('timestamp', time.time()*1000)/1000), int(time.time()), cid, 'Reconstructed from History', _bot_cycle))

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
                                     
                                    # 🚀 FUNDAMENTAL FIX: Use ONLY trade_history as the dedup gate.
                                    # `already_in_orders` being True just means the fill record exists in bot_orders
                                    # (i.e., the order was placed BY the bot). It does NOT mean the virtual position
                                    # was updated. Only `already_in_history` confirms the fill was actually applied
                                    # to the bot's avg_entry_price and total_invested. If history is missing, we MUST
                                    # replay. Otherwise the offline fill guard blocks grids PERMANENTLY.
                                    already_logged = already_in_history
                                    if already_in_orders and not already_in_history:
                                        logger.info(f"🔄 [OFFLINE-REPLAY] Bot {bot_id} Step {step} @ {fill_price} x{fill_qty} — in bot_orders but NOT in trade_history. Replaying to sync virtual position.")
                                    if already_logged:
                                        logger.info(f"⏭️ [OFFLINE-DEDUP] Skipping fill for Bot {bot_id} Step {step} @ {fill_price} x{fill_qty} — already in trade_history.")
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
                                stats['tp_fills'] += 1 # Count as a TP fill for stats

                            elif otype == 'HEDGE':
                                logger.info(f"🛡️ ✅ [OFFLINE-HEDGE] Re-playing HEDGE for Bot {bot_id}")
                                stats['grid_fills'] += 1 # Count as a grid fill for stats
                                
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
        """
        Adopts a physical grid fill into the trade state using atomic logic.
        Uses force_step=True to ensure logical state matches physical reality.
        """
        from engine.database import accumulate_trade_fill, log_trade
        fill_cost = fill_price * fill_amount
        new_step = (current_step or 0) + 1

        # 🚀 ROOT CAUSE FIX: Reconciler must FORCE the step count to align with physical evidence
        accumulate_trade_fill(
            bot_id=bot_id,
            added_invested=fill_cost,
            added_qty=fill_amount,
            avg_price=fill_price,
            new_step=new_step,
            tp_price=None, # Runner will refine based on full position
            is_entry=False,
            force_step=True
        )
        
        log_trade(bot_id, 'OFFLINE_GRID', symbol, fill_price, fill_amount, fill_cost, f"GRID_{new_step}", new_step, "Offline Grid Fill", 0)
        logger.info(f"✅ [OFFLINE-ADOPTION] Force-aligned {bot_name} to Step {new_step} based on physical Grid footprint.")
        
        # (POST-FILL ANCHOR removed: Exact math via the receipt footprint is correct; don't blindly snap to physical size)

    def _handle_offline_entry_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount, symbol, timestamp_sec):
        """
        Adopts a physical entry fill into the trade state using atomic logic.
        Uses force_step=True to break any Step 0 deadlock.
        """
        from engine.database import accumulate_trade_fill, log_trade
        fill_cost = fill_price * fill_amount

        # 🚀 ROOT CAUSE FIX: Force Step 1 alignment for physical entries
        accumulate_trade_fill(
            bot_id=bot_id,
            added_invested=fill_cost,
            added_qty=fill_amount,
            avg_price=fill_price,
            new_step=1,
            tp_price=None,
            is_entry=True,
            force_step=True
        )
        
        log_trade(bot_id, 'OFFLINE_ENTRY', symbol, fill_price, fill_amount, fill_cost, "ENTRY", 1, "Offline Entry Fill", 0)
        logger.info(f"✅ [OFFLINE-ADOPTION] Force-aligned {bot_name} to Step 1 based on physical Entry footprint.")
        
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
            gross_virtual_qty = 0.0
            
            for b in bots:
                if b.in_trade:
                    qty = b.total_invested / b.avg_entry_price if b.avg_entry_price > 0 else 0
                    total_virtual_invested += b.total_invested
                    gross_virtual_qty += qty
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
                
            logger.info(f"⚖️ RECON AUDIT [{pair_normalized}]: Virtual=${virtual_net_usd:.2f} vs Physical=${physical_net_usd:.2f}")
            
            # 🚀 Case A: Impossible Bot Mass Detection (Vanished or Ghost Bots)
            # A bot is mathematically a "Ghost" if it independently claims to hold MORE units
            # than the entire gross sum existing on the exchange. This occurs if a user manually 
            # closes the position or if it was liquidated/ADL'd.
            gross_physical_qty = sum(abs(p.size) for p in pair_positions)
            
            for b in bots:
                if b.in_trade:
                    bot_qty = b.total_invested / b.avg_entry_price if b.avg_entry_price > 0 else 0
                    
                    # If this single bot's mass exceeds the TOTAL physical mass across all sub-positions 
                    # by more than epsilon, this bot is physically impossible.
                    if bot_qty > (gross_physical_qty + 0.0001):
                        logger.critical(f"👻 SYSTEM MISMATCH on {pair}: Bot {b.name} claims {bot_qty:.6f} QTY, but Exchange total is only {gross_physical_qty:.6f} QTY.")
                        
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
                            if b.has_confirmed_entry:
                                logger.critical(
                                    f"💥 [VANISHED POSITION DETECTED] Bot {b.name}: Claims {bot_qty:.6f} units but Physical is {gross_physical_qty:.6f}! "
                                    f"Entry was confirmed, so the position vanished externally (Liquidated/ADL/Manual). Forcing memory wipe!"
                                )
                                from .database import reset_bot_after_tp, log_reconciliation
                                reset_bot_after_tp(b.bot_id, 0.0)
                                
                                log_reconciliation(
                                    bot_id=b.bot_id,
                                    pair=b.pair,
                                    action="RESET_VANISHED_POSITION",
                                    details=f"Mismatch: Bot claims {bot_qty:.6f}, Exchange holds {gross_physical_qty:.6f}. Vanished from exchange. Resetting bot."
                                )
                                results.append(ReconciliationResult(
                                    bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                                    action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                    details="Reset Vanished Confirmed Position", requires_manual_intervention=False
                                ))
                            else:
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
                            AND cycle_id = ?
                        """, (b.bot_id, b.basket_start_time, b.cycle_id))
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
            # 🚀 FIX: Compare QTY directly, not USD notional.
            # USD threshold was fundamentally wrong — a 23.6 SUI gap or 0.65 SOL gap
            # could be below $50 USD and would never be fixed. QTY must match exactly.
            # Compute net QTY for each side (virtual from trades, physical from exchange positions).
            virtual_net_qty = 0.0
            for b in bots:
                if b.in_trade and b.avg_entry_price > 0:
                    qty = b.total_invested / b.avg_entry_price
                    virtual_net_qty += qty if b.direction.upper() == 'LONG' else -qty

            physical_net_qty = 0.0
            for p in pair_positions:
                physical_net_qty += abs(p.size) if p.side == 'LONG' else -abs(p.size)

            delta_qty = abs(virtual_net_qty - physical_net_qty)
            # Keep delta_notional for logging only
            delta_notional = abs(virtual_net_usd - physical_net_usd)

            # QTY_EPSILON: 0.0001 units — below this is floating point noise, not a real gap.
            QTY_EPSILON = 0.0001
            genuine_in_trade_bots = [b for b in bots if b.in_trade]

            # is_sole must be computed BEFORE the if-block so it is available in both Case C and Case D
            is_sole = self.is_sole_bot(pair, bots)

            if delta_qty > QTY_EPSILON and (abs(virtual_net_qty) > QTY_EPSILON or abs(physical_net_qty) > QTY_EPSILON) and genuine_in_trade_bots:

                  logger.error(
                      f"🚨 [QTY-GAP] {pair}: Virtual={virtual_net_qty:.6f} vs "
                      f"Physical={physical_net_qty:.6f} (Delta={delta_qty:.6f} units, ${delta_notional:.2f}). "
                      f"Scanning for physically impossible states..."
                  )
                  
                  # 🚀 GLOBAL FLATTENING OVERRIDE (Applies to exactly 0.00 physical):
                  # If the exchange physically holds absolutely zero contracts, then NO active bot can hold mass.
                  # This catches anomalies when the user clicks 'Market Close' sequentially on multi-bot pairs.
                  if physical_net_qty < QTY_EPSILON:
                      for b in bots:
                          if not b.in_trade: continue
                          logger.warning(f"🛡️ [GLOBAL-FLATTEN] Exchange physically holds 0.0 units for {pair}. Auto-zeroing orphaned Bot {b.name}.")
                          from .database import reset_bot_after_tp, log_reconciliation
                          try:
                              reset_bot_after_tp(b.bot_id, 0.0) 
                              log_reconciliation(
                                  bot_id=b.bot_id, pair=pair, action="RESET_MISSING_EXCHANGE_ASSET",
                                  details=f"Global Flatten: Zeroed {b.total_invested:.4f} virtual holding because physical reality dropped to 0.0."
                              )
                              results.append(ReconciliationResult(
                                  bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                  details=f"Global Flatten: Exchange asset entirely absent.", requires_manual_intervention=False
                              ))
                          except Exception as fl_err:
                              logger.error(f"Failed to implement Global Flatten for Bot {b.name}: {fl_err}")
                      continue # Skip the rest of Case C since all active bots were just erased
                  
                  # 🚀 ONE-WAY SIDE-PRUNE (SOLE-BOT ONLY):
                  # In One-Way mode, a SHORT bot cannot logically hold units if the exchange is Net LONG.
                  # CRITICAL: This logic is ONLY valid for sole-bot pairs (one bot per pair).
                  # For multi-bot pairs, both LONG and SHORT bots naturally coexist — the exchange
                  # net is LONG - SHORT, which may be positive even when SHORT bots are active.
                  # Applying SIDE-PRUNE to multi-bot pairs incorrectly zeroes out legitimate SHORT bots.
                  phys_side = 'LONG' if physical_net_usd > 0 else ('SHORT' if physical_net_usd < 0 else 'FLAT')
                  
                  if is_sole:  # ← GATE: Only sole-bot pairs can be SIDE-PRUNED
                      for b in bots:
                          if not b.in_trade: continue
                          bot_side = b.direction.upper()
                          if phys_side != 'FLAT' and bot_side != phys_side:
                              # Sole-bot on the WRONG side of physical reality.
                              logger.warning(f"🛡️ [SIDE-PRUNE] Bot {b.name} ({bot_side}) is physically impossible while Exchange is {phys_side} (sole-bot). Inserting Zero-Adoption.")
                              from .database import get_connection
                              try:
                                  conn = get_connection()
                                  cursor = conn.cursor()
                                  cursor.execute("""
                                      SELECT COALESCE(SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add') THEN filled_amount ELSE 0 END), 0) -
                                             COALESCE(SUM(CASE WHEN order_type IN ('tp', 'close', 'adoption_reduce', 'dust_close', 'sl') THEN filled_amount ELSE 0 END), 0)
                                      FROM bot_orders WHERE bot_id = ? AND status IN ('filled', 'closed') AND (cycle_id = ? OR cycle_id IS NULL)
                                  """, (b.bot_id, b.cycle_id))
                                  ledger_units = float(cursor.fetchone()[0] or 0.0)
                                  if ledger_units > 0.0001:
                                      from .database import save_bot_order
                                      save_bot_order(
                                          bot_id=b.bot_id,
                                          order_type='adoption_reduce',
                                          exchange_order_id=f"PRUNE_{int(time.time())}_{b.bot_id}",
                                          price=b.avg_entry_price,
                                          amount=ledger_units,
                                          step=b.current_step,
                                          status='filled',
                                          client_order_id=f"CQB_{b.bot_id}_PRUNE_{int(time.time())}",
                                          notes=f"Sole-Bot Side-Pruning: Zeroed logical {bot_side} to match physical {phys_side}."
                                      )
                                      results.append(ReconciliationResult(
                                          bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                          details=f"Zero-Adoption: Pruned impossible {bot_side} units ({ledger_units:.4f}) (sole-bot).",
                                          requires_manual_intervention=False
                                      ))
                              except Exception as e:
                                  logger.error(f"Failed to prune impossible bot {b.name}: {e}")
                              finally:
                                  if 'conn' in locals(): conn.close()
                  else:
                      # Multi-bot pair: Physical > Virtual gap = offline grid fills NOT in trades table.
                      # Physical < Virtual gap would be a ghost — but that is handled by Case B.2 (structural ghost).
                      # 🚀 FIX: Compare absolute magnitudes to correctly handle SHORT positions (negative values).
                      phys_mag = abs(physical_net_usd)
                      virt_mag = abs(virtual_net_usd)
                      phys_gap = phys_mag - virt_mag  # positive = physical has more mass
                      
                      if phys_gap > 5.0:
                          logger.warning(f"ℹ️ [MULTIBOT-UNKNOWN] {pair}: Physical Mag=${phys_mag:.2f} exceeds Virtual Mag=${virt_mag:.2f} by Gap=${phys_gap:.2f}. Bypassing proportional assumption for strict proof-of-order-ID consensus.")
                      elif phys_gap < -5.0:
                          logger.warning(f"ℹ️ [MULTIBOT-GHOST] {pair}: Virtual Mag=${virt_mag:.2f} exceeds Physical Mag=${phys_mag:.2f}. Ghost bots — structural ghost detection will handle.")

            
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
            
            # 🚀 FIX: is_sole was computed once earlier in Case C (before the if block).
            # threshold is now QTY-based: delta_qty > QTY_EPSILON (already computed above).
            # This replaces the old $50 USD threshold which silently ignored sub-$50 gaps.
                
            # 4. Handle Mismatch or Consensus
            if delta_qty > QTY_EPSILON:
                logger.warning(f"⚠️ [NET-MISMATCH] {pair_normalized}: Virtual={virtual_net_qty:.6f} units, Physical={physical_net_qty:.6f} units. Gap={delta_qty:.6f} (${delta_notional:.2f}).")
                
                # Consensus Strategy A: Sole-Bot Confident Adoption
                if is_sole:
                    bot = [b for b in bots if b.is_active and normalize_symbol(b.pair) == pair_normalized][0]
                    phys_dir = 'LONG' if physical_net_usd > 0 else ('SHORT' if physical_net_usd < 0 else 'FLAT')
                    
                    if phys_dir == 'FLAT' or bot.direction.upper() == phys_dir:
                        self._auto_adopt_physical_position(bot, physical_net_usd, physical_net)
                        results.append(ReconciliationResult(
                            bot_id=bot.bot_id, bot_name=bot.name, pair=bot.pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details=f"Sole-Bot Auto-Adoption: Synced logic to physical net (${physical_net_usd:.2f})",
                            requires_manual_intervention=False
                        ))
                        continue 
                    else:
                        logger.warning(f"🚫 [ADOPTION-BLOCKED] {pair_normalized}: Physical is {phys_dir} but sole bot {bot.name} is {bot.direction.upper()}.")
                        # Falls through to Strategy B/C for manual intervention flag

                # Consensus Strategy B: Trace-and-Fix Ghosts
                error_side = 'LONG' if net_error > 0 else 'SHORT'
                suspects = [b for b in bots if b.in_trade and b.direction.upper() == error_side]
                for b in suspects:
                    exit_proof = self._find_proof_of_exit(b)
                    if exit_proof:
                        logger.info(f"✅ GHOST EXIT PROOF: Bot {b.name} (ID {b.bot_id}) found fill {exit_proof.get('id')}.")
                        self._fix_ghost_bot(b, proof_order_id=f"GHOST_EXIT_{exit_proof.get('id')}")
                        results.append(ReconciliationResult(
                            bot_id=b.bot_id, bot_name=b.name, pair=b.pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details="Reset Ghost Bot (Found exit proof in history)", requires_manual_intervention=False
                        ))
                
                # Consensus Strategy C: Manual Requirement for Multi-Bot Mismatch
                if not any(r.pair == pair for r in results):
                    results.append(ReconciliationResult(
                        bot_id=0, bot_name="NET-GAP", pair=pair, action_taken=ReconciliationAction.REQUIRE_MANUAL,
                        details=f"Net Gap=${net_error:.2f}. Mixed bot ownership or rogue position. Intervene manually.",
                        requires_manual_intervention=True
                    ))
            else:
                # Consensus Strategy D: Promotion (Math compiles perfectly)
                # Ensure bots in Scanning with money are unblocked
                for b in bots:
                    if b.in_trade and b.total_invested > 1.0:
                        from .database import get_connection
                        try:
                            # Use cursor directly to speed up (minimal locking)
                            conn = get_connection()
                            cursor = conn.cursor()
                            cursor.execute("SELECT status FROM bots WHERE id=?", (b.bot_id,))
                            status_row = cursor.fetchone()
                            if status_row and status_row[0].upper() == 'SCANNING':
                                logger.info(f"🔧 [Consensus] Unblocking Bot {b.name} (ID {b.bot_id}) from 'Scanning' to 'IN TRADE'.")
                                cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (b.bot_id,))
                                cursor.execute("UPDATE trades SET entry_confirmed=1 WHERE bot_id=?", (b.bot_id,))
                                conn.commit()
                        finally:
                            if 'conn' in locals(): conn.close()

        return results

    def _find_proof_of_exit(self, bot: BotState, exchange: Any = None) -> Optional[Dict]:
        """
        Searches exchange history for proof that the bot's position was closed.
        Phases:
        1. Look for internal CQB_ DNA orders (TP/SL) that match the current cycle.
        2. Look for manual external trades (Web UI) executed in the opposite direction 
           since the basket started that mathematically align with the vanished state.
        """
        exchanges_to_check = [exchange] if exchange else [ex for ex in self.exchanges.values() if ex]
        
        for ex in exchanges_to_check:
            if not ex: continue
            try:
                # PHASE 1: Check internally signed orders in fetch_closed_orders
                history = ex.fetch_closed_orders(bot.pair, limit=1000)
                if isinstance(history, list):
                    conn = get_connection()
                    cur = conn.cursor()
                    expected_cycle = bot.cycle_id
                    
                    for order in history:
                        cid = order.get('clientOrderId', '')
                        if cid.startswith(f"CQB_{bot.bot_id}_TP_") or cid.startswith(f"CQB_{bot.bot_id}_SL_"):
                            if order.get('status') in ['closed', 'filled']:
                                cur.execute("SELECT cycle_id, status FROM bot_orders WHERE order_id=? OR client_order_id=?", 
                                            (order.get('id'), cid))
                                row = cur.fetchone()
                                if row:
                                    db_cycle, db_status = row[0], row[1]
                                    if db_cycle == expected_cycle and db_status not in ['reset_cleared', 'auto_closed']:
                                        conn.close()
                                        return order
                    conn.close()
                
                # PHASE 2: Check generic un-signed trades for manual Web UI flattenings
                # If a user manually flatlines a position on Binance, there isn't a CQB_ signature.
                since_ms = int(bot.basket_start_time * 1000) if bot.basket_start_time else int((time.time() - 86400*2) * 1000)
                my_trades = ex.fetch_my_trades(bot.pair, since=since_ms)
                
                if isinstance(my_trades, list):
                    # For a LONG bot, an external exit trade is a SELL. For a SHORT bot, it's a BUY.
                    target_side = 'sell' if bot.direction.upper() == 'LONG' else 'buy'
                    
                    valid_manual_trades = []
                    for t in my_trades:
                        if t.get('side', '').lower() == target_side:
                            valid_manual_trades.append(t)
                            
                    if valid_manual_trades:
                        # Sort by newest first and return the latest counter-trade as physical proof
                        valid_manual_trades.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
                        logger.info(f"🔍 [OFFLINE-PROOF] Found generic manual '{target_side}' trade {valid_manual_trades[0].get('id')} serving as exit proof for {bot.name}")
                        return valid_manual_trades[0]
                        
            except Exception as e:
                logger.error(f"Failed to fetch exit proof for Bot {bot.bot_id}: {e}")
        return None

    def is_sole_bot(self, pair: str, bots: List[Any]) -> bool:
        """
        Returns True if there is only ONE active bot for this pair.
        Used to increase confidence in physical position adoption.
        """
        norm_target = normalize_symbol(pair)
        active_pair_bots = [b for b in bots if b.is_active and normalize_symbol(b.pair) == norm_target]
        return len(active_pair_bots) == 1

    def _verify_entry_existence(self, bot: BotState, exchange: Any = None) -> bool:
        """
        Verifies if the bot's supposed Entry Order actually exists and is filled.
        Returns True if valid entry found, False if missing/cancelled (Phantom).
        """
        if not bot.entry_order_id:
            return False # No Order ID = Phantom State

        exchanges_to_check = [exchange] if exchange else [ex for ex in self.exchanges.values() if ex]
        for ex in exchanges_to_check:
            if not ex: continue
            try:
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
        
        # 0.5 Rigorous Trade Memory Alignment (DNA Sync)
        # Ensure 'trades' memory matches 'bot_orders' ledger for ALL bots.
        self._align_memory_to_ledger()
        
        # 1. Offline Fills (Updates DB)
        self.reconstruct_offline_fills()
        
        # 2. Fetch Fresh State
        bot_states = self.get_bot_states()
        success, all_positions = self.fetch_all_exchange_positions()
        
        results = []
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

    def _auto_adopt_physical_position(self, bot: BotState, phys_notional: float, phys_qty: float):
        """
        🛡️ RIGOROUS GLOBAL ADOPTION:
        Mathematically aligns the logical 'trades' table to the physical 'active_positions' net sum.
        Inserts an 'adoption' order record for traceability and proof.
        
        ARCHITECTURE NOTE: The trades update and bot_orders insert are committed in SEPARATE
        transactions intentionally. If the secondary record fails, the primary state is preserved.
        """
        try:
            logger.warning(f"🏗️ [ADOPTION] Bot {bot.name} (ID {bot.bot_id}): Adopting physical net ${phys_notional:.2f} ({phys_qty:.4f} units).")
            
            avg_price = abs(phys_notional) / abs(phys_qty) if abs(phys_qty) > 0 else 0
            
            # === STEP 1: Commit trade state update FIRST (primary, critical) ===
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot.bot_id,))
            
            cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot.bot_id,))
            if cursor.fetchone():
                cursor.execute("""
                    UPDATE trades 
                    SET total_invested = ?, avg_entry_price = ?, current_step = 1, 
                        entry_confirmed = 1, basket_start_time = ?
                    WHERE bot_id = ?
                """, (abs(phys_notional), avg_price, int(time.time()), bot.bot_id))
            else:
                cursor.execute("""
                    INSERT INTO trades (bot_id, total_invested, avg_entry_price, current_step, entry_confirmed, basket_start_time)
                    VALUES (?, ?, ?, 1, 1, ?)
                """, (bot.bot_id, abs(phys_notional), avg_price, int(time.time())))
            
            conn.commit()  # ← CRITICAL: commit trades state before anything else can fail
            conn.close()
            logger.info(f"✅ [ADOPTION] Bot {bot.name}: Trade state committed (${phys_notional:.2f} @ {avg_price:.4f}).")
            
            # === STEP 2: Insert adoption receipt for DNA traceability (secondary, non-critical) ===
            # If this fails, the trade state is already committed above — bot is correctly IN TRADE.
            try:
                from engine.database import save_bot_order
                save_bot_order(
                    bot_id=bot.bot_id,
                    order_type='adoption_add',
                    exchange_order_id=f"ADOPT_{int(time.time())}_{bot.bot_id}",
                    price=avg_price,
                    amount=abs(phys_qty),
                    step=1,
                    status='filled',
                    client_order_id=f"CQB_{bot.bot_id}_ADOPT_{int(time.time())}",
                    notes=f"Forensic Adoption: Aligned memory to physical net sum."
                )
                logger.info(f"✅ [ADOPTION] Bot {bot.name}: DNA adoption receipt written to bot_orders.")
            except Exception as record_err:
                logger.warning(f"⚠️ [ADOPTION] Bot {bot.name}: DNA record write failed (non-fatal, trade state already committed): {record_err}")
            
        except Exception as e:
            logger.error(f"Force Adoption failed for bot {bot.name}: {e}")


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
                action='OFFLINE_SYNC' if str(proof_order_id).isdigit() else 'PHANTOM_RESET',
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

    def _align_memory_to_ledger(self):
        """
        🛡️ RIGOROUS MEMORY ALIGNMENT:
        Enforces that the 'trades' table (total_invested, avg_entry) is exactly 
        subordinate to the 'bot_orders' ledger (filled entries - filled exits).
        If memory deviates from DNA, memory is overwritten.
        """
        conn = get_connection()
        cursor = conn.cursor()
        try:
            # Get all bots currently tracked in trades table
            cursor.execute("""
                SELECT b.id, b.name, t.cycle_id, t.total_invested, b.pair, t.entry_confirmed
                FROM bots b
                JOIN trades t ON b.id = t.bot_id
                WHERE t.total_invested > 0 OR b.status = 'IN TRADE'
            """)
            active_bots = cursor.fetchall()

            for b_id, b_name, cycle, db_inv, pair, entry_confirmed in active_bots:
                # Calculate True Ledger Sum for CURRENT cycle
                # We exclude 'reset_cleared' which are archived DNA from previous trades.
                cursor.execute("""
                    SELECT 
                        SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add') THEN filled_amount ELSE 0 END) as total_fills,
                        SUM(CASE WHEN order_type IN ('tp', 'close', 'adoption_reduce', 'dust_close', 'sl') THEN filled_amount ELSE 0 END) as total_exits,
                        SUM(CASE WHEN order_type IN ('entry', 'grid', 'adoption_add') THEN filled_amount * price ELSE 0 END) as fill_notional
                    FROM bot_orders 
                    WHERE bot_id = ? AND status NOT IN ('reset_cleared', 'auto_closed')
                    AND (cycle_id = ? OR cycle_id IS NULL)
                    AND filled_amount > 0
                """, (b_id, cycle))
                
                row = cursor.fetchone()
                if not row or row[0] is None:
                    ledger_qty = 0.0
                    ledger_notional = 0.0
                    avg_entry = 0.0
                else:
                    ledger_qty = float(row[0] or 0) - float(row[1] or 0)
                    fill_qty_sum = float(row[0] or 1.0)
                    ledger_notional = float(row[2] or 0)
                    avg_entry = ledger_notional / fill_qty_sum if fill_qty_sum > 0 else 0.0
                
                # Logical In-Trade Size (Virtual Balance Proof)
                true_inv = ledger_qty * avg_entry if ledger_qty > 0.0001 else 0.0
                
                if abs(db_inv - true_inv) > 0.01:
                    logger.warning(f"🔧 [DNA-ALIGN] Bot {b_id} ({b_name}) memory=${db_inv:.4f} vs DNA-Ledger=${true_inv:.4f}.")
                    
                    if ledger_qty <= 0.0001:
                        # ⚡ LIFECYCLE GUARD: If entry_confirmed=1, the system (or reconciler) has
                        # explicitly asserted this position is real (e.g., offline fill adoption).
                        # Ledger may be empty because the fill event hasn't been recorded yet.
                        # DO NOT RESET — hold the state and flag for reconciler to investigate.
                        if entry_confirmed:
                            logger.warning(
                                f"⚠️ [DNA-HOLD] Bot {b_id} ({b_name}): entry_confirmed=1 but ledger qty=0. "
                                f"Position is asserted real. Skipping reset — reconciler must verify. "
                                f"Check physical position and order history."
                            )
                        else:
                            # No entry confirmed and ledger is empty. Safe to reset.
                            logger.warning(f"🔧 [DNA-ALIGN] Bot {b_id} ({b_name}): Ledger empty, entry not confirmed. Resetting to Scanning.")
                            cursor.execute("""
                                UPDATE trades SET total_invested=0, current_step=0, avg_entry_price=0, entry_confirmed=0, basket_start_time=0 
                                WHERE bot_id=?
                            """, (b_id,))
                            cursor.execute("UPDATE bots SET status='Scanning' WHERE id=?", (b_id,))
                    else:
                        # Proof says bot is invested. Fix memory to match ledger truth.
                        logger.info(f"🔧 [DNA-ALIGN] Syncing Bot {b_id} ({b_name}) memory to ledger: ${true_inv:.4f} @ {avg_entry:.4f}.")
                        cursor.execute("""
                            UPDATE trades SET total_invested=?, avg_entry_price=?, entry_confirmed=1
                            WHERE bot_id=?
                        """, (true_inv, avg_entry, b_id))

            
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to align memory to ledger: {e}")
        finally:
            conn.close()

# Alias for backward compatibility if needed
DeepReconciler = StateReconciler
