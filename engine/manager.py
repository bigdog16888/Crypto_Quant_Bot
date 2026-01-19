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
    trade_data: (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time, basket_start_time)
    Returns: dict with missions for the runner to execute (e.g. {'action': 'TP', 'price': ...})
    """
    import logging
    logger = logging.getLogger("TradeManager")

    # Safety unpack (handle cases where tuple length varies during migration)
    if len(trade_data) >= 8:
        # Compatibility unpacking
        # (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time, *basket_start)
        _, _, current_step, total_invested, avg_entry_price, target_tp_price, _, _ = trade_data[:8]
    else:
        return {'action': 'none'}
    
    # 1. Check Take Profit
    # Early Exit Logic Injection
    # trade_data unpack: (name, pair, current_step, total_invested, avg_entry_price, target_tp_price, last_exit_price, last_exit_time, basket_start_time)
    
    effective_tp = target_tp_price
    
    if settings.get('UseEarlyExit', False) and len(trade_data) > 8:
        basket_start_time = trade_data[8]
        if basket_start_time > 0:
            import time
            from datetime import datetime
            
            start_dt = datetime.fromtimestamp(basket_start_time)
            now_dt = datetime.fromtimestamp(time.time())
            
            effective_tp = calculate_early_exit_decay(
                basket_start_time=start_dt,
                current_time=now_dt,
                total_orders=current_step + 1,
                initial_tp=target_tp_price,
                break_even=avg_entry_price,
                settings=settings
            )
            
            # Log decay if significant
            if effective_tp < target_tp_price * 0.999:
                logger.debug(f"EE: {bot_name} TP decayed from {target_tp_price} to {effective_tp}")

    tp_hit = False
    if direction == 'LONG':
        if current_price >= effective_tp: tp_hit = True
    else:
        if current_price <= effective_tp: tp_hit = True
        
    if tp_hit:
        logger.info(f"💰 Bot {bot_name} Profit Target Hit! Signal to close at {current_price}")
        est_qty = total_invested / avg_entry_price if avg_entry_price > 0 else 0
        if direction == 'LONG':
            pnl = (current_price - avg_entry_price) * est_qty
        else:
            pnl = (avg_entry_price - current_price) * est_qty
        
        # MISSION: CLOSE POSITION
        return {
            'action': 'tp_hit',
            'bot_id': bot_id,
            'bot_name': bot_name,
            'pair': pair,
            'direction': direction,
            'exit_price': current_price,
            'qty': est_qty,
            'pnl': pnl,
            'current_step': current_step,
            'avg_entry_price': avg_entry_price,
            'total_invested': total_invested
        }

    # 2. Check Next Martingale Grid Order
    # Ensure this block runs even if max steps reached to MAINTAIN TP ORDER
    max_steps = int(settings.get('max_steps', 10))
    grid_mission_active = current_step < max_steps

    try:
        if grid_mission_active:
            next_order_price = strategy.calculate_next_grid_price(direction, current_price, avg_entry_price, current_step, strategy.last_market_data if hasattr(strategy, 'last_market_data') else None)
            
            # Calculate Grid Order Specs
            next_step = current_step + 1
            added_investment = strategy.calculate_lot_size(next_step, 0)
            
            # Recalculate Hypothetical Average & TP if this grid hits
            new_total = total_invested + added_investment
            new_avg = (avg_entry_price * total_invested + next_order_price * added_investment) / new_total
            
            # Pending Grid Order Data
            grid_price = next_order_price
            grid_qty = added_investment / next_order_price if next_order_price > 0 else 0
            grid_step = next_step
            grid_amount_usd = added_investment
        else:
            # Max steps reached, no grid order
            next_order_price = 0
            new_avg = avg_entry_price # No change
            
            # Empty Grid Data
            grid_price = None
            grid_qty = 0
            grid_step = 0
            grid_amount_usd = 0

        # Calculate TP (Always needed)
        tp_type = settings.get('TakeProfitType', 'USD')
        if tp_type == 'Percent':
            tp_pct = settings.get('TakeProfitPct', 1.0) / 100.0
            new_tp = new_avg * (1.0 + tp_pct) if direction == 'LONG' else new_avg * (1.0 - tp_pct)
        else:
            target_usd = settings.get('TakeProfitBase', 10.0)
            est_qty = total_invested / avg_entry_price if avg_entry_price > 0 else 0
            if grid_mission_active:
                 est_qty = new_total / new_avg # Use hypothetical qty if calculating future TP
            
            if est_qty > 0:
                dist = target_usd / est_qty
                new_tp = new_avg + dist if direction == 'LONG' else new_avg - dist
            else:
                new_tp = 0

        # Return a "sync" mission that tells the runner to maintain these orders
        return {
            'action': 'maintain_orders',
            'bot_id': bot_id,
            'bot_name': bot_name,
            'pair': pair,
            'direction': direction,
            'current_price': current_price,
            # Pending Grid Order (None if max steps)
            'grid_price': grid_price,
            'grid_step': grid_step,
            'grid_amount_usd': grid_amount_usd,
            'grid_qty': grid_qty,
            # Pending TP Order (Current TP for open position)
            'tp_price': effective_tp, # Dynamic/Decayed TP
            'tp_qty': total_invested / avg_entry_price if avg_entry_price > 0 else 0,
            # Future Data (for DB update if grid hits)
            'future_avg': new_avg,
            'future_tp': new_tp
        }
            
    except Exception as e:
        logger.error(f"Error checking grid for {bot_name}: {e}")

    # 3. Check Hedge
    drawdown_pc = 0.0
    if avg_entry_price > 0:
        if direction == 'LONG':
            drawdown_pc = (avg_entry_price - current_price) / avg_entry_price * 100
        else:
            drawdown_pc = (current_price - avg_entry_price) / avg_entry_price * 100

    hedge_trigger = check_hedge_entry(drawdown_pc, current_step, settings)
    if hedge_trigger:
        hedge_size = total_invested * hedge_trigger.get('size_mult', 1.0)
        logger.warning(f"🛡️ Bot {bot_name} - HEDGE TRIGGERED! Size: ${hedge_size} at {current_price}")
        
        # MISSION: OPEN HEDGE
        return {
            'action': 'hedge_open',
            'bot_id': bot_id,
            'bot_name': bot_name,
            'pair': pair,
            'direction': direction,
            'price': current_price,
            'amount_usd': hedge_size,
            'qty': hedge_size / current_price if current_price > 0 else 0,
            'step': current_step,
            'drawdown_pct': drawdown_pc
        }

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
