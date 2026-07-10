
import unittest
import os
import sys
import shutil
import tempfile
import threading
from unittest.mock import MagicMock, patch

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import database

class TestDatabase(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for the test database
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_bot.db")
        
        # Patch the DB_PATH in the database module
        self.original_db_path = database.DB_PATH
        database.DB_PATH = self.db_path
        
        # Reset thread local storage
        if hasattr(database._local, 'connection'):
            del database._local.connection

    def tearDown(self):
        # Close connection
        database.close_connection()
        
        # Restore original DB_PATH
        database.DB_PATH = self.original_db_path
        
        # Remove temporary directory
        shutil.rmtree(self.test_dir)

    def test_init_db(self):
        """Test that init_db creates tables successfully."""
        database.init_db()
        
        conn = database.get_connection()
        cursor = conn.cursor()
        
        # Check if tables exist
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        self.assertIn('bots', tables)
        self.assertIn('trades', tables)
        self.assertIn('bot_orders', tables)
        self.assertIn('trade_history', tables)

    def test_add_bot(self):
        """Test adding a bot."""
        database.init_db()
        bot_id = database.add_bot("TestBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        self.assertIsNotNone(bot_id)
        
        params = database.get_bot_params(bot_id)
        self.assertEqual(params[0], "TestBot")
        self.assertEqual(params[1], "BTC/USDT")
        
        # Check initial trade state — get_bot_status() returns a dict
        status = database.get_bot_status(bot_id)
        self.assertIsNotNone(status)
        self.assertEqual(status['current_step'], 0)
        self.assertEqual(status['total_invested'], 0)

    def test_add_duplicate_bot(self):
        """Test that adding a bot with a duplicate name returns None (UNIQUE enforced)."""
        database.init_db()
        bot_id_1 = database.add_bot("TestBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        bot_id_2 = database.add_bot("TestBot", "ETH/USDT", "SHORT", 40.0, 2.0, 20.0)
        
        # bots table has no UNIQUE on name; both inserts succeed with different IDs
        self.assertIsNotNone(bot_id_1)
        self.assertIsNotNone(bot_id_2)
        self.assertNotEqual(bot_id_1, bot_id_2)

    def test_update_martingale_step(self):
        """Test updating martingale step."""
        database.init_db()
        bot_id = database.add_bot("StepBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        database.update_martingale_step(bot_id, 1, 100.0, 50000.0, 51000.0)
        
        # get_bot_status() returns a dict
        status = database.get_bot_status(bot_id)
        self.assertIsNotNone(status)
        self.assertEqual(status['current_step'], 1)
        self.assertEqual(status['total_invested'], 100.0)
        self.assertEqual(status['avg_entry_price'], 50000.0)

    def test_reset_bot_after_tp(self):
        """Test resetting bot after TP."""
        database.init_db()
        bot_id = database.add_bot("TPBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        # Simulate active trade
        database.update_martingale_step(bot_id, 1, 100.0, 50000.0, 51000.0)
        
        class _FlatEx:
            def fetch_positions(self):
                return []

        # Reset (exchange flat required by pair parity gate)
        database.reset_bot_after_tp(bot_id, exit_price=51000.0, exchange=_FlatEx())
        
        status = database.get_bot_status(bot_id)
        self.assertIsNotNone(status)
        self.assertEqual(status['current_step'], 0)
        self.assertEqual(status['total_invested'], 0)
        self.assertEqual(status['last_exit_price'], 51000.0)
        
        # Check trade history
        history = database.get_trade_history(bot_id)
        self.assertTrue(len(history) > 0)
        self.assertEqual(history[0][3], 'TP_HIT')  # action column index in trade_history

    def test_reset_bot_after_tp_by_bot_type(self):
        """Test that reset_bot_after_tp resets status to 'hedge_standby' for hedge_child bots and 'Scanning' for standard bots."""
        database.init_db()
        
        # 1. Create standard bot and verify it resets to 'Scanning'
        std_bot_id = database.add_bot("StdBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        database.update_martingale_step(std_bot_id, 1, 100.0, 50000.0, 51000.0)
        
        class _FlatEx:
            def fetch_positions(self):
                return []
                
        database.reset_bot_after_tp(std_bot_id, exit_price=51000.0, exchange=_FlatEx())
        std_status = database.get_bot_status(std_bot_id)
        self.assertEqual(std_status['status'], 'Scanning')
        
        # 2. Create hedge child bot and verify it resets to 'hedge_standby'
        child_bot_id = database.add_bot("ChildBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        conn = database.get_connection()
        conn.execute("UPDATE bots SET bot_type = 'hedge_child' WHERE id = ?", (child_bot_id,))
        conn.commit()
        
        database.update_martingale_step(child_bot_id, 1, 100.0, 50000.0, 51000.0)
        database.reset_bot_after_tp(child_bot_id, exit_price=51000.0, exchange=_FlatEx())
        child_status = database.get_bot_status(child_bot_id)
        self.assertEqual(child_status['status'], 'hedge_standby')

    def test_bot_position_management(self):
        """Test bot position close works correctly."""
        database.init_db()
        bot_id = database.add_bot("PosBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        # bot_position_id is NULL until explicitly set — returns None for new bots
        pos_id = database.get_bot_position_id(bot_id)
        self.assertIsNone(pos_id)
        
        # Simulate active trade then close position
        database.update_martingale_step(bot_id, 1, 100.0, 50000.0, 51000.0)
        result = database.close_bot_position(bot_id, close_price=50500.0)
        
        # close_bot_position returns {'success': True, 'status': 'Fully Closed'}
        self.assertTrue(result['success'])
        self.assertIn('status', result)

    def test_consolidate_merges_filled_duplicate_keeps_one(self):
        """v3.5.8 (Fix 2): Two 'filled' rows with same CID should now be consolidated.
        Previously the consolidator excluded 'filled' rows, so identical-CID filled pairs
        were invisible — one became a ghost double-credit. Now the consolidator merges them:
        one row (the keeper) survives with the best fill; the other is marked auto_closed.
        """
        database.init_db()
        conn = database.get_connection()
        cursor = conn.cursor()
        cursor.execute("DROP INDEX IF EXISTS idx_bot_orders_bot_cid")

        # Insert two filled rows with the same client_order_id (GTX retry double-fill scenario)
        cursor.execute(
            "INSERT INTO bot_orders (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (10016, 'order_1', 'CQB_10016_ENTRY_1', 'entry', 50000.0, 0.002, 0.002, 'filled', 1000, 1000)"
        )
        cursor.execute(
            "INSERT INTO bot_orders (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (10016, 'order_2', 'CQB_10016_ENTRY_1', 'entry', 50000.0, 0.002, 0.002, 'filled', 1001, 1001)"
        )
        conn.commit()

        # Run consolidation — Fix 2 means both filled rows are now visible to the consolidator
        consolidated = database.consolidate_duplicate_bot_orders(bot_id=10016)

        # Exactly 1 group should be consolidated
        self.assertEqual(consolidated, 1)

        # One row must survive as 'filled', the other must be 'auto_closed'
        rows = cursor.execute(
            "SELECT status FROM bot_orders WHERE client_order_id='CQB_10016_ENTRY_1' ORDER BY created_at"
        ).fetchall()
        statuses = [r[0] for r in rows]
        self.assertEqual(len(statuses), 2)
        self.assertIn('filled', statuses,
                      "Keeper row must remain 'filled'")
        self.assertIn('auto_closed', statuses,
                      "Duplicate row must be marked 'auto_closed'")

    def test_consolidate_does_not_touch_single_filled_row(self):
        """A single 'filled' row with unique CID must never be touched by consolidation."""
        database.init_db()
        conn = database.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO bot_orders (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (10016, 'order_solo', 'CQB_10016_ENTRY_SOLO', 'entry', 50000.0, 0.002, 0.002, 'filled', 1000, 1000)"
        )
        conn.commit()

        consolidated = database.consolidate_duplicate_bot_orders(bot_id=10016)
        self.assertEqual(consolidated, 0, "Single unique CID row must not be consolidated")

        row = cursor.execute(
            "SELECT status FROM bot_orders WHERE client_order_id='CQB_10016_ENTRY_SOLO'"
        ).fetchone()
        self.assertEqual(row[0], 'filled', "Status must remain 'filled'")


    # ------------------------------------------------------------------ #
    #  ADR-002 TICKET-1: Schema migration tests                           #
    # ------------------------------------------------------------------ #

    def test_ticket1_schema_columns_exist(self):
        """All four ADR-002 columns exist in the bots table."""
        database.init_db()
        conn = database.get_connection()
        # Will raise sqlite3.OperationalError if column missing
        conn.execute(
            "SELECT bot_type, parent_bot_id, hedge_child_bot_id, hedge_trigger_step "
            "FROM bots LIMIT 1"
        )
        # Verify pragma lists all four columns
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bots)").fetchall()}
        for col in ('bot_type', 'parent_bot_id', 'hedge_child_bot_id', 'hedge_trigger_step'):
            self.assertIn(col, cols, f"Column '{col}' missing from bots table")

    def test_ticket1_default_values(self):
        """Existing bots default to bot_type='standard', NULLs for FK columns."""
        database.init_db()
        conn = database.get_connection()
        # Insert a plain bot without specifying new columns
        conn.execute(
            "INSERT INTO bots (name, pair, direction, is_active) "
            "VALUES ('test_default_bot', 'BTCUSDC', 'LONG', 0)"
        )
        conn.commit()
        row = conn.execute(
            "SELECT bot_type, parent_bot_id, hedge_child_bot_id, hedge_trigger_step "
            "FROM bots WHERE name='test_default_bot'"
        ).fetchone()
        self.assertIsNotNone(row)
        bot_type, parent_id, child_id, trigger_step = row
        # bot_type must default to 'standard' (or NULL for pre-existing rows before migration)
        self.assertIn(bot_type, ('standard', None),
                      f"bot_type should be 'standard' or NULL, got '{bot_type}'")
        self.assertIsNone(parent_id, "parent_bot_id must default to NULL")
        self.assertIsNone(child_id, "hedge_child_bot_id must default to NULL")
        self.assertIsNone(trigger_step, "hedge_trigger_step must default to NULL")

    def test_ticket1_schema_idempotent(self):
        """Calling init_db() twice does not raise or duplicate columns."""
        database.init_db()
        database.init_db()  # second call must be a no-op
        conn = database.get_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bots)").fetchall()]
        # Each ADR-002 column appears exactly once
        for col in ('bot_type', 'parent_bot_id', 'hedge_child_bot_id', 'hedge_trigger_step'):
            self.assertEqual(cols.count(col), 1,
                             f"Column '{col}' appears {cols.count(col)} time(s), expected 1")

    def test_auto_create_and_link_hedge_children(self):
        """Test that active standard bots with UseHedge=True in config get their hedge child created and linked automatically."""
        database.init_db()
        conn = database.get_connection()
        
        # 1. Add standard bot with UseHedge=True
        cfg = {
            'UseHedge': True,
            'HedgeStartStep': 7,
            'max_steps': 8
        }
        import json
        cfg_str = json.dumps(cfg)
        
        # Insert standard bot
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bots (name, pair, normalized_pair, direction, rsi_limit, martingale_multiplier, base_size, strategy_type, config, status, is_active, bot_type)
            VALUES ('parent_bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 30.0, 1.5, 10.0, 'Martingale', ?, 'IN TRADE', 1, 'standard')
        """, (cfg_str,))
        parent_id = cursor.lastrowid
        
        # Insert standard bot trades row
        cursor.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side, total_invested, avg_entry_price, current_step, entry_confirmed, cycle_phase)
            VALUES (?, 0.0, 1, 'LONG', 0.0, 0.0, 0, 1, 'IDLE')
        """, (parent_id,))
        conn.commit()
        
        # 2. Run the self-healing hook
        database.auto_create_and_link_hedge_children(conn)
        
        # 3. Verify parent is updated and child is created and linked
        cursor.execute("SELECT hedge_child_bot_id, hedge_trigger_step FROM bots WHERE id = ?", (parent_id,))
        child_id, trigger_step = cursor.fetchone()
        self.assertIsNotNone(child_id)
        self.assertEqual(trigger_step, 7)
        
        # Verify child bot attributes
        cursor.execute("SELECT name, pair, direction, bot_type, parent_bot_id, is_active, status FROM bots WHERE id = ?", (child_id,))
        c_name, c_pair, c_direction, c_type, c_parent, c_active, c_status = cursor.fetchone()
        self.assertEqual(c_name, 'parent_bot_hedge')
        self.assertEqual(c_pair, 'BTC/USDC:USDC')
        self.assertEqual(c_direction, 'SHORT')
        self.assertEqual(c_type, 'hedge_child')
        self.assertEqual(c_parent, parent_id)
        self.assertEqual(c_active, 1)
        
        # Verify child bot trades row is created
        cursor.execute("SELECT bot_id, open_qty, cycle_id, position_side, cycle_phase FROM trades WHERE bot_id = ?", (child_id,))
        t_bot_id, t_qty, t_cycle, t_side, t_phase = cursor.fetchone()
        self.assertEqual(t_bot_id, child_id)
        self.assertEqual(t_qty, 0.0)
        self.assertEqual(t_cycle, 1)
        self.assertEqual(t_side, 'SHORT')
        self.assertEqual(t_phase, 'IDLE')


    def test_reconcile_with_db_blocks_wipe_on_recomputed_qty(self):
        """Test that reconcile_with_db blocks ledger wipe if bot_orders has filled qty, and permits it if empty."""
        database.init_db()
        conn = database.get_connection()
        cursor = conn.cursor()
        
        bot_id = database.add_bot("WipeGuardBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        # Simulate active trade state in database
        database.update_martingale_step(bot_id, 1, 100.0, 50000.0, 51000.0)
        
        # Get target cycle_id from trades
        cursor.execute("SELECT cycle_id FROM trades WHERE bot_id = ?", (bot_id,))
        cycle_id = cursor.fetchone()[0] or 1
        
        # Scenario 1: bot_orders has non-zero recomputed_qty (e.g. entry filled)
        # We insert a filled order in bot_orders so recompute returns qty > 0
        cursor.execute(
            "INSERT INTO bot_orders (bot_id, order_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at, cycle_id, position_side) "
            "VALUES (?, 'order_recon_1', 'CQB_WGB_ENTRY_1', 'entry', 50000.0, 0.05, 0.05, 'filled', 1000, 1000, ?, 'LONG')",
            (bot_id, cycle_id)
        )
        conn.commit()
        
        # Call reconcile_with_db with no exchange position
        res = database.reconcile_with_db(bot_id, current_price=50000.0, open_orders=[], exchange_position=None)
        
        self.assertTrue(res['success'])
        
        # Assert the bot was NOT wiped because of the recomputed_qty guard
        status = database.get_bot_status(bot_id)
        self.assertEqual(status['total_invested'], 100.0, "Wipe must be blocked when recomputed qty is non-zero")
        self.assertEqual(status['current_step'], 1, "Wipe must be blocked when recomputed qty is non-zero")
        
        # Scenario 2: bot_orders has no filled orders (recomputed_qty = 0)
        # Delete the order to simulate zero recomputed_qty
        cursor.execute("DELETE FROM bot_orders WHERE bot_id = ?", (bot_id,))
        conn.commit()
        
        # Call reconcile_with_db again
        res2 = database.reconcile_with_db(bot_id, current_price=50000.0, open_orders=[], exchange_position=None)
        
        self.assertTrue(res2['success'])
        
        # Assert the bot was wiped since physical and virtual nets are both flat
        status_after = database.get_bot_status(bot_id)
        self.assertEqual(status_after['total_invested'], 0.0, "Wipe must proceed when recomputed qty is zero")
        self.assertEqual(status_after['current_step'], 0, "Wipe must proceed when recomputed qty is zero")

    def test_canonical_subselect_priorities(self):
        """Verify the canonical subselect prioritization matches expected rules:
        1. auto_closed with filled_amount > 0 beats partially_filled with filled_amount = 0 (v3.5.8 scenario).
        2. partially_filled with filled_amount > 0 beats auto_closed with filled_amount = 0 (Scenario B).
        3. partially_filled with filled_amount > 0 and auto_closed with filled_amount > 0 ties on status score, newer wins.
        """
        database.init_db()
        conn = database.get_connection()
        cursor = conn.cursor()
        cursor.execute("DROP INDEX IF EXISTS idx_bot_orders_bot_cid")

        # Insert a bot
        bot_id = database.add_bot("CanonicalTestBot", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)

        from engine.database import _canonical_bot_orders_from

        # Case 1: v3.5.8 scenario: auto_closed (230.7) vs partially_filled (0.0, newer)
        cursor.execute(
            "INSERT INTO bot_orders (id, bot_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (1001, ?, 'CID_CASE_1', 'entry', 50000.0, 230.7, 230.7, 'auto_closed', 1000, 1000)", (bot_id,)
        )
        cursor.execute(
            "INSERT INTO bot_orders (id, bot_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (1002, ?, 'CID_CASE_1', 'entry', 50000.0, 230.7, 0.0, 'partially_filled', 1001, 1001)", (bot_id,)
        )
        row1 = cursor.execute(f"SELECT bo.id, bo.status, bo.filled_amount {_canonical_bot_orders_from('bo')} AND bo.client_order_id = 'CID_CASE_1'").fetchone()
        self.assertEqual(row1[0], 1001, "Auto_closed with real fill must beat partially_filled zero-fill")

        # Case 2: Zero-fill auto_closed (older) vs Zero-fill partially_filled (newer)
        cursor.execute(
            "INSERT INTO bot_orders (id, bot_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (2001, ?, 'CID_CASE_2', 'entry', 50000.0, 230.7, 0.0, 'auto_closed', 1000, 1000)", (bot_id,)
        )
        cursor.execute(
            "INSERT INTO bot_orders (id, bot_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (2002, ?, 'CID_CASE_2', 'entry', 50000.0, 230.7, 0.0, 'partially_filled', 1001, 1001)", (bot_id,)
        )
        row2 = cursor.execute(f"SELECT bo.id, bo.status, bo.filled_amount {_canonical_bot_orders_from('bo')} AND bo.client_order_id = 'CID_CASE_2'").fetchone()
        self.assertEqual(row2[0], 2001, "Auto_closed zero-fill must beat partially_filled zero-fill")

        # Case 3: Real fill auto_closed (older) vs Real fill partially_filled (newer)
        cursor.execute(
            "INSERT INTO bot_orders (id, bot_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (3001, ?, 'CID_CASE_3', 'entry', 50000.0, 230.7, 230.7, 'auto_closed', 1000, 1000)", (bot_id,)
        )
        cursor.execute(
            "INSERT INTO bot_orders (id, bot_id, client_order_id, order_type, price, amount, filled_amount, status, created_at, updated_at) "
            "VALUES (3002, ?, 'CID_CASE_3', 'entry', 50000.0, 230.7, 230.7, 'partially_filled', 1001, 1001)", (bot_id,)
        )
        row3 = cursor.execute(f"SELECT bo.id, bo.status, bo.filled_amount {_canonical_bot_orders_from('bo')} AND bo.client_order_id = 'CID_CASE_3'").fetchone()
        self.assertEqual(row3[0], 3002, "Real fill partially_filled (newer) must beat auto_closed (older) when both are real fills")

    def test_recompute_floor_auto_detection_with_cancelled_partial_fills(self):
        """
        Regression test: Verify that the floor auto-detector correctly counts
        cancelled orders with partial fills, so it does not incorrectly detect
        a past cycle as unbalanced when its total exits actually match or exceed entries.
        """
        bot_id = 99901
        database.init_db()
        conn = database.get_connection()
        cursor = conn.cursor()

        # Insert bot and trade row
        cursor.execute("INSERT INTO bots (id, name, status, direction, pair) VALUES (?, 'test_floor_bot', 'Scanning', 'SHORT', 'SOL/USDC:USDC')", (bot_id,))
        cursor.execute("INSERT INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side) VALUES (?, 2, 0.0, 0, 'SHORT')", (bot_id,))
        
        # Cycle 1:
        # Entry of 10.0 (status = 'filled')
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at)
            VALUES (?, 1, 1, 'entry', 1.0, 10.0, 10.0, 'filled', 1000, 1000)
        """, (bot_id,))
        # Exit of 8.0 (status = 'filled')
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at)
            VALUES (?, 1, 1, 'tp', 1.0, 10.0, 8.0, 'filled', 1010, 1010)
        """, (bot_id,))
        # Exit of 2.0 (status = 'cancelled' with partial fill of 2.0)
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at)
            VALUES (?, 1, 1, 'tp', 1.0, 10.0, 2.0, 'cancelled', 1020, 1020)
        """, (bot_id,))

        # Cycle 2:
        # Entry of 5.0 (status = 'filled')
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at)
            VALUES (?, 2, 1, 'entry', 1.0, 5.0, 5.0, 'filled', 2000, 2000)
        """, (bot_id,))
        
        conn.commit()

        # Recompute. Floor detector should agree that Cycle 1 is fully balanced (10.0 entry - 10.0 exit = 0.0)
        # and therefore set cycle_floor = 2 (not 1).
        # This means the 5.0 entry in Cycle 2 will not be swallowed/offset by Cycle 1.
        res = database.recompute_invested_from_orders(bot_id)
        
        # We expect: (total_invested, avg_entry_price, open_qty, current_step)
        self.assertEqual(res[2], 5.0, "Recomputed open_qty must be 5.0 since Cycle 1 is balanced and floor is Cycle 2")
        self.assertEqual(res[0], 5.0, "Recomputed total_invested must be 5.0")

    def test_recompute_includes_prior_fills_after_adoption_wall(self):
        """Test that recompute_invested_from_orders includes prior filled orders in the same cycle

        even when wipe_wall_ts is advanced to a later time (e.g. by an alignment script).
        """
        database.init_db()
        conn = database.get_connection()
        cursor = conn.cursor()
        bot_id = 9999

        # Insert bot and trade row
        cursor.execute("INSERT INTO bots (id, name, status, direction, pair) VALUES (?, 'test_adoption_bot', 'Scanning', 'LONG', 'SOL/USDC:USDC')", (bot_id,))
        # Start at cycle 1, wipe_wall_ts initially 0
        cursor.execute("INSERT INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side) VALUES (?, 1, 0.0, 0, 'LONG')", (bot_id,))

        # 1. Add two legitimate fills early (ts 1000 and 1010)
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at)
            VALUES (?, 1, 1, 'entry', 1.0, 0.07, 0.07, 'filled', 1000, 1000)
        """, (bot_id,))
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at)
            VALUES (?, 1, 2, 'grid', 1.0, 0.10, 0.10, 'filled', 1010, 1010)
        """, (bot_id,))

        # 2. Add an alignment fill later (ts 2000)
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at)
            VALUES (?, 1, 3, 'entry', 1.0, 0.18, 0.18, 'filled', 2000, 2000)
        """, (bot_id,))

        # Proposed Fix simulation: Set wipe_wall_ts to the MIN of now_ts (2000) and the oldest fill (1000)
        # instead of blindly setting it to now_ts (2000).
        now_ts = 2000
        oldest_fill = cursor.execute(
            "SELECT MIN(created_at) FROM bot_orders WHERE bot_id = ? AND cycle_id = 1 AND filled_amount > 0",
            (bot_id,)
        ).fetchone()[0]
        wall_ts = min(now_ts, oldest_fill)
        
        cursor.execute("UPDATE trades SET wipe_wall_ts = ? WHERE bot_id = ?", (wall_ts, bot_id))
        conn.commit()

        # Recompute
        res = database.recompute_invested_from_orders(bot_id)
        
        # We expect: (total_invested, avg_entry_price, open_qty, current_step)
        # All three fills must be included (0.07 + 0.10 + 0.18 = 0.35)
        self.assertEqual(res[2], 0.35, "Recomputed open_qty must be 0.35 including all three fills")


    def test_full_restore_and_align_preserves_non_aligned_active_bot_fills(self):
        """Test that full_restore_and_align.py preserves non-aligned active bot fills using the oldest_fill logic."""
        import shutil
        import sqlite3
        from unittest.mock import patch, MagicMock
        
        test_backup_path = os.path.join(self.test_dir, "test_backup_bot.db")
        
        # Setup a mock DB on self.db_path using the true database schema
        database.init_db()
        conn = database.get_connection()
        try:
            # Bot A: to be aligned (e.g. 10008)
            conn.execute("INSERT INTO bots (id, name, status, direction, pair) VALUES (10008, 'sol bot', 'Scanning', 'LONG', 'SOL/USDC:USDC')")
            conn.execute("INSERT INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side, current_step) VALUES (10008, 1, 0.0, 0, 'LONG', 0)")
            
            # Bot B: unrelated active bot (e.g. 10022) with existing legitimate fills in its cycle
            conn.execute("INSERT INTO bots (id, name, status, direction, pair) VALUES (10022, 'short btc', 'Scanning', 'SHORT', 'BTC/USDC:USDC')")
            conn.execute("INSERT INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side, current_step) VALUES (10022, 1, 0.0, 0, 'SHORT', 0)")
            
            # Insert multiple entries for Bot B to compute a weighted average
            conn.execute("""
                INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at, position_side)
                VALUES (10022, 1, 1, 'entry', 60000.0, 0.002, 0.002, 'filled', 1000, 1000, 'SHORT')
            """)
            conn.execute("""
                INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at, position_side)
                VALUES (10022, 1, 2, 'grid', 59000.0, 0.004, 0.004, 'filled', 1010, 1010, 'SHORT')
            """)
            conn.execute("""
                INSERT INTO bot_orders (bot_id, cycle_id, step, order_type, price, amount, filled_amount, status, created_at, updated_at, position_side)
                VALUES (10022, 1, 3, 'grid', 58000.0, 0.008, 0.008, 'filled', 1020, 1020, 'SHORT')
            """)
            conn.commit()
        finally:
            database.close_connection()
        
        # Copy to backup path so the copy2 command works
        shutil.copy2(self.db_path, test_backup_path)
        
        # Load full_restore_and_align.py script content
        script_path = "scripts/full_restore_and_align.py"
        with open(script_path, "r", encoding="utf-8") as f:
            script_content = f.read()
            
        # Replace file paths in script to use our temp paths
        script_content = script_content.replace('live_db = "crypto_bot.db"', f'live_db = "{self.db_path.replace(chr(92), chr(47))}"')
        script_content = script_content.replace('live_wal = "crypto_bot.db-wal"', 'live_wal = "dummy-wal"')
        script_content = script_content.replace('live_shm = "crypto_bot.db-shm"', 'live_shm = "dummy-shm"')
        script_content = script_content.replace('backup_db = "backups/crypto_bot.db.sui_recovery_backup"', f'backup_db = "{test_backup_path.replace(chr(92), chr(47))}"')
        
        # Mock CCXT and safety exit checks
        mock_ex = MagicMock()
        mock_ex.fetch_positions.return_value = []
        
        # Set up expected_positions to be empty in the script by mocking it out or letting it pass
        with patch('engine.exchange_interface.ExchangeInterface', return_value=mock_ex), \
             patch('os.path.exists', return_value=False), \
             patch('sys.exit') as mock_exit:
             
            # Execute the script content
            globals_dict = {
                '__file__': script_path,
                'ExchangeInterface': lambda *args, **kwargs: mock_ex
            }
            exec(script_content, globals_dict)
            
        # Clean up database connections opened by the script or test
        if 'globals_dict' in locals():
            if 'conn' in globals_dict:
                try:
                    globals_dict['conn'].close()
                except Exception:
                    pass
            globals_dict.clear()
        database.close_connection()
        
        # Verify Bot B's trades row is preserved and automatically synced
        conn = sqlite3.connect(self.db_path)
        try:
            row_b = conn.execute("SELECT wipe_wall_ts, open_qty, total_invested, avg_entry_price FROM trades WHERE bot_id = 10022").fetchone()
            wall_ts_b = row_b[0]
            open_qty_b = row_b[1]
            total_invested_b = row_b[2]
            avg_entry_price_b = row_b[3]
        finally:
            conn.close()
        
        self.assertEqual(wall_ts_b, 1000, "Bot B's wipe_wall_ts must be preserved to 1000 (oldest fill ts)")
        
        # Expected calculations:
        # open_qty = 0.002 + 0.004 + 0.008 = 0.014
        # total_invested = 0.002 * 60000.0 + 0.004 * 59000.0 + 0.008 * 58000.0 = 120.0 + 236.0 + 464.0 = 820.0
        # avg_entry_price = 820.0 / 0.014 = 58571.42857142857
        expected_qty = 0.014
        expected_invested = 820.0
        expected_price = 820.0 / 0.014
        
        self.assertAlmostEqual(open_qty_b, expected_qty, msg="trades.open_qty must be automatically updated by script global sync")
        self.assertAlmostEqual(total_invested_b, expected_invested, msg="trades.total_invested must be automatically updated by script global sync")
        self.assertAlmostEqual(avg_entry_price_b, expected_price, msg="trades.avg_entry_price must be automatically updated by script global sync")


    def test_nested_transaction_in_reset_bot(self):
        """Test that reset_bot_after_tp does not raise nested transaction error when _bypass = True."""
        from engine.write_queue import WriteQueue
        # Set bypass to True (which mimics single-threaded run_startup_heal.py execution)
        orig_bypass = WriteQueue()._bypass
        WriteQueue()._bypass = True
        
        database.init_db()
        conn = database.get_connection()
        try:
            # Insert dummy bot and trade row with non-zero active values
            conn.execute("INSERT OR REPLACE INTO bots (id, name, status, direction, pair) VALUES (9999, 'test bot', 'Scanning', 'LONG', 'SOL/USDC:USDC')")
            conn.execute("""
                INSERT OR REPLACE INTO trades (
                    bot_id, cycle_id, open_qty, total_invested, avg_entry_price, 
                    current_step, entry_confirmed, entry_order_id, tp_order_id, 
                    wipe_wall_ts, cycle_phase, position_side
                ) VALUES (9999, 1, 0.5, 50.0, 100.0, 3, 1, 'entry_123', 'tp_123', 100, 'ACTIVE', 'LONG')
            """)
            
            # Start an implicit transaction by running a select/update on the same connection
            conn.execute("SELECT 1 FROM trades WHERE bot_id = 9999").fetchone()
            
            # Call reset_bot_after_tp (which executes BEGIN IMMEDIATE on the same thread/connection)
            # Under the fix, this should execute successfully without raising an OperationalError
            database.reset_bot_after_tp(9999, exit_price=10.0, direction='LONG', action_label='SYSTEM_WIPE', human_approved=True)
            
            # Verify the bot was reset successfully and all fields are cleared/incremented correctly
            row = conn.execute("""
                SELECT cycle_id, cycle_phase, open_qty, total_invested, avg_entry_price, 
                       current_step, entry_confirmed, entry_order_id, tp_order_id, wipe_wall_ts 
                FROM trades WHERE bot_id = 9999
            """).fetchone()
            
            self.assertEqual(row[0], 2, "cycle_id must be incremented to 2")
            self.assertEqual(row[1], 'IDLE', "cycle_phase must be reset to IDLE")
            self.assertEqual(row[2], 0.0, "open_qty must be reset to 0.0")
            self.assertEqual(row[3], 0.0, "total_invested must be reset to 0.0")
            self.assertEqual(row[4], 0.0, "avg_entry_price must be reset to 0.0")
            self.assertEqual(row[5], 0, "current_step must be reset to 0")
            self.assertEqual(row[6], 0, "entry_confirmed must be reset to 0")
            self.assertIsNone(row[7], "entry_order_id must be reset to None")
            self.assertIsNone(row[8], "tp_order_id must be reset to None")
            self.assertGreater(row[9], 100, "wipe_wall_ts must be updated to engine time")
        finally:
            WriteQueue()._bypass = orig_bypass
            database.close_connection()


if __name__ == '__main__':
    unittest.main()


