
import unittest
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.append(os.getcwd())

from engine.runner import BotRunner

class TestPartialFill(unittest.TestCase):
    def setUp(self):
        self.runner = BotRunner()
        self.runner.running = True
        self.runner.exchange = MagicMock()
        self.runner.exchanges = {}
        
    def test_partial_fill_chase(self):
        # Mock exchange methods
        self.runner.exchange._safe_request.return_value = {'bid': 50000.0, 'ask': 50001.0}
        
        # validate_order: returns qty passed to it
        def validate_side_effect(pair, side, qty, price):
            return (True, qty, price, "")
        self.runner.exchange.validate_order.side_effect = validate_side_effect
        
        self.runner.exchange.create_order.return_value = {'id': 'order_123'}
        
        # Mock wait_for_fill sequence
        # 1. Partial fill (0.4 filled) -> Timeout (False, order_state)
        # 2. Full fill (remaining 0.6) -> Success (True, order_state)
        
        self.runner.exchange.wait_for_fill.side_effect = [
            (False, {'id': 'order_1', 'filled': 0.4, 'price': 50000.0, 'average': 50000.0, 'status': 'open'}),
            (True, {'id': 'order_2', 'filled': 0.6, 'price': 50001.0, 'average': 50001.0, 'status': 'closed'})
        ]
        
        # Execute with chase
        success, avg_price, order_id = self.runner._execute_limit_with_chase(
            1, "TestBot", "BTC/USDT", "buy", 1.0, timeout=None
        )
        
        # Verify
        self.assertTrue(success, "Should succeed after partial fill chase")
        
        # Expected Avg Price: (0.4 * 50000 + 0.6 * 50001) / 1.0 = 50000.6
        expected_avg = (0.4 * 50000.0 + 0.6 * 50001.0) / 1.0
        self.assertAlmostEqual(avg_price, expected_avg, places=2)
        
        # Verify validate_order called with correct decreasing quantities
        calls = self.runner.exchange.validate_order.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(calls[0][0][2], 1.0) # First attempt: 1.0
        self.assertAlmostEqual(calls[1][0][2], 0.6) # Second attempt: 0.6

if __name__ == '__main__':
    unittest.main()
