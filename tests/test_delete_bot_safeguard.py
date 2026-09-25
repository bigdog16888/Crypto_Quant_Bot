import os
import sqlite3
import pytest
from engine.database import init_db, delete_bot, decommission_bot, close_connection, get_connection

@pytest.fixture
def temp_db(tmp_path):
    db_path = str(tmp_path / "test_delete_safeguard.db")
    
    # Set DB_PATH environment variable or patch engine.database.DB_PATH
    import engine.database
    original_db_path = engine.database.DB_PATH
    engine.database.DB_PATH = db_path
    
    # Remove connection from thread local storage
    if hasattr(engine.database._local, 'connection'):
        del engine.database._local.connection
       
    init_db()
    
    yield db_path
    
    # Restore DB_PATH
    engine.database.DB_PATH = original_db_path
    if hasattr(engine.database._local, 'connection'):
        del engine.database._local.connection

def run_sql(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()

def ensure_tables(db_path):
    """Ensure required tables exist in test database."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_positions (
                bot_id INTEGER,
                pair TEXT,
                side TEXT,
                size REAL,
                entry_price REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                order_type TEXT,
                status TEXT,
                amount REAL,
                price REAL,
                client_order_id TEXT,
                cycle_id INTEGER,
                created_at INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                bot_id INTEGER PRIMARY KEY,
                total_invested REAL DEFAULT 0,
                avg_entry_price REAL DEFAULT 0,
                open_qty REAL DEFAULT 0,
                cycle_id INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fill_claims (
                bot_id INTEGER,
                order_id TEXT,
                PRIMARY KEY (bot_id, order_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cross_reduction_claims (
                source_bot_id INTEGER,
                target_bot_id INTEGER,
                amount REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exchange_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                order_id TEXT,
                fill_ts INTEGER,
                qty REAL,
                price REAL
            )
        """)
        conn.commit()
    finally:
        conn.close()

def test_delete_bot_safeguards(temp_db):
    ensure_tables(temp_db)
    
    # Insert test bot
    run_sql(
        temp_db,
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (99999, 'Test Safeguard Bot', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')"
    )
    
    # Case 1: No active trades, no open orders, no exchange positions.
    # With new canonical pipeline, legacy delete_bot requires human_approved=True to succeed.
    # The wrapper delete_bot calls decommission_bot with human_approved=False -> blocked.
    # Use decommission_bot directly for explicit approval.
    assert delete_bot(99999)[0] is False  # blocked by human_approval gate
    close_connection()
    
    # Clean up for Case 1b - remove the bot first
    run_sql(temp_db, "DELETE FROM bots WHERE id = 99999")
    
    # Case 1b: Explicit human-approved decommission succeeds on clean bot
    run_sql(
        temp_db,
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (99999, 'Test Safeguard Bot', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')"
    )
    success, reason = decommission_bot(99999, human_approved=True)
    assert success is True
    assert "decommissioned cleanly" in reason
    close_connection()
    
    # Case 2: Active trade exists in trades table cache. Deletion should be blocked.
    run_sql(
        temp_db,
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (99998, 'Test Safeguard Bot 2', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')"
    )
    run_sql(
        temp_db,
        "INSERT INTO trades (bot_id, total_invested, avg_entry_price, open_qty, cycle_id) "
        "VALUES (99998, 50.0, 100.0, 0.5, 1)"
    )
    success, reason = decommission_bot(99998, human_approved=True)
    assert success is False
    assert "Active trade" in reason
    close_connection()
    
    # Clean up for Case 3
    run_sql(temp_db, "DELETE FROM trades WHERE bot_id = 99998")
    run_sql(temp_db, "DELETE FROM bots WHERE id = 99998")
    
    # Case 3: Open orders exist. Deletion should be blocked.
    run_sql(
        temp_db,
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (99997, 'Test Safeguard Bot 3', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')"
    )
    run_sql(
        temp_db,
        "INSERT INTO bot_orders (bot_id, order_type, status, amount, price, client_order_id, cycle_id, created_at) "
        "VALUES (99997, 'grid', 'open', 0.1, 95.0, 'CQB_99997_GRID_1', 1, 12345678)"
    )
    success, reason = decommission_bot(99997, human_approved=True)
    assert success is False
    assert "open internal orders" in reason
    close_connection()
    
    # Clean up for Case 4
    run_sql(temp_db, "DELETE FROM bot_orders WHERE bot_id = 99997")
    run_sql(temp_db, "DELETE FROM bots WHERE id = 99997")
    
    # Case 4: Live position on exchange exists (active_positions table).
    # Even if trades and bot_orders tables are clean, deletion should be blocked.
    run_sql(
        temp_db,
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (99996, 'Test Safeguard Bot 4', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')"
    )
    run_sql(
        temp_db,
        "INSERT INTO active_positions (bot_id, pair, side, size, entry_price) "
        "VALUES (99996, 'SOL/USDC:USDC', 'LONG', 0.5, 100.0)"
    )
    
    success, reason = decommission_bot(99996, human_approved=True)
    assert success is False
    assert "Live exchange position" in reason
    close_connection()
    
    # Clean up active_positions, human-approved decommission should succeed now
    run_sql(temp_db, "DELETE FROM active_positions")
    run_sql(temp_db, "DELETE FROM bots WHERE id = 99996")
    run_sql(
        temp_db,
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (99995, 'Test Safeguard Bot 5', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')"
    )
    success, reason = decommission_bot(99995, human_approved=True)
    assert success is True
    close_connection()
