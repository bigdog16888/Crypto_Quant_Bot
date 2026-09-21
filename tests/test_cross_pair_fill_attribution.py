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
2. A bot with fills belonging to ITS OWN pair should show fills correctly
3. A bot with fills in BOTH its own pair and another pair should only see its own pair
"""
import pytest
import tempfile
import os

from engine.position_ledger import compute_bot_position, _fetch_fills_for_bot, compute_pair_position
from engine.database import init_db


def test_bot_with_only_wrong_pair_fills(temp_db):
    """Test 1: Bot configured for Pair A, but has fills for Pair B → should see 0 fills."""
    conn = temp_db
    cursor = conn.cursor()

    # Bot configured for BTC/USDC:USDC (LONG)
    cursor.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
        VALUES (100318, 'test bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'Scanning', 'standard')
    """)

    # Fills for SUI/USDC:USDC (different pair!) - all SELL (would create huge short if misattributed)
    cursor.executemany("""
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


def test_bot_with_only_correct_pair_fills(temp_db):
    """Test 2: Bot configured for Pair A, has fills for Pair A → should see fills correctly."""
    conn = temp_db
    cursor = conn.cursor()

    # Bot configured for BTC/USDC:USDC (LONG)
    cursor.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
        VALUES (10016, 'long btc', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'REQUIRE_MANUAL_PROOF', 'standard')
    """)

    # Fills for BTC/USDC:USDC (correct pair)
    cursor.executemany("""
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


def test_bot_with_mixed_pair_fills(temp_db):
    """Test 3: Bot has fills for BOTH its pair and another pair → should only see its own."""
    conn = temp_db
    cursor = conn.cursor()

    # Bot configured for BTC/USDC:USDC (LONG)
    cursor.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
        VALUES (999, 'mixed bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'Scanning', 'standard')
    """)

    # Mix of BTC/USDC:USDC (correct) and ETH/USDC:USDC (wrong) fills
    cursor.executemany("""
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


def test_normalized_pair_matching(temp_db):
    """Test 4: Normalized pair matching works (CCXT format 'BTC/USDC:USDC' → 'BTCUSDC')."""
    conn = temp_db
    cursor = conn.cursor()

    # Bot configured with normalized_pair only
    cursor.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
        VALUES (1001, 'test live', 'BTCUSDT', 'BTCUSDC', 'LONG', 'REQUIRE_MANUAL_PROOF', 'standard')
    """)

    # Fills in CCXT format (BTC/USDC:USDC) which normalizes to BTCUSDC
    cursor.executemany("""
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


def test_original_bug_regression(temp_db):
    """Test 5: Reproduce the exact original bug scenario (bot 100318)."""
    conn = temp_db
    cursor = conn.cursor()

    # Exact bot 100318 config
    cursor.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
        VALUES (100318, 'test bot', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'Scanning', 'standard')
    """)

    # Exact fills from the bug (SUI/USDC:USDC)
    cursor.executemany("""
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


def test_pair_position_aggregation(temp_db):
    """Test 6: compute_pair_position correctly aggregates only valid fills per pair."""
    conn = temp_db
    cursor = conn.cursor()

    # Two bots on BTC/USDC:USDC
    cursor.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
        VALUES (10016, 'long btc', 'BTC/USDC:USDC', 'BTCUSDC', 'LONG', 'Scanning', 'standard')
    """)
    cursor.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
        VALUES (10022, 'short btc', 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT', 'Scanning', 'standard')
    """)
    # One bot on SUI/USDC:USDC (the one that had cross-contamination)
    cursor.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status, bot_type)
        VALUES (10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', 'Scanning', 'standard')
    """)

    # Use a checkpoint timestamp BEFORE the fills we'll insert
    checkpoint_ts = 1789210000

    # active_positions checkpoints for the bots - SET last_checked explicitly!
    cursor.execute("""
        INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
        VALUES (10016, 'BTCUSDC', 'LONG', 0.0, 50000.0, ?)
    """, (checkpoint_ts,))
    cursor.execute("""
        INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
        VALUES (10022, 'BTCUSDC', 'SHORT', 0.0, 50000.0, ?)
    """, (checkpoint_ts,))
    cursor.execute("""
        INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked)
        VALUES (10018, 'SUIUSDC', 'LONG', 0.0, 1.0, ?)
    """, (checkpoint_ts,))

    # Bot 10016: BTC fills (after checkpoint timestamp)
    cursor.executemany("""
        INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        ('5001', 'CQB_10016_E1', 'BTC/USDC:USDC', 'BUY', 0.1, 50000.0, 'backfill', 10016, 'entry', 1, 1, checkpoint_ts + 100, 1789211813),
        ('5002', 'CQB_10016_TP1', 'BTC/USDC:USDC', 'SELL', 0.05, 51000.0, 'backfill', 10016, 'tp', 1, 1, checkpoint_ts + 200, 1789211813),
    ])

    # Bot 10022: BTC fills (SHORT) - after checkpoint
    cursor.executemany("""
        INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        ('6001', 'CQB_10022_E1', 'BTC/USDC:USDC', 'SELL', 0.05, 50000.0, 'backfill', 10022, 'entry', 1, 1, checkpoint_ts + 100, 1789211813),
        ('6002', 'CQB_10022_TP1', 'BTC/USDC:USDC', 'BUY', 0.02, 49000.0, 'backfill', 10022, 'tp', 1, 1, checkpoint_ts + 200, 1789211813),
    ])

    # Bot 10018: SUI fills (should NOT appear in BTC pair)
    cursor.executemany("""
        INSERT INTO exchange_fills (exchange_order_id, client_order_id, symbol, side, qty, price, source, bot_id, order_type, step, cycle_id, fill_ts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        ('7001', 'CQB_10018_E1', 'SUI/USDC:USDC', 'BUY', 100.0, 1.0, 'backfill', 10018, 'entry', 1, 1, checkpoint_ts + 100, 1789211813),
    ])
    conn.commit()

    # Debug: verify fills are in the DB
    for bot_id in [10016, 10022, 10018]:
        rows = cursor.execute("SELECT symbol, side, qty FROM exchange_fills WHERE bot_id = ? AND fill_ts > ?", (bot_id, checkpoint_ts)).fetchall()
        print(f"Bot {bot_id} fills after checkpoint: {rows}")

    # Compute BTC pair - should only include 10016 and 10022
    btc_pos = compute_pair_position('BTC/USDC:USDC', conn)

    # Bot 10016: base 0.0 LONG + delta (BUY 0.1 - SELL 0.05) = +0.05
    # Bot 10022: base 0.0 SHORT + delta (SELL 0.05 - BUY 0.02) = -0.03 (short: sell adds, buy reduces)
    # Pair net = 0.05 + (-0.03) = 0.02
    print(f"BTC pair: net_qty={btc_pos.net_qty}, bots={len(btc_pos.bots)}")
    for bp in btc_pos.bots:
        print(f"  Bot {bp.bot_id}: net_qty={bp.net_qty}, fills={bp.fills_count}, direction={bp.direction}")

    # Compute SUI pair - should only include 10018
    sui_pos = compute_pair_position('SUI/USDC:USDC', conn)
    print(f"SUI pair: net_qty={sui_pos.net_qty}, bots={len(sui_pos.bots)}")
    for bp in sui_pos.bots:
        print(f"  Bot {bp.bot_id}: net_qty={bp.net_qty}, fills={bp.fills_count}, direction={bp.direction}")

    # Both pairs should have their respective bots
    assert len(btc_pos.bots) == 2, f"Expected 2 bots in BTC pair, got {len(btc_pos.bots)}"
    assert len(sui_pos.bots) == 1, f"Expected 1 bot in SUI pair, got {len(sui_pos.bots)}"
    
    # SUI pair bot should have the fills (delta from checkpoint)
    assert sui_pos.bots[0].bot_id == 10018
    # Net qty: base 0.0 + delta BUY 100 = 100.0
    assert sui_pos.bots[0].net_qty == 100.0, f"SUI bot net_qty should be 100.0, got {sui_pos.bots[0].net_qty}"
    assert sui_pos.bots[0].fills_count == 1, f"SUI bot should have 1 fill, got {sui_pos.bots[0].fills_count}"


if __name__ == '__main__':
    print("=" * 60)
    print("REGRESSION TEST: Cross-pair fill attribution bug")
    print("=" * 60)
    print("\nRun with: pytest tests/test_cross_pair_fill_attribution.py -v")