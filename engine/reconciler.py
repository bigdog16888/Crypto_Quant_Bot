"""
Comprehensive State Reconciliation System (Unified)

Merged from reconciliation.py and reconciler.py.
Handles:
1. Multi-bot position ownership on shared pairs
2. State recovery on bot restart (Auto-Healing)
3. Orphaned position detection and resolution (Smart Adoption)
4. Ghost order cleanup
5. Offline fill detection
6. Graceful shutdown and crash recovery
"""
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


class PositionOwner(Enum):
    """Position ownership status"""
    OWNER = "owner"           # This bot owns the position
    PASSENGER = "passenger"  # Bot tracks but doesn't own
    ORPHAN = "orphan"        # Position not claimed by any bot
    NONE = "none"            # No position exists


class ReconciliationAction(Enum):
    """Actions to take during reconciliation"""
    NO_ACTION = "no_action"
    CLAIM_POSITION = "claim_position"
    RESET_TO_IDLE = "reset_to_idle"
    CANCEL_ORDERS = "cancel_orders"
    MARK_TP_HIT = "mark_tp_hit"
    REPAIR_ORDERS = "repair_orders"
    REQUIRE_MANUAL = "require_manual"


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
    # Order tracking
    entry_order_id: Optional[str]
    tp_order_id: Optional[str]
    has_confirmed_entry: bool


@dataclass
class ExchangePosition:
    """Represents position data from exchange"""
    symbol: str
    side: Optional[str]  # 'long', 'short', None
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
    position_owner: PositionOwner
    action_taken: ReconciliationAction
    details: str
    requires_manual_intervention: bool


class StateReconciler:
    """
    Comprehensive state reconciliation engine.
    
    Works by:
    1. Fetching all exchange state (positions + orders)
    2. Mapping exchange orders to bots via Order ID
    3. Determining position ownership
    4. Reconciling DB state with exchange state
    5. Taking appropriate actions (Auto-Healing, Smart Adoption)
    """
    
    def __init__(self, exchanges: Optional[Dict[str, ExchangeInterface]] = None):
        if exchanges:
            self.exchanges = exchanges
        else:
            # Skip spot exchange if in FUTURES_ONLY_MODE
            if getattr(config, 'FUTURES_ONLY_MODE', False):
                self.exchanges = {
                    'future': ExchangeInterface(market_type='future')
                }
                logger.info("FUTURES_ONLY_MODE: Skipping spot exchange initialization")
            else:
                self.exchanges = {
                    'spot': ExchangeInterface(market_type='spot'),
                    'future': ExchangeInterface(market_type='future')
                }
        self.results: List[ReconciliationResult] = []
        
    def get_exchange(self, market_type: str) -> ExchangeInterface:
        """Get exchange interface for market type"""
        return self.exchanges.get(market_type, list(self.exchanges.values())[0])
    
    def fetch_all_exchange_positions(self) -> Tuple[bool, Dict[str, List[ExchangePosition]]]:
        """
        Fetch all positions from all active market types.
        Returns a tuple: (success: bool, positions: Dict[str, List[ExchangePosition]])
        """
        positions = {}
        success = True
        
        for mt, ex in self.exchanges.items():
            try:
                if mt == 'future':
                    raw_positions = ex.fetch_positions()
                else:
                    # Spot: check balances for base assets
                    raw_positions = self._fetch_spot_positions(ex)
                
                for pos in raw_positions:
                    sym = pos.get('symbol')
                    if sym:
                        if sym not in positions:
                            positions[sym] = []
                        
                        positions[sym].append(ExchangePosition(
                            symbol=sym,
                            side=pos.get('side'),
                            size=float(pos.get('contracts', 0) or pos.get('size', 0) or 0),
                            entry_price=float(pos.get('entryPrice', 0) or pos.get('price', 0) or 0),
                            mark_price=float(pos.get('markPrice', 0) or pos.get('price', 0) or 0),
                            unrealized_pnl=float(pos.get('unrealizedPnl', 0) or 0)
                        ))
            except Exception as e:
                logger.error(f"Failed to fetch {mt} positions: {e}")
                success = False
        
        return success, positions
    
    def _fetch_spot_positions(self, ex: ExchangeInterface) -> List[Dict[str, Any]]:
        """Fetch spot positions from balances"""
        positions: List[Dict[str, Any]] = []
        try:
            balance = ex.fetch_balance()
            if balance and isinstance(balance, dict) and 'total' in balance:
                total_balances = balance['total']
                if isinstance(total_balances, dict):
                    for asset, amount in total_balances.items():
                        if isinstance(amount, (int, float)) and amount > 0:
                            pair = f"{asset}/USDT"
                            try:
                                ticker = ex.fetch_ticker(pair)
                                if ticker and isinstance(ticker, dict):
                                    positions.append({
                                        'symbol': pair,
                                        'side': 'long' if amount > 0 else 'short',
                                        'contracts': amount,
                                        'entryPrice': ticker.get('last', 0),
                                        'markPrice': ticker.get('last', 0),
                                        'unrealizedPnl': 0
                                    })
                            except:
                                pass
        except Exception as e:
            logger.error(f"Failed to fetch spot balances: {e}")
        return positions
    
    def fetch_all_exchange_orders(self, pairs: List[str]) -> Dict[str, List[ExchangeOrder]]:
        """Fetch all open orders for given pairs"""
        orders_by_pair = {}
        
        for pair in pairs:
            orders_by_pair[pair] = []
            for mt, ex in self.exchanges.items():
                try:
                    raw_orders = ex.fetch_open_orders(pair)
                    if raw_orders:
                        for o in raw_orders:
                            orders_by_pair[pair].append(ExchangeOrder(
                                order_id=str(o.get('id', '')),
                                symbol=pair,
                                side=o.get('side', ''),
                                order_type=o.get('type', 'limit'),
                                price=float(o.get('price', 0) or 0),
                                amount=float(o.get('amount', 0) or 0),
                                status=o.get('status', 'open'),
                                client_order_id=o.get('clientOrderId')
                            ))
                except Exception as e:
                    logger.warning(f"Failed to fetch orders for {pair} on {mt}: {e}")
        
        return orders_by_pair
    
    def get_bot_states(self) -> List[BotState]:
        """Get state of all bots from database"""
        bots = get_all_bots()
        bot_states = []
        
        conn = get_connection()
        cursor = conn.cursor()
        
        for bot in bots:
            bot_id, name, pair, is_active, strat_type, total_invested, current_step = bot[:7]
            
            status = get_bot_status(bot_id)
            if not status:
                continue
            
            order_ids = get_bot_order_ids(bot_id)
            
            cursor.execute('''
                SELECT COUNT(*) FROM trade_history
                WHERE bot_id = ? AND action IN ('BUY', 'SELL')
                AND timestamp > ?
            ''', (bot_id, int(time.time()) - 86400))
            has_confirmed_entry = cursor.fetchone()[0] > 0
            
            cursor.execute('SELECT direction FROM bots WHERE id = ?', (bot_id,))
            dir_result = cursor.fetchone()
            direction = dir_result[0] if dir_result else 'LONG'
            
            bot_states.append(BotState(
                bot_id=bot_id,
                name=name,
                pair=pair,
                direction=direction,
                is_active=bool(is_active),
                in_trade=total_invested > 0,
                total_invested=total_invested or 0,
                avg_entry_price=status[4] or 0,
                target_tp_price=status[5] or 0,
                current_step=current_step or 0,
                entry_order_id=str(order_ids.get('entry_order_id')) if order_ids.get('entry_order_id') else None,
                tp_order_id=str(order_ids.get('tp_order_id')) if order_ids.get('tp_order_id') else None,
                has_confirmed_entry=has_confirmed_entry
            ))
        
        conn.close()
        return bot_states
    
    def match_order_to_bot(self, order_id: str, bot_states: List[BotState]) -> Optional[BotState]:
        """Find which bot owns a specific order ID"""
        if not order_id:
            return None
        
        for bot in bot_states:
            if bot.entry_order_id == order_id:
                return bot
            if bot.tp_order_id == order_id:
                return bot
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT bot_id FROM bot_orders WHERE order_id = ? LIMIT 1
        ''', (order_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            for bot in bot_states:
                if bot.bot_id == result[0]:
                    return bot
        
        return None
    
    def determine_position_ownership(
        self,
        position: Optional[ExchangePosition],
        orders: List[ExchangeOrder],
        bot_states: List[BotState],
        pair: str
    ) -> Dict[int, PositionOwner]:
        """
        Determine which bots own positions/orders on a pair.
        """
        ownership = {}
        
        for bot in bot_states:
            if bot.pair != pair:
                continue
            
            if bot.in_trade:
                ownership[bot.bot_id] = PositionOwner.OWNER
            else:
                ownership[bot.bot_id] = PositionOwner.NONE
                
        return ownership

    def reconcile_bot(
        self,
        bot: BotState,
        position: Optional[ExchangePosition],
        orders: List[ExchangeOrder],
        ownership: Dict[int, PositionOwner]
    ) -> ReconciliationResult:
        """Reconcile a single bot's state"""
        
        owner_status = ownership.get(bot.bot_id, PositionOwner.NONE)
        
        # Scenario 1: Bot thinks in trade, Exchange has NO position
        if bot.in_trade and (not position or position.size == 0):
            # Virtual Positioning Logic
            bots_on_pair_list = get_all_bots()
            net_virtual_size = 0.0
            
            conn = get_connection()
            cursor = conn.cursor()
            
            for b_tuple in bots_on_pair_list:
                b_id, b_pair = b_tuple[0], b_tuple[2]
                if b_pair != bot.pair: continue
                
                status = get_bot_status(b_id)
                if status and status[3] > 0:
                    cursor.execute('SELECT direction FROM bots WHERE id = ?', (b_id,))
                    d_res = cursor.fetchone()
                    if d_res:
                        d_dir = d_res[0]
                        qty = status[3] / status[4] if status[4] > 0 else 0
                        if d_dir == 'LONG':
                            net_virtual_size += qty
                        else:
                            net_virtual_size -= qty
            
            conn.close()
            
            if abs(net_virtual_size) < 0.0001:
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=owner_status,
                    action_taken=ReconciliationAction.NO_ACTION,
                    details="Virtually Hedged (Net Position ~0). State preserved.",
                    requires_manual_intervention=False
                )

            logger.warning(f"🔄 {bot.name}: DB shows IN TRADE but Exchange has NO position (Net Virtual: {net_virtual_size:.4f})")
            
            if bot.has_confirmed_entry:
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=owner_status,
                    action_taken=ReconciliationAction.MARK_TP_HIT,
                    details=f"Offline Close Detected. Expected TP: ${bot.target_tp_price:.2f}. Resetting.",
                    requires_manual_intervention=False
                )
            else:
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=owner_status,
                    action_taken=ReconciliationAction.RESET_TO_IDLE,
                    details="Ghost trade detected (no entry confirmation). Resetting to IDLE.",
                    requires_manual_intervention=False
                )
        
        # Scenario 2: Bot thinks IDLE, Exchange HAS position
        if not bot.in_trade and position and position.size > 0:
            pair_norm = normalize_symbol(bot.pair)
            pos_norm = normalize_symbol(position.symbol)
            
            if pair_norm == pos_norm:
                if owner_status == PositionOwner.OWNER:
                    return ReconciliationResult(
                        bot_id=bot.bot_id,
                        bot_name=bot.name,
                        pair=bot.pair,
                        position_owner=owner_status,
                        action_taken=ReconciliationAction.CLAIM_POSITION,
                        details=f"Position detected. Bot is owner. Importing: {position.size} @ {position.entry_price}",
                        requires_manual_intervention=False
                    )
            
            if bot.is_active and bot.pair.replace('/', '').upper() in position.symbol.replace('/', '').upper():
                logger.info(f"🔄 {bot.name}: Found orphaned position for active bot pair. Claiming...")
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=PositionOwner.OWNER,
                    action_taken=ReconciliationAction.CLAIM_POSITION,
                    details=f"Orphaned position recovered. Importing: {position.size} @ {position.entry_price}",
                    requires_manual_intervention=False
                )

            return ReconciliationResult(
                bot_id=bot.bot_id,
                bot_name=bot.name,
                pair=bot.pair,
                position_owner=PositionOwner.ORPHAN,
                action_taken=ReconciliationAction.NO_ACTION,
                details="Orphan position detected (Manual/External). Ignoring.",
                requires_manual_intervention=False
            )
        
        # Scenario 3: Both in trade
        if bot.in_trade and position and position.size > 0:
            return ReconciliationResult(
                bot_id=bot.bot_id,
                bot_name=bot.name,
                pair=bot.pair,
                position_owner=owner_status,
                action_taken=ReconciliationAction.NO_ACTION,
                details="State synchronized.",
                requires_manual_intervention=False
            )
        
        # Scenario 4: Both idle
        return ReconciliationResult(
            bot_id=bot.bot_id,
            bot_name=bot.name,
            pair=bot.pair,
            position_owner=PositionOwner.NONE,
            action_taken=ReconciliationAction.NO_ACTION,
            details="State synchronized. Bot is IDLE.",
            requires_manual_intervention=False
        )
    
    def execute_action(
        self,
        result: ReconciliationResult,
        exchange: ExchangeInterface
    ) -> bool:
        """Execute the reconciliation action"""
        if result.action_taken == ReconciliationAction.NO_ACTION:
            return True
        
        try:
            # Get bot direction first
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT direction FROM bots WHERE id = ?', (result.bot_id,))
            d_res = cursor.fetchone()
            d_dir = d_res[0] if d_res else 'LONG'

            if result.action_taken == ReconciliationAction.RESET_TO_IDLE:
                logger.info(f"🔄 Resetting {result.bot_name} to IDLE")
                reset_bot_after_tp(result.bot_id, exit_price=0, direction=d_dir, action_label='RESET', notes='Reconciler Reset')
                return True
            
            elif result.action_taken == ReconciliationAction.MARK_TP_HIT:
                logger.info(f"🎯 Marking TP hit for {result.bot_name}")
                current_price = exchange.get_last_price(result.pair)
                reset_bot_after_tp(result.bot_id, exit_price=current_price, direction=d_dir, action_label='RECONCILE_TP')
                return True
            
            elif result.action_taken == ReconciliationAction.CLAIM_POSITION:
                logger.info(f"🎯 Claiming position for {result.bot_name}")
                positions = exchange.fetch_positions()
                pair_normalized = normalize_symbol(result.pair)  # Use standard normalization!
                
                # Get bot's configured direction
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute('SELECT direction FROM bots WHERE id = ?', (result.bot_id,))
                bot_row = cursor.fetchone()
                bot_direction = bot_row[0] if bot_row else None
                
                for pos in positions:
                    pos_sym = normalize_symbol(pos.get('symbol', ''))  # Use standard normalization!
                    if pair_normalized == pos_sym:  # Exact match, not "in"
                        size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                        if size != 0:
                            entry_price = float(pos.get('entryPrice', 0) or pos.get('price', 0) or 0)
                            pos_direction = pos.get('side', 'long').upper()
                            
                            # CRITICAL: Check if bot direction matches position direction!
                            if bot_direction and bot_direction.upper() != pos_direction:
                                logger.warning(
                                    f"⛔ DIRECTION MISMATCH: Bot {result.bot_name} is {bot_direction} "
                                    f"but position is {pos_direction}. Skipping import!"
                                )
                                return False
                            
                            if entry_price > 0:
                                import_position_from_exchange(
                                    bot_id=result.bot_id,
                                    pair=result.pair,
                                    position_size=size,
                                    entry_price=entry_price,
                                    direction=pos_direction
                                )
                            break
                return True
            
            return True
        except Exception as e:
            logger.error(f"Failed to execute action for {result.bot_name}: {e}")
            return False
    
    def reconcile_all(self) -> List[ReconciliationResult]:
        """Main reconciliation entry point (StateReconciler style)"""
        logger.info("Starting comprehensive state reconciliation...")
        success, exchange_positions = self.fetch_all_exchange_positions()
        
        if not success:
            logger.error("Failed to fetch exchange positions. Aborting reconciliation.")
            return []
        
        all_bots = get_all_bots()
        all_pairs = list(set([b[2] for b in all_bots if b[2]]))
        exchange_orders = self.fetch_all_exchange_orders(all_pairs)
        bot_states = self.get_bot_states()
        
        results = []
        processed_pairs = set()
        
        for bot in bot_states:
            if bot.pair in processed_pairs:
                continue
            processed_pairs.add(bot.pair)
            
            position_list = exchange_positions.get(bot.pair, [])
            if not position_list:
                bot_norm = normalize_symbol(bot.pair)
                for p_sym, p_data in exchange_positions.items():
                    if normalize_symbol(p_sym) == bot_norm:
                        position_list = p_data
                        break
            
            position = None
            target_side = bot.direction.lower()
            if position_list:
                for p in position_list:
                    if str(p.side).lower() == target_side:
                        position = p
                        break
                if not position and len(position_list) == 1:
                    if str(position_list[0].side).lower() in ['both', 'none', target_side]:
                        position = position_list[0]
            
            orders = exchange_orders.get(bot.pair, [])
            bots_on_pair = [b for b in bot_states if b.pair == bot.pair]
            ownership = self.determine_position_ownership(position, orders, bots_on_pair, bot.pair)
            
            for b in bots_on_pair:
                result = self.reconcile_bot(b, position, orders, ownership)
                # Virtual Positioning: Each bot manages its own virtual position independently.
                # Multiple bots CAN trade the same pair. The ownership.py check_first_claim_policy
                # returns True for all bots (multi-bot allowed).
                results.append(result)
                ex = self.get_exchange('future')
                self.execute_action(result, ex)
        
        self.results = results
        return results

    # --- DEEP RECONCILER METHODS (Auto-Healing) ---

    def run(self):
        """Deep Reconciliation entry point (compat with DeepReconciler)"""
        logger.info("Starting Deep State Reconciliation (Auto-Healing)...")
        try:
            for market_type, exchange in self.exchanges.items():
                if not exchange: continue
                logger.info(f"Reconciling {market_type} orders...")
                self._reconcile_market(market_type, exchange)
                self._reconcile_positions(market_type, exchange)
            logger.info("✅ Deep Reconciliation Complete.")
        except Exception as e:
            logger.error(f"❌ Deep Reconciliation Failed: {e}")

    def _reconcile_market(self, market_type: str, exchange: ExchangeInterface):
        """Reconcile orders between exchange and DB"""
        try:
            try:
                ex_orders = exchange.exchange.fetch_open_orders()
            except Exception:
                ex_orders = []
                for sym in getattr(config, 'ALLOWED_SYMBOLS', []):
                    try:
                        orders = exchange.fetch_open_orders(sym)
                        ex_orders.extend(orders)
                    except: pass
            
            ex_order_map = {str(o['id']): o for o in ex_orders}
            
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT bo.order_id, bo.bot_id, b.pair 
                FROM bot_orders bo
                JOIN bots b ON bo.bot_id = b.id
                WHERE bo.status='open' AND b.is_active=1
            """)
            db_rows = cursor.fetchall()
            db_order_ids = {str(row[0]) for row in db_rows}

            # Detect Orphans
            for oid, order in ex_order_map.items():
                if oid not in db_order_ids:
                    client_id = order.get('clientOrderId', '')
                    if client_id.startswith('CQB_'):
                        self._cancel_orphan(exchange, order, is_tagged=True)
                    else:
                        self._log_manual_orphan(order)

            # Detect Ghosts
            for oid_str, bot_id, symbol in db_rows:
                if oid_str not in ex_order_map:
                    is_valid_symbol = False
                    try:
                        if symbol in exchange.exchange.markets:
                            is_valid_symbol = True
                    except: pass
                    
                    if is_valid_symbol:
                        self._close_ghost(oid_str, bot_id)
            conn.close()
        except Exception as e:
            logger.error(f"Failed to reconcile market {market_type}: {e}")

    def _cancel_orphan(self, exchange: ExchangeInterface, order: dict, is_tagged: bool = False):
        if is_tagged:
            try:
                logger.info(f"🗑️ Cancelling Tagged Orphan: {order['symbol']} (ID: {order['id']})")
                exchange.cancel_order(order['symbol'], order['id'])
            except Exception as e:
                logger.error(f"Failed to cancel orphan {order['id']}: {e}")

    def _log_manual_orphan(self, order: dict):
        logger.warning(f"⚠️ UNTAGGED ORDER (Possibly Manual): {order['symbol']} (ID: {order['id']})")

    def _close_ghost(self, order_id: str, bot_id: int):
        try:
            logger.warning(f"👻 Closing Ghost Order in DB: ID {order_id} (Bot {bot_id})")
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE bot_orders SET status='closed', updated_at=CURRENT_TIMESTAMP WHERE order_id=?", (order_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to close ghost {order_id}: {e}")

    def detect_offline_fills(self, exchange: ExchangeInterface, since_hours: int = 48):
        """Detect orders filled while bot was offline"""
        stats = {'grid_fills': 0, 'tp_fills': 0, 'entry_fills': 0, 'total_checked': 0}
        try:
            since_timestamp = int((time.time() - (since_hours * 3600)) * 1000)
            try:
                closed_orders = exchange.exchange.fetch_closed_orders(since=since_timestamp)
            except Exception:
                closed_orders = []
                for sym in getattr(config, 'ALLOWED_SYMBOLS', []):
                    try:
                        orders = exchange.exchange.fetch_closed_orders(sym, since=since_timestamp)
                        closed_orders.extend(orders)
                    except: pass
            
            stats['total_checked'] = len(closed_orders)
            if not closed_orders: return stats
            
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
            db_open_orders = {str(row[0]): row for row in cursor.fetchall()}
            
            for order in closed_orders:
                oid = str(order.get('id', ''))
                if oid in db_open_orders:
                    db_order = db_open_orders[oid]
                    order_id, bot_id, order_type, step, db_price, db_amount, bot_name, pair, direction, current_step, total_invested, avg_entry = db_order
                    
                    if order.get('status') in ['closed', 'filled']:
                        fill_price = float(order.get('average', 0) or order.get('price', 0))
                        fill_amount = float(order.get('filled', 0) or order.get('amount', 0))
                        
                        if order_type == 'grid':
                            self._handle_offline_grid_fill(cursor, bot_id, bot_name, fill_price, fill_amount, current_step, total_invested, avg_entry)
                            self._mark_order_filled(cursor, oid, fill_price)
                            stats['grid_fills'] += 1
                        elif order_type == 'tp':
                            self._handle_offline_tp_fill(bot_id, bot_name, fill_price)
                            self._mark_order_filled(cursor, oid, fill_price)
                            stats['tp_fills'] += 1
                        elif order_type == 'entry':
                            self._handle_offline_entry_fill(cursor, bot_id, bot_name, fill_price, fill_amount)
                            self._mark_order_filled(cursor, oid, fill_price)
                            stats['entry_fills'] += 1
            conn.commit()
            conn.close()
            return stats
        except Exception as e:
            logger.error(f"Offline fill detection failed: {e}")
            return stats

    def _handle_offline_grid_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount, current_step, total_invested, avg_entry):
        new_step = (current_step or 0) + 1
        fill_cost = fill_price * fill_amount
        new_invested = (total_invested or 0) + fill_cost
        new_avg_entry = ( (total_invested or 0) * (avg_entry or 0) + fill_cost ) / new_invested if new_invested > 0 else fill_price
        
        cursor.execute("UPDATE trades SET current_step = ?, total_invested = ?, avg_entry_price = ? WHERE bot_id = ?", 
                       (new_step, new_invested, new_avg_entry, bot_id))
        log_trade(bot_id, 'OFFLINE_GRID_FILL', None, fill_price, fill_amount, fill_cost, 
                  f'OFFLINE_{new_step}', new_step, 0, f"Grid fill offline at step {new_step}")

    def _handle_offline_tp_fill(self, bot_id, bot_name, fill_price):
        reset_bot_after_tp(bot_id, exit_price=fill_price)

    def _handle_offline_entry_fill(self, cursor, bot_id, bot_name, fill_price, fill_amount):
        fill_cost = fill_price * fill_amount
        cursor.execute("""
            UPDATE trades SET current_step = 1, total_invested = ?, avg_entry_price = ?, entry_confirmed = 1, basket_start_time = ?
            WHERE bot_id = ?
        """, (fill_cost, fill_price, int(time.time()), bot_id))
        log_trade(bot_id, 'OFFLINE_ENTRY_FILL', None, fill_price, fill_amount, fill_cost,
                  'OFFLINE_ENTRY', 1, 0, "Entry fill offline")

    def _reconcile_positions(self, market_type: str, exchange: ExchangeInterface):
        """Smart Position Adoption"""
        try:
            positions = exchange.fetch_positions()
            active_positions = {p['symbol']: p for p in positions if float(p.get('contracts', 0) or p.get('size', 0) or 0) > 0}
            if not active_positions: return

            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT b.id, b.name, b.pair, t.bot_id 
                FROM bots b
                LEFT JOIN trades t ON b.id = t.bot_id
                WHERE b.is_active=1
            """)
            bot_map = {}
            for bid, bname, bpair, t_bot_id in cursor.fetchall():
                if bpair not in bot_map: bot_map[bpair] = []
                bot_map[bpair].append({'id': bid, 'name': bname, 'has_trade': t_bot_id is not None})
            
            for symbol, pos in active_positions.items():
                bots = bot_map.get(symbol, [])
                if bots and not any(b['has_trade'] for b in bots):
                    self._attempt_adoption(cursor, bots[0], pos)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Position reconciliation failed: {e}")

    def _attempt_adoption(self, cursor, bot: dict, pos: dict):
        bot_id = bot['id']
        pair = pos['symbol']
        size = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
        entry = float(pos.get('entryPrice', 0) or pos.get('price', 0) or 0)
        
        logger.warning(f"🩹 AUTO-ADOPTING: {pair} for Bot {bot['name']}")
        cursor.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,)) 
        cursor.execute("""
            INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, entry_confirmed, basket_start_time)
            VALUES (?, 1, ?, ?, 1, ?)
        """, (bot_id, size*entry, entry, int(time.time())))
        log_trade(bot_id, 'AUTO_ADOPT', pair, entry, size, size*entry, 
                  'RECOVER_STATE', 1, 0, "Auto-Adopted Orphan Position")

    def _mark_order_filled(self, cursor, order_id, fill_price):
        cursor.execute("""
            UPDATE bot_orders SET status='filled', filled_at=?, price=?, updated_at=?
            WHERE order_id = ?
        """, (int(time.time()), fill_price, int(time.time()), order_id))


# Compatibility Alias
DeepReconciler = StateReconciler


def sync_filled_orders(exchange: ExchangeInterface, bot_states: List[Any]) -> int:
    """Startup check for offline fills (backward compatibility)"""
    reconciler = StateReconciler()
    stats = reconciler.detect_offline_fills(exchange)
    return stats['grid_fills'] + stats['tp_fills'] + stats['entry_fills']


def sync_all_bots():
    """Main entry point for state synchronization"""
    reconciler = StateReconciler()
    return reconciler.reconcile_all()


def get_orphan_positions() -> List[Dict]:
    """Detect orphan positions"""
    reconciler = StateReconciler()
    success, positions = reconciler.fetch_all_exchange_positions()
    bots = get_all_bots()
    bot_pairs = set([b[2] for b in bots if b[2]])
    
    orphans = []
    for sym, pos_list in positions.items():
        for pos in pos_list:
            if pos.size != 0 and sym not in bot_pairs:
                orphans.append({
                    'symbol': sym,
                    'side': pos.side,
                    'size': pos.size,
                    'entry_price': pos.entry_price,
                    'mark_price': pos.mark_price
                })
    return orphans


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Running state reconciliation...")
    results = sync_all_bots()
    print("\nRESULTS:")
    for r in results:
        print(f"{r.bot_name} ({r.pair}): {r.action_taken.value}")
