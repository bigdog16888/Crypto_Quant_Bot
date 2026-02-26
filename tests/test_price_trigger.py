import sys
import os
import pandas as pd
import numpy as np
import unittest

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine.strategies.martingale_strategy import MartingaleStrategy


class TestPriceTrigger(unittest.TestCase):
    def setUp(self):
        # Create dummy market data (100 rows, flat at 100)
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
        # Price 100 > Threshold 90 → Pass
        params = {'mode_price': 1, 'price_threshold': 90.0}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(self.market_data)
        self.assertTrue(buy)
        self.assertTrue(sell)

    def test_price_above_trigger_fails(self):
        # Price 100 < Threshold 110 → Fail
        params = {'mode_price': 1, 'price_threshold': 110.0}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(self.market_data)
        self.assertFalse(buy)
        self.assertFalse(sell)

    def test_price_below_trigger_passes(self):
        # Price 100 < Threshold 110 → Pass
        params = {'mode_price': 2, 'price_threshold': 110.0}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(self.market_data)
        self.assertTrue(buy)
        self.assertTrue(sell)

    def test_price_below_trigger_fails(self):
        # Price 100 > Threshold 90 → Fail
        params = {'mode_price': 2, 'price_threshold': 90.0}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(self.market_data)
        self.assertFalse(buy)
        self.assertFalse(sell)

    def test_no_triggers_always_enters(self):
        # No mode_price set (default 0 = OFF) → always enters
        params = {}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(self.market_data)
        self.assertTrue(buy, "No triggers enabled should always allow entry")
        self.assertTrue(sell)

    def test_confluence_price_pass_rsi_fail(self):
        # Price > 90 (OK), RSI Below 30 (Fail — flat data gives RSI ~50)
        params = {
            'mode_price': 1,
            'price_threshold': 90.0,
            'mode_rsi': 1,  # Below level
            'rsi_level': 30.0,
            'rsi_period': 14
        }
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(self.market_data)
        self.assertFalse(buy, "Entry should be blocked: Price passes but RSI fails")
        self.assertFalse(sell)

    def test_confluence_price_pass_rsi_pass(self):
        # Price > 90 (OK), RSI Above 60 (OK — flat data gives RSI ~99)
        params = {
            'mode_price': 1,
            'price_threshold': 90.0,
            'mode_rsi': 2,  # Above level
            'rsi_level': 60.0,
            'rsi_period': 14
        }
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(self.market_data)
        self.assertTrue(buy, "Entry should pass: both Price and RSI triggers met")
        self.assertTrue(sell)

    def test_cci_above_trigger(self):
        # Create trending-up data so CCI is high
        n = 100
        data = pd.DataFrame({
            'open': np.linspace(100, 120, n),
            'high': np.linspace(101, 121, n),
            'low': np.linspace(99, 119, n),
            'close': np.linspace(100, 120, n),
            'volume': [1000.0] * n,
        })
        params = {'mode_cci': 1, 'cci_level': 50, 'cci_period': 14}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(data)
        self.assertTrue(buy, "CCI should be above 50 on strongly trending data")

    def test_cci_below_trigger(self):
        # Create trending-down data so CCI is low
        n = 100
        data = pd.DataFrame({
            'open': np.linspace(120, 100, n),
            'high': np.linspace(121, 101, n),
            'low': np.linspace(119, 99, n),
            'close': np.linspace(120, 100, n),
            'volume': [1000.0] * n,
        })
        params = {'mode_cci': 2, 'cci_level': -50, 'cci_period': 14}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(data)
        self.assertTrue(buy, "CCI should be below -50 on strongly declining data")

    def test_pattern_consecutive_down(self):
        # Create data with 3 consecutive down candles at the end
        n = 50
        data = pd.DataFrame({
            'open': [100.0] * n,
            'high': [105.0] * n,
            'low': [95.0] * n,
            'close': [100.0] * n,
            'volume': [1000.0] * n,
        })
        # Set last 4 closes to create 3 consecutive downs
        data.iloc[-4, data.columns.get_loc('close')] = 103
        data.iloc[-3, data.columns.get_loc('close')] = 102
        data.iloc[-2, data.columns.get_loc('close')] = 101
        data.iloc[-1, data.columns.get_loc('close')] = 100

        params = {'pat_1_mode': 2, 'pat_1_count': 3}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(data)
        self.assertTrue(buy, "Pattern trigger should fire on 3 consecutive down candles")

    def test_pattern_consecutive_down_fails(self):
        # Flat data — no consecutive pattern
        params = {'pat_1_mode': 2, 'pat_1_count': 3}
        strategy = MartingaleStrategy(params=params)
        buy, sell = strategy.check_signals(self.market_data)
        self.assertFalse(buy, "Pattern should NOT fire on flat data")


if __name__ == '__main__':
    unittest.main()
