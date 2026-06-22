import unittest
import os
import sys
import shutil
import tempfile
import threading
import time

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import database
from engine.ledger import credit_fill, seal_trade_state
from engine.write_queue import WriteQueue

# Helper wrapped functions to test WriteQueue directly
def dummy_wrapped_func(val):
    return WriteQueue().put_and_wait(dummy_actual, val)

def dummy_actual(val):
    if val == "fail":
        raise ValueError("Intentional Failure")
    # Verify execution is running on the WriteQueue worker thread
    assert threading.current_thread().name == "WriteQueueWorker"
    time.sleep(0.01)
    return val * 2

def _seed_bot(conn, bot_id, pair, direction, open_qty=0.0, cycle=1):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (?, ?, ?, ?, ?, 1, 'IN TRADE')",
        (bot_id, f'bot_{bot_id}', pair, 'BTCUSDC', direction),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side) "
        "VALUES (?, ?, ?, 0, ?)",
        (bot_id, cycle, open_qty, direction),
    )
    conn.commit()

class TestWriteQueue(unittest.TestCase):
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

        # Disable bypass for testing the queue itself
        self.original_bypass = getattr(WriteQueue(), '_bypass', False)
        WriteQueue()._bypass = False

    def tearDown(self):
        # Close connection on caller thread and worker thread (while _bypass is still False)
        database.close_connection()
        
        # Restore bypass flag
        WriteQueue()._bypass = getattr(self, 'original_bypass', False)
        
        # Restore original DB_PATH
        database.DB_PATH = self.original_db_path
        
        # Remove temporary directory
        shutil.rmtree(self.test_dir)

    def test_write_queue_concurrency(self):
        """Test that 10 threads concurrently executing a wrapped function runs successfully on the worker thread."""
        results = []
        threads = []
        errors = []

        def run(i):
            try:
                res = dummy_wrapped_func(i)
                results.append(res)
            except Exception as e:
                errors.append(e)

        for i in range(10):
            t = threading.Thread(target=run, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Encountered unexpected errors: {errors}")
        self.assertEqual(sorted(results), [i * 2 for i in range(10)])

    def test_write_queue_exception_propagation(self):
        """Test that exceptions raised on the worker thread are correctly propagated back to the caller."""
        with self.assertRaises(ValueError) as ctx:
            dummy_wrapped_func("fail")
        self.assertEqual(str(ctx.exception), "Intentional Failure")

    def test_write_queue_handles_nested_wrapped_calls(self):
        """Test the nested-call case: credit_fill -> apply_oneway_entry_cross_reduction -> seal_trade_state
        Ensure it executes without deadlocking and updates the database correctly.
        """
        database.init_db()
        conn = database.get_connection()
        
        # Seed 10016 (LONG) and 10022 (SHORT)
        _seed_bot(conn, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.5)
        _seed_bot(conn, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.5)
        
        # Seed order history to justify 0.5 open_qty
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step) "
            "VALUES (10022, 'entry', 'order_sell_1', 'CQB_10022_ENTRY_1', 50000.0, 0.5, 0.5, 'filled', 1, 1)"
        )
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step) "
            "VALUES (10016, 'entry', 'order_buy_0', 'CQB_10016_ENTRY_0', 50000.0, 0.5, 0.5, 'filled', 1, 1)"
        )
        # Seed order_buy_1 that we are about to credit
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, cycle_id, step) "
            "VALUES (10016, 'grid', 'order_buy_1', 'CQB_10016_GRID_1', 50000.0, 0.1, 0.0, 'open', 1, 2)"
        )
        conn.commit()
        
        # Verify initial open_qty
        row16 = conn.execute("SELECT open_qty FROM trades WHERE bot_id = 10016").fetchone()
        row22 = conn.execute("SELECT open_qty FROM trades WHERE bot_id = 10022").fetchone()
        self.assertAlmostEqual(row16[0], 0.5)
        self.assertAlmostEqual(row22[0], 0.5)
        
        # Call credit_fill (wrapped), which triggers apply_oneway_entry_cross_reduction (wrapped)
        # and then seal_trade_state (wrapped)
        res = credit_fill(
            bot_id=10016,
            order_id='order_buy_1',
            cumulative_qty=0.1,
            avg_price=50000.0,
            order_type='grid',
            is_cumulative=False,
            sync_to_exchange=False,
            exchange=None,
        )
        self.assertTrue(res)
        
        # Verify post-netting state
        # 10016 got +0.1 entry but netted against 10022, so it receives a virtual_netting exit.
        # Its virtual open_qty remains 0.5.
        row16_post = conn.execute("SELECT open_qty FROM trades WHERE bot_id = 10016").fetchone()
        # 10022 got -0.1 reduction, open_qty becomes 0.4
        row22_post = conn.execute("SELECT open_qty FROM trades WHERE bot_id = 10022").fetchone()
        
        self.assertAlmostEqual(row16_post[0], 0.5)
        self.assertAlmostEqual(row22_post[0], 0.4)
