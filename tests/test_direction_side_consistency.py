"""
Test: bots.direction vs trades.position_side divergence check (task t_fc30b679).

get_pair_virtual_net (engine/database.py) signs each trades row by
trades.position_side; get_bot_signed_contribution (engine/parity_gates.py)
signs by bots.direction. If the two ever diverge for a bot with open_qty != 0,
pair-level net and bot-level contribution silently disagree.

engine.integrity.check_direction_side_consistency() is the read-only monitor
that NOTICEs that divergence. These tests verify:
  1. Divergent bot (direction=LONG, position_side=SHORT, open_qty>0) -> fires.
  2. Consistent bots -> silent.
  3. open_qty == 0 rows are out of scope -> silent even if divergent.
  4. Sign-convention mirroring: legacy position_side='BOTH' counts as LONG
     (matches get_pair_virtual_net), so BOTH+LONG is silent, BOTH+SHORT fires.
  5. The check is strictly read-only: DB rows are byte-identical after it runs.
"""

import os
import sqlite3
import sys
import unittest

sys.path.append(os.getcwd())

from engine.integrity import check_direction_side_consistency


def _make_db(rows):
    """rows: list of (bot_id, pair, direction, is_active, position_side, open_qty)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            pair TEXT,
            direction TEXT,
            is_active INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY,
            position_side TEXT,
            open_qty REAL DEFAULT 0
        )
    """)
    for bot_id, pair, direction, is_active, position_side, open_qty in rows:
        conn.execute(
            "INSERT INTO bots (id, pair, direction, is_active) VALUES (?, ?, ?, ?)",
            (bot_id, pair, direction, is_active),
        )
        conn.execute(
            "INSERT INTO trades (bot_id, position_side, open_qty) VALUES (?, ?, ?)",
            (bot_id, position_side, open_qty),
        )
    conn.commit()
    return conn


def _snapshot(conn):
    bots = conn.execute("SELECT * FROM bots ORDER BY id").fetchall()
    trades = conn.execute("SELECT * FROM trades ORDER BY bot_id").fetchall()
    return bots, trades


class TestDirectionSideConsistency(unittest.TestCase):

    def test_divergent_bot_fires(self):
        """direction=LONG but position_side=SHORT with open_qty>0 -> check fires."""
        conn = _make_db([
            (1, 'BTC/USDC', 'LONG', 1, 'SHORT', 2.5),
        ])
        with self.assertLogs('IntegrityEnforcer', level='ERROR') as cm:
            divs = check_direction_side_consistency(conn=conn)
        self.assertEqual(len(divs), 1)
        self.assertEqual(divs[0]['bot_id'], 1)
        self.assertEqual(divs[0]['pair'], 'BTC/USDC')
        self.assertEqual(divs[0]['direction'], 'LONG')
        self.assertEqual(divs[0]['position_side'], 'SHORT')
        self.assertAlmostEqual(divs[0]['open_qty'], 2.5)
        joined = "\n".join(cm.output)
        self.assertIn('DIR-SIDE-DIVERGENCE', joined)
        self.assertIn('LONG', joined)
        self.assertIn('SHORT', joined)

    def test_divergent_short_direction_long_side_fires(self):
        """Mirror case: direction=SHORT but position_side=LONG -> fires."""
        conn = _make_db([
            (2, 'ETH/USDC', 'SHORT', 1, 'LONG', 1.0),
        ])
        divs = check_direction_side_consistency(conn=conn)
        self.assertEqual(len(divs), 1)
        self.assertEqual(divs[0]['bot_id'], 2)

    def test_consistent_long_bot_silent(self):
        """direction=LONG, position_side=LONG, open_qty>0 -> silent."""
        conn = _make_db([
            (3, 'BTC/USDC', 'LONG', 1, 'LONG', 4.0),
        ])
        divs = check_direction_side_consistency(conn=conn)
        self.assertEqual(divs, [])

    def test_consistent_short_bot_silent(self):
        """direction=SHORT, position_side=SHORT, open_qty>0 -> silent."""
        conn = _make_db([
            (4, 'BTC/USDC', 'SHORT', 1, 'SHORT', 4.0),
        ])
        divs = check_direction_side_consistency(conn=conn)
        self.assertEqual(divs, [])

    def test_zero_open_qty_divergent_row_out_of_scope(self):
        """open_qty == 0 rows never fire the check, even if divergent."""
        conn = _make_db([
            (5, 'BTC/USDC', 'LONG', 1, 'SHORT', 0.0),
        ])
        divs = check_direction_side_consistency(conn=conn)
        self.assertEqual(divs, [])

    def test_legacy_both_side_with_long_direction_silent(self):
        """position_side='BOTH' (legacy migration default) is treated as LONG by
        get_pair_virtual_net, so BOTH + direction=LONG is NOT a divergence."""
        conn = _make_db([
            (6, 'BTC/USDC', 'LONG', 1, 'BOTH', 3.0),
        ])
        divs = check_direction_side_consistency(conn=conn)
        self.assertEqual(divs, [])

    def test_legacy_both_side_with_short_direction_fires(self):
        """BOTH counts as LONG for pair-net, but direction=SHORT signs negative
        in get_bot_signed_contribution -> real sign disagreement -> fires."""
        conn = _make_db([
            (7, 'BTC/USDC', 'SHORT', 1, 'BOTH', 3.0),
        ])
        divs = check_direction_side_consistency(conn=conn)
        self.assertEqual(len(divs), 1)
        self.assertEqual(divs[0]['bot_id'], 7)

    def test_mixed_population_reports_only_divergent(self):
        conn = _make_db([
            (10, 'BTC/USDC', 'LONG', 1, 'LONG', 1.0),    # consistent
            (11, 'BTC/USDC', 'SHORT', 1, 'SHORT', 2.0),  # consistent
            (12, 'BTC/USDC', 'LONG', 1, 'SHORT', 3.0),   # DIVERGENT
            (13, 'ETH/USDC', 'SHORT', 0, 'LONG', 0.5),   # DIVERGENT (inactive bot still reported)
            (14, 'ETH/USDC', 'LONG', 1, 'SHORT', 0.0),   # zero qty -> out of scope
        ])
        divs = check_direction_side_consistency(conn=conn)
        self.assertEqual(sorted(d['bot_id'] for d in divs), [12, 13])

    def test_check_is_read_only(self):
        """DB rows must be byte-identical after the check runs."""
        conn = _make_db([
            (20, 'BTC/USDC', 'LONG', 1, 'SHORT', 2.5),  # divergent
            (21, 'BTC/USDC', 'SHORT', 1, 'SHORT', 1.5), # consistent
        ])
        before = _snapshot(conn)
        check_direction_side_consistency(conn=conn)
        after = _snapshot(conn)
        self.assertEqual(before, after)

    def test_default_connection_path(self):
        """conn=None falls back to engine.database.get_connection (patched)."""
        from unittest.mock import patch
        conn = _make_db([
            (30, 'BTC/USDC', 'LONG', 1, 'SHORT', 1.0),
        ])
        with patch('engine.database.get_connection', return_value=conn):
            divs = check_direction_side_consistency()
        self.assertEqual(len(divs), 1)
        self.assertEqual(divs[0]['bot_id'], 30)


if __name__ == '__main__':
    unittest.main()
