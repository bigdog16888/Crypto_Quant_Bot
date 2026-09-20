"""
Test for Finding 2 — Side inference gap in adoption/race guard.

Tests that credit_fill receives and uses the real exchange side parameter
at the two call sites that currently omit it:
1. parity_gates.py:1135 (orphan adoption) - exch_order.get('side')
2. database.py:2173 (race guard) - _detail.get('side')
"""
import pytest
import sqlite3
import tempfile
import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))

from ledger import credit_fill
from database import get_connection, reset_bot_after_tp


@contextmanager
def mock_get_connection(conn):
    import engine.database
    original = engine.database.get_connection
    engine.database.get_connection = lambda: conn
    try:
        yield
    finally:
        engine.database.get_connection = original


def setup_test_db():
    """Create a test database with required tables."""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            name TEXT,
            pair TEXT,
            normalized_pair TEXT,
            direction TEXT,
            strategy_type TEXT,
            config TEXT,
            is_active INTEGER,
            status TEXT,
            bot_type TEXT,
            base_size REAL,
            avg_entry_price REAL DEFAULT 0, target_tp_price REAL DEFAULT 0,
            last_exit_price REAL DEFAULT 0, last_exit_time INTEGER DEFAULT 0,
            basket_start_time INTEGER DEFAULT 0, entry_confirmed INTEGER DEFAULT 0,
            entry_order_id TEXT, tp_order_id TEXT, bot_position_id TEXT,
            close_type TEXT, open_qty REAL DEFAULT 0, created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            step INTEGER,
            order_type TEXT,
            order_id TEXT,
            price REAL,
            amount REAL,
            filled_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            created_at INTEGER,
            client_order_id TEXT,
            updated_at INTEGER,
            notes TEXT,
            wipe_proof_source TEXT,
            wipe_proof_snapshot TEXT,
            cycle_id INTEGER,
            position_side TEXT,
            filled_at INTEGER,
            cumulative_filled REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE exchange_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange_order_id TEXT NOT NULL, client_order_id TEXT,
            symbol TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
            qty REAL NOT NULL, price REAL NOT NULL, fee REAL DEFAULT 0, fee_asset TEXT,
            fill_ts INTEGER NOT NULL, source TEXT NOT NULL, bot_id INTEGER, order_type TEXT,
            step INTEGER, cycle_id INTEGER, raw_json TEXT,
            created_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE(exchange_order_id, fill_ts, qty, price)
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            bot_id INTEGER, action TEXT, symbol TEXT, price REAL, amount REAL, step INTEGER,
            position_side TEXT, cycle_id INTEGER, open_qty REAL, created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE active_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, pair TEXT,
            side TEXT, size REAL, fetched_at INTEGER, source TEXT,
            UNIQUE(bot_id, pair, side)
        )
    """)
    conn.execute("""
        CREATE TABLE equity_snapshots (
            ts INTEGER PRIMARY KEY, equity REAL, cash REAL, cost REAL, unrealized_pnl REAL
        )
    """)
    conn.execute("""
        CREATE TABLE fill_claims (
            bot_id INTEGER, order_id TEXT, caller TEXT, claimed_at INTEGER,
            UNIQUE(bot_id, order_id)
        )
    """)
    conn.commit()
    return conn, db_path


def test_orphan_adoption_uses_exchange_side():
    """POSITIVE: Orphan adoption credits fill with REAL exchange side, not inferred bot direction.
    
    Scenario: Bot configured as LONG, but exchange order was SELL (e.g., hedge child fill,
    manual exchange action, or orphan from opposite-direction position).
    """
    conn, db_path = setup_test_db()
    try:
        # Bot configured as LONG
        conn.execute("""INSERT INTO bots (id, name, pair, normalized_pair, direction, strategy_type, config, is_active, status, bot_type, base_size) VALUES (1001, 'test', 'SOLUSDC', 'SOLUSDC', 'LONG', 'Martingale', '{}', 1, 'Scanning', 'standard', 10.0)""")
        # Order exists in DB
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, order_type, step, cycle_id, amount, status, updated_at)
            VALUES (1001, 'EXCH_12345', 'CQB_1001_1_1', 'adoption', 1, 1, 1.5, 'open', 1234567890)
        """)
        conn.commit()
        
        with mock_get_connection(conn):
            # Call credit_fill with EXPLICIT side='SELL' (opposite to bot's LONG config)
            result = credit_fill(
                bot_id=1001,
                order_id='EXCH_12345',
                cumulative_qty=1.5,
                avg_price=100.0,
                order_type='adoption',
                is_cumulative=True,
                side='SELL',  # REAL exchange side
                caller='orphan_adopt'
            )
            assert result == True, "credit_fill should succeed"
            
            # Verify exchange_fills recorded with SELL side (not inferred BUY)
            row = conn.execute("SELECT side FROM exchange_fills WHERE bot_id=1001 AND exchange_order_id='EXCH_12345'").fetchone()
            assert row is not None, "exchange_fills row should exist"
            assert row[0] == 'SELL', f"exchange_fills.side should be 'SELL' (real side), got '{row[0]}'"
    finally:
        conn.close()
        os.unlink(db_path)


def test_race_guard_uses_exchange_side():
    """POSITIVE: Race guard credits fill with REAL exchange side from fetch_order response.
    
    Scenario: Bot configured as SHORT, but exchange order was BUY (e.g., grid order on SHORT bot
    that filled on the opposite side due to one-way mode netting).
    """
    conn, db_path = setup_test_db()
    try:
        # Bot configured as SHORT
        conn.execute("""INSERT INTO bots (id, name, pair, normalized_pair, direction, strategy_type, config, is_active, status, bot_type, base_size) VALUES (1002, 'test', 'BTCUSDC', 'BTCUSDC', 'SHORT', 'Martingale', '{}', 1, 'Scanning', 'standard', 10.0)""")
        # Order exists in DB
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, order_type, step, cycle_id, amount, status, updated_at)
            VALUES (1002, 'EXCH_67890', 'CQB_1002_1_1', 'grid', 1, 1, 0.5, 'open', 1234567890)
        """)
        conn.commit()
        
        with mock_get_connection(conn):
            # Call credit_fill with EXPLICIT side='BUY' (opposite to bot's SHORT config)
            result = credit_fill(
                bot_id=1002,
                order_id='EXCH_67890',
                cumulative_qty=0.5,
                avg_price=50000.0,
                order_type='grid',
                is_cumulative=True,
                side='BUY',  # REAL exchange side
                caller='race_guard'
            )
            assert result == True, "credit_fill should succeed"
            
            # Verify exchange_fills recorded with BUY side (not inferred SELL)
            row = conn.execute("SELECT side FROM exchange_fills WHERE bot_id=1002 AND exchange_order_id='EXCH_67890'").fetchone()
            assert row is not None, "exchange_fills row should exist"
            assert row[0] == 'BUY', f"exchange_fills.side should be 'BUY' (real side), got '{row[0]}'"
    finally:
        conn.close()
        os.unlink(db_path)


def test_orphan_adoption_without_side_infers_correctly():
    """CONTROL: When side NOT provided, falls back to bot direction inference (backward compat)."""
    conn, db_path = setup_test_db()
    try:
        conn.execute("""INSERT INTO bots (id, name, pair, normalized_pair, direction, strategy_type, config, is_active, status, bot_type, base_size) VALUES (1003, 'test', 'ETHUSDC', 'ETHUSDC', 'LONG', 'Martingale', '{}', 1, 'Scanning', 'standard', 10.0)""")
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, order_type, step, cycle_id, amount, status, updated_at)
            VALUES (1003, 'EXCH_11111', 'CQB_1003_1_1', 'adoption', 1, 1, 1.0, 'open', 1234567890)
        """)
        conn.commit()
        
        with mock_get_connection(conn):
            # Call WITHOUT side parameter (current behavior)
            result = credit_fill(
                bot_id=1003,
                order_id='EXCH_11111',
                cumulative_qty=1.0,
                avg_price=2000.0,
                order_type='adoption',
                is_cumulative=True,
                caller='orphan_adopt'
            )
            assert result == True
            
            # Should infer BUY from LONG bot direction
            row = conn.execute("SELECT side FROM exchange_fills WHERE bot_id=1003 AND exchange_order_id='EXCH_11111'").fetchone()
            assert row is not None
            assert row[0] == 'BUY', f"Should infer BUY for LONG bot, got '{row[0]}'"
    finally:
        conn.close()
        os.unlink(db_path)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])