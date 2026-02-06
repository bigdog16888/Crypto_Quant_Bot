
from __future__ import annotations
import time
import logging
import json
from typing import Any, Optional, Dict
from datetime import datetime, timedelta
from engine.database import get_connection
from engine.exchange_interface import ExchangeInterface, normalize_symbol
from config.settings import config

logger = logging.getLogger("RiskManager")

def get_daily_realized_pnl(bot_id: int | None = None) -> float:
    """
    Calculates realized PnL for the current day (UTC).
    If bot_id is provided, calculates for that specific bot.
    Otherwise, calculates for the entire account (all bots).
    """
    try:
        # Start of day (UTC)
        now = datetime.utcnow()
        start_of_day = datetime(now.year, now.month, now.day)
        start_ts = start_of_day.timestamp()
        
        conn = get_connection()
        cursor = conn.cursor()
        
        query = "SELECT SUM(pnl) FROM trade_history WHERE timestamp >= ?"
        params = [start_ts]
        
        if bot_id is not None:
            query += " AND bot_id = ?"
            params.append(bot_id)
            
        cursor.execute(query, params)
        result = cursor.fetchone()
        
        return result[0] if result and result[0] is not None else 0.0
        
    except Exception as e:
        logger.error(f"Error calculating daily PnL: {e}")
        return 0.0

def get_unrealized_pnl(bot_id: Optional[int] = None, exchange: Any = None, exchange_snapshot: Optional[Dict[str, Any]] = None) -> float:
    """
    Calculates unrealized PnL from open positions.
    If bot_id is provided, only calculates for that bot's pair.
    Otherwise, calculates for the entire account.
    Supports using pre-fetched exchange_snapshot to avoid redundant API calls.
    """
    try:
        unrealized_pnl = 0.0
        
        if bot_id is not None:
            # 1. Get bot's pair and market type from DB
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT pair, config FROM bots WHERE id = ?", (bot_id,))
            row = cursor.fetchone()
            if not row:
                return 0.0
            
            pair, config_json = row
            cfg = json.loads(config_json) if config_json else {}
            market_type = cfg.get('market_type', config.MARKET_TYPE)
            target_norm = normalize_symbol(pair)
            
            # 2. Check snapshot first
            if exchange_snapshot and market_type in exchange_snapshot:
                positions = exchange_snapshot[market_type].get('positions', [])
                for p in positions:
                    if normalize_symbol(p.get('symbol', '')) == target_norm:
                        unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
                return unrealized_pnl
            
            # 3. Fallback to existing exchange or create temporary one
            ex = exchange
            if ex is None or ex.market_type != market_type:
                ex = ExchangeInterface(market_type=market_type)
            
            # 4. Fetch positions and filter by pair
            positions = ex.fetch_positions()
            for p in positions:
                if normalize_symbol(p.get('symbol', '')) == target_norm:
                    unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
            
        else:
            # Entire Account PnL
            # 1. Check snapshot first (covers all market types in snapshot)
            if exchange_snapshot:
                for mt in exchange_snapshot:
                    positions = exchange_snapshot[mt].get('positions', [])
                    for p in positions:
                        unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
                return unrealized_pnl
                
            # 2. Use provided exchange if available
            if exchange:
                positions = exchange.fetch_positions()
                for p in positions:
                    unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
            else:
                # 3. Fallback: Fetch positions for the default market type
                ex = ExchangeInterface(market_type=config.MARKET_TYPE)
                positions = ex.fetch_positions()
                for p in positions:
                    unrealized_pnl += float(p.get('unrealizedPnl', 0.0) or 0.0)
                    
        return unrealized_pnl
        
    except Exception as e:
        logger.error(f"Error calculating unrealized PnL: {e}")
        return 0.0

def check_daily_loss_limit(limit_amount: float, bot_id: Optional[int] = None, exchange: Any = None, exchange_snapshot: Optional[Dict[str, Any]] = None) -> bool:
    """
    Checks if the total daily loss (Realized + Unrealized) exceeds the limit.
    limit_amount: Positive float representing the max usage (e.g. 50.0 for $50 loss).
    Returns True if loss limit reached (i.e. Realized_PnL + Unrealized_PnL <= -limit).
    """
    if limit_amount <= 0:
        return False
        
    # 1. Get Realized PnL from DB
    realized_pnl = get_daily_realized_pnl(bot_id)
    
    # 2. Get Unrealized PnL (from Snapshot or Exchange)
    unrealized_pnl = get_unrealized_pnl(bot_id, exchange, exchange_snapshot)
    
    total_pnl = realized_pnl + unrealized_pnl
    
    # Logic: If total PnL is -60 and Limit is 50, then -60 <= -50 is True -> Stop.
    if total_pnl <= -abs(limit_amount):
        logger.warning(f"Daily loss limit hit for {'bot ' + str(bot_id) if bot_id else 'account'}: "
                       f"Realized=${realized_pnl:.2f}, Unrealized=${unrealized_pnl:.2f}, "
                       f"Total=${total_pnl:.2f} <= Limit=${-abs(limit_amount):.2f}")
        return True
        
    return False

def check_drawdown_reduction(drawdown_pct: float, threshold_pct: float, reduction_factor: float = 0.5) -> Optional[Dict[str, Any]]:
    """
    Evaluates if a position should be reduced due to high drawdown.
    
    Args:
        drawdown_pct: Current unrealized drawdown (%) of the position.
        threshold_pct: Drawdown level to trigger reduction.
        reduction_factor: Amount to reduce (0.5 = 50%).
        
    Returns:
        Optional[Dict[str, Any]]: Action plan (e.g., {'action': 'reduce', 'factor': 0.5}) or None.
    """
    if threshold_pct <= 0 or drawdown_pct < threshold_pct:
        return None
        
    return {
        'action': 'reduce',
        'factor': reduction_factor,
        'reason': f"Drawdown {drawdown_pct:.2f}% > Limit {threshold_pct:.2f}%"
    }
