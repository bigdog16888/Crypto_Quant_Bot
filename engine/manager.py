from datetime import datetime
import math
import logging

from engine.database import get_bot_order_ids
from engine.exchange_interface import normalize_symbol


def calculate_early_exit_decay(
    basket_start_time: datetime,
    current_time: datetime,
    total_orders: int,
    initial_tp: float,
    break_even: float,
    settings: dict
) -> float:
    """
    Calculates the adjusted Take Profit price based on Early Exit decay logic.
    User Requirement: "reduce takeprofit by 30% each 15 minutes untill brake even"
    """
    if not settings.get('UseEarlyExit', False):
        return initial_tp
        
    # Accelerated Params (Step Decay)
    interval_mins = settings.get('DecayIntervalMins', 60.0)
    decay_per_interval = settings.get('DecayPercentPerInterval', 0.0) / 100.0
    
    # Calculate duration
    duration_seconds = (current_time - basket_start_time).total_seconds()
    duration_hours = duration_seconds / 3600.0
    duration_mins = duration_seconds / 60.0
    
    ee_pc = 0.0
    

    # 2. Accelerated Interval-based decay (User Style: 30% per 15 mins)
    if decay_per_interval > 0:
        intervals_passed = math.floor(duration_mins / interval_mins)
        ee_pc += intervals_passed * decay_per_interval
        

    # Calculate Decay Factor (1.0 = No Decay, 0.0 = Full Decay to BE)
    decay_factor = 1.0 - ee_pc
    
    allow_loss = str(settings.get('EEAllowLoss', 'False')).lower() == 'true'
    if not allow_loss:
        # Cap decay_factor at 0.0 (Break-Even) if losses are not allowed
        if decay_factor < 0.0:
            decay_factor = 0.0
    else:
        # If losses are allowed, decay_factor can go below 0,
        # moving the TP past break-even into a loss.
        # But we shouldn't let it decay infinitely.
        # Let's cap the maximum loss at the Initial TP distance.
        # i.e. Decay factor goes from 1.0 (TP) -> 0.0 (BE) -> -1.0 (Loss equal to TP distance)
        max_decay = -1.0 
        if decay_factor < max_decay:
            decay_factor = max_decay
    
    # New TP is weighted average of InitialTP and BE
    # Logic: NewTP = BE + (InitialTP - BE) * DecayFactor
    adjusted_tp = break_even + (initial_tp - break_even) * decay_factor
    
    return adjusted_tp

def check_moving_profit_target(current_price: float, average_price: float, target_price: float, current_sl: float, direction: str, settings: dict) -> float:
    """
    Checks if the profit target has been reached to "lock in" profit.
    Equivalent to 'MaximizeProfit' logic.

    """
    if not settings.get('MaximizeProfit', False):
        return 0.0

    profit_set = settings.get('ProfitSet', 0.5)
    
    # Calculate the Moving Profit Target Price (TPbMP)
    # Logic: BE + (TP - BE) * ProfitSet
    # Example: BE=1.00, TP=1.10, Set=0.5 -> 1.05.
    moving_target = average_price + (target_price - average_price) * profit_set
    
    new_sl = 0.0
    
    if direction == 'buy':
        # If Price > MovingTarget, move SL to MovingTarget
        if current_price > moving_target:
            if current_sl == 0 or current_sl < moving_target:
                new_sl = moving_target
    elif direction == 'sell':
        # For Sell, TP is lower than BE.
        if current_price < moving_target:
            if current_sl == 0 or current_sl > moving_target:
                new_sl = moving_target
                
    return new_sl

def check_hedge_entry(current_step: int, settings: dict) -> dict | None:
    """
    Determines if a hedge lock order should be placed.
    Triggers when the bot reaches HedgeStartStep (step-based, not drawdown%).
    Returns a mission dict or None.
    """
    if not settings.get('UseHedge', False):
        return None
    hedge_step = int(settings.get('HedgeStartStep', 7))
    if current_step >= hedge_step:
        return {'action': 'open_hedge', 'trigger_step': current_step}
    return None

def calculate_hedge_lot(main_basket_lots: float, settings: dict) -> float:
    """
    Calculates the lot size for the hedge trade.
    """
    mult = settings.get('LotMultHedge', 1.0)
    return main_basket_lots * mult

def _check_trailing_stop(bot_id, bot_name, direction, current_price, target_tp_price, avg_entry_price, settings, logger):
    """
    Evaluates Trailing Profit conditions.
    Returns: (tp_hit: bool, is_trailing_exit: bool)
    """
    tp_hit = False
    is_trailing_exit = False
    trail_percent = float(settings.get('ProfitSet', 0.5))

    if direction == 'LONG':
        stored_peak = float(settings.get('trailing_peak', 0.0))
        peak_price = max(stored_peak, current_price, target_tp_price)
        
        if peak_price > stored_peak and peak_price > target_tp_price:
            try:
                from engine.database import update_bot_config_value
                update_bot_config_value(bot_id, 'trailing_peak', peak_price)
                logger.info(f"📈 Trailing Peak for {bot_name} updated to {peak_price}")
            except Exception as e:
                logger.error(f"Failed to update trailing peak: {e}")
        
        stop_price = target_tp_price + (peak_price - target_tp_price) * trail_percent
        
        if current_price <= stop_price and current_price >= avg_entry_price:
             tp_hit = True
             is_trailing_exit = True
             logger.info(f"Trailing Stop Hit for {bot_name}: Price {current_price} <= Stop {stop_price} (Peak {peak_price})")

    elif direction == 'SHORT':
         stored_peak_s = float(settings.get('trailing_peak', 99999999.0))
         if stored_peak_s == 0.0: stored_peak_s = 99999999.0
         
         peak_price = min(stored_peak_s, current_price, target_tp_price)
         
         if peak_price < stored_peak_s and peak_price < target_tp_price:
             try:
                from engine.database import update_bot_config_value
                update_bot_config_value(bot_id, 'trailing_peak', peak_price)
                logger.info(f"📉 Trailing Peak for {bot_name} updated to {peak_price}")
             except: pass
         
         stop_price = target_tp_price - (target_tp_price - peak_price) * trail_percent
         
         if current_price >= stop_price and current_price <= avg_entry_price:
             tp_hit = True
             is_trailing_exit = True
             logger.info(f"Trailing Stop Hit for {bot_name}: Price {current_price} >= Stop {stop_price} (Peak {peak_price})")
             
    return tp_hit, is_trailing_exit

def _calculate_expected_tp(avg_entry_price, direction, total_invested, settings):
    """Recalculates what TP price SHOULD be based on current settings."""
    tp_type = settings.get('TakeProfitType', 'USD')
    if tp_type == 'Percent':
        tp_pct = settings.get('TakeProfitPct', 1.0) / 100.0
        return avg_entry_price * (1.0 + tp_pct) if direction == 'LONG' else avg_entry_price * (1.0 - tp_pct)
    
    target_usd = settings.get('TakeProfitBase', 10.0)
    est_qty = total_invested / avg_entry_price if avg_entry_price > 0 else 0
    if est_qty > 0:
        dist = target_usd / est_qty
        return avg_entry_price + dist if direction == 'LONG' else avg_entry_price - dist
    return 0.0

def _find_bot_orders(bot_id, pair, direction, open_orders, target_tp_price, settings, logger):
    """
    Identifies THIS bot's TP and Grid orders on the exchange.
    Uses Phase 7 Tags (clientOrderId) as the source of truth, falls back to DB IDs and Price.
    """
    from engine.strategies.martingale_strategy import MartingaleStrategy
    from engine.risk_manager import check_drawdown_reduction
    from engine.bot_management import check_and_execute_stops
    
    bot_order_ids = get_bot_order_ids(bot_id)
    db_tp_id = bot_order_ids.get('tp_order_id')
    db_grid_ids = [o.get('order_id') for o in bot_order_ids.get('grid_orders', []) if o.get('status') == 'open']

    has_my_tp = False
    my_tp_order = None
    has_my_grid = False
    my_grid_ids = []

    # TAG PREFIX: CQB_{bot_id}_
    tag_prefix = f"CQB_{bot_id}_"

    for o in open_orders:
        oid = o.get('id')
        client_oid = o.get('clientOrderId', '')
        
        # 1. PRIMARY: Match by Tag (Phase 7)
        if client_oid.startswith(tag_prefix):
            if '_TP_' in client_oid:
                has_my_tp = True
                my_tp_order = o
            elif '_GRID_' in client_oid:
                has_my_grid = True
                my_grid_ids.append(oid)
            elif '_ENTRY_' in client_oid:
                # Entries don't block maintenance (they lead to it)
                pass
            continue

        # 2. SECONDARY: Match by DB ID
        if oid == db_tp_id:
            has_my_tp = True
            my_tp_order = o
        elif oid in db_grid_ids:
            has_my_grid = True
            my_grid_ids.append(oid)

    # 3. TERTIARY: Match by Price/Side (Legacy Phase 6 Support)
    if not has_my_tp and not settings.get('MaximizeProfit', False) and target_tp_price > 0:
        tp_side = 'sell' if direction == 'LONG' else 'buy'
        for o in open_orders:
            # Skip if already identified or owned by another bot (has a CQB tag)
            if o.get('clientOrderId', '').startswith('CQB_'): continue
            
            if (normalize_symbol(o.get('symbol')) == normalize_symbol(pair) and 
                o.get('side') == tp_side and 
                abs(float(o.get('price') or 0) - target_tp_price) / target_tp_price < 0.001):
                has_my_tp = True
                my_tp_order = o
                break
                
    return has_my_tp, my_tp_order, has_my_grid, my_grid_ids

def _detect_config_drift(bot_name, direction, avg_entry_price, total_invested, has_my_tp, my_tp_order, has_my_grid, my_grid_ids, open_orders, settings, logger):
    """Phase 6: Detects if exchange orders match current bot config."""
    config_changed = False
    reasons = []

    # TP Drift Check
    if has_my_tp and my_tp_order and not settings.get('MaximizeProfit', False):
        current_tp_price = float(my_tp_order.get('price', 0) or 0)
        expected_tp = _calculate_expected_tp(avg_entry_price, direction, total_invested, settings)
        
        if expected_tp > 0 and current_tp_price > 0:
            drift = abs(expected_tp - current_tp_price) / expected_tp * 100
            if drift > 0.1:
                config_changed = True
                reasons.append(f"TP {current_tp_price:.2f}→{expected_tp:.2f}")

    # Grid Drift Check
    if has_my_grid and my_grid_ids:
        my_grid_order = next((o for o in open_orders if o.get('id') in my_grid_ids), None)
        if my_grid_order:
            current_grid_price = float(my_grid_order.get('price', 0) or 0)
            spacing = float(settings.get('GridSpacing', 1.0)) / 100.0
            expected_grid = avg_entry_price * (1.0 + spacing) if direction != 'LONG' else avg_entry_price * (1.0 - spacing)
            
            if expected_grid > 0 and current_grid_price > 0:
                drift = abs(expected_grid - current_grid_price) / expected_grid * 100
                if drift > 0.5:
                    config_changed = True
                    reasons.append(f"Grid {current_grid_price:.2f}→{expected_grid:.2f}")

    if config_changed:
        logger.info(f"📝 Config Change Detected for {bot_name}: {', '.join(reasons)}")
    
    return config_changed

def _perform_tp_self_healing(bot_id, bot_name, direction, avg_entry_price, total_invested, current_step, target_tp_price, settings, logger):
    """Restores Target TP if it becomes 0.00 (Self-Healing)."""
    if current_step >= 0 and target_tp_price <= 0 and not settings.get('MaximizeProfit', False):
        try:
            logger.warning(f"🚑 Self-Healing: Restored Target TP for {bot_name}...")
            healed_tp = _calculate_expected_tp(avg_entry_price, direction, total_invested, settings)
            if healed_tp > 0:
                from engine.database import update_trade_tp_price
                update_trade_tp_price(bot_id, healed_tp)
                return healed_tp
        except Exception as e:
            logger.error(f"Self-Healing Failed: {e}")
    return target_tp_price




def emergency_close_all(exchange_interface, bots_in_trouble):
    """
    Kills all orders and closes all positions for specified bots.
    If bots_in_trouble is empty, it should close EVERYTHING.
    """
    logger = logging.getLogger(__name__)
    logger.warning("🚨 EMERGENCY CLOSE TRIGGERED 🚨")
    
    # 1. Fetch balance/positions to find what's open if bots_in_trouble not provided
    # For now, we assume we iterate through the DB's active bots
    for bot in bots_in_trouble:
        bot_id, pair = bot
        logger.info(f"Closing all for Bot {bot_id} on {pair}")
        
        # Cancel all open orders for this bot on this pair
        try:
            exchange_interface.cancel_orders_by_bot_id(bot_id, pair)
            logger.info(f"Orders for Bot {bot_id} canceled for {pair}")
        except Exception as e:
            logger.error(f"Failed to cancel orders for {pair}: {e}")
            
        # Market close position
        # This requires fetching current position size
        # TODO: Implement actual market liquidation logic
