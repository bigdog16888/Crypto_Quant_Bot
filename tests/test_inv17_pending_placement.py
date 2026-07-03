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
from engine.bot_executor import sync_stale_open_orders
from engine.ground_truth_reconciler import GroundTruthReconciler
from engine.exchange_interface import ExchangeInterface

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_inv17_pending_placement.db')
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

def _insert_order(conn, bot_id, order_type, filled_amount, amount, price, status, cycle_id=1, position_side='LONG', created_at=None, order_id=None):
    if created_at is None:
        created_at = int(time.time())
    if order_id is None:
        order_id = f"CQB_{bot_id}_{order_type}_{cycle_id}_{created_at}"
    conn.execute("""
        INSERT INTO bot_orders (bot_id, order_type, filled_amount, amount, price, status, cycle_id, position_side, created_at, client_order_id, order_id, step)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (bot_id, order_type, filled_amount, amount, price, status, cycle_id, position_side, created_at, order_id, order_id))
    conn.commit()

class TestStalePendingPlacementRecovery(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self.mock_exchange = MagicMock(spec=ExchangeInterface)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pending_placement_cancelled_when_not_found(self):
        # pending_placement row older than 30s. exchange.fetch_order raises OrderNotFound.
        _insert_bot(self.conn, 10001, 'test_bot', 'SOL/USDC', 'SOLUSDC', 'SHORT')
        _insert_trades(self.conn, 10001, open_qty=0.03, cycle_id=85)
        
        # Insert a pending_placement order older than 30s
        old_time = int(time.time()) - 40
        _insert_order(self.conn, 10001, 'close', 0.0, 0.03, 0.0, 'pending_placement', cycle_id=85, position_side='SHORT', created_at=old_time)
        
        # Mock exchange to raise OrderNotFound (-2013)
        self.mock_exchange.fetch_order.side_effect = Exception("Binance API 400: Order does not exist. (code -2013)")
        
        # Run stale sync (default max_age_seconds is 120, but pending_placement has a 30s cutoff)
        sync_stale_open_orders(10001, self.mock_exchange, self.conn)
        
        # Assert status updated to 'cancelled'
        status = self.conn.execute("SELECT status FROM bot_orders WHERE bot_id=10001").fetchone()[0]
        self.assertEqual(status, 'cancelled')

    def test_pending_placement_not_cancelled_when_young(self):
        # pending_placement row younger than 30s.
        _insert_bot(self.conn, 10001, 'test_bot', 'SOL/USDC', 'SOLUSDC', 'SHORT')
        _insert_trades(self.conn, 10001, open_qty=0.03, cycle_id=85)
        
        # Insert a pending_placement order younger than 30s (e.g. 10s old)
        young_time = int(time.time()) - 10
        _insert_order(self.conn, 10001, 'close', 0.0, 0.03, 0.0, 'pending_placement', cycle_id=85, position_side='SHORT', created_at=young_time)
        
        # Run stale sync
        sync_stale_open_orders(10001, self.mock_exchange, self.conn)
        
        # Assert exchange fetch_order was not called
        self.mock_exchange.fetch_order.assert_not_called()
        
        # Assert status remains 'pending_placement'
        status = self.conn.execute("SELECT status FROM bot_orders WHERE bot_id=10001").fetchone()[0]
        self.assertEqual(status, 'pending_placement')

    @patch('engine.ledger.credit_fill')
    def test_pending_placement_credited_when_found_filled(self, mock_credit_fill):
        # pending_placement row older than 30s. exchange.fetch_order returns filled order.
        _insert_bot(self.conn, 10001, 'test_bot', 'SOL/USDC', 'SOLUSDC', 'SHORT')
        _insert_trades(self.conn, 10001, open_qty=0.03, cycle_id=85)
        
        old_time = int(time.time()) - 40
        order_id = 'CQB_10001_CLOSE_85_9999'
        _insert_order(self.conn, 10001, 'close', 0.0, 0.03, 0.0, 'pending_placement', cycle_id=85, position_side='SHORT', created_at=old_time, order_id=order_id)
        
        # Mock exchange to return a filled order info dict
        self.mock_exchange.fetch_order.return_value = {
            'id': '12345',
            'status': 'closed',
            'filled': 0.03,
            'amount': 0.03,
            'price': 100.0,
            'average': 100.0,
            'clientOrderId': order_id
        }
        
        # Run stale sync
        sync_stale_open_orders(10001, self.mock_exchange, self.conn)
        
        # Assert credit_fill called with correct parameters
        mock_credit_fill.assert_called_once()
        call_kwargs = mock_credit_fill.call_args[1]
        self.assertEqual(call_kwargs['bot_id'], 10001)
        self.assertEqual(call_kwargs['order_id'], order_id)
        self.assertEqual(call_kwargs['cumulative_qty'], 0.03)

    def test_gtr_clears_stuck_pending_placement(self):
        # pending_placement row older than 30s in GTR pass. OrderNotFound.
        _insert_bot(self.conn, 10001, 'test_bot', 'SOL/USDC', 'SOLUSDC', 'SHORT')
        _insert_trades(self.conn, 10001, open_qty=0.03, cycle_id=85)
        
        old_time = int(time.time()) - 40
        _insert_order(self.conn, 10001, 'close', 0.0, 0.03, 0.0, 'pending_placement', cycle_id=85, position_side='SHORT', created_at=old_time)
        
        # Mock exchange fetch_positions so GTR does not fail
        self.mock_exchange.fetch_positions.return_value = []
        # Mock exchange fetch_order to raise OrderNotFound
        self.mock_exchange.fetch_order.side_effect = Exception("OrderNotFound code -2013")
        
        # Run GTR
        gtr = GroundTruthReconciler()
        results = gtr.run(self.mock_exchange, self.conn)
        
        # Assert stuck_pending_cleared contains bot_id
        self.assertIn(10001, results.get('stuck_pending_cleared', []))
        
        # Assert status updated to 'cancelled' in DB
        status = self.conn.execute("SELECT status FROM bot_orders WHERE bot_id=10001").fetchone()[0]
        self.assertEqual(status, 'cancelled')

if __name__ == "__main__":
    unittest.main()
