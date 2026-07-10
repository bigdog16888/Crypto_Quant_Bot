import os
import sys
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
import config

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_inv42.db')
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

class TestINV42HedgeLiveGuard(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        config.TRADING_ENABLED = True
        config.DRY_RUN = False

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_live_guard_prevents_catchup_in_maintain_orders(self):
        """
        INV-42: If parent is at step 7 (needs 0.151 hedge) and child's DB is wiped (open_qty=0, bot_orders empty),
        but exchange has net position of -0.151 (SHORT), the live guard must detect this,
        adjust the catch-up delta to 0, sync the DB to 0.151, and skip placing any order.
        """
        # Parent (bot 10016): BTC/USDC:USDC LONG, Step 7, cycle 52
        _insert_bot(self.conn, 10016, 'long btc price', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=1)
        _insert_trades(self.conn, 10016, open_qty=0.151, cycle_id=52, current_step=7)

        # Parent filled grid orders (steps 1..7) totaling 0.151
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10016, 'grid', 'CQB_10016_GRID_52_7', 62500, 0.151, 0.151, 'filled', 52, 7)
        """)
        self.conn.commit()

        # Child (bot 100317): BTC/USDC:USDC SHORT (wiped DB: open_qty = 0, no bot_orders rows for cycle 52)
        _insert_bot(self.conn, 100317, 'long btc price_hedge', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=52, current_step=1)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 3)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.get_last_price.return_value = 62500.0

        # Simulate exchange having the position already (netted to 0.0)
        mock_exchange.fetch_positions.return_value = [
            {
                'symbol': 'BTC/USDC:USDC',
                'net_qty': 0.0,
                'contracts': 0.0,
                'side': 'flat'
            }
        ]

        bot_status = {'open_qty': 0.0, 'total_invested': 0.0, 'cycle_id': 52, 'current_step': 1}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order') as mock_create:
            executor.maintain_orders(
                bot_id=100317,
                name='long btc price_hedge',
                pair='BTC/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=62500.0,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            # Assert no GTC catch-up entry is placed
            mock_create.assert_not_called()

        # Assert DB corrected: child open_qty should now be 0.151
        child_trade = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100317").fetchone()
        self.assertEqual(child_trade[0], 0.151)

        # Assert reconciliation row got inserted in bot_orders
        recon_order = self.conn.execute(
            "SELECT amount, filled_amount, status, notes FROM bot_orders WHERE bot_id=100317 AND notes LIKE '%Live-guard INV30%'"
        ).fetchone()
        self.assertIsNotNone(recon_order)
        self.assertEqual(recon_order[0], 0.151)
        self.assertEqual(recon_order[2], 'filled')

    def test_live_guard_prevents_catchup_in_signal_hedge_entry(self):
        """
        INV-42: If _signal_hedge_entry is called when parent hits step 7,
        but exchange already has the position, it should skip ordering.
        """
        # Parent (bot 10016)
        _insert_bot(self.conn, 10016, 'long btc price', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=1)
        _insert_trades(self.conn, 10016, open_qty=0.151, cycle_id=52, current_step=7)

        # Parent filled grid orders (steps 1..7) totaling 0.151
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10016, 'grid', 'CQB_10016_GRID_52_7', 62500, 0.151, 0.151, 'filled', 52, 7)
        """)
        self.conn.commit()

        # Child (bot 100317) - DB has 0.0 open
        _insert_bot(self.conn, 100317, 'long btc price_hedge', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=52, current_step=1)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 3)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.fetch_order.side_effect = Exception("Not found")

        # Simulate exchange having the position already (netted to 0.0)
        mock_exchange.fetch_positions.return_value = [
            {
                'symbol': 'BTC/USDC:USDC',
                'net_qty': 0.0,
                'contracts': 0.0,
                'side': 'flat'
            }
        ]

        with patch.object(mock_exchange, 'create_order') as mock_create:
            res = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='long btc price',
                parent_step=7,
                parent_cycle_id=52,
                direction='LONG',
                step_qty=0.151,
                step_fill_price=62500.0,
                pair='BTC/USDC:USDC',
                exchange=mock_exchange,
                current_price=62500.0
            )
            # The function should return True (skipped safely)
            self.assertTrue(res)
            mock_create.assert_not_called()

        # Assert DB corrected
        child_trade = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100317").fetchone()
        self.assertEqual(child_trade[0], 0.151)

    def test_live_guard_reconciliation_deduplication(self):
        """
        Regression test: Fires the live guard twice in a row for the same bot/step/cycle.
        Asserts that only one reconciliation row exists in bot_orders after both calls,
        with the correct (not doubled) quantity.
        """
        # Parent (bot 10016)
        _insert_bot(self.conn, 10016, 'long btc price', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=1)
        _insert_trades(self.conn, 10016, open_qty=0.151, cycle_id=52, current_step=7)

        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10016, 'grid', 'CQB_10016_GRID_52_7', 62500, 0.151, 0.151, 'filled', 52, 7)
        """)
        self.conn.commit()

        # Child (bot 100317) - DB has 0.0 open
        _insert_bot(self.conn, 100317, 'long btc price_hedge', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=52, current_step=1)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 3)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.fetch_order.side_effect = Exception("Not found")

        # Simulate exchange having the position already (netted to 0.0)
        mock_exchange.fetch_positions.return_value = [
            {
                'symbol': 'BTC/USDC:USDC',
                'net_qty': 0.0,
                'contracts': 0.0,
                'side': 'flat'
            }
        ]

        # Call the live guard the first time
        with patch.object(mock_exchange, 'create_order') as mock_create:
            res1 = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='long btc price',
                parent_step=7,
                parent_cycle_id=52,
                direction='LONG',
                step_qty=0.151,
                step_fill_price=62500.0,
                pair='BTC/USDC:USDC',
                exchange=mock_exchange,
                current_price=62500.0
            )
            self.assertTrue(res1)
            mock_create.assert_not_called()

        # Check DB state
        child_trade1 = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100317").fetchone()
        self.assertEqual(child_trade1[0], 0.151)
        recon_count1 = self.conn.execute(
            "SELECT COUNT(*) FROM bot_orders WHERE bot_id=100317 AND client_order_id LIKE '%LIVE_GUARD_RECON%'"
        ).fetchone()[0]
        self.assertEqual(recon_count1, 1)

        # Call the live guard the second time (simulating a retry or double firing before child DB step advances)
        # We manually change the filled_amount of the first recon order to 0.0 in the DB, simulating a wipe/reset correction.
        self.conn.execute(
            "UPDATE bot_orders SET filled_amount = 0.0 WHERE bot_id=100317 AND client_order_id LIKE '%LIVE_GUARD_RECON%'"
        )
        self.conn.commit()

        # Call the live guard the second time
        with patch.object(mock_exchange, 'create_order') as mock_create:
            res2 = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='long btc price',
                parent_step=7,
                parent_cycle_id=52,
                direction='LONG',
                step_qty=0.151,
                step_fill_price=62500.0,
                pair='BTC/USDC:USDC',
                exchange=mock_exchange,
                current_price=62500.0
            )
            self.assertTrue(res2)
            mock_create.assert_not_called()

        # Assert only ONE reconciliation row exists after both calls
        recon_orders = self.conn.execute(
            "SELECT amount, filled_amount, status FROM bot_orders WHERE bot_id=100317 AND client_order_id LIKE '%LIVE_GUARD_RECON%'"
        ).fetchall()
        
        self.assertEqual(len(recon_orders), 1)
        self.assertEqual(recon_orders[0][0], 0.151)
        # It should have replaced/re-inserted the row with filled_amount back to 0.151
        self.assertEqual(recon_orders[0][1], 0.151)

    def test_live_guard_partial_coverage(self):
        """
        INV-42: If parent is at step 7 (needs 0.151 hedge) and child's DB is wiped (open_qty=0),
        but exchange net is +0.051 (meaning parent +0.151 and child -0.100),
        then:
        (a) live_hedge_qty should compute to 0.100.
        (b) adjusted_delta should be 0.051.
        (c) a real catch-up order should be placed for the remaining 0.051.
        (d) child DB should be synced to 0.100.
        """
        # Parent (bot 10016)
        _insert_bot(self.conn, 10016, 'long btc price', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=1)
        _insert_trades(self.conn, 10016, open_qty=0.151, cycle_id=52, current_step=7)

        # Parent filled grid orders (steps 1..7) totaling 0.151
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10016, 'grid', 'CQB_10016_GRID_52_7', 62500, 0.151, 0.151, 'filled', 52, 7)
        """)
        self.conn.commit()

        # Child (bot 100317) - DB has 0.0 open
        _insert_bot(self.conn, 100317, 'long btc price_hedge', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=52, current_step=1)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 3)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.get_last_price.return_value = 62500.0

        # Simulate exchange having partial position (+0.051 net)
        mock_exchange.fetch_positions.return_value = [
            {
                'symbol': 'BTC/USDC:USDC',
                'net_qty': 0.051,
                'contracts': 0.051,
                'side': 'long'
            }
        ]

        bot_status = {'open_qty': 0.0, 'total_invested': 0.0, 'cycle_id': 52, 'current_step': 1}
        bot_config = {'bot_type': 'hedge_child'}

        with patch.object(mock_exchange, 'create_order') as mock_create:
            mock_create.return_value = {
                'id': 'CQB_100317_CATCHUP_TEST',
                'clientOrderId': 'CQB_100317_ENTRY_52_1_CATCHUP',
                'amount': 0.051,
                'price': 62500.0,
                'status': 'open',
                'filled': 0.0
            }
            executor.maintain_orders(
                bot_id=100317,
                name='long btc price_hedge',
                pair='BTC/USDC:USDC',
                direction='SHORT',
                bot_status=bot_status,
                current_price=62500.0,
                exchange=mock_exchange,
                market_snapshot={'open_orders': []},
                bot_config=bot_config
            )
            # Assert a real order is placed for the reduced amount (0.051)
            mock_create.assert_called_once()
            call_args = mock_create.call_args[0]
            self.assertEqual(call_args[0], 'BTC/USDC:USDC')
            self.assertEqual(call_args[2], 'sell')
            self.assertAlmostEqual(call_args[3], 0.051)

        # Assert DB corrected child open_qty to 0.100 (covered portion)
        child_trade = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100317").fetchone()
        self.assertAlmostEqual(child_trade[0], 0.100)

        # Assert reconciliation row got inserted in bot_orders for the covered 0.100 portion
        recon_order = self.conn.execute(
            "SELECT amount, filled_amount, status FROM bot_orders WHERE bot_id=100317 AND notes LIKE '%Live-guard INV30%'"
        ).fetchone()
        self.assertIsNotNone(recon_order)
        self.assertAlmostEqual(recon_order[0], 0.100)
        self.assertAlmostEqual(recon_order[1], 0.100)
        self.assertEqual(recon_order[2], 'filled')

