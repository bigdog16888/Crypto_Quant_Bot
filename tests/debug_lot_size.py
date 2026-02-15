
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.strategies.martingale_strategy import MartingaleStrategy
import pandas as pd

def test_calc():
    print("--- Debugging calculate_lot_size ---")
    
    # Mock Params
    params = {
        'base_size': 150.0,
        'martingale_multiplier': 2.0,
        'direction': 'LONG'
    }
    
    strategy = MartingaleStrategy(params)
    
    # Mock Precision Metadata (BTC/USDC usually 0.00001 or 0.001?)
    # Test case 1: Precision 3 (Default)
    strategy.set_precision_metadata({'qty_precision': 3, 'price_precision': 2, 'step_size': 0.001, 'tick_size': 0.01})
    
    price = 68828.00
    
    qty = strategy.calculate_lot_size(0, 10000, price)
    print(f"Price: {price}, Base Size: {params['base_size']}")
    print(f"Precision: 3 -> Calculated Qty: {qty}")
    
    expected_raw = 150.0 / 68828.00
    print(f"Raw Math: {expected_raw}")
    
    # Test case 2: Precision 0 (Edge case logic?)
    strategy.set_precision_metadata({'qty_precision': 0, 'price_precision': 2, 'step_size': 1, 'tick_size': 0.01})
    qty_0 = strategy.calculate_lot_size(0, 10000, price)
    print(f"Precision: 0 -> Calculated Qty: {qty_0}")

if __name__ == "__main__":
    test_calc()
