import random
import pandas as pd
import sys
import os
import logging
import numpy as np
from engine.strategies.market_maker import MarketMakerStrategy
from engine.strategies.mql4_strategy import MQL4Strategy

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def generate_market_data(length=100, start_price=10000.0, trend=0.0):
    """Generates synthetic OHLCV data."""
    prices = [start_price]
    for _ in range(length):
        change = np.random.normal(trend, 10.0)
        prices.append(prices[-1] + change)
    
    df = pd.DataFrame({'close': prices, 'open': prices, 'high': prices, 'low': prices, 'volume': 1000})
    # Add slight volatility to high/low
    df['high'] = df['close'] + 5.0
    df['low'] = df['close'] - 5.0
    df['timestamp'] = pd.date_range(start='2024-01-01', periods=len(df), freq='h')
    return df

def test_market_maker():
    print("\n--- Testing Market Maker Strategy ---")
    
    params = {
        'spread_pct': 0.002,       # 0.2% Spread
        'skew_factor': 10.0,       # Shift $10 per unit inventory
        'order_size': 0.1,
        'max_inventory': 1.0,
        'reprice_threshold': 0.001
    }
    
    mm = MarketMakerStrategy("TestMM", params)
    mid_price = 10000.0
    
    # Case 1: Neutral Inventory
    bid, ask = mm.calculate_quotes(mid_price, current_inventory=0.0)
    print(f"Neutral (Inv=0): Bid={bid:.2f}, Ask={ask:.2f}, Spread={ask-bid:.2f}")
    assert bid < mid_price < ask
    
    # Case 2: Long Inventory (Shift Down)
    bid_long, ask_long = mm.calculate_quotes(mid_price, current_inventory=0.5)
    print(f"Long (Inv=0.5):  Bid={bid_long:.2f}, Ask={ask_long:.2f} (Shifted Down by {mid_price-((bid_long+ask_long)/2):.2f})")
    assert bid_long < bid
    assert ask_long < ask
    
    # Case 3: Short Inventory (Shift Up)
    bid_short, ask_short = mm.calculate_quotes(mid_price, current_inventory=-0.5)
    print(f"Short (Inv=-0.5): Bid={bid_short:.2f}, Ask={ask_short:.2f} (Shifted Up by {((bid_short+ask_short)/2)-mid_price:.2f})")
    assert bid_short > bid
    assert ask_short > ask
    
    print(" [OK] Market Maker Logic Validated")

def test_patterns():
    print("\n--- Testing Indicator-Aware Patterns ---")
    
    # Create data with Rising Prices but FALLING RSI (Divergence setup)
    # This requires precise construction, or we just test the 'check_pattern' method directly.
    
    strategy = MQL4Strategy("TestPat")
    
    # 1. Consecutive Up Pattern
    series_up = pd.Series([10, 11, 12, 13, 14, 15])
    assert strategy.check_pattern(series_up, mode=1, count=3) == True
    assert strategy.check_pattern(series_up, mode=2, count=3) == False
    print(" [OK] Consecutive Up Logic: PASS")
    
    # 2. Consecutive Down Pattern
    series_down = pd.Series([20, 19, 18, 17, 16])
    assert strategy.check_pattern(series_down, mode=2, count=3) == True
    assert strategy.check_pattern(series_down, mode=1, count=3) == False
    print(" [OK] Consecutive Down Logic: PASS")
    
    # 3. Broken Pattern
    series_mixed = pd.Series([10, 12, 11, 13, 15])
    assert strategy.check_pattern(series_mixed, mode=1, count=4) == False
    print(" [OK] Broken Pattern Logic: PASS")

if __name__ == "__main__":
    test_market_maker()
    test_patterns()
