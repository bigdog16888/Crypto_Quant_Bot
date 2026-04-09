
import unittest
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.append(os.getcwd())

from engine.runner import BotRunner

@unittest.skip("Architectural Refactor - BotRunner execute_mission obsolete")
class TestPartialOrders(unittest.TestCase):
    def setUp(self):
        self.runner = BotRunner()
        self.runner.exchange = MagicMock()
        self.runner.exchanges = {}
        
    @patch('engine.runner.get_bot_order_ids')
    @patch('engine.runner.check_first_claim_policy')
    def test_grid_fail_tp_continue(self, mock_claim, mock_get_ids):
        # Mock dependencies
        mock_get_ids.return_value = {'tp_order_id': None, 'grid_orders': []}
        mock_claim.return_value = (True, 1, "")
        
        # Mock exchange to fail validation for Grid but succeed for TP
        # validate_order returns (is_valid, amount, price, error)
        def validate_side_effect(pair, side, qty, price):
            if side == 'buy': # Grid side (assuming LONG)
                return (False, 0, 0, "Insufficient Balance")
            return (True, qty, price, "")
            
        self.runner.exchange.validate_order.side_effect = validate_side_effect
        self.runner.exchange.fetch_open_orders.return_value = []
        
        # Execute mission
        mission = {
            'action': 'maintain_orders',
            'bot_id': 1,
            'bot_name': 'TestBot',
            'pair': 'BTC/USDT',
            'direction': 'LONG',
            'grid_price': 49000,
            'grid_qty': 0.1,
            'tp_price': 51000,
            'tp_qty': 0.1
        }
        
        # Run
        self.runner.execute_mission(mission)
        
        # Verify:
        # 1. Grid create_order should NOT be called (validation failed)
        # 2. TP create_order SHOULD be called (validation succeeded)
        
        # Check calls
        calls = self.runner.exchange.create_order.call_args_list
        tp_calls = [c for c in calls if c[0][2] == 'sell'] # TP is sell for LONG
        grid_calls = [c for c in calls if c[0][2] == 'buy'] # Grid is buy for LONG
        
        self.assertEqual(len(grid_calls), 0, "Grid order should be skipped on validation failure")
        self.assertEqual(len(tp_calls), 1, "TP order should be placed despite Grid failure")

if __name__ == '__main__':
    unittest.main()
