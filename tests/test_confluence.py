import pandas as pd
import numpy as np
import sys
import os

# Ensure engine can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.strategies.mql4_strategy import MQL4Strategy

def generate_mock_data(n=100):
    dates = pd.date_range(start='2023-01-01', periods=n, freq='15min')
    data = pd.DataFrame({
        'open': np.linspace(100, 110, n),
        'high': np.linspace(101, 111, n),
        'low': np.linspace(99, 109, n),
        'close': np.linspace(100, 110, n),
        'volume': np.random.randint(100, 1000, n)
    }, index=dates)
    return data

def test_8_trigger_confluence():
    print("Testing 8-Trigger Confluence...")
    
    # 1. Test Single Indicator (CCI Above)
    params = {
        'mode_cci': 1, 'cci_level': 50, 'cci_period': 14, 'cci_tf': '15m'
    }
    strat = MQL4Strategy(params=params)
    data = generate_mock_data(100) # Trends up, CCI will be high
    buy, sell = strat.check_signals(data)
    print(f"Single Indicator (CCI Above): Buy={buy}, Sell={sell}")
    assert buy == True, "CCI Above trigger failed"

    # 2. Test Confluence (CCI Above + Pattern Slot 1 Down)
    params.update({
        'pat_1_mode': 2, 'pat_1_count': 3, 'pat_1_tf': '15m'
    })
    strat = MQL4Strategy(params=params)
    # Mock data is trending UP, so Pattern (Consec Down) should fail the confluence
    buy, sell = strat.check_signals(data)
    print(f"Confluence (CCI Above + Pattern Down): Buy={buy}, Sell={sell}")
    assert buy == False, "Confluence should have failed due to mismatching pattern"

    # 3. Test Multiple Patterns
    # Create data with 3 down candles at the end
    data_mixed = data.copy()
    data_mixed.iloc[-3:, data_mixed.columns.get_loc('close')] = [105, 104, 103]
    
    params = {
        'pat_1_mode': 2, 'pat_1_count': 3, 'pat_1_tf': '15m'
    }
    strat = MQL4Strategy(params=params)
    buy, sell = strat.check_signals(data_mixed)
    print(f"Pattern Trigger (Consec Down): Buy={buy}, Sell={sell}")
    assert buy == True, "Pattern trigger failed on matching data"

    print("✅ 8-Trigger Confluence verified!")

if __name__ == "__main__":
    test_8_trigger_confluence()
