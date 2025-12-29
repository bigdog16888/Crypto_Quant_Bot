import math

def calculate_next_trade_size(current_lots: float, multiplier: float, lot_step: float, lot_decimal: int) -> float:
    """
    Calculates the next trade size using Martingale logic.
    Logic from MQL4:
    Lots[Index] = ND(MathMax(Lots[Index - 1] * Multiplier_, Lots[Index - 1] + LotStep), LotDecimal);
    """
    next_lots_mult = current_lots * multiplier
    next_lots_step = current_lots + lot_step
    
    # Use standard round if lot_decimal is 0 (standard lots usually 2, but logic allows 0)
    # Python round() rounds to nearest even number for .5 cases, MQL4 NormalizeDouble uses standard rounding.
    # For safety in finance, we often want explicit control, but replicating MQL4 ND behavior:
    next_lots = max(next_lots_mult, next_lots_step)
    
    if lot_decimal == 0:
        return float(round(next_lots))
    else:
        return float(round(next_lots, lot_decimal))

def calculate_break_even_price(orders: list) -> float:
    """
    Calculates the new break-even price for a basket of orders.
    MQL4 Logic:
    BEb = ND(BEb / LbT, Digits);
    Where BEb initially is Sum(OrderLots * OrderOpenPrice)
    and LbT is TotalLots.
    
    orders: list of dicts with keys 'lots', 'open_price', 'type' (BUY/SELL)
            Assumes all orders in list are of same direction for simple BE calc.
    """
    total_lots = 0.0
    weighted_sum = 0.0
    
    for order in orders:
        lots = order['lots']
        price = order['open_price']
        total_lots += lots
        weighted_sum += (lots * price)
        
    if total_lots == 0:
        return 0.0
        
    break_even = weighted_sum / total_lots
    return break_even

def calculate_profit_target(break_even_price: float, total_lots: float, target_profit_currency: float, pip_value: float) -> float:
    """
    Calculate TP price based on a currency profit target (optional utility).
    """
    if total_lots == 0 or pip_value == 0:
        return 0.0
    
    pips_needed = target_profit_currency / pip_value / total_lots
    # Assuming BUY for now, logic differs for SELL (-pips)
    return break_even_price + (pips_needed * 0.0001)

def calculate_grid_spacing_atr(atr_value: float, total_orders: int, settings: dict) -> tuple[float, float]:
    """
    Calculates the Grid Distance and Grid TakeProfit based on ATR and Martingale level.
    
    Logic ported from MQL4 (AutoCal block):
       If Level > Set4: Grid = ATR * 12, TP = ATR * 18
       If Level > Set3: Grid = ATR * 8,  TP = ATR * 12
       If Level > Set2: Grid = ATR * 4,  TP = ATR * 8
       If Level > Set1: Grid = ATR * 2,  TP = ATR * 4
       Else:            Grid = ATR * 1,  TP = ATR * 2
       
    Args:
        atr_value: Current ATR value (in PRICE units, e.g., 0.0020 for 20 pips).
        total_orders: Current number of open orders (or next order index).
        settings: Dict containing 'SetLevels' [l1, l2, l3, l4] and 'GAF' (Grid Adjust Factor).
        
    Returns:
        (grid_dist, tp_dist) in PRICE units.
    """
    # Default levels if not provided
    set_levels = settings.get('SetLevels', [4, 8, 12, 16]) # Corresponds to Set1Level...Set4Level
    gaf = settings.get('GAF', 1.0) # Grid Adjustment Factor
    
    s1, s2, s3, s4 = set_levels[0], set_levels[1], set_levels[2], set_levels[3]
    
    g_mult = 1.0
    tp_mult = 2.0
    
    if total_orders >= s4 and s4 > 0:
        g_mult = 12.0
        tp_mult = 18.0
    elif total_orders >= s3 and s3 > 0:
        g_mult = 8.0
        tp_mult = 12.0
    elif total_orders >= s2 and s2 > 0:
        g_mult = 4.0
        tp_mult = 8.0
    elif total_orders >= s1 and s1 > 0:
        g_mult = 2.0
        tp_mult = 4.0
    else:
        g_mult = 1.0
        tp_mult = 2.0
        
    # Apply Grid Adjustment Factor (GAF)
    grid_dist = atr_value * g_mult * gaf
    tp_dist = atr_value * tp_mult * gaf
    
    return grid_dist, tp_dist
