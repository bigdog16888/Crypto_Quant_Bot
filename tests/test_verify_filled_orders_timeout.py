
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db, verify_filled_orders_against_exchange
import tempfile
import shutil

class TestVerifyFilledOrdersTimeout(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        db_path = os.path.join(self.test_dir, 'test_timeout.db')
        database.DB_PATH = db_path
        database._local = database.threading.local()
        init_db()
        self.conn = get_connection()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_timeout_on_fetch_order_skips_gracefully(self):
        """Test that a timeout on fetch_order logs warning and skips order, doesn't hang"""
        # Insert a bot with a filled order
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type, is_active)
            VALUES (10001, 'test_bot', 'SOL/USDC:USDC', 'SOLUSDC', 'SHORT', 'IN TRADE', 'standard', 1)
        """)
        self.conn.execute("""
            INSERT INTO bot_orders (id, bot_id, order_type, client_order_id, order_id, amount, filled_amount, status, cycle_id, step)
            VALUES (1, 10001, 'entry', 'CQB_10001_TEST', 'ORDER123', 1.0, 1.0, 'filled', 1, 1)
        """)
        self.conn.commit()

        # Create mock exchange that hangs on fetch_order
        mock_exchange = MagicMock()
        
        # Make fetch_order raise a timeout exception (simulating CCXT timeout)
        from ccxt.base.errors import RequestTimeout
        mock_exchange.fetch_order.side_effect = RequestTimeout("DNS resolution failed")
        mock_exchange.fetch_order_by_client_order_id.side_effect = RequestTimeout("DNS resolution failed")

        # This should NOT hang - it should catch the timeout, log warning, and continue
        import logging
        with self.assertLogs('engine.database', level='WARNING') as cm:
            healed = verify_filled_orders_against_exchange(mock_exchange, bot_id=10001)
        
        # Should complete without hanging (healed=0 since order was skipped)
        self.assertEqual(healed, 0)
        
        # Should have logged the timeout warning
        warning_logs = [log for log in cm.output if 'timeout/error' in log or 'Could not fetch order after timeout' in log]
        self.assertTrue(len(warning_logs) > 0, f"Expected timeout warning in logs: {cm.output}")

if __name__ == '__main__':
    unittest.main()
