import unittest
import os
import sys
import shutil
import tempfile
import time
from engine import database

class TestReconciliationPnLBasis(unittest.TestCase):
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

        # Initialize database
        database.init_db()
        self.conn = database.get_connection()
        self.cursor = self.conn.cursor()

    def tearDown(self):
        # Close connection
        database.close_connection()
        
        # Restore original DB_PATH
        database.DB_PATH = self.original_db_path
        
        # Remove temporary directory
        shutil.rmtree(self.test_dir)

    def test_reconciliation_cost_basis_calculation(self):
        """
        Verify that synthetic LIVE_GUARD reconciliation rows with real market prices
        result in a correct average entry price.
        """
        # Add a LONG bot
        bot_id = database.add_bot("HedgePnLTestBot", "ETH/USDC:USDC", "LONG", 30.0, 1.5, 10.0)
        cycle_id = 1

        # 1. Insert a REAL buy fill at market price
        database.save_bot_order(
            bot_id, 'entry', 'CQB_REAL_ENTRY_1',
            price=1800.0, amount=0.5, step=1, status='filled',
            client_order_id='CQB_REAL_ENTRY_1', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 0.5 WHERE client_order_id = ?", ('CQB_REAL_ENTRY_1',))

        # 2. Insert a synthetic LIVE_GUARD_INV30 reconciliation fill with a REAL price (per the new fix)
        database.save_bot_order(
            bot_id, 'entry', 'CQB_SYNTHETIC_INV30',
            price=1810.0, amount=1.0, step=2, status='filled',
            client_order_id='CQB_TEST_LIVE_GUARD_INV30_1_2', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 1.0 WHERE client_order_id = ?", ('CQB_TEST_LIVE_GUARD_INV30_1_2',))

        # We must insert a trades row so safe_wipe_bot / reset_bot_after_tp can load trades state
        self.cursor.execute("""
            INSERT OR REPLACE INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (bot_id, cycle_id, 2, 1.5, 2710.0, 1806.6666))

        # Insert exit order (flatten_close) with price = 0
        database.save_bot_order(
            bot_id, 'flatten_close', 'CQB_TEST_FLATTEN',
            price=0.0, amount=1.5, step=2, status='filled',
            client_order_id='CQB_TEST_FLATTEN', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 1.5 WHERE client_order_id = ?", ('CQB_TEST_FLATTEN',))
        self.conn.commit()

        # 3. Trigger a manual wipe / close (calls reset_bot_after_tp internally)
        database.reset_bot_after_tp(
            bot_id,
            exit_price=0.0, # Wiped at 0 exit price
            action_label='MANUAL_CLOSE',
            notes='test manual close reconciliation basis',
            human_approved=True
        )

        # 4. Fetch the resulting trade_history record
        self.cursor.execute("""
            SELECT price, amount, pnl, notes
            FROM trade_history
            WHERE bot_id = ? AND action = 'MANUAL_CLOSE'
        """, (bot_id,))
        row = self.cursor.fetchone()
        
        self.assertIsNotNone(row)
        exit_price, amount, pnl, notes = row
        self.assertIn("entry_avg=1806.666667", notes)

    def test_real_order_executed_at_real_fill_price(self):
        """
        Verify that when a real close/flatten order executed, the PnL calculation
        uses that real exit price, even if the database close order price was 0.0.
        """
        # Add a LONG bot
        bot_id = database.add_bot("RealExitPnLTestBot", "ETH/USDC:USDC", "LONG", 30.0, 1.5, 10.0)
        cycle_id = 1

        # 1. Insert entry orders (Total cost = 2710.0, qty = 1.5, avg = 1806.6667)
        database.save_bot_order(
            bot_id, 'entry', 'CQB_REAL_ENTRY_1',
            price=1800.0, amount=0.5, step=1, status='filled',
            client_order_id='CQB_REAL_ENTRY_1', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 0.5 WHERE client_order_id = ?", ('CQB_REAL_ENTRY_1',))

        database.save_bot_order(
            bot_id, 'entry', 'CQB_SYNTHETIC_INV30',
            price=1810.0, amount=1.0, step=2, status='filled',
            client_order_id='CQB_TEST_LIVE_GUARD_INV30_1_2', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 1.0 WHERE client_order_id = ?", ('CQB_TEST_LIVE_GUARD_INV30_1_2',))

        self.cursor.execute("""
            INSERT OR REPLACE INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (bot_id, cycle_id, 2, 1.5, 2710.0, 1806.6666))

        # 2. Insert exit order (flatten_close) with price = 0.0 (simulating pre-fill or recovered order)
        database.save_bot_order(
            bot_id, 'flatten_close', 'CQB_TEST_FLATTEN',
            price=0.0, amount=1.5, step=2, status='filled',
            client_order_id='CQB_TEST_FLATTEN', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 1.5 WHERE client_order_id = ?", ('CQB_TEST_FLATTEN',))
        self.conn.commit()

        # 3. Call reset_bot_after_tp with a real average exit price = 1805.0
        database.reset_bot_after_tp(
            bot_id,
            exit_price=1805.0, # Real average close price
            action_label='MANUAL_CLOSE',
            notes='test real exit price',
            human_approved=True
        )

        # 4. Fetch PnL from trade_history
        self.cursor.execute("""
            SELECT pnl, notes
            FROM trade_history
            WHERE bot_id = ? AND action = 'MANUAL_CLOSE'
        """, (bot_id,))
        row = self.cursor.fetchone()
        self.assertIsNotNone(row)
        pnl, notes = row

        # Expected entry average: 1806.666667
        # Expected exit value: 1.5 * 1805.0 = 2707.5
        # Expected PnL: 2707.5 - 2710.0 = -2.50
        self.assertAlmostEqual(pnl, -2.50, places=2)

    def test_phantom_wipe_no_order_placed(self):
        """
        Verify that a phantom wipe (no exit order placed, exit_price=0.0) correctly computes realized PnL to 0.0.
        """
        # Add a LONG bot
        bot_id = database.add_bot("PhantomWipeTestBot", "ETH/USDC:USDC", "LONG", 30.0, 1.5, 10.0)
        cycle_id = 1

        # 1. Insert entry orders (Total cost = 2710.0, qty = 1.5, avg = 1806.6667)
        database.save_bot_order(
            bot_id, 'entry', 'CQB_REAL_ENTRY_1',
            price=1800.0, amount=0.5, step=1, status='filled',
            client_order_id='CQB_REAL_ENTRY_1', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 0.5 WHERE client_order_id = ?", ('CQB_REAL_ENTRY_1',))

        database.save_bot_order(
            bot_id, 'entry', 'CQB_SYNTHETIC_INV30',
            price=1810.0, amount=1.0, step=2, status='filled',
            client_order_id='CQB_TEST_LIVE_GUARD_INV30_1_2', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 1.0 WHERE client_order_id = ?", ('CQB_TEST_LIVE_GUARD_INV30_1_2',))

        self.cursor.execute("""
            INSERT OR REPLACE INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (bot_id, cycle_id, 2, 1.5, 2710.0, 1806.6666))
        self.conn.commit()

        # 2. Call reset_bot_after_tp with exit_price=0.0 (no exit order in database)
        database.reset_bot_after_tp(
            bot_id,
            exit_price=0.0, # Phantom wipe
            action_label='SYSTEM_WIPE',
            notes='test phantom wipe',
            human_approved=True
        )

        # 3. Fetch PnL from trade_history
        self.cursor.execute("""
            SELECT pnl, notes
            FROM trade_history
            WHERE bot_id = ? AND action = 'SYSTEM_WIPE'
        """, (bot_id,))
        row = self.cursor.fetchone()
        self.assertIsNotNone(row)
        pnl, notes = row

        # Expected PnL: 0.0
        self.assertEqual(pnl, 0.0)

if __name__ == "__main__":
    unittest.main()
