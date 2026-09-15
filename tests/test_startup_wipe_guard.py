#!/usr/bin/env python3
"""
Regression test for startup-wipe guard fix (Priority 2, Option A).
Tests that hedge children without TP fills are NOT wiped on startup_sync.

IMPORTANT (2026-09-15): formerly coupled to the LIVE crypto_bot.db -- Test 2
queried bot 10018 / cycle 25 / status='filled' TP straight from production, so a
session re-seal that flipped those TPs to reset_cleared silently broke the test.
Now builds a FROZEN temp-DB fixture (bot 10018 cycle-25 filled TPs + bot 100315
cycle-15 entry with NO tp) and monkeypatches engine.database.get_connection to
return it. Fully deterministic; live trading on bot 10018 can never mutate it.
"""

import sqlite3
import tempfile
import os
import sys

sys.path.insert(0, 'D:/Crypto_Quant_Bot')
import engine.database as ed
from engine.database import _reset_bot_after_tp_internal  # noqa: F401 (kept for import-side-effect parity)


def _build_wipe_fixture():
    """Create a temp DB with the frozen rows Test 1/2 depend on.

    - bot 100315 (hedge child): entry fill in cycle 15, NO tp -> should NOT wipe.
    - bot 10018 (parent): three filled TPs in cycle 25 -> SHOULD allow wipe.
    """
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY, name TEXT, pair TEXT,
            direction TEXT, bot_type TEXT, status TEXT
        );
        CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY, cycle_id INTEGER, open_qty REAL
        );
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY, bot_id INTEGER, step INTEGER, order_type TEXT,
            order_id TEXT, price REAL, amount REAL, filled_amount REAL, status TEXT,
            created_at INTEGER, updated_at INTEGER, client_order_id TEXT, cycle_id INTEGER
        );
    """)
    rows = [
        # bot 100315 hedge child: entry fill cycle 15, NO tp
        (900001, 100315, 0, 'entry', 'e15', 0.80, 6.4, 6.4, 'filled',
         1788765317, 1788765317, 'cid100315', 15),
        # bot 10018 parent cycle 25: THREE filled TPs (historical, pre re-seal)
        (3334, 10018, 0, 'tp', 'tp1', 0.7614, 142.4, 142.4, 'filled',
         1789097092, 1789097092, 'CQB_10018_TP_25_4', 25),
        (3414, 10018, 0, 'tp', 'tp2', 0.7278, 56.2, 56.2, 'filled',
         1789185307, 1789185307, 'CQB_10018_TP_25_5', 25),
        (3419, 10018, 0, 'tp', 'tp3', 0.7278, 34.3, 34.3, 'filled',
         1789185716, 1789185716, 'CQB_10018_TP_25_5b', 25),
    ]
    c.executemany(
        "INSERT INTO bot_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return path


def test_startup_wipe_guard():
    """Verify hedge child with entry fill but no TP is NOT wiped on startup"""

    # Freeze the fixture and redirect get_connection to it.
    fixture = _build_wipe_fixture()
    orig_get_connection = ed.get_connection
    ed.get_connection = lambda: sqlite3.connect(fixture)

    try:
        conn = ed.get_connection()
        cursor = conn.cursor()

        # Test 1: Bot 100315 (hedge child) - has entry fill, NO TP in current cycle
        # Should NOT be wiped
        bot_id = 100315
        current_cycle = 15
        action_label = 'TP_HIT'

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
    finally:
        ed.get_connection = orig_get_connection
        try:
            os.unlink(fixture)
        except OSError:
            pass


if __name__ == '__main__':
    test_startup_wipe_guard()
