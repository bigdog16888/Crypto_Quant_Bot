#!/usr/bin/env python3
"""
Regression test for SUI cycle-sweep bug fix.
Built from REAL order data for bot 10018 (sui long).
"""

import sqlite3
import tempfile
import os

# Real database connection
REAL_DB = 'D:/Crypto_Quant_Bot/crypto_bot.db'

def get_real_order_data(bot_id=10018):
    """Extract real order data from production database."""
    conn = sqlite3.connect(REAL_DB)
    cur = conn.cursor()
    
    cur.execute('''
        SELECT id, bot_id, order_type, status, filled_amount, price, 
               filled_at, created_at, cycle_id, client_order_id, position_side
        FROM bot_orders
        WHERE bot_id = ? AND filled_amount > 0
        ORDER BY cycle_id, id
    ''', (bot_id,))
    
    rows = cur.fetchall()
    conn.close()
    return rows

def create_test_fixture():
    """Create a test database with the exact SUI order history."""
    # Get real data
    real_orders = get_real_order_data(10018)
    
    # Create temp database
    fd, tmp_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    conn = sqlite3.connect(tmp_path)
    cur = conn.cursor()
    
    # Create schema matching production
    cur.execute('''
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY,
            bot_id INTEGER,
            order_type TEXT,
            status TEXT,
            filled_amount REAL,
            price REAL,
            filled_at INTEGER,
            created_at INTEGER,
            cycle_id INTEGER,
            client_order_id TEXT,
            position_side TEXT
        )
    ''')
    
    cur.execute('''
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            name TEXT,
            direction TEXT,
            bot_type TEXT,
            pair TEXT,
            is_active INTEGER DEFAULT 1
        )
    ''')
    
    cur.execute('''
        CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY,
            open_qty REAL DEFAULT 0,
            total_invested REAL DEFAULT 0,
            avg_entry_price REAL DEFAULT 0,
            status TEXT DEFAULT 'Scanning'
        )
    ''')
    
    # Insert bot
    cur.execute('INSERT INTO bots VALUES (10018, "sui long", "LONG", "standard", "SUI/USDC", 1)')
    
    # Insert orders with their REAL statuses (all reset_cleared)
    for row in real_orders:
        cur.execute('''
            INSERT INTO bot_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ''', row)
    
    conn.commit()
    conn.close()
    
    return tmp_path

def run_cycle_sweep_buggy(db_path, bot_id, current_cycle):
    """Current buggy sweep: only looks at status='filled' orders."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    swept = []
    for cycle in range(current_cycle):
        cur.execute('''
            SELECT 
                SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END),
                SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END)
            FROM bot_orders
            WHERE bot_id = ? AND cycle_id = ? AND status = 'filled'
        ''', (bot_id, cycle))
        entry, exit = cur.fetchone()
        entry = entry or 0
        exit = exit or 0
        
        if abs(entry - exit) < 1e-6 and (entry > 0 or exit > 0):
            swept.append(cycle)
    
    conn.close()
    return swept

def run_cycle_sweep_fixed(db_path, bot_id, current_cycle):
    """Fixed sweep: looks at ALL fills regardless of status."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    swept = []
    for cycle in range(current_cycle):
        cur.execute('''
            SELECT 
                SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END),
                SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END)
            FROM bot_orders
            WHERE bot_id = ? AND cycle_id = ? AND filled_amount > 0
        ''', (bot_id, cycle))
        entry, exit = cur.fetchone()
        entry = entry or 0
        exit = exit or 0
        
        if abs(entry - exit) < 1e-6 and (entry > 0 or exit > 0):
            swept.append(cycle)
    
    conn.close()
    return swept

def test_sweep_reproduces_bug():
    """Test that buggy sweep finds NOTHING (all orders are reset_cleared)."""
    db_path = create_test_fixture()
    try:
        swept = run_cycle_sweep_buggy(db_path, 10018, 26)
        print(f"Buggy sweep result: {swept}")
        assert swept == [], f"Expected [], got {swept}"
        print("✅ Buggy sweep correctly finds NOTHING (all orders are reset_cleared)")
        return True
    finally:
        os.unlink(db_path)

def test_sweep_fixed_identifies_correct_cycles():
    """Test that fixed sweep correctly identifies which cycles to sweep."""
    db_path = create_test_fixture()
    try:
        swept = run_cycle_sweep_fixed(db_path, 10018, 26)
        print(f"Fixed sweep result: {swept}")
        
        expected_swept = [0, 1, 2, 3, 4, 5, 8, 11, 12, 14, 15, 17, 18, 20, 22, 23]
        expected_not_swept = [6, 10, 25]
        
        assert swept == expected_swept, f"Expected {expected_swept}, got {swept}"
        print("✅ Fixed sweep correctly identifies balanced cycles")
        
        for cycle in expected_not_swept:
            assert cycle not in swept, f"Cycle {cycle} should NOT be swept"
        print("✅ Fixed sweep correctly does NOT sweep unbalanced cycles (6, 10, 25)")
        
        return True
    finally:
        os.unlink(db_path)

def test_classify_reset_cleared():
    """Test the classification logic on real data."""
    db_path = create_test_fixture()
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # Get cycles with reset_cleared orders
        cur.execute('''
            SELECT DISTINCT cycle_id FROM bot_orders 
            WHERE bot_id = 10018 AND status = 'reset_cleared' AND filled_amount > 0
            ORDER BY cycle_id
        ''')
        cycles = [r[0] for r in cur.fetchall()]
        
        classifications = {}
        for cycle in cycles:
            # Count reset_cleared orders
            cur.execute('''
                SELECT COUNT(*), SUM(filled_amount)
                FROM bot_orders
                WHERE bot_id = 10018 AND cycle_id = ? AND status = 'reset_cleared' AND filled_amount > 0
            ''', (cycle,))
            reset_count, reset_units = cur.fetchone()
            reset_count = reset_count or 0
            reset_units = reset_units or 0
            
            # True balance
            cur.execute('''
                SELECT 
                    SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END),
                    SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END)
                FROM bot_orders
                WHERE bot_id = 10018 AND cycle_id = ? AND filled_amount > 0
            ''', (cycle,))
            entry, exit = cur.fetchone()
            entry = entry or 0
            exit = exit or 0
            net = entry - exit
            balanced = abs(net) < 1e-6
            classification = 'LEGITIMATE' if balanced else 'INCORRECT'
            
            classifications[cycle] = {
                'reset_orders': reset_count,
                'reset_units': reset_units,
                'entry': entry,
                'exit': exit,
                'net': net,
                'balanced': balanced,
                'classification': classification
            }
        
        conn.close()
        
        # Verify exact expected results
        assert classifications[6]['classification'] == 'INCORRECT'
        assert classifications[6]['reset_orders'] == 1
        assert classifications[6]['reset_units'] == 7.6
        assert classifications[6]['net'] == 7.6
        
        assert classifications[10]['classification'] == 'INCORRECT'
        assert classifications[10]['reset_orders'] == 4
        assert classifications[10]['reset_units'] == 73.3
        assert classifications[10]['net'] == 58.3
        
        assert classifications[25]['classification'] == 'INCORRECT'
        assert classifications[25]['reset_orders'] == 7
        assert classifications[25]['reset_units'] == 543.0
        assert abs(classifications[25]['net'] - 111.5) < 0.01
        
        # Verify all other cycles are LEGITIMATE
        for cycle in [0,1,2,3,4,5,8,11,12,14,15,17,18,20,22,23]:
            assert classifications[cycle]['classification'] == 'LEGITIMATE', f"Cycle {cycle} should be LEGITIMATE"
        
        print("✅ Classification matches expected results exactly")
        print(f"   Cycle 6: {classifications[6]['reset_orders']} orders, {classifications[6]['reset_units']} units, net={classifications[6]['net']} → INCORRECT")
        print(f"   Cycle 10: {classifications[10]['reset_orders']} orders, {classifications[10]['reset_units']} units, net={classifications[10]['net']} → INCORRECT")
        print(f"   Cycle 25: {classifications[25]['reset_orders']} orders, {classifications[25]['reset_units']} units, net={classifications[25]['net']} → INCORRECT")
        
        return True
    finally:
        os.unlink(db_path)

if __name__ == '__main__':
    print("=" * 60)
    print("SUI CYCLE-SWEEP REGRESSION TEST (Real Data Fixture)")
    print("=" * 60)
    
    test_sweep_reproduces_bug()
    print()
    test_sweep_fixed_identifies_correct_cycles()
    print()
    test_classify_reset_cleared()
    print()
    print("=" * 60)
    print("ALL TESTS PASSED ✅")
    print("=" * 60)