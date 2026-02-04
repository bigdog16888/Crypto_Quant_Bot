#!/usr/bin/env python3
import sys
import unittest
from unittest.mock import MagicMock
sys.path.insert(0, '.')

from engine.manager import _find_bot_orders

class TestPhase8Tags(unittest.TestCase):
    def setUp(self):
        self.bot_id = 99
        self.pair = 'BTC/USDT'
        self.logger = MagicMock()
        self.settings = {}
        
        # Tags
        self.tp_tag = f"CQB_{self.bot_id}_TP_123"
        self.grid_tag = f"CQB_{self.bot_id}_GRID_456"
        self.other_bot_tag = "CQB_100_TP_789"
        
    def test_tag_priority_over_db(self):
        """Phase 8: Tagged orders should be identified even if DB ID mismatch."""
        # Setup: Exchange has order with CORRECT TAG but DIFFERENT ID from DB
        open_orders = [{
            'id': 'exchange_oid_999',
            'clientOrderId': self.tp_tag,
            'symbol': 'BTC/USDT',
            'side': 'sell',
            'price': 100000
        }]
        
        # Mock DB to return a DIFFERENT ID
        with unittest.mock.patch('engine.database.get_bot_order_ids') as mock_db:
            mock_db.return_value = {'tp_order_id': 'old_db_id_111', 'grid_orders': []}
            
            has_tp, tp_order, has_grid, _ = _find_bot_orders(
                self.bot_id, self.pair, 'LONG', open_orders, 100000, self.settings, self.logger
            )
            
            # Assert: matched by TAG despite ID mismatch
            self.assertTrue(has_tp)
            self.assertEqual(tp_order['id'], 'exchange_oid_999')
            
    def test_ignore_other_bot_tags(self):
        """Phase 8: Should ignore orders tagged for other bots."""
        open_orders = [{
            'id': 'exchange_oid_888',
            'clientOrderId': self.other_bot_tag, # Bot 100
            'symbol': 'BTC/USDT',
            'side': 'sell'
        }]
        
        with unittest.mock.patch('engine.database.get_bot_order_ids') as mock_db:
            mock_db.return_value = {'tp_order_id': 'none', 'grid_orders': []}
            
            has_tp, _, _, _ = _find_bot_orders(
                self.bot_id, self.pair, 'LONG', open_orders, 100000, self.settings, self.logger
            )
            
            self.assertFalse(has_tp)

if __name__ == '__main__':
    print("Running Phase 8 Robustness Checks...")
    unittest.main()
