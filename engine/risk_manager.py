
import time
import logging
from datetime import datetime, timedelta
from engine.database import get_connection

logger = logging.getLogger("RiskManager")

def get_daily_realized_pnl(bot_id: int = None) -> float:
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

def check_daily_loss_limit(limit_amount: float, bot_id: int = None) -> bool:
    """
    Checks if the realized daily loss exceeds the limit.
    limit_amount: Positive float representing the max usage (e.g. 50.0 for $50 loss).
    Returns True if loss limit reached (i.e. PnL <= -limit).
    """
    if limit_amount <= 0:
        return False
        
    daily_pnl = get_daily_realized_pnl(bot_id)
    
    # Logic: If PnL is -60 and Limit is 50, then -60 <= -50 is True -> Stop.
    if daily_pnl <= -abs(limit_amount):
        return True
        
    return False

def check_drawdown_reduction(drawdown_pct: float, threshold_pct: float, reduction_factor: float = 0.5) -> dict:
    """
    Evaluates if a position should be reduced due to high drawdown.
    
    Args:
        drawdown_pct: Current unrealized drawdown (%) of the position.
        threshold_pct: Drawdown level to trigger reduction.
        reduction_factor: Amount to reduce (0.5 = 50%).
        
    Returns:
        dict: Action plan (e.g., {'action': 'reduce', 'factor': 0.5}) or None.
    """
    if threshold_pct <= 0 or drawdown_pct < threshold_pct:
        return None
        
    return {
        'action': 'reduce',
        'factor': reduction_factor,
        'reason': f"Drawdown {drawdown_pct:.2f}% > Limit {threshold_pct:.2f}%"
    }
