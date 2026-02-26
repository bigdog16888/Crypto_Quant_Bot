
import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import json
import time

# Ensure root is in path
sys.path.append(os.getcwd())

from engine.runner import BotRunner

class TestGhostClearingHedged(unittest.TestCase):
    def setUp(self):
        self.runner = BotRunner()
        # Mock dependencies
        self.runner.get_active_bots = MagicMock()
        self.runner.exchanges = {'future': MagicMock()}
        self.mock_db = MagicMock()
        
    @patch('engine.database.get_connection')
    @patch('engine.database.log_trade')
    @patch('engine.runner.normalize_symbol', side_effect=lambda x: x.replace('/', ''))
    def test_hedged_net_zero_no_reset(self, mock_norm, mock_log_trade, mock_get_conn):
        """
        Scenario: 
        Bot A: Long $265 on BTC/USDC
        Bot B: Short $269 on BTC/USDC
        System Net: -$4
        Exchange Net: -$4.50 (rounding difference)
        
        Expected: NO reset occurs because diff ($0.50) is well within percentage tolerance.
        """
        # (id, name, pair, direction, strategy, config, invested, step, rsi, active)
        bots = [
            (10001, "Bot_Long", "BTC/USDC", "LONG", "Martingale", '{"market_type": "future"}', 265.0, 1, 30.0, 1),
            (10002, "Bot_Short", "BTC/USDC", "SHORT", "Martingale", '{"market_type": "future"}', 269.0, 1, 30.0, 1)
        ]
        self.runner.get_active_bots.return_value = bots
        
        # Snapshots
        # Note: exchange net is -$4.50 (side='SHORT', qty-contracts * price = 4.50)
        snap_pos = [
            {'symbol': 'BTCUSDC', 'contracts': 0.0001, 'entryPrice': 45000, 'side': 'SHORT', 'notional': 4.5}
        ]
        self.runner.exchanges['future'].fetch_positions.return_value = snap_pos
        self.runner.exchanges['future'].fetch_open_orders.return_value = []
        
        # We need to set cycle_count to a multiple of 10 to trigger adoption/ghost logic
        self.runner.cycle_count = 10
        
        # Mock DB connection
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        
        # Run cycle
        with patch.object(self.runner, '_bot_executor'):
            self.runner.run_cycle()
            
        # Verify no DB updates for trade clear (no ghost-bust)
        # log_trade is called for actual trades, reset_bot calls log_trade with action='SYSTEM_FIX'
        for call in mock_log_trade.call_args_list:
            self.assertNotEqual(call[1].get('action'), 'SYSTEM_FIX')
            
        print("✅ Success: Hedged positions near net-zero did not trigger false resets.")

    @patch('engine.database.get_connection')
    @patch('engine.database.log_trade')
    @patch('engine.runner.normalize_symbol', side_effect=lambda x: x.replace('/', ''))
    def test_real_ghost_reset(self, mock_norm, mock_log_trade, mock_get_conn):
        """
        Scenario:
        Bot A: Long $500 on BTC/USDC
        Exchange Net: 0 (Position Closed/Gone)
        
        Expected: Ghost-bust fires and resets the bot.
        """
        bots = [
            (30001, "Bot_Ghost", "BTC/USDC", "LONG", "Martingale", '{"market_type": "future"}', 500.0, 1, 30.0, 1)
        ]
        self.runner.get_active_bots.return_value = bots
        self.runner.cycle_count = 10
        
        # Exchange has NO positions
        self.runner.exchanges['future'].fetch_positions.return_value = []
        self.runner.exchanges['future'].fetch_open_orders.return_value = []
        
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        
        with patch.object(self.runner, '_bot_executor'):
            self.runner.run_cycle()
            
        # Verify SYSTEM_FIX log_trade was called
        fix_calls = [call for call in mock_log_trade.call_args_list if call[1].get('action') == 'SYSTEM_FIX']
        self.assertTrue(len(fix_calls) > 0, "Ghost Busted should have logged a SYSTEM_FIX")
        self.assertEqual(fix_calls[0][1].get('bot_id'), 30001)
        
        print("✅ Success: Real ghost position was correctly reset.")

if __name__ == '__main__':
    unittest.main()
