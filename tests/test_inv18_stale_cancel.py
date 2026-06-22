"""
Unit and integration tests for INV-18 stale cancel buffer and pre-commit resolve guard.
"""

import os
import sys
import time
import tempfile
import shutil
import unittest
import sqlite3
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.reconciler import StateReconciler
from engine.bot_executor import BotExecutor

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_inv18.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

class TestInv18StaleCancel(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        # Clear connection local cache
        if hasattr(database._local, 'connection') and database._local.connection:
            try:
                database._local.connection.close()
            except Exception:
                pass
            database._local.connection = None

    @patch('engine.reconciler.get_connection')
    @patch('engine.database.get_connection')
    def test_pre_commit_resolve_guard(self, mock_db_conn, mock_recon_conn):
        """
        Verify that:
        1. If lookup fails with a transient exception, the placing order is NOT deleted.
        2. If lookup confirms the order is NOT on the exchange (NotFound), the placing order IS deleted.
        """
        mock_db_conn.return_value = self.conn
        mock_recon_conn.return_value = self.conn

        bot_id = 12345
        # Insert bot
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status)
            VALUES (?, 'test bot', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')
        """, (bot_id,))
        self.conn.commit()

        # Seed a placing order
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, price, amount, filled_amount, status, created_at, client_order_id, cycle_id)
            VALUES (?, 1, 'grid', 10.0, 5.0, 0.0, 'placing', 1779940000, 'CQB_12345_GRID_1_1', 1)
        """, (bot_id,))
        self.conn.commit()

        # Case 1: Transient Exception (e.g., ccxt.NetworkError or Timeout)
        mock_ex = MagicMock()
        mock_ex.fetch_order.side_effect = Exception("ccxt.NetworkError: Request timed out")
        mock_ex.fetch_open_orders.side_effect = Exception("ccxt.NetworkError: API offline")

        reconciler = StateReconciler(exchanges={'future': mock_ex})
        # Clear cooldowns
        if hasattr(StateReconciler, '_last_global_offline_scan'):
            delattr(StateReconciler, '_last_global_offline_scan')
        _pair_key = '_last_pair_scan_SOLUSDC'
        if hasattr(StateReconciler, _pair_key):
            delattr(StateReconciler, _pair_key)

        # Run scan
        reconciler.reconstruct_offline_fills(since_hours=6, pair_filter='SOLUSDC')

        # Check that the order is still in database and status is still 'placing' (not deleted)
        row = self.conn.execute("SELECT * FROM bot_orders WHERE bot_id = ?", (bot_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'placing')

        # Case 2: Order explicitly confirmed NOT on exchange (NotFound exception)
        import ccxt
        mock_ex.fetch_order.side_effect = ccxt.OrderNotFound("Order CQB_12345_GRID_1_1 not found")
        # Ensure fallback doesn't find it either (returns empty list)
        mock_ex.fetch_open_orders.side_effect = None
        mock_ex.fetch_open_orders.return_value = []
        mock_ex.fetch_closed_orders.return_value = []

        if hasattr(StateReconciler, '_last_global_offline_scan'):
            delattr(StateReconciler, '_last_global_offline_scan')
        if hasattr(StateReconciler, _pair_key):
            delattr(StateReconciler, _pair_key)

        reconciler.reconstruct_offline_fills(since_hours=6, pair_filter='SOLUSDC')

        # The placing row should be deleted now
        row = self.conn.execute("SELECT * FROM bot_orders WHERE bot_id = ?", (bot_id,)).fetchone()
        self.assertIsNone(row)

    @patch('engine.bot_executor.get_connection')
    @patch('engine.database.get_connection')
    def test_stale_cancel_buffer(self, mock_db_conn, mock_executor_conn):
        """
        Verifies BotExecutor updates a stale order's status to 'cancelling' and
        deletes or credits it on the subsequent cycle depending on filled_amount.
        """
        mock_db_conn.return_value = self.conn
        mock_executor_conn.return_value = self.conn

        bot_id = 20025
        pair = 'SOL/USDC:USDC'

        # Insert active bot
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status)
            VALUES (?, 'sol long', ?, 'SOLUSDC', 'LONG', 1, 'IN TRADE')
        """, (bot_id, pair))
        # Seed trade state
        self.conn.execute("""
            INSERT INTO trades (bot_id, cycle_id, current_step, total_invested, open_qty, entry_confirmed, position_side)
            VALUES (?, 5, 1, 50.0, 5.0, 1, 'LONG')
        """, (bot_id,))
        self.conn.commit()

        # Seed a stale grid order from previous step (step 1)
        # Note: since current_step is 1, expected_grid_step is 2. Step 1 grid order is stale!
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, amount, filled_amount, status, created_at, order_type, price, step, cycle_id)
            VALUES (?, 'stale_grid_123', 'CQB_20025_GRID_5_1', 5.0, 0.0, 'open', ?, 'grid', 10.0, 1, 5)
        """, (bot_id, int(time.time()) - 200))
        self.conn.commit()

        # Mock the exchange
        mock_ex = MagicMock()
        mock_ex.fetch_open_orders.return_value = [{
            'id': 'stale_grid_123',
            'clientOrderId': 'CQB_20025_GRID_5_1',
            'symbol': pair,
            'status': 'open',
            'filled': 0.0,
            'amount': 5.0,
        }]
        mock_ex.cancel_order.return_value = {'id': 'stale_grid_123', 'status': 'cancelled'}
        mock_ex.fetch_order.return_value = {
            'id': 'stale_grid_123',
            'clientOrderId': 'CQB_20025_GRID_5_1',
            'symbol': pair,
            'status': 'open',
            'filled': 0.0,
            'amount': 5.0,
            'price': 10.0,
            'average': 10.0,
        }
        mock_ex.get_symbol_precision.return_value = {
            'tick_size': 0.01,
            'step_size': 0.001,
            'min_notional': 5.0
        }

        # Mock strategy instance
        mock_strategy = MagicMock()
        mock_strategy.params = {'base_size': 100.0, 'martingale_multiplier': 2.0}
        mock_strategy.max_steps = 10
        mock_strategy.calculate_take_profit_amount.return_value = 0.0
        mock_strategy.calculate_take_profit_price.return_value = 0.0
        mock_strategy.calculate_grid_order_price.return_value = (0.0, 0.0)
        mock_strategy.calculate_grid_order_amount.return_value = 0.0

        # Mock gate_maintain_orders_allowed to return True and strategy to return mock_strategy
        with patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, "")), \
             patch('engine.bot_executor.BotExecutor._get_strategy_instance', return_value=mock_strategy):
            # Instantiate executor
            executor = BotExecutor(runner=None)

            # 1. Run maintenance. The stale order should be cancelled on exchange and status updated to 'cancelling' in DB
            executor.maintain_orders(
                bot_id=bot_id,
                name='sol long',
                pair=pair,
                direction='LONG',
                bot_status={'id': bot_id, 'cycle_id': 5, 'current_step': 1, 'total_invested': 50.0, 'open_qty': 5.0, 'tp_order_id': None},
                current_price=10.0,
                exchange=mock_ex,
                market_snapshot=None,
                bot_config={'base_size': 100.0, 'martingale_multiplier': 2.0}
            )

            # Assert cancel_order was called on the exchange
            mock_ex.cancel_order.assert_called_once_with('stale_grid_123', pair)

            # Assert order status in DB is now 'cancelling'
            row = self.conn.execute("SELECT status, filled_amount FROM bot_orders WHERE order_id = 'stale_grid_123'").fetchone()
            self.assertEqual(row['status'], 'cancelling')
            self.assertEqual(row['filled_amount'], 0.0)

            # 2. Subsequent cycle - Case A: Order is successfully cancelled (no longer in open orders, filled_amount=0)
            mock_ex.fetch_open_orders.return_value = []  # No longer open on exchange
            mock_ex.cancel_order.reset_mock()
            # BUG 4 FIX: fetch_order must confirm the cancellation; update mock to return 'cancelled'
            mock_ex.fetch_order.return_value = {
                'id': 'stale_grid_123',
                'clientOrderId': 'CQB_20025_GRID_5_1',
                'symbol': pair,
                'status': 'cancelled',
                'filled': 0.0,
                'amount': 5.0,
                'price': 10.0,
                'average': 10.0,
            }

            executor.maintain_orders(
                bot_id=bot_id,
                name='sol long',
                pair=pair,
                direction='LONG',
                bot_status={'id': bot_id, 'cycle_id': 5, 'current_step': 1, 'total_invested': 50.0, 'open_qty': 5.0, 'tp_order_id': None},
                current_price=10.0,
                exchange=mock_ex,
                market_snapshot=None,
                bot_config={'base_size': 100.0, 'martingale_multiplier': 2.0}
            )

            # The order row should be deleted from DB
            row = self.conn.execute("SELECT * FROM bot_orders WHERE order_id = 'stale_grid_123'").fetchone()
            self.assertIsNone(row)

            # 3. Subsequent cycle - Case B: Order was filled during the window (filled_amount > 0)
            # Seed the order back as 'cancelling' but with filled_amount = 5.0 (updated via WS fill)
            self.conn.execute("DELETE FROM bot_orders WHERE order_id = 'stale_grid_123'")
            self.conn.execute("""
                INSERT INTO bot_orders (bot_id, order_id, client_order_id, amount, filled_amount, status, created_at, order_type, price, step, cycle_id)
                VALUES (?, 'stale_grid_123', 'CQB_20025_GRID_5_1', 5.0, 5.0, 'cancelling', ?, 'grid', 10.0, 1, 5)
            """, (bot_id, int(time.time()) - 200))
            self.conn.commit()

            with patch('engine.ledger.credit_fill') as mock_credit_fill, \
                 patch('engine.ledger.seal_trade_state') as mock_seal:

                executor.maintain_orders(
                    bot_id=bot_id,
                    name='sol long',
                    pair=pair,
                    direction='LONG',
                    bot_status={'id': bot_id, 'cycle_id': 5, 'current_step': 1, 'total_invested': 50.0, 'open_qty': 5.0, 'tp_order_id': None},
                    current_price=10.0,
                    exchange=mock_ex,
                    market_snapshot=None,
                    bot_config={'base_size': 100.0, 'martingale_multiplier': 2.0}
                )

                # Verify credit_fill was called to attribute the fill
                mock_credit_fill.assert_called_once_with(
                    bot_id=bot_id,
                    order_id='stale_grid_123',
                    cumulative_qty=5.0,
                    avg_price=10.0,
                    order_type='grid',
                    is_cumulative=True,
                    caller='cancel_verify',
                )
                mock_seal.assert_called_once_with(bot_id)

                # Order status in DB should be updated to 'filled'
                row = self.conn.execute("SELECT status, filled_amount FROM bot_orders WHERE order_id = 'stale_grid_123'").fetchone()
                self.assertEqual(row['status'], 'filled')
                self.assertEqual(row['filled_amount'], 5.0)

if __name__ == '__main__':
    unittest.main()
