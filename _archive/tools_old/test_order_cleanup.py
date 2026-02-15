#!/usr/bin/env python3
"""
COMPREHENSIVE TEST SUITE: Order Cleanup Verification
Tests that order cleanup works FUNDAMENTALLY in all scenarios
"""

import sys
from pathlib import Path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from engine.database import get_connection, reset_bot_after_tp, reconcile_with_db
import time

def reset_test_environment():
    """Clean slate for testing"""
    conn = get_connection()
    cur = conn.cursor()
    
    # Clean up any existing test data
    cur.execute("DELETE FROM bot_orders WHERE order_id LIKE 'TEST_%'")
    cur.execute("UPDATE trades SET total_invested = 0, current_step = 0 WHERE bot_id IN (43, 40)")
    conn.commit()
    print("✓ Test environment reset")

def verify_cleanup(bot_id, test_name):
    """Verify orders were cleaned up"""
    conn = get_connection()
    cur = conn.cursor()
    
    # Check position is reset
    cur.execute("SELECT total_invested, current_step FROM trades WHERE bot_id = ?", (bot_id,))
    result = cur.fetchone()
    invested, step = result if result else (None, None)
    
    # Check open orders
    cur.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status = 'open'", (bot_id,))
    open_count = cur.fetchone()[0]
    
    # Check auto-closed orders
    cur.execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id = ? AND status = 'auto_closed' AND order_id LIKE 'TEST_%'", (bot_id,))
    closed_count = cur.fetchone()[0]
    
    success = (invested == 0 and step == 0 and open_count == 0 and closed_count > 0)
    
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"{status} | {test_name}")
    print(f"     Position: invested=${invested}, step={step}")
    print(f"     Orders: {open_count} open, {closed_count} auto-closed")
    
    return success

def test_1_reset_bot_after_tp():
    """Test 1: reset_bot_after_tp cleans up orders"""
    print("\n" + "="*60)
    print("TEST 1: reset_bot_after_tp() order cleanup")
    print("="*60)
    
    bot_id = 43
    conn = get_connection()
    cur = conn.cursor()
    
    # Setup: Create position with 3 orders
    print("Setting up position with 3 orders...")
    cur.execute("UPDATE trades SET total_invested = 5000.0, current_step = 2, avg_entry_price = 60000.0 WHERE bot_id = ?", (bot_id,))
    
    cur.execute("""
        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at)
        VALUES 
            (?, 0, 'entry', 'TEST_ENTRY_001', 60000.0, 0.08, 'open', ?),
            (?, 1, 'grid', 'TEST_GRID_001', 59000.0, 0.04, 'open', ?),
            (?, 2, 'tp', 'TEST_TP_001', 62000.0, 0.12, 'open', ?)
    """, (bot_id, int(time.time()), bot_id, int(time.time()), bot_id, int(time.time())))
    conn.commit()
    
    # Execute: Call reset_bot_after_tp
    print("Calling reset_bot_after_tp...")
    reset_bot_after_tp(bot_id, exit_price=61500.0, direction='LONG', action_label='TP_HIT')
    
    # Verify
    return verify_cleanup(bot_id, "reset_bot_after_tp")

def test_2_reconcile_with_db():
    """Test 2: reconcile_with_db cleans up orders"""
    print("\n" + "="*60)
    print("TEST 2: reconcile_with_db() order cleanup")
    print("="*60)
    
    bot_id = 40  # Use existing bot
    conn = get_connection()
    cur = conn.cursor()
    
    # Setup: Create position with orders
    print("Setting up position with 2 orders...")
    cur.execute("UPDATE trades SET total_invested = 3000.0, current_step = 1, avg_entry_price = 65000.0 WHERE bot_id = ?", (bot_id,))
    
    cur.execute("""
        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at)
        VALUES 
            (?, 0, 'entry', 'TEST_ENTRY_002', 65000.0, 0.046, 'open', ?),
            (?, 1, 'tp', 'TEST_TP_002', 67000.0, 0.046, 'open', ?)
    """, (bot_id, int(time.time()), bot_id, int(time.time())))
    conn.commit()
    
    # Execute: Call reconcile_with_db with no exchange position (simulates closed position)
    print("Calling reconcile_with_db (position closed on exchange)...")
    reconcile_with_db(bot_id, current_price=66000.0, open_orders=[], exchange_position=None)
    
    # Verify
    return verify_cleanup(bot_id, "reconcile_with_db")

def test_3_multiple_orders():
    """Test 3: Cleanup with many orders (5+)"""
    print("\n" + "="*60)
    print("TEST 3: Cleanup with 5+ orders")
    print("="*60)
    
    bot_id = 40  # Reuse bot 40 for this test
    conn = get_connection()
    cur = conn.cursor()
    
    # Setup: Create position with 5 orders
    print("Setting up position with 5 orders...")
    cur.execute("UPDATE trades SET total_invested = 8000.0, current_step = 4, avg_entry_price = 64000.0 WHERE bot_id = ?", (bot_id,))
    
    orders = [
        (bot_id, 0, 'entry', 'TEST_ENTRY_003', 64000.0, 0.125, 'open', int(time.time())),
        (bot_id, 1, 'grid', 'TEST_GRID_003_A', 63000.0, 0.063, 'open', int(time.time())),
        (bot_id, 2, 'grid', 'TEST_GRID_003_B', 62000.0, 0.032, 'open', int(time.time())),
        (bot_id, 3, 'grid', 'TEST_GRID_003_C', 61000.0, 0.016, 'open', int(time.time())),
        (bot_id, 4, 'tp', 'TEST_TP_003', 66000.0, 0.236, 'open', int(time.time())),
    ]
    
    cur.executemany("""
        INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, orders)
    conn.commit()
    
    # Execute
    print("Calling reset_bot_after_tp...")
    reset_bot_after_tp(bot_id, exit_price=65500.0, direction='LONG', action_label='TP_HIT')
    
    # Verify
    return verify_cleanup(bot_id, "Multiple orders (5+)")

def test_4_quick_check_sync():
    """Test 4: Verify quick_check shows clean state"""
    print("\n" + "="*60)
    print("TEST 4: quick_check.py verification")
    print("="*60)
    
    conn = get_connection()
    cur = conn.cursor()
    
    # Check database state
    cur.execute("SELECT COUNT(*) FROM trades WHERE total_invested > 0")
    bots_in_trade = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM bot_orders WHERE status = 'open' AND order_id LIKE 'TEST_%'")
    test_orders_open = cur.fetchone()[0]
    
    print(f"Bots in trade: {bots_in_trade}")
    print(f"Test orders still open: {test_orders_open}")
    
    success = (bots_in_trade == 0 and test_orders_open == 0)
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"{status} | Database clean state")
    
    return success

def main():
    print("="*60)
    print("COMPREHENSIVE ORDER CLEANUP VERIFICATION")
    print("="*60)
    
    reset_test_environment()
    
    results = []
    
    # Run all tests
    results.append(("Test 1: reset_bot_after_tp", test_1_reset_bot_after_tp()))
    results.append(("Test 2: reconcile_with_db", test_2_reconcile_with_db()))
    results.append(("Test 3: Multiple orders", test_3_multiple_orders()))
    results.append(("Test 4: Quick check sync", test_4_quick_check_sync()))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} | {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 ALL TESTS PASSED - ORDER CLEANUP WORKS FUNDAMENTALLY!")
        return 0
    else:
        print(f"\n❌ {total - passed} TEST(S) FAILED - FIX NEEDED!")
        return 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n❌ TEST SUITE CRASHED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
