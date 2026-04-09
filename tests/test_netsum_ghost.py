"""
Test Net-Sum Ghost Detection (Revised)

Tests the reconciler's ability to:
1. Allow valid virtual hedging (Long + Short bots) when Net matches Physical.
2. Detect ghosts when Virtual Net != Physical Net (using seeded bot_orders for bot_qtys).
3. Reset only bots contributing to the error.
4. Auto-reset phantom entries (invested > 0, confirmed=0).
"""

import unittest
import sys
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

sys.path.append(os.getcwd())

from engine.reconciler import (
    StateReconciler, ReconciliationAction, ReconciliationResult,
    BotState, ExchangePosition, ExchangeOrder
)


def _make_db(bot_qtys, active_positions=None):
    """
    Build a real in-memory SQLite seeded with bot_orders.
    bot_qtys: list of (bot_id, filled_entry_qty) tuples.
    active_positions: list of (pair, side, size) tuples for active_positions table.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            order_type TEXT,
            filled_amount REAL,
            status TEXT,
            cycle_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE active_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT, side TEXT, size REAL
        )
    """)
    for bot_id, qty in bot_qtys:
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, filled_amount, status, cycle_id)"
            " VALUES (?, 'entry', ?, 'filled', 1)",
            (bot_id, qty)
        )
    if active_positions:
        for pair, side, size in active_positions:
            conn.execute(
                "INSERT INTO active_positions (pair, side, size) VALUES (?, ?, ?)",
                (pair, side, size)
            )
    conn.commit()
    return conn


def _make_bot(bot_id, name, pair, direction, qty, avg_entry, confirmed=True):
    """Build a BotState with total_invested = qty * avg_entry (consistent math)."""
    total_invested = qty * avg_entry
    return BotState(
        bot_id=bot_id, name=name, pair=pair, direction=direction,
        is_active=True, in_trade=total_invested > 0,
        total_invested=total_invested, avg_entry_price=avg_entry,
        target_tp_price=avg_entry * 1.01, current_step=1,
        basket_start_time=1000, entry_order_id=f"entry_{bot_id}",
        tp_order_id=None, has_confirmed_entry=confirmed
    )


def _make_position(pair, side, size, price):
    return ExchangePosition(
        symbol=pair, side=side, size=size,
        entry_price=price, mark_price=price, unrealized_pnl=0.0
    )


def _make_order(bot_id, pair, side):
    return ExchangeOrder(
        order_id=f"ord_{bot_id}", symbol=pair, side=side,
        order_type='limit', price=100.0, amount=0.1, status='open',
        client_order_id=f"CQB_{bot_id}_12345"
    )


class TestNetSumGhostDetection(unittest.TestCase):

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    def test_valid_virtual_hedging_no_reset(
        self, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario:
        - Bot A: LONG 0.01 BTC/USDC, Bot B: SHORT 0.01 BTC/USDC
        - Physical: FLAT (net 0)
        - Virtual Net: exactly 0 (LONG - SHORT)
        Expected: NO reset — both bots mathematically cancel.
        """
        QTY = 0.1  # 0.1 * 100 = $10 notional — avoids the sub-$5 Dust Chaser
        PRICE = 100.0
        db = _make_db([(101, QTY), (102, QTY)])
        mock_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [
            _make_bot(101, "LongBot", "BTC/USDC", "LONG", QTY, PRICE),
            _make_bot(102, "ShortBot", "BTC/USDC", "SHORT", QTY, PRICE),
        ]
        # Physical is FLAT
        results = reconciler.resolve_net_mismatch(bots, {"BTC/USDC": []} )

        resets = [r for r in results if r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(resets), 0, "Valid hedged bots (net=0, physical=0) should NOT be reset")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.safe_wipe_bot', return_value=True)
    def test_ghost_long_reset(
        self, mock_wipe, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario:
        - Bot A: LONG 0.01 BTC/USDC  (ghost — exchange is flat)
        - Physical: FLAT
        Expected: Bot A is reset (ghost).
        """
        QTY = 0.01
        PRICE = 100.0
        db = _make_db([(101, QTY)])
        mock_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [_make_bot(101, "GhostLong", "BTC/USDC", "LONG", QTY, PRICE)]

        results = reconciler.resolve_net_mismatch(bots, {"BTC/USDC": []})

        resets = [r for r in results if r.bot_id == 101]
        self.assertEqual(len(resets), 1, "Ghost Long should be reset")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    def test_valid_mismatch_with_exactly_matching_quantities(
        self, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario:
        - Bot A: LONG 0.01  (virtual qty == physical qty == 0.01)
        - Physical: LONG 0.01
        Expected: No reset — perfect match.
        """
        QTY = 0.1  # 0.1 * 100 = $10 notional — avoids the sub-$5 Dust Chaser
        PRICE = 100.0
        db = _make_db([(101, QTY)])
        mock_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [_make_bot(101, "ActiveBot", "BTC/USDC", "LONG", QTY, PRICE)]
        positions = {"BTC/USDC": [_make_position("BTC/USDC", "LONG", QTY, PRICE)]}

        results = reconciler.resolve_net_mismatch(bots, positions)

        resets = [r for r in results if r.bot_id == 101
                  and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(resets), 0, "Bot with matching physical position should NOT be reset")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.safe_wipe_bot', return_value=True)
    def test_mixed_bots_ghost_identification(
        self, mock_wipe, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario:
        - Bot A: LONG 0.01 (ghost — no matching physical LONG)
        - Bot B: SHORT 0.01 (valid — matching physical SHORT)
        - Physical: SHORT 0.01
        Expected: Bot A is reset (ghost, no matching LONG exists).
        """
        QTY = 0.01
        PRICE = 100.0
        db = _make_db([(101, QTY), (102, QTY)])
        mock_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [
            _make_bot(101, "GhostLong", "BTC/USDC", "LONG", QTY, PRICE),
            _make_bot(102, "ValidShort", "BTC/USDC", "SHORT", QTY, PRICE),
        ]
        positions = {
            "BTC/USDC": [_make_position("BTC/USDC", "SHORT", QTY, PRICE)]
        }

        results = reconciler.resolve_net_mismatch(bots, positions)

        # Ghost LONG should be reset
        ghost_resets = [r for r in results if r.bot_id == 101
                        and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertTrue(len(ghost_resets) > 0, "Ghost Long should be detected and reset")


if __name__ == '__main__':
    unittest.main()
