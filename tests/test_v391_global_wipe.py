"""
Unit and Integration Tests for v3.9.1 Global Wipe Detection Startup Check.
"""

import os
import sys
import time
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.parity_gates import detect_and_repair_global_wipe
from config.settings import config

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_v391.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

def _insert_bot(conn, bot_id, name, pair, norm_pair, direction,
                status='IN TRADE', bot_type='standard', is_active=1):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction,
                          status, bot_type, is_active,
                          rsi_limit, martingale_multiplier, base_size, strategy_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 0, 'Martingale')
    """, (bot_id, name, pair, norm_pair, direction, status, bot_type, is_active))
    conn.commit()

def _insert_trades(conn, bot_id, open_qty=0.0,
                   cycle_id=1, position_side='LONG', avg_entry_price=0.0, total_invested=0.0):
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step, entry_confirmed)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1)
    """, (bot_id, open_qty, cycle_id, position_side, total_invested, avg_entry_price))
    conn.commit()

class TestV391GlobalWipe(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self._orig_wipe_detection = getattr(config, 'ENABLE_GLOBAL_WIPE_DETECTION', True)
        config.ENABLE_GLOBAL_WIPE_DETECTION = True

    def tearDown(self):
        config.ENABLE_GLOBAL_WIPE_DETECTION = self._orig_wipe_detection
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_global_wipe_not_triggered_single_bot_flat(self):
        # Setup: Only 1 active bot with open_qty > 0.0001
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, 10016, open_qty=0.25, total_invested=100.0)

        # Exchange is flat
        class MockExchangeFlat:
            def fetch_positions(self):
                return []  # Empty -> flat

        ex = MockExchangeFlat()
        
        # Call detect_and_repair_global_wipe
        res = detect_and_repair_global_wipe(ex)
        self.assertFalse(res['triggered'])
        self.assertIn("only 1 bot(s)", res['skipped_reason'])
        self.assertEqual(res['bots_affected'], 0)

    def test_global_wipe_triggered(self):
        # Setup: 2 active bots with open_qty > 0.0001
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, 10016, open_qty=0.25, total_invested=100.0)

        _insert_bot(self.conn, 10017, 'eth long', 'ETH/USDC:USDC', 'ETHUSDC', 'LONG')
        _insert_trades(self.conn, 10017, open_qty=0.5, total_invested=200.0)

        # Exchange is flat
        class MockExchangeFlat:
            def fetch_positions(self):
                return []

            def fetch_open_orders(self, symbol):
                return []

            def cancel_orders_by_bot_id(self, bot_id, symbol):
                return 0

        ex = MockExchangeFlat()

        with patch('engine.parity_gates.purge_phantom_ledger_when_exchange_flat', return_value=(True, "purged bots [10016, 10017]")) as mock_purge:
            res = detect_and_repair_global_wipe(ex)
            self.assertTrue(res['triggered'])
            self.assertEqual(res['skipped_reason'], '')
            self.assertEqual(len(res['pairs_purged']), 2)
            self.assertIn('BTC/USDC:USDC', res['pairs_purged'])
            self.assertIn('ETH/USDC:USDC', res['pairs_purged'])
            self.assertEqual(res['bots_affected'], 4) # 2 pairs, each calls purge mock returning 2 purged bots -> total 4

    def test_global_wipe_not_triggered_exchange_not_flat(self):
        # Setup: 2 active bots with open_qty > 0.0001
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, 10016, open_qty=0.25, total_invested=100.0)

        _insert_bot(self.conn, 10017, 'eth long', 'ETH/USDC:USDC', 'ETHUSDC', 'LONG')
        _insert_trades(self.conn, 10017, open_qty=0.5, total_invested=200.0)

        # Exchange is NOT flat (has non-zero physical position)
        class MockExchangeNotFlat:
            def fetch_positions(self):
                return [{'symbol': 'BTCUSDC', 'contracts': 0.25, 'net_qty': 0.25}]

        ex = MockExchangeNotFlat()

        res = detect_and_repair_global_wipe(ex)
        self.assertFalse(res['triggered'])
        self.assertEqual(res['skipped_reason'], 'exchange has active positions')

    def test_global_wipe_config_disabled(self):
        config.ENABLE_GLOBAL_WIPE_DETECTION = False

        # Setup: 2 active bots with open_qty > 0.0001
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, 10016, open_qty=0.25, total_invested=100.0)

        _insert_bot(self.conn, 10017, 'eth long', 'ETH/USDC:USDC', 'ETHUSDC', 'LONG')
        _insert_trades(self.conn, 10017, open_qty=0.5, total_invested=200.0)

        # Exchange is flat
        class MockExchangeFlat:
            def fetch_positions(self):
                return []

        ex = MockExchangeFlat()

        res = detect_and_repair_global_wipe(ex)
        self.assertFalse(res['triggered'])
        self.assertEqual(res['skipped_reason'], 'disabled by config')

if __name__ == '__main__':
    unittest.main()
