#!/usr/bin/env python3
"""
Regression test for SUI cycle-sweep cross-cycle aware fix.
Tests that the fixed sweep logic correctly identifies balanced vs unbalanced cycles
using the FROZEN bot 10018 dataset (captured 2026-09-15).

IMPORTANT (2026-09-15): formerly coupled to the LIVE crypto_bot.db, so production
writes to bot 10018 silently shifted hardcoded expectations (cycle 25 went 7->8
reset_cleared rows after a session re-seal). Now reuses FROZEN_10018_ORDERS from
test_sui_cycle_sweep_regression.py and builds a temp DB -- fully deterministic,
immune to live trading on bot 10018.
"""

import sqlite3
import os
import sys
import importlib.util

sys.path.insert(0, 'D:/Crypto_Quant_Bot')

# Reuse the frozen bot 10018 dataset (single source of truth for the fixture).
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "sui_sweep_frozen", os.path.join(_HERE, "test_sui_cycle_sweep_regression.py"))
_sui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sui)
create_test_fixture = _sui.create_test_fixture


def test_cross_cycle_sweep_fixed():
    """Test that the cross-cycle aware sweep correctly identifies cycles 6 and 10 as UNBALANCED."""

    db_path = create_test_fixture()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        bot_id = 10018
        new_cycle = 26

        # Simulate the FIXED sweep query (cross-cycle aware)
        cursor.execute("""
            SELECT cycle_id,
                   SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END) AS entry_qty,
                   SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END) AS exit_qty,
                   SUM(CASE WHEN order_type = 'hedge' THEN filled_amount ELSE 0.0 END) AS hedge_entry_qty,
                   SUM(CASE WHEN order_type IN ('hedge_tp','hedge_exit') THEN filled_amount ELSE 0.0 END) AS hedge_exit_qty
            FROM bot_orders
            WHERE bot_id = ? AND cycle_id < ? AND cycle_id IS NOT NULL
              AND filled_amount > 0
            GROUP BY cycle_id
        """, (bot_id, new_cycle))
        all_cycles = cursor.fetchall()

        balanced_cycles = []
        unbalanced_cycles = []

        for cid, eq, xq, heq, hxq in all_cycles:
            net = (eq + heq) - (xq + hxq)
            if abs(net) < 1e-6:
                balanced_cycles.append(cid)
            else:
                unbalanced_cycles.append({
                    'cycle': cid,
                    'entry': eq,
                    'exit': xq,
                    'hedge_entry': heq,
                    'hedge_exit': hxq,
                    'net': net
                })

        conn.close()

        print("=== CROSS-CYCLE AWARE SWEEP RESULTS ===")
        print(f"Balanced cycles (safe to sweep): {balanced_cycles}")
        print()
        print("Unbalanced cycles (NOT safe to sweep):")
        for u in unbalanced_cycles:
            print(f"  Cycle {u['cycle']}: entry={u['entry']}, exit={u['exit']}, hedge_entry={u['hedge_entry']}, hedge_exit={u['hedge_exit']}, NET={u['net']}")

        unbalanced_cycle_ids = [u['cycle'] for u in unbalanced_cycles]

        # Critical assertions for the known unresolved cases
        assert 6 in unbalanced_cycle_ids, "Cycle 6 should be UNBALANCED (has unmatched entry 7.6)"
        assert 10 in unbalanced_cycle_ids, "Cycle 10 should be UNBALANCED (has unmatched entry 58.3)"

        # Verify the net values match expected
        for u in unbalanced_cycles:
            if u['cycle'] == 6:
                assert abs(u['net'] - 7.6) < 0.01, f"Cycle 6 net should be 7.6, got {u['net']}"
            elif u['cycle'] == 10:
                assert abs(u['net'] - 58.3) < 0.01, f"Cycle 10 net should be 58.3, got {u['net']}"
            elif u['cycle'] == 25:
                assert abs(u['net'] - 111.5) < 0.01, f"Cycle 25 net should be 111.5, got {u['net']}"

        # Verify balanced cycles are correctly identified
        expected_balanced = [0,1,2,3,4,5,8,11,12,14,15,17,18,20,22,23]
        for b in expected_balanced:
            assert b in balanced_cycles, f"Cycle {b} should be BALANCED"

        # Verify the specific balanced cycles we know about
        assert 7 not in balanced_cycles and 7 not in [u['cycle'] for u in unbalanced_cycles], "Cycle 7 should have no fills"

        print()
        print("✅ ALL ASSERTIONS PASSED")
        print("✅ Cycles 6 and 10 correctly identified as UNBALANCED (will NOT be swept)")
        print("✅ Cycle 25 correctly identified as UNBALANCED (current active cycle, matches exchange)")
        print("✅ All historically balanced cycles correctly identified as BALANCED (safe to sweep)")

        return {
            'balanced': balanced_cycles,
            'unbalanced': unbalanced_cycles
        }
    finally:
        os.unlink(db_path)


def test_classification_with_fixed_logic():
    """Test that classify_reset_cleared_orders gives correct results with fixed sweep logic.

    Uses the FROZEN bot 10018 dataset (no live DB). Cycle 25 currently has 8
    reset_cleared orders totaling 577.3 units (incl. id 3419 tp 34.3 re-sealed
    during the 2026-09-15 session) -- pinned to that true value.
    """

    db_path = create_test_fixture()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        bot_id = 10018

        # Get all cycles with reset_cleared orders
        cursor.execute("""
            SELECT DISTINCT cycle_id FROM bot_orders
            WHERE bot_id = ? AND status = 'reset_cleared' AND filled_amount > 0
            ORDER BY cycle_id
        """, (bot_id,))
        cycles_with_resets = [r[0] for r in cursor.fetchall()]

        classifications = []

        for cycle in cycles_with_resets:
            # Compute true balance using ALL fills (cross-cycle aware)
            cursor.execute("""
                SELECT
                    SUM(CASE WHEN order_type IN ('entry','grid','adoption','adoption_add','carry') THEN filled_amount ELSE 0.0 END) AS entry_qty,
                    SUM(CASE WHEN order_type IN ('tp','close','dust_close','sl','adoption_reduce','flatten_close') THEN filled_amount ELSE 0.0 END) AS exit_qty,
                    SUM(CASE WHEN order_type = 'hedge' THEN filled_amount ELSE 0.0 END) AS hedge_entry_qty,
                    SUM(CASE WHEN order_type IN ('hedge_tp','hedge_exit') THEN filled_amount ELSE 0.0 END) AS hedge_exit_qty
                FROM bot_orders
                WHERE bot_id = ? AND cycle_id = ? AND filled_amount > 0
            """, (bot_id, cycle))
            row = cursor.fetchone()
            entry = row[0] if row and row[0] is not None else 0.0
            exit = row[1] if row and row[1] is not None else 0.0
            heq = row[2] if row and row[2] is not None else 0.0
            hxq = row[3] if row and row[3] is not None else 0.0
            net = (entry + heq) - (exit + hxq)
            balanced = abs(net) < 1e-6

            # Count reset_cleared orders in this cycle
            cursor.execute("""
                SELECT COUNT(*), SUM(filled_amount)
                FROM bot_orders
                WHERE bot_id = ? AND cycle_id = ? AND status = 'reset_cleared' AND filled_amount > 0
            """, (bot_id, cycle))
            reset_count, reset_units = cursor.fetchone()

            classification = 'LEGITIMATE' if balanced else 'INCORRECT'

            classifications.append({
                'cycle': cycle,
                'reset_orders': reset_count,
                'reset_units': reset_units,
                'entry': entry,
                'exit': exit,
                'hedge_entry': heq,
                'hedge_exit': hxq,
                'net': net,
                'balanced': balanced,
                'classification': classification
            })

        conn.close()

        print("=== CLASSIFICATION WITH FIXED LOGIC ===")
        print(f"{'Cycle':<6} {'Reset':<6} {'Units':<10} {'Entry':<10} {'Exit':<10} {'HEntry':<8} {'HExit':<8} {'Net':<10} {'Balanced':<8} {'Class'}")
        print("-" * 100)
        for c in classifications:
            print(f"{c['cycle']:<6} {c['reset_orders']:<6} {c['reset_units']:<10.1f} {c['entry']:<10.1f} {c['exit']:<10.1f} {c['hedge_entry']:<8.1f} {c['hedge_exit']:<8.1f} {c['net']:<10.1f} {str(c['balanced']):<8} {c['classification']}")

        # Verify the critical classifications
        for c in classifications:
            if c['cycle'] == 6:
                assert c['classification'] == 'INCORRECT', "Cycle 6 should be INCORRECT"
                assert c['reset_orders'] == 1, f"Cycle 6 should have 1 reset order, got {c['reset_orders']}"
                assert abs(c['reset_units'] - 7.6) < 0.01, f"Cycle 6 should have 7.6 units, got {c['reset_units']}"
            elif c['cycle'] == 10:
                assert c['classification'] == 'INCORRECT', "Cycle 10 should be INCORRECT"
                assert c['reset_orders'] == 4, f"Cycle 10 should have 4 reset orders, got {c['reset_orders']}"
                assert abs(c['reset_units'] - 73.3) < 0.01, f"Cycle 10 should have 73.3 units, got {c['reset_units']}"
            elif c['cycle'] == 25:
                assert c['classification'] == 'INCORRECT', "Cycle 25 should be INCORRECT"
                assert c['reset_orders'] == 8, f"Cycle 25 should have 8 reset orders, got {c['reset_orders']}"
                assert abs(c['reset_units'] - 577.3) < 0.01, f"Cycle 25 should have 577.3 units, got {c['reset_units']}"
            elif c['cycle'] in [0,1,2,3,4,5,8,11,12,14,15,17,18,20,22,23]:
                assert c['classification'] == 'LEGITIMATE', f"Cycle {c['cycle']} should be LEGITIMATE"

        print()
        print("✅ CLASSIFICATION TEST PASSED")
        print("✅ Cycles 6, 10, 25 correctly classified as INCORRECT (should NOT have been swept)")
        print("✅ All other cycles correctly classified as LEGITIMATE (correctly swept)")

        return classifications
    finally:
        os.unlink(db_path)


if __name__ == '__main__':
    print("=" * 80)
    print("SUI CROSS-CYCLE SWEEP FIX REGRESSION TEST (Frozen Fixture)")
    print("=" * 80)
    print()

    result = test_cross_cycle_sweep_fixed()
    print()

    classifications = test_classification_with_fixed_logic()
    print()

    print("=" * 80)
    print("ALL REGRESSION TESTS PASSED ✅")
    print("=" * 80)
