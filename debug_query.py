#!/usr/bin/env python3
"""
Debug the bot_orders query
"""
import sqlite3
import time
from engine.hedge_watchdog import _child_hedge_qty_from_orders

conn = sqlite3.connect(":memory:")
conn.execute(
    """CREATE TABLE bot_orders (
        id INTEGER PRIMARY KEY,
        bot_id INTEGER,
        step INTEGER,
        order_type TEXT,
        order_id TEXT,
        price REAL,
        amount REAL,
        filled_amount REAL,
        status TEXT,
        created_at INTEGER,
        client_order_id TEXT,
        updated_at INTEGER,
        notes TEXT,
        wipe_proof_source TEXT,
        wipe_proof_snapshot TEXT,
        cycle_id INTEGER,
        position_side TEXT,
        filled_at INTEGER,
        cumulative_filled REAL
    )"""
)

now = int(time.time())

# Add filled entry order
conn.execute(
    """INSERT INTO bot_orders 
    (bot_id, step, order_type, order_id, price, amount, filled_amount, status, 
     created_at, client_order_id, updated_at, notes, wipe_proof_source, 
     wipe_proof_snapshot, cycle_id, position_side, filled_at, cumulative_filled)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
    (100, 1, 'entry', 'test_entry_1', 100.0, 0.5, 0.5, 'filled',
     now, 'cli_entry_1', now, None, None, None, 1, 'BOTH', now, 0.5),
)
conn.commit()

# Debug the query directly
row = conn.execute(
    """
    SELECT 
        COALESCE(SUM(
            CASE 
                WHEN status IN ('open', 'new', 'placing', 'cancelling') THEN amount
                ELSE filled_amount
            END
        ), 0) AS hedge_qty,
        MAX(CASE WHEN order_type IN ('entry', 'grid') THEN 1 ELSE 0 END) AS has_entry,
        MIN(CASE WHEN order_type IN ('entry', 'grid') AND status IN ('open', 'new', 'placing', 'cancelling', 'filled', 'partially_filled') THEN created_at END) AS oldest_entry_created,
        MAX(CASE WHEN order_type IN ('entry', 'grid') AND filled_at > 0 THEN filled_at END) AS latest_filled_at
    FROM bot_orders
    WHERE bot_id = ? AND step = ? AND cycle_id = ?
      AND order_type IN ('entry', 'grid')
      AND status NOT IN ('cancelled', 'failed', 'reset_cleared', 'auto_closed', 'rejected')
    """,
    (100, 1, 1)
).fetchone()
print(f"Direct query result: {row}")

hedge_qty, has_entry, oldest_created, latest_filled = _child_hedge_qty_from_orders(100, 1, 1, conn)
print(f"Function result: hedge_qty={hedge_qty}, has_entry={has_entry}, oldest_created={oldest_created}, latest_filled={latest_filled}")