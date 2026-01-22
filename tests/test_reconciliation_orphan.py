
import unittest
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.append(os.getcwd())

from engine.reconciliation import StateReconciler, PositionOwner, ReconciliationAction

class MockBot:
    def __init__(self, bot_id, name, pair, in_trade):
        self.bot_id = bot_id
        self.name = name
        self.pair = pair
        self.in_trade = in_trade

class MockPosition:
    def __init__(self, size, entry_price):
        self.size = size
        self.entry_price = entry_price

class TestOrphanLogic(unittest.TestCase):
    @patch('engine.reconciliation.logger')
    def test_reconcile_bot_orphan(self, mock_logger):
        reconciler = StateReconciler()
        
        bot = MockBot(1, "TestBot", "BTC/USDT", in_trade=False)
        position = MockPosition(size=0.1, entry_price=50000)
        orders = []
        
        # Scenario: Bot IDLE (in_trade=False), Position EXISTS, Bot NOT owner
        ownership = {} 
        
        # Call reconcile_bot
        result = reconciler.reconcile_bot(bot, position, orders, ownership)
        
        # Verify result
        self.assertEqual(result.position_owner, PositionOwner.ORPHAN)
        self.assertEqual(result.action_taken, ReconciliationAction.NO_ACTION)
        self.assertFalse(result.requires_manual_intervention)
        self.assertIn("Orphan position detected (Manual/External)", result.details)
        
        # Verify logger warning
        mock_logger.warning.assert_called()

if __name__ == '__main__':
    unittest.main()
