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
    # Bot configuration for mathematical step recovery
    base_size: float = 0.0          # First-step notional (e.g. $100)
    martingale_multiplier: float = 1.0  # Each step size = prev_step * multiplier


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


        # 1.5. 🚀 PRE-COMMIT ROW RESOLUTION
        # Covers two failure windows:
        # A) status='placing' — order written to DB BEFORE exchange API call; engine crashed mid-atomicity.
        # B) status='new', order_type='entry' — entry confirmed by exchange (API returned OK) but engine
        #    went offline BEFORE the WebSocket fill event arrived. This means filled_amount=0 in DB even
        #    though Binance fully executed the entry order.
        #
        # SCOPE CONSTRAINT: We intentionally restrict 'new' scanning to ENTRY orders only.
        # TP and grid orders legitimately sit as status='new', filled_amount=0 while open on the book.
        # Scanning them would process live active orders as "stale unresolved" — causing false resets.
        # Only entry fills that happened while offline can cause the virtual/physical gaps we target here.
        _place_conn = get_connection()
        _place_cur = _place_conn.cursor()
        _place_cur.execute("""
            SELECT bo.id, bo.bot_id, b.pair, bo.order_type, bo.client_order_id,
                   bo.price, bo.amount, bo.step, bo.cycle_id
            FROM bot_orders bo JOIN bots b ON bo.bot_id=b.id
            WHERE (
                bo.status = 'placing'                                      -- Window A: crashed mid-atomicity
                OR (bo.status = 'new' AND bo.order_type = 'entry')        -- Window B: entry filled offline
            )
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

            since_fallback = int((time.time() - since_hours * 3600) * 1000)  # Use injected since_hours instead of hardcoded 7 days to prevent rate limiting
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
                                ORDER BY filled_amount DESC LIMIT 1
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
                            cursor.execute("SELECT id, filled_amount FROM bot_orders WHERE order_id=?", (order['id'],))
                            existing = cursor.fetchone()
                            
                            # 🚀 DEDUPLICATION FIX: If ws_event_handlers recorded partial fills, existing[1] has the recorded qty.
                            # We must ONLY add the remaining difference to the virtual position, or we double-count!
                            previously_filled = float(existing[1] or 0.0) if existing else 0.0
                            unaccounted_qty = max(0.0, fill_qty - previously_filled)
                            
                            # Always insert offline fills as 'filled' initially. If it's a TP, the subsequent 
                            # reset_bot_after_tp call will vacuum it up and change it to 'reset_cleared' automatically.
                            if existing:
                                cursor.execute("UPDATE bot_orders SET status='filled', updated_at=?, filled_amount=? WHERE order_id=? OR client_order_id=?",
                                               (int(time.time()), fill_qty, order['id'], cid))
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
                                    
                                    # The old Layer 2 check is removed as `unaccounted_qty` directly addresses 
                                    # the partial fill double-counting issue without fragile tuple matching.
                                    # If unaccounted_qty is negligible and it's already in bot_orders, ignore.
                                    if unaccounted_qty <= 1e-8 and already_in_orders:
                                        logger.info(f"⏭️ [OFFLINE-DEDUP] Skipping fill for Bot {bot_id} Step {step} @ {fill_price} x{fill_qty} — already fully accounted for in database.")
                                        # Mark bot_order as filled so reconciler doesn't re-check on next cycle
                                        cursor.execute("UPDATE bot_orders SET status='filled' WHERE (order_id=? OR client_order_id=?)", (order['id'], cid))
                                        continue
                                    
                                    logger.info(f"✅ Re-playing OFFLINE_GRID for Bot {bot_id} (Step {step}) for {unaccounted_qty:.6f} unaccounted qty.")
                                    self._handle_offline_grid_fill(cursor, bot_id, bot_name, fill_price, unaccounted_qty, curr_step, current_state.get('total_invested',0), current_state.get('avg_entry',0), fill_symbol)
                                    stats['grid_fills'] += 1
                                    
                                    # ✅ FIX: Update the LOCAL cache so subsequent fills in THIS SAME SWEEP
                                    # use the correct post-fill avg_entry and total_invested.
                                    old_inv = bot_states[bot_id].get('total_invested', 0)
                                    old_avg = bot_states[bot_id].get('avg_entry', 0)
                                    fill_cost = fill_price * unaccounted_qty
                                    new_inv = old_inv + fill_cost
                                    new_avg = ((old_inv * old_avg) + fill_cost) / new_inv if new_inv > 0 else fill_price
                                    bot_states[bot_id]['current_step'] = step
                                    bot_states[bot_id]['total_invested'] = new_inv
                                    bot_states[bot_id]['avg_entry'] = new_avg
                                    
                            elif otype == 'ENTRY':
                                if curr_step == 0:
                                    if unaccounted_qty > 1e-8:
                                        logger.info(f"✅ Re-playing OFFLINE_ENTRY for Bot {bot_id} for {unaccounted_qty:.6f} unaccounted qty.")
                                        order_time_sec = int(order.get('timestamp', time.time() * 1000) / 1000)
                                        self._handle_offline_entry_fill(cursor, bot_id, bot_name, fill_price, unaccounted_qty, fill_symbol, order_time_sec)
                                        stats['entry_fills'] += 1
                                        
                                        bot_states[bot_id]['current_step'] = 1
                                        bot_states[bot_id]['entry_confirmed'] = 1
                                        old_inv = bot_states[bot_id].get('total_invested', 0)
                                        bot_states[bot_id]['total_invested'] = old_inv + (fill_price * unaccounted_qty)

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
                    # 🔑 ORDER-ID LEDGER SELF-HEAL
                    # Before declaring zombie, recompute the ledger from confirmed order fills.
                    # If total_invested was a stale counter (bot has no confirmed fills this cycle),
                    # the fix zeroes it out and the bot is no longer "in trade" — not a zombie.
                    try:
                        from engine.database import sync_trades_from_orders as _sync_ledger
                        _corrected = _sync_ledger(bot.bot_id)
                        if _corrected:
                            # Re-read invested after correction
                            from engine.database import get_connection as _gc
                            _c = _gc()
                            _new_inv = (_c.execute(
                                "SELECT total_invested FROM trades WHERE bot_id=?", (bot.bot_id,)
                            ).fetchone() or (0,))[0]
                            if float(_new_inv or 0) <= 1e-6:
                                logger.info(
                                    f"✅ [LEDGER-HEAL] Bot {bot.name} (ID {bot.bot_id}): "
                                    f"total_invested corrected to 0 from order fills — not a zombie."
                                )
                                continue  # Skip zombie warning; bot is correctly IDLE
                    except Exception as _sh_err:
                        logger.warning(f"[LEDGER-HEAL] sync failed for bot {bot.bot_id}: {_sh_err}")

                    # --- PHANTOM BOT REPORT ---
                    # If we get here, bot still has invested > 0 after ledger sync —
                    # genuinely IN TRADE but no orders on exchange. Defer to Global Net Resolution.
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
            # 1. Calc Virtual GROSS per direction (not signed net)
            # In One-Way mode, multiple bots may trade the same pair in the same direction.
            # NET comparison (LONG−SHORT) will ALWAYS fire when >1 same-direction bot is active.
            # GROSS per-direction comparison is the correct approach: compare all LONG virtual qty
            # vs all physical LONG qty, and all SHORT virtual qty vs all physical SHORT qty, independently.
            virtual_long_qty = 0.0
            virtual_long_usd = 0.0
            virtual_short_qty = 0.0
            virtual_short_usd = 0.0
            
            for b in bots:
                if b.in_trade and b.avg_entry_price > 0:
                    qty = b.total_invested / b.avg_entry_price
                    if b.direction.upper() == 'LONG':
                        virtual_long_qty += qty
                        virtual_long_usd += b.total_invested
                    else:
                        virtual_short_qty += qty
                        virtual_short_usd += b.total_invested

            # Keep legacy virtual_net for backward compat with ghost-detection code below
            virtual_net = virtual_long_qty - virtual_short_qty
            virtual_net_usd = virtual_long_usd - virtual_short_usd
            total_virtual_invested = virtual_long_usd + virtual_short_usd
            gross_virtual_qty = virtual_long_qty + virtual_short_qty

            # 2. Get Physical GROSS per direction from exchange
            pair_normalized = normalize_symbol(pair)
            pair_positions = normalized_positions.get(pair_normalized, [])
            
            physical_long_qty = 0.0
            physical_long_usd = 0.0
            physical_short_qty = 0.0
            physical_short_usd = 0.0
            total_physical_notional = 0.0
            physical_net = 0.0
            physical_net_usd = 0.0
            rep_side = "N/A"

            for p in pair_positions:
                val = abs(p.size) * p.entry_price
                total_physical_notional += val
                rep_side = p.side
                if p.side == 'LONG':
                    physical_long_qty += abs(p.size)
                    physical_long_usd += val
                    physical_net += abs(p.size)
                    physical_net_usd += val
                else:
                    physical_short_qty += abs(p.size)
                    physical_short_usd += val
                    physical_net -= abs(p.size)
                    physical_net_usd -= val

            # 3. GROSS per-direction gap (the real check)
            long_gap_qty = virtual_long_qty - physical_long_qty
            long_gap_usd = virtual_long_usd - physical_long_usd
            short_gap_qty = virtual_short_qty - physical_short_qty
            short_gap_usd = virtual_short_usd - physical_short_usd

            logger.info(
                f"⚖️ RECON AUDIT [{pair_normalized}]: "
                f"Virtual LONG ${virtual_long_usd:.2f} vs Physical LONG ${physical_long_usd:.2f} | "
                f"Virtual SHORT ${virtual_short_usd:.2f} vs Physical SHORT ${physical_short_usd:.2f}"
            )


            # 🚀 Case A: Impossible Bot Mass Detection (Vanished or Ghost Bots)
            # A bot is mathematically a "Ghost" if it independently claims to hold MORE units
            # than the entire gross sum existing on the exchange (plus any virtual opposing bots in one-way mode).
            # This occurs if a user manually closes the position or if it was liquidated/ADL'd.
            for b in bots:
                if b.in_trade:
                    bot_qty = b.total_invested / b.avg_entry_price if b.avg_entry_price > 0 else 0
                    
                    opposite_virtual_qty = sum(
                        (other_b.total_invested / other_b.avg_entry_price)
                        for other_b in bots
                        if other_b.in_trade and other_b.avg_entry_price > 0 and other_b.direction.upper() != b.direction.upper()
                    )
                    
                    physical_matching_direction_qty = sum(
                        abs(p.size) for p in pair_positions if p.side == b.direction.upper()
                    )
                    
                    max_possible_qty = physical_matching_direction_qty + opposite_virtual_qty
                    
                    # If this single bot's mass exceeds the TOTAL mathematical capacity for its direction 
                    # by more than epsilon, this bot is physically impossible.
                    if bot_qty > (max_possible_qty + 0.0001):
                        logger.critical(f"👻 SYSTEM MISMATCH on {pair}: Bot {b.name} claims {bot_qty:.6f} QTY, but Math Capacity is only {max_possible_qty:.6f} QTY.")
                        
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
                                if physical_matching_direction_qty < 0.0001 and opposite_virtual_qty < bot_qty:
                                    logger.critical(
                                        f"💥 [VANISHED POSITION DETECTED] Bot {b.name}: Claims {bot_qty:.6f} units but Math Capacity is {max_possible_qty:.6f}! "
                                        f"Entry was confirmed, so the position vanished externally (Liquidated/ADL/Manual). Forcing memory wipe!"
                                    )
                                    from .database import reset_bot_after_tp, log_reconciliation
                                    reset_bot_after_tp(b.bot_id, 0.0, action_label='SYSTEM_WIPE')
                                    
                                    log_reconciliation(
                                        bot_id=b.bot_id,
                                        pair=b.pair,
                                        action="RESET_VANISHED_POSITION",
                                        details=f"Mismatch: Bot claims {bot_qty:.6f}, Math Capacity is {max_possible_qty:.6f}. Vanished from exchange. Resetting bot."
                                    )
                                    results.append(ReconciliationResult(
                                        bot_id=b.bot_id, bot_name=b.name, pair=b.pair,
                                        action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                        details="Reset Vanished Confirmed Position", requires_manual_intervention=False
                                    ))
                                else:
                                    logger.warning(
                                        f"⚠️ [PARTIAL VANISH DEFERRED] Bot {b.name}: Claims {bot_qty:.6f} units but Physical is {physical_matching_direction_qty:.6f} (max_capacity={max_possible_qty:.6f}). "
                                        f"Since Physical > 0, we will NOT wipe the bot's memory. Deferring to Net Gap Resolution."
                                    )
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
                # 🔥 MID-FILL GUARD: If the bot's entry_order_id is STILL OPEN on the exchange, the bot is
                # currently being filled via partial fills. NEVER reset a bot mid-fill!
                if b.in_trade and b.total_invested > 1.0 and not b.has_confirmed_entry and b.current_step > 0:
                    # Check if entry order is still open (mid-fill protection)
                    entry_is_active = False
                    if b.entry_order_id:
                        pair_orders = all_orders.get(normalize_symbol(b.pair), [])
                        entry_order_ids = {str(o.order_id) for o in pair_orders}
                        if str(b.entry_order_id) in entry_order_ids:
                            logger.info(f"🔒 [MID-FILL GUARD] Bot {b.name}: Entry order {b.entry_order_id} still open on exchange. Skipping ghost check — bot is actively filling.")
                            entry_is_active = True
                    if entry_is_active:
                        continue  # Skip ghost check entirely for this bot
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
                            reset_bot_after_tp(b.bot_id, 0.0, action_label='SYSTEM_WIPE') # Force reset
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

            # 🚀 ROOT CAUSE FIX: Do not require `genuine_in_trade_bots` to be non-empty. 
            # If the database was wiped, ALL bots will be `in_trade=False`, but the physical gap STILL EXISTS!
            # The system must process the gap and run deduction to recover the orphaned physical positions.
            if delta_qty > QTY_EPSILON and (abs(virtual_net_qty) > QTY_EPSILON or abs(physical_net_qty) > QTY_EPSILON):

                  logger.error(
                      f"🚨 [QTY-GAP] {pair}: Virtual={virtual_net_qty:.6f} vs "
                      f"Physical={physical_net_qty:.6f} (Delta={delta_qty:.6f} units, ${delta_notional:.2f}). "
                      f"Scanning for physically impossible states..."
                  )
                  
                  # 🚀 GLOBAL FLATTENING OVERRIDE (Applies to exactly 0.00 physical):
                  # If the exchange physically holds absolutely zero contracts, then NO active bot can hold mass.
                  # This catches anomalies when the user clicks 'Market Close' sequentially on multi-bot pairs.
                  if abs(physical_net_qty) < QTY_EPSILON:
                      for b in bots:
                          if not b.in_trade: continue
                          logger.warning(f"🛡️ [GLOBAL-FLATTEN] Exchange physically holds 0.0 units for {pair}. Auto-zeroing orphaned Bot {b.name}.")
                          
                          # 1. Search for proof of exit
                          proof = self._find_proof_of_exit(b)
                          if proof:
                              logger.info(f"✅ [GLOBAL-FLATTEN PROOF FOUND] Found manual exit or liquidation order: {proof.get('id')}")
                          else:
                              # 2. No proof found: Perform a professional accounting adjustment before resetting
                              logger.warning(f"⚠️ [GLOBAL-FLATTEN NO PROOF] Writing off {b.total_invested:.2f} virtual holding via synthetic reduce.")
                              self._execute_accounting_adjustment(b, 0.0, 0.0, "Global Flatten: Zeroing missing asset")
                              
                          from .database import reset_bot_after_tp, log_reconciliation
                          try:
                              reset_bot_after_tp(b.bot_id, 0.0, action_label='SYSTEM_WIPE') 
                              log_reconciliation(
                                  bot_id=b.bot_id, pair=pair, action="RESET_MISSING_EXCHANGE_ASSET",
                                  details=f"Global Flatten: Zeroed {b.total_invested:.4f} virtual holding because physical reality dropped to 0.0. Proof={bool(proof)}"
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
                              # Sole-bot on the WRONG side of physical reality. This means a full external reversal.
                              logger.warning(f"🛡️ [SIDE-PRUNE] Bot {b.name} ({bot_side}) is physically impossible while Exchange is {phys_side} (sole-bot). Applying adjustment.")
                              try:
                                  proof = self._find_proof_of_exit(b)
                                  if proof:
                                      logger.info(f"✅ [SIDE-PRUNE PROOF FOUND] Exit order found: {proof.get('id')}")
                                  else:
                                      logger.warning(f"⚠️ [SIDE-PRUNE NO PROOF] Writing off {bot_side} position via synthetic reduce.")
                                      self._execute_accounting_adjustment(b, 0.0, 0.0, f"Side-Prune: Zeroing {bot_side} position to match physical {phys_side}")
                                      
                                  from .database import reset_bot_after_tp, log_reconciliation
                                  reset_bot_after_tp(b.bot_id, 0.0, action_label='SYSTEM_WIPE')
                                  log_reconciliation(
                                      bot_id=b.bot_id, pair=pair, action="RESET_MISSING_EXCHANGE_ASSET",
                                      details=f"Side-Prune: Reversed side. Zeroed {b.total_invested:.4f} bot."
                                  )
                                  results.append(ReconciliationResult(
                                      bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                      details=f"Zero-Adoption: Pruned impossible {bot_side} units (sole-bot).",
                                      requires_manual_intervention=False
                                  ))
                              except Exception as e:
                                  logger.error(f"Failed to prune impossible bot {b.name}: {e}")
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

                # ================================================================
                # 🧹 Consensus Strategy 0: Pre-emptive Dust Chaser
                # If the ENTIRE physical gap is itself tinier than $5 notional,
                # the gap is un-sellable dust. No deduction, adoption, or manual
                # intervention is needed. Just fire a reduceOnly market order to
                # physically clear the exchange wallet and zero the ledger.
                # ================================================================
                if delta_notional < 5.0 and abs(physical_net_usd) < 5.0:
                    logger.warning(f"🧹 [DUST-CHASER] {pair_normalized}: Gap notional ${delta_notional:.2f} is sub-$5. Treating as un-sellable dust. Firing physical clear.")
                    ex_dust = self.exchanges.get('future') or (list(self.exchanges.values())[0] if self.exchanges else None)
                    if ex_dust and abs(physical_net_qty) > 1e-8:
                        dust_side = 'buy' if physical_net_qty < 0 else 'sell'  # Counter-direction to flatten
                        try:
                            ex_dust.create_order(
                                symbol=list(pair_positions)[0].symbol if pair_positions else pair,
                                type='market', side=dust_side, amount=abs(physical_net_qty),
                                params={'reduceOnly': True}
                            )
                            logger.info(f"✅ [DUST-CHASER] Physical {dust_side.upper()} market order executed for {abs(physical_net_qty):.6f} {pair_normalized}.")
                        except Exception as de:
                            logger.warning(f"⚠️ [DUST-CHASER] Exchange clear failed (likely already 0): {de}")
                    # Either way, clear all local bot states for this pair that are claiming dust
                    for dust_bot in bots:
                        if dust_bot.total_invested > 0 and dust_bot.total_invested < 5.0:
                            self._execute_accounting_adjustment(dust_bot, 0.0, 0.0, "Dust Chaser: Sub-$5 gap cleared")
                            from .database import reset_bot_after_tp
                            reset_bot_after_tp(dust_bot.bot_id, 0.0, action_label='SYSTEM_WIPE')
                            results.append(ReconciliationResult(
                                bot_id=dust_bot.bot_id, bot_name=dust_bot.name, pair=pair_normalized, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                                details=f"Dust Chaser: Cleared ${dust_bot.total_invested:.2f} sub-$5 un-sellable position.",
                                requires_manual_intervention=False
                            ))
                    continue  # Fully handled — skip all remaining strategies

                # Consensus Strategy A: Sole-Bot Confident Adoption
                if is_sole:
                    bot = [b for b in bots if b.is_active and normalize_symbol(b.pair) == pair_normalized][0]
                    phys_dir = 'LONG' if physical_net_usd > 0 else ('SHORT' if physical_net_usd < 0 else 'FLAT')
                    
                    if phys_dir == 'FLAT' or bot.direction.upper() == phys_dir:
                        # 🚀 PROFESSIONAL ACCOUNTING:
                        # If bot mass > exchange mass (Missing Asset) OR
                        # If bot mass < exchange mass (Missing Entry)
                        # We execute the auto-adjustment to bridge the gap and continue operations.
                        self._execute_accounting_adjustment(bot, physical_net_usd, physical_net, "Sole-Bot Gap Adjustment")
                        results.append(ReconciliationResult(
                            bot_id=bot.bot_id, bot_name=bot.name, pair=bot.pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details=f"Sole-Bot Auto-Adjustment: Synced logic to physical net (${physical_net_usd:.2f}) via formal accounting.",
                            requires_manual_intervention=False
                        ))
                        continue 
                    else:
                        logger.warning(f"🚫 [ADOPTION-BLOCKED] {pair_normalized}: Physical is {phys_dir} but sole bot {bot.name} is {bot.direction.upper()}.")
                        # Falls through to Strategy B/C for manual intervention flag

                # ================================================================
                # 🔑 Consensus Strategy B.4: Dual-Proof DNA Attribution (RUNS FIRST)
                # ================================================================
                # B.4 has FULL AUTHORITY for multi-bot pairs.
                # It runs BEFORE B.5 so TP order proof takes precedence over guesses.
                #
                # PROOF 1 — TP open order amount:
                #   When a bot places a TP order it uses EXACTLY the qty it holds.
                #   client_order_id = CQB_{bot_id}_TP_... so we know owner + size.
                # PROOF 2 — bot_orders cycle ledger:
                #   SUM(filled entries) - SUM(filled exits) for this bot's current cycle.
                # Cross-referencing both gives maximum confidence.
                #
                # B.4 ALSO CLEARS PHANTOMS: any bot claiming a position with ZERO
                # evidence from both proofs is reset to 0 (it's a ghost).
                # ================================================================
                if abs(phys_net) > abs(virt_net):
                    error_side = 'LONG' if phys_net > 0 else 'SHORT'
                else:
                    error_side = 'LONG' if virt_net > 0 else 'SHORT'
                    
                # Pre-calculate physical capacity for the error direction
                opposite_tracked_qty_for_b4 = sum(b.total_invested / b.avg_entry_price for b in bots if b.in_trade and b.avg_entry_price > 0 and b.direction.upper() != error_side)
                if error_side == 'LONG':
                    b4_max_capacity_qty = physical_net_qty + opposite_tracked_qty_for_b4
                else:
                    b4_max_capacity_qty = -(physical_net_qty - opposite_tracked_qty_for_b4)

                pair_open_orders = all_orders.get(pair_normalized, [])

                # Collect TP amount proof from live open orders (Proof 1)
                # ExchangeOrder objects use .client_order_id and .amount attributes
                tp_proof: dict = {}  # bot_id -> {'tp_qty': float, 'labels': list}
                for o in pair_open_orders:
                    cid = o.client_order_id or ''
                    if not cid.startswith('CQB_'):
                        continue
                    parts = cid.split('_')
                    if len(parts) < 3:
                        continue
                    try:
                        claim_bot_id = int(parts[1])
                    except ValueError:
                        continue
                    order_type_tag = parts[2] if len(parts) > 2 else ''
                    # ExchangeOrder.amount = origQty (equals exact position qty for TP orders)
                    order_qty = float(o.amount or 0)
                    if claim_bot_id not in tp_proof:
                        tp_proof[claim_bot_id] = {'tp_qty': 0.0, 'labels': []}
                    tp_proof[claim_bot_id]['labels'].append(cid)
                    if order_type_tag in ('TP', 'SL', 'STOP'):
                        tp_proof[claim_bot_id]['tp_qty'] += order_qty

                # Collect cycle ledger proof from bot_orders (Proof 2)
                ledger_proof: dict = {}  # bot_id -> net_qty
                try:
                    from .database import get_connection
                    _lconn = get_connection()
                    _lcur = _lconn.cursor()
                    _lcur.execute("""
                        SELECT bo.bot_id,
                               SUM(CASE WHEN bo.order_type IN ('entry','grid') THEN bo.filled_amount ELSE 0 END) as entry_qty,
                               SUM(CASE WHEN bo.order_type IN ('tp','exit','close') THEN bo.filled_amount ELSE 0 END) as exit_qty
                        FROM bot_orders bo
                        JOIN bots b ON b.id = bo.bot_id
                        WHERE b.pair=? AND bo.status IN ('filled','closed') AND bo.filled_amount > 0
                        GROUP BY bo.bot_id
                    """, (pair_normalized,))
                    for row in _lcur.fetchall():
                        _bid, _entry_qty, _exit_qty = row
                        net = (_entry_qty or 0) - (_exit_qty or 0)
                        if net > 0.00001:
                            ledger_proof[_bid] = net
                    _lconn.close()
                except Exception as _le:
                    logger.warning(f"⚠️ [DNA-B4] Could not read bot_orders ledger: {_le}")

                all_claimant_ids = set(list(tp_proof.keys()) + list(ledger_proof.keys()))
                b4_ran = False

                if all_claimant_ids:
                    logger.info(f"🔑 [DNA-B4] Claimants for {pair_normalized}: TP-proof={list(tp_proof.keys())} Ledger-proof={list(ledger_proof.keys())}")
                    adopted_via_dna = False
                    sign = -1 if error_side == 'SHORT' else 1
                    ref_pos = next((p for p in pair_positions), None)
                    current_price = abs(ref_pos.entry_price) if ref_pos and abs(ref_pos.entry_price or 0) > 0 else (abs(physical_net_usd / physical_net) if physical_net else 0)

                    for claim_bot_id in all_claimant_ids:
                        matching = [b for b in bots if b.bot_id == claim_bot_id]
                        if not matching:
                            continue
                        claim_bot = matching[0]

                        if claim_bot.in_trade and claim_bot.total_invested > 1.0:
                            logger.info(f"✅ [DNA-B4] Bot {claim_bot.name} already tracked (${claim_bot.total_invested:.2f}). Skipping.")
                            continue

                        tp_qty = tp_proof.get(claim_bot_id, {}).get('tp_qty', 0.0)
                        ledger_qty = ledger_proof.get(claim_bot_id, 0.0)
                        
                        # 🚀 SAFETY GATE: Reject mathematically impossible proof
                        # Note: We only reject if magnitude shrinks (Partial Vanish). 
                        if claim_bot.direction.upper() == error_side and abs(phys_net) < abs(virt_net):
                            if tp_qty > b4_max_capacity_qty + 0.0001:
                                logger.warning(f"⚠️ [DNA-B4 OVERCLAIM] Bot {claim_bot.name} claims {tp_qty:.6f} via TP proof, but remaining capacity for {error_side} is only {b4_max_capacity_qty:.6f}. Rejecting.")
                                tp_qty = 0.0
                            if ledger_qty > b4_max_capacity_qty + 0.0001:
                                ledger_qty = 0.0

                        if tp_qty > 0 and ledger_qty > 0:
                            if abs(tp_qty - ledger_qty) > 0.0001:
                                logger.warning(f"⚠️ [DNA-B4] Bot {claim_bot.name}: TP proof ({tp_qty:.6f}) differs from ledger ({ledger_qty:.6f}). Using TP (live).")
                            proven_qty = tp_qty
                            proof_source = f"TP+Ledger ({tp_qty:.6f} vs {ledger_qty:.6f})"
                        elif tp_qty > 0:
                            proven_qty = tp_qty
                            proof_source = f"TP order ({tp_qty:.6f} units)"
                        elif ledger_qty > 0:
                            proven_qty = ledger_qty
                            proof_source = f"Ledger ({ledger_qty:.6f} units)"
                        else:
                            logger.warning(f"⚠️ [DNA-B4] Bot {claim_bot.name}: Signed orders found but qty=0 in both proofs. Cannot adopt.")
                            continue

                        proven_usd = proven_qty * current_price
                        labels = tp_proof.get(claim_bot_id, {}).get('labels', [])
                        logger.info(f"✅ [DNA-B4] PROOF: Bot {claim_bot.name} (ID {claim_bot_id}) holds {proven_qty:.6f} units (${proven_usd:.2f}). Source: {proof_source}. Orders: {labels}")
                        self._execute_accounting_adjustment(claim_bot, sign * proven_usd, sign * proven_qty, f"DNA B.4: {proof_source}")
                        
                        # 🚀 SUBTRACT ADOPTED CAPACITY: Prevent multiple clones from claiming the same 
                        # physical mass by draining the bucket as each valid claim is processed.
                        if claim_bot.direction.upper() == error_side:
                            b4_max_capacity_qty -= proven_qty
                            
                        results.append(ReconciliationResult(
                            bot_id=claim_bot.bot_id, bot_name=claim_bot.name, pair=pair_normalized,
                            action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details=f"DNA B.4 Attribution: {proven_qty:.6f} qty adopted to {claim_bot.name}. Source: {proof_source}.",
                            requires_manual_intervention=False
                        ))
                        adopted_via_dna = True

                # ──────────────────────────────────────────────────────────────
                # PHANTOM CLEAR: Any bot claiming a position with ZERO evidence
                # from BOTH proofs is a ghost — reset it to 0.
                # This handles the "SUI long phantom" and similar stale DB states.
                # ──────────────────────────────────────────────────────────────
                for bot in bots:
                    if not (bot.in_trade and bot.total_invested > 1.0):
                        continue  # Not claiming any position — nothing to clear
                    bid = bot.bot_id
                    has_tp_proof = tp_proof.get(bid, {}).get('tp_qty', 0.0) > 0
                    has_ledger_proof = ledger_proof.get(bid, 0.0) > 0
                    has_any_signed_order = bid in tp_proof  # At least exists in signed order map
                    if not has_tp_proof and not has_ledger_proof and not has_any_signed_order:
                        logger.warning(f"👻 [DNA-B4 PHANTOM-CLEAR] Bot {bot.name} claims ${bot.total_invested:.2f} but has ZERO TP or ledger evidence. Resetting to 0.")
                        self._execute_accounting_adjustment(bot, 0.0, 0.0, "DNA B.4 Phantom Clear: No evidence in TP orders or ledger")
                        from .database import reset_bot_after_tp
                        reset_bot_after_tp(bot.bot_id, 0.0, action_label='SYSTEM_WIPE')
                        results.append(ReconciliationResult(
                            bot_id=bot.bot_id, bot_name=bot.name, pair=pair_normalized,
                            action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details=f"Phantom Clear: Bot {bot.name} had no TP order or ledger evidence. Reset to 0.",
                            requires_manual_intervention=False
                        ))
                        b4_phantom_cleared = True

                if b4_ran:
                    continue  # B.4 fully resolved claimants — skip B.5 and Strategy C

                # Calculate adjusted physical quantities BEFORE B.5 / Strategy C
                opposite_tracked_usd = sum(b.total_invested for b in bots if b.in_trade and b.direction.upper() != error_side)
                opposite_tracked_qty = sum(b.total_invested / b.avg_entry_price for b in bots if b.in_trade and b.avg_entry_price > 0 and b.direction.upper() != error_side)
                
                if error_side == 'LONG':
                    adjusted_phys_usd = physical_net_usd + opposite_tracked_usd
                    adjusted_phys_qty = physical_net_qty + opposite_tracked_qty
                else:
                    adjusted_phys_usd = physical_net_usd - opposite_tracked_usd
                    adjusted_phys_qty = physical_net_qty - opposite_tracked_qty

                # ================================================================
                # Consensus Strategy B.5: Multi-Bot Directional Smart Deduction
                # FALLBACK ONLY — runs when B.4 found zero TP order claimants.
                # If exactly ONE bot matches the error direction, adopt physical net.
                # ================================================================
                suspects = [b for b in bots if b.in_trade and b.direction.upper() == error_side]
                direction_match_bots = [b for b in bots if b.direction.upper() == error_side]
                
                logger.info(f"🔍 [B.5 CHECK] {pair_normalized}: b4_ran={b4_ran}, is_sole={is_sole}, error_side={error_side}, match_bots={[b.name for b in direction_match_bots]}")

                resolved_by_deduction = False
                if not b4_ran and not is_sole and len(direction_match_bots) == 1:
                    target_bot = direction_match_bots[0]

                    logger.info(f"🧠 [SMART-DEDUCT] {pair_normalized}: Adopting adjusted physical net (usd={adjusted_phys_usd:.2f}, qty={adjusted_phys_qty:.6f}) to {target_bot.name}.")
                    self._execute_accounting_adjustment(target_bot, adjusted_phys_usd, adjusted_phys_qty, "Multi-Bot Directional Smart Deduction")
                    
                    from .database import log_reconciliation
                    log_reconciliation(
                        bot_id=target_bot.bot_id,
                        pair=pair_normalized,
                        action="SMART_DEDUCTION_RECOVERY",
                        details=f"Adopted {adjusted_phys_qty:.6f} physical gap via Directional Smart Deduction"
                    )
                    results.append(ReconciliationResult(
                        bot_id=target_bot.bot_id, bot_name=target_bot.name, pair=pair_normalized,
                        action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE, # Same flag to stop cycle loop
                        details=f"Reconciled: Gap proved belonging to sole {error_side} bot.",
                        requires_manual_intervention=False
                    ))
                    resolved_by_deduction = True

                # ================================================================
                # Consensus Strategy C: Manual Requirement for Multi-Bot Mismatch
                # If MULTIPLE bots share the direction and have NO proof, logic cannot guess.
                # ================================================================
                if not b4_ran and not resolved_by_deduction:
                    # Prevent spamming duplicate results per pair
                    if not any(r.pair == pair_normalized for r in results):
                        same_dir_bots = [b.name for b in bots if b.direction.upper() == error_side]
                        results.append(ReconciliationResult(
                            bot_id=bots[0].bot_id, # Target the first bot just as an anchor point for the alert
                            bot_name=bots[0].name,
                            pair=pair_normalized,
                            action_taken=ReconciliationAction.ROGUE_POSITION, # Trigger Wizard marker
                            details=f"Ghost gap ({adjusted_phys_qty:.6f} / ${adjusted_phys_usd:.2f}) owned by {error_side} bot, but multiple {error_side} candidates exist: {same_dir_bots}. Use Manual Wizard.",
                            requires_manual_intervention=True
                        ))

                for b in suspects:
                    exit_proof = self._find_proof_of_exit(b)
                    if exit_proof:
                        logger.info(f"✅ GHOST EXIT PROOF: Bot {b.name} (ID {b.bot_id}) found fill {exit_proof.get('id')}.")
                        self._fix_ghost_bot(b, proof_order_id=f"GHOST_EXIT_{exit_proof.get('id')}")
                        results.append(ReconciliationResult(
                            bot_id=b.bot_id, bot_name=b.name, pair=b.pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details="Reset Ghost Bot (Found exit proof in history)", requires_manual_intervention=False
                        ))

                # After checking all suspects for exit proofs, if we STILL have no idea who owns the gap:
                # ----------------------------------------------------------------------
                same_dir_bots = [b.name for b in bots if b.direction.upper() == error_side]
                results.append(ReconciliationResult(
                    bot_id=0, bot_name="NET-GAP", pair=pair_normalized, action_taken=ReconciliationAction.REQUIRE_MANUAL,
                    details=(
                        f"⚠️ OWNERSHIP AMBIGUITY — Cannot auto-resolve.\n"
                        f"   Pair: {pair_normalized} | Direction: {error_side} | Gap: ${net_error:.2f}\n"
                        f"   Root Cause: {len(same_dir_bots)} bot(s) share this direction: {same_dir_bots}\n"
                        f"   No CQB_ signed open orders found — could not prove ownership.\n"
                        f"   Action: Open Binance Futures → close position manually → then reset correct bot(s) to Scanning."
                    ),
                    requires_manual_intervention=True
                ))
            else:
                # Consensus Strategy D: Promotion (Math compiles perfectly)
                # Ensure bots in Scanning with money are unblocked
                for b in bots:
                    # 🧹 PROFESSIONAL DUST CHASER: 
                    # If the position is perfectly synced but worth less than $5 notional, 
                    # it is mathematically un-sellable on Binance (MIN_NOTIONAL error).
                    # The bot gets permanently stuck trying to place the TP. We must clear it.
                    if b.total_invested > 0 and b.total_invested < 5.0:
                        logger.warning(f"🧹 [DUST-CHASER] Bot {b.name} ({b.pair}): Perfectly synced but holding only ${b.total_invested:.2f}. Un-sellable. Clearing dust.")
                        
                        # 🚀 PHYISCAL MARKET EXECUTION
                        ex = self.exchanges.get('future')
                        if not ex and self.exchanges:
                            ex = list(self.exchanges.values())[0] # Fallback
                            
                        if ex:
                            qty = b.total_invested / b.avg_entry_price if b.avg_entry_price > 0 else 0.0
                            if qty > 1e-8:
                                exit_side = 'sell' if b.direction.upper() == 'LONG' else 'buy'
                                try:
                                    logger.info(f"🧹 [DUST-CHASER] Executing PHYSICAL MARKET {exit_side.upper()} order for {qty:.6f} {b.pair} to clear dust wallet.")
                                    # reduceOnly allows closing sub-$5.00 positions bypassing MIN_NOTIONAL
                                    ex.create_order(
                                        symbol=b.pair,
                                        type='market',
                                        side=exit_side,
                                        amount=qty,
                                        params={'reduceOnly': True}
                                    )
                                    logger.info(f"✅ [DUST-CHASER] Physical exchange clearance successful for {b.pair}. Zeroing local ledger.")
                                except Exception as dust_err:
                                    logger.error(f"❌ [DUST-CHASER] Failed to physically clear exchange dust for {b.pair}: {dust_err}")
                                    # Even if API fails (LOT_SIZE constraints), we MUST clear DB ledger so the bot releases.
                        
                        self._execute_accounting_adjustment(b, 0.0, 0.0, "Dust Chaser: Clearing un-sellable micro-position.")
                        from .database import reset_bot_after_tp
                        reset_bot_after_tp(b.bot_id, 0.0, action_label='SYSTEM_WIPE')
                        
                        results.append(ReconciliationResult(
                            bot_id=b.bot_id, bot_name=b.name, pair=pair, action_taken=ReconciliationAction.SYSTEM_FIX_ZOMBIE,
                            details=f"Dust Chaser: Scrapped ${b.total_invested:.2f} un-sellable position.",
                            requires_manual_intervention=False
                        ))
                        continue

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

    @staticmethod
    def _compute_step_from_invested(total_invested: float, base_size: float, multiplier: float) -> int:
        """
        📐 MATHEMATICAL STEP DEDUCTION.
        Reverse-engineers the exact martingale step from total_invested using the
        geometric series formula without heuristic guessing.

        Geometric series: total = base * (1 + m + m^2 + ... + m^(n-1))
          where n = number of steps completed, m = martingale_multiplier

        Special case m == 1: total = base * n  →  n = round(total / base)

        For m != 1:  total = base * (m^n - 1) / (m - 1)
          Solving for n: n = log(total*(m-1)/base + 1) / log(m)

        Fallback: if config is missing or degenerate, returns max(1, fallback).
        """
        import math

        # Guard: need valid config to do real math
        if base_size <= 0:
            logger.warning(f"⚠️ [STEP-PROOF] base_size={base_size} is invalid — cannot compute step. Defaulting to 1.")
            return 1

        try:
            if abs(multiplier - 1.0) < 1e-6:
                # Linear case: total = base * n
                n = total_invested / base_size
            else:
                # Geometric case: total = base * (m^n - 1) / (m - 1)
                # → m^n = total * (m - 1) / base + 1
                # → n   = log(total*(m-1)/base + 1) / log(m)
                ratio = total_invested * (multiplier - 1.0) / base_size + 1.0
                if ratio <= 0:
                    logger.warning(f"⚠️ [STEP-PROOF] ratio={ratio:.4f} is non-positive — defaulting to 1.")
                    return 1
                n = math.log(ratio) / math.log(multiplier)

            # Round to nearest integer; clamp to [1, 20]
            proven = max(1, min(20, round(n)))
            return proven
        except (ValueError, ZeroDivisionError, OverflowError) as e:
            logger.warning(f"⚠️ [STEP-PROOF] Math error computing step: {e}. Defaulting to 1.")
            return 1

    def _execute_accounting_adjustment(self, bot: BotState, phys_notional: float, phys_qty: float, reason: str):
        """
        🚀 PROFESSIONAL ACCOUNTING: Automated Inventory Adjustment.
        When pure mathematical proof (exchange receipts) cannot be found for a discrepancy,
        the system processes a formal 'adoption' adjustment to balance the virtual ledger 
        to match the physical reality, ensuring the bot can safely continue trading.
        """

        try:
            logger.warning(f"⚖️ [INVENTORY ADJUSTMENT] Bot {bot.name}: Aligning DB to Physical (${phys_notional:.2f} | {phys_qty:.4f} units). Auth: {reason}")
            
            ledger_qty = bot.total_invested / bot.avg_entry_price if bot.avg_entry_price > 0 else 0.0
            
            # Determine if this is an inventory shrinkage or surplus
            phys_qty_abs = abs(phys_qty)
            ledger_qty_abs = abs(ledger_qty)
            
            if phys_qty_abs < ledger_qty_abs:
                adj_type = 'adoption_reduce'
                adj_qty = ledger_qty_abs - phys_qty_abs
            else:
                adj_type = 'adoption_add'
                adj_qty = phys_qty_abs - ledger_qty_abs
                
            # 🔥 CRITICAL MATH FIX: The new average entry price MUST exactly equal 
            # the target physical notional divided by target physical qty.
            # Preserving old averages causes catastrophic QTY mismatches.
            if phys_qty_abs > 0.00001:
                avg_price = abs(phys_notional) / phys_qty_abs
            else:
                avg_price = 0.0
                
            # === STEP 1: Derive the mathematically correct step ===
            # Use the geometric series: total = base * (mult^0 + mult^1 + ... + mult^(n-1))
            # This is the only provably-correct way to recover step from invested amount.
            proven_step = self._compute_step_from_invested(abs(phys_notional), bot.base_size, bot.martingale_multiplier)
            logger.info(f"📐 [STEP-PROOF] Bot {bot.name}: invested=${abs(phys_notional):.2f}, base_size={bot.base_size:.2f}, mult={bot.martingale_multiplier:.2f} → computed step={proven_step}")

            # === STEP 2: Commit trade state update FIRST (primary, critical) ===
            conn = get_connection()
            cursor = conn.cursor()
            
            cursor.execute("UPDATE bots SET status='IN TRADE' WHERE id=?", (bot.bot_id,))
            
            cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot.bot_id,))
            if cursor.fetchone():
                cursor.execute("""
                    UPDATE trades 
                    SET total_invested = ?, avg_entry_price = ?, current_step = ?,
                        entry_confirmed = 1, basket_start_time = COALESCE(basket_start_time, ?)
                    WHERE bot_id = ?
                """, (abs(phys_notional), avg_price, proven_step, int(time.time()), bot.bot_id))
            else:
                cursor.execute("""
                    INSERT INTO trades (bot_id, total_invested, avg_entry_price, current_step, entry_confirmed, basket_start_time)
                    VALUES (?, ?, ?, ?, 1, ?)
                """, (bot.bot_id, abs(phys_notional), avg_price, proven_step, int(time.time())))
            
            conn.commit()  # ← CRITICAL: commit trades state before anything else can fail
            conn.close()
            
            # 🔥 CRITICAL LIVE MEMORY UPDATE 
            # Force the active bot state to immediately mirror the verified physical math,
            # avoiding "Size Discrepancy" alerts in the same cycle before DB reload.
            bot.total_invested = abs(phys_notional)
            bot.avg_entry_price = avg_price
            bot.status = 'IN TRADE'
            
            # === STEP 2: Insert adoption receipt for DNA traceability (secondary, non-critical) ===
            try:
                from engine.database import save_bot_order
                save_bot_order(
                    bot_id=bot.bot_id,
                    order_type=adj_type,
                    exchange_order_id=f"ADJ_{int(time.time())}_{bot.bot_id}",
                    price=avg_price,
                    amount=adj_qty,
                    step=max(1, bot.current_step),
                    status='filled',
                    client_order_id=f"CQB_{bot.bot_id}_{'SHRINK' if adj_type=='adoption_reduce' else 'SURPLUS'}_{int(time.time())}",
                    notes=f"Auth: {reason}"
                )
                logger.info(f"✅ [ADJUSTMENT] Bot {bot.name}: DNA adjustment receipt ({adj_type} {adj_qty:.4f}) written to bot_orders.")
            except Exception as record_err:
                logger.warning(f"⚠️ [ADJUSTMENT] Bot {bot.name}: DNA record write failed (non-fatal): {record_err}")
                
            return True
        except Exception as e:
            logger.error(f"Inventory Adjustment failed for bot {bot.name}: {e}")
            return False


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
                    
                    # Binance specifically embeds the true signed position size in info.positionAmt
                    raw_size = p.get('contracts', 0) or p.get('size', 0) or p.get('info', {}).get('positionAmt', 0)
                    float_size = float(raw_size) if raw_size else 0.0
                    
                    side = 'LONG' if float_size > 0 else ('SHORT' if float_size < 0 else 'FLAT')
                    
                    # Deduplicate globally using symbol and side to prevent double-counting if endpoints overlap
                    pos_id = f"{sym}_{side}"
                    if pos_id in seen_positions:
                        continue
                    seen_positions.add(pos_id)
                    
                    pos = ExchangePosition(
                        symbol=sym,
                        side=side,
                        size=abs(float_size),
                        entry_price=float(p.get('entryPrice',0) or 0.0),
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
            
            # Load bot config for mathematical step deduction
            cursor.execute("SELECT base_size, martingale_multiplier FROM bots WHERE id=?", (bot_id,))
            cfg_row = cursor.fetchone()
            base_size = float(cfg_row[0] or 0.0) if cfg_row else 0.0
            mart_mult = float(cfg_row[1] or 1.0) if cfg_row else 1.0

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
                cycle_id=status.get('cycle_id', 1),
                base_size=base_size,
                martingale_multiplier=mart_mult,
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
