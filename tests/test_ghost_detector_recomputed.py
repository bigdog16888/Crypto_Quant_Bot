import os
import sys
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.oneway_netting import detect_bot_ghost
from engine.exchange_interface import ExchangeInterface

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_ghost_detector_recomputed.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

def _insert_bot(conn, bot_id, name, pair, norm_pair, direction, status='IN TRADE', bot_type='standard'):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
    """, (bot_id, name, pair, norm_pair, direction, status, bot_type))
    conn.commit()

def _insert_trades(conn, bot_id, open_qty=0.0, cycle_id=1, position_side='LONG', avg_entry_price=0.0, basket_start_time=None):
    if basket_start_time is None:
        basket_start_time = int(time.time())
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side, total_invested, avg_entry_price, current_step, entry_confirmed, basket_start_time)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)
    """, (bot_id, open_qty, cycle_id, position_side, open_qty * avg_entry_price, avg_entry_price, basket_start_time))
    conn.commit()

def _insert_order(conn, bot_id, order_type, filled_amount, amount, price, status, cycle_id=1, position_side='LONG', created_at=None):
    if created_at is None:
        created_at = int(time.time())
    conn.execute("""
        INSERT INTO bot_orders (bot_id, order_type, filled_amount, amount, price, status, cycle_id, position_side, created_at, client_order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (bot_id, order_type, filled_amount, amount, price, status, cycle_id, position_side, created_at, f"CQB_{bot_id}_{order_type}_{cycle_id}_{created_at}"))
    conn.commit()

class TestGhostDetectorRecomputed(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self.mock_exchange = MagicMock(spec=ExchangeInterface)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    @patch('engine.oneway_netting.logger.warning')
    def test_ghost_false_when_no_fills_in_cycle(self, mock_warning):
        # Bot has open_qty=1.0 but zero bot_orders fills in current cycle_id=1.
        _insert_bot(self.conn, 10001, 'test_bot', 'BTC/USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, 10001, open_qty=1.0, cycle_id=1)
        
        # Call detect_bot_ghost
        is_ghost = detect_bot_ghost(self.mock_exchange, 10001, self.conn)
        
        # Assert detect_bot_ghost returns False (not declared ghost due to safety guard)
        self.assertFalse(is_ghost)
        
        # Assert WARNING log contains 'cycle_id mismatch'
        mock_warning.assert_called_once()
        log_msg = mock_warning.call_args[0][0]
        self.assertIn("Possible cycle_id mismatch", log_msg)

    def test_ghost_true_when_fills_sum_to_zero(self):
        # Bot has open_qty=1.0, has filled entry AND filled exit both 1.0 in current cycle. recompute returns 0.
        _insert_bot(self.conn, 10001, 'test_bot', 'BTC/USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, 10001, open_qty=1.0, cycle_id=1)
        
        # Seed matching entry and exit fills to recompute to 0
        _insert_order(self.conn, 10001, 'entry', 1.0, 1.0, 1000.0, 'filled', cycle_id=1, position_side='LONG')
        _insert_order(self.conn, 10001, 'tp', 1.0, 1.0, 1010.0, 'filled', cycle_id=1, position_side='LONG')
        
        # Call detect_bot_ghost
        is_ghost = detect_bot_ghost(self.mock_exchange, 10001, self.conn)
        
        # Assert detect_bot_ghost returns True
        self.assertTrue(is_ghost)

if __name__ == "__main__":
    unittest.main()
