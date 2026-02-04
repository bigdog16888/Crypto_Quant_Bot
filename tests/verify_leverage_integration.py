#!/usr/bin/env python3
import sys
import unittest
sys.path.insert(0, '.')

from engine.exchange_interface import ExchangeInterface, normalize_symbol

class TestLeverageIntegration(unittest.TestCase):
    def setUp(self):
        print("Initializing ExchangeInterface...")
        self.ex = ExchangeInterface(market_type='future', validate=False)
        
    def test_calculate_real_leverage(self):
        print("Fetching positions...")
        positions = self.ex.fetch_positions()
        
        # Test on a known active bot pair (e.g. BTC/USDC)
        target_pair = 'BTC/USDC'
        print(f"Testing calculation for {target_pair}...")
        
        # 1. Fetch raw calculation
        lev = self.ex.calculate_real_leverage(target_pair)
        print(f"Calculated Leverage: {lev}")
        
        # 2. Assert it returns a valid integer
        self.assertIsNotNone(lev, "Leverage calculation returned None")
        self.assertIsInstance(lev, int, "Leverage should be an integer")
        self.assertTrue(lev > 0, "Leverage should be positive")
        
        # 3. Test on a non-existent pair if possible, or just skip
        # lev_empty = self.ex.calculate_real_leverage('UNKNOWN/USDT')
        # self.assertIsNone(lev_empty)

if __name__ == '__main__':
    unittest.main()
