#!/usr/bin/env python3
"""
test_cross_pair_fill_attribution.py — Regression test for cross-pair fill attribution bug.

Bug: compute_bot_position() was fetching fills by bot_id ONLY, without filtering by the
bot's configured pair. This caused fills from one pair (e.g., SUI/USDC:USDC) to be
attributed to a bot configured for a different pair (e.g., BTC/USDC:USDC), producing
impossible phantom positions like -2332 BTC.

Fix: _fetch_fills_for_bot() now filters by bot's pair (both CCXT and normalized formats).

Test scenarios:
1. A bot with fills belonging to ANOTHER pair should show 0 fills (filtered out)
2. A bot with fills belonging to ITS OWN pair should show correct fills (not filtered out)
3. A bot with fills in BOTH its own pair and another pair should only see its own pair
"""
import sys
import sqlite3
import tempfile
import os

# Add project to path
sys.path.insert(0, 'D:/Crypto_Quant_Bot')

from engine.position_ledger import compute_bot_position, _fetch_fills_for_bot, compute_pair_position
from engine.database import init_db, get_connection

def setup_test_db():
    """Create a fresh test database with the schema."""
    # Use a temporary file
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp.close()
    db_path = tmp.name
    
    conn = sqlite3.connect(db_path)
    
    # Create tables matching production schema
    conn.executescript("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            name TEXT,
            pair TEXT,
            normalized_pair TEXT,
            direction TEXT,
            status TEXT,
            is_active INTEGER DEFAULT 1,
            bot_type TEXT DEFAULT 'standard'
        );
        
        CREATE TABLE exchange_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exchange_order_id TEXT,
            client_order_id TEXT,
            symbol TEXT,
            side TEXT,
            qty REAL,
            price REAL,
            fee REAL DEFAULT 0,
            fee_asset TEXT,
            fill_ts INTEGER,
            source TEXT,
            bot_id INTEGER,
            order_type TEXT,
            step INTEGER,
            cycle_id INTEGER,
            raw_json TEXT,
            created_at INTEGER
        );
    """)
    
    return db_path, conn

def test_bot_with_only_wrong_pair_fills():
    """Test 1: Bot configured for Pair A, but has fills for Pair B → should see 0 fills."""
    db_path, conn = setup_test_db()
    
    try:
        # Bot configured for BTC/USDC:USDC (LONG)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
            VALUES (100318, 'test bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'Scanning', 'standard')
        """)
        
        # Fills for SUI/USDC:USDC (different pair!) - all SELL (would create huge short if misattributed)
        conn.executemany("""
            INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('149925759', 'CQB_100318_ENTRY_157_1', 'SUI/USDC:USDC', 'SELL', 1044.3, 0.7408, 'backfill', 100318, 'entry', 1, 157, 1784188337, 1789211813),
            ('149925760', 'CQB_100318_ENTRY_157_7', 'SUI/USDC:USDC', 'SELL', 1044.3, 0.7425, 'backfill', 100318, 'entry', 7, 157, 1784188338, 1789211813),
            ('149940626', 'CQB_100318_ENTRY_157_8', 'SUI/USDC:USDC', 'SELL', 7.3, 0.739, 'backfill', 100318, 'entry', 8, 157, 1784189463, 1789211813),
            ('150498145', 'CQB_100318_FLATTEN', 'SUI/USDC:USDC', 'SELL', 244.1, 0.7371, 'backfill', 100318, 'grid', 1784245225, 7, 1784245225, 1789211813),
            ('166319243', 'CQB_100318_TP_157', 'SUI/USDC:USDC', 'BUY', 7.9, 0.685, 'backfill', 100318, 'tp', 0, 157, 1786000205, 1789211813),
        ])
        conn.commit()
        
        # Compute position - should be 0 because fills are for wrong pair
        bp = compute_bot_position(100318, conn)
        
        assert bp.fills_count == 0, f"Expected 0 fills, got {bp.fills_count}"
        assert bp.net_qty == 0.0, f"Expected net_qty 0.0, got {bp.net_qty}"
        assert bp.entry_cost == 0.0, f"Expected entry_cost 0.0, got {bp.entry_cost}"
        
        print("✅ Test 1 PASSED: Bot with only wrong-pair fills shows 0 fills")
        return True
    finally:
        conn.close()
        os.unlink(db_path)

def test_bot_with_only_correct_pair_fills():
    """Test 2: Bot configured for Pair A, has fills for Pair A → should see fills correctly."""
    db_path, conn = setup_test_db()
    
    try:
        # Bot configured for BTC/USDC:USDC (LONG)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
            VALUES (10016, 'long btc', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'REQUIRE_MANUAL_PROOF', 'standard')
        """)
        
        # Fills for BTC/USDC:USDC (correct pair)
        conn.executemany("""
            INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('1001', 'CQB_10016_E1', 'BTC/USDC:USDC', 'BUY', 0.002, 65507.8, 'backfill', 10016, 'entry', 1, 2, 1784188337, 1789211813),
            ('1002', 'CQB_10016_G2', 'BTC/USDC:USDC', 'BUY', 0.003, 65397.7, 'backfill', 10016, 'grid', 2, 2, 1784188338, 1789211813),
            ('1003', 'CQB_10016_TP1', 'BTC/USDC:USDC', 'SELL', 0.01, 65262.6, 'backfill', 10016, 'tp', 1, 2, 1784189463, 1789211813),
        ])
        conn.commit()
        
        bp = compute_bot_position(10016, conn)
        
        # Should see all 3 fills
        assert bp.fills_count == 3, f"Expected 3 fills, got {bp.fills_count}"
        # net = 0.002 + 0.003 - 0.01 = -0.005 (but LONG so SELL reduces)
        # Actually: BUY 0.002 + BUY 0.003 = +0.005, then SELL 0.01 = -0.005 net
        assert bp.net_qty == -0.005, f"Expected net_qty -0.005, got {bp.net_qty}"
        
        print("✅ Test 2 PASSED: Bot with correct-pair fills sees them correctly")
        return True
    finally:
        conn.close()
        os.unlink(db_path)

def test_bot_with_mixed_pair_fills():
    """Test 3: Bot has fills for BOTH its pair and another pair → should only see its own."""
    db_path, conn = setup_test_db()
    
    try:
        # Bot configured for BTC/USDC:USDC (LONG)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
            VALUES (999, 'mixed bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'Scanning', 'standard')
        """)
        
        # Mix of BTC/USDC:USDC (correct) and ETH/USDC:USDC (wrong) fills
        conn.executemany("""
            INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            # Correct pair fills
            ('2001', 'CQB_999_E1', 'BTC/USDC:USDC', 'BUY', 0.01, 50000.0, 'backfill', 999, 'entry', 1, 1, 1784188337, 1789211813),
            ('2002', 'CQB_999_G2', 'BTC/USDC:USDC', 'BUY', 0.02, 49500.0, 'backfill', 999, 'grid', 2, 1, 1784188338, 1789211813),
            # Wrong pair fills (should be ignored)
            ('3001', 'CQB_999_E1_ETH', 'ETH/USDC:USDC', 'BUY', 10.0, 3000.0, 'backfill', 999, 'entry', 1, 1, 1784188337, 1789211813),
            ('3002', 'CQB_999_TP_ETH', 'ETH/USDC:USDC', 'SELL', 5.0, 3100.0, 'backfill', 999, 'tp', 1, 1, 1784188338, 1789211813),
        ])
        conn.commit()
        
        bp = compute_bot_position(999, conn)
        
        # Should only see 2 fills (BTC ones), not 4
        assert bp.fills_count == 2, f"Expected 2 fills (BTC only), got {bp.fills_count}"
        # BTC: BUY 0.01 + BUY 0.02 = +0.03 net
        assert bp.net_qty == 0.03, f"Expected net_qty 0.03, got {bp.net_qty}"
        
        print("✅ Test 3 PASSED: Bot with mixed fills only sees its own pair")
        return True
    finally:
        conn.close()
        os.unlink(db_path)

def test_normalized_pair_matching():
    """Test 4: Normalized pair matching works (CCXT format 'BTC/USDC:USDC' → 'BTCUSDC')."""
    db_path, conn = setup_test_db()
    
    try:
        # Bot configured with normalized_pair only
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
            VALUES (1001, 'test live', 'BTCUSDT', 'BTCUSDC', 'LONG', 'REQUIRE_MANUAL_PROOF', 'standard')
        """)
        
        # Fills in CCXT format (BTC/USDC:USDC) which normalizes to BTCUSDC
        conn.executemany("""
            INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('4001', 'CQB_1001_E1', 'BTC/USDC:USDC', 'BUY', 0.01, 50000.0, 'backfill', 1001, 'entry', 1, 1, 1784188337, 1789211813),
            ('4002', 'CQB_1001_G2', 'BTCUSDC', 'BUY', 0.02, 49500.0, 'backfill', 1001, 'grid', 2, 1, 1784188338, 1789211813),
        ])
        conn.commit()
        
        bp = compute_bot_position(1001, conn)
        
        # Should see both fills (both normalize to BTCUSDC)
        assert bp.fills_count == 2, f"Expected 2 fills, got {bp.fills_count}"
        assert bp.net_qty == 0.03, f"Expected net_qty 0.03, got {bp.net_qty}"
        
        print("✅ Test 4 PASSED: Normalized pair matching works for both CCXT and WS formats")
        return True
    finally:
        conn.close()
        os.unlink(db_path)

def test_original_bug_regression():
    """Test 5: Reproduce the exact original bug scenario (bot 100318)."""
    db_path, conn = setup_test_db()
    
    try:
        # Exact bot 100318 config
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
            VALUES (100318, 'test bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'Scanning', 'standard')
        """)
        
        # Exact fills from the bug (SUI/USDC:USDC)
        conn.executemany("""
            INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('149925759', 'CQB_100318_ENTRY_157_1_CATCHUP_17841', 'SUI/USDC:USDC', 'SELL', 1044.3, 0.7408, 'backfill', 100318, 'entry', 1, 157, 1784188337, 1789211813),
            ('149925760', 'CQB_100318_ENTRY_157_7_1784188339', 'SUI/USDC:USDC', 'SELL', 1044.3, 0.7425, 'backfill', 100318, 'entry', 7, 157, 1784188338, 1789211813),
            ('149940626', 'CQB_100318_ENTRY_157_8_GTC', 'SUI/USDC:USDC', 'SELL', 7.3, 0.739, 'backfill', 100318, 'entry', 8, 157, 1784189463, 1789211813),
            ('150498145', 'CQB_100318_FLATTEN_1784245225', 'SUI/USDC:USDC', 'SELL', 244.1, 0.7371, 'backfill', 100318, 'grid', 1784245225, 7, 1784245225, 1789211813),
            ('166319243', 'CQB_100318_TP_157_BE_FB_1786000203', 'SUI/USDC:USDC', 'BUY', 7.9, 0.685, 'backfill', 100318, 'tp', 0, 157, 1786000205, 1789211813),
        ])
        conn.commit()
        
        bp = compute_bot_position(100318, conn)
        
        # Bug would produce: net_qty = -2332.1 (wrong!)
        # Fix produces: net_qty = 0.0 (correct - filtered out)
        assert bp.fills_count == 0, f"REGRESSION: Expected 0 fills, got {bp.fills_count} (bug not fixed!)"
        assert bp.net_qty == 0.0, f"REGRESSION: Expected net_qty 0.0, got {bp.net_qty} (bug not fixed!)"
        
        print("✅ Test 5 PASSED: Original bug scenario (bot 100318) now correctly returns 0")
        return True
    finally:
        conn.close()
        os.unlink(db_path)

def test_pair_position_aggregation():
    """Test 6: compute_pair_position correctly aggregates only valid fills."""
    db_path, conn = setup_test_db()
    
    try:
        # Two bots on BTC/USDC:USDC
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
            VALUES (10016, 'long btc', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'Scanning', 'standard')
        """)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
            VALUES (10022, 'short btc', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', 'Scanning', 'standard')
        """)
        # One bot on SUI/USDC:USDC (the one that had cross-contamination)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
            VALUES (10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', 'Scanning', 'standard')
        """)
        
        # Bot 10016: BTC fills
        conn.executemany("""
            INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('5001', 'CQB_10016_E1', 'BTC/USDC:USDC', 'BUY', 0.1, 50000.0, 'backfill', 10016, 'entry', 1, 1, 1784188337, 1789211813),
            ('5002', 'CQB_10016_TP1', 'BTC/USDC:USDC', 'SELL', 0.05, 51000.0, 'backfill', 10016, 'tp', 1, 1, 1784188338, 1789211813),
        ])
        
        # Bot 10022: BTC fills (SHORT)
        conn.executemany("""
            INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('6001', 'CQB_10022_E1', 'BTC/USDC:USDC', 'SELL', 0.05, 50000.0, 'backfill', 10022, 'entry', 1, 1, 1784188337, 1789211813),
            ('6002', 'CQB_10022_TP1', 'BTC/USDC:USDC', 'BUY', 0.02, 49000.0, 'backfill', 10022, 'tp', 1, 1, 1784188338, 1789211813),
        ])
        
        # Bot 10018: SUI fills (should NOT appear in BTC pair)
        conn.executemany("""
            INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('7001', 'CQB_10018_E1', 'SUI/USDC:USDC', 'BUY', 100.0, 1.0, 'backfill', 10018, 'entry', 1, 1, 1784188337, 1789211813),
        ])
        conn.commit()
        
        # Compute BTC pair - should only include 10016 and 10022
        btc_pos = compute_pair_position('BTC/USDC:USDC', conn)
        
        # Bot 10016: BUY 0.1, SELL 0.05 → net = +0.05
        # Bot 10022: SELL 0.05, BUY 0.02 → net = -0.03 (SHORT, so SELL increases short)
        # Pair net = 0.05 + (-0.03) = 0.02
        assert btc_pos.net_qty == 0.02, f"BTC pair net_qty should be 0.02, got {btc_pos.net_qty}"
        # Only 2 bots in the pair (10016 and 10022)
        assert len([b for b in btc_pos.bots if b.net_qty != 0]) == 2, f"Expected 2 non-zero bots in BTC pair"
        
        # Compute SUI pair - should only include 10018
        sui_pos = compute_pair_position('SUI/USDC:USDC', conn)
        assert sui_pos.net_qty == 100.0, f"SUI pair net_qty should be 100.0, got {sui_pos.net_qty}"
        assert len([b for b in sui_pos.bots if b.net_qty != 0]) == 1, f"Expected 1 non-zero bot in SUI pair"
        
        print("✅ Test 6 PASSED: Pair aggregation correctly isolates fills per pair")
        return True
    finally:
        conn.close()
        os.unlink(db_path)

if __name__ == '__main__':
    print("=" * 60)
    print("REGRESSION TEST: Cross-pair fill attribution bug")
    print("=" * 60)
    
    tests = [
        test_bot_with_only_wrong_pair_fills,
        test_bot_with_only_correct_pair_fills,
        test_bot_with_mixed_pair_fills,
        test_normalized_pair_matching,
        test_original_bug_regression,
        test_pair_position_aggregation,
    ]
    
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"❌ {test.__name__} FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {test.__name__} ERROR: {e}")
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    if failed > 0:
        sys.exit(1)
    sys.exit(0)