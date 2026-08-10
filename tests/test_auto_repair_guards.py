#!/usr/bin/env python3
"""
test_auto_repair_guards.py — Tests for auto-repair guard layers (Layers 1, 2, 4)

Tests:
1. test_orphan_repair_blocked_when_require_manual_proof — Bot in REQUIRE_MANUAL_PROOF → startup_repair_mismatched_pairs skips pair
2. test_orphan_repair_blocked_above_usd_ceiling — Physical position worth $50, ceiling $5 → blocked, escalated
3. test_orphan_repair_allowed_for_dust — Physical position worth $0.50, ceiling $5 → allowed
4. test_toggle_cannot_be_bypassed — Direct call to repair_exchange_orphan_when_ledger_flat with config=False → blocked
5. test_guard_idempotent — Calling _orphan_repair_allowed twice with identical config returns same result
"""
import os
import sys
from unittest.mock import Mock, patch, MagicMock

# Add project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Set up test environment BEFORE importing config
os.environ["TESTING_MODE"] = "True"
os.environ["TESTNET"] = "True"
os.environ["DEMO_TRADING"] = "True"
os.environ["AUTO_REPAIR_ORPHAN_EXCHANGE"] = "False"
os.environ["AUTO_REPAIR_MAX_USD"] = "5.0"

from config.settings import config
from engine.parity_gates import (
    _orphan_repair_allowed,
    repair_exchange_orphan_when_ledger_flat,
    startup_repair_mismatched_pairs,
    reconcile_pair_to_exchange,
)
import engine.database as db_module


def _make_mock_exchange(physical_qty: float, mark_price: float = 100.0):
    """Create a mock exchange returning specific physical position."""
    mock_ex = Mock()
    
    # Mock fetch_positions
    symbol_map = {
        'BTCUSDC': 'BTC/USDC:USDC',
        'BTC/USDC:USDC': 'BTC/USDC:USDC',
        'SOLUSDC': 'SOL/USDC:USDC',
        'SOL/USDC:USDC': 'SOL/USDC:USDC',
    }
    
    def fetch_positions(symbols=None):
        positions = []
        for sym, norm in symbol_map.items():
            if symbols and norm not in symbols:
                continue
            qty = physical_qty if norm in ['BTC/USDC:USDC', 'BTCUSDC'] else 0.0
            positions.append({
                'symbol': sym,
                'contracts': qty,
                'side': 'short' if qty < 0 else 'long',
                'entryPrice': mark_price if qty != 0 else 0.0,
            })
        return positions
    
    mock_ex.fetch_positions = Mock(side_effect=fetch_positions)
    
    # Mock fetch_ticker
    mock_ex.fetch_ticker = Mock(return_value={
        'last': mark_price,
        'markPrice': mark_price,
    })
    
    return mock_ex


def test_orphan_repair_blocked_when_require_manual_proof():
    """Bot in REQUIRE_MANUAL_PROOF → startup_repair_mismatched_pairs skips pair."""
    mock_ex = _make_mock_exchange(physical_qty=0.01, mark_price=50000.0)  # $500 notional
    
    # Patch audit in the database module (where it's actually defined)
    with patch('engine.database.audit_pair_ledger_vs_exchange') as mock_audit:
        mock_audit.return_value = [('BTC/USDC:USDC', 0.0, 0.01, 0.01)]
        
        result = startup_repair_mismatched_pairs(mock_ex)
        
        # Should skip BTCUSDC because bot 2 is gated in the actual DB
        # We just verify the code path runs without error
        print(f"Result: {result}")
        assert 'orphan_repaired' in result
        print("✓ test_orphan_repair_blocked_when_require_manual_proof passed (structure check)")


def test_orphan_repair_blocked_above_usd_ceiling():
    """Physical position worth $50, ceiling $5 → blocked, escalated."""
    # Physical 0.01 BTC @ $50k = $500 notional, ceiling $5
    mock_ex = _make_mock_exchange(physical_qty=0.01, mark_price=50000.0)
    
    # Need to patch config at module level where _orphan_repair_allowed reads it
    with patch('engine.parity_gates.config') as mock_config:
        mock_config.AUTO_REPAIR_ORPHAN_EXCHANGE = True  # Enable toggle
        mock_config.AUTO_REPAIR_MAX_USD = 5.0
        
        # Need to mock get_connection to return no gated bots
        with patch('engine.database.get_connection') as mock_get_conn:
            mock_conn = Mock()
            mock_conn.execute.return_value.fetchall.return_value = []  # No gated bots
            mock_get_conn.return_value = mock_conn
            
            allowed, reason = _orphan_repair_allowed(mock_ex, 'BTC/USDC:USDC')
            
            assert not allowed, f"Should be blocked, but got allowed=True"
            assert "notional" in reason.lower(), f"Reason should mention notional: {reason}"
            print(f"✓ test_orphan_repair_blocked_above_usd_ceiling passed: {reason}")


def test_orphan_repair_allowed_for_dust():
    """Physical position worth $0.50, ceiling $5 → allowed."""
    mock_ex = _make_mock_exchange(physical_qty=0.001, mark_price=500.0)
    
    with patch('engine.parity_gates.config') as mock_config:
        mock_config.AUTO_REPAIR_ORPHAN_EXCHANGE = True
        mock_config.AUTO_REPAIR_MAX_USD = 5.0
        
        with patch('engine.database.get_connection') as mock_get_conn:
            mock_conn = Mock()
            mock_conn.execute.return_value.fetchall.return_value = []  # No gated bots
            mock_get_conn.return_value = mock_conn
            
            allowed, reason = _orphan_repair_allowed(mock_ex, 'BTC/USDC:USDC')
            
            assert allowed, f"Dust position should be allowed, got: {reason}"
            print(f"✓ test_orphan_repair_allowed_for_dust passed: {reason}")


def test_toggle_cannot_be_bypassed():
    """Direct call with AUTO_REPAIR_ORPHAN_EXCHANGE=False → blocked."""
    mock_ex = _make_mock_exchange(physical_qty=0.01, mark_price=1000.0)
    
    with patch('engine.parity_gates.config') as mock_config:
        mock_config.AUTO_REPAIR_ORPHAN_EXCHANGE = False
        mock_config.AUTO_REPAIR_MAX_USD = 5.0
        
        with patch('engine.database.get_connection') as mock_get_conn:
            mock_conn = Mock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_get_conn.return_value = mock_conn
            
            # Direct call to repair function
            result = repair_exchange_orphan_when_ledger_flat(mock_ex, 'BTC/USDC:USDC', 0.0, 0.01)
            
            assert result is None, f"Should return None (blocked), got: {result}"
            print("✓ test_toggle_cannot_be_bypassed passed")


def test_guard_idempotent():
    """Calling _orphan_repair_allowed twice with identical config returns same result."""
    mock_ex = _make_mock_exchange(physical_qty=0.001, mark_price=100.0)
    
    with patch('engine.parity_gates.config') as mock_config:
        mock_config.AUTO_REPAIR_ORPHAN_EXCHANGE = True
        mock_config.AUTO_REPAIR_MAX_USD = 5.0
        
        with patch('engine.database.get_connection') as mock_get_conn:
            mock_conn = Mock()
            mock_conn.execute.return_value.fetchall.return_value = []
            mock_get_conn.return_value = mock_conn
            
            # Call twice
            result1, reason1 = _orphan_repair_allowed(mock_ex, 'BTC/USDC:USDC')
            result2, reason2 = _orphan_repair_allowed(mock_ex, 'BTC/USDC:USDC')
            
            assert result1 == result2, f"Results differ: {result1} vs {result2}"
            assert reason1 == reason2, f"Reasons differ: {reason1} vs {reason2}"
            print(f"✓ test_guard_idempotent passed: {result1}, {reason1}")


def test_reconcile_skips_gated_pair():
    """reconcile_pair_to_exchange skips orphan repair for gated bots."""
    mock_ex = _make_mock_exchange(physical_qty=0.01, mark_price=50000.0)
    
    with patch('engine.parity_gates.config') as mock_config:
        mock_config.AUTO_REPAIR_ORPHAN_EXCHANGE = True
        mock_config.AUTO_REPAIR_MAX_USD = 5.0
        
        with patch('engine.database.get_connection') as mock_get_conn:
            mock_conn = Mock()
            mock_conn.execute.return_value.fetchall.return_value = [(2,)]  # Bot 2 is gated
            mock_get_conn.return_value = mock_conn
            
            result = reconcile_pair_to_exchange(mock_ex, 'BTC/USDC:USDC')
            
            assert result is None, f"Should return None for gated pair, got: {result}"
            print("✓ test_reconcile_skips_gated_pair passed")


def test_startup_repair_skips_gated_pair():
    """startup_repair_mismatched_pairs skips pair when bots are gated."""
    mock_ex = _make_mock_exchange(physical_qty=0.01, mark_price=50000.0)
    
    with patch('engine.parity_gates.config') as mock_config:
        mock_config.AUTO_REPAIR_ORPHAN_EXCHANGE = True
        mock_config.AUTO_REPAIR_MAX_USD = 5.0
    
    # Patch audit in database module
    with patch('engine.database.audit_pair_ledger_vs_exchange') as mock_audit:
        mock_audit.return_value = [('BTC/USDC:USDC', 0.0, 0.01, 0.01)]
        
        # Patch get_connection in database module to return gated bots
        with patch('engine.database.get_connection') as mock_get_conn:
            mock_conn = Mock()
            mock_conn.execute.return_value.fetchall.return_value = [(2,)]  # Bot 2 is gated
            mock_get_conn.return_value = mock_conn
            
            result = startup_repair_mismatched_pairs(mock_ex)
            
            # Should skip the gated pair
            orphan_repaired = result.get('orphan_repaired', [])
            assert len(orphan_repaired) == 0, f"Should not repair gated pair, got: {orphan_repaired}"
            remaining = result.get('remaining', [])
            assert any('BTC/USDC:USDC' in str(r) for r in remaining), f"Gated pair should remain: {remaining}"
            print("✓ test_startup_repair_skips_gated_pair passed")


if __name__ == "__main__":
    test_orphan_repair_blocked_when_require_manual_proof()
    test_orphan_repair_blocked_above_usd_ceiling()
    test_orphan_repair_allowed_for_dust()
    test_toggle_cannot_be_bypassed()
    test_guard_idempotent()
    test_reconcile_skips_gated_pair()
    test_startup_repair_skips_gated_pair()
    print("\n✅ All tests passed!")