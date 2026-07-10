import unittest
import os
import sys
import shutil
import tempfile
import time
from engine import database

class TestRealizedPnLTpHit(unittest.TestCase):
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

    def test_long_bot_realized_pnl_tp_hit(self):
        """
        Verify that a LONG bot's realized PnL is computed correctly from the bot_orders
        ledger, even when trades cache has already been zeroed out before reset_bot_after_tp is called.
        """
        bot_id = database.add_bot("LongPnLTestBot", "SOL/USDC:USDC", "LONG", 30.0, 1.5, 10.0)
        
        # We need to simulate cycle_id = 1
        cycle_id = 1
        
        # Insert entries (buys)
        # Entry (step 1) at $100
        database.save_bot_order(
            bot_id, 'entry', 'CQB_TEST_ENTRY_1',
            price=100.0, amount=10.0, step=1, status='filled',
            client_order_id='CQB_TEST_ENTRY_1', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 10.0, filled_at = ? WHERE client_order_id = ?", (int(time.time() - 100), 'CQB_TEST_ENTRY_1'))
        
        # Grid (step 2) at $95
        database.save_bot_order(
            bot_id, 'grid', 'CQB_TEST_GRID_2',
            price=95.0, amount=20.0, step=2, status='filled',
            client_order_id='CQB_TEST_GRID_2', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 20.0, filled_at = ? WHERE client_order_id = ?", (int(time.time() - 50), 'CQB_TEST_GRID_2'))
        
        # Grid (step 3) at $90
        database.save_bot_order(
            bot_id, 'grid', 'CQB_TEST_GRID_3',
            price=90.0, amount=40.0, step=3, status='filled',
            client_order_id='CQB_TEST_GRID_3', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 40.0, filled_at = ? WHERE client_order_id = ?", (int(time.time() - 20), 'CQB_TEST_GRID_3'))

        # Insert exit (TP buy back / sell)
        # TP (step 3) at $98
        database.save_bot_order(
            bot_id, 'tp', 'CQB_TEST_TP_1',
            price=98.0, amount=70.0, step=3, status='filled',
            client_order_id='CQB_TEST_TP_1', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 70.0, filled_at = ? WHERE client_order_id = ?", (int(time.time() - 10), 'CQB_TEST_TP_1'))
        
        self.conn.commit()

        # Zero out trades table cache to simulate seal_trade_state running first
        self.cursor.execute("""
            UPDATE trades 
            SET total_invested = 0.0, avg_entry_price = 0.0, open_qty = 0.0, current_step = 0
            WHERE bot_id = ?
        """, (bot_id,))
        self.conn.commit()

        class _FlatEx:
            def fetch_positions(self):
                return []

        # Trigger reset
        database.reset_bot_after_tp(bot_id, exit_price=98.0, exchange=_FlatEx())

        # Assert correct PnL logged in trade_history
        self.cursor.execute("SELECT pnl, amount, cost_usdc, notes FROM trade_history WHERE bot_id = ?", (bot_id,))
        row = self.cursor.fetchone()
        self.assertIsNotNone(row)
        logged_pnl, logged_amount, logged_cost, logged_notes = row
        
        # Expected PnL: (98.0 - 100.0)*10.0 + (98.0 - 95.0)*20.0 + (98.0 - 90.0)*40.0
        # = -20.0 + 60.0 + 320.0 = +360.0
        self.assertAlmostEqual(logged_pnl, 360.0, places=4)
        self.assertEqual(logged_amount, 70.0)
        self.assertEqual(logged_cost, 6500.0)
        self.assertIn("entry_avg=92.857143", logged_notes)
        self.assertIn("pnl=$360.0000", logged_notes)

    def test_short_bot_realized_pnl_tp_hit(self):
        """
        Verify that a SHORT bot's realized PnL is computed correctly from the bot_orders
        ledger, even when trades cache has already been zeroed out before reset_bot_after_tp is called.
        """
        bot_id = database.add_bot("ShortPnLTestBot", "SOL/USDC:USDC", "SHORT", 30.0, 1.5, 10.0)
        
        # We need to simulate cycle_id = 1
        cycle_id = 1
        
        # Insert entries (sells for SHORT bot)
        # Entry (step 1) at $100
        database.save_bot_order(
            bot_id, 'entry', 'CQB_TEST_ENTRY_1_S',
            price=100.0, amount=10.0, step=1, status='filled',
            client_order_id='CQB_TEST_ENTRY_1_S', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 10.0, filled_at = ? WHERE client_order_id = ?", (int(time.time() - 100), 'CQB_TEST_ENTRY_1_S'))
        
        # Grid (step 2) at $105
        database.save_bot_order(
            bot_id, 'grid', 'CQB_TEST_GRID_2_S',
            price=105.0, amount=20.0, step=2, status='filled',
            client_order_id='CQB_TEST_GRID_2_S', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 20.0, filled_at = ? WHERE client_order_id = ?", (int(time.time() - 50), 'CQB_TEST_GRID_2_S'))
        
        # Grid (step 3) at $110
        database.save_bot_order(
            bot_id, 'grid', 'CQB_TEST_GRID_3_S',
            price=110.0, amount=40.0, step=3, status='filled',
            client_order_id='CQB_TEST_GRID_3_S', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 40.0, filled_at = ? WHERE client_order_id = ?", (int(time.time() - 20), 'CQB_TEST_GRID_3_S'))

        # Insert exit (TP buy back / buy)
        # TP (step 3) at $102
        database.save_bot_order(
            bot_id, 'tp', 'CQB_TEST_TP_1_S',
            price=102.0, amount=70.0, step=3, status='filled',
            client_order_id='CQB_TEST_TP_1_S', cycle_id=cycle_id
        )
        self.cursor.execute("UPDATE bot_orders SET filled_amount = 70.0, filled_at = ? WHERE client_order_id = ?", (int(time.time() - 10), 'CQB_TEST_TP_1_S'))
        
        self.conn.commit()

        # Zero out trades table cache to simulate seal_trade_state running first
        self.cursor.execute("""
            UPDATE trades 
            SET total_invested = 0.0, avg_entry_price = 0.0, open_qty = 0.0, current_step = 0
            WHERE bot_id = ?
        """, (bot_id,))
        self.conn.commit()

        class _FlatEx:
            def fetch_positions(self):
                return []

        # Trigger reset
        database.reset_bot_after_tp(bot_id, exit_price=102.0, exchange=_FlatEx())

        # Assert correct PnL logged in trade_history
        self.cursor.execute("SELECT pnl, amount, cost_usdc, notes FROM trade_history WHERE bot_id = ?", (bot_id,))
        row = self.cursor.fetchone()
        self.assertIsNotNone(row)
        logged_pnl, logged_amount, logged_cost, logged_notes = row
        
        # Expected PnL: (100.0 - 102.0)*10.0 + (105.0 - 102.0)*20.0 + (110.0 - 102.0)*40.0
        # = -20.0 + 60.0 + 320.0 = +360.0
        self.assertAlmostEqual(logged_pnl, 360.0, places=4)
        self.assertEqual(logged_amount, 70.0)
        self.assertEqual(logged_cost, 7500.0)
        self.assertIn("entry_avg=107.142857", logged_notes)
        self.assertIn("pnl=$360.0000", logged_notes)

if __name__ == '__main__':
    unittest.main()
