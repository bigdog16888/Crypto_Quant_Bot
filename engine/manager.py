from datetime import datetime
import math

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
        
    # Standard MQL4 Params
    start_hours = settings.get('EEStartHours', 0.0)
    hours_pc = settings.get('EEHoursPC', 0.0) # Percent per hour
    
    # Accelerated Params (Custom)
    interval_mins = settings.get('DecayIntervalMins', 60.0)
    decay_per_interval = settings.get('DecayPercentPerInterval', 0.0) / 100.0
    
    # Calculate duration
    duration_seconds = (current_time - basket_start_time).total_seconds()
    duration_hours = duration_seconds / 3600.0
    duration_mins = duration_seconds / 60.0
    
    ee_pc = 0.0
    
    # 1. Standard Time-based decay (MQL4 Style)
    if duration_hours > start_hours:
        ee_pc += (duration_hours - start_hours) * (hours_pc / 100.0)
        
    # 2. Accelerated Interval-based decay (User Style: 30% per 15 mins)
    if decay_per_interval > 0:
        intervals_passed = duration_mins / interval_mins
        ee_pc += intervals_passed * decay_per_interval
        
    # 3. Level-based decay
    start_level = settings.get('EEStartLevel', 5)
    level_pc = settings.get('EELevelPC', 0.0) / 100.0
    if total_orders >= start_level:
        ee_pc += (total_orders - start_level + 1) * level_pc
        
    # Calculate Decay Factor (1.0 = No Decay, 0.0 = Full Decay to BE)
    decay_factor = 1.0 - ee_pc
    
    allow_loss = settings.get('EEAllowLoss', False)
    if not allow_loss and decay_factor < 0:
        decay_factor = 0.0 # Floor at BreakEven
    
    # New TP is weighted average of InitialTP and BE
    # Logic: NewTP = BE + (InitialTP - BE) * DecayFactor
    adjusted_tp = break_even + (initial_tp - break_even) * decay_factor
    
    return adjusted_tp

def check_moving_profit_target(current_price: float, average_price: float, target_price: float, current_sl: float, direction: str, settings: dict) -> float:
    """
    Checks if the profit target has been reached to "lock in" profit.
    Equivalent to 'MaximizeProfit' logic in MQL4.
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

def check_hedge_entry(drawdown_percent: float, open_levels: int, settings: dict) -> dict | None:
    """
    Determines if a hedge trade should be opened based on drawdown or levels.
    """
    if not settings.get('UseHedge', False):
        return None
        
    hedge_start = settings.get('HedgeStart', 20.0)
    use_dd = settings.get('HedgeTypeDD', True)
    
    trigger = False
    if use_dd:
        if drawdown_percent >= hedge_start:
            trigger = True
    else:
        if open_levels >= int(hedge_start):
            trigger = True
            
    if not trigger:
        return None
        
    return {
        'action': 'open_hedge',
        'trigger_value': drawdown_percent if use_dd else open_levels,
        'size_mult': settings.get('LotMultHedge', 1.0)
    }

def calculate_hedge_lot(main_basket_lots: float, settings: dict) -> float:
    """
    Calculates the lot size for the hedge trade.
    """
    mult = settings.get('LotMultHedge', 1.0)
    return main_basket_lots * mult

def manage_trade(bot_id, bot_name, pair, direction, settings, trade_data, current_price, strategy, exchange_interface):
    """
    Core trade management logic called by the runner.
    trade_data: (bot_id, current_step, total_invested, avg_entry_price, target_tp_price)
    Returns: dict with actions taken (e.g. {'action': 'tp_hit'})
    """
    import logging
    from engine.database import update_martingale_step, reset_bot_after_tp
    logger = logging.getLogger("TradeManager")
    
    _, current_step, total_invested, avg_entry_price, target_tp_price = trade_data
    
    # 1. Check Take Profit
    tp_hit = False
    if direction == 'LONG':
        if current_price >= target_tp_price: tp_hit = True
    else:
        if current_price <= target_tp_price: tp_hit = True
        
    if tp_hit:
        logger.info(f"💰 Bot {bot_name} Profit Target Hit! Closing at {current_price}")
        # In real execution, we would market close here
        # For now, we reset the bot state
        reset_bot_after_tp(bot_id)
        return {'action': 'tp_hit'}

    # 2. Check Next Martingale Grid Order
    # Calculate next order price based on strategy
    # Note: strategy.calculate_next_grid_price() logic handles direction
    try:
        # We need market data for ATR grid if enabled
        # This is passed from the runner to the strategy usually
        # For now, we assume strategy has access or we pass simplified context
        # In runner, we'll ensure strategy.last_market_data is set
        next_order_price = strategy.calculate_next_grid_price(direction, current_price, avg_entry_price, current_step, strategy.last_market_data if hasattr(strategy, 'last_market_data') else None)
        
        grid_trigger = False
        if direction == 'LONG':
            if current_price <= next_order_price: grid_trigger = True
        else:
            if current_price >= next_order_price: grid_trigger = True
            
        if grid_trigger:
            next_step = current_step + 1
            added_investment = strategy.calculate_lot_size(next_step, 0) # Account balance currently unused
            
            logger.info(f"📥 Bot {bot_name} Triggering Grid Step {next_step} at {current_price} (Order Size: {added_investment})")
            
            # Update DB (Calculated new avg price and tp is simplified here)
            new_total = total_invested + added_investment
            # Avg Price = Price1*S1 + Price2*S2 / (S1+S2)
            new_avg = (avg_entry_price * total_invested + current_price * added_investment) / new_total
            
            # Simple TP logic (e.g. 1% profit) - in real scenario, strategy would provide this
            new_tp = new_avg * (1.01 if direction == 'LONG' else 0.99) 
            
            update_martingale_step(bot_id, next_step, added_investment, new_avg, new_tp)
            return {'action': 'grid_step', 'step': next_step}
            
    except Exception as e:
        logger.error(f"Error checking grid for {bot_name}: {e}")

    # 3. Check Hedge (Automated Hedge Executor)
    # Drawdown Calculation (approximate)
    drawdown_pc = 0.0
    if avg_entry_price > 0:
        if direction == 'LONG':
            drawdown_pc = (avg_entry_price - current_price) / avg_entry_price * 100
        else:
            drawdown_pc = (current_price - avg_entry_price) / avg_entry_price * 100

    hedge_trigger = check_hedge_entry(drawdown_pc, current_step, settings)
    if hedge_trigger:
        # EXECUTE HEDGE
        hedge_size = total_invested * hedge_trigger.get('size_mult', 1.0)
        logger.warning(f"🛡️ Bot {bot_name} AUTOMATED HEDGE TRIGGERED! Size: {hedge_size} at {current_price}")
        # In real execution: exchange_interface.create_order(...)
        return {'action': 'hedge_opened', 'size': hedge_size}

    return {'action': 'none'}

def emergency_close_all(exchange_interface, bots_in_trouble):
    """
    Kills all orders and closes all positions for specified bots.
    If bots_in_trouble is empty, it should close EVERYTHING.
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.warning("🚨 EMERGENCY CLOSE TRIGGERED 🚨")
    
    # 1. Fetch balance/positions to find what's open if bots_in_trouble not provided
    # For now, we assume we iterate through the DB's active bots
    for bot in bots_in_trouble:
        bot_id, pair = bot
        logger.info(f"Closing all for Bot {bot_id} on {pair}")
        
        # Cancel all open orders for this pair
        try:
            exchange_interface.exchange.cancel_all_orders(pair)
            logger.info(f"All orders canceled for {pair}")
        except Exception as e:
            logger.error(f"Failed to cancel orders for {pair}: {e}")
            
        # Market close position
        # This requires fetching current position size
        # TODO: Implement actual market liquidation logic
