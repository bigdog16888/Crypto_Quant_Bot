"""
Comprehensive State Reconciliation System

Handles:
1. Multi-bot position ownership on shared pairs
2. State recovery on bot restart
3. Orphaned position detection and resolution
4. Graceful shutdown and crash recovery

Architecture:
- Exchange is the single source of truth for positions
- Order IDs track which bot owns which orders
- First-claim policy for shared positions
- Comprehensive logging for audit trail
"""
import logging
import time
import sqlite3
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

from .database import (
    get_connection, get_bot_status, get_all_bots, reset_bot_after_tp,
    log_trade, get_bot_order_ids, save_bot_order, update_order_status,
    DB_PATH
)
from .exchange_interface import ExchangeInterface
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
    5. Taking appropriate actions
    """
    
    def __init__(self):
        self.exchanges = {
            'spot': ExchangeInterface(market_type='spot'),
            'future': ExchangeInterface(market_type='future')
        }
        self.results: List[ReconciliationResult] = []
        
    def get_exchange(self, market_type: str) -> ExchangeInterface:
        """Get exchange interface for market type"""
        return self.exchanges.get(market_type, self.exchanges['future'])
    
    def fetch_all_exchange_positions(self) -> Dict[str, ExchangePosition]:
        """Fetch all positions from all active market types"""
        positions = {}
        
        for mt, ex in self.exchanges.items():
            try:
                if mt == 'future':
                    raw_positions = ex.exchange.fetch_positions()
                else:
                    # Spot: check balances for base assets
                    raw_positions = self._fetch_spot_positions(ex)
                
                for pos in raw_positions:
                    sym = pos.get('symbol')
                    if sym:
                        positions[sym] = ExchangePosition(
                            symbol=sym,
                            side=pos.get('side'),
                            size=float(pos.get('contracts', 0) or 0),
                            entry_price=float(pos.get('entryPrice', 0) or 0),
                            mark_price=float(pos.get('markPrice', 0) or 0),
                            unrealized_pnl=float(pos.get('unrealizedPnl', 0) or 0)
                        )
            except Exception as e:
                logger.error(f"Failed to fetch {mt} positions: {e}")
        
        return positions
    
    def _fetch_spot_positions(self, ex: ExchangeInterface) -> List[Dict]:
        """Fetch spot positions from balances"""
        positions = []
        try:
            balance = ex.fetch_balance()
            if balance and 'total' in balance:
                for asset, amount in balance['total'].items():
                    if isinstance(amount, (int, float)) and amount > 0:
                        # Get price for this asset
                        pair = f"{asset}/USDT"
                        try:
                            ticker = ex.exchange.fetch_ticker(pair)
                            if ticker:
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
            
            # Get full status
            status = get_bot_status(bot_id)
            if not status:
                continue
            
            # Get order IDs
            order_ids = get_bot_order_ids(bot_id)
            
            # Check for confirmed entry in trade_history
            cursor.execute('''
                SELECT COUNT(*) FROM trade_history
                WHERE bot_id = ? AND action IN ('BUY', 'SELL')
                AND timestamp > ?
            ''', (bot_id, int(time.time()) - 86400))  # Last 24 hours
            has_confirmed_entry = cursor.fetchone()[0] > 0
            
            # Parse direction from bot params
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
                entry_order_id=order_ids.get('entry_order_id'),
                tp_order_id=order_ids.get('tp_order_id'),
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
        
        # Check bot_orders table for grid orders
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
    ) -> Dict[str, PositionOwner]:
        """
        Determine which bots own positions/orders on a pair.
        
        Uses First-Claim Policy:
        - Bot with earliest entry_order_id "owns" the position
        - Other bots on same pair become "passengers"
        - If no order IDs match, position is "orphan"
        """
        ownership = {}
        
        if not position or position.size == 0:
            # No position - all bots should be idle
            for bot in bot_states:
                if bot.pair == pair:
                    ownership[bot.bot_id] = PositionOwner.NONE
            return ownership
        
        # Find which bot placed the first order for this position
        owner_bot_id = None
        owner_order_time = float('inf')
        
        for order in orders:
            if order.status != 'open':
                continue
            
            matched_bot = self.match_order_to_bot(order.order_id, bot_states)
            if matched_bot and matched_bot.pair == pair:
                # Get order creation time from DB
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT created_at FROM bot_orders WHERE order_id = ?
                ''', (order.order_id,))
                result = cursor.fetchone()
                conn.close()
                
                order_time = result[0] if result else 0
                if order_time < owner_order_time:
                    owner_order_time = order_time
                    owner_bot_id = matched_bot.bot_id
        
        # Assign ownership
        for bot in bot_states:
            if bot.pair != pair:
                continue
            
            if bot.bot_id == owner_bot_id:
                ownership[bot.bot_id] = PositionOwner.OWNER
            else:
                # Other bots on same pair are passengers
                ownership[bot.bot_id] = PositionOwner.PASSENGER
        
        # Check for orphan position (no matching bot)
        if owner_bot_id is None:
            # Position exists but no bot claims it
            ownership[0] = PositionOwner.ORPHAN  # Special key for orphan
        
        return ownership
    
    def reconcile_bot(
        self,
        bot: BotState,
        position: Optional[ExchangePosition],
        orders: List[ExchangeOrder],
        ownership: Dict[str, PositionOwner]
    ) -> ReconciliationResult:
        """Reconcile a single bot's state"""
        
        owner_status = ownership.get(bot.bot_id, PositionOwner.NONE)
        
        # Scenario 1: Bot thinks in trade, Exchange has NO position
        if bot.in_trade and (not position or position.size == 0):
            logger.warning(f"🔄 {bot.name}: DB shows IN TRADE but Exchange has NO position")
            
            if bot.has_confirmed_entry:
                # Entry was confirmed - likely TP hit while offline
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=owner_status,
                    action_taken=ReconciliationAction.MARK_TP_HIT,
                    details="Entry was confirmed, position likely closed while offline. Resetting to IDLE.",
                    requires_manual_intervention=False
                )
            else:
                # Ghost trade - no confirmation
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
            if owner_status == PositionOwner.OWNER:
                # This bot SHOULD own the position - claim it
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=owner_status,
                    action_taken=ReconciliationAction.CLAIM_POSITION,
                    details=f"Position detected on exchange. Bot is owner. Importing: {position.size} @ {position.entry_price}",
                    requires_manual_intervention=False
                )
            elif owner_status == PositionOwner.PASSENGER:
                # Another bot owns this position
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=owner_status,
                    action_taken=ReconciliationAction.NO_ACTION,
                    details="Position exists but another bot is owner. Bot becomes passenger.",
                    requires_manual_intervention=False
                )
            else:
                # Orphan position
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=PositionOwner.ORPHAN,
                    action_taken=ReconciliationAction.REQUIRE_MANUAL,
                    details="ORPHAN POSITION detected! No bot claims this position. Manual intervention required.",
                    requires_manual_intervention=True
                )
        
        # Scenario 3: Both in trade - verify ownership
        if bot.in_trade and position and position.size > 0:
            if owner_status == PositionOwner.OWNER:
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=owner_status,
                    action_taken=ReconciliationAction.NO_ACTION,
                    details="State synchronized. Bot owns position.",
                    requires_manual_intervention=False
                )
            else:
                return ReconciliationResult(
                    bot_id=bot.bot_id,
                    bot_name=bot.name,
                    pair=bot.pair,
                    position_owner=owner_status,
                    action_taken=ReconciliationAction.NO_ACTION,
                    details="Position exists but bot is passenger. Monitoring only.",
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
            if result.action_taken == ReconciliationAction.RESET_TO_IDLE:
                logger.info(f"🔄 Resetting {result.bot_name} to IDLE")
                reset_bot_after_tp(result.bot_id, exit_price=0)
                log_trade(
                    bot_id=result.bot_id,
                    action='AUTO_RESET',
                    symbol=result.pair,
                    price=0,
                    amount=0,
                    cost_usdc=0,
                    order_id='AUTO_RESET',
                    step=0,
                    pnl=0,
                    notes=result.details
                )
                return True
            
            elif result.action_taken == ReconciliationAction.MARK_TP_HIT:
                logger.info(f"🎯 Marking TP hit for {result.bot_name}")
                # Use current price as exit price approximation
                current_price = exchange.get_last_price(result.pair)
                reset_bot_after_tp(result.bot_id, exit_price=current_price)
                log_trade(
                    bot_id=result.bot_id,
                    action='TP_HIT_OFFLINE',
                    symbol=result.pair,
                    price=current_price,
                    amount=0,
                    cost_usdc=0,
                    order_id='OFFLINE_TP',
                    step=0,
                    pnl=0,
                    notes="TP hit while bot was offline"
                )
                return True
            
            elif result.action_taken == ReconciliationAction.CLAIM_POSITION:
                logger.info(f"� Claiming position for {result.bot_name}")
                # Import position from exchange
                # This requires fetching position details and updating DB
                # For now, log and require manual review for safety
                log_trade(
                    bot_id=result.bot_id,
                    action='POSITION_IMPORT',
                    symbol=result.pair,
                    price=0,
                    amount=0,
                    cost_usdc=0,
                    order_id='IMPORT_CLAIM',
                    step=0,
                    pnl=0,
                    notes=f"Position claimed: {result.details}"
                )
                return True
            
            elif result.action_taken == ReconciliationAction.REQUIRE_MANUAL:
                logger.critical(f"🚨 MANUAL INTERVENTION REQUIRED: {result.details}")
                return True
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to execute action for {result.bot_name}: {e}")
            return False
    
    def reconcile_all(self) -> List[ReconciliationResult]:
        """
        Main reconciliation entry point.
        
        Performs full state reconciliation for all bots.
        """
        logger.info("=" * 60)
        logger.info("Starting comprehensive state reconciliation...")
        logger.info("=" * 60)
        
        # Step 1: Fetch all exchange state
        logger.info("📡 Fetching exchange state...")
        exchange_positions = self.fetch_all_exchange_positions()
        
        # Get unique pairs from all bots
        all_bots = get_all_bots()
        all_pairs = list(set([b[2] for b in all_bots if b[2]]))
        
        exchange_orders = self.fetch_all_exchange_orders(all_pairs)
        
        # Step 2: Get all bot states from DB
        logger.info("📊 Fetching bot states from database...")
        bot_states = self.get_bot_states()
        
        # Step 3: Reconcile each pair
        results = []
        processed_pairs = set()
        
        for bot in bot_states:
            if bot.pair in processed_pairs:
                continue
            processed_pairs.add(bot.pair)
            
            # Get exchange data for this pair
            position = exchange_positions.get(bot.pair)
            orders = exchange_orders.get(bot.pair, [])
            
            # Get all bots on this pair
            bots_on_pair = [b for b in bot_states if b.pair == bot.pair]
            
            # Determine ownership
            ownership = self.determine_position_ownership(
                position, orders, bots_on_pair, bot.pair
            )
            
            # Reconcile each bot
            for b in bots_on_pair:
                result = self.reconcile_bot(b, position, orders, ownership)
                results.append(result)
                
                # Execute action
                market_type = 'future'  # TODO: Get from bot config
                ex = self.get_exchange(market_type)
                self.execute_action(result, ex)
        
        self.results = results
        
        # Summary
        logger.info("=" * 60)
        logger.info("RECONCILIATION SUMMARY:")
        logger.info(f"  Total bots processed: {len(results)}")
        logger.info(f"  Actions taken: {sum(1 for r in results if r.action_taken != ReconciliationAction.NO_ACTION)}")
        logger.info(f"  Manual intervention required: {sum(1 for r in results if r.requires_manual_intervention)}")
        logger.info("=" * 60)
        
        return results


def sync_all_bots():
    """
    Main entry point for state synchronization.
    Call this on bot startup and periodically during operation.
    """
    reconciler = StateReconciler()
    return reconciler.reconcile_all()


def get_orphan_positions() -> List[Dict]:
    """
    Detect positions on exchange that don't match any bot.
    Returns list of orphan positions requiring manual review.
    """
    reconciler = StateReconciler()
    positions = reconciler.fetch_all_exchange_positions()
    bots = get_all_bots()
    bot_pairs = set([b[2] for b in bots if b[2]])
    
    orphans = []
    for sym, pos in positions.items():
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
    # Run reconciliation and print results
    logging.basicConfig(level=logging.INFO)
    
    print("Running state reconciliation...")
    results = sync_all_bots()
    
    print("\n" + "=" * 60)
    print("RESULTS:")
    for r in results:
        print(f"\n{r.bot_name} ({r.pair}):")
        print(f"  Owner: {r.position_owner.value}")
        print(f"  Action: {r.action_taken.value}")
        print(f"  Details: {r.details}")
        if r.requires_manual_intervention:
            print("  ⚠️  REQUIRES MANUAL REVIEW!")
    print("=" * 60)
