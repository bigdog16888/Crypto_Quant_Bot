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

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_inv30.db')
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
                   cycle_id=1, position_side='LONG', avg_entry_price=0.0, current_step=1):
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step, entry_confirmed)
        VALUES (?, ?, ?, ?, 0, ?, ?, 1)
    """, (bot_id, open_qty, cycle_id, position_side, avg_entry_price, current_step))
    conn.commit()

class TestINV30HedgeReconciliation(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_inv30_no_action_when_synced(self):
        """INV-30: child.open_qty == parent_hedgeable_qty -> no catch-up, status unchanged"""
        # Setup parent (trigger=7, step=7, cycle=128, open_qty=1148.1)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=1148.1, cycle_id=128, current_step=7)
        
        # parent pre-trigger fills (steps 1..6) sum = 7.9
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_6', 0.68, 7.9, 7.9, 'filled', 128, 6)
        """)
        self.conn.commit()
        
        # Setup child (parent=10018, cycle=128, open_qty=1140.2)
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='IN TRADE')
        _insert_trades(self.conn, 100318, open_qty=1140.2, cycle_id=128, current_step=1)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)
        mock_exchange.fetch_open_orders.return_value = []

        bot_status = {'open_qty': 1140.2, 'total_invested': 100.0, 'cycle_id': 128, 'current_step': 1}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order') as mock_create:
            executor.maintain_orders(
                bot_id=100318,
                name='sui long_hedge',
                pair='SUI/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=0.68,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            mock_create.assert_not_called()
            
        child_bot = self.conn.execute("SELECT status FROM bots WHERE id=100318").fetchone()
        self.assertEqual(child_bot[0], 'IN TRADE')

    def test_inv30_catchup_entry_when_underhedged(self):
        """INV-30: child.open_qty < parent_hedgeable_qty by 287.5 -> places catch-up order for 287.5"""
        # Setup parent (trigger=7, step=7, cycle=128, open_qty=1148.1)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=1148.1, cycle_id=128, current_step=7)
        
        # parent pre-trigger fills (steps 1..6) sum = 7.9
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_6', 0.68, 7.9, 7.9, 'filled', 128, 6)
        """)
        # parent step 7 fill (amount = 1148.1 - 7.9 = 1140.2)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_7', 0.68, 1140.2, 1140.2, 'filled', 128, 7)
        """)
        self.conn.commit()
        
        # Setup child (parent=10018, cycle=128, open_qty=852.7)
        # parent_hedgeable_qty = 1148.1 - 7.9 = 1140.2
        # child_open_qty = 852.7
        # drift = 1140.2 - 852.7 = 287.5
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='IN TRADE')
        _insert_trades(self.conn, 100318, open_qty=852.7, cycle_id=128, current_step=1)

        # Child has filled order at step 1 for 852.7
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_128_1_CATCHUP_OLD', 0.68, 852.7, 852.7, 'filled', 128, 1)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)
        mock_exchange.fetch_open_orders.return_value = []
        
        mock_order = {
            'id': 'EX_CHILD_CATCHUP_123',
            'status': 'open',
            'clientOrderId': 'CQB_100318_ENTRY_128_1_CATCHUP_123456789'
        }
        mock_exchange.create_order.return_value = mock_order

        bot_status = {'open_qty': 852.7, 'total_invested': 100.0, 'cycle_id': 128, 'current_step': 1}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order', return_value=mock_order) as mock_create:
            executor.maintain_orders(
                bot_id=100318,
                name='sui long_hedge',
                pair='SUI/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=0.68,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            placed_qty = args[3]
            placed_price = args[4]
            self.assertAlmostEqual(placed_qty, 287.5)
            self.assertEqual(placed_price, 0.68)
            self.assertIn('CATCHUP', kwargs['params']['newClientOrderId'])
            
        child_bot = self.conn.execute("SELECT status FROM bots WHERE id=100318").fetchone()
        self.assertEqual(child_bot[0], 'IN TRADE')
        
        # Verify order saved in DB
        db_order = self.conn.execute("SELECT order_id, amount, price, status, notes FROM bot_orders WHERE bot_id=100318 AND order_id = 'EX_CHILD_CATCHUP_123'").fetchone()
        self.assertIsNotNone(db_order)
        self.assertEqual(db_order[0], 'EX_CHILD_CATCHUP_123')
        self.assertAlmostEqual(db_order[1], 287.5)
        self.assertEqual(db_order[2], 0.68)
        self.assertEqual(db_order[3], 'open')
        self.assertIn('[INV-30]', db_order[4])

    def test_inv30_pending_flatten_when_overhedged(self):
        """INV-30: child.open_qty > parent_hedgeable_qty -> status becomes pending_flatten, no action on exchange"""
        # Setup parent (trigger=7, step=7, cycle=128, open_qty=1148.1)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=1148.1, cycle_id=128, current_step=7)
        
        # parent pre-trigger fills (steps 1..6) sum = 7.9
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_6', 0.68, 7.9, 7.9, 'filled', 128, 6)
        """)
        self.conn.commit()
        
        # Setup child (parent=10018, cycle=128, open_qty=1200.0)
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='IN TRADE')
        _insert_trades(self.conn, 100318, open_qty=1200.0, cycle_id=128, current_step=1)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)
        mock_exchange.fetch_open_orders.return_value = []

        bot_status = {'open_qty': 1200.0, 'total_invested': 100.0, 'cycle_id': 128, 'current_step': 1}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order') as mock_create:
            executor.maintain_orders(
                bot_id=100318,
                name='sui long_hedge',
                pair='SUI/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=0.68,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            mock_create.assert_not_called()
            
        child_bot = self.conn.execute("SELECT status FROM bots WHERE id=100318").fetchone()
        self.assertEqual(child_bot[0], 'pending_flatten')

    def test_inv30_tolerance_respected(self):
        """INV-30: drift <= step_size tolerance -> no action"""
        # Setup parent (trigger=7, step=7, cycle=128, open_qty=1148.1)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=1148.1, cycle_id=128, current_step=7)
        
        # parent pre-trigger fills (steps 1..6) sum = 7.9
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_6', 0.68, 7.9, 7.9, 'filled', 128, 6)
        """)
        # parent step 7 fill (1140.2)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_7', 0.68, 1140.2, 1140.2, 'filled', 128, 7)
        """)
        self.conn.commit()
        
        # Setup child (parent=10018, cycle=128, open_qty=1140.15) -> drift = 0.05
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='IN TRADE')
        _insert_trades(self.conn, 100318, open_qty=1140.15, cycle_id=128, current_step=1)

        # Child has filled order at step 1 for 1140.15
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_128_1_CATCHUP_OLD', 0.68, 1140.15, 1140.15, 'filled', 128, 1)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1} # Tolerance is 0.1
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 2)
        mock_exchange.fetch_open_orders.return_value = []

        bot_status = {'open_qty': 1140.15, 'total_invested': 100.0, 'cycle_id': 128, 'current_step': 1}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order') as mock_create:
            executor.maintain_orders(
                bot_id=100318,
                name='sui long_hedge',
                pair='SUI/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=0.68,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            mock_create.assert_not_called()
            
        child_bot = self.conn.execute("SELECT status FROM bots WHERE id=100318").fetchone()
        self.assertEqual(child_bot[0], 'IN TRADE')

    def test_inv30_no_duplicate_catchup_after_fill(self):
        """INV-30: Child has a catch-up entry with status='filled' for step 2. Parent step 2 qty matches child filled qty. Assert INV-30 does NOT place another catch-up order."""
        # Setup parent (trigger=7, current_step=8, cycle=130, open_qty=3094.0)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=3094.0, cycle_id=130, current_step=8)
        
        # Parent fills at step 7 (503.8) and step 8 (2590.2)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_130_7', 0.66, 503.8, 503.8, 'filled', 130, 7)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_130_8', 0.66, 2590.2, 2590.2, 'filled', 130, 8)
        """)
        
        # Setup child (parent=10018, cycle=130, open_qty=3094.0)
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='IN TRADE')
        _insert_trades(self.conn, 100318, open_qty=3094.0, cycle_id=130, current_step=2)

        # Child has filled catch-up entries for step 1 (503.8) and step 2 (2590.2)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_130_1_CATCHUP_1', 0.69, 503.8, 503.8, 'filled', 130, 1)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_130_2_CATCHUP_2', 0.68, 2590.2, 2590.2, 'filled', 130, 2)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)
        mock_exchange.fetch_open_orders.return_value = []

        bot_status = {'open_qty': 3094.0, 'total_invested': 100.0, 'cycle_id': 130, 'current_step': 2}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order') as mock_create:
            executor.maintain_orders(
                bot_id=100318,
                name='sui long_hedge',
                pair='SUI/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=0.68,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            mock_create.assert_not_called()

    def test_inv30_per_step_iteration(self):
        """INV-30: Parent has fills at steps 7 and 8. Child has fill for step 7 only. Assert INV-30 places catch-up for step 8 only (not aggregate delta)."""
        # Setup parent (trigger=7, current_step=8, cycle=130, open_qty=3094.0)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=3094.0, cycle_id=130, current_step=8)
        
        # Parent fills at step 7 (503.8) and step 8 (2590.2)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_130_7', 0.66, 503.8, 503.8, 'filled', 130, 7)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_130_8', 0.66, 2590.2, 2590.2, 'filled', 130, 8)
        """)
        
        # Setup child (parent=10018, cycle=130, open_qty=503.8)
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='IN TRADE')
        _insert_trades(self.conn, 100318, open_qty=503.8, cycle_id=130, current_step=1)

        # Child has filled catch-up entry for step 1 (503.8), step 2 is empty/missing
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_130_1_CATCHUP_1', 0.69, 503.8, 503.8, 'filled', 130, 1)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)
        mock_exchange.fetch_open_orders.return_value = []
        
        mock_order = {
            'id': 'EX_CHILD_CATCHUP_INV30_STEP2',
            'status': 'open',
            'clientOrderId': 'CQB_100318_ENTRY_130_2_CATCHUP_999'
        }
        mock_exchange.create_order.return_value = mock_order

        bot_status = {'open_qty': 503.8, 'total_invested': 100.0, 'cycle_id': 130, 'current_step': 1}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order', return_value=mock_order) as mock_create:
            executor.maintain_orders(
                bot_id=100318,
                name='sui long_hedge',
                pair='SUI/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=0.68,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            placed_qty = args[3]
            self.assertAlmostEqual(placed_qty, 2590.2)
            self.assertIn('CQB_100318_ENTRY_130_2_CATCHUP', kwargs['params']['newClientOrderId'])

    def test_inv30_single_catchup_per_cycle(self):
        """INV-30: Parent has unfilled steps 7, 8, and 9. Child has nothing. Assert only ONE catch-up order placed per maintain_orders cycle."""
        # Setup parent (trigger=7, current_step=9, cycle=130, open_qty=9142.7)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=9142.7, cycle_id=130, current_step=9)
        
        # Parent fills at step 7 (503.8), step 8 (2590.2), step 9 (6048.7)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_130_7', 0.66, 503.8, 503.8, 'filled', 130, 7)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_130_8', 0.66, 2590.2, 2590.2, 'filled', 130, 8)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_130_9', 0.66, 6048.7, 6048.7, 'filled', 130, 9)
        """)
        
        # Setup child (parent=10018, cycle=130, open_qty=0.0)
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='IN TRADE')
        _insert_trades(self.conn, 100318, open_qty=0.0, cycle_id=130, current_step=0)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)
        mock_exchange.fetch_open_orders.return_value = []
        
        mock_order = {
            'id': 'EX_CHILD_CATCHUP_INV30_STEP1',
            'status': 'open',
            'clientOrderId': 'CQB_100318_ENTRY_130_1_CATCHUP_777'
        }
        mock_exchange.create_order.return_value = mock_order

        bot_status = {'open_qty': 0.0, 'total_invested': 0.0, 'cycle_id': 130, 'current_step': 0}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order', return_value=mock_order) as mock_create:
            executor.maintain_orders(
                bot_id=100318,
                name='sui long_hedge',
                pair='SUI/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=0.68,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            # Assert only ONE order placed
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            placed_qty = args[3]
            self.assertAlmostEqual(placed_qty, 503.8)
            self.assertIn('CQB_100318_ENTRY_130_1_CATCHUP', kwargs['params']['newClientOrderId'])

if __name__ == '__main__':
    unittest.main()
