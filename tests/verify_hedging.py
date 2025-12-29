import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

from engine.manager import check_moving_profit_target, check_hedge_entry
from config.settings import get_settings

def verify_moving_profit():
    print("\n--- Verifying Moving Profit Target (MaximizeProfit) ---")
    settings = get_settings()
    settings['MaximizeProfit'] = True
    settings['ProfitSet'] = 0.5
    
    # Scene 1: Buy Basket. BE=1.0000, TP=1.1000. Price rises to 1.0600.
    # Moving TP should be: BE + (TP-BE)*0.5 => 1.00 + 0.10*0.5 = 1.0500.
    # Price (1.06) > MovingTP (1.05) -> Trigger!
    be = 1.0000
    tp = 1.1000
    price = 1.0600
    current_sl = 0.0
    
    new_sl = check_moving_profit_target(price, be, tp, current_sl, 'buy', settings)
    print(f"Buy | BE: {be}, TP: {tp}, Price: {price} -> New SL: {new_sl:.4f} | Expected: 1.0500")
    
    # Scene 2: Sell Basket. BE=1.0000, TP=0.9000. Price drops to 0.9400.
    # Moving TP: BE + (TP-BE)*0.5 => 1.00 + (-0.10)*0.5 = 0.9500.
    # Price (0.94) < MovingTP (0.95) -> Trigger!
    tp_sell = 0.9000
    price_sell = 0.9400
    
    new_sl_sell = check_moving_profit_target(price_sell, be, tp_sell, current_sl, 'sell', settings)
    print(f"Sell| BE: {be}, TP: {tp_sell}, Price: {price_sell} -> New SL: {new_sl_sell:.4f} | Expected: 0.9500")
    
    # Scene 3: Not reached yet
    price_low = 1.0100
    new_sl_low = check_moving_profit_target(price_low, be, tp, current_sl, 'buy', settings)
    print(f"Buy Low| BE: {be}, TP: {tp}, Price: {price_low} -> New SL: {new_sl_low:.4f} | Expected: 0.0000")

def verify_hedging():
    print("\n--- Verifying Hedging Logic ---")
    settings = get_settings()
    settings['UseHedge'] = True
    settings['HedgeStart'] = 20.0 # 20%
    settings['HedgeTypeDD'] = True
    
    # Scene 1: Drawdown 15% (No trigger)
    action = check_hedge_entry(15.0, 5, settings)
    print(f"DD 15% -> Action: {action} | Expected: None")
    
    # Scene 2: Drawdown 25% (Trigger)
    action = check_hedge_entry(25.0, 10, settings)
    print(f"DD 25% -> Action: {action} | Expected: Action dict with trigger 25.0")
    
    if action and action['action'] == 'open_hedge':
        print("SUCCESS: Hedge trigger confirmed.")

if __name__ == "__main__":
    verify_moving_profit()
    verify_hedging()
