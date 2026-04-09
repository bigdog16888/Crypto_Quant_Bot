"""
Test Partial Fill Cancel Block

Ensures `execute_entry` in BotExecutor does NOT cancel an entry order 
when chasing if the order is already partially filled.
"""

import unittest
import sys
import os
import time
from unittest.mock import MagicMock, patch

sys.path.append(os.getcwd())

from engine.bot_executor import BotExecutor

class TestPartialFill(unittest.TestCase):
    def setUp(self):
        self.runner = MagicMock()
        self.bot_executor = BotExecutor(self.runner)
        self.mock_exchange = MagicMock()
        self.mock_strategy = MagicMock()

    @patch('engine.bot_executor.logger')
    @patch('engine.bot_executor.get_bot_status')
    @patch('engine.bot_executor.get_bot_order_ids')
    def test_partial_fill_blocks_chase_cancel(self, mock_get_order_ids, mock_get_status, mock_logger):
        bot_id = 1
        name = "TestBot"
        pair = "BTC/USDT"
        side = "buy"
        amount = 1.0
        price = 50000.0

        mock_get_order_ids.return_value = {}
        mock_status = {'total_invested': 0, 'basket_start_time': 0, 'current_step': 0}
        
        # Simulate an order that was placed 65 seconds ago (past the 60s chase timeout)
        past_timestamp = int(time.time() * 1000) - 65000
        
        # 1. Order is 40% filled
        existing_order = {
            'id': 'stuck_order_123',
            'clientOrderId': f'CQB_{bot_id}_ENTRY_1',
            'timestamp': past_timestamp,
            'filled': 0.4,
            'status': 'open'
        }
        
        self.mock_exchange.fetch_open_orders.return_value = [existing_order]
        
        # Strategy mock
        self.bot_executor._get_strategy_instance = MagicMock(return_value=self.mock_strategy)

        # Run execute_entry
        self.bot_executor.execute_entry(
            bot_id, name, pair, side, amount, price,
            exchange=self.mock_exchange,
            market_snapshot=None,
            bot_config={},
            bot_status=mock_status
        )
        
        # Verify cancel_order was NOT called because partial fill blocked it
        self.mock_exchange.cancel_order.assert_not_called()
        
        # Verify logger info was written stating CANCEL BLOCKED
        cancel_blocked_log = [call for call in mock_logger.info.call_args_list 
                              if "CANCEL BLOCKED" in str(call)]
        self.assertTrue(len(cancel_blocked_log) > 0, "Should have logged that cancel was blocked due to partial fill")

if __name__ == '__main__':
    unittest.main()
