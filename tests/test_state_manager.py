import unittest
from unittest.mock import MagicMock, patch
import sys
import os
from pathlib import Path

# Add root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.state_manager import StateManager, UnifiedBotState, HealthReport

class TestStateManager(unittest.TestCase):
    def setUp(self):
        # Reset singleton
        StateManager._instance = None
        self.sm = StateManager()
        
        # Mock dependencies
        self.sm.get_connection = MagicMock()
        self.sm._exchange = MagicMock()
        self.mock_conn = self.sm.get_connection.return_value
        self.mock_cursor = self.mock_conn.cursor.return_value

    def test_normalize_pair(self):
        """Test pair normalization logic including futures suffixes"""
        self.assertEqual(self.sm._normalize_pair("BTC/USDT"), "BTCUSDT")
        self.assertEqual(self.sm._normalize_pair("BTC/USDC"), "BTCUSDC")
        self.assertEqual(self.sm._normalize_pair("BTC/USDC:USDC"), "BTCUSDC")
        self.assertEqual(self.sm._normalize_pair("XAU/USDT:USDT"), "XAUUSDT")
        self.assertEqual(self.sm._normalize_pair("ETH-PERP"), "ETHPERP")

    def test_get_bot_state_consistent(self):
        """Test fetching a consistent bot state"""
        # Mock DB returns
        # bots: id, name, pair, direction, is_active, status
        self.mock_cursor.fetchone.side_effect = [
            (1, "TestBot", "BTC/USDT", "LONG", 1, "IN TRADE"), # bots
            (1, 100.0, 50000.0, 51000.0), # trades: step, invested, entry, tp
            ("owner", 1, 0.002) # ownership
        ]
        
        # Mock Exchange
        # positions list
        self.sm.exchange.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT', 'contracts': 0.002, 'entryPrice': 50000.0}
        ]
        # open orders (must have bot's clientOrderId to be recognized as TP)
        self.sm.exchange.exchange.fetch_open_orders.return_value = [
            {'id': '123', 'symbol': 'BTC/USDT', 'clientOrderId': 'CQB_1_TP_TEST'}
        ]
        
        state = self.sm.get_bot_state(1)
        
        self.assertTrue(state.is_consistent)
        self.assertTrue(state.in_trade)
        self.assertTrue(state.exchange_has_position)
        self.assertEqual(state.total_invested, 100.0)
        self.assertEqual(state.exchange_position_size, 0.002)

    def test_get_bot_state_inconsistent_missing_position(self):
        """Test detection of DB says 'IN TRADE' but Exchange has no position"""
        self.mock_cursor.fetchone.side_effect = [
            (1, "TestBot", "BTC/USDT", "LONG", 1, "IN TRADE"),
            (1, 100.0, 50000.0, 51000.0),
            ("owner", 1, 0.002)
        ]
        
        # Exchange returns empty positions
        self.sm.exchange.exchange.fetch_positions.return_value = []
        self.sm.exchange.exchange.fetch_open_orders.return_value = []
        
        state = self.sm.get_bot_state(1)
        
        self.assertFalse(state.is_consistent)
        self.assertIn("DB says IN TRADE but exchange has no position", state.inconsistencies[0])

    def test_get_bot_state_inconsistent_missing_orders(self):
        """Test detection of Position exists but no TP orders"""
        self.mock_cursor.fetchone.side_effect = [
            (1, "TestBot", "BTC/USDT", "LONG", 1, "IN TRADE"),
            (1, 100.0, 50000.0, 51000.0),
            ("owner", 1, 0.002)
        ]
        
        # Position exists
        self.sm.exchange.exchange.fetch_positions.return_value = [
            {'symbol': 'BTC/USDT', 'contracts': 0.002, 'entryPrice': 50000.0}
        ]
        # BUT no open orders
        self.sm.exchange.exchange.fetch_open_orders.return_value = []
        
        state = self.sm.get_bot_state(1)
        
        self.assertFalse(state.is_consistent)
        self.assertTrue(any("missing TP" in x for x in state.inconsistencies))

if __name__ == '__main__':
    unittest.main()
