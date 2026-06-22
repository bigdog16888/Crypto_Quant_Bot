"""
Unit and Integration Tests for v3.9.0 Fixes:
1. get_authoritative_close_qty abstraction
2. _reset_to_hedge_standby skips and idempotency guard
3. purge_phantom_ledger_when_exchange_flat cancel and logging
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
from engine.database import get_connection, init_db, save_bot_order
from engine.bot_executor import _reset_to_hedge_standby
from engine.parity_gates import purge_phantom_ledger_when_exchange_flat
from engine.oneway_netting import get_authoritative_close_qty


def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_v390.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path


def _insert_bot(conn, bot_id, name, pair, norm_pair, direction,
                status='IN TRADE', bot_type='standard', is_active=1,
                parent_bot_id=None, hedge_child_bot_id=None, hedge_trigger_step=None):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction,
                          status, bot_type, is_active, parent_bot_id,
                          hedge_child_bot_id, hedge_trigger_step,
                          rsi_limit, martingale_multiplier, base_size, strategy_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 0, 'Martingale')
    """, (bot_id, name, pair, norm_pair, direction,
          status, bot_type, is_active, parent_bot_id,
          hedge_child_bot_id, hedge_trigger_step))
    conn.commit()


def _insert_trades(conn, bot_id, open_qty=0.0,
                   cycle_id=1, position_side='LONG', avg_entry_price=0.0):
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step, entry_confirmed)
        VALUES (?, ?, ?, ?, 0, ?, 1, 1)
    """, (bot_id, open_qty, cycle_id, position_side, avg_entry_price))
    conn.commit()


class TestV390Fixes(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_get_authoritative_close_qty_direction_logic(self):
        class MockExchange:
            def __init__(self, signed_net):
                self.signed_net = signed_net

            def fetch_positions(self):
                return [{'symbol': 'BTC/USDC:USDC', 'contracts': self.signed_net, 'net_qty': self.signed_net, 'side': 'long' if self.signed_net > 0 else 'short'}]

        # Case 1: Long, positive net -> returns min of db_qty and physical_net
        ex = MockExchange(0.3)
        self.assertEqual(get_authoritative_close_qty(ex, 'BTC/USDC:USDC', 'LONG', 0.5), 0.3)
        self.assertEqual(get_authoritative_close_qty(ex, 'BTC/USDC:USDC', 'LONG', 0.1), 0.1)

        # Case 2: Long, negative net -> returns 0
        ex_short = MockExchange(-0.3)
        self.assertEqual(get_authoritative_close_qty(ex_short, 'BTC/USDC:USDC', 'LONG', 0.5), 0.0)

        # Case 3: Short, negative net -> returns min of db_qty and abs(physical_net)
        self.assertEqual(get_authoritative_close_qty(ex_short, 'BTC/USDC:USDC', 'SHORT', 0.5), 0.3)
        self.assertEqual(get_authoritative_close_qty(ex_short, 'BTC/USDC:USDC', 'SHORT', 0.2), 0.2)

        # Case 4: Short, positive net -> returns 0
        self.assertEqual(get_authoritative_close_qty(ex, 'BTC/USDC:USDC', 'SHORT', 0.5), 0.0)

    def test_reset_to_hedge_standby_skip_phase1_when_flat(self):
        # Parent bot
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', hedge_child_bot_id=100322, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=10)

        # Child bot (SHORT) with db open_qty = 0.345
        _insert_bot(self.conn, 100322, 'btc long_hedge', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100322, open_qty=0.345, cycle_id=5, position_side='SHORT')

        # Mock exchange position = 0 (flat)
        class MockExchangeFlat:
            def fetch_positions(self):
                return []  # No open positions -> flat

            def create_order(self, *args, **kwargs):
                pass

            def cancel_orders_by_bot_id(self, *args, **kwargs):
                pass

        ex = MockExchangeFlat()

        with patch.object(ex, 'create_order') as mock_create, \
             patch.object(ex, 'cancel_orders_by_bot_id') as mock_cancel, \
             patch('engine.exchange_interface.ExchangeInterface.fetch_positions', return_value=[]):
            
            _reset_to_hedge_standby(100322, self.conn, 10, exchange=ex)
            
            # Since exchange is flat, Phase 1 close order placement must be skipped!
            mock_create.assert_not_called()
            mock_cancel.assert_not_called()

            # Status should be updated to standby
            status = self.conn.execute("SELECT status FROM bots WHERE id=100322").fetchone()[0]
            self.assertEqual(status, 'hedge_standby')

            # db open_qty should be zeroed
            oq = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100322").fetchone()[0]
            self.assertEqual(oq, 0.0)

    def test_reset_to_hedge_standby_idempotency_guard(self):
        # Parent bot
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', hedge_child_bot_id=100322, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=10)

        # Child bot
        _insert_bot(self.conn, 100322, 'btc long_hedge', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100322, open_qty=0.345, cycle_id=10, position_side='SHORT')

        # Seed an in-flight 'reset_close' order from less than 10 seconds ago
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, created_at, updated_at)
            VALUES (100322, 'reset_close', 'CQB_100322_RESET_CLOSE_inflight', 0.0, 0.345, 0.0, 'open', 10, ?, ?)
        """, (int(time.time()) - 10, int(time.time()) - 10))
        self.conn.commit()

        class MockExchangeWithPosition:
            def fetch_positions(self):
                return [{'symbol': 'BTC/USDC:USDC', 'contracts': -0.345, 'net_qty': -0.345, 'side': 'short'}]

            def create_order(self, *args, **kwargs):
                pass

            def cancel_orders_by_bot_id(self, *args, **kwargs):
                pass

        ex = MockExchangeWithPosition()

        with patch.object(ex, 'create_order') as mock_create, \
             patch('engine.exchange_interface.ExchangeInterface.fetch_positions', return_value=[]):
            _reset_to_hedge_standby(100322, self.conn, 10, exchange=ex)
            
            # Should skip Phase 1 order placement because of the in-flight check!
            mock_create.assert_not_called()

    def test_purge_phantom_ledger_cancels_and_logs(self):
        # Standard bot with some open_qty
        _insert_bot(self.conn, 10050, 'sui bot', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', status='REQUIRE_MANUAL_PROOF')
        _insert_trades(self.conn, 10050, open_qty=10.0, cycle_id=2, position_side='LONG')

        class MockExchangeWithOrders:
            def fetch_open_orders(self, symbol):
                return [
                    {'id': 'order_1', 'clientOrderId': 'CQB_10050_TP_2_1', 'symbol': 'SUI/USDC:USDC'},
                    {'id': 'order_2', 'clientOrderId': 'CQB_10050_GRID_2_2', 'symbol': 'SUI/USDC:USDC'},
                    {'id': 'order_other', 'clientOrderId': 'CQB_999_GRID_2_2', 'symbol': 'SUI/USDC:USDC'}
                ]
            
            def cancel_orders_by_bot_id(self, bot_id, symbol):
                return 2

        ex = MockExchangeWithOrders()

        with patch('engine.database.safe_wipe_bot', return_value=True) as mock_wipe:
            ok, msg = purge_phantom_ledger_when_exchange_flat(ex, 'SUI/USDC:USDC', 10.0, 0.0)
            self.assertTrue(ok)
            self.assertIn("purged bots", msg)

            mock_wipe.assert_called_once_with(
                10050, 'SUI/USDC:USDC', 'LONG', reason='PHANTOM_LEDGER_PURGE', force=True, human_approved=True
            )

            # Check that ghost_order_cancel was written with the expected CIDs list
            audit_row = self.conn.execute(
                "SELECT notes FROM bot_orders WHERE bot_id = 10050 AND order_type = 'ghost_order_cancel'"
            ).fetchone()
            self.assertIsNotNone(audit_row)
            self.assertIn("CQB_10050_TP_2_1, CQB_10050_GRID_2_2", audit_row[0])


if __name__ == '__main__':
    unittest.main()
