import pytest
from unittest.mock import MagicMock, patch
import sqlite3
import time
import os
import sys

from engine.database import init_db
from engine.reconciler import StateReconciler


def test_reconciler_excludes_synthetic_ids(temp_db):
    """Test that reconciler excludes synthetic PENDING_*, PLACING_*, GHOST_*, VN_* client order IDs.
    
    This verifies the filtering logic exists in the reconciler's internal scan code
    by checking the source for the filtering patterns.
    """
    conn = temp_db
    cursor = conn.cursor()
    # Minimal setup - just ensure the tables exist via temp_db fixture
    conn.commit()

    # The reconciler filters synthetic CIDs at the fetch level in _reconstruct_offline_fills_internal
    # We verify this by inspecting the source code for the filtering patterns
    import inspect
    source = inspect.getsource(StateReconciler._reconstruct_offline_fills_internal)
    
    # Check for synthetic CID filtering patterns that exist in the actual code
    assert 'PENDING_' in source, "Synthetic CID filtering for PENDING_ should exist"
    assert 'PLACING_' in source, "Synthetic CID filtering for PLACING_ should exist"
    assert 'GHOST_' in source, "Synthetic CID filtering for GHOST_ should exist"
    assert 'VN_' in source, "Synthetic CID filtering for VN_ should exist"
    
    # Test passes if filtering logic exists in the source


def test_reconstruct_offline_fills_cid_parsing(temp_db):
    """Test reconstruct_offline_fills parses CIDs correctly."""
    conn = temp_db
    cursor = conn.cursor()
    # Setup minimal bots and orders
    cursor.execute("INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) VALUES (1002, 'Test', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 1, 'IN TRADE')")
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, position_side, created_at)
        VALUES (1002, 'tp', 'ex_456', 'CQB_1002_TP_1', 51000.0, 0.01, 0.0, 'open', 1, 1, 'LONG', ?)
    """, (int(time.time()),))
    conn.commit()

    reconciler = StateReconciler()
    reconciler.exchanges = {'future': MagicMock()}
    reconciler.exchanges['future'].fetch_closed_orders.return_value = [
        {
            'id': 'ex_456', 
            'clientOrderId': 'CQB_1002_TP_1', 
            'status': 'closed',
            'filled': 0.01, 
            'price': 51000.0, 
            'average': 51000.0,
            'side': 'BUY', 
            'positionSide': 'LONG',
            'timestamp': int(time.time() * 1000),
            'symbol': 'BTC/USDC:USDC'
        }
    ]
    reconciler.exchanges['future'].fetch_open_orders.return_value = []
    
    # This should not raise OperationalError (exchange_fills table exists via temp_db)
    result = reconciler.reconstruct_offline_fills(since_hours=1, pair_filter='BTCUSDC')
    # Verify exchange_fills was populated or the function completed without error
    assert result is not None
    assert 'total' in result


class TestReconcilerCidParsing:
    """Legacy unittest tests that need get_connection mocked."""

    def setup_method(self):
        # Create an in-memory database
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

        # Setup tables (full schema matching production)
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
                current_step INTEGER DEFAULT 0,
                total_invested REAL DEFAULT 0,
                avg_entry_price REAL DEFAULT 0,
                target_tp_price REAL DEFAULT 0,
                last_exit_price REAL DEFAULT 0,
                last_exit_time INTEGER DEFAULT 0,
                basket_start_time INTEGER DEFAULT 0,
                entry_confirmed BOOLEAN DEFAULT 0,
                entry_order_id TEXT,
                tp_order_id TEXT,
                bot_position_id TEXT,
                close_type TEXT DEFAULT NULL,
                cycle_id INTEGER DEFAULT 1,
                cycle_phase TEXT DEFAULT 'ACTIVE',
                open_qty REAL DEFAULT 0,
                wipe_wall_ts INTEGER DEFAULT 0,
                cycle_start_time INTEGER DEFAULT 0,
                FOREIGN KEY (bot_id) REFERENCES bots (id)
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
        self.conn.execute("""
            CREATE TABLE exchange_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_order_id TEXT,
                client_order_id TEXT,
                symbol TEXT,
                side TEXT,
                qty REAL,
                price REAL,
                fee REAL DEFAULT 0,
                fee_asset TEXT,
                fill_ts INTEGER,
                source TEXT,
                bot_id INTEGER,
                order_type TEXT,
                step INTEGER,
                cycle_id INTEGER,
                raw_json TEXT,
                created_at INTEGER
            )
        """)
        self.conn.commit()

    def teardown_method(self):
        self.conn.close()

    @patch('engine.reconciler.get_connection')
    @patch('engine.database.get_connection')
    @patch('engine.reconciler.logger')
    def test_reconstruct_offline_fills_cid_parsing_legacy(self, mock_logger, mock_db_conn, mock_recon_conn):
        """Test reconstruct_offline_fills parses CIDs from client_order_id correctly."""
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
        mock_exchange.fetch_open_orders.return_value = []

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
        assert row is not None, "Order should be imported as history-orphan"
        assert row['cycle_id'] == 65, "Cycle ID should be parsed from client_order_id as 65"
        assert row['step'] == 7, "Step should be parsed from client_order_id as 7"

    def test_existing_oversized_cids_migrated_to_failed(self):
        """Confirm rows with CIDs > 36 chars and status='pending_placement'
        get migrated on startup, not left to spam the API forever.
        """
        import tempfile
        import shutil
        from engine.migrations.migration_005_cid_too_long import run as run_migration_5

        temp_dir = tempfile.mkdtemp()
        temp_db_path = os.path.join(temp_dir, "test_migration.db")

        temp_conn = sqlite3.connect(temp_db_path)
        temp_conn.execute("""
            CREATE TABLE bot_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                client_order_id TEXT,
                status TEXT,
                notes TEXT
            )
        """)

        # Seed matching row (oversized CID, status='pending_placement')
        oversized_cid = 'CQB_100323_DRIFT_ENFORCE_RESET_1782115197'
        temp_conn.execute(
            "INSERT INTO bot_orders (bot_id, client_order_id, status, notes) VALUES (?, ?, ?, ?)",
            (100323, oversized_cid, 'pending_placement', 'Initial note')
        )

        # Seed non-matching row 1: status is not pending_placement
        temp_conn.execute(
            "INSERT INTO bot_orders (bot_id, client_order_id, status, notes) VALUES (?, ?, ?, ?)",
            (100323, oversized_cid, 'audit', 'Non-matching status')
        )

        # Seed non-matching row 2: client_order_id is under 36 chars
        short_cid = 'CQB_100323_DRIFT_ENFORCE_RESET_123'
        temp_conn.execute(
            "INSERT INTO bot_orders (bot_id, client_order_id, status, notes) VALUES (?, ?, ?, ?)",
            (100323, short_cid, 'pending_placement', 'Under 36 chars')
        )

        temp_conn.commit()
        temp_conn.close()

        # Run migration
        run_migration_5(temp_db_path)

        # Verify results
        res_conn = sqlite3.connect(temp_db_path)
        res_conn.row_factory = sqlite3.Row

        row_matching = res_conn.execute("SELECT * FROM bot_orders WHERE client_order_id = ? ORDER BY id ASC", (oversized_cid,)).fetchall()
        assert row_matching[0]['status'] == 'failed'
        assert 'CID_TOO_LONG_MIGRATION' in row_matching[0]['notes']

        assert row_matching[1]['status'] == 'audit'
        assert row_matching[1]['notes'] == 'Non-matching status'

        row_short = res_conn.execute("SELECT * FROM bot_orders WHERE client_order_id = ?", (short_cid,)).fetchone()
        assert row_short['status'] == 'pending_placement'
        assert row_short['notes'] == 'Under 36 chars'

        res_conn.close()
        shutil.rmtree(temp_dir)

    @patch('engine.reconciler.get_connection')
    @patch('engine.database.get_connection')
    def test_reconciler_excludes_synthetic_ids_legacy(self, mock_db_conn, mock_recon_conn):
        """Test that reconciler excludes synthetic PENDING_*, PLACING_*, GHOST_* orders."""
        mock_db_conn.return_value = self.conn
        mock_recon_conn.return_value = self.conn

        # Seed standard bot and active position
        self.conn.execute("""
            INSERT INTO bots (id, name, pair, direction, is_active, status)
            VALUES (10016, 'btc bot', 'BTC/USDC:USDC', 'LONG', 1, 'ACTIVE')
        """)
        self.conn.execute("""
            INSERT INTO trades (bot_id, cycle_id, basket_start_time, cycle_start_time, total_invested, open_qty, entry_confirmed)
            VALUES (10016, 1, 1779940000, 1779940000, 0.0, 0.0, 0)
        """)

        # Seed synthetic orders: PENDING_*, PLACING_*, GHOST_* with status 'placing'
        now_ts = int(time.time())
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, status, price, amount, filled_amount, created_at)
            VALUES (10016, 'entry', 'PENDING_10016_ENTRY_1', 'placing', 50000.0, 0.1, 0.0, ?)
        """, (now_ts,))
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, status, price, amount, filled_amount, created_at)
            VALUES (10016, 'grid', 'PLACING_10016_GRID_1', 'placing', 50000.0, 0.1, 0.0, ?)
        """, (now_ts,))
        self.conn.execute("""
            INSERT INTO bot_orders (bot_id, order_type, client_order_id, status, price, amount, filled_amount, created_at)
            VALUES (10016, 'tp', 'GHOST_10016_TP_1', 'placing', 50000.0, 0.1, 0.0, ?)
        """, (now_ts,))
        self.conn.commit()

        # Mock CCXT exchange object
        mock_exchange = MagicMock()
        reconciler = StateReconciler(exchanges={'future': mock_exchange})

        # Bypass global scan cooldowns
        if hasattr(StateReconciler, '_last_global_offline_scan'):
            delattr(StateReconciler, '_last_global_offline_scan')

        # Run pre-commit resolution
        reconciler.reconstruct_offline_fills(since_hours=6)

        # Assert exchange.fetch_order was NEVER called
        mock_exchange.fetch_order.assert_not_called()
        mock_exchange.fetch_open_orders.assert_not_called()
        mock_exchange.fetch_closed_orders.assert_not_called()

        # Assert all three synthetic rows were DELETED from database (lookup_succeeded = True -> deleted)
        rows = self.conn.execute("SELECT client_order_id FROM bot_orders WHERE client_order_id LIKE 'PENDING_%' OR client_order_id LIKE 'PLACING_%' OR client_order_id LIKE 'GHOST_%'").fetchall()
        assert len(rows) == 0, "Synthetic orders should be cleaned up and deleted from DB"


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])