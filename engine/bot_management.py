"""
Bot Position Management API

Provides functions to:
- Close bot positions (full or partial)
- Update close settings (stop after PnL, stop after time)
- Check stop conditions
- Manage bot state independently
"""
import logging
import time
from typing import Dict, Any, Optional, List
from .database import (
    close_bot_position, get_bot_close_settings, update_bot_close_settings,
    check_stop_after_conditions, get_bot_status, get_bot_params,
    log_trade, get_connection
)
from .exchange_interface import ExchangeInterface

logger = logging.getLogger("BotManagement")


def close_position(bot_id: int, close_pct: float = 100.0, reason: str = "Manual Close", exchange_interface=None, order_type='limit') -> Dict[str, Any]:
    """
    Close a bot's position (partial or full) on the exchange.
    
    Args:
        bot_id: The bot to close
        close_pct: Percentage of position to close (100 = full close)
        reason: Reason for close
        exchange_interface: Optional ExchangeInterface instance to reuse
        order_type: 'limit' (Post-Only) or 'market' (Panic)
    
    Returns:
        dict with success status and details
    """
    # Get bot info
    params = get_bot_params(bot_id)
    if not params:
        return {'success': False, 'error': 'Bot not found'}
    
    name, pair, direction = params[0], params[1], params[2]
    config_json = params[7]
    config_dict = json.loads(config_json) if isinstance(config_json, str) else (config_json or {})
    market_type = config_dict.get('market_type', 'future')
    
    # Get current trade state
    status = get_bot_status(bot_id)
    if not status:
        return {'success': False, 'error': 'Could not get bot status'}
    
    total_invested = status['total_invested']
    if total_invested <= 0:
        return {'success': False, 'error': 'No position to close'}
    
    # Get exchange and current price
    exchange = exchange_interface or ExchangeInterface(market_type=market_type)
    current_price = exchange.get_last_price(pair)
    
    if current_price == 0:
        return {'success': False, 'error': 'Could not get current price'}
    
    # Calculate close amount
    close_amount = total_invested * (close_pct / 100.0)
    
    # For futures: calculate quantity
    if market_type == 'future':
        close_qty = close_amount / current_price
    else:
        # Spot: close the asset
        base_asset = pair.split('/')[0]
        balance = exchange.fetch_balance()
        if not balance or not isinstance(balance, dict):
            return {'success': False, 'error': 'Could not fetch balance'}
        base_info = balance.get(base_asset, {})
        current_holdings = float(base_info.get('total', 0))
        close_qty = current_holdings * (close_pct / 100.0)
    
    if close_qty <= 0:
        return {'success': False, 'error': 'Invalid close quantity'}
    
    # Determine close side (opposite of position direction)
    close_side = 'sell' if direction == 'LONG' else 'buy'
    
    # Execute order to close
    try:
        mode_label = "LIMIT (Post-Only)" if order_type == 'limit' else "MARKET (Panic)"
        logger.info(f"🔴 {mode_label} Closing {close_pct:.0f}% of {name}'s position: {close_qty:.6f} {pair}")
        
        # Professional Order Routing
        close_order = exchange.create_order(
            symbol=pair, 
            type=order_type, 
            side=close_side, 
            amount=close_qty,
            price=current_price if order_type == 'limit' else None,
            params={'reduceOnly': True},
            post_only=(order_type == 'limit')
        )
        
        if not close_order or close_order.get('status') == 'rejected':
            return {'success': False, 'error': f'Close order rejected: {close_order}'}
        
        # Get fill price (market order might not have immediate price in response, use current as fallback)
        fill_price = float(close_order.get('price', current_price) or current_price)
        filled_qty = float(close_order.get('filled', close_qty) or close_qty)
        
        logger.info(f"✅ Close order placed/filled: {filled_qty} @ {fill_price}")
        
    except Exception as e:
        logger.error(f"❌ Failed to execute close order for {name}: {e}")
        return {'success': False, 'error': str(e)}
    
    # Now update database to reflect the close
    from .database import close_bot_position
    result = close_bot_position(
        bot_id=bot_id,
        close_type='MANUAL',
        close_price=fill_price,
        close_pct=close_pct,
        notes=f"{reason}: {close_pct:.0f}%"
    )
    
    result['exchange_order'] = close_order
    result['fill_price'] = fill_price
    result['filled_qty'] = filled_qty
    
    return result


def partial_close(bot_id: int, pct: float, reason: str = "Partial Close") -> Dict[str, Any]:
    """
    Partially close a bot's position.
    
    Args:
        bot_id: The bot to partially close
        pct: Percentage to close (e.g., 50 = close 50%)
        reason: Reason for partial close
    
    Returns:
        dict with success status and details
    """
    if pct <= 0 or pct >= 100:
        return {'success': False, 'error': 'Percentage must be between 0 and 100'}
    
    return close_position(bot_id, close_pct=pct, reason=reason)


def set_stop_after_pnl(bot_id: int, pnl_target: float) -> bool:
    """
    Set a PnL target at which the bot will automatically close.
    
    Args:
        bot_id: The bot to update
        pnl_target: PnL target in USD (0 to disable)
    
    Returns:
        success status
    """
    return update_bot_close_settings(bot_id, stop_after_pnl=pnl_target)


def set_stop_after_time(bot_id: int, hours: float) -> bool:
    """
    Set a time limit after which the bot will automatically close.
    
    Args:
        bot_id: The bot to update
        hours: Hours in trade before auto-close (0 to disable)
    
    Returns:
        success status
    """
    return update_bot_close_settings(bot_id, stop_after_time=hours)


def set_manual_close_pct(bot_id: int, pct: float) -> bool:
    """
    Set the default percentage for manual close.
    
    Args:
        bot_id: The bot to update
        pct: Percentage to close (100 = full close)
    
    Returns:
        success status
    """
    return update_bot_close_settings(bot_id, manual_close_pct=pct)





def check_and_execute_stops(bot_id: int, exchange_interface=None) -> Optional[Dict[str, Any]]:
    """
    Check if any stop conditions are met and execute close if so.
    Call this at the start of each cycle.
    
    Args:
        bot_id: The bot to check
        exchange_interface: Optional ExchangeInterface instance to reuse
    
    Returns:
        dict with result if stop was executed, None otherwise
    """
    # Get trade state
    status = get_bot_status(bot_id)
    if not status:
        return None
    
    # status: dict with keys 'name', 'pair', 'current_step', 'total_invested', 'avg_entry_price', ...
    name = status['name']
    pair = status['pair']
    step = status['current_step']
    total_invested = status['total_invested']
    avg_entry_price = status['avg_entry_price']
    
    if total_invested <= 0:
        return None  # No position, no stops to check
    
    # Get current price for PnL calculation
    params = get_bot_params(bot_id)
    if not params:
        return None
    
    config_json = params[7]
    config_dict = config_json if isinstance(config_json, dict) else {}
    market_type = config_dict.get('market_type', 'future')
    
    exchange = exchange_interface or ExchangeInterface(market_type=market_type)
    current_price = exchange.get_last_price(pair)
    
    if current_price == 0:
        return None
    
    # Calculate current PnL
    direction = params[2]
    invested = total_invested
    entry = avg_entry_price
    
    if direction == 'LONG':
        current_pnl = (current_price - entry) / entry * invested
    else:  # SHORT
        current_pnl = (entry - current_price) / entry * invested
    
    # Calculate hours in trade
    import time
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT basket_start_time FROM trades WHERE bot_id = ?', (bot_id,))
    result = cursor.fetchone()
    conn.close()
    
    basket_start = result[0] if result else 0
    hours_in_trade = (time.time() - basket_start) / 3600 if basket_start > 0 else 0
    
    # Check conditions
    stop_result = check_stop_after_conditions(bot_id, current_pnl, hours_in_trade)
    
    if stop_result and stop_result.get('triggered'):
        logger.info(f"🚨 Stop condition triggered for {name}: {stop_result.get('conditions')}")
        
        conditions = stop_result.get('conditions', [])
        if not conditions:
            return None
        
        first_condition = conditions[0]
        
        # Execute close
        close_result = close_position(
            bot_id=bot_id,
            close_pct=100.0,
            reason=first_condition.get('type', 'UNKNOWN'),
            exchange_interface=exchange
        )
        
        return {
            'action': 'stop_executed',
            'bot_id': bot_id,
            'bot_name': name,
            'reason': first_condition.get('type', 'UNKNOWN'),
            'details': first_condition.get('reason', ''),
            'pnl_at_close': current_pnl,
            'close_result': close_result
        }
    
    return None


def get_position_summary(bot_id: int) -> Dict[str, Any]:
    """
    Get a summary of a bot's current position state.
    
    Args:
        bot_id: The bot to query
    
    Returns:
        dict with position details
    """
    status = get_bot_status(bot_id)
    if not status:
        return {'error': 'Bot not found'}
    
    params = get_bot_params(bot_id)
    if not params:
        return {'error': 'Could not get bot params'}
    
    config_json = params[7]
    config_dict = config_json if isinstance(config_json, dict) else {}
    market_type = config_dict.get('market_type', 'future')
    
    exchange = ExchangeInterface(market_type=market_type)
    current_price = exchange.get_last_price(pair) if (pair := status['pair']) else 0
    
    # status: dict
    name = status['name']
    pair = status['pair']
    step = status['current_step']
    total_invested = status['total_invested']
    avg_entry_price = status['avg_entry_price']
    target_tp = status['target_tp_price']
    
    direction = params[2]
    
    # Calculate PnL
    unrealized_pnl = 0
    pnl_pct = 0
    if total_invested > 0 and avg_entry_price > 0 and current_price > 0:
        if direction == 'LONG':
            unrealized_pnl = (current_price - avg_entry_price) / avg_entry_price * total_invested
        else:
            unrealized_pnl = (avg_entry_price - current_price) / avg_entry_price * total_invested
        pnl_pct = unrealized_pnl / total_invested * 100
    
    # Get close settings
    close_settings = get_bot_close_settings(bot_id)
    
    return {
        'bot_id': bot_id,
        'bot_name': name,
        'pair': pair,
        'direction': direction,
        'in_trade': total_invested > 0,
        'current_step': step,
        'total_invested': total_invested,
        'avg_entry_price': avg_entry_price,
        'target_tp_price': target_tp,
        'current_price': current_price,
        'unrealized_pnl': unrealized_pnl,
        'pnl_pct': pnl_pct,
        'close_settings': close_settings
    }


def get_all_positions_summary() -> List[Dict[str, Any]]:
    """
    Get position summary for all active bots.
    
    Returns:
        list of position summaries
    """
    from .database import get_all_bots
    
    bots = get_all_bots()
    summaries = []
    
    for bot in bots:
        bot_id = bot[0]
        summary = get_position_summary(bot_id)
        if 'error' not in summary:
            summaries.append(summary)
    
    return summaries


if __name__ == "__main__":
    # Test
    import json
    
    print("Bot Position Management API")
    print("=" * 40)
    
    # Get all positions
    positions = get_all_positions_summary()
    print(f"\nActive positions: {len(positions)}")
    
    for p in positions:
        print(f"\n{p['bot_name']} ({p['pair']}):")
        print(f"  In Trade: {p['in_trade']}")
        if p['in_trade']:
            print(f"  Invested: ${p['total_invested']:.2f}")
            print(f"  Entry: ${p['avg_entry_price']:.4f}")
            print(f"  Current: ${p['current_price']:.4f}")
            print(f"  PnL: ${p['unrealized_pnl']:.2f} ({p['pnl_pct']:.2f}%)")
        print(f"  Close Settings: {json.dumps(p['close_settings'], indent=4)}")
