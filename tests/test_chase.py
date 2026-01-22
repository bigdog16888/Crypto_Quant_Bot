
import unittest
import time
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.append(os.getcwd())

from engine.runner import BotRunner

class TestChaseLogic(unittest.TestCase):
    def setUp(self):
        self.runner = BotRunner()
        self.runner.running = True # Must be running for loops to work
        self.runner.exchange = MagicMock()
        self.runner.exchanges = {}
        
    def test_infinite_chase(self):
        """Test that chase logic continues beyond the initial list."""
        
        # Mock exchange methods
        self.runner.exchange._safe_request.return_value = {'bid': 50000.0, 'ask': 50001.0}
        self.runner.exchange.validate_order.return_value = (True, 0.1, 50000.0, "")
        self.runner.exchange.create_order.return_value = {'id': 'order_123'}
        
        # Behavior: 
        # Attempt 1: Not filled
        # Attempt 2: Not filled
        # Attempt 3: Filled
        
        # wait_for_fill returns (filled, order)
        # We simulate 2 failures then success
        self.runner.exchange.wait_for_fill.side_effect = [
            (False, None), 
            (False, None),
            (True, {'average': 50000.0})
        ]
        
        # Config with short list
        config = {'chase_intervals': [1, 1]} 
        
        # Patch get_bot_params to return our config
        with patch('engine.runner.get_bot_params') as mock_get_params:
            # Mock return: (..., config_json) at index 7
            import json
            mock_get_params.return_value = [None]*7 + [json.dumps(config)]
            
            # Execute
            # We pass timeout=0 to disable the global timeout check for this test logic 
            # (or we rely on the internal logic I'm about to write)
            success, price, oid = self.runner._execute_limit_with_chase(
                1, "TestBot", "BTC/USDT", "buy", 0.001, timeout=None
            )
            
            self.assertTrue(success)
            self.assertEqual(self.runner.exchange.wait_for_fill.call_count, 3)
            # It should have called create_order 3 times (and cancel 2 times)
            self.assertEqual(self.runner.exchange.create_order.call_count, 3)
            self.assertEqual(self.runner.exchange.exchange.cancel_order.call_count, 2)

if __name__ == '__main__':
    unittest.main()
