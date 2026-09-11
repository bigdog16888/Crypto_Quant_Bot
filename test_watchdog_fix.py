#!/usr/bin/env python3
"""
Quick verification test for the O-10 watchdog fix.
Tests the grace window logic using bot_orders instead of trades.open_qty.
"""
import sqlite3
import time
from engine.hedge_watchdog import verify_hedge_engagement, _child_hedge_qty_from_orders

def make_test_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            direction TEXT,
            status TEXT,
            hedge_child_bot_id INTEGER,
            hedge_trigger_step INTEGER,
            last_error TEXT,
            last_error_time REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY,
            open_qty REAL
        )"""
    )
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
    return conn

def seed_bot(conn, bid, direction="LONG", status="IN TRADE",
             hedge_child=None, hedge_trigger=7, last_error=None, last_error_time=None):
    conn.execute(
        "INSERT INTO bots (id, direction, status, hedge_child_bot_id, hedge_trigger_step, last_error, last_error_time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bid, direction, status, hedge_child, hedge_trigger, last_error, last_error_time),
    )
    conn.commit()

def seed_trade(conn, bid, open_qty):
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, open_qty) VALUES (?, ?)",
        (bid, open_qty),
    )
    conn.commit()

def seed_order(conn, bot_id, step, order_type, status, amount, filled_amount=0.0, 
               created_at=None, filled_at=0, cycle_id=1):
    now = int(time.time())
    conn.execute(
        """INSERT INTO bot_orders 
        (bot_id, step, order_type, order_id, price, amount, filled_amount, status, 
         created_at, client_order_id, updated_at, notes, wipe_proof_source, 
         wipe_proof_snapshot, cycle_id, position_side, filled_at, cumulative_filled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (bot_id, step, order_type, f"test_{order_type}_{step}", 100.0, amount, filled_amount, status,
         created_at or now, f"cli_{order_type}_{step}", now, None, None, None, cycle_id, "BOTH", filled_at, filled_amount),
    )
    conn.commit()

print("=" * 60)
print("TEST 1: Legacy fallback - trades.open_qty works")
print("=" * 60)
conn = make_test_conn()
seed_bot(conn, 200, direction="LONG", hedge_child=100, hedge_trigger=7, status="IN TRADE")
seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
seed_trade(conn, 100, 0.5)

# No step/cycle passed - should use legacy fallback
res = verify_hedge_engagement(200, "LONG", conn, config={"HEDGE_MIN_QTY": 0.001})
print(f"Result: {res}")
assert res["engaged"] == True, "Should be engaged via legacy fallback"
assert res["freeze_parent"] == False
print("PASS: Legacy fallback works\n")

print("=" * 60)
print("TEST 2: New logic - bot_orders with sufficient hedge_qty")
print("=" * 60)
conn = make_test_conn()
seed_bot(conn, 200, direction="LONG", hedge_child=100, hedge_trigger=7, status="IN TRADE")
seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
seed_trade(conn, 100, 0.0)  # trades shows 0

# Add filled entry order in bot_orders (signal path source)
seed_order(conn, 100, step=1, order_type="entry", status="filled", amount=0.5, filled_amount=0.5, cycle_id=1)

# Pass child_step and parent_cycle_id - should use new logic
res = verify_hedge_engagement(200, "LONG", conn, 
                              config={"HEDGE_MIN_QTY": 0.001, "child_step": 1, "parent_cycle_id": 1})
print(f"Result: {res}")
assert res["engaged"] == True, "Should be engaged via bot_orders"
assert res["freeze_parent"] == False
print("PASS: New logic with bot_orders works\n")

print("=" * 60)
print("TEST 3: Grace window - pending order within engage_timeout")
print("=" * 60)
conn = make_test_conn()
seed_bot(conn, 200, direction="LONG", hedge_child=100, hedge_trigger=7, status="IN TRADE")
seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
seed_trade(conn, 100, 0.0)

# Add pending entry order (placed but not filled yet)
now = int(time.time())
seed_order(conn, 100, step=1, order_type="entry", status="open", amount=0.5, filled_amount=0.0, 
           created_at=now, filled_at=0, cycle_id=1)

# Should be engaged due to grace window (engage_timeout default 30s)
res = verify_hedge_engagement(200, "LONG", conn,
                              config={"HEDGE_MIN_QTY": 0.001, "HEDGE_ENGAGE_TIMEOUT": 30, 
                                      "child_step": 1, "parent_cycle_id": 1})
print(f"Result: {res}")
assert res["engaged"] == True, "Should be engaged within grace window"
assert res["freeze_parent"] == False
print("PASS: Grace window works for pending order\n")

print("=" * 60)
print("TEST 4: Grace window expired - pending order past engage_timeout")
print("=" * 60)
conn = make_test_conn()
seed_bot(conn, 200, direction="LONG", hedge_child=100, hedge_trigger=7, status="IN TRADE")
seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
seed_trade(conn, 100, 0.0)

# Add OLD pending entry order (placed > engage_timeout ago)
old_time = int(time.time()) - 60  # 60 seconds ago
seed_order(conn, 100, step=1, order_type="entry", status="open", amount=0.5, filled_amount=0.0,
           created_at=old_time, filled_at=0, cycle_id=1)

# Should NOT be engaged - grace window expired
res = verify_hedge_engagement(200, "LONG", conn,
                              config={"HEDGE_MIN_QTY": 0.001, "HEDGE_ENGAGE_TIMEOUT": 30,
                                      "child_step": 1, "parent_cycle_id": 1})
print(f"Result: {res}")
assert res["engaged"] == False, "Should NOT be engaged - grace window expired"
assert res["freeze_parent"] == True
assert res["reason"] == "child_not_offsetting"
print("PASS: Grace window correctly expires\n")

print("=" * 60)
print("TEST 5: Recent fill within grace window after expired order")
print("=" * 60)
conn = make_test_conn()
seed_bot(conn, 200, direction="LONG", hedge_child=100, hedge_trigger=7, status="IN TRADE")
seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
seed_trade(conn, 100, 0.0)

# Old pending order
old_time = int(time.time()) - 60
seed_order(conn, 100, step=1, order_type="entry", status="open", amount=0.5, filled_amount=0.0,
           created_at=old_time, filled_at=0, cycle_id=1)

# Recent fill credited
recent_fill = int(time.time()) - 10  # 10 seconds ago
seed_order(conn, 100, step=1, order_type="entry", status="filled", amount=0.5, filled_amount=0.5,
           created_at=recent_fill, filled_at=recent_fill, cycle_id=1)

# Should be engaged due to recent fill within grace window
res = verify_hedge_engagement(200, "LONG", conn,
                              config={"HEDGE_MIN_QTY": 0.001, "HEDGE_ENGAGE_TIMEOUT": 30,
                                      "child_step": 1, "parent_cycle_id": 1})
print(f"Result: {res}")
assert res["engaged"] == True, "Should be engaged due to recent fill within grace window"
assert res["freeze_parent"] == False
print("PASS: Recent fill within grace window works\n")

print("=" * 60)
print("TEST 6: _child_hedge_qty_from_orders aggregation matches signal path")
print("=" * 60)
conn = make_test_conn()
seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
seed_trade(conn, 100, 0.0)

# Add mixed orders matching signal path logic:
# - 1 open entry order (amount=0.3, not filled)
# - 1 filled entry order (amount=0.2, filled_amount=0.2)
# - 1 open grid order (amount=0.1)
# - 1 cancelled order (should be excluded)
now = int(time.time())
seed_order(conn, 100, step=1, order_type="entry", status="open", amount=0.3, filled_amount=0.0, created_at=now, cycle_id=1)
seed_order(conn, 100, step=1, order_type="entry", status="filled", amount=0.2, filled_amount=0.2, created_at=now, filled_at=now, cycle_id=1)
seed_order(conn, 100, step=1, order_type="grid", status="open", amount=0.1, filled_amount=0.0, created_at=now, cycle_id=1)
seed_order(conn, 100, step=1, order_type="entry", status="cancelled", amount=0.5, filled_amount=0.0, created_at=now, cycle_id=1)

# Expected: open entry (0.3) + filled entry (0.2) + open grid (0.1) = 0.6
hedge_qty, has_entry, oldest_created, latest_filled = _child_hedge_qty_from_orders(100, 1, 1, conn)
print(f"hedge_qty={hedge_qty}, has_entry={has_entry}, oldest_created={oldest_created}, latest_filled={latest_filled}")
assert abs(hedge_qty - 0.6) < 0.001, f"Expected 0.6, got {hedge_qty}"
assert has_entry == True
assert oldest_created is not None
assert latest_filled is not None
print("PASS: Aggregation matches signal path logic\n")

print("=" * 60)
print("ALL TESTS PASSED!")
print("=" * 60)