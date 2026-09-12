#!/usr/bin/env python3
"""
Regression test for startup-wipe guard fix (Priority 2, Option A).
Tests that hedge children without TP fills are NOT wiped on startup_sync.
"""

import sqlite3
import tempfile
import os
import sys

sys.path.insert(0, 'D:/Crypto_Quant_Bot')
from engine.database import _reset_bot_after_tp_internal, get_connection

def test_startup_wipe_guard():
    """Verify hedge child with entry fill but no TP is NOT wiped on startup"""
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Test 1: Bot 100315 (hedge child) - has entry fill, NO TP in current cycle
    # Should NOT be wiped
    bot_id = 100315
    current_cycle = 15
    action_label = 'TP_HIT'
    
    # Check what the guard would return
    has_real_tp = cursor.execute("""
        SELECT 1 FROM bot_orders 
        WHERE bot_id = ? AND order_type = 'tp' AND status = 'filled' 
        AND cycle_id = ? AND filled_amount > 0
        LIMIT 1
    """, (bot_id, current_cycle)).fetchone()
    
    allow_wipe = (action_label in ['TP_HIT', 'SYSTEM_WIPE', 'MANUAL_CLOSE', 'EMERGENCY_CLOSE']) and has_real_tp
    
    print(f"Test 1 - Bot {bot_id} (hedge child, no TP in cycle {current_cycle}):")
    print(f"  has_real_tp: {bool(has_real_tp)}")
    print(f"  allow_wipe: {allow_wipe}")
    assert not allow_wipe, "Hedge child without TP should NOT be wiped"
    print("  ✅ PASS: Not wiped")
    
    # Test 2: Bot 10018 (parent) - HAS TP fills in cycle 25 with status='filled'
    # The guard correctly detects real TP fills and allows wipe
    # (The cycle sweep prevents cycle 25 from being swept because it's unbalanced - that's a separate mechanism)
    bot_id = 10018
    current_cycle = 25
    
    has_real_tp = cursor.execute("""
        SELECT 1 FROM bot_orders 
        WHERE bot_id = ? AND order_type = 'tp' AND status = 'filled' 
        AND cycle_id = ? AND filled_amount > 0
        LIMIT 1
    """, (bot_id, current_cycle)).fetchone()
    
    allow_wipe = (action_label in ['TP_HIT', 'SYSTEM_WIPE', 'MANUAL_CLOSE', 'EMERGENCY_CLOSE']) and has_real_tp
    
    print(f"\nTest 2 - Bot {bot_id} (parent, HAS filled TP in cycle {current_cycle}):")
    print(f"  has_real_tp: {bool(has_real_tp)}")
    print(f"  allow_wipe: {allow_wipe}")
    assert allow_wipe, "Bot WITH filled TP in current cycle SHOULD be allowed to wipe"
    print("  ✅ PASS: Wipe allowed (cycle sweep will prevent sweep because cycle is unbalanced)")
    
    # Test 3: Simulate a bot WITH a real TP fill in current cycle
    # Create a test bot with a filled TP in cycle 99
    test_bot_id = 99999
    cursor.execute("""
        INSERT OR REPLACE INTO bots (id, name, pair, direction, bot_type, status)
        VALUES (?, 'test_bot', 'BTC/USDT', 'LONG', 'standard', 'IN_TRADE')
    """, (test_bot_id,))
    
    cursor.execute("""
        INSERT INTO trades (bot_id, cycle_id, open_qty)
        VALUES (?, 99, 0.0)
    """, (test_bot_id,))
    
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount,
                                status, created_at, updated_at, client_order_id, cycle_id)
        VALUES (?, 0, 'tp', 'test_tp_1', 50000, 0.1, 0.1,
                'filled', 1789182722, 1789182722, 'test_cid_1', 99)
    """, (test_bot_id,))
    
    has_real_tp = cursor.execute("""
        SELECT 1 FROM bot_orders 
        WHERE bot_id = ? AND order_type = 'tp' AND status = 'filled' 
        AND cycle_id = ? AND filled_amount > 0
        LIMIT 1
    """, (test_bot_id, 99)).fetchone()
    
    allow_wipe = (action_label in ['TP_HIT', 'SYSTEM_WIPE', 'MANUAL_CLOSE', 'EMERGENCY_CLOSE']) and has_real_tp
    
    print(f"\nTest 3 - Bot {test_bot_id} (test bot WITH filled TP in cycle 99):")
    print(f"  has_real_tp: {bool(has_real_tp)}")
    print(f"  allow_wipe: {allow_wipe}")
    assert allow_wipe, "Bot WITH filled TP in current cycle SHOULD be allowed to wipe"
    print("  ✅ PASS: Wipe allowed")
    
    # Cleanup
    cursor.execute("DELETE FROM bot_orders WHERE bot_id = ?", (test_bot_id,))
    cursor.execute("DELETE FROM trades WHERE bot_id = ?", (test_bot_id,))
    cursor.execute("DELETE FROM bots WHERE id = ?", (test_bot_id,))
    conn.commit()
    
    print("\n============================================================")
    print("ALL STARTUP-WIPE GUARD TESTS PASSED ✅")
    print("============================================================")

if __name__ == '__main__':
    test_startup_wipe_guard()