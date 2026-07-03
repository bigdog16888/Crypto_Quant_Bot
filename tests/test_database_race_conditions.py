import unittest
import os
import sys
import shutil
import tempfile
import logging
from unittest.mock import MagicMock

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import database

class TestDatabaseRaceConditions(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for the test database
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_bot_race.db")
        
        # Patch the DB_PATH in the database module
        self.original_db_path = database.DB_PATH
        database.DB_PATH = self.db_path
        
        # Reset thread local storage
        if hasattr(database._local, 'connection'):
            del database._local.connection

        # Set up log capture
        self.log_messages = []
        class CaptureHandler(logging.Handler):
            def __init__(self, target):
                super().__init__()
                self.target = target
            def emit(self, record):
                self.target.append(record)

        self.handler = CaptureHandler(self.log_messages)
        database.logger.addHandler(self.handler)
        database.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        # Remove handler
        database.logger.removeHandler(self.handler)
        
        # Close connection
        database.close_connection()
        
        # Restore original DB_PATH
        database.DB_PATH = self.original_db_path
        
        # Remove temporary directory
        shutil.rmtree(self.test_dir)

    def test_downgrade_prevention(self):
        """Test 1: Once an order is filled, it cannot be downgraded to cancelled."""
        database.init_db()
        bot_id = database.add_bot("Bot1", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        # Insert mock open order
        database.save_bot_order(bot_id, 'tp', 'test_order_1', 100.0, 1.0, 1, 'open', 'CQB_1_TP_1')
        
        # Transition to filled
        res1 = database.update_order_status('test_order_1', 'filled', bot_id=bot_id, filled_qty=1.0)
        self.assertTrue(res1)
        
        # Try to downgrade to cancelled
        res2 = database.update_order_status('test_order_1', 'cancelled', bot_id=bot_id, filled_qty=0.0)
        self.assertFalse(res2)
        
        # Verify status in database remains filled
        conn = database.get_connection()
        row = conn.execute("SELECT status, filled_amount FROM bot_orders WHERE order_id = ?", ('test_order_1',)).fetchone()
        self.assertEqual(row[0], 'filled')
        self.assertEqual(row[1], 1.0)

    def test_cancel_fill_race_simulation(self):
        """Test 2: Simulate race condition paths where cancelled and filled arrive in different order."""
        database.init_db()
        bot_id = database.add_bot("Bot2", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        # Scenario A: Cancel arrives first, then late fill arrives with quantity
        database.save_bot_order(bot_id, 'tp', 'test_order_2', 100.0, 1.0, 1, 'open', 'CQB_2_TP_2')
        
        res_cancel = database.update_order_status('test_order_2', 'cancelled', bot_id=bot_id, filled_qty=0.0)
        self.assertTrue(res_cancel)
        
        # Fill arrives late with evidence of fill
        res_fill = database.update_order_status('test_order_2', 'filled', bot_id=bot_id, filled_qty=1.0)
        self.assertTrue(res_fill)
        
        conn = database.get_connection()
        row2 = conn.execute("SELECT status, filled_amount FROM bot_orders WHERE order_id = ?", ('test_order_2',)).fetchone()
        self.assertEqual(row2[0], 'filled')
        self.assertEqual(row2[1], 1.0)
        
        # Scenario B: Fill arrives first, then cancel arrives (ignored)
        database.save_bot_order(bot_id, 'tp', 'test_order_3', 100.0, 1.0, 1, 'open', 'CQB_2_TP_3')
        
        res_fill_first = database.update_order_status('test_order_3', 'filled', bot_id=bot_id, filled_qty=1.0)
        self.assertTrue(res_fill_first)
        
        res_cancel_second = database.update_order_status('test_order_3', 'cancelled', bot_id=bot_id, filled_qty=0.0)
        self.assertFalse(res_cancel_second)
        
        row3 = conn.execute("SELECT status, filled_amount FROM bot_orders WHERE order_id = ?", ('test_order_3',)).fetchone()
        self.assertEqual(row3[0], 'filled')
        self.assertEqual(row3[1], 1.0)

    def test_cycle_tagging_inheritance(self):
        """Test 3: Verify update inherits cycle_id from existing order record."""
        database.init_db()
        bot_id = database.add_bot("Bot3", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        # Manually set current cycle in trades to 122
        conn = database.get_connection()
        conn.execute("UPDATE trades SET cycle_id = 122 WHERE bot_id = ?", (bot_id,))
        conn.commit()
        
        # Save open order (should get cycle_id = 122)
        database.save_bot_order(bot_id, 'tp', 'test_order_4', 100.0, 1.0, 1, 'open', 'CQB_3_TP_4', cycle_id=None)
        
        row_init = conn.execute("SELECT cycle_id FROM bot_orders WHERE order_id = ?", ('test_order_4',)).fetchone()
        self.assertEqual(row_init[0], 122)
        
        # Advance trades cycle to 123
        conn.execute("UPDATE trades SET cycle_id = 123 WHERE bot_id = ?", (bot_id,))
        conn.commit()
        
        # Update existing order without passing cycle_id. It should inherit 122.
        database.save_bot_order(bot_id, 'tp', 'test_order_4', 100.0, 1.0, 1, 'filled', 'CQB_3_TP_4', cycle_id=None)
        
        row_final = conn.execute("SELECT cycle_id FROM bot_orders WHERE order_id = ?", ('test_order_4',)).fetchone()
        self.assertEqual(row_final[0], 122)

    def test_cycle_tagging_fallback_alert(self):
        """Test 4: Verify fallback to trades cycle logs a loud warning on terminal status updates."""
        database.init_db()
        bot_id = database.add_bot("Bot4", "BTC/USDT", "LONG", 30.0, 1.5, 10.0)
        
        # Set trades cycle to 123
        conn = database.get_connection()
        conn.execute("UPDATE trades SET cycle_id = 123 WHERE bot_id = ?", (bot_id,))
        conn.commit()
        
        # Clear log messages
        self.log_messages.clear()
        
        # Save brand new order (no pre-existing row) with terminal status 'filled'
        database.save_bot_order(bot_id, 'tp', 'test_order_5', 100.0, 1.0, 1, 'filled', 'CQB_4_TP_5', cycle_id=None)
        
        # Row should get cycle_id = 123
        row = conn.execute("SELECT cycle_id FROM bot_orders WHERE order_id = ?", ('test_order_5',)).fetchone()
        self.assertEqual(row[0], 123)
        
        # Verify loud warning was logged
        warnings = [rec.message for rec in self.log_messages if rec.levelname == 'WARNING' and '🚨 [CYCLE-FALLBACK]' in rec.message]
        self.assertEqual(len(warnings), 1)
        self.assertIn("CQB_4_TP_5", warnings[0])
        self.assertIn("123", warnings[0])

if __name__ == '__main__':
    unittest.main()
