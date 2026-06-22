"""
Unit tests for INV-19 Compliance (Unique client_order_id index and suffixing logic).
"""

import os
import sys
import time
import tempfile
import shutil
import unittest
import sqlite3
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.reconciler import StateReconciler

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_inv19.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path

class TestInv19Compliance(unittest.TestCase):

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_inv19_unique_index_constraint(self):
        """
        Verifies that attempting to manually insert a duplicate (bot_id, client_order_id)
        triggers sqlite3.IntegrityError.
        """
        # Insert first order
        self.conn.execute(
            "INSERT INTO bot_orders (bot_id, client_order_id, status) VALUES (?, ?, ?)",
            (1001, "CQB_1001_GRID_1_1", "filled")
        )
        self.conn.commit()

        # Insert second order with same bot_id and client_order_id (should fail unique constraint)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO bot_orders (bot_id, client_order_id, status) VALUES (?, ?, ?)",
                (1001, "CQB_1001_GRID_1_1", "filled")
            )
            self.conn.commit()

    @patch('engine.database.get_connection')
    def test_inv19_generate_cid_uniqueness(self, mock_get_conn):
        """
        Verifies that generate_cid suffixes on collision, returns base CID on
        for_check=True, and suffixes _R{timestamp} directly on is_replacement=True.
        """
        mock_get_conn.return_value = self.conn

        # Test normal generation (no collision)
        cid1 = database.generate_cid(1001, "GRID", 1, 1)
        self.assertEqual(cid1, "CQB_1001_GRID_1_1")

        # Insert it to database to trigger collision on next call
        self.conn.execute(
            "INSERT INTO bot_orders (bot_id, client_order_id) VALUES (?, ?)",
            (1001, "CQB_1001_GRID_1_1")
        )
        self.conn.commit()

        # Test generation with collision (should suffix with timestamp)
        cid2 = database.generate_cid(1001, "GRID", 1, 1)
        self.assertTrue(cid2.startswith("CQB_1001_GRID_1_1_"))
        self.assertNotEqual(cid2, "CQB_1001_GRID_1_1")

        # Test with for_check=True (should return base CID even on collision, without query/suffixing)
        cid_check = database.generate_cid(1001, "GRID", 1, 1, for_check=True)
        self.assertEqual(cid_check, "CQB_1001_GRID_1_1")

        # Test with is_replacement=True (should directly append _R{timestamp} and skip checks)
        cid_repl = database.generate_cid(1001, "GRID", 1, 1, is_replacement=True)
        self.assertTrue(cid_repl.startswith("CQB_1001_GRID_1_1_R"))

    @patch('engine.reconciler.get_connection')
    @patch('engine.database.get_connection')
    @patch('engine.reconciler.logger')
    def test_inv19_reconstructor_suffix(self, mock_logger, mock_db_conn, mock_recon_conn):
        """
        Mocks reconstruct_offline_fills receiving duplicate CIDs from CCXT history
        and asserts they are saved with unique suffixes.
        """
        mock_db_conn.return_value = self.conn
        mock_recon_conn.return_value = self.conn

        # Seed data: Active Bot
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status)
            VALUES (100313, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', 1, 'ACTIVE')
        """)
        # Trades table
        self.conn.execute("""
            INSERT INTO trades (bot_id, cycle_id, basket_start_time, cycle_start_time, total_invested, open_qty, entry_confirmed)
            VALUES (100313, 63, 1779940000, 1779940000, 0.0, 0.0, 0)
        """)
        # Active position on exchange to trigger physical/virtual gap scan
        self.conn.execute("""
            INSERT INTO active_positions (bot_id, pair, side, size)
            VALUES (100313, 'XRPUSDC', 'SHORT', 663.9)
        """)
        
        # Pre-seed an existing order with the CID we want to reconstruct, but filled_amount = 0
        # This simulates a GTX order placement receipt that failed on our side but was filled on exchange
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, client_order_id, cycle_id)
            VALUES (100313, 7, 'entry', '118490365', 1.278, 663.9, 0.0, 'failed', 1779940000, 'CQB_100313_ENTRY_65_7_R', 65)
        """)
        self.conn.commit()

        # Mock CCXT exchange object
        mock_exchange = MagicMock()
        mock_exchange.fetch_closed_orders.return_value = [
            {
                'id': '118490366',
                'clientOrderId': 'CQB_100313_ENTRY_65_7_R',
                'info': {'clientOrderId': 'CQB_100313_ENTRY_65_7_R'},
                'status': 'filled',
                'filled': 663.9,
                'amount': 663.9,
                'average': 1.278,
                'price': 1.278,
                'timestamp': 1779940797000,
                'lastTradeTimestamp': 1779940797000,
                'side': 'sell',
                'symbol': 'XRP/USDC:USDC'
            }
        ]

        reconciler = StateReconciler(exchanges={'future': mock_exchange})

        # Bypass global cooldowns
        if hasattr(StateReconciler, '_last_global_offline_scan'):
            delattr(StateReconciler, '_last_global_offline_scan')
        _pair_key = '_last_pair_scan_XRPUSDC'
        if hasattr(StateReconciler, _pair_key):
            delattr(StateReconciler, _pair_key)

        # Run reconciler sync for XRPUSDC
        reconciler.reconstruct_offline_fills(since_hours=6, pair_filter='XRPUSDC')

        # Check that a new history-orphan was inserted with a suffixed client_order_id
        # The database should now have BOTH the original row with 'CQB_100313_ENTRY_65_7_R' (filled_amount=0)
        # AND a new row with 'CQB_100313_ENTRY_65_7_R_{timestamp}' (filled_amount=663.9)
        rows = self.conn.execute("SELECT * FROM bot_orders WHERE bot_id = 100313 ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        
        # First row is the pre-seeded one
        self.assertEqual(rows[0]['client_order_id'], 'CQB_100313_ENTRY_65_7_R')
        self.assertEqual(rows[0]['filled_amount'], 0.0)

        # Second row is the newly imported history-orphan, which must be suffixed
        new_cid = rows[1]['client_order_id']
        self.assertTrue(new_cid.startswith('CQB_100313_ENTRY_65_7_R_'))
        self.assertEqual(rows[1]['filled_amount'], 663.9)
        self.assertEqual(rows[1]['order_id'], '118490366')

if __name__ == '__main__':
    unittest.main()
