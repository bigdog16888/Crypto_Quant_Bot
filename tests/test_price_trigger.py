import sys
import os
import pandas as pd
import unittest

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.strategies.martingale_strategy import MartingaleStrategy

class TestPriceTrigger(unittest.TestCase):
    def setUp(self):
        # Create dummy market data
        self.market_data = pd.DataFrame({
            'open': [100.0] * 100,
            'high': [105.0] * 100,
            'low': [95.0] * 100,
            'close': [100.0] * 100,
            'volume': [1000.0] * 100,
            'timestamp': pd.date_range(start='2024-01-01', periods=100, freq='1h')
        })
        self.market_data.set_index('timestamp', drop=False, inplace=True)

    def test_price_above_trigger_passes(self):
        # Trigger Condition: Price > 90
        # Current Price: 100
        # Expected: Pass (True, True)
        params = {
            'mode_price': 1, # Above
            'price_threshold': 90.0,
            'use_any_entry': False
        }
        strategy = MartingaleStrategy(name="TestBot", params=params)
        buy, sell = strategy.check_signals(self.market_data)
        
        print(f"Test Pass (Price 100 > Thresh 90): Buy={buy}, Sell={sell}")
        self.assertTrue(buy)
        self.assertTrue(sell)

    def test_price_above_trigger_fails(self):
        # Trigger Condition: Price > 110
        # Current Price: 100
        # Expected: Fail (False, False)
        params = {
            'mode_price': 1, # Above
            'price_threshold': 110.0
        }
        strategy = MartingaleStrategy(name="TestBot", params=params)
        buy, sell = strategy.check_signals(self.market_data)
        
        print(f"Test Fail (Price 100 < Thresh 110): Buy={buy}, Sell={sell}")
        self.assertFalse(buy)
        self.assertFalse(sell)

    def test_price_below_trigger_passes(self):
        # Trigger Condition: Price < 110
        # Current Price: 100
        # Expected: Pass
        params = {
            'mode_price': 2, # Below
            'price_threshold': 110.0
        }
        strategy = MartingaleStrategy(name="TestBot", params=params)
        buy, sell = strategy.check_signals(self.market_data)
        
        print(f"Test Pass (Price 100 < Thresh 110): Buy={buy}, Sell={sell}")
        self.assertTrue(buy)
        self.assertTrue(sell)

    def test_price_below_trigger_fails(self):
        # Trigger Condition: Price < 90
        # Current Price: 100
        # Expected: Fail
        params = {
            'mode_price': 2, # Below
            'price_threshold': 90.0
        }
        strategy = MartingaleStrategy(name="TestBot", params=params)
        buy, sell = strategy.check_signals(self.market_data)
        
        print(f"Test Fail (Price 100 > Thresh 90): Buy={buy}, Sell={sell}")
        self.assertFalse(buy)
        self.assertFalse(sell)

    def test_confluence_fails(self):
        # Price Trigger Pass, but another Trigger Fails (e.g., CCI)
        # CCI defaults to 0 in mock?
        # Let's use simple logic.
        # mode_price = 1 (Passes, 100 > 90)
        # mode_rsi = 1 (RSI Below 30). Mock RSI checks last 14. 
        # Flat data -> RSI = 50 (neutral) or undefined. 
        # iRSI helper returns 50.0 if empty.
        
        # Test Case: Price OK, RSI Fail
        # Price > 90 (OK)
        # RSI < 30 (Fail, since RSI=50)
        
        params = {
            'mode_price': 1,
            'price_threshold': 90.0,
            'mode_rsi': 1, # Below
            'rsi_level': 30.0,
            'rsi_period': 14
        }
        strategy = MartingaleStrategy(name="TestBot", params=params)
        buy, sell = strategy.check_signals(self.market_data)
        
        print(f"Test Confluence Fail (Price OK, RSI Fail): Buy={buy}, Sell={sell}")
        self.assertFalse(buy)
        self.assertFalse(sell)

if __name__ == '__main__':
    unittest.main()
