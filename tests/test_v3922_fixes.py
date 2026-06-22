"""
Unit tests for v3.9.22 fixes:
  1. test_gated_bots_participate_in_netting:
     Verify that a neighbor bot in 'require_manual_proof' status is NOT skipped
     during cross-reduction (is included in neighbor list and reduced).
  2. test_apply_oneway_entry_cross_reduction_bidirectional:
     Verify that a cross-reduction writes a virtual netting row on the neighbor bot AND the filling bot.
  3. test_virtual_netting_exclusion_from_verification_and_sync:
     Ensure virtual_netting orders are excluded from verify_filled_orders_against_exchange
     and sync_stale_open_orders queries.
"""

import os
import sys
import time
import tempfile
import shutil
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db, save_bot_order, verify_filled_orders_against_exchange
from engine.bot_executor import sync_stale_open_orders
from engine.oneway_netting import apply_oneway_entry_cross_reduction

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_v3922.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

def _insert_bot(conn, bot_id, name, pair, norm_pair, direction,
                status='IN TRADE', bot_type='standard', parent_bot_id=None, is_active=1):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status,
                          bot_type, parent_bot_id, is_active, rsi_limit, martingale_multiplier,
                          base_size, strategy_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 100, 'Martingale')
    """, (bot_id, name, pair, norm_pair, direction, status, bot_type, parent_bot_id, is_active))
    conn.commit()

def _insert_trades(conn, bot_id, open_qty=1.0, cycle_id=1, position_side='LONG',
                   avg_entry_price=60000.0, total_invested=60000.0,
                   target_tp_price=61000.0, basket_start_time=None, current_step=1):
    if basket_start_time is None:
        basket_start_time = int(time.time()) - 3600
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step,
                            entry_confirmed, target_tp_price, basket_start_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (bot_id, open_qty, cycle_id, position_side, total_invested,
          avg_entry_price, current_step, target_tp_price, basket_start_time))
    conn.commit()


class TestV3922Fixes(unittest.TestCase):
    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if hasattr(database._local, 'connection') and database._local.connection:
            try:
                database._local.connection.close()
            except Exception:
                pass
            database._local.connection = None

    def test_gated_bots_participate_in_netting(self):
        """
        Verify that a neighbor bot in 'require_manual_proof' status is NOT skipped
        during cross-reduction (is included in neighbor list and reduced).
        """
        # Bot A (LONG, status='require_manual_proof', gated but has position)
        _insert_bot(self.conn, 10016, 'long btc price', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='require_manual_proof')
        _insert_trades(self.conn, 10016, open_qty=0.050, cycle_id=36, position_side='LONG')

        # Bot B (SHORT, active bot that fills entry)
        _insert_bot(self.conn, 10022, 'short btc', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', status='IN TRADE')
        _insert_trades(self.conn, 10022, open_qty=0.010, cycle_id=35, position_side='SHORT')

        # Run cross reduction for a SHORT fill of 0.015
        with patch('engine.ledger.seal_trade_state') as mock_seal:
            cut = apply_oneway_entry_cross_reduction(
                filling_bot_id=10022,
                pair='BTC/USDC:USDC',
                direction='SHORT',
                delta=0.015,
                source_order_id='EX_SHORT_FILL_123',
                avg_price=61636.0
            )

            # Confirm cross-reduction occurred and cut was applied
            self.assertEqual(cut, 0.015)

            # Verify that virtual_netting orders were written for both bots
            # Bot 10016 (neighbor bot) virtual_netting exit
            nb_vns = self.conn.execute(
                "SELECT price, amount, status, client_order_id, cycle_id FROM bot_orders WHERE bot_id = 10016 AND order_type = 'virtual_netting'"
            ).fetchall()
            self.assertEqual(len(nb_vns), 1)
            self.assertEqual(nb_vns[0][1], 0.015)
            self.assertEqual(nb_vns[0][2], 'filled')
            self.assertEqual(nb_vns[0][4], 36)

            # Bot 10022 (filling bot) virtual_netting exit
            fill_vns = self.conn.execute(
                "SELECT price, amount, status, client_order_id, cycle_id FROM bot_orders WHERE bot_id = 10022 AND order_type = 'virtual_netting'"
            ).fetchall()
            self.assertEqual(len(fill_vns), 1)
            self.assertEqual(fill_vns[0][1], 0.015)
            self.assertEqual(fill_vns[0][2], 'filled')
            self.assertEqual(fill_vns[0][4], 35)

            # Verify seal_trade_state was called on both bots to recompute open_qty
            mock_seal.assert_any_call(10016, force_recompute=True)
            mock_seal.assert_any_call(10022, force_recompute=True)

    def test_apply_oneway_entry_cross_reduction_bidirectional(self):
        """
        Verify that a cross-reduction writes a virtual netting row on the neighbor bot AND the filling bot.
        """
        # Bot A (LONG, active)
        _insert_bot(self.conn, 10016, 'long btc price', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='IN TRADE')
        _insert_trades(self.conn, 10016, open_qty=0.030, cycle_id=36, position_side='LONG')

        # Bot B (SHORT, active bot that fills entry)
        _insert_bot(self.conn, 10022, 'short btc', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', status='IN TRADE')
        _insert_trades(self.conn, 10022, open_qty=0.0, cycle_id=35, position_side='SHORT')

        # Run cross reduction for a SHORT fill of 0.010
        with patch('engine.ledger.seal_trade_state') as mock_seal:
            cut = apply_oneway_entry_cross_reduction(
                filling_bot_id=10022,
                pair='BTC/USDC:USDC',
                direction='SHORT',
                delta=0.010,
                source_order_id='EX_SHORT_FILL_123',
                avg_price=61636.0
            )

            self.assertEqual(cut, 0.010)

            # Check bot_orders for both bots
            # Bot 10016 (neighbor bot) virtual_netting exit
            nb_order = self.conn.execute(
                "SELECT bot_id, amount, status, cycle_id FROM bot_orders WHERE bot_id = 10016 AND order_type = 'virtual_netting'"
            ).fetchone()
            self.assertIsNotNone(nb_order)
            self.assertEqual(nb_order[1], 0.010)
            self.assertEqual(nb_order[2], 'filled')
            self.assertEqual(nb_order[3], 36)

            # Bot 10022 (filling bot) virtual_netting exit
            fill_order = self.conn.execute(
                "SELECT bot_id, amount, status, cycle_id FROM bot_orders WHERE bot_id = 10022 AND order_type = 'virtual_netting'"
            ).fetchone()
            self.assertIsNotNone(fill_order)
            self.assertEqual(fill_order[1], 0.010)
            self.assertEqual(fill_order[2], 'filled')
            self.assertEqual(fill_order[3], 35)

    def test_virtual_netting_exclusion_from_verification_and_sync(self):
        """
        Ensure virtual_netting orders are excluded from verify_filled_orders_against_exchange
        and sync_stale_open_orders queries.
        """
        # Insert a bot
        _insert_bot(self.conn, 10016, 'long btc price', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', status='IN TRADE')
        _insert_trades(self.conn, 10016, open_qty=0.030, cycle_id=36, position_side='LONG')

        # Insert a virtual_netting order with status 'filled' and client_order_id CQB_...
        save_bot_order(
            10016, 'virtual_netting', 'VN_10016_TEST', price=60000.0, amount=0.01, step=0,
            status='filled', client_order_id='CQB_10016_VNET_TEST', cycle_id=36
        )
        # update filled_amount as well
        self.conn.execute(
            "UPDATE bot_orders SET filled_amount = 0.01 WHERE client_order_id = 'CQB_10016_VNET_TEST'"
        )
        self.conn.commit()

        # Insert a stale open virtual_netting order to test sync_stale_open_orders query
        # We write it with created_at way back in the past
        past_time = int(time.time()) - 7200
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (10016, 'virtual_netting', 'VN_STALE_OPEN', 'CQB_10016_VNET_STALE', 60000.0, 0.01, 'open', 0, 36, ?)
        """, (past_time,))
        self.conn.commit()

        # Mock exchange API
        mock_exchange = MagicMock()

        # 1. Run verify_filled_orders_against_exchange
        verify_filled_orders_against_exchange(mock_exchange, bot_id=10016)
        # Since virtual_netting is excluded, exchange.fetch_order or fetch_order_by_client_order_id should NOT be called.
        mock_exchange.fetch_order.assert_not_called()
        mock_exchange.fetch_order_by_client_order_id.assert_not_called()

        # 2. Run sync_stale_open_orders
        sync_stale_open_orders(10016, mock_exchange, self.conn, max_age_seconds=3600)
        # Since virtual_netting is excluded, exchange.fetch_order should NOT be called for VN_STALE_OPEN.
        mock_exchange.fetch_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
