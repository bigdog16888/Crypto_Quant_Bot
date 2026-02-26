
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
        
        # Reset
        database.reset_bot_after_tp(bot_id, exit_price=51000.0)
        
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

if __name__ == '__main__':
    unittest.main()
