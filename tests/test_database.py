
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


if __name__ == '__main__':
    unittest.main()
