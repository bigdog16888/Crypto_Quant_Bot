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
    import_position_from_exchange, update_martingale_step,
    DB_PATH
)
from .exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

logger = logging.getLogger("StateReconciliation")


class ReconciliationAction(Enum):
    """Actions to take during reconciliation"""
    NO_ACTION = "no_action"
    RESET_TO_IDLE = "reset_to_idle"
    MARK_TP_HIT = "mark_tp_hit"
    REPAIR_ORDERS = "repair_orders"
    REQUIRE_MANUAL = "require_manual"
    SYSTEM_FIX_ZOMBIE = "system_fix_zombie"


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
        
    def get_exchange(self, market_type: str) -> ExchangeInterface:
        return self.exchanges.get(market_type, list(self.exchanges.values())[0])

    # ------------------------------------------------------------------
    # STEP 1: OFFLINE FILL DETECTION
    # ------------------------------------------------------------------
    def detect_offline_fills(self, since_hours: int = 48) -> Dict[str, int]:
        """
        Scans exchange history for orders that filled while we were offline.
        Updates the DB immediately so subsequent checks see the correct state.
        """
        stats = {'grid_fills': 0, 'tp_fills': 0, 'entry_fills': 0, 'total': 0}
        
        for mt, ex in self.exchanges.items():
            if not ex: continue
            try:
                # Fetch recent closed orders
                since_ts = int((time.time() - (since_hours * 3600)) * 1000)
                try:
                    closed_orders = ex.exchange.fetch_closed_orders(since=since_ts)
                except Exception:
                    # Fallback for exchanges requiring symbols
                    closed_orders = []
                    for sym in getattr(config, 'ALLOWED_SYMBOLS', []):
                        try:
                            closed_orders.extend(ex.exchange.fetch_closed_orders(sym, since=since_ts))
                        except: pass
                
                if not closed_orders: continue
                
                # Filter for orders we know about in DB that are still 'open'
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT bo.order_id, bo.bot_id, bo.order_type, bo.step, bo.price, bo.amount,
                           b.name, b.pair, b.direction, t.current_step, t.total_invested, t.avg_entry_price
                    FROM bot_orders bo
                    JOIN bots b ON bo.bot_id = b.id
                    LEFT JOIN trades t ON bo.bot_id = t.bot_id
                    WHERE bo.status = 'open' AND bo.order_id IS NOT NULL
                """)
                db_open_rows = cursor.fetchall()
                db_map = {str(row[0]): row for row in db_open_rows}
                
                for order in closed_orders:
                    oid = str(order.get('id', ''))
                    if oid in db_map and order.get('status') in ['filled', 'closed']:
                        # FOUND ONE! It filled while we were sleeping.
                        row = db_map[oid]
                        # unpack row
                        (order_id, bot_id, order_type, step, db_price, db_amount, 
                         bot_name, pair, direction, current_step, total_invested, avg_entry) = row
                         
                        fill_price = float(order.get('average', 0) or order.get('price', 0))
                        fill_qty = float(order.get('filled', 0) or order.get('amount', 0))
                        
                        logger.info(f"🕵️ DETECTED OFFLINE FILL: {bot_name} {order_type} {pair} @ {fill_price}")
                        
                        if order_type == 'tp':
                            self._handle_offline_tp_fill(bot_id, bot_name, fill_price)
                            stats['tp_fills'] += 1
                        elif order_type == 'grid':
                            self._handle_offline_grid_fill(cursor, bot_id, bot_name, fill_price, fill_qty, current_step, total_invested, avg_entry)
                            stats['grid_fills'] += 1
                        elif order_type == 'entry':
                            self._handle_offline_entry_fill(cursor, bot_id, bot_name, fill_price, fill_qty)
                            stats['entry_fills'] += 1
                            
                        # Mark order as filled in DB
                        self._mark_order_filled(cursor, oid, fill_price)
                        
                conn.commit()
                conn.close()
                
            except Exception as e:
                logger.error(f"Failed offline fill check on {mt}: {e}")
                
        stats['total'] = stats['grid_fills'] + stats['tp_fills'] + stats['entry_fills']
        return stats

    def _mark_order_filled(self, cursor, order_id, fill_price):
        cursor.execute("""
            UPDATE bot_orders SET status='filled', filled_at=?, price=?, updated_at=?
            WHERE order_id = ?
        """, (int(time.time()), fill_price, int(time.time()), order_id))

    def _handle_offline_tp_fill(self, bot_id, bot_name, fill_price):
        reset_bot_after_tp(bot_id, exit_price=fill_price, action_label='OFFLINE_TP')
        # Note: reset_bot_after_tp handles DB commits internaly, but here we are in a transaction from caller...
        # Ideally reset_bot_after_tp should be transaction-aware or we use a separate connection. 
        # Using a separate connection inside reset_bot_after_tp is fine as sqlite handles concurrency (WAL).

    def _handle_offline_grid_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount, current_step, total_invested, avg_entry):
        new_step = (current_step or 0) + 1
        fill_cost = fill_price * fill_amount
        new_invested = (total_invested or 0) + fill_cost
        new_avg = ((total_invested or 0) * (avg_entry or 0) + fill_cost) / new_invested if new_invested > 0 else fill_price
        
        # UPSERT trade
        cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE trades SET current_step=?, total_invested=?, avg_entry_price=? WHERE bot_id=?", 
                           (new_step, new_invested, new_avg, bot_id))
        else:
            cursor.execute("INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price) VALUES (?,?,?,?)",
                           (bot_id, new_step, new_invested, new_avg))
        
        log_trade(bot_id, 'OFFLINE_GRID', None, fill_price, fill_amount, fill_cost, f"GRID_{new_step}", new_step, "Offline Grid Fill", 0)

    def _handle_offline_entry_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount):
        fill_cost = fill_price * fill_amount
        cursor.execute("SELECT bot_id FROM trades WHERE bot_id=?", (bot_id,))
        if cursor.fetchone():
            cursor.execute("UPDATE trades SET current_step=1, total_invested=?, avg_entry_price=?, entry_confirmed=1, basket_start_time=? WHERE bot_id=?",
                           (fill_cost, fill_price, int(time.time()), bot_id))
        else:
            cursor.execute("INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time) VALUES (?,1,?,?,1,?)",
                           (bot_id, fill_cost, fill_price, int(time.time())))
        
        log_trade(bot_id, 'OFFLINE_ENTRY', None, fill_price, fill_amount, fill_cost, "ENTRY", 1, "Offline Entry Fill", 0)

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
            if not bot.in_trade:
                continue
                
            # If we just started a trade (< 60s), give it grace period (propagation delay)
            if (time.time() - bot.basket_start_time) < 60:
                continue
            
            # Check if this bot's orders exist in the exchange list
            pair_orders = all_orders.get(bot.pair, [])
            
            # We look for ANY order tagged with this bot's ID (via clientOrderId or mapped ID)
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
                # ZOMBIE DETECTED
                # Bot thinks it's trading, but it has ZERO orders on the exchange.
                # This is a dangerous state (could be infinite loop of "waiting for fill").
                # ACTION: Reset to IDLE.
                logger.warning(f"🧟 ZOMBIE BOT DETECTED: {bot.name} (ID {bot.bot_id}) has status 'IN TRADE' but NO orders on exchange.")
                
                res = ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    action_taken=ReconciliationAction.RESET_TO_IDLE,
                    details="Zombie State Detected (In Trade but No Orders)",
                    requires_manual_intervention=False
                )
                results.append(res)
                
                # Execute Fix
                reset_bot_after_tp(bot.bot_id, exit_price=0, action_label='ZOMBIE_RESET', notes='Reconciler: No Orders Found')
        
        return results

    # ------------------------------------------------------------------
    # STEP 3: NET-SUM VERIFICATION
    # ------------------------------------------------------------------
    def verify_net_sum(self, bot_states: List[BotState], positions: Dict[str, List[ExchangePosition]]) -> List[ReconciliationResult]:
        """
        Calculates Net Virtual Position and compares with Net Physical Position.
        Warns if significant deviation exists.
        """
        results = []
        
        # Group bots by pair
        bots_by_pair = {}
        for bot in bot_states:
            if bot.pair not in bots_by_pair: bots_by_pair[bot.pair] = []
            bots_by_pair[bot.pair].append(bot)
            
        for pair, bots in bots_by_pair.items():
            # 1. Calc Virtual Net
            virtual_net = 0.0
            for b in bots:
                if b.in_trade:
                    qty = b.total_invested / b.avg_entry_price if b.avg_entry_price > 0 else 0
                    if b.direction.upper() == 'LONG':
                        virtual_net += qty
                    else:
                        virtual_net -= qty
            
            # 2. Get Physical Net
            physical_net = 0.0
            pair_positions = positions.get(pair, [])
            for p in pair_positions:
                if p.side.upper() == 'LONG': physical_net += p.size
                else: physical_net -= p.size
                
            # 3. Compare
            diff = abs(virtual_net - physical_net)
            
            # Threshold: 1% discrepancy or fixed dust amount
            is_mismatch = False
            if physical_net != 0:
                if diff / abs(physical_net) > 0.05: # >5% mismatch
                   is_mismatch = True
            elif virtual_net != 0: # physical is 0, virtual is not
                if abs(virtual_net) > 10.0: # Ignore dust < $10 equiv
                    is_mismatch = True
            
            if is_mismatch:
                msg = f"⚠️ SYSTEM BALANCE MISMATCH on {pair}: Virtual Net={virtual_net:.4f}, Physical Net={physical_net:.4f}"
                logger.warning(msg)
                # We do NOT return a result that triggers action here.
                # We just log it. The UI 'System Mismatch' banner handles the user alert.
                # Auto-correcting here is dangerous for 'Ownerless' manual trades.
                
        return results

    # ------------------------------------------------------------------
    # MAIN ENTRY POINT
    # ------------------------------------------------------------------
    def reconcile_all(self):
        logger.info("🔄 STARTING RECONCILIATION CYCLE")
        
        # 1. Offline Fills (Updates DB)
        self.detect_offline_fills()
        
        # 2. Fetch Fresh State
        bot_states = self.get_bot_states()
        success, all_positions = self.fetch_all_exchange_positions()
        
        # Flatten orders
        all_pairs = list(set([b.pair for b in bot_states]))
        all_orders = self.fetch_all_exchange_orders(all_pairs)
        
        results = []
        
        # 3. Individual Bot Validation (Zombies)
        zombie_results = self.validate_individual_bots(bot_states, all_orders)
        results.extend(zombie_results)
        
        # 4. Global Net Check
        if success:
            self.verify_net_sum(bot_states, all_positions)
        
        logger.info(f"✅ RECONCILIATION COMPLETE. {len(results)} actions taken.")
        return results

    # ------------------------------------------------------------------
    # HELPER METHODS (Preserved/Adapted)
    # ------------------------------------------------------------------
    def fetch_all_exchange_positions(self) -> Tuple[bool, Dict[str, List[ExchangePosition]]]:
        all_pos = {}
        success = True
        for mt, ex in self.exchanges.items():
            if not ex: continue
            try:
                raw = ex.fetch_positions()
                for p in raw:
                    sym = normalize_symbol(p.get('symbol', ''))
                    pos = ExchangePosition(
                        symbol=sym,
                        side='LONG' if float(p.get('contracts',0) or p.get('size',0)) > 0 else 'SHORT',
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
        for pair in pairs:
            orders_by_pair[pair] = []
            for mt, ex in self.exchanges.items():
                if not ex: continue
                try:
                    raw = ex.fetch_open_orders(pair)
                    for o in raw:
                        orders_by_pair[pair].append(ExchangeOrder(
                            order_id=str(o.get('id','')),
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
            bot_id, name, pair, is_active, strat, inv, step = b[:7]
            status = get_bot_status(bot_id)
            if not status: continue
            
            order_ids = get_bot_order_ids(bot_id)
            cursor.execute("SELECT COUNT(*) FROM trade_history WHERE bot_id=? AND action IN ('BUY','SELL') AND timestamp > ?", (bot_id, int(time.time()-86400)))
            confirmed = cursor.fetchone()[0] > 0
            
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
                has_confirmed_entry=confirmed
            ))
        conn.close()
        return states

# Alias for backward compatibility if needed
DeepReconciler = StateReconciler
