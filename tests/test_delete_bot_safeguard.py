import os
import sqlite3
import pytest
from engine.database import init_db, delete_bot, close_connection

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

def test_delete_bot_safeguards(temp_db):
    # Insert test bot
    run_sql(
        temp_db,
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (99999, 'Test Safeguard Bot', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')"
    )
    
    # Case 1: No active trades, no open orders, no exchange positions. Deletion should succeed.
    assert delete_bot(99999) is True
    close_connection()
    
    # Re-insert bot for Case 2
    run_sql(
        temp_db,
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (99999, 'Test Safeguard Bot', 'SOL/USDC:USDC', 'SOLUSDC', 'LONG', 1, 'ACTIVE')"
    )
    
    # Case 2: Active trade exists in trades table cache. Deletion should be blocked.
    run_sql(
        temp_db,
        "INSERT INTO trades (bot_id, total_invested, avg_entry_price, open_qty, cycle_id) "
        "VALUES (99999, 50.0, 100.0, 0.5, 1)"
    )
    assert delete_bot(99999) is False
    close_connection()
    
    # Clean up trades table for Case 3
    run_sql(temp_db, "DELETE FROM trades WHERE bot_id = 99999")
    
    # Case 3: Open orders exist. Deletion should be blocked.
    run_sql(
        temp_db,
        "INSERT INTO bot_orders (bot_id, order_type, status, amount, price, client_order_id, cycle_id, created_at) "
        "VALUES (99999, 'grid', 'open', 0.1, 95.0, 'CQB_99999_GRID_1', 1, 12345678)"
    )
    assert delete_bot(99999) is False
    close_connection()
    
    # Clean up bot_orders table for Case 4
    run_sql(temp_db, "DELETE FROM bot_orders WHERE bot_id = 99999")
    
    # Case 4: Live position on exchange exists (active_positions table).
    # Even if trades and bot_orders tables are clean, deletion should be blocked.
    run_sql(
        temp_db,
        "INSERT INTO active_positions (bot_id, pair, side, size, entry_price) "
        "VALUES (99999, 'SOL/USDC:USDC', 'LONG', 0.5, 100.0)"
    )
    
    assert delete_bot(99999) is False
    close_connection()
    
    # Clean up active_positions, deletion should succeed now
    run_sql(temp_db, "DELETE FROM active_positions")
    assert delete_bot(99999) is True
    close_connection()
