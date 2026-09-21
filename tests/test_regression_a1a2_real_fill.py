#!/usr/bin/env python3
"""
Regression test: A1+A2 active_positions upsert on REAL fill path.

This test simulates a REAL fill arriving via credit_fill → seal_trade_state
with main_open_qty > 0, and asserts active_positions gets upserted correctly.

RED on pre-A1+A2 code (no upsert happens).
GREEN with A1+A2 code (upsert happens and active_positions matches trades).
"""
import sys
import os
import tempfile
import sqlite3

# Use a temp DB for isolation
TEST_DB = tempfile.mktemp(suffix='_test_a1a2_real_fill.db')
os.environ['CRYPTO_BOT_DB'] = TEST_DB

# Import after setting env
sys.path.insert(0, '/d/Crypto_Quant_Bot')

# CRITICAL: Patch DB_PATH in engine.database BEFORE importing ledger
import engine.database as database
database.DB_PATH = TEST_DB
database._local.connection = None  # Force new connection

from engine.ledger import credit_fill, seal_trade_state

def setup_test_db():
    """Create test DB with production-matched schema."""
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    
    # Production-matched schemas (from .schema output)
    conn.executescript("""
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
        
        CREATE TABLE fill_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            exchange_order_id TEXT,
            fill_ts INTEGER,
            qty REAL,
            price REAL,
            claimed_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(bot_id, exchange_order_id, fill_ts, qty)
        );
    """)
    
    # Insert test bot (SUIUSDC-like LONG bot)
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, is_active, bot_type)
        VALUES (10018, 'sui long test', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', 'IN TRADE', 1, 'standard')
    """)
    
    # Initialize trades row (stale state like the incident)
    conn.execute("""
        INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, 
                           target_tp_price, cycle_id, cycle_phase, open_qty, position_side)
        VALUES (10018, 25, 0.0, 0.0, 0.0, 25, 'ACTIVE', 0.0, 'LONG')
    """)
    
    # Add an OPEN entry order in bot_orders (simulating pre-fill state for cycle 25)
    # credit_fill will update filled_amount from 0 → 58.6 and status from 'open' → 'filled'
    conn.execute("""
        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, 
                               filled_amount, status, created_at, client_order_id,
                               updated_at, cycle_id, position_side, filled_at, cumulative_filled)
        VALUES (10018, 1, 'entry', 'TEST_OID_1', 0.706, 58.6, 0.0, 'open', 
                1789647000, 'TEST_CLIENT_1', 1789647000, 25, 'LONG', 0, 0.0)
    """)
    
    conn.commit()
    conn.close()

def get_active_positions(bot_id):
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM active_positions WHERE bot_id=?", (bot_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_trades(bot_id):
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE bot_id=?", (bot_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def test_a1a2_real_fill_path():
    print("=" * 70)
    print("REGRESSION TEST: A1+A2 active_positions upsert on REAL fill path")
    print("=" * 70)
    
    setup_test_db()
    
    # PHASE 1: Initial state - trades has stale 0.0, no active_positions row
    print("\n[PHASE 1] Initial state (simulates SUI cycle-25 incident):")
    trades_before = get_trades(10018)
    ap_before = get_active_positions(10018)
    print(f"  trades.open_qty: {trades_before['open_qty']} (STALE)")
    print(f"  active_positions: {ap_before}")
    print(f"  >>> BUG: trades.open_qty=0.0 but fill exists in bot_orders")
    
    # PHASE 2: Simulate REAL fill arriving - credit_fill triggers seal_trade_state
    print("\n[PHASE 2] Simulating real fill: credit_fill(10018, 'TEST_OID_1', 58.6, 0.706, 'entry')")
    credit_fill(
        bot_id=10018,
        order_id='TEST_OID_1',
        cumulative_qty=58.6,
        avg_price=0.706,
        order_type='entry',
        fill_ts=1789647000,
        caller='test_regression',
        side='BUY'
    )
    print("  credit_fill completed (should have called seal_trade_state internally)")
    
    # PHASE 3: Verify A1 upsert happened
    print("\n[PHASE 3] State AFTER credit_fill + seal_trade_state:")
    trades_after = get_trades(10018)
    ap_after = get_active_positions(10018)
    print(f"  trades.open_qty: {trades_after['open_qty']}")
    print(f"  trades.total_invested: {trades_after['total_invested']}")
    print(f"  trades.avg_entry_price: {trades_after['avg_entry_price']}")
    print(f"  active_positions: {ap_after}")
    
    # VERIFICATION
    print("\n" + "=" * 70)
    print("VERIFICATION:")
    
    # Check 1: trades.open_qty was updated from fill
    if abs(trades_after['open_qty'] - 58.6) < 1e-8:
        print("  ✓ PASS: trades.open_qty updated to 58.6 (from fill)")
    else:
        print(f"  ✗ FAIL: trades.open_qty = {trades_after['open_qty']}, expected 58.6")
        return False
    
    # Check 2: active_positions row EXISTS and matches
    if ap_after is None:
        print("  ✗ FAIL: active_positions row NOT CREATED (A1 upsert missing)")
        return False
    else:
        print("  ✓ PASS: active_positions row EXISTS")
    
    # Check 3: active_positions.size matches trades.open_qty
    if abs(ap_after['size'] - trades_after['open_qty']) < 1e-8:
        print(f"  ✓ PASS: active_positions.size ({ap_after['size']}) == trades.open_qty ({trades_after['open_qty']})")
    else:
        print(f"  ✗ FAIL: active_positions.size ({ap_after['size']}) != trades.open_qty ({trades_after['open_qty']})")
        return False
    
    # Check 4: active_positions.entry_price matches trades.avg_entry_price
    if abs(ap_after['entry_price'] - trades_after['avg_entry_price']) < 1e-6:
        print(f"  ✓ PASS: entry_price matches avg_entry_price ({ap_after['entry_price']})")
    else:
        print(f"  ✗ FAIL: entry_price ({ap_after['entry_price']}) != avg_entry_price ({trades_after['avg_entry_price']})")
        return False
    
    # Check 5: side is LONG (matches bot direction)
    if ap_after['side'] == 'LONG':
        print("  ✓ PASS: active_positions.side = LONG (matches bot direction)")
    else:
        print(f"  ✗ FAIL: side = {ap_after['side']}, expected LONG")
        return False
    
    print("\n" + "=" * 70)
    print("RESULT: GREEN - A1+A2 working correctly on real fill path")
    print("=" * 70)
    return True

if __name__ == '__main__':
    success = test_a1a2_real_fill_path()
    sys.exit(0 if success else 1)