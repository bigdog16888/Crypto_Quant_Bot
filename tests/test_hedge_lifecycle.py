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
# TICKET-2: Migration script tests (using temp DB, not production)
# ---------------------------------------------------------------------------

@unittest.skip("One-time migration is obsolete and hedge_qty has been dropped from trades table")
class TestTicket2Migration(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        # Simulate a parent bot with hedge_qty > 0
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG')
        _insert_trades(self.conn, 10017, open_qty=0.0, hedge_qty=44.7,
                       position_side='LONG', avg_entry_price=0.0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _run_migration(self):
        """Import and run the migration function."""
        # Re-import to pick up temp DB path
        from scripts.migrate_hedge_to_child_bot import migrate
        migrate(dry_run=False)

    def test_ticket2_child_bot_created(self):
        """Migration creates a hedge_child bot linked to parent."""
        self._run_migration()
        conn = get_connection()
        child = conn.execute(
            "SELECT id, direction, bot_type, status FROM bots "
            "WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
        ).fetchone()
        self.assertIsNotNone(child, "Hedge child bot must be created")
        child_id, direction, bot_type, status = child
        self.assertEqual(direction, 'SHORT', "Child must be SHORT when parent is LONG")
        self.assertEqual(bot_type, 'hedge_child')
        self.assertEqual(status, 'IN TRADE')

    def test_ticket2_parent_hedge_qty_zeroed(self):
        """After migration parent's hedge_qty is 0 (INV-5)."""
        self._run_migration()
        conn = get_connection()
        row = conn.execute("SELECT hedge_qty FROM trades WHERE bot_id=10017").fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row[0] or 0), 0.0, places=4,
                               msg="Parent hedge_qty must be 0 after migration")

    def test_ticket2_child_open_qty_matches_former_hedge(self):
        """Child bot's open_qty == former parent hedge_qty."""
        self._run_migration()
        conn = get_connection()
        row = conn.execute(
            "SELECT t.open_qty FROM trades t "
            "JOIN bots b ON b.id=t.bot_id "
            "WHERE b.parent_bot_id=10017 AND b.bot_type='hedge_child'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row[0]), 44.7, places=2,
                               msg="Child open_qty must match former hedge_qty")

    def test_ticket2_parent_linked_to_child(self):
        """Parent bot's hedge_child_bot_id is set to child bot's id."""
        self._run_migration()
        conn = get_connection()
        parent_row = conn.execute(
            "SELECT hedge_child_bot_id FROM bots WHERE id=10017"
        ).fetchone()
        self.assertIsNotNone(parent_row)
        child_id = parent_row[0]
        self.assertIsNotNone(child_id, "hedge_child_bot_id must be set on parent")

        # Verify cross-reference
        child_row = conn.execute(
            "SELECT parent_bot_id FROM bots WHERE id=?", (child_id,)
        ).fetchone()
        self.assertEqual(child_row[0], 10017, "Child parent_bot_id must point back to 10017")

    def test_ticket2_migration_idempotent(self):
        """Running migration twice produces identical state (no duplicate child)."""
        self._run_migration()
        conn = get_connection()
        child_count_1 = conn.execute(
            "SELECT COUNT(*) FROM bots WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
        ).fetchone()[0]

        self._run_migration()  # second run
        child_count_2 = conn.execute(
            "SELECT COUNT(*) FROM bots WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
        ).fetchone()[0]

        self.assertEqual(child_count_1, 1, "Exactly one child after first migration")
        self.assertEqual(child_count_2, 1, "Still exactly one child after second migration")

    def test_ticket2_audit_order_created(self):
        """Migration inserts an audit bot_orders entry for the inherited position."""
        self._run_migration()
        conn = get_connection()
        child_id = conn.execute(
            "SELECT id FROM bots WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
        ).fetchone()[0]
        order = conn.execute(
            "SELECT order_type, filled_amount, status FROM bot_orders WHERE bot_id=?",
            (child_id,)
        ).fetchone()
        self.assertIsNotNone(order, "Audit bot_orders row must exist for child")
        otype, filled, status = order
        self.assertEqual(otype, 'entry')
        self.assertAlmostEqual(float(filled), 44.7, places=2)
        self.assertEqual(status, 'filled')

    def test_ticket2_active_positions_reassigned(self):
        """Orphan active_positions row (bot_id=0) is reassigned to child bot."""
        # active_positions table is created by init_db() in setUp; insert orphan now
        conn = get_connection()
        conn.execute("""
            INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
            VALUES (0, 'XRPUSDC', 'SHORT', 44.7, 2.2, ?)
        """, (int(time.time()),))
        conn.commit()

        # Verify orphan is there before migration
        pre = conn.execute(
            "SELECT COUNT(*) FROM active_positions WHERE bot_id=0"
        ).fetchone()[0]
        self.assertEqual(pre, 1, "Orphan must exist before migration")

        # Run migration WITHOUT calling init_db again (which would wipe active_positions)
        from scripts.migrate_hedge_to_child_bot import migrate
        migrate(dry_run=False)

        child_id = get_connection().execute(
            "SELECT id FROM bots WHERE parent_bot_id=10017 AND bot_type='hedge_child'"
        ).fetchone()[0]

        orphan_count = get_connection().execute(
            "SELECT COUNT(*) FROM active_positions WHERE bot_id=0 AND pair='XRPUSDC'"
        ).fetchone()[0]
        self.assertEqual(orphan_count, 0, "No orphan rows should remain after migration")

        assigned = get_connection().execute(
            "SELECT COUNT(*) FROM active_positions WHERE bot_id=? AND pair='XRPUSDC'",
            (child_id,)
        ).fetchone()[0]
        self.assertEqual(assigned, 1, "active_positions must be assigned to child bot")


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

    def test_ticket6_parent_fill_does_not_reduce_hedge_child(self):
        """When parent (LONG) fills an entry, hedge child open_qty is not touched."""
        from engine.oneway_netting import apply_oneway_entry_cross_reduction

        conn = get_connection()
        before_qty = float(conn.execute(
            "SELECT open_qty FROM trades WHERE bot_id=99001"
        ).fetchone()[0] or 0)

        apply_oneway_entry_cross_reduction(
            filling_bot_id=10017,
            pair='XRP/USDC:USDC',
            direction='LONG',
            delta=1.0,
            source_order_id='TEST_SUPPRESS_001',
        )

        after_qty = float(get_connection().execute(
            "SELECT open_qty FROM trades WHERE bot_id=99001"
        ).fetchone()[0] or 0)

        self.assertAlmostEqual(before_qty, after_qty, places=4,
                               msg=f"Hedge child open_qty changed: {before_qty} → {after_qty}. "
                                   "Cross-reduction must be suppressed between parent and child.")

    def test_ticket6_child_fill_does_not_reduce_parent(self):
        """When child (SHORT) fills an entry, parent open_qty is not touched."""
        from engine.oneway_netting import apply_oneway_entry_cross_reduction

        conn = get_connection()
        before_qty = float(conn.execute(
            "SELECT open_qty FROM trades WHERE bot_id=10017"
        ).fetchone()[0] or 0)

        apply_oneway_entry_cross_reduction(
            filling_bot_id=99001,
            pair='XRP/USDC:USDC',
            direction='SHORT',
            delta=1.0,
            source_order_id='TEST_SUPPRESS_002',
        )

        after_qty = float(get_connection().execute(
            "SELECT open_qty FROM trades WHERE bot_id=10017"
        ).fetchone()[0] or 0)

        self.assertAlmostEqual(before_qty, after_qty, places=4,
                               msg=f"Parent open_qty changed: {before_qty} → {after_qty}. "
                                   "Cross-reduction must be suppressed between child and parent.")



# ---------------------------------------------------------------------------
# SCENARIO-6: Migration idempotency (integrated)
# ---------------------------------------------------------------------------

@unittest.skip("One-time migration is obsolete and hedge_qty has been dropped from trades table")
class TestScenario6MigrationIdempotency(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        conn = get_connection()
        _insert_bot(conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG')
        _insert_trades(conn, 10017, open_qty=0.0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_scenario6_full_migration_state(self):
        """Full scenario: migrate, verify all invariants, run twice (idempotent)."""
        from scripts.migrate_hedge_to_child_bot import migrate

        migrate(dry_run=False)

        conn = get_connection()
        child = conn.execute(
            "SELECT id, direction, open_qty FROM bots b "
            "JOIN trades t ON t.bot_id=b.id "
            "WHERE b.parent_bot_id=10017 AND b.bot_type='hedge_child'"
        ).fetchone()
        self.assertIsNotNone(child)
        child_id, direction, open_qty = child
        self.assertEqual(direction, 'SHORT')
        self.assertAlmostEqual(float(open_qty), 44.7, places=2)

        parent_hedge = float(conn.execute(
            "SELECT hedge_qty FROM trades WHERE bot_id=10017"
        ).fetchone()[0] or 0)
        self.assertAlmostEqual(parent_hedge, 0.0, places=4)

        # Run again — idempotent
        migrate(dry_run=False)

        child_count = conn.execute(
            "SELECT COUNT(*) FROM bots WHERE parent_bot_id=10017"
        ).fetchone()[0]
        self.assertEqual(child_count, 1, "Must be exactly one child after two migrations")

        child_qty_after = float(conn.execute(
            "SELECT open_qty FROM trades WHERE bot_id=?", (child_id,)
        ).fetchone()[0] or 0)
        self.assertAlmostEqual(child_qty_after, 44.7, places=2,
                               msg="Child open_qty must not be doubled on second run")


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
                cycle_id=1
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
                cycle_id=1
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
                cycle_id=1
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
                cycle_id=1
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
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
        _insert_trades(self.conn, 10017, open_qty=10.0)

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

        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.fetch_positions.return_value = []
        mock_exchange.fetch_open_orders.return_value = []

        # Run handle_tp_completion for parent (10017)
        with patch('engine.database.reset_bot_after_tp') as mock_reset:
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

        mock_exchange = MagicMock(spec=ExchangeInterface)
        mock_exchange.fetch_positions.return_value = []
        mock_exchange.fetch_open_orders.return_value = []

        sentinel_error = RuntimeError("injected-save_bot_order-failure")

        with patch('engine.database.reset_bot_after_tp'), \
             patch('engine.database.save_bot_order', side_effect=sentinel_error), \
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
        _insert_bot(self.conn, 10017, 'xrp long', 'XRP/USDC:USDC', 'XRPUSDC', 'LONG', hedge_child_bot_id=99001)
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
                cycle_id=2,  # Parent is now on cycle 2
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
        _insert_trades(self.conn, 10017, open_qty=0.1, position_side='LONG')

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


if __name__ == '__main__':
    unittest.main()
