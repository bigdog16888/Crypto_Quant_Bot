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
    MQL4 Logic:
      Decay occurs based on TIME (hours passed) and DEPTH (number of levels).
      EEpc = (Hours - StartHours) * EEHoursPC + (Levels - StartLevel) * EELevelPC
      NewTP = InitialTP * (1 - EEpc) + BreakEven * EEpc (Linear interpolation towards BE)
    """
    if not settings.get('UseEarlyExit', False):
        return initial_tp
        
    start_hours = settings.get('EEStartHours', 3.0)
    hours_pc = settings.get('EEHoursPC', 0.5) / 100.0 
    start_level = settings.get('EEStartLevel', 5)
    level_pc = settings.get('EELevelPC', 10.0) / 100.0
    allow_loss = settings.get('EEAllowLoss', False)
    
    # Calculate duration in hours
    duration_seconds = (current_time - basket_start_time).total_seconds()
    duration_hours = duration_seconds / 3600.0
    
    ee_pc = 0.0
    
    # Time-based decay
    if duration_hours > start_hours:
        ee_pc += (duration_hours - start_hours) * hours_pc
        
    # Level-based decay
    if total_orders >= start_level:
        ee_pc += (total_orders - start_level + 1) * level_pc
        
    # Calculate Decay Factor (1.0 = No Decay, 0.0 = Full Decay to BE)
    decay_factor = 1.0 - ee_pc
    
    if not allow_loss and decay_factor < 0:
        decay_factor = 0.0 # Floor at BreakEven
    
    # New TP is weighted average of InitialTP and BE
    adjusted_tp = (initial_tp - break_even) * decay_factor + break_even
    
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
