"""
Test for retry-queue cross-ID gap fix (Item 7, P2).

Tests that _fill_credited_by_sibling correctly:
1. Stands down for GTX chase duplicate (step-saturated auto_closed with STEP_SATURATED notes) — POSITIVE
2. Does NOT stand down for TP cascade race guard auto_closed — NEGATIVE (legitimate order)
3. Does NOT stand down for RECONCILE auto_closed — NEGATIVE (legitimate order)
4. Does NOT stand down for auto_closed with no notes — NEGATIVE (legitimate order)
"""
import pytest
import sqlite3
import tempfile
import os
import sys
from contextlib import contextmanager

# Add engine to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))

from ws_event_handlers import _fill_credited_by_sibling


def setup_test_db():
    """Create a test database with bot_orders and fill_claims tables."""
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            order_id TEXT,
            client_order_id TEXT,
            step INTEGER,
            cycle_id INTEGER,
            status TEXT,
            filled_amount REAL DEFAULT 0,
            notes TEXT,
            updated_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE fill_claims (
            bot_id INTEGER NOT NULL,
            order_id TEXT NOT NULL,
            caller TEXT,
            claimed_at INTEGER,
            PRIMARY KEY (bot_id, order_id)
        )
    """)
    conn.commit()
    return conn, db_path


@contextmanager
def mock_get_connection(conn):
    """Context manager to mock engine.database.get_connection."""
    import engine.database
    original = engine.database.get_connection
    engine.database.get_connection = lambda: conn
    try:
        yield
    finally:
        engine.database.get_connection = original


def test_gtx_chase_duplicate_stands_down():
    """POSITIVE: Step-saturated auto_closed with STEP_SATURATED notes -> stand down."""
    conn, db_path = setup_test_db()
    try:
        # Simulate: our order was auto_closed due to step-saturation (sibling got credited)
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, step, cycle_id, status, filled_amount, notes, updated_at)
            VALUES (1001, 'WS_12345', 'CID_12345', 3, 42, 'auto_closed', 0.0, 'STEP_SATURATED:already_credited=1.5,capacity=1.575', 1234567890)
        """)
        # Sibling order was credited
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, step, cycle_id, status, filled_amount, notes, updated_at)
            VALUES (1001, 'WS_67890', 'CID_67890', 3, 42, 'filled', 1.5, '', 1234567890)
        """)
        conn.commit()
        
        with mock_get_connection(conn):
            result = _fill_credited_by_sibling(1001, 'WS_12345', 'CID_12345', 1.0)
            assert result == True, f"Expected True (stand down), got {result}"
    finally:
        conn.close()
        os.unlink(db_path)


def test_tp_cascade_race_guard_does_not_stand_down():
    """NEGATIVE: TP cascade race guard auto_closed -> should NOT stand down (legitimate order may still fill)."""
    conn, db_path = setup_test_db()
    try:
        # Simulate: order auto_closed by TP cascade race guard
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, step, cycle_id, status, filled_amount, notes, updated_at)
            VALUES (1002, 'WS_11111', 'CID_11111', 1, 10, 'auto_closed', 0.0, 'TP_CASCADE_RACE_GUARD: locked at tp_ts=1234567890', 1234567890)
        """)
        conn.commit()
        
        with mock_get_connection(conn):
            result = _fill_credited_by_sibling(1002, 'WS_11111', 'CID_11111', 1.0)
            assert result == False, f"Expected False (escalate), got {result}"
    finally:
        conn.close()
        os.unlink(db_path)


def test_reconcile_auto_closed_does_not_stand_down():
    """NEGATIVE: RECONCILE auto_closed (no notes) -> should NOT stand down."""
    conn, db_path = setup_test_db()
    try:
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, step, cycle_id, status, filled_amount, notes, updated_at)
            VALUES (1003, 'WS_22222', 'CID_22222', 2, 5, 'auto_closed', 0.0, NULL, 1234567890)
        """)
        conn.commit()
        
        with mock_get_connection(conn):
            result = _fill_credited_by_sibling(1003, 'WS_22222', 'CID_22222', 1.0)
            assert result == False, f"Expected False (escalate), got {result}"
    finally:
        conn.close()
        os.unlink(db_path)


def test_bot_reset_auto_closed_does_not_stand_down():
    """NEGATIVE: Bot reset auto_closed (no notes) -> should NOT stand down."""
    conn, db_path = setup_test_db()
    try:
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, step, cycle_id, status, filled_amount, notes, updated_at)
            VALUES (1004, 'WS_33333', 'CID_33333', 1, 1, 'auto_closed', 0.0, '', 1234567890)
        """)
        conn.commit()
        
        with mock_get_connection(conn):
            result = _fill_credited_by_sibling(1004, 'WS_33333', 'CID_33333', 1.0)
            assert result == False, f"Expected False (escalate), got {result}"
    finally:
        conn.close()
        os.unlink(db_path)


def test_fill_claims_still_works():
    """POSITIVE: Existing fill_claims path still works for cross-ID."""
    conn, db_path = setup_test_db()
    try:
        # fill_claims has entry with client_order_id (written by reconciler)
        # Retry queue (WS) calls with exchange_order_id and client_order_id
        conn.execute("""
            INSERT INTO fill_claims (bot_id, order_id, caller, claimed_at)
            VALUES (1005, 'CID_99999', 'reconciler', 1234567890)
        """)
        # Our order row exists and is filled
        conn.execute("""
            INSERT INTO bot_orders (bot_id, order_id, client_order_id, step, cycle_id, status, filled_amount, notes, updated_at)
            VALUES (1005, 'EXCH_99999', 'CID_99999', 1, 1, 'filled', 2.0, '', 1234567890)
        """)
        conn.commit()
        
        with mock_get_connection(conn):
            # Retry queue calls with exchange_order_id (WS) and client_order_id
            result = _fill_credited_by_sibling(1005, 'EXCH_99999', 'CID_99999', 2.0)
            assert result == True, f"Expected True (stand down via fill_claims), got {result}"
    finally:
        conn.close()
        os.unlink(db_path)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])