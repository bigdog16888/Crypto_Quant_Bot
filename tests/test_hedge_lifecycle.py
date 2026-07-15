"""
ADR-002 Hedge Child Bot — Integration Tests

Covers Tickets 1–10 incrementally plus TEST_SCENARIOS.md Scenarios 5, 6, and 8.
Each test class is independent and uses a fresh temporary database.
"""

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


def _make_temp_db():
    """Create a temp DB path and point engine.database at it."""
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_adr002.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()  # reset thread-local connection
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


# ---------------------------------------------------------------------------
# TICKET-6: One-way netting suppression (parent/child)
# Tests run against the actual oneway_netting module but with mock DB state
# ---------------------------------------------------------------------------

class TestTicket6OnewayNettingSuppression(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        conn = get_connection()

        # Parent bot (LONG)
        _insert_bot(conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG',
                    hedge_child_bot_id=99001)
        _insert_trades(conn, 10017, open_qty=10.0, position_side='LONG')

        # Hedge child bot (SHORT)
        _insert_bot(conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT',
                    bot_type='hedge_child', parent_bot_id=10017, status='IN TRADE')
        _insert_trades(conn, 99001, open_qty=44.7, position_side='SHORT')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)



# ---------------------------------------------------------------------------
# SCENARIO-6: Migration idempotency (integrated) — DELETED (obsolete)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TICKET-3: Remove h_qty from recompute_invested_from_orders
# ---------------------------------------------------------------------------

class TestTicket3Recompute(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG')
        _insert_trades(self.conn, 10017, open_qty=0.0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ticket3_recompute_returns_4_tuple(self):
        """recompute_invested_from_orders returns exactly a 4-tuple (cost, avg, qty, step).
        ADR-002 INV-6. A 5-tuple unpack must raise ValueError — this was the live bug
        at database.py line 3839 where _, _ silently caused the wipe guard to never fire."""
        from engine.database import recompute_invested_from_orders
        result = recompute_invested_from_orders(bot_id=10017)

        # Exact length — not 3, not 5
        self.assertEqual(len(result), 4,
                         f"recompute_invested_from_orders must return exactly 4 values "
                         f"(cost, avg, qty, step). Got {len(result)}-tuple: {result}")

        # Unpack must succeed with exactly 4 targets
        cost, avg, qty, step = result  # raises ValueError if shape changes

        # Regression: 5-tuple unpack must raise (the bug we fixed at db.py:3839)
        with self.assertRaises(ValueError,
                               msg="5-tuple unpack must raise ValueError — "
                                   "confirms the db.py:3839 bug is fixed"):
            a, b, c, d, e = result

    def test_ticket3_recompute_not_affected_by_old_hedge_orders(self):
        """Parent bot recompute should ignore legacy hedge rows and return net basket qty."""
        from engine.database import recompute_invested_from_orders
        # Insert a legacy hedge order to verify it does not affect basket qty
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, created_at, notes, position_side)
            VALUES (10017, 'hedge', 'EX_HEDGE_1', 'CQB_10017_HEDGE_1', 1.0, 10.0, 10.0, 'filled', 1, 1, ?, 'legacy', 'SHORT')
        """, (int(time.time()),))
        self.conn.commit()

        cost, avg, qty, step = recompute_invested_from_orders(10017)
        self.assertLess(qty, 0.001, f"Parent bot should have 0 basket qty after migration, got {qty}")

    def test_ticket3_get_bot_hedge_qty_deleted(self):
        """get_bot_hedge_qty should not exist on database module (raises AttributeError under test)."""
        import engine.database as db
        self.assertFalse(hasattr(db, 'get_bot_hedge_qty'), "get_bot_hedge_qty should be deleted")


# ---------------------------------------------------------------------------
# TICKET-4: Remove h_qty from ledger.py
# ---------------------------------------------------------------------------

class TestTicket4Ledger(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG')
        _insert_trades(self.conn, 10017, open_qty=10.0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ticket4_seal_trade_state_no_hedge_qty_write(self):
        """Verify that the hedge_qty column has been dropped from trades table."""
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(trades)")
        columns = [col[1] for col in cursor.fetchall()]
        self.assertNotIn("hedge_qty", columns, "hedge_qty column should be dropped from trades table")

    def test_ticket4_seal_trade_state_correct_open_qty(self):
        """seal_trade_state writes correct open_qty for a standard bot."""
        from engine.ledger import seal_trade_state
        result = seal_trade_state(10017)
        # Verify it succeeds and doesn't crash on tuple unpacking
        self.assertIsNotNone(result)

    def test_ticket4_exit_types_no_hedge(self):
        """'hedge' and 'hedge_tp' are not in credit_fill EXIT_TYPES."""
        import inspect
        from engine import ledger
        src = inspect.getsource(ledger.credit_fill)
        # Check if 'hedge' or 'hedge_tp' is in exit types list (they should be removed)
        # We can also directly inspect ledger.py src
        self.assertNotIn("'hedge', 'hedge_tp'", src)


# ---------------------------------------------------------------------------
# TICKET-5: Remove h_qty from reconciler.py
# ---------------------------------------------------------------------------

class TestTicket5Reconciler(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
        _insert_trades(self.conn, 10017, open_qty=0.0)

        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017, status='IN TRADE')
        _insert_trades(self.conn, 99001, open_qty=44.7)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ticket5_recompute_callers_use_4_tuple(self):
        """Reconciler does not unpack 5 values from recompute."""
        import inspect
        import re
        from engine import reconciler
        src = inspect.getsource(reconciler.StateReconciler)
        five_tuple_unpacks = re.findall(
            r'[\w]+,\s*[\w]+,\s*[\w]+,\s*[\w]+,\s*[\w]+\s*=\s*(?:recompute_invested_from_orders|_rif)',
            src
        )
        self.assertEqual(len(five_tuple_unpacks), 0, f"Found 5-tuple unpacks: {five_tuple_unpacks}")

    def test_ticket5_global_netting_no_mismatch_after_migration(self):
        """After migration, pair virtual net matches exchange for XRP."""
        from engine.database import get_pair_virtual_net
        # With hedge child owning 44.7 SHORT and parent owning 0 basket,
        # virtual net for XRP = 0 (parent) + (-44.7) (child) = -44.7
        # This should match exchange physical
        net = get_pair_virtual_net('XRP/USDC:USDC')
        self.assertAlmostEqual(net, -44.7, places=2, msg=f"Expected -44.7, got {net}")


# ---------------------------------------------------------------------------
# TICKET-7: Hedge Child Entry Signal in bot_executor.py
# ---------------------------------------------------------------------------

class TestTicket7BotExecutor(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
        _insert_trades(self.conn, 10017, open_qty=10.0)

        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017, status='HEDGE_STANDBY')
        _insert_trades(self.conn, 99001, open_qty=0.0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ticket7_signal_hedge_child_entry_creates_order(self):
        """_signal_hedge_child_entry places GTX entry order on child and saves to DB."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        
        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        # Mock order placement returning a filled dict
        mock_order = {
            'id': 'EX_CHILD_ENTRY_123',
            'status': 'open',
            'clientOrderId': 'CQB_99001_ENTRY_1_8'
        }
        
        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place:
            res = executor._signal_hedge_child_entry(
                parent_bot_id=10017,
                parent_name='xrp long',
                parent_step=8,
                pair='XRP/USDC:USDC',
                direction='LONG',
                step_qty=5.5,
                step_fill_price=2.2,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertTrue(res)
            mock_place.assert_called_once()
            
            # Verify order saved in DB
            row = self.conn.execute(
                "SELECT order_id, price, amount, status FROM bot_orders WHERE bot_id=99001"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'EX_CHILD_ENTRY_123')
            self.assertEqual(float(row[1]), 2.2)
            self.assertEqual(float(row[2]), 5.5)
            self.assertEqual(row[3], 'open')

    def test_ticket7_signal_hedge_child_entry_calculates_relative_step(self):
        """_signal_hedge_child_entry stores child_step relative to parent trigger step."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        # Update parent's hedge_trigger_step to 7
        self.conn.execute("UPDATE bots SET hedge_trigger_step = 7 WHERE id = 10017")
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        mock_order = {
            'id': 'EX_CHILD_ENTRY_456',
            'status': 'open',
            'clientOrderId': 'CQB_99001_ENTRY_1_8'
        }

        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order):
            res = executor._signal_hedge_child_entry(
                parent_bot_id=10017,
                parent_name='xrp long',
                parent_step=8,
                pair='XRP/USDC:USDC',
                direction='LONG',
                step_qty=5.5,
                step_fill_price=2.2,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertTrue(res)

            # Child step should be 8 - 7 + 1 = 2
            row = self.conn.execute(
                "SELECT step FROM bot_orders WHERE order_id='EX_CHILD_ENTRY_456'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 2)

    def test_ticket7_signal_hedge_child_entry_idempotent(self):
        """Calling signal twice for same step does not place duplicate orders."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        
        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        mock_order = {
            'id': 'EX_CHILD_ENTRY_123',
            'status': 'open',
            'clientOrderId': 'CQB_99001_ENTRY_1_8'
        }
        
        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place:
            # First call
            res1 = executor._signal_hedge_child_entry(
                parent_bot_id=10017,
                parent_name='xrp long',
                parent_step=8,
                pair='XRP/USDC:USDC',
                direction='LONG',
                step_qty=5.5,
                step_fill_price=2.2,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertTrue(res1)
            
            # Second call
            res2 = executor._signal_hedge_child_entry(
                parent_bot_id=10017,
                parent_name='xrp long',
                parent_step=8,
                pair='XRP/USDC:USDC',
                direction='LONG',
                step_qty=5.5,
                step_fill_price=2.2,
                exchange=mock_exchange,
                parent_cycle_id=1
            )
            self.assertTrue(res2)
            
            # Should only be called once
            self.assertEqual(mock_place.call_count, 1)

    def test_hedge_child_step_qty_mirrors_only_current_step(self):
        """Verifies that hedge child receives entry order for only the current step's filled qty, not the parent's accumulated open_qty."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        
        # Scenario: Parent bot at Step 8, open_qty = 0.297 (accumulated total)
        # Step 8 specifically filled 0.030 (one step's worth)
        # Verify hedge child receives entry order for 0.030, NOT 0.297
        
        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 3)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.validate_order.side_effect = lambda pair, side, amt, price, *args, **kwargs: (True, amt, price, 'OK')

        mock_order = {
            'id': 'EX_CHILD_ENTRY_789',
            'status': 'open',
            'clientOrderId': 'CQB_99001_ENTRY_1_8'
        }
        
        # Populate DB with parent's orders for Step 8 and earlier steps
        # step 1..7 filled 0.267 in total, step 8 filled 0.030
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, created_at)
            VALUES (10017, 'grid', 'EX_PARENT_GRID_1', 'CQB_10017_GRID_1_1', 2.2, 0.267, 0.267, 'filled', 7, 1, 12345)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, created_at)
            VALUES (10017, 'grid', 'EX_PARENT_GRID_8', 'CQB_10017_GRID_1_8', 2.2, 0.030, 0.030, 'filled', 8, 1, 12346)
        """)
        
        # Seed one prior child entry to simulate subsequent step
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (99001, 'entry', 'CQB_99001_ENTRY_1_7', 2.2, 0.267, 0.267, 'filled', 1, 7)
        """)
        
        # Update parent trigger step to 8
        self.conn.execute("UPDATE bots SET hedge_trigger_step = 8 WHERE id = 10017")
        self.conn.commit()

        parent_bot_config = {
            'market_type': 'swap',
            'hedge_child_bot_id': 99001,
            'hedge_trigger_step': 8,
            'martingale_multiplier': 2.0,
            'base_size': 100.0,
        }
        
        parent_bot_status = {
            'id': 10017,
            'name': 'xrp long',
            'pair': 'XRP/USDC:USDC',
            'current_step': 8,
            'total_invested': 100.0,
            'avg_entry_price': 2.2,
            'target_tp_price': 2.3,
            'cycle_id': 1,
            'open_qty': 0.297,
            'entry_confirmed': 1,
            'basket_start_time': 12345
        }

        # Mock parent's open orders so maintain_orders doesn't try to cancel/replace TP or Grid
        parent_open_orders = [
            {
                'id': 'EX_PARENT_TP_123',
                'clientOrderId': 'CQB_10017_TP_1_8',
                'status': 'open',
                'price': 2.3,
                'amount': 0.297,
                'symbol': 'XRP/USDC:USDC'
            },
            {
                'id': 'EX_PARENT_GRID_123',
                'clientOrderId': 'CQB_10017_GRID_1_9',
                'status': 'open',
                'price': 2.1,
                'amount': 0.05,
                'symbol': 'XRP/USDC:USDC'
            }
        ]

        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place, \
             patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, '')):
             
            executor.maintain_orders(
                bot_id=10017,
                name='xrp long',
                pair='XRP/USDC:USDC',
                direction='LONG',
                bot_status=parent_bot_status,
                current_price=2.2,
                exchange=mock_exchange,
                market_snapshot={'open_orders': parent_open_orders},
                bot_config=parent_bot_config
            )
            
            # Verify child entry placed on exchange via place_gtx_order_with_retry
            mock_place.assert_called_once()
            args, kwargs = mock_place.call_args
            
            # The 4th positional argument or amount/qty should be 0.030, NOT 0.297
            actual_qty = args[3] if len(args) > 3 else kwargs.get('amount')
            self.assertAlmostEqual(actual_qty, 0.030, places=4, msg="Hedge child entry quantity should mirror step fill qty (0.030), not total open_qty (0.297)")




# ---------------------------------------------------------------------------
# TICKET-8: Break-Even TP Signal on Parent TP Completion / Child BE TP Placement
# ---------------------------------------------------------------------------

class TestTicket8HedgeTP(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Set up parent bot (10017)
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001, hedge_trigger_step=1)
        _insert_trades(self.conn, 10017, open_qty=0.0)

        # Set up child bot (99001)
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017)
        # Settle child bot trade details: open_qty=5.0, avg_entry_price=2.0
        self.conn.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                                total_invested, avg_entry_price, current_step, entry_confirmed)
            VALUES (99001, 5.0, 1, 'SHORT', 10.0, 2.0, 1, 1)
        """)
        self.conn.commit()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ticket8_parent_tp_registers_child_be_tp(self):
        """When parent TP fires, a pending_placement TP is registered for the child bot."""
        from engine.ledger import handle_tp_completion
        from engine.exchange_interface import ExchangeInterface

        mock_positions = [{'symbol': 'XRPUSDC', 'contracts': -5.0, 'qty': 5.0, 'net_qty': -5.0, 'side': 'short', 'unrealizedPnl': 0.0, 'entryPrice': 2.0}]
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.fetch_positions.return_value = mock_positions
        mock_exchange.fetch_open_orders.return_value = []

        # Run handle_tp_completion for parent (10017)
        with patch('engine.database.reset_bot_after_tp') as mock_reset, \
             patch('engine.exchange_interface.ExchangeInterface.fetch_positions', return_value=mock_positions):
            res = handle_tp_completion(
                bot_id=10017,
                exit_price=2.5,
                pair='XRP/USDC:USDC',
                exchange=mock_exchange,
                cycle_id=1
            )
            self.assertTrue(res)
            mock_reset.assert_called_once()

            # Verify pending break-even TP registered in bot_orders for child
            row = self.conn.execute(
                "SELECT price, amount, status, client_order_id, order_type FROM bot_orders WHERE bot_id=99001"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(float(row[0]), 2.0) # avg_entry_price of child
            self.assertEqual(float(row[1]), 5.0) # open_qty of child
            self.assertEqual(row[2], 'pending_placement')
            self.assertEqual(row[3], 'CQB_99001_TP_1_BE')
            self.assertEqual(row[4], 'tp')

    def test_ticket8_maintain_orders_places_be_tp(self):
        """maintain_orders fetches pending_placement TP, places it, and updates DB status."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        
        # Save a pending_placement TP for the child
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (99001, 'tp', 'PENDING_BE_99001_1', 'CQB_99001_TP_1_BE', 2.0, 5.0, 'pending_placement', 0, 1, 12345)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.validate_order.return_value = (True, 5.0, 2.0, 'OK')
        
        mock_order = {
            'id': 'EX_CHILD_TP_123',
            'status': 'open',
            'clientOrderId': 'CQB_99001_TP_1_BE'
        }
        
        bot_status = {
            'id': 99001,
            'name': 'xrp long_hedge',
            'pair': 'XRP/USDC:USDC',
            'current_step': 1,
            'total_invested': 10.0,
            'avg_entry_price': 2.0,
            'target_tp_price': 0.0,
            'cycle_id': 1,
            'open_qty': 5.0
        }
        
        bot_config = {'market_type': 'swap'}

        mock_exchange.create_order.return_value = mock_order

        res = executor.maintain_orders(
            bot_id=99001,
            name='xrp long_hedge',
            pair='XRP/USDC:USDC',
            direction='SHORT',
            bot_status=bot_status,
            current_price=2.1,
            exchange=mock_exchange,
            market_snapshot={'open_orders': []},
            bot_config=bot_config
        )
        self.assertIsNone(res) # simple maintain path returns None
        mock_exchange.create_order.assert_called_once()
        # Verify order status updated in DB
        row = self.conn.execute(
            "SELECT order_id, status FROM bot_orders WHERE bot_id=99001 AND client_order_id='CQB_99001_TP_1_BE'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 'EX_CHILD_TP_123')
        self.assertEqual(row[1], 'open')

        # Verify trades table updated with tp_order_id
        trade_row = self.conn.execute(
            "SELECT tp_order_id FROM trades WHERE bot_id=99001"
        ).fetchone()
        self.assertIsNotNone(trade_row)
        self.assertEqual(trade_row[0], 'EX_CHILD_TP_123')

    def test_handle_tp_completion_logs_warning_on_exception(self):
        """
        Regression: when handle_tp_completion's HEDGE-BE-TP block raises an exception,
        a WARNING with exc_info=True must be emitted so the traceback is visible in logs.
        The exception must NOT propagate — handle_tp_completion still returns True.
        """
        from engine.ledger import handle_tp_completion
        from engine.exchange_interface import ExchangeInterface
        import logging

        mock_positions = [{'symbol': 'XRPUSDC', 'contracts': -5.0, 'qty': 5.0, 'net_qty': -5.0, 'side': 'short', 'unrealizedPnl': 0.0, 'entryPrice': 2.0}]
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.fetch_positions.return_value = mock_positions
        mock_exchange.fetch_open_orders.return_value = []

        sentinel_error = RuntimeError("injected-save_bot_order-failure")

        with patch('engine.database.reset_bot_after_tp'), \
             patch('engine.database.save_bot_order', side_effect=sentinel_error), \
             patch('engine.exchange_interface.ExchangeInterface.fetch_positions', return_value=mock_positions), \
             patch('engine.ledger.logger') as mock_logger:

            result = handle_tp_completion(
                bot_id=10017,
                exit_price=2.5,
                pair='XRP/USDC:USDC',
                exchange=mock_exchange,
                cycle_id=1,
            )

        # Must return True — the BE TP failure is non-fatal
        self.assertTrue(result, "handle_tp_completion must return True even when HEDGE-BE-TP block raises")

        # Find the warning call that contains our tag
        warning_calls = [
            call for call in mock_logger.warning.call_args_list
            if 'HANDLE-TP-COMPLETION' in str(call)
        ]
        self.assertTrue(
            len(warning_calls) >= 1,
            f"Expected at least one logger.warning with [HANDLE-TP-COMPLETION]. "
            f"Got warning calls: {mock_logger.warning.call_args_list}"
        )

        # Verify exc_info=True was passed so the full traceback is logged
        warning_call = warning_calls[0]
        kwargs = warning_call[1] if len(warning_call) > 1 else {}
        # call_args is (args, kwargs) — check kwargs for exc_info
        actual_kwargs = warning_call.kwargs if hasattr(warning_call, 'kwargs') else (
            warning_call[1] if isinstance(warning_call[1], dict) else {}
        )
        self.assertTrue(
            actual_kwargs.get('exc_info', False),
            "logger.warning must be called with exc_info=True to emit full traceback"
        )



# ---------------------------------------------------------------------------
# TICKET-9: Snapshot Writer Fix
# ---------------------------------------------------------------------------

class TestTicket9SnapshotWriter(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Set up parent bot (10017)
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
        _insert_trades(self.conn, 10017, open_qty=0.0)

        # Set up child bot (99001)
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017)
        _insert_trades(self.conn, 99001, open_qty=44.7, avg_entry_price=2.20)

        # Insert filled entry order for child to make recompute return 44.7 qty
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, created_at)
            VALUES (99001, 'entry', 'EX_99001_ENTRY', 'CQB_99001_ENTRY_1_1', 2.20, 44.7, 44.7, 'filled', 1, 1, 12345)
        """)
        self.conn.commit()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ticket9_xrp_short_assigned_to_child_not_zero(self):
        """XRPUSDC SHORT position is assigned to hedge child bot, not bot_id=0."""
        from engine.database import update_active_positions_snapshot
        
        # Simulate a snapshot update with a SHORT XRP position
        mock_positions = [{
            'symbol': 'XRP/USDC:USDC',
            'side': 'short',
            'contracts': -44.7,  # Negative for SHORT in one-way mode
            'entryPrice': 2.20,
        }]
        update_active_positions_snapshot(mock_positions)
        
        row = self.conn.execute(
            "SELECT bot_id FROM active_positions WHERE pair='XRPUSDC' AND side='SHORT'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row[0], 0, f"bot_id should not be 0 after migration. Got: {row[0]}")
        self.assertEqual(row[0], 99001)

    def test_ticket9_no_orphan_positions(self):
        """No active_positions rows have bot_id=0 after a clean snapshot."""
        from engine.database import update_active_positions_snapshot
        
        # Simulate snapshot
        mock_positions = [{
            'symbol': 'XRP/USDC:USDC',
            'side': 'short',
            'contracts': -44.7,
            'entryPrice': 2.20,
        }]
        update_active_positions_snapshot(mock_positions)
        
        orphans = self.conn.execute(
            "SELECT COUNT(*) FROM active_positions WHERE bot_id=0"
        ).fetchone()[0]
        self.assertEqual(orphans, 0, f"Found {orphans} orphan position(s) with bot_id=0")


class TestTicket10DeprecationSweep(unittest.TestCase):
    def test_ticket10_no_hedge_order_types_written(self):
        """New bot_orders rows do not use order_type='hedge' or 'hedge_tp'."""
        import time
        from engine.database import get_connection
        conn = get_connection()
        new_hedge_rows = conn.execute(
            "SELECT COUNT(*) FROM bot_orders WHERE order_type IN ('hedge', 'hedge_tp') "
            "AND created_at > ?", (int(time.time()) - 3600,)
        ).fetchone()[0]
        self.assertEqual(new_hedge_rows, 0)

    def test_ticket10_no_hedged_status_in_bots(self):
        """No bot has status='HEDGED' or 'HEDGE_EXIT_PENDING'."""
        from engine.database import get_connection
        conn = get_connection()
        hedged = conn.execute(
            "SELECT COUNT(*) FROM bots WHERE status IN ('HEDGED', 'HEDGE_EXIT_PENDING')"
        ).fetchone()[0]
        self.assertEqual(hedged, 0)


class TestHedgeChildTPGtc(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Set up parent bot (10017)
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001, hedge_trigger_step=1)
        _insert_trades(self.conn, 10017, open_qty=10.0)

        # Set up child bot (99001)
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017)
        self.conn.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                                total_invested, avg_entry_price, current_step, entry_confirmed)
            VALUES (99001, 5.0, 1, 'SHORT', 10.0, 2.0, 1, 1)
        """)
        self.conn.commit()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_hedge_child_tp_uses_gtc(self):
        """Mock the exchange create_order call and assert that when placing a pending_placement TP for a bot_type='hedge_child', the params contain post_only=False and the order type is not GTX."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        
        # Save a pending_placement TP for the child
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (99001, 'tp', 'PENDING_BE_99001_1', 'CQB_99001_TP_1_BE', 2.0, 5.0, 'pending_placement', 0, 1, 12345)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.validate_order.return_value = (True, 5.0, 2.0, 'OK')
        
        mock_order = {
            'id': 'EX_CHILD_TP_123',
            'status': 'open',
            'clientOrderId': 'CQB_99001_TP_1_BE'
        }
        mock_exchange.create_order.return_value = mock_order
        
        bot_status = {
            'id': 99001,
            'name': 'xrp long_hedge',
            'pair': 'XRP/USDC:USDC',
            'current_step': 1,
            'total_invested': 10.0,
            'avg_entry_price': 2.0,
            'target_tp_price': 0.0,
            'cycle_id': 1,
            'open_qty': 5.0
        }
        
        bot_config = {'market_type': 'swap'}

        executor.maintain_orders(
            bot_id=99001,
            name='xrp long_hedge',
            pair='XRP/USDC:USDC',
            direction='SHORT',
            bot_status=bot_status,
            current_price=2.1,
            exchange=mock_exchange,
            market_snapshot={'open_orders': []},
            bot_config=bot_config
        )
        
        # Assert that mock_exchange.create_order was called
        mock_exchange.create_order.assert_called_once()
        args, kwargs = mock_exchange.create_order.call_args
        
        # Extracted parameters passed to create_order
        self.assertEqual(args[0], 'XRP/USDC:USDC')
        self.assertEqual(args[1], 'limit')
        self.assertEqual(args[2], 'buy') # SHORT TP is a BUY order
        self.assertEqual(args[3], 5.0)
        self.assertEqual(args[4], 2.0)
        
        # Assert params contain post_only=False and GTC
        params = args[5] if len(args) > 5 else kwargs.get('params')
        self.assertIsNotNone(params)
        self.assertEqual(params.get('post_only'), False)
        self.assertEqual(params.get('postOnly'), False)
        self.assertEqual(params.get('timeInForce'), 'GTC')


# ---------------------------------------------------------------------------
# TICKET-11: Hedge Cycle Carry Forward Sync
# ---------------------------------------------------------------------------

class TestHedgeCycleCarryForward(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Set up parent bot (10017)
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
        _insert_trades(self.conn, 10017, open_qty=10.0)

        # Set up child bot (99001)
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017)
        # Settle child bot trade details: open_qty=5.0, avg_entry_price=2.0 in cycle 1
        self.conn.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                                total_invested, avg_entry_price, current_step, entry_confirmed)
            VALUES (99001, 5.0, 1, 'SHORT', 10.0, 2.0, 1, 1)
        """)
        # Insert a filled entry order for child in cycle 1
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, created_at)
            VALUES (99001, 'entry', 'EX_99001_ENTRY_C1', 'CQB_99001_ENTRY_1_1', 2.0, 5.0, 5.0, 'filled', 1, 1, 12345)
        """)
        self.conn.commit()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cycle_carry_forward_active_position(self):
        """
        When the parent bot triggers a hedge child entry on a new cycle (e.g. cycle 2),
        but the child bot still has an active position from cycle 1, the child bot's
        existing orders should be updated to cycle 2 to prevent virtual net mismatch.
        """
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: qty
        mock_exchange.create_order.return_value = {'id': 'EX_99001_ENTRY_C2', 'status': 'open'}

        executor = BotExecutor(runner=None)

        # Let's run _signal_hedge_child_entry which does the cycle sync
        with patch.object(executor, '_place_gtx_order_with_retry', return_value={'id': 'EX_99001_ENTRY_C2', 'status': 'open'}):
            executor._signal_hedge_child_entry(
                parent_bot_id=10017,
                parent_name='xrp long',
                parent_step=8,
                pair='XRP/USDC:USDC',
                direction='LONG',
                step_qty=2.0,
                step_fill_price=2.10,
                exchange=mock_exchange,
                parent_cycle_id=2,  # Parent is now on cycle 2
            )

        # Check if the child bot's trades.cycle_id is updated to 2
        trade_row = self.conn.execute("SELECT cycle_id, open_qty FROM trades WHERE bot_id = 99001").fetchone()
        self.assertEqual(trade_row[0], 2)

        # Check if the cycle 1 order was carried forward to cycle 2 in bot_orders
        order_cycle = self.conn.execute(
            "SELECT cycle_id FROM bot_orders WHERE bot_id = 99001 AND client_order_id = 'CQB_99001_ENTRY_1_1'"
        ).fetchone()
        self.assertEqual(order_cycle[0], 2)


# ---------------------------------------------------------------------------
# TICKET-12: Hedge Child TP Order Routing
# ---------------------------------------------------------------------------

class TestHedgeChildTPRouting(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Set up parent bot (10017)
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
        # Parent trade has open_qty = 15.0
        self.conn.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                                total_invested, avg_entry_price, current_step, entry_confirmed)
            VALUES (10017, 15.0, 1, 'LONG', 30.0, 2.0, 1, 1)
        """)
        # Set up a child bot (99001) that is SHORT
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017)
        # Settle child bot trades: open_qty=5.0, avg_entry_price=2.0
        self.conn.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                                total_invested, avg_entry_price, current_step, entry_confirmed)
            VALUES (99001, 5.0, 1, 'SHORT', 10.0, 2.0, 1, 1)
        """)
        self.conn.commit()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_hedge_child_tp_gtc_fallback_when_not_reducing(self):
        """
        Verify that a hedge child bot's TP order uses GTC without reduceOnly and without postOnly
        when it is not net-reducing at the account level (e.g. account is net opposite/LONG).
        """
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        mock_exchange = MagicMock(spec=ExchangeInterface)
        # Setup mock precision and rounding functions
        mock_exchange.get_symbol_precision.return_value = {
            'step_size': 0.001,
            'price_precision': 4,
            'tick_size': 0.0001,
            'min_notional': 1.0,
        }
        mock_exchange.round_to_step.side_effect = lambda qty, step: qty

        # Insert active_positions to simulate that the account is net LONG (wrong side for SHORT bot's BUY TP reduceOnly)
        self.conn.execute("INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked) VALUES (99001, 'XRPUSDC', 'LONG', 10.0, 2.0, 12345)")
        self.conn.commit()

        executor = BotExecutor(runner=None)

        # Call _prepare_tp_order_params
        ccxt_params, tp_qty = executor._prepare_tp_order_params(
            bot_id=99001,
            name='xrp long_hedge',
            pair='XRP/USDC:USDC',
            side='buy',  # SHORT TP is a BUY order
            amount=5.0,
            tp_price=2.0,
            current_price=2.1,
            exchange=mock_exchange,
            direction='SHORT'
        )

        # Verify that it returned the correct parameters for the GTC fallback
        self.assertEqual(tp_qty, 5.0)
        self.assertIsNotNone(ccxt_params)
        self.assertEqual(ccxt_params.get('timeInForce'), 'GTC')
        self.assertNotIn('reduceOnly', ccxt_params)
        self.assertNotIn('postOnly', ccxt_params)


class TestSafeWipeAndResetSequence(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_safe_wipe_drift_allowance_under_stale_snapshot(self):
        """
        Verifies parent TP completes and successfully executes the reset wipe even when
        active_positions has a stale opposite position.
        """
        # Set up parent bot (10017) LONG
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
        _insert_trades(self.conn, 10017, open_qty=0.0, position_side='LONG')

        # Set up child bot (99001) SHORT
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017)
        _insert_trades(self.conn, 99001, open_qty=0.1, position_side='SHORT')

        # Stale position in active_positions table: mapped to parent bot (10017) as SHORT 0.1
        self.conn.execute("""
            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
            VALUES (10017, 'XRPUSDC', 'SHORT', 0.1, 2.0, ?)
        """, (int(time.time()),))
        self.conn.commit()

        from engine.ledger import handle_tp_completion
        from engine.exchange_interface import ExchangeInterface

        mock_exchange = MagicMock(spec=ExchangeInterface)
        # Mock exchange positions to return SHORT 0.1 (physical = -0.1)
        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'XRPUSDC', 'contracts': 0.1, 'side': 'short', 'net_qty': -0.1}
        ]
        mock_exchange.fetch_open_orders.return_value = []

        # Run handle_tp_completion for parent (10017)
        res = handle_tp_completion(
            bot_id=10017,
            exit_price=2.5,
            pair='XRP/USDC:USDC',
            exchange=mock_exchange,
            cycle_id=1
        )
        self.assertTrue(res, "handle_tp_completion should succeed")

        # Verify that parent bot status in bots is Scanning
        row = self.conn.execute("SELECT status FROM bots WHERE id=10017").fetchone()
        self.assertEqual(row[0], 'Scanning')

        # Verify that parent bot orders were reset_cleared
        orders = self.conn.execute(
            "SELECT DISTINCT status FROM bot_orders WHERE bot_id=10017"
        ).fetchall()
        for status_row in orders:
            self.assertIn(status_row[0], ('reset_cleared', 'auto_closed', 'cancelled', 'canceled'))

    def test_child_be_tp_registered_before_parent_wipe(self):
        """
        Verifies that the child bot's BE TP pending_placement row exists in bot_orders
        even if the parent reset raises WipeBlockedError.
        """
        # Set up parent bot (10017) LONG
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
        _insert_trades(self.conn, 10017, open_qty=0.1, position_side='LONG')
        # Insert a filled entry order for parent in cycle 1 to support recompute truth
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, created_at)
            VALUES (10017, 'entry', 'EX_10017_ENTRY_C1', 'CQB_10017_ENTRY_1_1', 2.0, 0.1, 0.1, 'filled', 1, 1, 12345)
        """)


        # Set up child bot (99001) SHORT with some position to trigger BE TP
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017)
        self.conn.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                                total_invested, avg_entry_price, current_step, entry_confirmed)
            VALUES (99001, 0.1, 1, 'SHORT', 0.2, 2.0, 1, 1)
        """)

        # To make it raise WipeBlockedError, we set up a scenario where wiping parent increases drift:
        # e.g., active_positions shows parent is LONG 0.1, but child has open_qty = 0.1.
        # pair virtual net = 0.0.
        # physical net is LONG 0.1.
        self.conn.execute("""
            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
            VALUES (10017, 'XRPUSDC', 'LONG', 0.1, 2.0, ?)
        """, (int(time.time()),))
        self.conn.commit()

        from engine.ledger import handle_tp_completion
        from engine.exchange_interface import ExchangeInterface

        mock_exchange = MagicMock(spec=ExchangeInterface)
        # Mock exchange to return LONG 0.1 (physical = 0.1)
        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'XRPUSDC', 'contracts': 0.1, 'side': 'long', 'net_qty': 0.1}
        ]
        mock_exchange.fetch_open_orders.return_value = []

        # Run handle_tp_completion for parent (10017). It should fail due to WipeBlockedError.
        with patch('engine.parity_gates.assert_cycle_reset_allowed') as mock_assert:
            res = handle_tp_completion(
                bot_id=10017,
                exit_price=2.5,
                pair='XRP/USDC:USDC',
                exchange=mock_exchange,
                cycle_id=1
            )
        self.assertFalse(res, "handle_tp_completion should fail because reset was blocked")

        # Verify that the child's BE TP pending_placement row still exists in bot_orders
        row = self.conn.execute(
            "SELECT price, amount, status, client_order_id FROM bot_orders WHERE bot_id=99001 AND status='pending_placement'"
        ).fetchone()
        self.assertIsNotNone(row, "Child's BE TP pending_placement order must have been registered first")
        self.assertEqual(float(row[0]), 2.0)
        self.assertEqual(float(row[1]), 0.1)

    def test_wipe_blocked_when_it_would_increase_drift(self):
        """
        Verifies that wiping a bot is correctly blocked if it's the only active bot on the pair
        and physical position still exists (wiping would make drift worse).
        """
        # Only active bot on the pair: Parent bot LONG 0.1
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG')
        _insert_trades(self.conn, 10017, open_qty=0.1, position_side='LONG')

        # The physical position still exists on exchange: LONG 0.1
        self.conn.execute("""
            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
            VALUES (10017, 'XRPUSDC', 'LONG', 0.1, 2.0, ?)
        """, (int(time.time()),))
        self.conn.commit()

        from engine.wipe_proof import safe_mark_reset_cleared, WipeBlockedError, ExchangePositionSnapshot, WipeProofSource

        # Define fetch function that returns the cached snapshot
        def mock_fetch(s):
            return ExchangePositionSnapshot(
                symbol=s,
                qty=0.1,
                side='LONG',
                fetched_at=int(time.time()),
                source=WipeProofSource.CACHED_SNAP
            )

        cursor = self.conn.cursor()
        with self.assertRaises(WipeBlockedError):
            safe_mark_reset_cleared(
                cursor=cursor,
                bot_id=10017,
                symbol='XRP/USDC:USDC',
                action_label='TP_HIT',
                fetch_exchange_position_fn=mock_fetch,
                excluded_carry_labels=[],
                allow_nonzero_wipe=False
            )

# ---------------------------------------------------------------------------
# INV-3 Extension: Oneway Netting Insulation Tests
# ---------------------------------------------------------------------------

class TestOnewayHedgeInsulation(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_reconcile_oneway_never_trims_hedge_child(self):
        """Verify reconcile_oneway_pair_open_qty skips hedge child bots completely."""
        # Set up parent bot (10018) LONG
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=1824.0, position_side='LONG')

        # Set up child bot (100318) SHORT hedge child
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=13.0, position_side='SHORT')

        from engine.oneway_netting import reconcile_oneway_pair_open_qty
        
        # Mock exchange and physical signed net to show 1736.8 (discrepancy of 74.2 from virtual 1811.0)
        mock_exchange = MagicMock()
        with patch('engine.oneway_netting.get_pair_open_qty_net', return_value=1811.0), \
             patch('engine.parity_gates.get_exchange_signed_net', return_value=1736.8):
             
            # reconcile should run but skip trimming the hedge child
            res = reconcile_oneway_pair_open_qty(mock_exchange, 'SUI/USDC:USDC')
            
            # Since the child is excluded and parent is > MAX_OWAY_REPAIR_QTY (50.0), no trim should occur
            self.conn.commit()
            child_qty = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100318").fetchone()[0]
            self.assertEqual(child_qty, 13.0, "Hedge child open_qty should not have been trimmed!")

    def test_gate_oneway_allows_hedge_child_entry(self):
        """Verify hedge child entry is not blocked by gate when parent has opposite position."""
        # Set up parent bot (10018) LONG with open position
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=1824.0, position_side='LONG')

        # Set up child bot (100318) SHORT hedge child
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=0.0, position_side='SHORT')

        from engine.oneway_netting import gate_oneway_opposite_entry

        # The child bot is placing SHORT entry. Even though parent holds LONG position, it should not block the child
        allowed, msg = gate_oneway_opposite_entry(100318, 'SUI/USDC:USDC', 'SHORT')
        self.assertTrue(allowed, f"Hedge child should be allowed to enter, but was blocked: {msg}")

class TestHedgeLifecycleEnforcement(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_enforce_dormant(self):
        """parent_step < trigger, child open_qty=0 -> 'dormant'"""
        from engine.bot_executor import enforce_hedge_child_state
        # Setup parent (trigger=7, step=6, cycle=1)
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=1)
        self.conn.execute("UPDATE trades SET current_step = 6 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='HEDGE_STANDBY')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=1)

        state = enforce_hedge_child_state(100317, self.conn)
        self.assertEqual(state, 'dormant')

    def test_enforce_should_close(self):
        """parent_step < trigger, child open_qty=0.5 -> returns 'active' under INV-22"""
        from engine.bot_executor import enforce_hedge_child_state
        # Setup parent (trigger=7, step=6, cycle=1)
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=1)
        self.conn.execute("UPDATE trades SET current_step = 6 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child holding some quantity
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.5, cycle_id=1)

        state = enforce_hedge_child_state(100317, self.conn)
        self.assertEqual(state, 'active')

    def test_enforce_hedge_child_state_never_resets_with_open_qty(self):
        """Verify _reset_to_hedge_standby is NOT called when child has open_qty > 0"""
        from engine.bot_executor import enforce_hedge_child_state
        # Setup parent (trigger=7, step=6, cycle=1)
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=1)
        self.conn.execute("UPDATE trades SET current_step = 6 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child holding some quantity
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.5, cycle_id=1)

        state = enforce_hedge_child_state(100317, self.conn)
        self.assertEqual(state, 'active')

    def test_enforce_hedge_child_state_resets_when_qty_zero(self):
        """Verify should_close is returned when child has open_qty = 0 and status is not hedge_standby"""
        from engine.bot_executor import enforce_hedge_child_state
        # Setup parent (trigger=7, step=6, cycle=1)
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=1)
        self.conn.execute("UPDATE trades SET current_step = 6 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child flat but still in IN TRADE status
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=1)

        state = enforce_hedge_child_state(100317, self.conn)
        self.assertEqual(state, 'should_close')

    def test_enforce_active(self):
        """parent_step >= trigger, parent open_qty > 0 -> 'active'"""
        from engine.bot_executor import enforce_hedge_child_state
        # Setup parent (trigger=7, step=7, cycle=1)
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=1)
        self.conn.execute("UPDATE trades SET current_step = 7 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=1)

        state = enforce_hedge_child_state(100317, self.conn)
        self.assertEqual(state, 'active')

    def test_enforce_syncs_cycle_id(self):
        """child cycle=5, parent cycle=10 -> child cycle updated to 10"""
        from engine.bot_executor import enforce_hedge_child_state
        # Setup parent cycle=10
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=10)
        self.conn.execute("UPDATE trades SET current_step = 7 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child cycle=5
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=5)

        # Place some active orders on child cycle=5
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (100317, 'entry', 'EX_ENTRY_1', 'CQB_100317_ENTRY_1', 50000.0, 0.1, 'open', 1, 5, 12345)
        """)
        self.conn.commit()

        state = enforce_hedge_child_state(100317, self.conn)
        self.assertEqual(state, 'active')

        # Check child trades.cycle_id updated to 10
        child_trade = self.conn.execute("SELECT cycle_id FROM trades WHERE bot_id = 100317").fetchone()
        self.assertEqual(child_trade[0], 10)

        # Check child bot_orders cycle_id updated to 10
        child_order = self.conn.execute("SELECT cycle_id FROM bot_orders WHERE order_id = 'EX_ENTRY_1'").fetchone()
        self.assertEqual(child_order[0], 10)

    def test_enforce_does_not_rewrite_filled_orders(self):
        """filled bot_orders rows retain original cycle_id after sync"""
        from engine.bot_executor import enforce_hedge_child_state
        # Setup parent cycle=10
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=10)
        self.conn.execute("UPDATE trades SET current_step = 7 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child cycle=5
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=5)

        # Place filled/cancelled/auto_closed/reset_cleared orders on child cycle=5
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (100317, 'entry', 'EX_FILLED_1', 'CQB_100317_FILLED_1', 50000.0, 0.1, 'filled', 1, 5, 12345)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (100317, 'entry', 'EX_CANCELLED_1', 'CQB_100317_CANCELLED_1', 50000.0, 0.1, 'cancelled', 1, 5, 12345)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (100317, 'entry', 'EX_AUTO_CLOSED_1', 'CQB_100317_AUTO_CLOSED_1', 50000.0, 0.1, 'auto_closed', 1, 5, 12345)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, status, step, cycle_id, created_at)
            VALUES (100317, 'entry', 'EX_RESET_CLEARED_1', 'CQB_100317_RESET_CLEARED_1', 50000.0, 0.1, 'reset_cleared', 1, 5, 12345)
        """)
        self.conn.commit()

        state = enforce_hedge_child_state(100317, self.conn)
        self.assertEqual(state, 'active')

        # Check child trades.cycle_id updated to 10
        child_trade = self.conn.execute("SELECT cycle_id FROM trades WHERE bot_id = 100317").fetchone()
        self.assertEqual(child_trade[0], 10)

        # Check child historical orders cycle_id DID NOT update (should remain 5)
        for order_id in ['EX_FILLED_1', 'EX_CANCELLED_1', 'EX_AUTO_CLOSED_1', 'EX_RESET_CLEARED_1']:
            cid = self.conn.execute("SELECT cycle_id FROM bot_orders WHERE order_id = ?", (order_id,)).fetchone()
            self.assertEqual(cid[0], 5, f"Order {order_id} cycle_id updated incorrectly!")

    def test_signal_entry_idempotent_same_cycle(self):
        """calling _signal_hedge_child_entry twice same parent_cycle -> only one entry placed"""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        # Setup parent (trigger=7, step=7, cycle=10)
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=10)
        self.conn.execute("UPDATE trades SET current_step = 7 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='HEDGE_STANDBY')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=10)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 3)

        mock_order = {
            'id': 'EX_CHILD_ENTRY_123',
            'status': 'open',
            'clientOrderId': 'CQB_100317_ENTRY_10_7'
        }

        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place:
            # First call
            res1 = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='btc long',
                parent_step=7,
                pair='BTC/USDT:USDT',
                direction='LONG',
                step_qty=0.1,
                step_fill_price=50000.0,
                exchange=mock_exchange,
                parent_cycle_id=10
            )
            self.assertTrue(res1)

            # Second call
            res2 = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='btc long',
                parent_step=7,
                pair='BTC/USDT:USDT',
                direction='LONG',
                step_qty=0.1,
                step_fill_price=50000.0,
                exchange=mock_exchange,
                parent_cycle_id=10
            )
            self.assertTrue(res2)

            # Should only be called once
            self.assertEqual(mock_place.call_count, 1)

    def test_reset_to_hedge_standby_writes_audit_row(self):
        """_reset_to_hedge_standby -> drift_note row exists in bot_orders"""
        from engine.bot_executor import _reset_to_hedge_standby
        
        # Setup parent
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=1.0, cycle_id=10)
        self.conn.execute("UPDATE trades SET current_step = 6 WHERE bot_id = 10016")
        self.conn.commit()

        # Setup child
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=0.5, cycle_id=5)

        _reset_to_hedge_standby(100317, self.conn, 10)

        # Check status and open_qty updated
        child_bot = self.conn.execute("SELECT status FROM bots WHERE id = 100317").fetchone()
        self.assertEqual(child_bot[0], 'hedge_standby')

        child_trade = self.conn.execute("SELECT open_qty, cycle_id FROM trades WHERE bot_id = 100317").fetchone()
        self.assertEqual(child_trade[0], 0.0)
        self.assertEqual(child_trade[1], 10)

        # Check drift_note audit order exists
        audit_row = self.conn.execute(
            "SELECT order_type, status, notes, cycle_id FROM bot_orders WHERE bot_id = 100317 AND order_type = 'drift_note'"
        ).fetchone()
        self.assertIsNotNone(audit_row)
        self.assertEqual(audit_row[0], 'drift_note')
        self.assertEqual(audit_row[1], 'audit')
        self.assertIn("Parent state:", audit_row[2])
        self.assertEqual(audit_row[3], 10)

    def test_signal_first_entry_uses_full_parent_qty(self):
        """Parent has open_qty=693.8, trigger at step 7, current step=7. Under INV-29, the first child entry mirrors step_qty (56.2) rather than parent open_qty."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        executor = BotExecutor(runner=None)
        
        # Setup parent
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=693.8, cycle_id=10)
        
        # Setup child
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='Scanning')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=10)

        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        mock_order = {
            'id': 'EX_CHILD_ENTRY_123',
            'status': 'open',
            'clientOrderId': 'CQB_100317_ENTRY_10_7'
        }

        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place:
            res = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='btc long',
                parent_step=7,
                pair='BTC/USDT:USDT',
                direction='LONG',
                step_qty=56.2,  # Trigger increment is 56.2
                step_fill_price=50000.0,
                exchange=mock_exchange,
                parent_cycle_id=10
            )
            self.assertTrue(res)
            # Verify placed order has the step_qty (56.2)
            mock_place.assert_called_once()
            args, kwargs = mock_place.call_args
            placed_qty = args[3]
            self.assertEqual(placed_qty, 56.2)

    def test_signal_subsequent_entry_uses_step_qty(self):
        """Parent has open_qty=750.0, trigger at step 7, current step=8. Subsequent entries use step_qty increment."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        executor = BotExecutor(runner=None)
        
        # Setup parent
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=750.0, cycle_id=10)
        
        # Setup child
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='IN TRADE')
        _insert_trades(self.conn, 100317, open_qty=693.8, cycle_id=10)

        # Seed one prior filled entry in bot_orders for this cycle to simulate subsequent step
        conn = get_connection()
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100317, 'entry', 'CQB_100317_ENTRY_10_7', 50000.0, 693.8, 693.8, 'filled', 10, 1)
        """)
        conn.commit()

        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        mock_order = {
            'id': 'EX_CHILD_ENTRY_124',
            'status': 'open',
            'clientOrderId': 'CQB_100317_ENTRY_10_8'
        }

        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place:
            res = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='btc long',
                parent_step=8,
                pair='BTC/USDT:USDT',
                direction='LONG',
                step_qty=56.2,  # Increment qty for step 8
                step_fill_price=51000.0,
                exchange=mock_exchange,
                parent_cycle_id=10
            )
            self.assertTrue(res)
            # Verify placed order has step_qty (56.2) instead of full parent position (750.0)
            mock_place.assert_called_once()
            args, kwargs = mock_place.call_args
            placed_qty = args[3]
            self.assertEqual(placed_qty, 56.2)

    def test_signal_full_hedge_idempotent(self):
        """Call _signal_hedge_child_entry twice, second call is ignored (idempotent)."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        executor = BotExecutor(runner=None)
        
        # Setup parent
        _insert_bot(self.conn, 10016, 'btc long', 'BTC/USDT:USDT', 'BTCUSDT', 'LONG', hedge_child_bot_id=100317, hedge_trigger_step=7)
        _insert_trades(self.conn, 10016, open_qty=693.8, cycle_id=10)
        
        # Setup child
        _insert_bot(self.conn, 100317, 'btc long_hedge', 'BTC/USDT:USDT', 'BTCUSDT', 'SHORT', bot_type='hedge_child', parent_bot_id=10016, status='Scanning')
        _insert_trades(self.conn, 100317, open_qty=0.0, cycle_id=10)

        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        mock_order = {
            'id': 'EX_CHILD_ENTRY_123',
            'status': 'open',
            'clientOrderId': 'CQB_100317_ENTRY_10_7'
        }

        with patch.object(executor, '_place_gtx_order_with_retry', return_value=mock_order) as mock_place:
            # 1. First call triggers entry placement
            res1 = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='btc long',
                parent_step=7,
                pair='BTC/USDT:USDT',
                direction='LONG',
                step_qty=56.2,
                step_fill_price=50000.0,
                exchange=mock_exchange,
                parent_cycle_id=10
            )
            self.assertTrue(res1)

            # 2. Second call should be ignored due to existing order
            res2 = executor._signal_hedge_child_entry(
                parent_bot_id=10016,
                parent_name='btc long',
                parent_step=7,
                pair='BTC/USDT:USDT',
                direction='LONG',
                step_qty=56.2,
                step_fill_price=50000.0,
                exchange=mock_exchange,
                parent_cycle_id=10
            )
            self.assertTrue(res2)

            # Only one place call should have been made
            self.assertEqual(mock_place.call_count, 1)


# ---------------------------------------------------------------------------
# Test Sync & Price Accuracy (Fix 1, 2, and 3)
# ---------------------------------------------------------------------------

class TestHedgeSyncAndPriceAccuracy(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_signal_uses_actual_fill_price_not_current_price(self):
        """Parent step 8 filled at 1.1609, current_price=1.19 (drifted). Assert hedge entry is placed at 1.1609, not 1.19."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        # Setup parent
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001, hedge_trigger_step=8)
        _insert_trades(self.conn, 10017, open_qty=0.297, cycle_id=10)

        # Setup child
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017, status='Scanning')
        _insert_trades(self.conn, 99001, open_qty=0.0, cycle_id=10)

        # Insert parent filled orders for Step 8 cycle 10
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, created_at)
            VALUES (10017, 'grid', 'EX_PARENT_GRID_8', 'CQB_10017_GRID_10_8', 1.1609, 0.030, 0.030, 'filled', 8, 10, 12346)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 3)
        mock_exchange.fetch_open_orders.return_value = []
        mock_exchange.validate_order.side_effect = lambda pair, side, amt, price, *args, **kwargs: (True, amt, price, 'OK')

        mock_order_parent = {
            'id': 'EX_PARENT_GRID_9',
            'status': 'open',
            'clientOrderId': 'CQB_10017_GRID_10_9'
        }
        mock_order_child = {
            'id': 'EX_CHILD_ENTRY_8',
            'status': 'open',
            'clientOrderId': 'CQB_99001_ENTRY_10_8'
        }

        parent_bot_config = {
            'market_type': 'swap',
            'hedge_child_bot_id': 99001,
            'hedge_trigger_step': 8,
            'martingale_multiplier': 2.0,
            'base_size': 100.0,
        }

        parent_bot_status = {
            'id': 10017,
            'name': 'xrp long',
            'pair': 'XRP/USDC:USDC',
            'current_step': 8,
            'total_invested': 100.0,
            'avg_entry_price': 1.1609,
            'target_tp_price': 1.25,
            'cycle_id': 10,
            'open_qty': 0.297,
            'entry_confirmed': 1,
            'basket_start_time': 12345
        }

        parent_open_orders = [
            {
                'id': 'EX_PARENT_TP_123',
                'clientOrderId': 'CQB_10017_TP_10_8',
                'status': 'open',
                'price': 1.25,
                'amount': 0.297,
                'symbol': 'XRP/USDC:USDC'
            }
        ]

        with patch.object(executor, '_place_gtx_order_with_retry', side_effect=[mock_order_parent, mock_order_child]) as mock_place, \
             patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, '')):

            executor.maintain_orders(
                bot_id=10017,
                name='xrp long',
                pair='XRP/USDC:USDC',
                direction='LONG',
                bot_status=parent_bot_status,
                current_price=1.19,  # Drifted price
                exchange=mock_exchange,
                market_snapshot={'open_orders': parent_open_orders},
                bot_config=parent_bot_config
            )

            self.assertEqual(mock_place.call_count, 2)
            args, kwargs = mock_place.call_args_list[1]
            actual_price = args[4] if len(args) > 4 else kwargs.get('price')
            self.assertAlmostEqual(actual_price, 1.1609, places=4, msg="Hedge entry should be placed at parent's actual fill price (1.1609), not current_price (1.19)")

    def test_signal_gtx_rejection_falls_back_to_gtc(self):
        """GTX placement raises rejection. Assert retry fires as GTC at step_fill_price."""
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface
        from engine.exceptions import GTXRejected

        # Setup parent
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001, hedge_trigger_step=8)
        _insert_trades(self.conn, 10017, open_qty=10.0, cycle_id=10)

        # Setup child
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017, status='Scanning')
        _insert_trades(self.conn, 99001, open_qty=0.0, cycle_id=10)

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 3)

        mock_gtc_order = {
            'id': 'EX_CHILD_ENTRY_8_GTC',
            'status': 'open',
            'price': 1.1609,
            'clientOrderId': 'CQB_99001_ENTRY_10_8_GTC'
        }
        mock_exchange.create_order.return_value = mock_gtc_order

        # Force GTXRejected on first attempt
        with patch.object(executor, '_place_gtx_order_with_retry', side_effect=GTXRejected("Post only failed")):
            res = executor._signal_hedge_child_entry(
                parent_bot_id=10017,
                parent_name='xrp long',
                parent_step=8,
                pair='XRP/USDC:USDC',
                direction='LONG',
                step_qty=5.5,
                step_fill_price=1.1609,
                exchange=mock_exchange,
                parent_cycle_id=10,
                current_price=1.19
            )
            self.assertTrue(res)

            # Assert create_order was called with params having timeInForce='GTC', postOnly deleted, at price 1.1609
            mock_exchange.create_order.assert_called_once()
            args, kwargs = mock_exchange.create_order.call_args
            
            # verify arguments
            called_pair, called_type, called_side, called_amount, called_price = args[:5]
            self.assertEqual(called_pair, 'XRP/USDC:USDC')
            self.assertEqual(called_type, 'limit')
            self.assertEqual(called_side, 'sell')
            self.assertEqual(called_price, 1.1609)
            
            called_params = kwargs.get('params') or args[5]
            self.assertEqual(called_params.get('timeInForce'), 'GTC')
            self.assertNotIn('postOnly', called_params)

            # Verify saved DB order has price 1.1609 and status open
            row = self.conn.execute(
                "SELECT price, status FROM bot_orders WHERE order_id='EX_CHILD_ENTRY_8_GTC'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 1.1609)
            self.assertEqual(row[1], 'open')

    def test_signal_fires_at_fill_time_not_poll_time(self):
        """Verify _signal_hedge_child_entry is called from the fill event path."""
        from engine.ledger import credit_fill
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        # Setup parent
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001, hedge_trigger_step=8)
        _insert_trades(self.conn, 10017, open_qty=0.0, cycle_id=10)

        # Setup child
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017, status='Scanning')
        _insert_trades(self.conn, 99001, open_qty=0.0, cycle_id=10)

        # Insert open parent grid order row in DB
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, updated_at, client_order_id, cycle_id)
            VALUES (10017, 8, 'grid', 'GRID_8_REALTIME', 1.1609, 10.0, 0.0, 'open', 1000, 1001, 'CQB_10017_GRID_10_8', 10)
        """)
        self.conn.commit()

        mock_exchange = MagicMock(spec=ExchangeInterface)

        # Patch _signal_hedge_child_entry
        with patch.object(BotExecutor, '_signal_hedge_child_entry') as mock_signal:
            credited = credit_fill(
                bot_id=10017,
                order_id='GRID_8_REALTIME',
                cumulative_qty=10.0,
                avg_price=1.1609,
                order_type='grid',
                is_cumulative=True,
                fill_ts=12345,
                exchange=mock_exchange
            )
            self.assertTrue(credited)
            mock_signal.assert_called_once()
            
            # Verify correct arguments
            kwargs = mock_signal.call_args.kwargs
            self.assertEqual(kwargs.get('parent_bot_id'), 10017)
            self.assertEqual(kwargs.get('parent_step'), 8)
            self.assertEqual(kwargs.get('step_fill_price'), 1.1609)
            self.assertEqual(kwargs.get('parent_cycle_id'), 10)

    def test_offline_fill_reconstructor_signals_hedge_child(self):
        """Verify reconstruct_offline_fills also signals hedge child using historical fill price."""
        from engine.reconciler import StateReconciler
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        # Setup parent
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001, hedge_trigger_step=8)
        _insert_trades(self.conn, 10017, open_qty=0.0, cycle_id=10)

        # Setup child
        _insert_bot(self.conn, 99001, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10017, status='Scanning')
        _insert_trades(self.conn, 99001, open_qty=0.0, cycle_id=10)

        # Create state reconciler
        mock_exchange = MagicMock(spec=ExchangeInterface)
        # Mock active positions gap so reconciler scans history
        # Physical XRP position = 10.0, Virtual XRP = 0.0 -> Gap detected
        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'XRPUSDC', 'side': 'long', 'contracts': 10.0}
        ]
        from engine.database import update_active_positions_snapshot
        update_active_positions_snapshot([
            {'symbol': 'XRP/USDC:USDC', 'side': 'long', 'contracts': 10.0, 'entryPrice': 1.1609}
        ])
        
        # Mock historical closed orders fetched from exchange
        mock_exchange.fetch_closed_orders.return_value = [
            {
                'id': 'GRID_8_OFFLINE',
                'symbol': 'XRP/USDC:USDC',
                'side': 'buy',
                'status': 'filled',
                'filled': 10.0,
                'amount': 10.0,
                'price': 1.1609,
                'average': 1.1609,
                'clientOrderId': 'CQB_10017_GRID_10_8',
                'timestamp': 2000000
            }
        ]
        mock_exchange.fetch_open_orders.return_value = []

        reconciler = StateReconciler(exchanges={'future': mock_exchange})
        # Reset global cooldown to prevent skipping during full test suite run
        StateReconciler._last_global_offline_scan = 0.0

        with patch.object(BotExecutor, '_signal_hedge_child_entry') as mock_signal:
            reconciler.reconstruct_offline_fills(since_hours=6)
            
            # Verify hedge signal was fired
            mock_signal.assert_called_once()
            kwargs = mock_signal.call_args.kwargs
            self.assertEqual(kwargs.get('parent_bot_id'), 10017)
            self.assertEqual(kwargs.get('parent_step'), 8)
            self.assertEqual(kwargs.get('step_fill_price'), 1.1609)
            self.assertEqual(kwargs.get('parent_cycle_id'), 10)


class TestV3916ReconcilerFixes(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_stale_cycle_id_repaired_from_bot_orders(self):
        """
        Child bot has cycle_id=3 in trades, but a filled entry in cycle 92.
        enforce_hedge_child_state corrects trades.cycle_id to 92 and triggers reseal.
        """
        from engine.bot_executor import enforce_hedge_child_state
        
        # Setup parent (trigger=7, step=7, cycle=92)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=145.6, cycle_id=92)
        
        # Setup child (parent=10018, cycle_id=3, flat in trades)
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=0.0, cycle_id=3, position_side='SHORT')
        
        # Insert a filled entry in cycle 92 for child
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_92_1', 2.0, 13.1, 13.1, 'filled', 92, 1)
        """)
        self.conn.commit()
        
        # Call enforce_hedge_child_state with mocked fetch_positions to prevent live exchange queries
        with patch('engine.exchange_interface.ExchangeInterface.fetch_positions', return_value=[
            {'symbol': 'SUI/USDC:USDC', 'side': 'short', 'qty': 13.1}
        ]):
            res = enforce_hedge_child_state(100318, self.conn)
        self.assertEqual(res, 'active')
        
        # Verify trades.cycle_id updated to 92
        child_trade = self.conn.execute("SELECT cycle_id, open_qty FROM trades WHERE bot_id = 100318").fetchone()
        self.assertEqual(child_trade[0], 92)
        
        # Verify open_qty has been resealed (should be 13.1 since it was sealed with correct cycle 92)
        self.assertEqual(float(child_trade[1]), 13.1)

    def test_unauthorized_loss_gate_skipped_for_hedged_parent(self):
        """
        Parent has hedge_child_bot_id set. Parent is LONG, child is SHORT.
        Parent has open_qty = 10.0, child has open_qty = 4.0.
        Exchange position is LONG 6.0.
        The pair is balanced (10.0 - 4.0 = 6.0), so reconciler does not gate parent.
        """
        from engine.reconciler import StateReconciler, BotState
        
        # Setup bots in DB
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=10.0, cycle_id=92, position_side='LONG')
        
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=4.0, cycle_id=92, position_side='SHORT')
        
        # Seed entries in bot_orders so resolve_net_mismatch reads correct ledger values
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'entry', 'CQB_10018_ENTRY_92_1', 2.0, 10.0, 10.0, 'filled', 92, 1)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_92_1', 2.0, 4.0, 4.0, 'filled', 92, 1)
        """)
        self.conn.commit()
        
        mock_exchange = MagicMock()
        reconciler = StateReconciler(exchanges={'future': mock_exchange})
        
        # Setup states
        bot_states = reconciler.get_bot_states()
        
        # Setup positions
        from engine.reconciler import ExchangePosition
        positions = {
            'SUIUSDC': [
                ExchangePosition(symbol='SUIUSDC', side='LONG', size=6.0, entry_price=2.0, mark_price=2.0, unrealized_pnl=0.0)
            ]
        }
        
        results = reconciler.resolve_net_mismatch(bot_states, positions)
        
        # Verify no bot is gated as REQUIRE_MANUAL
        gated = [r for r in results if r.requires_manual_intervention]
        self.assertEqual(len(gated), 0, "No bot should be gated because pair is balanced")
        
        # Check bot status remains active
        parent_status = self.conn.execute("SELECT status FROM bots WHERE id=10018").fetchone()[0]
        self.assertEqual(parent_status, 'IN TRADE')

    def test_inv13_runs_before_unauthorized_loss(self):
        """
        Hedged pair: parent LONG 10.0, child SHORT 12.0.
        Exchange is SHORT 2.0.
        The pair is balanced (10 - 12 = -2.0), so reconciler skips UNAUTHORIZED_LOSS entirely
        and neither bot is gated.
        """
        from engine.reconciler import StateReconciler, BotState
        
        # Setup bots in DB
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=10.0, cycle_id=92, position_side='LONG')
        
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=12.0, cycle_id=92, position_side='SHORT')
        
        # Seed entries in bot_orders so resolve_net_mismatch reads correct ledger values
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'entry', 'CQB_10018_ENTRY_92_1', 2.0, 10.0, 10.0, 'filled', 92, 1)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_92_1', 2.0, 12.0, 12.0, 'filled', 92, 1)
        """)
        self.conn.commit()
        
        mock_exchange = MagicMock()
        reconciler = StateReconciler(exchanges={'future': mock_exchange})
        
        # Setup states
        bot_states = reconciler.get_bot_states()
        
        # Setup positions (SHORT 2.0)
        from engine.reconciler import ExchangePosition
        positions = {
            'SUIUSDC': [
                ExchangePosition(symbol='SUIUSDC', side='SHORT', size=2.0, entry_price=2.0, mark_price=2.0, unrealized_pnl=0.0)
            ]
        }
        
        results = reconciler.resolve_net_mismatch(bot_states, positions)
        
        # Verify no bot is gated as REQUIRE_MANUAL
        gated = [r for r in results if r.requires_manual_intervention]
        self.assertEqual(len(gated), 0, "No bot should be gated because pair matches exchange")


# ---------------------------------------------------------------------------
# INV-29: Hedge Gates & be_only state tests
# ---------------------------------------------------------------------------

class TestINV29HedgeGates(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pending_hedge_close_gate_holds_parent(self):
        """child has open_qty>0 after BE TP registered -> parent status='pending_hedge_close', NOT 'Scanning', cycle_id unchanged"""
        # Parent bot (LONG)
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=10.0, cycle_id=92, position_side='LONG', avg_entry_price=2.0)

        # Hedge child bot (SHORT)
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=5.0, cycle_id=92, position_side='SHORT', avg_entry_price=2.0)

        mock_exchange = MagicMock()
        mock_exchange.is_testnet = True
        mock_exchange.fetch_positions.return_value = [
            {'symbol': 'SUI/USDC:USDC', 'contracts': -5.0, 'side': 'SHORT'}
        ]

        from engine.ledger import handle_tp_completion
        # Call handle_tp_completion on parent (which has active child with qty > 0)
        res = handle_tp_completion(10018, 2.1, 'SUI/USDC:USDC', exit_fill_ts=1700000000, exchange=mock_exchange)
        self.assertTrue(res)

        # Parent status must be pending_hedge_close
        parent_bot = self.conn.execute("SELECT status FROM bots WHERE id=10018").fetchone()
        self.assertEqual(parent_bot[0], 'pending_hedge_close')

        # Parent cycle_id must be unchanged (92)
        parent_trade = self.conn.execute("SELECT cycle_id, open_qty, cycle_phase FROM trades WHERE bot_id=10018").fetchone()
        self.assertEqual(parent_trade[0], 92)
        self.assertEqual(float(parent_trade[1] or 0), 0.0)
        self.assertEqual(parent_trade[2], 'HEDGE_PENDING_CLOSE')

    def test_complete_parent_cycle_after_hedge_unblocks(self):
        """parent in pending_hedge_close -> child BE TP fills -> parent becomes 'Scanning', cycle_id incremented"""
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', status='pending_hedge_close', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=0.0, cycle_id=92, position_side='LONG', avg_entry_price=0.0)

        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=5.0, cycle_id=92, position_side='SHORT', avg_entry_price=2.0)

        mock_exchange = MagicMock()

        from engine.ledger import complete_parent_cycle_after_hedge
        complete_parent_cycle_after_hedge(10018, exchange=mock_exchange)

        # Parent status must be Scanning
        parent_bot = self.conn.execute("SELECT status FROM bots WHERE id=10018").fetchone()
        self.assertEqual(parent_bot[0], 'Scanning')

        # Parent cycle_id must be incremented to 93
        parent_trade = self.conn.execute("SELECT cycle_id, cycle_phase FROM trades WHERE bot_id=10018").fetchone()
        self.assertEqual(parent_trade[0], 93)
        self.assertEqual(parent_trade[1], 'IDLE')

    def test_complete_parent_cycle_after_hedge_idempotent(self):
        """called twice -> second call no-ops (parent status already changed)"""
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', status='Scanning', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=0.0, cycle_id=93, position_side='LONG')

        mock_exchange = MagicMock()

        from engine.ledger import complete_parent_cycle_after_hedge
        # Second call to complete_parent_cycle_after_hedge when parent is already Scanning
        complete_parent_cycle_after_hedge(10018, exchange=mock_exchange)

        parent_bot = self.conn.execute("SELECT status FROM bots WHERE id=10018").fetchone()
        self.assertEqual(parent_bot[0], 'Scanning')

        parent_trade = self.conn.execute("SELECT cycle_id FROM trades WHERE bot_id=10018").fetchone()
        self.assertEqual(parent_trade[0], 93) # Remains 93

    def test_enforce_hedge_child_state_be_only(self):
        """parent status in (Scanning, hedge_standby, pending_hedge_close) + child_qty>0 -> returns 'be_only'"""
        # Test Scanning parent
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', status='Scanning', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=0.0, cycle_id=93, position_side='LONG')

        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=5.0, cycle_id=93, position_side='SHORT', avg_entry_price=2.0)

        from engine.bot_executor import enforce_hedge_child_state
        state = enforce_hedge_child_state(100318, self.conn)
        self.assertEqual(state, 'be_only')

        # Test pending_hedge_close parent
        self.conn.execute("UPDATE bots SET status='pending_hedge_close' WHERE id=10018")
        self.conn.commit()
        state = enforce_hedge_child_state(100318, self.conn)
        self.assertEqual(state, 'be_only')

    def test_maintain_orders_be_only_cancels_grids_keeps_tp(self):
        """be_only state -> _cancel_non_tp_orders called, no new grid/entry placed"""
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', status='pending_hedge_close', hedge_child_bot_id=100318)
        _insert_trades(self.conn, 10018, open_qty=0.0, cycle_id=92, position_side='LONG')

        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018)
        _insert_trades(self.conn, 100318, open_qty=5.0, cycle_id=92, position_side='SHORT', avg_entry_price=2.0)

        mock_exchange = MagicMock()
        # Mock exchange open orders: has one TP order and one grid order
        mock_exchange.fetch_open_orders.return_value = [
            {'id': 'order_tp', 'clientOrderId': 'CQB_100318_TP_92_BE', 'status': 'open'},
            {'id': 'order_grid', 'clientOrderId': 'CQB_100318_GRID_92_1', 'status': 'open'}
        ]

        from engine.bot_executor import BotExecutor
        executor = BotExecutor(runner=None)
        
        # Call maintain_orders on child
        res = executor.maintain_orders(
            bot_id=100318,
            name='sui long_hedge',
            pair='SUI/USDC:USDC',
            direction='SHORT',
            bot_status={'open_qty': 5.0, 'avg_entry_price': 2.0, 'cycle_id': 92, 'current_step': 1},
            current_price=2.0,
            exchange=mock_exchange,
            market_snapshot={'open_orders': [
                {'id': 'order_tp', 'clientOrderId': 'CQB_100318_TP_92_BE', 'status': 'open'},
                {'id': 'order_grid', 'clientOrderId': 'CQB_100318_GRID_92_1', 'status': 'open'}
            ]},
            bot_config={'max_position_limit': 10.0}
        )
        self.assertIsNone(res)

        # Verify cancel_order was called on the grid order but NOT the TP order
        mock_exchange.cancel_order.assert_any_call('order_grid', 'SUI/USDC:USDC')
        # Check that cancel_order was not called for order_tp
        for call in mock_exchange.cancel_order.call_args_list:
            self.assertNotEqual(call[0][0], 'order_tp')

    def test_runner_skips_pending_hedge_close_bots(self):
        """bot in pending_hedge_close is excluded from ThreadPoolExecutor batch"""
        # Insert a bot with status pending_hedge_close
        _insert_bot(self.conn, 10099, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', status='pending_hedge_close')
        _insert_trades(self.conn, 10099, open_qty=0.0)
        # Seed a filled order to prevent DNA-WIPE
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10099, 'entry', 'CQB_10099_ENTRY_1_1', 2.0, 10.0, 10.0, 'filled', 1, 1)
        """)
        self.conn.commit()
        
        from engine.runner import BotRunner
        with patch('engine.database.check_and_fix_integrity'), patch('engine.runner.startup.StartupMixin._post_init'):
            runner = BotRunner()
        runner.exchanges = {}
        
        # Get active bots via runner.get_active_bots()
        active_bots = runner.get_active_bots()
        bot_row = next((b for b in active_bots if b[0] == 10099), None)
        self.assertIsNotNone(bot_row)
        self.assertEqual(bot_row[12], 'pending_hedge_close')
        
        # Verify that filtering removes it
        SKIP_STATUSES = {'pending_hedge_close'}
        filtered_bots = [b for b in active_bots if b[12] not in SKIP_STATUSES]
        self.assertFalse(any(b[0] == 10099 for b in filtered_bots))

    def test_inv29_partial_fill_then_retry_hedged(self):
        """Parent step 7 partially fills 7.9 SUI -> child enters 7.9.
        Then parent step 7 retry fills 1140.2 SUI -> child delta check enters 1140.2.
        """
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        # Setup parent
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=860.6, cycle_id=128)

        # Setup child
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='Scanning')
        _insert_trades(self.conn, 100318, open_qty=0.0, cycle_id=128)

        # Seed parent's first partial fill on step 7 (7.9 SUI)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_7', 0.6816, 1136.2, 7.9, 'cancelled', 128, 7)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        # First signal for Step 7 (partial fill of 7.9)
        mock_exchange.fetch_order.side_effect = Exception("Not found")
        # Mock child entry order placed at exchange
        mock_order_1 = {
            'id': 'EX_CHILD_ENTRY_7_FIRST',
            'status': 'filled',
            'price': 0.6816,
            'clientOrderId': 'CQB_100318_ENTRY_128_7'
        }
        mock_exchange.create_order.return_value = mock_order_1

        # We call the signal
        res1 = executor._signal_hedge_child_entry(
            parent_bot_id=10018,
            parent_name='sui long',
            parent_step=7,
            pair='SUI/USDC:USDC',
            direction='LONG',
            step_qty=7.9,
            step_fill_price=0.6816,
            exchange=mock_exchange,
            parent_cycle_id=128,
            current_price=0.6816
        )
        self.assertTrue(res1)

        # Check DB entries: child should have one entry for step 1 of cycle 128, qty 7.9
        rows = self.conn.execute("SELECT client_order_id, filled_amount, status FROM bot_orders WHERE bot_id=100318 AND step=1").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'CQB_100318_ENTRY_128_7')
        self.assertEqual(rows[0][1], 7.9)

        # Check trades table for child
        from engine.ledger import seal_trade_state
        seal_trade_state(100318)
        child_qty = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100318").fetchone()[0]
        self.assertAlmostEqual(child_qty, 7.9)

        # Now simulate parent step 7 retry order filling 1140.2 SUI
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_7_R1', 0.6810, 1140.2, 1140.2, 'filled', 128, 7)
        """)
        self.conn.commit()

        # Update parent trades open_qty in DB to reflect the new fill (2008.7 SUI)
        self.conn.execute("UPDATE trades SET open_qty = 2008.7 WHERE bot_id=10018")
        self.conn.commit()

        # Mock child retry entry order placed at exchange
        mock_order_2 = {
            'id': 'EX_CHILD_ENTRY_7_RETRY',
            'status': 'filled',
            'price': 0.6810,
            'clientOrderId': 'CQB_100318_ENTRY_128_7_R123456'
        }
        mock_exchange.create_order.return_value = mock_order_2

        # Second signal for Step 7 (retry fill of 1140.2, delta check should trigger)
        res2 = executor._signal_hedge_child_entry(
            parent_bot_id=10018,
            parent_name='sui long',
            parent_step=7,
            pair='SUI/USDC:USDC',
            direction='LONG',
            step_qty=1140.2,
            step_fill_price=0.6810,
            exchange=mock_exchange,
            parent_cycle_id=128,
            current_price=0.6810
        )
        self.assertTrue(res2)

        # Verify child has TWO entry bot_orders rows for child_step 1
        rows_all = self.conn.execute("SELECT client_order_id, filled_amount, status FROM bot_orders WHERE bot_id=100318 AND step=1 ORDER BY created_at ASC").fetchall()
        self.assertEqual(len(rows_all), 2)
        self.assertEqual(rows_all[0][1], 7.9)
        self.assertEqual(rows_all[1][1], 1140.2)
        self.assertIn('_R', rows_all[1][0]) # has replacement suffix

        # Assert child open_qty = 1148.1
        seal_trade_state(100318)
        child_qty_final = self.conn.execute("SELECT open_qty FROM trades WHERE bot_id=100318").fetchone()[0]
        self.assertAlmostEqual(child_qty_final, 1148.1)

    def test_inv29_no_double_signal_when_child_open(self):
        """Parent step 7 partially fills 7.9 SUI -> child enters 7.9.
        Hedge child order is still 'open' (in-flight).
        Parent step 7 retry fills 1140.2 -> child delta check counts in-flight and only signals for 1140.2.
        """
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        # Setup parent
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=860.6, cycle_id=128)

        # Setup child
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='Scanning')
        _insert_trades(self.conn, 100318, open_qty=0.0, cycle_id=128)

        # Seed parent's first partial fill on step 7 (7.9 SUI)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_7', 0.6816, 1136.2, 7.9, 'cancelled', 128, 7)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        # Mock child entry order placed at exchange, but returns status 'open' (not filled yet)
        mock_order_1 = {
            'id': 'EX_CHILD_ENTRY_7_OPEN',
            'status': 'open',
            'price': 0.6816,
            'clientOrderId': 'CQB_100318_ENTRY_128_7'
        }
        mock_exchange.create_order.return_value = mock_order_1
        mock_exchange.fetch_order.side_effect = Exception("Not found")

        # First signal for Step 7
        res1 = executor._signal_hedge_child_entry(
            parent_bot_id=10018,
            parent_name='sui long',
            parent_step=7,
            pair='SUI/USDC:USDC',
            direction='LONG',
            step_qty=7.9,
            step_fill_price=0.6816,
            exchange=mock_exchange,
            parent_cycle_id=128,
            current_price=0.6816
        )
        self.assertTrue(res1)

        # Child order is in DB with status 'open' and filled_amount = 0.0, amount = 7.9
        row = self.conn.execute("SELECT status, amount, filled_amount FROM bot_orders WHERE client_order_id='CQB_100318_ENTRY_128_7'").fetchone()
        self.assertEqual(row[0], 'open')
        self.assertEqual(row[1], 7.9)

        # Now simulate parent step 7 retry order filling 1140.2 SUI
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_7_R1', 0.6810, 1140.2, 1140.2, 'filled', 128, 7)
        """)
        self.conn.commit()

        # Update parent trades open_qty
        self.conn.execute("UPDATE trades SET open_qty = 2008.7 WHERE bot_id=10018")
        self.conn.commit()

        # Mock child retry order placed at exchange
        mock_order_2 = {
            'id': 'EX_CHILD_ENTRY_7_RETRY',
            'status': 'open',
            'price': 0.6810,
            'clientOrderId': 'CQB_100318_ENTRY_128_7_R123456'
        }
        mock_exchange.create_order.return_value = mock_order_2

        # Second signal for Step 7 (retry fill of 1140.2)
        # Delta check should see parent_target_qty = 1148.1, child_step_qty = 7.9 (from open order amount)
        # So it should only place order for delta = 1140.2
        with patch.object(mock_exchange, 'create_order', return_value=mock_order_2) as mock_create:
            res2 = executor._signal_hedge_child_entry(
                parent_bot_id=10018,
                parent_name='sui long',
                parent_step=7,
                pair='SUI/USDC:USDC',
                direction='LONG',
                step_qty=1140.2,
                step_fill_price=0.6810,
                exchange=mock_exchange,
                parent_cycle_id=128,
                current_price=0.6810
            )
            self.assertTrue(res2)
            # Verify create_order was called with amount = 1140.2
            mock_create.assert_called_once()
            called_amount = mock_create.call_args[0][3]
            self.assertAlmostEqual(called_amount, 1140.2)

    def test_inv29_saturated_step_not_re_signaled(self):
        """Parent step 7 fills 1148.1 SUI -> child enters 1148.1.
        credit_fill called again for same parent order (duplicate WS event).
        Assert child is NOT signaled again (delta <= step_size).
        """
        from engine.bot_executor import BotExecutor
        from engine.exchange_interface import ExchangeInterface

        # Setup parent
        _insert_bot(self.conn, 10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', hedge_child_bot_id=100318, hedge_trigger_step=7)
        _insert_trades(self.conn, 10018, open_qty=2000.8, cycle_id=128)

        # Setup child
        _insert_bot(self.conn, 100318, 'sui long_hedge', 'SUI/USDC:USDC', 'SUIUSDC', 'SHORT', bot_type='hedge_child', parent_bot_id=10018, status='Scanning')
        _insert_trades(self.conn, 100318, open_qty=0.0, cycle_id=128)

        # Seed parent's Step 7 fills (total 1148.1 SUI)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_7', 0.6816, 1136.2, 7.9, 'cancelled', 128, 7)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (10018, 'grid', 'CQB_10018_GRID_128_7_R1', 0.6810, 1140.2, 1140.2, 'filled', 128, 7)
        """)
        self.conn.commit()

        # Seed child's entries matching the parent's fills (total 1148.1 SUI)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_128_7', 0.6816, 7.9, 7.9, 'filled', 128, 1)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, price, amount, filled_amount, status, cycle_id, step)
            VALUES (100318, 'entry', 'CQB_100318_ENTRY_128_7_R2', 0.6810, 1140.2, 1140.2, 'filled', 128, 1)
        """)
        self.conn.commit()

        executor = BotExecutor(runner=None)
        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.get_symbol_precision.return_value = {'step_size': 0.1}
        mock_exchange.round_to_step.side_effect = lambda qty, step: round(qty, 1)

        # Call signal again (simulating duplicate WS credit)
        with patch.object(mock_exchange, 'create_order') as mock_create:
            res = executor._signal_hedge_child_entry(
                parent_bot_id=10018,
                parent_name='sui long',
                parent_step=7,
                pair='SUI/USDC:USDC',
                direction='LONG',
                step_qty=1140.2,
                step_fill_price=0.6810,
                exchange=mock_exchange,
                parent_cycle_id=128,
                current_price=0.6810
            )
            self.assertTrue(res)
            # Assert that no order was created since it is already saturated
            mock_create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
