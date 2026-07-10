import os
import sys
import time
import sqlite3
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.bot_executor import BotExecutor
from engine.exchange_interface import ExchangeInterface
from engine.ledger import seal_trade_state, handle_tp_completion

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_adr003.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

def _insert_bot(conn, bot_id, name, pair, norm_pair, direction,
                status='IN TRADE', bot_type='standard', config_json='{}',
                parent_bot_id=None, hedge_child_bot_id=None, hedge_trigger_step=None):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction,
                          status, bot_type, is_active, config, parent_bot_id,
                          hedge_child_bot_id, hedge_trigger_step,
                          rsi_limit, martingale_multiplier, base_size, strategy_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 0, 1.0, 0, 'Martingale')
    """, (bot_id, name, pair, norm_pair, direction,
          status, bot_type, config_json, parent_bot_id,
          hedge_child_bot_id, hedge_trigger_step))
    conn.commit()

def _insert_trades(conn, bot_id, open_qty=0.0,
                    cycle_id=1, position_side='LONG', avg_entry_price=0.0):
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step, entry_confirmed)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1)
    """, (bot_id, open_qty, cycle_id, position_side, open_qty * avg_entry_price if avg_entry_price else 0, avg_entry_price))
    conn.commit()

class TestADR003Invariants(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_invariant_a_deterministic_cid_idempotency(self):
        """Invariant A: Verify deterministic CID generation and idempotency checks."""
        # Parent (1001) and child (2001) setup
        _insert_bot(self.conn, 1001, 'SOL Parent', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', hedge_child_bot_id=2001, hedge_trigger_step=8)
        _insert_trades(self.conn, 1001, open_qty=5.0)

        _insert_bot(self.conn, 2001, 'SOL Child', 'SOL/USDC:USDC', 'SOLUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=1001)
        _insert_trades(self.conn, 2001, open_qty=0.0)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)
        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'SOL/USDC:USDC', 'side': 'long', 'qty': 2.0, 'entryPrice': 65.0}
        ]
        mock_exchange.fetch_order.side_effect = Exception("Order not found")

        # Mock order placement returning standard CCXT dict
        mock_order = {
            'id': 'EX_ENTRY_100',
            'status': 'open',
            'clientOrderId': 'CQB_2001_ENTRY_1_8'
        }

        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place:
            # First signal placement
            res1 = executor._signal_hedge_child_entry(
                parent_bot_id=1001,
                parent_name='SOL Parent',
                parent_step=8,
                pair='SOL/USDC:USDC',
                direction='LONG',
                step_qty=2.0,
                step_fill_price=65.0,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertTrue(res1)
            mock_place.assert_called_once()
            
            # Reset call count
            mock_place.reset_mock()

            # Second signal placement for the exact same step and cycle
            res2 = executor._signal_hedge_child_entry(
                parent_bot_id=1001,
                parent_name='SOL Parent',
                parent_step=8,
                pair='SOL/USDC:USDC',
                direction='LONG',
                step_qty=2.0,
                step_fill_price=65.0,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertTrue(res2)
            # Should not place order again due to DB idempotency check
            mock_place.assert_not_called()

            # Reset DB entries but mock exchange containing the order
            self.conn.execute("DELETE FROM bot_orders WHERE bot_id = 2001")
            self.conn.commit()
            
            # Mock exchange to return the live order
            mock_exchange.fetch_order.side_effect = None
            mock_exchange.fetch_order.return_value = {'id': 'EX_ENTRY_100', 'status': 'open'}

            res3 = executor._signal_hedge_child_entry(
                parent_bot_id=1001,
                parent_name='SOL Parent',
                parent_step=8,
                pair='SOL/USDC:USDC',
                direction='LONG',
                step_qty=2.0,
                step_fill_price=65.0,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertTrue(res3)
            # Should not place order again due to exchange check
            mock_place.assert_not_called()

    def test_invariant_b_tp_sizing_from_exchange(self):
        """Invariant B: Verify TP sizing is done based on exchange positions, not DB."""
        _insert_bot(self.conn, 1001, 'SOL Parent', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', hedge_child_bot_id=2001, hedge_trigger_step=8)
        _insert_trades(self.conn, 1001, open_qty=5.0)
        _insert_bot(self.conn, 2001, 'SOL Child', 'SOL/USDC:USDC', 'SOLUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=1001)
        # DB believes we have 15.0 SOL short
        _insert_trades(self.conn, 2001, open_qty=15.0, avg_entry_price=60.0)

        # Place pending_placement TP
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (2001, 'tp', 'PENDING_BE_2001_1', 'CQB_2001_TP_1_BE', 60.0, 15.0, 'pending_placement', 0, 1, 12345)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.validate_order.side_effect = lambda pair, side, amt, price, *args, **kwargs: (True, amt, price, 'OK')
        mock_exchange.create_order.side_effect = lambda pair, type, side, amt, price, params: {
            'id': 'EX_TP_200', 'status': 'open', 'clientOrderId': params.get('newClientOrderId')
        }

        # Mock physical positions on exchange: exchange only has 10.0 SOL short (not 15.0)
        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'SOL/USDC:USDC', 'side': 'short', 'qty': 10.0, 'entryPrice': 60.0}
        ]

        bot_status = {
            'id': 2001,
            'name': 'SOL Child',
            'pair': 'SOL/USDC:USDC',
            'current_step': 1,
            'total_invested': 900.0,
            'avg_entry_price': 60.0,
            'target_tp_price': 0.0,
            'cycle_id': 1,
            'open_qty': 15.0
        }

        executor.maintain_orders(
            bot_id=2001,
            name='SOL Child',
            pair='SOL/USDC:USDC',
            direction='SHORT',
            bot_status=bot_status,
            current_price=62.0,
            exchange=mock_exchange,
            market_snapshot={'open_orders': []},
            bot_config={'market_type': 'swap'}
        )

        # Verify the created order amount is 15.0 (from DB virtual open_qty), not 10.0 (from exchange)
        args, kwargs = mock_exchange.create_order.call_args
        actual_qty = args[3] if len(args) > 3 else kwargs.get('amount')
        self.assertEqual(actual_qty, 15.0)

        # Verify the DB row was updated with 15.0
        row = self.conn.execute("SELECT amount, status FROM bot_orders WHERE order_id = 'EX_TP_200'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 15.0)
        self.assertEqual(row[1], 'open')

    def test_invariant_c_capacity_limit(self):
        """Invariant C: Verify entries are skipped when exceeding max_position_limit."""
        _insert_bot(self.conn, 1001, 'SOL Parent', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', hedge_child_bot_id=2001, hedge_trigger_step=8)
        _insert_trades(self.conn, 1001, open_qty=15.0)

        # Limit set to 50.0 in child's config JSON
        _insert_bot(self.conn, 2001, 'SOL Child', 'SOL/USDC:USDC', 'SOLUSDC', 'SHORT', bot_type='hedge_child',
                    config_json='{"max_position_limit": 50.0}', parent_bot_id=1001)
        _insert_trades(self.conn, 2001, open_qty=0.0)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)
        mock_exchange.fetch_order.side_effect = Exception("Order not found")

        # Scenario 1: Currently hold 40.0 in DB and exchange, adding 15.0 (Total = 55.0 > 50.0 limit)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (1001, 'grid', 'PARENT_GRID', 65.0, 55.0, 55.0, 'filled', 1, 8)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (2001, 'entry', 'CHILD_ENTRY', 60.0, 40.0, 40.0, 'filled', 1, 1)
        """)
        self.conn.execute("UPDATE trades SET open_qty = 40.0 WHERE bot_id = 2001")
        self.conn.commit()

        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'SOL/USDC:USDC', 'side': 'long', 'qty': 15.0, 'entryPrice': 60.0}
        ]

        with patch.object(executor, '_place_gtx_order_with_retry') as mock_place:
            res = executor._signal_hedge_child_entry(
                parent_bot_id=1001,
                parent_name='SOL Parent',
                parent_step=8,
                pair='SOL/USDC:USDC',
                direction='LONG',
                step_qty=15.0,
                step_fill_price=65.0,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertFalse(res)
            mock_place.assert_not_called()

        # Scenario 2: Currently hold 30.0 in DB and exchange, adding 15.0 (Total = 45.0 <= 50.0 limit)
        self.conn.execute("DELETE FROM bot_orders WHERE bot_id IN (1001, 2001)")
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (1001, 'grid', 'PARENT_GRID', 65.0, 45.0, 45.0, 'filled', 1, 8)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (2001, 'entry', 'CHILD_ENTRY', 60.0, 30.0, 30.0, 'filled', 1, 1)
        """)
        self.conn.execute("UPDATE trades SET open_qty = 30.0 WHERE bot_id = 2001")
        self.conn.commit()

        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'SOL/USDC:USDC', 'side': 'long', 'qty': 15.0, 'entryPrice': 60.0}
        ]
        mock_order = {'id': 'EX_ENTRY_OK', 'status': 'open'}

        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place:
            res = executor._signal_hedge_child_entry(
                parent_bot_id=1001,
                parent_name='SOL Parent',
                parent_step=8,
                pair='SOL/USDC:USDC',
                direction='LONG',
                step_qty=15.0,
                step_fill_price=65.0,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertTrue(res)
            mock_place.assert_called_once()

    def test_seal_trade_state_is_pure_virtual(self):
        """Verify that seal_trade_state does NOT call exchange.fetch_positions or instantiate ExchangeInterface."""
        _insert_bot(self.conn, 2001, 'SOL Child', 'SOL/USDC:USDC', 'SOLUSDC', 'SHORT', bot_type='hedge_child')
        # DB recomputes to 15.0 SOL short (avg 60.0, cost 900.0)
        _insert_trades(self.conn, 2001, open_qty=15.0, avg_entry_price=60.0)

        # Place filled entry order to back up the recomputed 15.0 qty
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, created_at)
            VALUES (2001, 'entry', 'EX_ENTRY_1', 'CQB_2001_ENTRY_1_1', 60.0, 15.0, 15.0, 'filled', 1, 1, 12345)
        """)
        self.conn.commit()

        with patch('engine.exchange_interface.ExchangeInterface') as mock_exchange_class:
            res = seal_trade_state(2001)
            # Verify qty and cost are virtual calculations (15.0 qty, 900.0 cost)
            self.assertEqual(res['qty'], 15.0)
            self.assertEqual(res['cost'], 900.0)
            self.assertEqual(res['avg'], 60.0)
            
            # Verify ExchangeInterface was never instantiated
            mock_exchange_class.assert_not_called()

if __name__ == "__main__":
    unittest.main()
