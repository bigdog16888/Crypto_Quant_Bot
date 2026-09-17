"""
tests/test_offline_fill_reconciliation.py

Unit test for offline fill reconciliation at startup.
Tests that reconstruct_offline_fills correctly credits fills where bot_orders
has status='filled' but trades.open_qty hasn't been updated.
"""
import sqlite3
import time
import pytest
from unittest.mock import MagicMock, patch, Mock
from engine.reconciler import StateReconciler
from engine.ledger import credit_fill, seal_trade_state
from engine.database import get_connection
from engine.database import get_connection


@pytest.fixture
def temp_db(tmp_path):
    """Create a self-contained test database matching production schema."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    # Production-matching schema (critical: trades has NO 'direction' column)
    conn.executescript("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            name TEXT, pair TEXT, normalized_pair TEXT,
            direction TEXT, is_active INTEGER DEFAULT 1,
            status TEXT DEFAULT 'Scanning', config_json TEXT,
            hedge_child_bot_id INTEGER,
            cascade_started_at INTEGER
        );
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
            entry_order_id TEXT, tp_order_id TEXT, bot_position_id TEXT,
            close_type TEXT DEFAULT NULL,
            cycle_id INTEGER DEFAULT 1,
            cycle_phase TEXT DEFAULT 'ACTIVE',
            open_qty REAL DEFAULT 0,
            wipe_wall_ts INTEGER DEFAULT 0,
            position_side TEXT DEFAULT 'BOTH',
            cycle_start_time INTEGER DEFAULT 0,
            FOREIGN KEY (bot_id) REFERENCES bots (id)
        );
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER, step INTEGER, order_type TEXT,
            order_id TEXT, price REAL, amount REAL, filled_amount REAL,
            status TEXT, position_side TEXT, created_at INTEGER, updated_at INTEGER,
            client_order_id TEXT, cycle_id INTEGER, filled_at INTEGER,
            cumulative_filled REAL DEFAULT 0,
            FOREIGN KEY (bot_id) REFERENCES bots (id)
        );
        CREATE TABLE active_positions (
            bot_id INTEGER, pair TEXT, side TEXT, size REAL,
            entry_price REAL, last_checked INTEGER,
            PRIMARY KEY (bot_id, pair, side)
        );
        CREATE TABLE fill_claims (
            bot_id INTEGER, order_id TEXT, fill_ts INTEGER,
            qty REAL, PRIMARY KEY (bot_id, order_id, fill_ts)
        );
        CREATE TABLE exchange_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange_order_id TEXT, client_order_id TEXT,
            symbol TEXT, side TEXT, qty REAL, price REAL,
            fee REAL, fee_asset TEXT, fill_ts INTEGER,
            source TEXT, bot_id INTEGER, order_type TEXT,
            step INTEGER, cycle_id INTEGER, raw_json TEXT,
            created_at INTEGER
        );

        CREATE TABLE manual_whitelists (
            id INTEGER PRIMARY KEY,
            pair TEXT,
            side TEXT,
            qty REAL
        );
        CREATE TABLE schema_migrations (
            version TEXT PRIMARY KEY, applied_at INTEGER, description TEXT
        );
    """)

    # Seed bot 10018 (SUI LONG) with trades record
    conn.execute(
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', 1, 'IN TRADE')"
    )
    conn.execute(
        "INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, "
        "target_tp_price, last_exit_price, last_exit_time, basket_start_time, "
        "entry_confirmed, entry_order_id, tp_order_id, bot_position_id, close_type, "
        "cycle_id, cycle_phase, open_qty, wipe_wall_ts, position_side, cycle_start_time) "
        "VALUES (10018, 4, 0.185, 0.37, 0.40, 0.0, 0, 0, 1, 'E1', 'TP1', 'adoption_add', "
        "'filled', 155, 'PARTIAL_CLOSE_PENDING', 0.5, 0, 'LONG', 0)"
    )
    # Pre-seed a FILLED bot_orders row that exchange confirms but ledger hasn't credited
    conn.execute(
        "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, "
        "filled_amount, status, created_at, updated_at, client_order_id, cycle_id, filled_at, cumulative_filled) "
        "VALUES (10018, 4, 'tp', '6952644362', 0.40, 153.6, 0, 'open', "
        "?, ?, 'CQB_10018_TP3_155_2', 155, ?, 153.6)",
        (int(time.time()), int(time.time()), int(time.time()))
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mock_exchange():
    """Mock exchange that returns the fill for order 6952644362."""
    ex = MagicMock()
    # fetch_my_trades returns the recent fill
    ex.fetch_my_trades.return_value = [{
        'info': {'orderId': '6952644362', 'clientOrderId': 'CQB_10018_TP3_155_2'},
        'datetime': '2026-09-16T09:55:43.909Z',
        'side': 'sell',  # TP for LONG = sell
        'amount': 153.6,
        'price': 0.40,
        'order': '6952644362',
        'id': '4276675200'
    }]
    # fetch_closed_orders for CID matching
    ex.fetch_closed_orders.return_value = [{
        'id': '6952644362',
        'clientOrderId': 'CQB_10018_TP3_155_2',
        'status': 'filled',
        'filled': 153.6,
        'amount': 153.6,
        'average': 0.40,
        'price': 0.40,
        'side': 'SELL',
        'timestamp': int(time.time() * 1000),
        'info': {'clientOrderId': 'CQB_10018_TP3_155_2', 'orderId': '6952644362'}
    }]
    # fetch_order for direct CID lookup
    ex.fetch_order.return_value = {
        'id': '6952644362',
        'clientOrderId': 'CQB_10018_TP3_155_2',
        'status': 'filled',
        'filled': 153.6,
        'amount': 153.6,
        'average': 0.40,
        'price': 0.40,
        'side': 'SELL',
        'timestamp': int(time.time() * 1000),
        'info': {'clientOrderId': 'CQB_10018_TP3_155_2', 'orderId': '6952644362'}
    }
    return ex


def test_reconstruct_offline_fills_credits_missed_tp(temp_db, mock_exchange):
    """
    RED -> GREEN: reconstruct_offline_fills should credit the missed TP fill
    and propagate to trades.open_qty and active_positions.
    """
    # Set up reconciler with mocked exchange
    reconciler = StateReconciler(exchanges={'future': mock_exchange})
    # Debug: directly test credit_fill\n    from engine.ledger import credit_fill\n    result = credit_fill(\n        bot_id=10018,\n        order_id="6952644362",\n        cumulative_qty=153.6,\n        avg_price=0.40,\n        order_type="tp",\n        is_cumulative=True,\n        caller="test-debug",\n        side="SELL"\)\n    print(f"credit_fill result: {result}")\n    
    # Point DB_PATH to our temp database
    import engine.database as engine_database
    original_db_path = engine_database.DB_PATH
    engine_database.DB_PATH = temp_db
    
    try:
        # Run the offline fill reconciliation
        stats = reconciler.reconstruct_offline_fills(since_hours=6, pair_filter='SUIUSDC')
        
        # Verify the fill was credited
        conn = get_connection()
        # Check trades.open_qty was reduced (TP credited)
        trade = conn.execute("SELECT open_qty, cycle_phase FROM trades WHERE bot_id = 10018").fetchone()
        assert trade is not None, "Trades record missing"
        open_qty, cycle_phase = trade
        
        # The 153.6 TP fill should have been credited, reducing open_qty from 0.5
        # (original had 0.5 open_qty, but 153.6 TP fill credits against it)
        # Since the TP was for the full position, open_qty should be 0
        # and cycle_phase should advance
        assert open_qty == 0.0, f"Expected open_qty=0 after TP credit, got {open_qty}"
        
        # Verify active_positions updated (should be removed for flat position)
        ap = conn.execute("SELECT size FROM active_positions WHERE bot_id = 10018 AND pair = 'SUI/USDC:USDC'").fetchone()
        # Position should be flat (0 or row removed)
        if ap:
            assert ap[0] == 0.0, f"Expected active_positions size=0, got {ap[0]}"
        
        # Verify exchange_fills recorded the fill
        ef = conn.execute("SELECT qty FROM exchange_fills WHERE exchange_order_id = '6952644362'").fetchone()
        print(f'DEBUG: exchange_fills rows = {conn.execute("SELECT * FROM exchange_fills").fetchall()}')
        assert ef is not None, "Fill not recorded in exchange_fills"
        assert ef[0] == 153.6, f"Expected fill qty 153.6, got {ef[0]}"
        
        print(f"SUCCESS: Stats={stats}, open_qty={open_qty}, cycle_phase={cycle_phase}")
        
    finally:
        engine_database.DB_PATH = original_db_path


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "-s"]))