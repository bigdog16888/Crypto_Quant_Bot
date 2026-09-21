"""
Regression test for active_positions staleness bug (Track A1+A2).

This test reproduces the SUIUSDC cycle-25 incident where:
- Exchange shows 58.6 LONG position
- active_positions correctly shows 58.6 LONG (updated by offline fill reconstruction)
- trades.open_qty remains 0.0 (NOT updated from active_positions)

The bug: credit_fill() -> seal_trade_state() updates trades, but active_positions
is only updated by reconstruct_offline_fills(), and there's NO path from
active_positions back to trades.

Test scenario:
1. Simulate a bot with an exchange position (via active_positions upsert)
2. Run seal_trade_state() -- should flag mismatch, NOT auto-adopt (Track D)
3. Verify trades.open_qty remains 0.0 and MANUAL-REVIEW flag is written

This test should FAIL (RED) before the fix, PASS (GREEN) after A1+A2.
"""

import pytest
import sqlite3
import sys
import os

sys.path.insert(0, r'D:\Crypto_Quant_Bot')

from engine.database import get_connection, init_db, DB_PATH
from engine.ledger import seal_trade_state, credit_fill
from engine.position_ledger import _checkpoint_bot_position
from engine.exchange_interface import normalize_symbol


def _reset_test_tables(conn):
    """Reset test tables - drop and recreate to match production EXACTLY."""
    conn.executescript("""
        DROP TABLE IF EXISTS bots;
        DROP TABLE IF EXISTS trades;
        DROP TABLE IF EXISTS active_positions;
        DROP TABLE IF EXISTS bot_orders;
        DROP TABLE IF EXISTS exchange_fills;
        
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
            position_side TEXT DEFAULT 'LONG'
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
        
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            step INTEGER DEFAULT 0,
            order_type TEXT,
            order_id TEXT,
            price REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            filled_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'open',
            created_at INTEGER,
            client_order_id TEXT,
            updated_at INTEGER DEFAULT 0,
            notes TEXT,
            wipe_proof_source TEXT,
            wipe_proof_snapshot TEXT,
            cycle_id INTEGER DEFAULT 1,
            position_side TEXT DEFAULT 'BOTH',
            filled_at INTEGER DEFAULT 0
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


def test_active_positions_staleness_bug(temp_db):
    """
    Test that seal_trade_state() FLAGS (not auto-adopts) when active_positions
    has position but bot_orders has no fills (Track D: orphan adoption requires
    explicit per-bot review).
    """
    conn = temp_db
    _reset_test_tables(conn)
    
    # Insert test bot matching the SUIUSDC case
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status)
        VALUES (?, ?, ?, ?, ?, 1, 'IN TRADE')
    """, (10018, 'sui long', 'SUI/USDC:USDC', normalize_symbol('SUI/USDC:USDC').upper(), 'LONG'))
    conn.execute("""
        INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, 
                           entry_confirmed, cycle_id, cycle_phase, open_qty, position_side)
        VALUES (?, 5, 0.0, 0.0, 1, 25, 'ACTIVE', 0.0, 'LONG')
    """, (10018,))
    conn.commit()
    
    # Simulate what reconstruct_offline_fills does -- upsert active_positions
    conn.execute("""
        INSERT OR REPLACE INTO active_positions (bot_id, pair, side, size, entry_price, last_checked, last_updated)
        VALUES (?, ?, ?, ?, ?, strftime('%s','now'), datetime('now'))
    """, (10018, 'SUI/USDC:USDC', 'LONG', 58.6, 0.706))
    conn.commit()
    
    # Pre-condition: trades.open_qty is 0.0 (stale)
    row = conn.execute("SELECT open_qty FROM trades WHERE bot_id=10018").fetchone()
    assert row[0] == 0.0, f"Pre-condition failed: trades.open_qty should be 0.0, got {row[0]}"
    
    # ACT: Call seal_trade_state() -- should flag mismatch, NOT auto-adopt
    seal_trade_state(10018)
    
    # ASSERT: trades.open_qty should remain 0.0 (no auto-adopt)
    row = conn.execute("SELECT open_qty FROM trades WHERE bot_id=10018").fetchone()
    
    print(f"trades.open_qty after seal_trade_state: {row[0]}")
    print(f"Expected: 0.0 (no auto-adopt per Track D)")
    
    # Verify no auto-adopt
    assert row[0] == 0.0, f"AUTO-ADOPT VIOLATION: trades.open_qty={row[0]}, expected 0.0 (flag-only)"
    
    # Verify flag was written to bots.notes
    row = conn.execute("SELECT notes FROM bots WHERE id=10018").fetchone()
    print(f"bots.notes after seal_trade_state: {row[0]}")
    assert row[0] is not None and "MANUAL-REVIEW" in row[0] and "A2 mismatch" in row[0],         f"Expected MANUAL-REVIEW A2 mismatch flag in bots.notes, got: {row[0]}"
    print(f"bots.notes flag: {row[0]}")


def test_checkpoint_reads_active_positions(temp_db):
    """
    Test that _checkpoint_bot_position reads from active_positions correctly.
    This should PASS even before fix (it already works).
    """
    conn = temp_db
    _reset_test_tables(conn)
    
    # Insert test bot matching the SUIUSDC case
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status)
        VALUES (?, ?, ?, ?, ?, 1, 'IN TRADE')
    """, (10018, 'sui long', 'SUI/USDC:USDC', normalize_symbol('SUI/USDC:USDC').upper(), 'LONG'))
    conn.execute("""
        INSERT INTO trades (bot_id, current_step, total_invested, avg_entry_price, 
                           entry_confirmed, cycle_id, cycle_phase, open_qty, position_side)
        VALUES (?, 5, 0.0, 0.0, 1, 25, 'ACTIVE', 0.0, 'LONG')
    """, (10018,))
    conn.commit()
    
    # Simulate what reconstruct_offline_fills does -- upsert active_positions
    conn.execute("""
        INSERT OR REPLACE INTO active_positions (bot_id, pair, side, size, entry_price, last_checked, last_updated)
        VALUES (?, ?, ?, ?, ?, strftime('%s','now'), datetime('now'))
    """, (10018, 'SUI/USDC:USDC', 'LONG', 58.6, 0.706))
    conn.commit()
    
    bp = _checkpoint_bot_position(conn, 10018, 'SUI/USDC:USDC', 'LONG')
    
    print(f"_checkpoint_bot_position net_qty: {bp.net_qty}")
    assert abs(bp.net_qty - 58.6) < 0.0001, f"Checkpoint should read 58.6 from active_positions, got {bp.net_qty}"


if __name__ == '__main__':
    print("=== REGRESSION TEST: active_positions staleness (SUIUSDC cycle-25) ===")
    print()
    
    print("Test 1: checkpoint reads active_positions (should PASS)")
    try:
        test_checkpoint_reads_active_positions()
        print("  PASS +")
    except AssertionError as e:
        print(f"  FAIL -: {e}")
    
    print()
    print("Test 2: seal_trade_state flags mismatch (should FAIL before fix)")
    try:
        test_active_positions_staleness_bug()
        print("  PASS + (fix works!)")
    except AssertionError as e:
        print(f"  FAIL -: {e}")
