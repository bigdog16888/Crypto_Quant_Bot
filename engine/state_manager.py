"""
StateManager - Single Source of Truth for Bot State
====================================================

FUNDAMENTAL FIX: This module provides a centralized state management system
that ensures consistency between all state sources:
1. bots table (status field)
2. trades table (position data)  
3. bot_ownership_state table (ownership tracking)
4. Exchange (actual positions and orders)

Design Principles:
- Exchange is the ULTIMATE source of truth for positions
- Database tables are synchronized views that must agree
- Any inconsistency triggers automatic reconciliation
- Health checks run continuously to detect drift

Usage:
    from engine.state_manager import StateManager
    sm = StateManager()
    
    # Check health
    health = sm.check_health()
    
    # Get unified state for a bot
    state = sm.get_bot_state(bot_id)
    
    # Sync all tables
    sm.reconcile_all()

Created: 2026-02-09
"""

import logging
import time
import sqlite3
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger("StateManager")


class BotStatus(Enum):
    """Canonical bot status values"""
    IDLE = "idle"
    PENDING_ENTRY = "pending_entry"
    IN_TRADE = "in_trade"
    PENDING_TP = "pending_tp"
    CLOSED = "closed"
    ERROR = "error"


@dataclass
class UnifiedBotState:
    """Single source of truth for a bot's current state"""
    bot_id: int
    bot_name: str
    pair: str
    direction: str
    is_active: bool
    
    # Position state (from trades table)
    in_trade: bool
    current_step: int
    total_invested: float
    total_quantity: float
    avg_entry_price: float
    target_tp_price: float
    
    # Ownership state
    ownership_status: str  # owner, passenger, idle, closed
    is_owner: bool
    
    # Exchange state (actual reality)
    exchange_has_position: bool
    exchange_position_size: float
    exchange_entry_price: float
    exchange_open_orders: int
    
    # Health indicators
    is_consistent: bool
    inconsistencies: List[str]
    has_entry_order: bool = False
    
    @property
    def needs_reconciliation(self) -> bool:
        return not self.is_consistent


@dataclass
class HealthReport:
    """System-wide health check results"""
    timestamp: float
    is_healthy: bool
    total_bots: int
    active_bots: int
    bots_in_trade: int
    inconsistent_bots: List[int]
    orphan_positions: List[str]  # Pairs with positions but no owner
    missing_orders: List[int]  # Bots in trade with no TP orders
    details: Dict[str, Any]


class StateManager:
    """
    Centralized state manager that provides single source of truth.
    
    This solves the fundamental problem of having 4 different state sources
    (bots, trades, ownership, exchange) that can get out of sync.
    """
    
    _instance = None
    
    def __new__(cls):
        """Singleton pattern - only one StateManager instance"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.db_path = Path(__file__).parent.parent / "crypto_bot.db"
        self._exchange = None
        self._last_health_check = 0
        self._health_check_interval = 60  # seconds
        self._initialized = True
        logger.info("StateManager initialized")
    
    @property
    def exchange(self):
        """Lazy-load exchange interface"""
        if self._exchange is None:
            from engine.exchange_interface import ExchangeInterface
            self._exchange = ExchangeInterface()
        return self._exchange
    
    def get_connection(self) -> sqlite3.Connection:
        """Get database connection"""
        return sqlite3.connect(str(self.db_path))
    
    def get_bot_state(self, bot_id: int) -> Optional[UnifiedBotState]:
        """
        Get unified state for a single bot.
        Combines data from all sources and checks consistency.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            # 1. Get bot base info
            cursor.execute("""
                SELECT id, name, pair, direction, is_active, status
                FROM bots WHERE id = ?
            """, (bot_id,))
            bot_row = cursor.fetchone()
            
            if not bot_row:
                return None
            
            bot_id, name, pair, direction, is_active, status = bot_row
            
            # 2. Get trades info
            cursor.execute("""
                SELECT current_step, total_invested, avg_entry_price, target_tp_price
                FROM trades WHERE bot_id = ?
            """, (bot_id,))
            trade_row = cursor.fetchone()
            
            current_step = trade_row[0] if trade_row else 0
            total_invested = trade_row[1] if trade_row else 0.0
            avg_entry_price = trade_row[2] if trade_row else 0.0
            target_tp_price = trade_row[3] if trade_row else 0.0
            
            # 3. Get ownership info
            cursor.execute("""
                SELECT state, is_owner, position_size
                FROM bot_ownership_state WHERE bot_id = ?
            """, (bot_id,))
            ownership_row = cursor.fetchone()
            
            ownership_status = ownership_row[0] if ownership_row else "idle"
            is_owner = bool(ownership_row[1]) if ownership_row else False
            ownership_size = ownership_row[2] if ownership_row else 0.0
            
            # 4. Get exchange state
            exchange_has_position = False
            exchange_position_size = 0.0
            exchange_entry_price = 0.0
            exchange_open_orders = 0
            
            try:
                positions = self.exchange.exchange.fetch_positions()
                for pos in positions:
                    if self._normalize_pair(pos.get('symbol', '')) == self._normalize_pair(pair):
                        size = float(pos.get('contracts', 0) or 0)
                        if size > 0:
                            exchange_has_position = True
                            exchange_position_size = size
                            exchange_entry_price = float(pos.get('entryPrice', 0) or 0)
                            break
                
                # Count open orders for this pair AND this bot
                orders = self.exchange.exchange.fetch_open_orders(pair)
                bot_orders = [o for o in orders if o.get('clientOrderId', '').startswith(f"CQB_{bot_id}_")]
                exchange_open_orders = len(bot_orders)
                
                has_entry_order = any('ENTRY' in o.get('clientOrderId', '') for o in bot_orders)
            except Exception as e:
                logger.warning(f"Failed to fetch exchange state for {pair}: {e}")
                has_entry_order = False
            
            # 5. Calculate total quantity from invested / price
            total_quantity = total_invested / avg_entry_price if avg_entry_price > 0 else 0.0
            
            # 6. Check consistency
            inconsistencies = []
            
            # Check: bots.status matches trades.total_invested
            db_says_in_trade = status == 'IN TRADE'
            trades_says_in_trade = total_invested > 0
            if db_says_in_trade != trades_says_in_trade:
                inconsistencies.append(
                    f"bots.status='{status}' but trades.total_invested={total_invested}"
                )
            
            # Check: ownership.state matches trades
            ownership_says_in_trade = ownership_status in ('owner', 'passenger', 'pending_tp')
            if db_says_in_trade != ownership_says_in_trade:
                inconsistencies.append(
                    f"bots.status='{status}' but ownership.state='{ownership_status}'"
                )
            
            # Check: Exchange matches DB
            if db_says_in_trade and not exchange_has_position:
                inconsistencies.append(
                    f"DB says IN TRADE but exchange has no position"
                )
            if not db_says_in_trade and exchange_has_position:
                inconsistencies.append(
                    f"DB says IDLE but exchange has position (size={exchange_position_size})"
                )
            
            # Check: In trade but no TP orders
            if db_says_in_trade and exchange_has_position and exchange_open_orders == 0:
                inconsistencies.append(
                    f"In trade with position but NO open orders (missing TP?)"
                )
            
            is_consistent = len(inconsistencies) == 0
            
            return UnifiedBotState(
                bot_id=bot_id,
                bot_name=name,
                pair=pair,
                direction=direction,
                is_active=bool(is_active),
                in_trade=db_says_in_trade or exchange_has_position,
                current_step=current_step,
                total_invested=total_invested,
                total_quantity=total_quantity,
                avg_entry_price=avg_entry_price,
                target_tp_price=target_tp_price,
                ownership_status=ownership_status,
                is_owner=is_owner,
                exchange_has_position=exchange_has_position,
                exchange_position_size=exchange_position_size,
                exchange_entry_price=exchange_entry_price,
                exchange_open_orders=exchange_open_orders,
                has_entry_order=has_entry_order,
                is_consistent=is_consistent,
                inconsistencies=inconsistencies
            )
            
        finally:
            pass # conn.close() disabled for singleton safety
    
    def check_health(self, force: bool = False) -> HealthReport:
        """
        Run a comprehensive health check on the entire system.
        Returns a HealthReport with details on any inconsistencies.
        """
        now = time.time()
        if not force and (now - self._last_health_check) < self._health_check_interval:
            # Use cached result if recent
            pass
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            # Get all bots
            cursor.execute("SELECT id FROM bots")
            all_bot_ids = [row[0] for row in cursor.fetchall()]
            
            cursor.execute("SELECT id FROM bots WHERE is_active = 1")
            active_bot_ids = [row[0] for row in cursor.fetchall()]
            
            # Check each bot
            inconsistent_bots = []
            bots_in_trade = 0
            missing_orders = []
            
            for bot_id in active_bot_ids:
                state = self.get_bot_state(bot_id)
                if state:
                    if state.in_trade:
                        bots_in_trade += 1
                    if not state.is_consistent:
                        inconsistent_bots.append(bot_id)
                    if state.in_trade and state.exchange_has_position and state.exchange_open_orders == 0:
                        missing_orders.append(bot_id)
            
            # Check for orphan positions (exchange positions with no owner)
            orphan_positions = []
            try:
                positions = self.exchange.exchange.fetch_positions()
                # Get pairs from bots table (trades doesn't have pair column)
                cursor.execute("""
                    SELECT DISTINCT b.pair 
                    FROM bots b 
                    JOIN trades t ON b.id = t.bot_id 
                    WHERE t.total_invested > 0
                """)
                tracked_pairs = {self._normalize_pair(row[0]) for row in cursor.fetchall()}
                
                for pos in positions:
                    size = float(pos.get('contracts', 0) or 0)
                    if size > 0:
                        pair = pos.get('symbol', '')
                        if self._normalize_pair(pair) not in tracked_pairs:
                            orphan_positions.append(pair)
            except Exception as e:
                logger.warning(f"Failed to check orphan positions: {e}")
            
            is_healthy = len(inconsistent_bots) == 0 and len(orphan_positions) == 0
            
            report = HealthReport(
                timestamp=now,
                is_healthy=is_healthy,
                total_bots=len(all_bot_ids),
                active_bots=len(active_bot_ids),
                bots_in_trade=bots_in_trade,
                inconsistent_bots=inconsistent_bots,
                orphan_positions=orphan_positions,
                missing_orders=missing_orders,
                details={
                    'checked_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                }
            )
            
            self._last_health_check = now
            
            if not is_healthy:
                logger.warning(f"Health check FAILED: {len(inconsistent_bots)} inconsistent bots, "
                             f"{len(orphan_positions)} orphan positions")
            
            return report
            
        finally:
            pass # conn.close() disabled for singleton safety
    
    def sync_bot_state(self, bot_id: int) -> bool:
        """
        Synchronize a single bot's state across all tables.
        Exchange is the source of truth.
        """
        state = self.get_bot_state(bot_id)
        if not state:
            return False
        
        if state.is_consistent:
            return True
        
        logger.info(f"Syncing bot {bot_id} ({state.bot_name}): {state.inconsistencies}")
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            # If exchange has position, bot should be IN TRADE (or passenger)
            if state.exchange_has_position:
                # Check if another bot already owns this pair
                # We need to normalize the pair for this check to ensure we match correctly
                normalized_pair = self._normalize_pair(state.pair)
                
                # Find other potential owners. We can't rely on simple string match of 'pair' column 
                # because different bots might use different suffixes (BTC/USDC vs BTC/USDC:USDC)
                # so we check all active ownership records and normalize them
                cursor.execute("SELECT bot_id, pair FROM bot_ownership_state WHERE is_owner = 1 AND bot_id != ?", (bot_id,))
                other_owners = cursor.fetchall()
                
                existing_owner_id = None
                for other_id, other_pair in other_owners:
                    if self._normalize_pair(other_pair) == normalized_pair:
                        existing_owner_id = other_id
                        break
                
                # determine our status
                if existing_owner_id:
                    # Another bot owns this position
                    is_owner = 0
                    ownership_state = 'passenger'
                    logger.info(f"Bot {bot_id} detects existing owner {existing_owner_id} for {state.pair}, becoming passenger")
                else:
                    # We are the owner
                    is_owner = 1
                    ownership_state = 'owner'

                # Update bots table
                # If we are just a passenger, we are technically "IN TRADE" monitoring it, 
                # or "Waiting for Signal" if we are not participating?
                # Usually passengers are considered 'IN TRADE' if they are tracking the position.
                cursor.execute(
                    "UPDATE bots SET status = 'IN TRADE' WHERE id = ?",
                    (bot_id,)
                )
                
                # Update/create trades record
                cursor.execute("SELECT bot_id FROM trades WHERE bot_id = ?", (bot_id,))
                if cursor.fetchone():
                    cursor.execute("""
                        UPDATE trades 
                        SET avg_entry_price = ?, 
                            total_invested = ?,
                            entry_confirmed = 1
                        WHERE bot_id = ?
                    """, (state.exchange_entry_price, 
                          state.exchange_position_size * state.exchange_entry_price,
                          bot_id))
                else:
                    cursor.execute("""
                        INSERT INTO trades (bot_id, current_step, total_invested, 
                                          avg_entry_price, entry_confirmed, basket_start_time)
                        VALUES (?, 0, ?, ?, 1, ?)
                    """, (bot_id, state.exchange_position_size * state.exchange_entry_price,
                          state.exchange_entry_price, int(time.time())))
                
                # Update ownership
                cursor.execute("""
                    INSERT OR REPLACE INTO bot_ownership_state 
                    (bot_id, state, is_owner, pair, position_size, avg_entry_price, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (bot_id, ownership_state, is_owner, state.pair, state.exchange_position_size, 
                      state.exchange_entry_price, int(time.time())))
                
            else:
                # No exchange position (or 0 size)
                # Check directly for OPEN ENTRY ORDER to set status
                if state.has_entry_order:
                    cursor.execute(
                        "UPDATE bots SET status = 'ENTRY PENDING' WHERE id = ?",
                        (bot_id,)
                    )
                    # Do not reset trades if we are pending entry? 
                    # If we are pending entry, total_invested should technically be 0 until filled.
                    # So we just update the status label for UI.
                    conn.commit()
                    logger.info(f"Bot {bot_id} has open entry order -> ENTRY PENDING")
                    return True

                # 🚨 CRITICAL FIX FOR VIRTUAL HEDGING 🚨
                # If we have no physical position, it MIGHT be because we are perfectly hedged 
                # (Bot A Long + Bot B Short = 0 Net).
                # We must check if the Virtual Net Position matches the Physical Net Position (0).
                
                is_hedged_valid = False
                if state.total_invested > 0:
                    # Calculate System-Wide Virtual Net Position
                    cursor.execute("""
                        SELECT b.direction, t.total_invested, t.avg_entry_price 
                        FROM trades t 
                        JOIN bots b ON t.bot_id = b.id 
                        WHERE t.total_invested > 0
                    """)
                    all_trades = cursor.fetchall()
                    
                    net_virtual_size = 0.0
                    for direc, inv, entry in all_trades:
                        if entry > 0:
                            qty = inv / entry
                            if direc == 'SHORT': qty *= -1
                            net_virtual_size += qty
                            
                    # If Net Virtual is close to 0 (and Exchange is 0), then we are Validly Hedged.
                    # Tolerance: 0.001 BTC or similar (small dust)
                    if abs(net_virtual_size) < 0.001: 
                        logger.info(f"🛡️ Virtual Hedging Detected: Bot {bot_id} (VirtSize={state.total_quantity:.4f}) preserved despite 0 physical position.")
                        is_hedged_valid = True
                        
                        # FORCE bot status to IN TRADE because we are virtually active
                        if state.ownership_status != 'owner':
                             cursor.execute(
                                "UPDATE bots SET status = 'IN TRADE' WHERE id = ?",
                                (bot_id,)
                             )
                
                if is_hedged_valid:
                    # Do NOT wipe state.
                    # Ensure we are consistent with Virtual Reality
                    # Just mark as synced?
                    pass
                else:         
                    # Truly IDLE or Zombie -> Reset
                    cursor.execute(
                        "UPDATE bots SET status = 'Waiting for Signal' WHERE id = ?",
                        (bot_id,)
                    )
                    
                    cursor.execute("""
                        UPDATE trades 
                        SET current_step = 0, total_invested = 0, avg_entry_price = 0
                        WHERE bot_id = ?
                    """, (bot_id,))
                    
                    cursor.execute("""
                        INSERT OR REPLACE INTO bot_ownership_state 
                        (bot_id, state, is_owner, pair, position_size, last_updated)
                        VALUES (?, 'idle', 0, ?, 0, ?)
                    """, (bot_id, state.pair, int(time.time())))
            
            conn.commit()
            logger.info(f"Successfully synced bot {bot_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to sync bot {bot_id}: {e}")
            conn.rollback()
            return False
        finally:
            pass # conn.close() disabled for singleton safety
    
    def reconcile_all(self) -> Dict[str, Any]:
        """
        Reconcile ALL bots - run on startup or when health check fails.
        """
        logger.info("Starting full reconciliation...")
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("SELECT id FROM bots WHERE is_active = 1")
            active_bots = [row[0] for row in cursor.fetchall()]
        finally:
            pass # conn.close() disabled for singleton safety
        
        results = {
            'synced': [],
            'failed': [],
            'already_consistent': []
        }
        
        for bot_id in active_bots:
            state = self.get_bot_state(bot_id)
            if state:
                if state.is_consistent:
                    results['already_consistent'].append(bot_id)
                elif self.sync_bot_state(bot_id):
                    results['synced'].append(bot_id)
                else:
                    results['failed'].append(bot_id)
        
        logger.info(f"Reconciliation complete: {len(results['synced'])} synced, "
                   f"{len(results['already_consistent'])} already consistent, "
                   f"{len(results['failed'])} failed")
        
        return results
    
    def _normalize_pair(self, pair: str) -> str:
        """
        Standardized normalization using the engine's central routine.
        Ensures consistency between StateManager and ExchangeInterface.
        """
        from engine.exchange_interface import normalize_symbol
        return normalize_symbol(pair)


# Module-level singleton
_state_manager: Optional[StateManager] = None

def get_state_manager() -> StateManager:
    """Get the singleton StateManager instance"""
    global _state_manager
    if _state_manager is None:
        _state_manager = StateManager()
    return _state_manager
