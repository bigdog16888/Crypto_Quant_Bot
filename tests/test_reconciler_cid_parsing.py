import unittest
from unittest.mock import MagicMock, patch
import sqlite3
import time
import os
import sys

sys.path.append(os.getcwd())

from engine.reconciler import StateReconciler

class TestReconcilerCidParsing(unittest.TestCase):

    def setUp(self):
        # Create an in-memory database
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        
        # Setup tables
        self.conn.execute("""
            CREATE TABLE bot_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                step INTEGER,
                order_type TEXT,
                order_id TEXT,
                price REAL,
                amount REAL,
                filled_amount REAL,
                status TEXT,
                created_at INTEGER,
                updated_at INTEGER,
                client_order_id TEXT,
                notes TEXT,
                cycle_id INTEGER,
                filled_at INTEGER,
                position_side TEXT,
                wipe_proof_source TEXT,
                wipe_proof_snapshot TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE active_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT, side TEXT, size REAL
            )
        """)
        self.conn.execute("""
            CREATE TABLE bots (
                id INTEGER PRIMARY KEY,
                name TEXT, pair TEXT, normalized_pair TEXT, direction TEXT, is_active INTEGER, status TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE trades (
                bot_id INTEGER PRIMARY KEY,
                cycle_id INTEGER DEFAULT 1,
                total_invested REAL DEFAULT 0,
                entry_confirmed INTEGER DEFAULT 0,
                position_side TEXT DEFAULT 'BOTH',
                avg_entry_price REAL DEFAULT 0,
                target_tp_price REAL DEFAULT 0,
                current_step INTEGER DEFAULT 0,
                basket_start_time INTEGER DEFAULT 0,
                wipe_wall_ts INTEGER DEFAULT 0,
                open_qty REAL DEFAULT 0,
                cycle_phase TEXT DEFAULT 'ACTIVE',
                cycle_start_time INTEGER DEFAULT 0,
                last_exit_price REAL DEFAULT 0,
                last_exit_time INTEGER DEFAULT 0,
                close_type TEXT DEFAULT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE manual_whitelists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                created_at INTEGER
            )
        """)
        self.conn.execute("""
            CREATE TABLE trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                price REAL,
                amount REAL,
                action TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE reconciliation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                bot_id INTEGER,
                pair TEXT,
                action TEXT,
                details TEXT,
                proof_order_id TEXT
            )
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    @patch('engine.reconciler.get_connection')
    @patch('engine.database.get_connection')
    @patch('engine.reconciler.logger')
    def test_reconstruct_offline_fills_cid_parsing(self, mock_logger, mock_db_conn, mock_recon_conn):
        mock_db_conn.return_value = self.conn
        mock_recon_conn.return_value = self.conn

        # Seed data
        # Child bot (100313) is at cycle 63 in trades table
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status)
            VALUES (100313, 'xrp long_hedge', 'XRP/USDC:USDC', 'XRPUSDC', 'SHORT', 1, 'ACTIVE')
        """)
        self.conn.execute("""
            INSERT INTO trades (bot_id, cycle_id, basket_start_time, cycle_start_time, total_invested, open_qty, entry_confirmed)
            VALUES (100313, 63, 1779940000, 1779940000, 0.0, 0.0, 0)
        """)
        
        # We need a physical/virtual gap so the reconciler scans this pair
        # Active positions: size = -663.9 (short side has position)
        self.conn.execute("""
            INSERT INTO active_positions (pair, side, size)
            VALUES ('XRPUSDC', 'SHORT', 663.9)
        """)
        self.conn.commit()

        # Mock CCXT exchange object
        mock_exchange = MagicMock()
        
        # Closed orders fetched from exchange has clientOrderId CQB_100313_ENTRY_65_7_R (placed for cycle 65)
        # Note: the timestamp is after the cycle_start_time 1779940000 (timestamp in ms)
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

        # Check that the order was inserted into the database with cycle_id = 65 and step = 7
        row = self.conn.execute("SELECT * FROM bot_orders WHERE client_order_id = 'CQB_100313_ENTRY_65_7_R'").fetchone()
        self.assertIsNotNone(row, "Order should be imported as history-orphan")
        self.assertEqual(row['cycle_id'], 65, "Cycle ID should be parsed from client_order_id as 65")
        self.assertEqual(row['step'], 7, "Step should be parsed from client_order_id as 7")

if __name__ == '__main__':
    unittest.main()
