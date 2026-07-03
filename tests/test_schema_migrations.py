import os
import sys
import tempfile
import shutil
import sqlite3
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.migrations.migration_base import SafeMigration
from engine.migrations.migration_008_archive_legacy_netting import Migration008

class TestMigrationDummy(SafeMigration):
    version = 'test_migration_dummy'
    description = 'Dummy test migration'
    requires_flat_positions = True

    run_impl_called = False

    @classmethod
    def _run_impl(cls, conn):
        cls.run_impl_called = True
        conn.execute("UPDATE bots SET name='migrated_dummy'")

class TestSchemaMigrations(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, 'test_migrations.db')
        self.conn = sqlite3.connect(self.db_path)
        
        # Create minimal bots and trades tables for migrations
        self.conn.execute("""
            CREATE TABLE bots (
                id INTEGER PRIMARY KEY,
                name TEXT,
                pair TEXT,
                normalized_pair TEXT,
                direction TEXT,
                status TEXT,
                bot_type TEXT,
                is_active INTEGER,
                rsi_limit REAL,
                martingale_multiplier REAL,
                base_size REAL,
                strategy_type TEXT,
                cascade_started_at INTEGER
            )
        """)
        self.conn.execute("""
            CREATE TABLE trades (
                bot_id INTEGER PRIMARY KEY,
                open_qty REAL,
                cycle_id INTEGER,
                position_side TEXT,
                total_invested REAL,
                avg_entry_price REAL,
                current_step INTEGER,
                entry_confirmed INTEGER,
                basket_start_time INTEGER
            )
        """)
        self.conn.execute("""
            CREATE TABLE bot_orders (
                bot_id INTEGER,
                order_type TEXT,
                filled_amount REAL,
                amount REAL,
                price REAL,
                status TEXT,
                cycle_id INTEGER,
                position_side TEXT,
                created_at INTEGER,
                client_order_id TEXT
            )
        """)
        # Insert a dummy bot
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type, is_active, cascade_started_at)
            VALUES (10001, 'dummy_bot', 'BTC/USDC', 'BTCUSDC', 'LONG', 'IN TRADE', 'standard', 1, 0)
        """)
        self.conn.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side, total_invested, avg_entry_price, current_step, entry_confirmed)
            VALUES (10001, 0.0, 1, 'LONG', 0.0, 0.0, 1, 1)
        """)
        self.conn.commit()
        TestMigrationDummy.run_impl_called = False

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_migration_skipped_when_already_applied(self):
        # Set up schema_migrations with dummy already applied
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at INTEGER NOT NULL,
                description TEXT
            )
        """)
        self.conn.execute(
            "INSERT INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
            (TestMigrationDummy.version, 12345, TestMigrationDummy.description)
        )
        self.conn.commit()

        # Set positions open to show that if it wasn't skipped, it would fail preflight
        self.conn.execute("UPDATE trades SET open_qty = 1.0 WHERE bot_id = 10001")
        self.conn.commit()

        # Run migration — should skip preflight/impl cleanly without error
        TestMigrationDummy.run(self.conn)
        self.assertFalse(TestMigrationDummy.run_impl_called)

    def test_migration_runs_when_not_applied(self):
        # schema_migrations does not have dummy migration recorded
        # Run migration
        TestMigrationDummy.run(self.conn)
        self.assertTrue(TestMigrationDummy.run_impl_called)

        # Check recorded in schema_migrations
        applied = self.conn.execute(
            "SELECT applied_at, description FROM schema_migrations WHERE version=?",
            (TestMigrationDummy.version,)
        ).fetchone()
        self.assertIsNotNone(applied)
        self.assertEqual(applied[1], TestMigrationDummy.description)

    def test_migration008_noop_when_nothing_to_archive(self):
        # Insert archived virtual_netting rows (or none)
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, status, client_order_id)
            VALUES (10001, 'virtual_netting', 'archived_legacy', 'CQB_10001_VN_1')
        """)
        # Open positions exists (would trigger INV-33 if preflight ran)
        self.conn.execute("UPDATE trades SET open_qty = 1.0 WHERE bot_id = 10001")
        self.conn.commit()

        # Run Migration008 — should complete as no-op without raising RuntimeError
        try:
            Migration008.run(self.conn)
        except RuntimeError as e:
            self.fail(f"Migration008 raised RuntimeError unexpectedly: {e}")

        # Check that it recorded as applied
        applied = self.conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version='migration_008_archive_legacy_netting'"
        ).fetchone()
        self.assertIsNotNone(applied)

    def test_migration008_blocks_when_work_exists_and_bots_open(self):
        # Insert unarchived virtual_netting row
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, status, client_order_id)
            VALUES (10001, 'virtual_netting', 'filled', 'CQB_10001_VN_1')
        """)
        # Open positions exists
        self.conn.execute("UPDATE trades SET open_qty = 1.0 WHERE bot_id = 10001")
        self.conn.commit()

        # Run Migration008 — should raise INV-33 RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            Migration008.run(self.conn)
        self.assertIn("INV-33 VIOLATED", str(ctx.exception))

if __name__ == '__main__':
    unittest.main()
