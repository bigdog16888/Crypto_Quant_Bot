"""
Unit tests for v3.9.19 fixes:
  1. test_hedge_ghost_detection_wipes_child
  2. test_hedge_ghost_detection_skips_active
  3. test_hedge_ghost_detection_parent_also_flat
  4. test_missed_be_tp_self_healing
  5. test_missed_be_tp_not_duplicate
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
from engine.database import get_connection, init_db, save_bot_order
from engine.oneway_netting import detect_hedge_child_ghost, wipe_hedge_child_ghost

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_v3919.db')
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
                   avg_entry_price=1700.0, total_invested=1700.0,
                   target_tp_price=1730.0, basket_start_time=None, current_step=1):
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


class TestHedgeGhostDetection(unittest.TestCase):
    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Set up parent bot (10017) LONG
        _insert_bot(self.conn, 10017, 'eth long', 'ETH/USDC:USDC', 'ETHUSDC', 'LONG')
        _insert_trades(self.conn, 10017, open_qty=0.028, avg_entry_price=1800.0, total_invested=50.4, current_step=1)

        # Set up child bot (99001) SHORT
        _insert_bot(self.conn, 99001, 'eth long_hedge', 'ETH/USDC:USDC', 'ETHUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017)
        _insert_trades(self.conn, 99001, open_qty=0.415, avg_entry_price=1800.0, total_invested=747.0, current_step=1, position_side='SHORT')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if hasattr(database._local, 'connection') and database._local.connection:
            try:
                database._local.connection.close()
            except Exception:
                pass
            database._local.connection = None

    def test_hedge_ghost_detection_wipes_child(self):
        """
        If parent has 0.028 LONG, child has 0.415 SHORT, but exchange signed net is 0.028
        (meaning the child position is flat/gone), detect_hedge_child_ghost returns True
        and wipe_hedge_child_ghost sets child bot status to hedge_standby, zeroes trades open_qty,
        cancels exchange orders, and registers a drift_note audit row.
        """
        # Insert orders to verify clearing/cancelling logic
        save_bot_order(
            99001, 'entry', 'ORDER_1_ETH', price=1800.0, amount=0.415, step=1, status='filled',
            client_order_id='CQB_99001_ENTRY_1_ETH', cycle_id=1
        )
        save_bot_order(
            99001, 'tp', 'ORDER_1B_ETH', price=1750.0, amount=0.415, step=1, status='filled',
            client_order_id='CQB_99001_TP_1B_ETH', cycle_id=1
        )
        save_bot_order(
            99001, 'tp', 'ORDER_2_ETH', price=1750.0, amount=0.415, step=0, status='open',
            client_order_id='CQB_99001_TP_1_ETH', cycle_id=1
        )

        mock_exchange = MagicMock()
        # exchange net matches parent-only contribution (0.028)
        with patch('engine.parity_gates.get_exchange_signed_net', return_value=0.028), \
             patch('engine.parity_gates.qty_tolerance', return_value=0.0001), \
             patch('engine.exchange_interface.ExchangeInterface.fetch_positions', return_value=[]):
            
            is_ghost = detect_hedge_child_ghost(mock_exchange, 99001, self.conn)
            self.assertTrue(is_ghost)
            
            # Wipe child ghost
            wipe_hedge_child_ghost(mock_exchange, 99001, self.conn)
            
            # Verify DB changes
            bot_row = self.conn.execute("SELECT status FROM bots WHERE id=99001").fetchone()
            self.assertEqual(bot_row[0], 'hedge_standby')
            
            trade_row = self.conn.execute("SELECT open_qty, avg_entry_price, total_invested, current_step FROM trades WHERE bot_id=99001").fetchone()
            self.assertEqual(trade_row[0], 0.0)
            self.assertEqual(trade_row[1], 0.0)
            self.assertEqual(trade_row[2], 0.0)
            self.assertEqual(trade_row[3], 0)
            
            # Verify orders were cleared/cancelled
            order1_status = self.conn.execute("SELECT status FROM bot_orders WHERE order_id='ORDER_1_ETH'").fetchone()
            self.assertEqual(order1_status[0], 'reset_cleared')
            
            order2_status = self.conn.execute("SELECT status FROM bot_orders WHERE order_id='ORDER_2_ETH'").fetchone()
            self.assertEqual(order2_status[0], 'cancelled')

            # Check for drift_note
            drift_note = self.conn.execute("SELECT order_type, notes FROM bot_orders WHERE bot_id=99001 AND order_type='drift_note'").fetchone()
            self.assertIsNotNone(drift_note)
            self.assertIn("DB claims 0.415 but exchange is flat", drift_note[1])
            
            # Verify cancellation called
            mock_exchange.cancel_orders_by_bot_id.assert_called_once_with(99001, 'ETH/USDC:USDC')

    def test_hedge_ghost_detection_skips_active(self):
        """
        If child has active position (exchange signed net = 0.028 - 0.415 = -0.387),
        detect_hedge_child_ghost returns False.
        """
        mock_exchange = MagicMock()
        with patch('engine.parity_gates.get_exchange_signed_net', return_value=-0.387), \
             patch('engine.parity_gates.qty_tolerance', return_value=0.0001):
            
            is_ghost = detect_hedge_child_ghost(mock_exchange, 99001, self.conn)
            self.assertFalse(is_ghost)

    def test_hedge_ghost_detection_parent_also_flat(self):
        """
        If both parent and child have open_qty > 0 but exchange signed net is 0.0,
        detect_hedge_child_ghost returns False (leaving it to the global wipe check).
        """
        mock_exchange = MagicMock()
        with patch('engine.parity_gates.get_exchange_signed_net', return_value=0.0), \
             patch('engine.parity_gates.qty_tolerance', return_value=0.0001):
            
            is_ghost = detect_hedge_child_ghost(mock_exchange, 99001, self.conn)
            self.assertFalse(is_ghost, "Should return False because parent's position is also gone, which is a global wipe case.")

    def test_hedge_ghost_detection_with_other_bots_active(self):
        """
        If other standard bots on the same pair are active, their signed net contribution
        is subtracted from exchange net to isolate the child's physical quantity.
        """
        # Insert a standard bot (10011) SHORT with open_qty = 0.042 on the same pair
        _insert_bot(self.conn, 10011, 'eth standard short', 'ETH/USDC:USDC', 'ETHUSDC', 'SHORT')
        _insert_trades(self.conn, 10011, open_qty=0.042, avg_entry_price=1800.0, total_invested=75.6, current_step=1, position_side='SHORT')

        mock_exchange = MagicMock()
        # Case A: Child is a ghost.
        # Seed matching entry and exit fills to sum to 0.0
        save_bot_order(
            99001, 'entry', 'ORDER_G1_ETH', price=1800.0, amount=0.415, step=1, status='filled',
            client_order_id='CQB_99001_GENTRY_1_ETH', cycle_id=1
        )
        save_bot_order(
            99001, 'tp', 'ORDER_G2_ETH', price=1750.0, amount=0.415, step=1, status='filled',
            client_order_id='CQB_99001_GTP_1_ETH', cycle_id=1
        )
        
        is_ghost = detect_hedge_child_ghost(mock_exchange, 99001, self.conn)
        self.assertTrue(is_ghost)

        # Case B: Child is NOT a ghost.
        # Clear/wipe orders for this bot and seed only entry fill so it recomputes to 0.415
        self.conn.execute("DELETE FROM bot_orders WHERE bot_id=99001")
        self.conn.commit()
        save_bot_order(
            99001, 'entry', 'ORDER_L1_ETH', price=1800.0, amount=0.415, step=1, status='filled',
            client_order_id='CQB_99001_LENTRY_1_ETH', cycle_id=1
        )
        
        is_ghost = detect_hedge_child_ghost(mock_exchange, 99001, self.conn)
        self.assertFalse(is_ghost)


class TestMissedBETPSelfHealing(unittest.TestCase):
    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Set up parent bot (10017) SHORT, status = Scanning (completed cycle)
        _insert_bot(self.conn, 10017, 'eth short', 'ETH/USDC:USDC', 'ETHUSDC', 'SHORT', status='Scanning')
        _insert_trades(self.conn, 10017, open_qty=0.0)

        # Set up child bot (99001) LONG
        _insert_bot(self.conn, 99001, 'eth short_hedge', 'ETH/USDC:USDC', 'ETHUSDC', 'LONG', bot_type='hedge_child', parent_bot_id=10017)
        _insert_trades(self.conn, 99001, open_qty=0.5, avg_entry_price=1800.0, total_invested=900.0, current_step=1, target_tp_price=0.0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if hasattr(database._local, 'connection') and database._local.connection:
            try:
                database._local.connection.close()
            except Exception:
                pass
            database._local.connection = None

    def test_missed_be_tp_self_healing(self):
        """
        If parent is Scanning, child has open_qty > 0 and no TP exists,
        maintain_orders immediately places/registers a BE TP.
        """
        from engine.bot_executor import BotExecutor
        executor = BotExecutor(runner=None)
        
        mock_exchange = MagicMock()
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.validate_order.return_value = (True, 0.5, 1800.0, 'OK')
        mock_exchange.create_order.return_value = {
            'id': 'EX_TP_123',
            'status': 'open',
            'clientOrderId': 'CQB_99001_TP_1_INV26_BE'
        }
        
        bot_status = {
            'id': 99001,
            'name': 'eth short_hedge',
            'pair': 'ETH/USDC:USDC',
            'current_step': 1,
            'total_invested': 900.0,
            'avg_entry_price': 1800.0,
            'target_tp_price': 0.0,
            'cycle_id': 1,
            'open_qty': 0.5
        }
        bot_config = {'market_type': 'swap'}

        with patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, '')):
            executor.maintain_orders(
                bot_id=99001,
                name='eth short_hedge',
                pair='ETH/USDC:USDC',
                direction='LONG',
                bot_status=bot_status,
                current_price=1810.0,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            
            # Check that a pending_placement order was inserted in the database
            row = self.conn.execute(
                "SELECT price, amount, status, client_order_id FROM bot_orders WHERE bot_id=99001 AND order_type='tp'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(float(row[0]), 1800.0) # Break-even TP is at avg_entry_price
            self.assertEqual(float(row[1]), 0.5)
            self.assertEqual(row[2], 'open') # It should have been placed immediately during the same maintain_orders cycle!
            self.assertIn("INV26", row[3])

    def test_missed_be_tp_not_duplicate(self):
        """
        If parent is Scanning, child has open_qty > 0 and a TP already exists (open),
        maintain_orders does not place a duplicate.
        """
        # Seed an open TP order in the DB
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (99001, 'tp', 'EX_TP_EXISTING', 'CQB_99001_TP_1_INV26_BE', 1800.0, 0.5, 'open', 0, 1, 12345)
        """)
        self.conn.commit()

        from engine.bot_executor import BotExecutor
        executor = BotExecutor(runner=None)
        
        mock_exchange = MagicMock()
        mock_exchange.fetch_open_orders.return_value = [
            {'id': 'EX_TP_EXISTING', 'clientOrderId': 'CQB_99001_TP_1_INV26_BE', 'status': 'open', 'price': 1800.0, 'amount': 0.5}
        ]
        
        bot_status = {
            'id': 99001,
            'name': 'eth short_hedge',
            'pair': 'ETH/USDC:USDC',
            'current_step': 1,
            'total_invested': 900.0,
            'avg_entry_price': 1800.0,
            'target_tp_price': 1800.0,
            'cycle_id': 1,
            'open_qty': 0.5
        }
        bot_config = {'market_type': 'swap'}

        with patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, '')):
            executor.maintain_orders(
                bot_id=99001,
                name='eth short_hedge',
                pair='ETH/USDC:USDC',
                direction='LONG',
                bot_status=bot_status,
                current_price=1810.0,
                exchange=mock_exchange,
                market_snapshot=None,
                bot_config=bot_config
            )
            
            # Count the TP orders in bot_orders for this bot
            count = self.conn.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id=99001 AND order_type='tp'").fetchone()[0]
            self.assertEqual(count, 1, "Should not create a duplicate TP order.")
