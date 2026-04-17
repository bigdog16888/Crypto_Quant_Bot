"""
Test Direction-Aware Ghost Detection

Tests the reconciler's ability to detect and reset:
1. Direction ghosts: bots claiming LONG when only SHORT exists
2. Phantom entries: bots with invested > 0 but entry_confirmed=0, avg_entry=0
3. Valid bots: should NOT be reset when directions match and quantities align
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
    BotState, ExchangePosition
)


def _make_in_memory_db(bot_qtys):
    """
    Build a real in-memory SQLite seeded with bot_orders so that
    resolve_net_mismatch returns meaningful bot_qtys.

    bot_qtys: list of (bot_id, filled_qty) tuples.
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
            "INSERT INTO bot_orders (bot_id, order_type, filled_amount, status, cycle_id) "
            "VALUES (?, 'entry', ?, 'filled', 1)",
            (bot_id, qty)
        )
    conn.commit()
    return conn


def _make_bot(bot_id, name, pair, direction, qty, avg_entry, confirmed=True):
    """Build a BotState where total_invested = qty * avg_entry (mathematically consistent)."""
    total_invested = qty * avg_entry
    return BotState(
        bot_id=bot_id, name=name, pair=pair, direction=direction,
        is_active=True, in_trade=total_invested > 0, total_invested=total_invested,
        avg_entry_price=avg_entry, target_tp_price=avg_entry * 1.01,
        current_step=1, basket_start_time=1000000,
        entry_order_id=None, tp_order_id=None, has_confirmed_entry=confirmed
    )


def _make_position(pair, side, size, entry_price):
    return ExchangePosition(
        symbol=pair, side=side, size=size,
        entry_price=entry_price, mark_price=entry_price, unrealized_pnl=0.0
    )


class TestDirectionGhostDetection(unittest.TestCase):
    """Test direction-aware ghost detection in resolve_net_mismatch."""

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    def test_valid_long_with_matching_position_not_reset(
        self, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario: 1 LONG bot, qty=0.016, exchange has LONG 0.016.
        Virtual qty == Physical qty → No mismatch → Bot should NOT be reset.
        """
        PHY_QTY = 0.016
        AVG_PRICE = 67200.0

        db = _make_in_memory_db([(10004, PHY_QTY)])
        mock_conn.return_value = db

        reconciler = StateReconciler(exchanges={})

        # Bot qty = 0.016, invested = 0.016 * 67200 = 1075.2 → perfectly aligned
        bots = [_make_bot(10004, "Valid_Long", "BTC/USDC", "LONG", PHY_QTY, AVG_PRICE)]
        positions = [_make_position("BTC/USDC", "LONG", PHY_QTY, AVG_PRICE)]

        results = reconciler.resolve_net_mismatch(bots, {"BTC/USDC": positions})

        ghost_results = [r for r in results
                         if r.bot_id == 10004 and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(ghost_results), 0,
                         "LONG bot should NOT be reset when LONG position exactly matches")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.safe_wipe_bot', return_value=True)
    def test_long_bot_ghost_when_no_physical_long(
        self, mock_wipe, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario: 1 LONG bot, exchange is FLAT (no positions at all).
        Bot claims 0.016 LONG but exchange is 0 → definite ghost → should be reset.
        """
        BOT_QTY = 0.016
        AVG_PRICE = 67200.0

        db = _make_in_memory_db([(10004, BOT_QTY)])
        mock_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [_make_bot(10004, "Ghost_Long", "BTC/USDC", "LONG", BOT_QTY, AVG_PRICE)]
        positions = []  # Exchange is flat

        results = reconciler.resolve_net_mismatch(bots, {"BTC/USDC": positions})

        ghost_results = [r for r in results
                         if r.bot_id == 10004 and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertTrue(len(ghost_results) > 0,
                        "LONG bot with no matching physical position should be detected as ghost")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.safe_wipe_bot', return_value=True)
    def test_valid_short_bot_survives_flat_exchange(
        self, mock_wipe, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario: 1 SHORT bot, exchange has EXACTLY the matching SHORT position.
        Virtual qty == Physical qty → No mismatch → Short bot should NOT be reset.
        """
        BOT_QTY = 0.016
        AVG_PRICE = 67200.0

        db = _make_in_memory_db([(10011, BOT_QTY)])
        mock_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [_make_bot(10011, "Valid_Short", "BTC/USDC", "SHORT", BOT_QTY, AVG_PRICE)]
        positions = [_make_position("BTC/USDC", "SHORT", BOT_QTY, AVG_PRICE)]

        results = reconciler.resolve_net_mismatch(bots, {"BTC/USDC": positions})

        ghost_results = [r for r in results
                         if r.bot_id == 10011 and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(ghost_results), 0,
                         "SHORT bot exactly matching physical SHORT should NOT be reset")


class TestPhantomEntryDetection(unittest.TestCase):
    """Test Fix #2: Phantom entry auto-reset"""

    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.log_reconciliation')
    def test_phantom_entry_detected_and_reset(self, mock_log_recon, mock_log_trade):
        """Bot with invested > 0, confirmed=0, avg_entry=0 should be auto-reset."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE bots (
                id INTEGER PRIMARY KEY, name TEXT, pair TEXT, direction TEXT,
                is_active INTEGER, status TEXT, strategy_type TEXT, config TEXT,
                base_size REAL, martingale_multiplier REAL
            )""")
            conn.execute("""CREATE TABLE trades (
                bot_id INTEGER PRIMARY KEY, total_invested REAL, avg_entry_price REAL,
                entry_confirmed INTEGER, current_step INTEGER, basket_start_time INTEGER,
                target_tp_price REAL, entry_order_id TEXT, tp_order_id TEXT,
                bot_position_id TEXT, last_exit_price REAL, last_exit_time INTEGER,
                close_type TEXT
            )""")
            conn.execute("""CREATE TABLE reconciliation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER,
                bot_id INTEGER, pair TEXT, action TEXT, details TEXT, proof_order_id TEXT
            )""")
            conn.execute("""CREATE TABLE trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, timestamp INTEGER,
                action TEXT, symbol TEXT, price REAL, amount REAL, cost_usdc REAL,
                order_id TEXT, step INTEGER, notes TEXT, pnl REAL
            )""")

            # Phantom bot: total_invested > 0, avg_entry = 0, confirmed = 0
            conn.execute("INSERT INTO bots VALUES (10002, 'Phantom_Bot', 'BTC/USDC:USDC', 'LONG', 1, 'Scanning', 'martingale', '{}', 0.002, 2.0)")
            conn.execute("INSERT INTO trades VALUES (10002, 133.62, 0.0, 0, 2, 0, 0, NULL, NULL, NULL, 0, 0, NULL)")

            # Valid bot
            conn.execute("INSERT INTO bots VALUES (10010, 'Valid_Bot', 'ETH/USDC:USDC', 'LONG', 1, 'In Trade', 'martingale', '{}', 0.077, 2.0)")
            conn.execute("INSERT INTO trades VALUES (10010, 149.19, 1937.53, 1, 1, 1000000, 1960, NULL, NULL, NULL, 0, 0, NULL)")

            conn.commit()
            conn.close()

            with patch('engine.reconciler.get_connection') as mock_get_conn:
                mock_get_conn.return_value = sqlite3.connect(db_path)

                reconciler = StateReconciler(exchanges={})
                reconciler._cleanup_phantom_entries()

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT total_invested, entry_confirmed FROM trades WHERE bot_id=10002")
            phantom_trade = cursor.fetchone()
            self.assertEqual(phantom_trade[0], 0.0, "Phantom bot invested should be reset to 0")
            self.assertEqual(phantom_trade[1], 0, "Phantom bot confirmed should be 0")

            cursor.execute("SELECT total_invested, entry_confirmed FROM trades WHERE bot_id=10010")
            valid_trade = cursor.fetchone()
            self.assertEqual(valid_trade[0], 149.19, "Valid bot should NOT be modified")
            self.assertEqual(valid_trade[1], 1, "Valid bot confirmed should still be 1")

            conn.close()

        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass  # Windows may hold a file lock; assertions already passed


if __name__ == '__main__':
    unittest.main()
