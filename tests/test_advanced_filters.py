import os
import sys
import pandas as pd
import numpy as np

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.strategies.martingale_strategy import MartingaleStrategy

def test_mtf_logic():
    print("\n--- Testing MTF Confluence Logic ---")
    # Base data (5m)
    data_5m = pd.DataFrame({
        'timestamp': pd.date_range(start='2023-01-01', periods=100, freq='5min'),
        'open': np.random.uniform(20000, 21000, 100),
        'high': np.random.uniform(21000, 22000, 100),
        'low': np.random.uniform(19000, 20000, 100),
        'close': np.random.uniform(20000, 21000, 100),
        'volume': np.random.uniform(1, 10, 100)
    })
    
    # 1. Test WITH MTF - Should be False if 1H doesn't align
    params_mtf = {
        'cci_entry': 1, # Standard
        'cci_period': 14,
        'UseMTFConfluence': True,
        'MTF_Timeframe': '1h'
    }
    strat = MartingaleStrategy(name="MTF_Test", params=params_mtf)
    buy, sell = strat.check_signals(data_5m)
    print(f"MTF Enabled (High TF Random) - Buy: {buy}, Sell: {sell}")

def test_consecutive_logic():
    print("\n--- Testing Consecutive Candle Trigger ---")
    # Force 4 consecutive lower candles
    data_down = pd.DataFrame({
        'timestamp': pd.date_range(start='2023-01-01', periods=10, freq='5min'),
        'close': [100, 105, 110, 108, 106, 104, 102, 100, 98, 96], # Last 4 are 102, 100, 98, 96 (All lower)
        'high': 110, 'low': 90, 'open': 100, 'volume': 10
    })
    
    params_cons = {
        'bollinger_entry': 1,
        'boll_period': 20,
        'boll_deviation': 2.0,
        'trigger_candles': 4
    }
    strat = MartingaleStrategy(name="Cons_Test", params=params_cons)
    
    # Price 96 is far below any random high, so boll buy might trigger if distance allows
    # But we want to see if trigger_candles filters it
    buy, sell = strat.check_signals(data_down)
    print(f"Consecutive Logic (4 lower) - Buy Triggered: {buy}")

if __name__ == "__main__":
    test_mtf_logic()
    test_consecutive_logic()
