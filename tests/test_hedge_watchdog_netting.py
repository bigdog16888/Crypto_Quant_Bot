"""
Regression tests for O-10 Pair-Level Netting Watchdog (One-Way Mode).

Tests:
1. XAU/USDT incident scenario — exact numbers from the real incident
2. Netting OK case — correctly does NOT fire when virtual == physical
3. 3+ bots on one pair — confirms sum-based check scales correctly
"""
import pytest
import sqlite3
import tempfile
import os
from unittest.mock import Mock, MagicMock, patch

# Import the watchdog functions
from engine.hedge_watchdog import (
    verify_netting_engagement,
    verify_all_pairs_netting,
    get_config,
    _count_engine_hedge_failures,
)
from engine.parity_gates import pair_parity_ok, get_exchange_signed_net
import engine.database


class MockExchange:
    """Mock exchange that returns controlled position data.
    
    Mimics the output of ExchangeInterface.fetch_positions() which returns:
    - symbol: normalized (e.g., 'BTC/USDT:USDT')
    - contracts: signed position amount
    - net_qty: signed magnitude for netting math
    - side: 'long' or 'short'
    - unrealizedPnl: float
    - entryPrice: float
    """
    
    def __init__(self, positions_dict=None):
        # positions_dict: {normalized_symbol: {'contracts': float, 'net_qty': float, 'side': str, 'unrealizedPnl': float, 'entryPrice': float}}
        self.positions_data = positions_dict or {}
    
    def fetch_positions(self):
        """Return positions in the format returned by ExchangeInterface.fetch_positions()."""
        result = []
        for symbol, data in self.positions_data.items():
            pos = {
                'symbol': symbol,
                'contracts': data.get('contracts', 0),
                'net_qty': data.get('net_qty', 0),
                'qty': abs(data.get('contracts', 0)),
                'side': data.get('side', 'long'),
                'unrealizedPnl': data.get('unrealizedPnl', 0),
                'entryPrice': data.get('entryPrice', 0),
            }
            result.append(pos)
        return result


class MockConfig:
    """Mock config object."""
    MIN_HEDGE_QTY = 0.0001
    HEDGE_ENGAGE_TIMEOUT_SECONDS = 300
    HEDGE_FAIL_WINDOW_SECONDS = 86400
    PAIR_NETTING_TOLERANCE = 0.002
    TESTING_MODE = True


def create_test_db():
    """Create an in-memory test database with bots and trades tables."""
    conn = sqlite3.connect(':memory:')
    
    conn.execute("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            name TEXT,
            pair TEXT,
            normalized_pair TEXT,
            direction TEXT,
            is_active INTEGER DEFAULT 1,
            status TEXT DEFAULT 'Scanning',
            bot_type TEXT DEFAULT 'standard',
            parent_bot_id INTEGER,
            hedge_child_bot_id INTEGER,
            hedge_trigger_step INTEGER,
            last_error TEXT,
            last_error_time REAL
        )
    """)
    
    conn.execute("""
        CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY,
            open_qty REAL DEFAULT 0,
            cycle_phase TEXT DEFAULT 'IDLE',
            position_side TEXT DEFAULT 'LONG',
            cycle_id INTEGER DEFAULT 1,
            total_invested REAL DEFAULT 0,
            avg_entry_price REAL DEFAULT 0,
            wipe_wall_ts INTEGER DEFAULT 0
        )
    """)
    
    return conn


def run_with_test_db(conn, test_func):
    """
    Run a test function with the test database connection monkey-patched.
    
    The watchdog uses pair_parity_ok -> get_pair_virtual_net -> get_connection(),
    so we need to patch the global get_connection to return our test connection.
    """
    original_get_connection = engine.database.get_connection
    engine.database.get_connection = lambda: conn
    try:
        return test_func(conn)
    finally:
        engine.database.get_connection = original_get_connection


def test_xau_incident_scenario():
    """
    REGRESSION TEST: XAU/USDT incident from 2026-08-21.
    
    Scenario:
    - Parent 10019 (SHORT) at step 7: open_qty = 0.125 (DB)
    - Child 100319 (LONG) hedge entry filled: open_qty = 0.063 (DB)
    - Exchange net position: -0.186 (SHORT)
    - Virtual net = 0.063 - 0.125 = -0.062
    - Delta = -0.186 - (-0.062) = -0.124 > tolerance (0.002)
    - Expected: FREEZE parent (netting diverged)
    """
    def _test(conn):
        # Parent bot 10019: SHORT, hedge child 100319
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status, 
                              bot_type, hedge_child_bot_id, hedge_trigger_step)
            VALUES (10019, 'short gold', 'XAU/USDT:USDT', 'XAU/USDT:USDT', 'SHORT', 1, 'IN TRADE',
                    'standard', 100319, 7)
        """)
        
        # Child bot 100319: LONG, parent 10019
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, parent_bot_id)
            VALUES (100319, 'short gold_hedge', 'XAU/USDT:USDT', 'XAU/USDT:USDT', 'LONG', 1, 'IN TRADE',
                    'hedge_child', 10019)
        """)
        
        # Trades
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (10019, 0.125, 'ACTIVE', 'SHORT', 1)")
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (100319, 0.063, 'ACTIVE', 'LONG', 1)")
        
        # Exchange: net -0.186 (the step-8 fill happened while engine dead)
        exchange = MockExchange({
            'XAU/USDT:USDT': {'contracts': -0.186, 'net_qty': -0.186, 'side': 'short', 'unrealizedPnl': -4.83, 'entryPrice': 4540.46}
        })
        
        cfg = MockConfig()
        
        # Call the watchdog
        result = verify_netting_engagement(
            parent_bot_id=10019,
            parent_direction='SHORT',
            conn=conn,
            exchange=exchange,
            config=cfg
        )
        
        print(f"Result: {result}")
        
        # Assertions
        assert result['engaged'] == False, "Should NOT be engaged - netting diverged"
        assert result['freeze_parent'] == True, "Should freeze parent"
        assert result['reason'].startswith('netting_diverged'), f"Wrong reason: {result['reason']}"
        assert abs(result['virtual_net'] - (-0.062)) < 1e-6, f"Virtual net wrong: {result['virtual_net']}"
        assert abs(result['physical_net'] - (-0.186)) < 1e-6, f"Physical net wrong: {result['physical_net']}"
        assert abs(result['delta'] - (-0.124)) < 1e-6, f"Delta wrong: {result['delta']}"
        assert result['child_bot_id'] == 100319
        assert result['pair'] == 'XAU/USDT:USDT'
        
        print("✅ XAU incident test PASSED")
    
    conn = create_test_db()
    run_with_test_db(conn, _test)


def test_netting_ok_no_freeze():
    """
    TEST: Netting is within tolerance — should NOT freeze.
    
    Scenario:
    - Parent SHORT 0.100, Child LONG 0.050
    - Exchange net = -0.050 (parent 0.100 short - child 0.050 long = 0.050 short)
    - Virtual net = -0.050
    - Delta = 0.0 < tolerance
    - Expected: engaged=True, freeze_parent=False
    """
    def _test(conn):
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, hedge_child_bot_id, hedge_trigger_step)
            VALUES (20001, 'test parent', 'BTC/USDC:USDC', 'BTC/USDC:USDC', 'SHORT', 1, 'IN TRADE',
                    'standard', 20002, 5)
        """)
        
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, parent_bot_id)
            VALUES (20002, 'test child', 'BTC/USDC:USDC', 'BTC/USDC:USDC', 'LONG', 1, 'IN TRADE',
                    'hedge_child', 20001)
        """)
        
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (20001, 0.100, 'ACTIVE', 'SHORT', 1)")
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (20002, 0.050, 'ACTIVE', 'LONG', 1)")
        
        # Exchange net matches virtual
        exchange = MockExchange({
            'BTC/USDC:USDC': {'contracts': -0.050, 'net_qty': -0.050, 'side': 'short', 'unrealizedPnl': 10.0, 'entryPrice': 50000}
        })
        
        cfg = MockConfig()
        
        result = verify_netting_engagement(
            parent_bot_id=20001,
            parent_direction='SHORT',
            conn=conn,
            exchange=exchange,
            config=cfg
        )
        
        print(f"Result: {result}")
        
        assert result['engaged'] == True, "Should be engaged - netting matches"
        assert result['freeze_parent'] == False, "Should NOT freeze"
        assert result['reason'] == 'netting_ok', f"Wrong reason: {result['reason']}"
        assert abs(result['delta']) < 1e-6, f"Delta should be ~0: {result['delta']}"
        
        print("✅ Netting OK test PASSED")
    
    conn = create_test_db()
    run_with_test_db(conn, _test)


def test_three_bots_on_one_pair():
    """
    TEST: 3+ bots on same pair — sum-based check scales correctly.
    
    Scenario:
    - Bot A (parent): SHORT 0.200
    - Bot B (child of A): LONG 0.080
    - Bot C (unrelated): SHORT 0.050
    - Virtual net = -0.200 + 0.080 - 0.050 = -0.170
    - Exchange net = -0.170
    - Expected: engaged=True (all bots summed, not just parent/child)
    """
    def _test(conn):
        # Parent A with child B
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, hedge_child_bot_id, hedge_trigger_step)
            VALUES (30001, 'parent A', 'ETH/USDC:USDC', 'ETH/USDC:USDC', 'SHORT', 1, 'IN TRADE',
                    'standard', 30002, 4)
        """)
        
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, parent_bot_id)
            VALUES (30002, 'child B', 'ETH/USDC:USDC', 'ETH/USDC:USDC', 'LONG', 1, 'IN TRADE',
                    'hedge_child', 30001)
        """)
        
        # Unrelated bot C on same pair (no hedge relationship)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type)
            VALUES (30003, 'unrelated C', 'ETH/USDC:USDC', 'ETH/USDC:USDC', 'SHORT', 1, 'IN TRADE',
                    'standard')
        """)
        
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (30001, 0.200, 'ACTIVE', 'SHORT', 1)")
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (30002, 0.080, 'ACTIVE', 'LONG', 1)")
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (30003, 0.050, 'ACTIVE', 'SHORT', 1)")
        
        # Exchange matches sum of all three
        exchange = MockExchange({
            'ETH/USDC:USDC': {'contracts': -0.170, 'net_qty': -0.170, 'side': 'short', 'unrealizedPnl': 5.0, 'entryPrice': 3000}
        })
        
        cfg = MockConfig()
        
        # Call for parent A
        result = verify_netting_engagement(
            parent_bot_id=30001,
            parent_direction='SHORT',
            conn=conn,
            exchange=exchange,
            config=cfg
        )
        
        print(f"Result: {result}")
        
        assert result['engaged'] == True, "Should be engaged - all 3 bots summed correctly"
        assert result['freeze_parent'] == False
        assert abs(result['virtual_net'] - (-0.170)) < 1e-6, f"Virtual net should be -0.170: {result['virtual_net']}"
        assert abs(result['physical_net'] - (-0.170)) < 1e-6
        
        print("✅ Three bots test PASSED")
    
    conn = create_test_db()
    run_with_test_db(conn, _test)


def test_verify_all_pairs_netting():
    """
    TEST: Periodic function checks all pairs with hedge children.
    """
    def _test(conn):
        # Pair 1: XAU - diverged (should freeze)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, hedge_child_bot_id, hedge_trigger_step)
            VALUES (40001, 'xau parent', 'XAU/USDT:USDT', 'XAU/USDT:USDT', 'SHORT', 1, 'IN TRADE',
                    'standard', 40002, 7)
        """)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, parent_bot_id)
            VALUES (40002, 'xau child', 'XAU/USDT:USDT', 'XAU/USDT:USDT', 'LONG', 1, 'IN TRADE',
                    'hedge_child', 40001)
        """)
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (40001, 0.100, 'ACTIVE', 'SHORT', 1)")
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (40002, 0.020, 'ACTIVE', 'LONG', 1)")
        
        # Pair 2: SOL - OK
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, hedge_child_bot_id, hedge_trigger_step)
            VALUES (50001, 'sol parent', 'SOL/USDC:USDC', 'SOL/USDC:USDC', 'SHORT', 1, 'IN TRADE',
                    'standard', 50002, 5)
        """)
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type, parent_bot_id)
            VALUES (50002, 'sol child', 'SOL/USDC:USDC', 'SOL/USDC:USDC', 'LONG', 1, 'IN TRADE',
                    'hedge_child', 50001)
        """)
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (50001, 0.500, 'ACTIVE', 'SHORT', 1)")
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (50002, 0.200, 'ACTIVE', 'LONG', 1)")
        
        exchange = MockExchange({
            'XAU/USDT:USDT': {'contracts': -0.200, 'net_qty': -0.200, 'side': 'short', 'unrealizedPnl': -10.0, 'entryPrice': 4500},  # diverged: virtual -0.080 vs -0.200
            'SOL/USDC:USDC': {'contracts': -0.300, 'net_qty': -0.300, 'side': 'short', 'unrealizedPnl': 5.0, 'entryPrice': 90},     # OK: virtual -0.300
        })
        
        cfg = MockConfig()
        
        results = verify_all_pairs_netting(conn, exchange, cfg)
        
        print(f"Results: {results}")
        
        assert len(results) == 2, "Should check both pairs"
        
        xau_result = next(r for r in results if r['pair'] == 'XAU/USDT:USDT')
        sol_result = next(r for r in results if r['pair'] == 'SOL/USDC:USDC')
        
        assert xau_result['freeze_parent'] == True, "XAU should freeze"
        assert xau_result['reason'].startswith('netting_diverged')
        assert sol_result['engaged'] == True, "SOL should be OK"
        assert sol_result['freeze_parent'] == False
        
        print("✅ verify_all_pairs_netting test PASSED")
    
    conn = create_test_db()
    run_with_test_db(conn, _test)


def test_no_hedge_child_configured():
    """
    TEST: Parent without hedge child — freezes with correct reason.
    """
    def _test(conn):
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status,
                              bot_type)
            VALUES (60001, 'lonely parent', 'LINK/USDC:USDC', 'LINK/USDC:USDC', 'SHORT', 1, 'IN TRADE',
                    'standard')
        """)
        conn.execute("INSERT INTO trades (bot_id, open_qty, cycle_phase, position_side, cycle_id) VALUES (60001, 0.100, 'ACTIVE', 'SHORT', 1)")
        
        exchange = MockExchange({
            'LINK/USDC:USDC': {'contracts': -0.100, 'net_qty': -0.100, 'side': 'short', 'unrealizedPnl': 0, 'entryPrice': 10}
        })
        
        cfg = MockConfig()
        
        result = verify_netting_engagement(
            parent_bot_id=60001,
            parent_direction='SHORT',
            conn=conn,
            exchange=exchange,
            config=cfg
        )
        
        print(f"Result: {result}")
        
        assert result['engaged'] == False
        assert result['freeze_parent'] == True
        assert result['reason'] == 'no_hedge_child_bot_id'
        assert result['child_bot_id'] is None
        
        print("✅ No hedge child test PASSED")
    
    conn = create_test_db()
    run_with_test_db(conn, _test)


if __name__ == '__main__':
    test_xau_incident_scenario()
    test_netting_ok_no_freeze()
    test_three_bots_on_one_pair()
    test_verify_all_pairs_netting()
    test_no_hedge_child_configured()
    print("\n🎉 ALL TESTS PASSED")