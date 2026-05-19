import sys
import os
import pandas as pd
import numpy as np

# Add current directory to path
sys.path.append(os.getcwd())

from engine.strategy import Strategy

def run_verification():
    print("--- Strategy Pandas-TA Verification ---")
    
    # Generate sample data roughly mimicking an uptrend then downtrend
    # Length needs to be sufficient for indicators (e.g. MACD needs 26+9)
    length = 100
    dates = pd.date_range(start='2025-01-01', periods=length, freq='1h')
    
    # Uptrend
    close = np.linspace(100, 110, length)
    # Add some volatility
    noise = np.random.normal(0, 0.2, length) 
    close += noise
    
    high = close + 0.5
    low = close - 0.5
    open_ = close - 0.1 # Simplistic
    
    df = pd.DataFrame({
        'open': open_,
        'high': high,
        'low': low,
        'close': close,
        'volume': np.random.randint(100, 1000, length)
    }, index=dates)
    
    print(f"Generated {length} candles of sample data.")
    
    strategy = Strategy()
    
    # Enable indicators to test them
    strategy.cci_entry = 1 # Standard CCI check
    strategy.bollinger_entry = 1 # Standard Bollinger check
    # strategy.stoch_entry = 1
    # strategy.macd_entry = 1
    
    print("\nRunning check_signals...")
    try:
        buy, sell = strategy.check_signals(df)
        print(f"Result -> Buy: {buy}, Sell: {sell}")
        print("Success: check_signals executed without error using pandas-ta.")
        
    except Exception as e:
        print(f"Error during check_signals: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_verification()
