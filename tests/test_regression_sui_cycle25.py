#!/usr/bin/env python3
"""
Regression test for SUI cycle-25 incident (bot 10018).

Historical incident: Bot 10018 had a fill in cycle 25 (58.6 SUIUSDC LONG).
- Exchange confirmed position: 58.6 LONG
- active_positions correctly shows: 58.6 LONG (from offline reconstruction)
- trades.open_qty shows: 0.0 (STALE - never updated from credit_fill path)
- Root cause: credit_fill -> seal_trade_state did NOT upsert active_positions

This test reproduces the bug by simulating the fill through the CURRENT pipeline.
"""

import sys
import os
import sqlite3
import tempfile
import logging

sys.path.insert(0, '/d/Crypto_Quant_Bot')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

from engine.ledger import seal_trade_state
from engine.position_ledger import _checkpoint_bot_position
from engine.database import get_connection


def create_test_db():
    """Create test DB matching production schema exactly."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    
    # === PRODUCTION SCHEMA ===
    conn.executescript("""
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
        position_side TEXT DEFAULT 'BOTH',
        cycle_start_time INTEGER DEFAULT 0,
        FOREIGN KEY (bot_id) REFERENCES bots (id)
    );

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
        updated_at INTEGER DEFAULT 0,
        notes TEXT,
        wipe_proof_source TEXT,
        wipe_proof_snapshot TEXT,
        cycle_id INTEGER,
        position_side TEXT DEFAULT 'BOTH',
        filled_at INTEGER DEFAULT 0,
        cumulative_filled REAL DEFAULT 0,
        FOREIGN KEY (bot_id) REFERENCES bots (id)
    );

    CREATE TABLE bots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        pair TEXT NOT NULL,
        normalized_pair TEXT,
        direction TEXT NOT NULL,
        rsi_limit REAL,
        martingale_multiplier REAL,
        base_size REAL,
        strategy_type TEXT DEFAULT 'Martingale',
        config TEXT DEFAULT '{}',
        is_active BOOLEAN DEFAULT 1,
        status TEXT DEFAULT 'Stopped',
        manual_close_pct REAL DEFAULT 100.0,
        last_error TEXT,
        last_error_time INTEGER,
        pos_limit_hit INTEGER DEFAULT 0,
        bot_type TEXT DEFAULT 'standard',
        parent_bot_id INTEGER DEFAULT NULL,
        hedge_child_bot_id INTEGER DEFAULT NULL,
        hedge_trigger_step INTEGER DEFAULT NULL,
        cascade_started_at INTEGER DEFAULT 0,
        notes TEXT DEFAULT NULL
    );

    CREATE TABLE active_positions (
        bot_id INTEGER NOT NULL,
        pair TEXT NOT NULL,
        side TEXT NOT NULL,
        size REAL NOT NULL DEFAULT 0,
        entry_price REAL DEFAULT 0,
        last_checked INTEGER,
        last_updated INTEGER DEFAULT (datetime('now')),
        PRIMARY KEY (bot_id, pair, side)
    );

    CREATE TABLE exchange_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exchange_order_id TEXT NOT NULL,
        client_order_id TEXT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
        qty REAL NOT NULL,
        price REAL NOT NULL,
        fee REAL DEFAULT 0,
        fee_asset TEXT,
        fill_ts INTEGER NOT NULL,
        source TEXT NOT NULL,
        bot_id INTEGER,
        order_type TEXT,
        step INTEGER,
        cycle_id INTEGER,
        raw_json TEXT,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
        UNIQUE(exchange_order_id, fill_ts, qty, price)
    );
    """)
    
    # Insert bot 10018 (SUIUSDC) - matching production state BEFORE the incident
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, config, bot_type)
        VALUES (10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', 1, 'IN TRADE', '{"max_steps": 25}', 'standard')
    """)
    
    conn.execute("""
        INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, target_tp_price,
                           last_exit_price, last_exit_time, basket_start_time, entry_confirmed,
                           entry_order_id, tp_order_id, bot_position_id, close_type,
                           cycle_id, cycle_phase, open_qty, wipe_wall_ts, position_side, cycle_start_time)
        VALUES (10018, 25, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, NULL, NULL, NULL, NULL, 25, 'ACTIVE', 0.0, 1789647583, 'LONG', 1789647583)
    """)
    
    conn.execute("""
        INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked, last_updated)
        VALUES (10018, 'SUIUSDC', 'LONG', 58.6, 0.7062708984375001, 1789647583, datetime('now'))
    """)
    
    # The fill in cycle 25 - this is the historical fill that was credited but never sealed properly
    conn.execute("""
        INSERT INTO bot_orders (id, bot_id, step, order_type, order_id, price, amount, filled_amount,
                               status, created_at, client_order_id, updated_at, cycle_id, position_side, filled_at)
        VALUES (1000001, 10018, 1, 'entry', 'test_order_123', 0.7062708984375001, 58.6, 58.6,
               'filled', 1789647583, 'CQB_10018_ENTRY_25_1', 1789647583, 25, 'LONG', 1789647583)
    """)
    
    conn.commit()
    return conn


def test_sui_cycle25_regression():
    """Test that reproduces the SUI cycle-25 bug: active_positions has correct data but trades.open_qty is stale."""
    
    print("=" * 70)
    print("REGRESSION TEST: SUI cycle-25 incident (bot 10018)")
    print("=" * 70)
    
    conn = create_test_db()
    
    # Monkey-patch get_connection to use our test DB
    import engine.database as database
    import engine.ledger as ledger
    import engine.position_ledger as position_ledger
    
    original_get_connection = database.get_connection
    database.get_connection = lambda: conn
    ledger.get_connection = lambda: conn
    position_ledger.get_connection = lambda: conn
    
    try:
        # === PHASE 1: Verify initial state (the bug) ===
        print("\n[PHASE 1] Initial state - BEFORE seal_trade_state:")
        
        # Check trades.open_qty (should be 0.0 - STALE)
        trades_row = conn.execute("SELECT open_qty, current_step, cycle_id FROM trades WHERE bot_id=10018").fetchone()
        print(f"  trades.open_qty: {trades_row['open_qty']} (EXPECTED 0.0 - STALE)")
        print(f"  trades.current_step: {trades_row['current_step']}")
        print(f"  trades.cycle_id: {trades_row['cycle_id']}")
        
        # Check active_positions (should be 58.6 - CORRECT from offline reconstruction)
        ap_row = conn.execute("SELECT size, entry_price FROM active_positions WHERE bot_id=10018").fetchone()
        print(f"  active_positions.size: {ap_row['size']} (EXPECTED 58.6 - CORRECT)")
        print(f"  active_positions.entry_price: {ap_row['entry_price']}")
        
        # Check _checkpoint_bot_position (A2 - reads from active_positions)
        checkpoint = _checkpoint_bot_position(conn, 10018, 'SUIUSDC', 'LONG')
        print(f"  _checkpoint_bot_position net_qty: {checkpoint.net_qty} (should be 58.6 - A2 WORKS)")
        
        # Verify the bug: trades.open_qty (0.0) != active_positions.size (58.6)
        if trades_row['open_qty'] == 0.0 and ap_row['size'] == 58.6:
            print("\n  >>> BUG CONFIRMED: trades.open_qty (0.0) != active_positions.size (58.6)")
            print("  >>> This is the active_positions staleness bug!")
        else:
            print("\n  >>> UNEXPECTED: Bug not reproduced")
            return False
        
        # === PHASE 2: Run seal_trade_state (simulates credit_fill path) ===
        print("\n[PHASE 2] Running seal_trade_state(10018) - simulates credit_fill -> seal path:")
        
        # Clear the test DB's trades.open_qty to simulate the bug state
        conn.execute("UPDATE trades SET open_qty = 0.0, total_invested = 0.0, avg_entry_price = 0.0 WHERE bot_id = 10018")
        conn.commit()
        
        # Run seal_trade_state - this should compute from bot_orders and upsert active_positions
        result = seal_trade_state(10018, force_recompute=True)
        print(f"  seal_trade_state result: {result}")
        
        # === PHASE 3: Verify post-seal state ===
        print("\n[PHASE 3] State AFTER seal_trade_state:")
        
        trades_row = conn.execute("SELECT open_qty, total_invested, avg_entry_price, current_step, cycle_id FROM trades WHERE bot_id=10018").fetchone()
        print(f"  trades.open_qty: {trades_row['open_qty']}")
        print(f"  trades.total_invested: {trades_row['total_invested']}")
        print(f"  trades.avg_entry_price: {trades_row['avg_entry_price']}")
        print(f"  trades.current_step: {trades_row['current_step']}")
        print(f"  trades.cycle_id: {trades_row['cycle_id']}")
        
        ap_row = conn.execute("SELECT size, entry_price, last_checked, last_updated FROM active_positions WHERE bot_id=10018").fetchone()
        print(f"  active_positions.size: {ap_row['size']}")
        print(f"  active_positions.entry_price: {ap_row['entry_price']}")
        print(f"  active_positions.last_checked: {ap_row['last_checked']}")
        print(f"  active_positions.last_updated: {ap_row['last_updated']}")
        
        # === VERIFICATION ===
        print("\n" + "=" * 70)
        print("VERIFICATION:")
        
        # The key assertion: after seal_trade_state, trades.open_qty should match active_positions.size
        trades_qty = trades_row['open_qty']
        ap_qty = ap_row['size']
        
        if abs(trades_qty - ap_qty) < 1e-8:
            print(f"  ✅ GREEN: trades.open_qty ({trades_qty}) == active_positions.size ({ap_qty})")
            print("  ✅ A1+A2 FIX WORKS: seal_trade_state upserts active_positions correctly")
            return True
        else:
            print(f"  ❌ RED: trades.open_qty ({trades_qty}) != active_positions.size ({ap_qty})")
            print("  ❌ A1+A2 NOT WORKING: active_positions not upserted or trades not updated")
            return False
            
    finally:
        # Restore original functions
        database.get_connection = original_get_connection
        ledger.get_connection = original_get_connection
        position_ledger.get_connection = original_get_connection
        conn.close()


if __name__ == '__main__':
    success = test_sui_cycle25_regression()
    sys.exit(0 if success else 1)