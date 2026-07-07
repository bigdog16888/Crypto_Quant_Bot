import pytest
import sqlite3
import time
from unittest.mock import MagicMock
from engine.oneway_netting import get_typical_position_size, detect_unowned_exchange_positions

@pytest.fixture
def temp_db():
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    # Create necessary tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY,
            name TEXT,
            direction TEXT,
            bot_type TEXT,
            is_active INTEGER,
            normalized_pair TEXT,
            pair TEXT,
            status TEXT,
            notes TEXT
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            bot_id INTEGER PRIMARY KEY,
            cycle_id INTEGER,
            open_qty REAL,
            cycle_phase TEXT,
            wipe_wall_ts INTEGER,
            position_side TEXT
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            order_type TEXT,
            status TEXT,
            amount REAL,
            filled_amount REAL,
            price REAL,
            step INTEGER,
            cycle_id INTEGER,
            client_order_id TEXT,
            created_at INTEGER
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS unowned_position_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            pair TEXT NOT NULL,
            normalized_pair TEXT NOT NULL,
            exchange_qty REAL NOT NULL,
            db_qty REAL NOT NULL,
            detected_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_review',
            notes TEXT
        );
    """)
    yield conn
    conn.close()

def test_get_typical_position_size(temp_db):
    conn = temp_db
    bot_id = 10022
    
    # Empty history
    assert get_typical_position_size(conn, bot_id) == 0.0
    
    # Insert entry fills
    conn.execute("""
        INSERT INTO bot_orders (bot_id, order_type, status, amount, filled_amount, price, step, cycle_id, client_order_id, created_at)
        VALUES (?, 'entry', 'filled', 0.02, 0.02, 60000, 1, 1, 'cid1', ?)
    """, (bot_id, int(time.time())))
    conn.execute("""
        INSERT INTO bot_orders (bot_id, order_type, status, amount, filled_amount, price, step, cycle_id, client_order_id, created_at)
        VALUES (?, 'grid', 'filled', 0.04, 0.04, 60000, 2, 1, 'cid2', ?)
    """, (bot_id, int(time.time())))
    
    # Verify average is (0.02 + 0.04) / 2 = 0.03
    assert get_typical_position_size(conn, bot_id) == pytest.approx(0.03)

def test_detect_unowned_exchange_positions_match(temp_db):
    conn = temp_db
    
    # Insert active bot
    conn.execute("INSERT INTO bots VALUES (10022, 'short btc', 'SHORT', 'standard', 1, 'BTCUSDC', 'BTC/USDC:USDC', 'Scanning', NULL)")
    conn.execute("INSERT INTO trades VALUES (10022, 51, 0.0, 'IDLE', 0, 'BOTH')")
    
    # Mock exchange
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': -0.028, 'side': 'SHORT', 'entryPrice': 60000.0}
    ]
    
    # Case A: Bot has no order history (typical_size = 0.0), drift is -0.028
    # Since typical_size is 0.0, it should match any size!
    detect_unowned_exchange_positions(conn, mock_exchange)
    
    alert = conn.execute("SELECT bot_id, pair, exchange_qty, db_qty, status FROM unowned_position_alerts").fetchone()
    assert alert is not None
    assert alert[0] == 10022
    assert alert[1] == 'BTC/USDC:USDC'
    assert alert[2] == -0.028
    assert alert[3] == 0.0
    assert alert[4] == 'pending_review'

def test_detect_unowned_exchange_positions_no_match_fallback(temp_db):
    conn = temp_db
    
    # Insert active bot
    conn.execute("INSERT INTO bots VALUES (10022, 'short btc', 'SHORT', 'standard', 1, 'BTCUSDC', 'BTC/USDC:USDC', 'Scanning', NULL)")
    conn.execute("INSERT INTO trades VALUES (10022, 51, 0.0, 'IDLE', 0, 'BOTH')")
    
    # Insert filled order history to establish a typical size of 0.01
    conn.execute("""
        INSERT INTO bot_orders (bot_id, order_type, status, amount, filled_amount, price, step, cycle_id, client_order_id, created_at)
        VALUES (10022, 'entry', 'filled', 0.01, 0.01, 60000, 1, 51, 'cid1', 1000)
    """)
    
    # Mock exchange with an unowned position of -0.05 (mismatches typical size 0.01)
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': -0.05, 'side': 'SHORT', 'entryPrice': 60000.0}
    ]
    
    # Run detector
    detect_unowned_exchange_positions(conn, mock_exchange)
    
    # Should fall back to NULL bot_id because typical_size=0.01 mismatches drift=0.05
    alert = conn.execute("SELECT bot_id, pair, exchange_qty, db_qty, status, notes FROM unowned_position_alerts").fetchone()
    assert alert is not None
    assert alert[0] is None
    assert alert[1] == 'BTC/USDC:USDC'
    assert alert[2] == -0.05
    assert "no matching flat bot could be found" in alert[5]

def test_exact_tolerance_gap_triggers_alert(temp_db):
    conn = temp_db
    from engine.parity_gates import qty_tolerance
    tol = qty_tolerance()
    
    # Insert active bot
    conn.execute("INSERT INTO bots VALUES (10022, 'short btc', 'SHORT', 'standard', 1, 'BTCUSDC', 'BTC/USDC:USDC', 'Scanning', NULL)")
    conn.execute("INSERT INTO trades VALUES (10022, 51, 0.0, 'IDLE', 0, 'BOTH')")
    
    # Mock exchange with an unowned position equal to exactly the negative tolerance (e.g., -0.002)
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {'symbol': 'BTC/USDC:USDC', 'contracts': -tol, 'side': 'SHORT', 'entryPrice': 60000.0}
    ]
    
    # Run detector
    detect_unowned_exchange_positions(conn, mock_exchange)
    
    # Assert that an alert was generated
    alert = conn.execute("SELECT bot_id, pair, exchange_qty, db_qty, status FROM unowned_position_alerts").fetchone()
    assert alert is not None
    assert alert[0] == 10022
    assert alert[1] == 'BTC/USDC:USDC'
    assert alert[2] == -tol

