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


def _make_db(bot_qtys, active_positions=None, unconfirmed_bots=None):
    """
    Build a real in-memory SQLite seeded with bot_orders, bots, and trades.
    bot_qtys: list of (bot_id, filled_entry_qty) tuples.
    active_positions: list of (pair, side, size) tuples for active_positions table.
    """
    if unconfirmed_bots is None:
        unconfirmed_bots = set()
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            order_id TEXT,
            client_order_id TEXT,
            order_type TEXT,
            price REAL,
            amount REAL,
            filled_amount REAL,
            status TEXT,
            step INTEGER,
            cycle_id INTEGER,
            created_at INTEGER,
            updated_at INTEGER,
            filled_at INTEGER,
            notes TEXT,
            position_side TEXT,
            wipe_proof_source TEXT,
            wipe_proof_snapshot TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE active_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT, side TEXT, size REAL
        )
    """)
    conn.execute("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            name TEXT,
            pair TEXT,
            normalized_pair TEXT,
            direction TEXT,
            is_active INTEGER,
            status TEXT,
            pos_limit_hit INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY,
            cycle_id INTEGER DEFAULT 1,
            total_invested REAL DEFAULT 0,
            entry_confirmed INTEGER DEFAULT 0,
            position_side TEXT DEFAULT 'BOTH',
            avg_entry_price REAL DEFAULT 0,
            target_tp_price REAL DEFAULT 0,
            current_step INTEGER DEFAULT 0,
            basket_start_time INTEGER DEFAULT 0,
            wipe_wall_ts INTEGER DEFAULT 0,
            open_qty REAL DEFAULT 0,
            cycle_phase TEXT DEFAULT 'ACTIVE',
            cycle_start_time INTEGER DEFAULT 0,
            last_exit_price REAL DEFAULT 0,
            last_exit_time INTEGER DEFAULT 0,
            close_type TEXT DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE reconciliation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            bot_id INTEGER,
            pair TEXT,
            action TEXT,
            details TEXT,
            proof_order_id TEXT,
            FOREIGN KEY (bot_id) REFERENCES bots (id)
        )
    """)
    conn.execute("""
        CREATE TABLE manual_whitelists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            created_at INTEGER
        )
    """)
    
    for bot_id, qty in bot_qtys:
        direction = "LONG" if bot_id == 101 else "SHORT"
        name = f"Bot{bot_id}"
        pair = "BTC/USDC"
        normalized_pair = "BTCUSDC"
        
        # Insert bot order (entry order)
        conn.execute("""
            INSERT INTO bot_orders 
            (bot_id, order_type, filled_amount, amount, price, status, cycle_id, position_side)
            VALUES (?, 'entry', ?, ?, 100.0, 'filled', 1, ?)
        """, (bot_id, qty, qty, direction))
        
        # Insert bot
        conn.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status)
            VALUES (?, ?, ?, ?, ?, 1, 'ACTIVE')
        """, (bot_id, name, pair, normalized_pair, direction))
        
        # Insert trade
        confirmed = 0 if bot_id in unconfirmed_bots else 1
        conn.execute("""
            INSERT INTO trades 
            (bot_id, cycle_id, total_invested, entry_confirmed, position_side, avg_entry_price, target_tp_price, current_step, basket_start_time, wipe_wall_ts, open_qty, cycle_phase)
            VALUES (?, 1, ?, ?, ?, 100.0, 101.0, 1, 1000, 0, ?, 'ACTIVE')
        """, (bot_id, qty * 100.0, confirmed, direction, qty))
        
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
        tp_order_id=None, has_confirmed_entry=confirmed, cycle_id=1
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
    @patch('engine.database.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    def test_valid_virtual_hedging_no_reset(
        self, mock_reset, mock_log_trade, mock_log_recon, mock_db_conn, mock_recon_conn, mock_logger
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
        mock_db_conn.return_value = db
        mock_recon_conn.return_value = db

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
    @patch('engine.database.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.safe_wipe_bot', return_value=True)
    def test_ghost_long_reset(
        self, mock_wipe, mock_reset, mock_log_trade, mock_log_recon, mock_db_conn, mock_recon_conn, mock_logger
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
        mock_db_conn.return_value = db
        mock_recon_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [_make_bot(101, "GhostLong", "BTC/USDC", "LONG", QTY, PRICE)]

        results = reconciler.resolve_net_mismatch(bots, {"BTC/USDC": []})

        resets = [r for r in results if r.bot_id == 101 and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(resets), 1, "Ghost Long should be reset")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.database.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    def test_valid_mismatch_with_exactly_matching_quantities(
        self, mock_reset, mock_log_trade, mock_log_recon, mock_db_conn, mock_recon_conn, mock_logger
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
        mock_db_conn.return_value = db
        mock_recon_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [_make_bot(101, "ActiveBot", "BTC/USDC", "LONG", QTY, PRICE)]
        positions = {"BTC/USDC": [_make_position("BTC/USDC", "LONG", QTY, PRICE)]}

        results = reconciler.resolve_net_mismatch(bots, positions)

        resets = [r for r in results if r.bot_id == 101
                  and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(resets), 0, "Bot with matching physical position should NOT be reset")

    @patch('engine.reconciler.get_connection')
    @patch('engine.database.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.safe_wipe_bot', return_value=True)
    def test_mixed_bots_ghost_identification(
        self, mock_wipe, mock_reset, mock_log_trade, mock_log_recon, mock_db_conn, mock_recon_conn
    ):
        """
        Scenario:
        - Bot A: LONG 0.1 (ghost — no matching physical LONG)
        - Bot B: SHORT 0.1 (valid — matching physical SHORT)
        - Physical: SHORT 0.1
        Expected: Bot A is reset (ghost, no matching LONG exists).
        """
        QTY = 0.1   # $10 notional — avoids the sub-$5 Dust Chaser path
        PRICE = 100.0
        db = _make_db([(101, QTY), (102, QTY)], unconfirmed_bots={101})
        mock_db_conn.return_value = db
        mock_recon_conn.return_value = db

        reconciler = StateReconciler(exchanges={})
        bots = [
            _make_bot(101, "GhostLong", "BTC/USDC", "LONG", QTY, PRICE, confirmed=False),
            _make_bot(102, "ValidShort", "BTC/USDC", "SHORT", QTY, PRICE),
        ]
        positions = {
            "BTC/USDC": [_make_position("BTC/USDC", "SHORT", QTY, PRICE)]
        }

        results = reconciler.resolve_net_mismatch(bots, positions)
        print("TEST RESULTS:", results)

        # Ghost LONG should be reset
        ghost_resets = [r for r in results if r.bot_id == 101
                        and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertTrue(len(ghost_resets) > 0, "Ghost Long should be detected and reset")


if __name__ == '__main__':
    unittest.main()
