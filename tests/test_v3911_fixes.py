"""
Unit and integration tests for v3.9.11 fixes:
1. BUG 1: positionSide in handle_flatten (ledger.py)
2. BUG 2: fill_claims idempotency table (INV-20)
3. BUG 3: Stuck 'placing' status (reconciler.py)
4. BUG 4: Cancelling buffer zero-fill delete verification (bot_executor.py)
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
from engine.ledger import handle_flatten, credit_fill
from engine.reconciler import StateReconciler
from engine.bot_executor import BotExecutor

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_v3911.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

def _insert_bot(conn, bot_id, name, pair, norm_pair, direction, status='Scanning', bot_type='standard', is_active=1):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type, is_active,
                          rsi_limit, martingale_multiplier, base_size, strategy_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 0, 'Martingale')
    """, (bot_id, name, pair, norm_pair, direction, status, bot_type, is_active))
    conn.commit()

def _insert_trades(conn, bot_id, open_qty=0.0, cycle_id=1, position_side='LONG', avg_entry_price=0.0, total_invested=0.0):
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step, entry_confirmed)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1)
    """, (bot_id, open_qty, cycle_id, position_side, total_invested, avg_entry_price))
    conn.commit()


class TestV3911Fixes(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if hasattr(database._local, 'connection') and database._local.connection:
            try:
                database._local.connection.close()
            except Exception:
                pass
            database._local.connection = None

    def test_bug1_handle_flatten_params_mainnet(self):
        """
        BUG 1: Check that positionSide is NOT included in params passed to create_order on mainnet.
        """
        bot_id = 10001
        pair = 'BTC/USDC:USDC'
        norm_pair = 'BTCUSDC'

        _insert_bot(self.conn, bot_id, 'btc long', pair, norm_pair, 'LONG')
        _insert_trades(self.conn, bot_id, open_qty=0.1, cycle_id=1, position_side='LONG', avg_entry_price=60000.0, total_invested=6000.0)

        # Mock exchange as mainnet (is_testnet = False)
        mock_ex = MagicMock()
        mock_ex.is_testnet = False
        mock_ex.exchange = MagicMock()
        mock_ex.exchange.sandbox = False
        mock_ex.fetch_open_orders.return_value = []
        mock_ex.create_order.return_value = {'id': 'close_order_id', 'price': 60100.0, 'average': 60100.0}

        success = handle_flatten(bot_id, pair, mock_ex, reason='FORCE_SL')
        self.assertTrue(success)

        # Ensure create_order was called and positionSide is NOT in the params dict
        mock_ex.create_order.assert_called_once()
        args, kwargs = mock_ex.create_order.call_args
        params = kwargs.get('params', {})
        self.assertNotIn('positionSide', params)
        self.assertEqual(params.get('reduceOnly'), True)

    def test_bug1_handle_flatten_params_testnet(self):
        """
        BUG 1: Check that positionSide='BOTH' is included in params passed to create_order on testnet.
        """
        bot_id = 10002
        pair = 'BTC/USDC:USDC'
        norm_pair = 'BTCUSDC'

        _insert_bot(self.conn, bot_id, 'btc long', pair, norm_pair, 'LONG')
        _insert_trades(self.conn, bot_id, open_qty=0.1, cycle_id=1, position_side='LONG', avg_entry_price=60000.0, total_invested=6000.0)

        # Mock exchange as testnet (is_testnet = True)
        mock_ex = MagicMock()
        mock_ex.is_testnet = True
        mock_ex.fetch_open_orders.return_value = []
        mock_ex.create_order.return_value = {'id': 'close_order_id', 'price': 60100.0, 'average': 60100.0}

        success = handle_flatten(bot_id, pair, mock_ex, reason='FORCE_SL')
        self.assertTrue(success)

        # Ensure create_order was called and positionSide='BOTH' is in the params dict
        mock_ex.create_order.assert_called_once()
        args, kwargs = mock_ex.create_order.call_args
        params = kwargs.get('params', {})
        self.assertEqual(params.get('positionSide'), 'BOTH')
        self.assertEqual(params.get('reduceOnly'), True)

    def test_bug2_fill_claims_migration_exists_and_idempotent(self):
        """
        BUG 2: Check that migration_003_fill_claims can run idempotently.
        """
        from engine.migrations.migration_003_fill_claims import run as run_migration
        
        # Test runs successfully on the temp db
        run_migration(self.db_path)
        
        # Verify table exists in SQLite schema
        cursor = self.conn.cursor()
        res = cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fill_claims'").fetchone()
        self.assertIsNotNone(res)

        # Verify running again does not throw or raise errors (idempotency check)
        try:
            run_migration(self.db_path)
        except Exception as e:
            self.fail(f"Second run of migration_003 raised exception: {e}")

    def test_bug2_credit_fill_singleton_guard(self):
        """
        BUG 2: Verify that credit_fill is a singleton per (bot_id, order_id) using fill_claims.
        """
        bot_id = 10003
        order_id = 'ex_order_unique_999'

        _insert_bot(self.conn, bot_id, 'btc long', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, bot_id, open_qty=0.0, cycle_id=1, position_side='LONG')

        # Insert a bot order row to credit against
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, order_type)
            VALUES (?, ?, 'CQB_10003_GRID_1_1', 60000.0, 1.0, 0.0, 'open', 1, 1, 'grid')
        """, (bot_id, order_id))
        self.conn.commit()

        # Run migration 3 to ensure fill_claims is ready
        from engine.migrations.migration_003_fill_claims import run as run_migration
        run_migration(self.db_path)

        # Call credit_fill first time -> should succeed and return True
        res1 = credit_fill(
            bot_id=bot_id,
            order_id=order_id,
            cumulative_qty=1.0,
            avg_price=60000.0,
            order_type='grid',
            caller='test_caller_1'
        )
        self.assertTrue(res1)

        # Call credit_fill second time -> should be blocked by fill_claims guard and return False
        res2 = credit_fill(
            bot_id=bot_id,
            order_id=order_id,
            cumulative_qty=1.0,
            avg_price=60000.0,
            order_type='grid',
            caller='test_caller_2'
        )
        self.assertFalse(res2)

        # Verify claim details in database
        claim = self.conn.execute("SELECT * FROM fill_claims WHERE bot_id=? AND order_id=?", (bot_id, order_id)).fetchone()
        self.assertIsNotNone(claim)
        self.assertEqual(claim['caller'], 'test_caller_1')

    def test_bug3_reconstruct_offline_fills_unconditional_status_update(self):
        """
        BUG 3: Verify that reconstruct_offline_fills unconditionally updates the order status in the DB
        even if credit_fill returns False (due to fill_claims collision or MAX() protection).
        """
        bot_id = 10004
        order_id = 'ex_order_10004'
        cid = 'CQB_10004_GRID_1_1'

        _insert_bot(self.conn, bot_id, 'btc long', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, bot_id, open_qty=0.0, cycle_id=1, position_side='LONG')

        # Insert a placing order row
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, order_type, created_at)
            VALUES (?, ?, ?, 60000.0, 1.0, 0.0, 'placing', 1, 1, 'grid', ?)
        """, (bot_id, order_id, cid, int(time.time())))
        self.conn.commit()

        # Mock exchange to return order filled
        mock_ex = MagicMock()
        mock_ex.fetch_order.return_value = {
            'id': order_id,
            'clientOrderId': cid,
            'status': 'closed',
            'filled': 1.0,
            'price': 60000.0,
            'average': 60000.0,
        }
        mock_ex.fetch_positions.return_value = []

        # Clear cooldowns to force scan
        if hasattr(StateReconciler, '_last_global_offline_scan'):
            delattr(StateReconciler, '_last_global_offline_scan')
        _pair_key = '_last_pair_scan_BTCUSDC'
        if hasattr(StateReconciler, _pair_key):
            delattr(StateReconciler, _pair_key)

        reconciler = StateReconciler(exchanges={'future': mock_ex})

        # Mock credit_fill to return False (simulating a collision/no-op)
        with patch('engine.ledger.credit_fill', return_value=False) as mock_credit:
            reconciler.reconstruct_offline_fills(since_hours=6, pair_filter='BTCUSDC')
            
            mock_credit.assert_called_once()
            
            # The status should still be updated to 'filled' (promoting it out of placing)
            row = self.conn.execute("SELECT status FROM bot_orders WHERE id = (SELECT id FROM bot_orders LIMIT 1)").fetchone()
            self.assertEqual(row['status'], 'filled')

    def test_bug4_cancel_buffer_verify_before_delete(self):
        """
        BUG 4: Verifies the stale cancel buffer re-verifies with the exchange before deleting a zero-fill row.
        """
        bot_id = 10005
        pair = 'BTC/USDC:USDC'

        _insert_bot(self.conn, bot_id, 'btc long', pair, 'BTCUSDC', 'LONG', status='IN TRADE')
        _insert_trades(self.conn, bot_id, open_qty=1.0, cycle_id=1, position_side='LONG', avg_entry_price=60000.0, total_invested=6000.0)

        # Seed a cancelling order (representing a stale order cancel initiated in previous cycle)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, order_type)
            VALUES (?, 'order_c4', 'CQB_10005_GRID_1_1', 60000.0, 1.0, 0.0, 'cancelling', 1, 1, 'grid')
        """, (bot_id,))
        self.conn.commit()

        mock_ex = MagicMock()
        mock_ex.fetch_open_orders.return_value = [] # Order is no longer open
        mock_ex.get_symbol_precision.return_value = {'tick_size': 0.01, 'step_size': 0.001, 'min_notional': 5.0}
        mock_ex.get_best_bid_ask.return_value = (60000.0, 60001.0)

        # Case 1: Exchange reports the order filled (filled=1.0) despite DB showing 0
        mock_ex.fetch_order.return_value = {
            'id': 'order_c4',
            'clientOrderId': 'CQB_10005_GRID_1_1',
            'status': 'closed',
            'filled': 1.0,
            'price': 60000.0,
            'average': 60000.0
        }

        mock_strategy = MagicMock()
        mock_strategy.params = {'base_size': 100.0, 'martingale_multiplier': 2.0}
        mock_strategy.max_steps = 10
        mock_strategy.calculate_take_profit_price.return_value = 0.0
        mock_strategy.calculate_take_profit_amount.return_value = 0.0

        with patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, "")), \
             patch('engine.bot_executor.BotExecutor._get_strategy_instance', return_value=mock_strategy), \
             patch('engine.ledger.credit_fill') as mock_credit_fill, \
             patch('engine.ledger.seal_trade_state') as mock_seal:

            executor = BotExecutor(runner=None)
            executor.maintain_orders(
                bot_id=bot_id,
                name='btc long',
                pair=pair,
                direction='LONG',
                bot_status={'id': bot_id, 'cycle_id': 1, 'current_step': 1, 'total_invested': 6000.0, 'open_qty': 1.0, 'tp_order_id': None},
                current_price=60000.0,
                exchange=mock_ex,
                market_snapshot=None,
                bot_config={'base_size': 100.0, 'martingale_multiplier': 2.0}
            )

            # Assert credit_fill called with the late exchange fill
            mock_credit_fill.assert_called_once_with(
                bot_id=bot_id,
                order_id='order_c4',
                cumulative_qty=1.0,
                avg_price=60000.0,
                order_type='grid',
                is_cumulative=True,
                caller='cancel_verify'
            )
            mock_seal.assert_called_once_with(bot_id)

            # Order status updated to 'filled' in DB, NOT deleted
            row = self.conn.execute("SELECT status FROM bot_orders WHERE order_id = 'order_c4'").fetchone()
            self.assertEqual(row['status'], 'filled')

        # Case 2: Exchange confirms order is truly cancelled with 0 fill
        # Seed order status back to 'cancelling' and filled_amount=0.0
        self.conn.execute("UPDATE bot_orders SET status='cancelling', filled_amount=0.0 WHERE order_id='order_c4'")
        self.conn.commit()

        mock_ex.fetch_order.return_value = {
            'id': 'order_c4',
            'clientOrderId': 'CQB_10005_GRID_1_1',
            'status': 'cancelled',
            'filled': 0.0,
            'price': 60000.0,
            'average': 60000.0
        }

        with patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, "")), \
             patch('engine.bot_executor.BotExecutor._get_strategy_instance', return_value=mock_strategy):

            executor = BotExecutor(runner=None)
            executor.maintain_orders(
                bot_id=bot_id,
                name='btc long',
                pair=pair,
                direction='LONG',
                bot_status={'id': bot_id, 'cycle_id': 1, 'current_step': 1, 'total_invested': 6000.0, 'open_qty': 1.0, 'tp_order_id': None},
                current_price=60000.0,
                exchange=mock_ex,
                market_snapshot=None,
                bot_config={'base_size': 100.0, 'martingale_multiplier': 2.0}
            )

            # Row should be deleted
            row = self.conn.execute("SELECT * FROM bot_orders WHERE order_id = 'order_c4'").fetchone()
            self.assertIsNone(row)


if __name__ == '__main__':
    unittest.main()
