"""
Unit tests for ADR-004 Position Lifecycle Integrity:
1. Item 1: TP reset must verify all grid positions are closed before wiping cycle (PARTIAL_CLOSE_PENDING phase).
2. Item 2: TP replacement must use remaining qty (trades.open_qty) not original qty.
3. Item 3: Hedge child TP qty must use virtual open_qty (trades.open_qty) not exchange physical.
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
import engine.ledger as ledger
from engine.database import get_connection, init_db, save_bot_order, get_bot_status
from engine.bot_executor import BotExecutor

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_adr004.db')
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
                   target_tp_price=61000.0, basket_start_time=None, current_step=1, cycle_phase='ACTIVE'):
    if basket_start_time is None:
        basket_start_time = int(time.time()) - 3600
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step,
                            entry_confirmed, target_tp_price, basket_start_time, cycle_phase)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
    """, (bot_id, open_qty, cycle_id, position_side, total_invested,
          avg_entry_price, current_step, target_tp_price, basket_start_time, cycle_phase))
    conn.commit()


class TestADR004PositionLifecycle(unittest.TestCase):
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

    def test_item1_tp_reset_requires_zero_qty_otherwise_partial_close_pending(self):
        """
        Verify that handle_tp_completion transitions the bot to PARTIAL_CLOSE_PENDING
        and places a market close order if open_qty > 0.0001 after TP hit.
        """
        bot_id = 10050
        _insert_bot(self.conn, bot_id, 'test_bot', 'BTC/USDT', 'BTCUSDT', 'LONG')
        _insert_trades(self.conn, bot_id, open_qty=0.05, cycle_id=1, position_side='LONG', cycle_phase='ACTIVE')

        # Insert some entry fills in bot_orders so seal_trade_state registers a positive open_qty
        save_bot_order(bot_id, 'entry', 'order_entry_1', price=60000.0, amount=0.05, step=1, status='filled', client_order_id='CQB_10050_ENTRY_1', cycle_id=1)
        self.conn.execute("UPDATE bot_orders SET filled_amount = 0.05 WHERE client_order_id = 'CQB_10050_ENTRY_1'")
        self.conn.commit()

        # Mock exchange interface
        mock_exchange = MagicMock()
        mock_exchange.is_testnet = True
        mock_exchange.fetch_positions.return_value = [{'symbol': 'BTCUSDT', 'contracts': 0.05, 'side': 'long'}]
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.create_order.return_value = {'id': 'close_order_id_123', 'status': 'filled'}

        # Execute handle_tp_completion
        with patch('engine.database.reset_bot_after_tp') as mock_reset:
            # We expect handle_tp_completion to return False because bot reset is deferred
            res = ledger.handle_tp_completion(
                bot_id=bot_id,
                exit_price=61000.0,
                pair='BTC/USDT',
                exchange=mock_exchange,
                cycle_id=1
            )
            self.assertFalse(res)
            mock_reset.assert_not_called()

            # Verify that the cycle phase transitioned to PARTIAL_CLOSE_PENDING
            status = get_bot_status(bot_id)
            self.assertEqual(status['cycle_phase'], 'PARTIAL_CLOSE_PENDING')

            # Verify close order was placed on exchange
            mock_exchange.create_order.assert_called_once()
            args, kwargs = mock_exchange.create_order.call_args
            self.assertEqual(args[0], 'BTCUSDT')
            self.assertEqual(args[1], 'market')
            self.assertEqual(args[2], 'sell')
            self.assertEqual(args[3], 0.05)
            self.assertTrue(kwargs.get('params', {}).get('reduceOnly'))

            # Verify close order registered in bot_orders table
            orders = self.conn.execute(
                "SELECT status, order_type, amount FROM bot_orders WHERE bot_id = ? AND order_type = 'close'",
                (bot_id,)
            ).fetchall()
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0][0], 'filled')
            self.assertEqual(orders[0][2], 0.05)

        # Now test that maintain_orders will reset the bot if open_qty reaches 0
        # First, mock credit_fill to record the close order fill
        ledger.credit_fill(
            bot_id=bot_id,
            order_id='close_order_id_123',
            cumulative_qty=0.05,
            avg_price=61000.0,
            order_type='close',
            is_cumulative=True
        )

        # Run maintain_orders
        executor = BotExecutor(runner=None)
        status_before = get_bot_status(bot_id)
        self.assertEqual(status_before['open_qty'], 0.0) # Accumulator correctly decremented to 0

        with patch('engine.database.reset_bot_after_tp') as mock_reset_final:
            executor.maintain_orders(
                bot_id=bot_id,
                name='test_bot',
                pair='BTC/USDT',
                direction='LONG',
                bot_status=status_before,
                current_price=61000.0,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config={}
            )
            mock_reset_final.assert_called_once_with(
                bot_id=bot_id,
                exit_price=61000.0,
                action_label='TP_HIT',
                notes='Partial close settled. Resetting bot.',
                exchange=mock_exchange
            )

    def test_item2_sync_replace_tp_uses_remaining_qty_unconditionally(self):
        """
        Verify that _sync_replace_tp unconditionally reads trades.open_qty
        and uses it as the replacement quantity.
        """
        bot_id = 10051
        _insert_bot(self.conn, bot_id, 'test_bot_2', 'BTC/USDT', 'BTCUSDT', 'LONG')
        # We start with open_qty = 0.4 (WS/credit_fill already updated it after a partial fill)
        _insert_trades(self.conn, bot_id, open_qty=0.4, cycle_id=1, position_side='LONG', total_invested=24000.0)

        # Existing TP order in DB (originally size 1.0, but partially filled 0.6)
        save_bot_order(bot_id, 'tp', 'tp_order_ex', price=61000.0, amount=1.0, step=0, status='open', client_order_id='CQB_10051_TP_1', cycle_id=1)
        self.conn.execute(
            "UPDATE bot_orders SET filled_amount = 0.6, created_at = ?, updated_at = ? WHERE client_order_id = 'CQB_10051_TP_1'",
            (int(time.time()) - 30, int(time.time()) - 30)
        )
        self.conn.commit()

        # Mock exchange API
        mock_exchange = MagicMock()
        mock_exchange.is_testnet = True
        mock_exchange.cancel_order.return_value = {
            'id': 'tp_order_ex',
            'status': 'canceled',
            'filled': 0.6,
            'amount': 1.0,
            'average': 61000.0
        }
        mock_exchange.fetch_order.return_value = {
            'id': 'tp_order_ex',
            'status': 'canceled',
            'filled': 0.6,
            'amount': 1.0,
            'average': 61000.0
        }
        # Validate order mock
        mock_exchange.validate_order.return_value = (True, 0.4, 61000.0, 'Valid')
        # Place order mock
        mock_exchange.create_order.return_value = {'id': 'tp_new_id', 'status': 'open'}

        executor = BotExecutor(runner=None)
        bot_status = get_bot_status(bot_id)

        # Execute _sync_replace_tp
        # Even though db_qty starts at 1.0 (original), the function must overwrite it with 0.4
        new_order = executor._sync_replace_tp(
            bot_id=bot_id,
            name='test_bot_2',
            pair='BTC/USDT',
            direction='LONG',
            bot_status=bot_status,
            exchange=mock_exchange,
            db_tp=61000.0,
            db_qty=1.0, # original target size
            existing_tp_order={'id': 'tp_order_ex', 'clientOrderId': 'CQB_10051_TP_1'}
        )

        self.assertIsNotNone(new_order)
        # Verify replacement order size is 0.4
        mock_exchange.create_order.assert_called_once()
        args, kwargs = mock_exchange.create_order.call_args
        self.assertEqual(args[3], 0.4) # amount parameter is 4th argument

    def test_item3_hedge_child_tp_qty_uses_virtual_open_qty_directly(self):
        """
        Verify that:
        1. seal_trade_state does not overwrite hedge child open_qty with physical net.
        2. maintain_orders sets TP amount strictly to trades.open_qty for hedge child bots.
        """
        parent_id = 10060
        child_id = 10061
        _insert_bot(self.conn, parent_id, 'parent_bot', 'SUI/USDT', 'SUIUSDT', 'LONG')
        _insert_bot(self.conn, child_id, 'child_bot', 'SUI/USDT', 'SUIUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=parent_id)

        # Child has virtual open_qty = 13.1
        _insert_trades(self.conn, child_id, open_qty=13.1, cycle_id=1, position_side='SHORT', total_invested=26.2, avg_entry_price=2.0)
        # Parent has virtual open_qty = 32.7
        _insert_trades(self.conn, parent_id, open_qty=32.7, cycle_id=1, position_side='LONG', total_invested=65.4, avg_entry_price=2.0)

        # Mock exchange positions to show a net physical position of 19.6 SUI (representing parent - child: 32.7 - 13.1 = 19.6)
        # If the child bot uses exchange physical positions directly, it would think its position is 19.6 instead of its virtual 13.1.
        mock_exchange = MagicMock()
        mock_exchange.is_testnet = True
        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'SUIUSDT', 'qty': 32.7, 'side': 'long'},
            {'symbol': 'SUIUSDT', 'qty': 13.1, 'side': 'short'}
        ]

        # Verify seal_trade_state does NOT overwrite child open_qty
        # Since child_id is hedge_child, it bypasses drift checks entirely
        # Let's insert a child entry fill receipt in DB so recompute yields 13.1
        save_bot_order(child_id, 'entry', 'child_entry_1', price=2.0, amount=13.1, step=1, status='filled', client_order_id='CQB_10061_ENTRY_1', cycle_id=1)
        self.conn.execute("UPDATE bot_orders SET filled_amount = 13.1 WHERE client_order_id = 'CQB_10061_ENTRY_1'")
        self.conn.commit()

        # seal_trade_state execution
        res = ledger.seal_trade_state(child_id)
        self.assertAlmostEqual(res['qty'], 13.1)

        # Verify maintain_orders places a TP for exactly 13.1
        # Setup pending_placement TP for child
        save_bot_order(child_id, 'tp', 'PENDING_BE_10061_1', price=2.0, amount=13.1, step=0, status='pending_placement', client_order_id='CQB_10061_TP_1_BE', cycle_id=1)
        self.conn.commit()

        child_status = get_bot_status(child_id)
        mock_exchange.get_best_bid_ask.return_value = (2.0, 2.01)
        mock_exchange.validate_order.return_value = (True, 13.1, 2.0, 'Valid')
        mock_exchange.create_order.return_value = {'id': 'child_tp_order_id_ex', 'status': 'open'}

        executor = BotExecutor(runner=None)
        # maintain_orders call
        executor.maintain_orders(
            bot_id=child_id,
            name='child_bot',
            pair='SUI/USDT',
            direction='SHORT',
            bot_status=child_status,
            current_price=2.0,
            exchange=mock_exchange,
            market_snapshot={'open_orders': []},
            bot_config={}
        )

        # Assert child TP order placed with amount 13.1 (virtual open_qty)
        mock_exchange.create_order.assert_called_once()
        args, kwargs = mock_exchange.create_order.call_args
        self.assertEqual(args[3], 13.1)


if __name__ == '__main__':
    unittest.main()
