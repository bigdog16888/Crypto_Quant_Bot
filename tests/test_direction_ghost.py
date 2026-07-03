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


def _make_in_memory_db(bot_qtys, pairs=None):
    if pairs is None:
        pairs = ['BTC/USDC']
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            pair TEXT,
            normalized_pair TEXT,
            direction TEXT,
            is_active INTEGER,
            status TEXT,
            config TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY,
            cycle_id INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            order_type TEXT,
            filled_amount REAL,
            amount REAL DEFAULT 0.0,
            status TEXT,
            cycle_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE active_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            pair TEXT,
            side TEXT,
            size REAL,
            entry_price REAL,
            last_checked INTEGER
        )
    """)
    from engine.exchange_interface import normalize_symbol
    for i, pair in enumerate(pairs):
        bot_id = bot_qtys[i][0] if i < len(bot_qtys) else 10000 + i
        conn.execute(
            "INSERT INTO bots (id, pair, normalized_pair, direction, is_active, status, config) "
            "VALUES (?, ?, ?, 'LONG', 1, 'In Trade', '{}')",
            (bot_id, pair, normalize_symbol(pair))
        )
        conn.execute(
            "INSERT INTO trades (bot_id, cycle_id) VALUES (?, 1)",
            (bot_id,)
        )
    for bot_id, qty in bot_qtys:
        conn.execute(
            "INSERT INTO bot_orders (bot_id, order_type, filled_amount, status, cycle_id) "
            "VALUES (?, 'entry', ?, 'filled', 1)",
            (bot_id, qty)
        )
    import time
    conn.execute(
        "INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked) "
        "VALUES (0, 'GLOBAL', 'FLAT', 0.0, 0.0, ?)",
        (int(time.time()),)
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
    @patch('engine.database.get_connection')
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
    @patch('engine.database.get_connection')
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
        reconciler._flat_snapshots_counts = {'BTCUSDC': 2}
        bots = [_make_bot(10004, "Ghost_Long", "BTC/USDC", "LONG", BOT_QTY, AVG_PRICE)]
        positions = []  # Exchange is flat

        results = reconciler.resolve_net_mismatch(bots, {"BTC/USDC": positions})

        ghost_results = [r for r in results
                         if r.bot_id == 10004 and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertTrue(len(ghost_results) > 0,
                        "LONG bot with no matching physical position should be detected as ghost")

    @patch('engine.reconciler.logger')
    @patch('engine.database.get_connection')
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
                id INTEGER PRIMARY KEY,
                name TEXT,
                pair TEXT,
                normalized_pair TEXT,
                direction TEXT,
                is_active INTEGER,
                status TEXT,
                strategy_type TEXT,
                config TEXT,
                base_size REAL,
                martingale_multiplier REAL,
                rsi_limit REAL,
                manual_close_pct REAL DEFAULT 100.0,
                last_error TEXT,
                last_error_time INTEGER,
                pos_limit_hit INTEGER DEFAULT 0,
                cascade_started_at INTEGER DEFAULT 0
            )""")
            conn.execute("""CREATE TABLE trades (
                bot_id INTEGER PRIMARY KEY,
                current_step INTEGER DEFAULT 0,
                total_invested REAL DEFAULT 0,
                avg_entry_price REAL DEFAULT 0,
                target_tp_price REAL DEFAULT 0,
                last_exit_price REAL DEFAULT 0,
                last_exit_time INTEGER DEFAULT 0,
                basket_start_time INTEGER DEFAULT 0,
                entry_confirmed BOOLEAN DEFAULT 0,
                entry_order_id TEXT,
                tp_order_id TEXT,
                bot_position_id TEXT,
                close_type TEXT DEFAULT NULL,
                cycle_id INTEGER DEFAULT 1,
                cycle_phase TEXT DEFAULT 'ACTIVE',
                open_qty REAL DEFAULT 0,
                wipe_wall_ts INTEGER DEFAULT 0,
                hedge_qty REAL DEFAULT 0,
                cycle_start_time INTEGER DEFAULT 0,
                position_side TEXT DEFAULT 'BOTH'
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
            conn.execute("""CREATE TABLE bot_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                step INTEGER DEFAULT 0,
                order_type TEXT,
                order_id TEXT,
                price REAL DEFAULT 0,
                amount REAL DEFAULT 0,
                filled_amount REAL DEFAULT 0,
                status TEXT DEFAULT 'open',
                created_at INTEGER,
                client_order_id TEXT,
                updated_at INTEGER DEFAULT 0,
                notes TEXT,
                wipe_proof_source TEXT,
                wipe_proof_snapshot TEXT,
                cycle_id INTEGER DEFAULT 1,
                position_side TEXT DEFAULT 'BOTH',
                filled_at INTEGER DEFAULT 0
            )""")
            conn.execute("""CREATE TABLE active_positions (
                bot_id INTEGER NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                size REAL NOT NULL DEFAULT 0,
                entry_price REAL DEFAULT 0,
                last_checked INTEGER,
                PRIMARY KEY (bot_id, pair, side)
            )""")

            # Phantom bot: total_invested > 0, avg_entry = 0, confirmed = 0
            conn.execute("""
                INSERT INTO bots (id, name, pair, direction, is_active, status, strategy_type, config, base_size, martingale_multiplier)
                VALUES (10002, 'Phantom_Bot', 'BTC/USDC:USDC', 'LONG', 1, 'Scanning', 'martingale', '{}', 0.002, 2.0)
            """)
            conn.execute("""
                INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, current_step, cycle_phase, cycle_id)
                VALUES (10002, 133.62, 0.0, 0, 2, 'ACTIVE', 1)
            """)

            # Valid bot
            conn.execute("""
                INSERT INTO bots (id, name, pair, direction, is_active, status, strategy_type, config, base_size, martingale_multiplier)
                VALUES (10010, 'Valid_Bot', 'ETH/USDC:USDC', 'LONG', 1, 'In Trade', 'martingale', '{}', 0.077, 2.0)
            """)
            conn.execute("""
                INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, current_step, basket_start_time, target_tp_price, cycle_phase, cycle_id)
                VALUES (10010, 149.19, 1937.53, 1, 1, 1000000, 1960, 'ACTIVE', 1)
            """)

            conn.commit()
            conn.close()

            temp_db_conn = sqlite3.connect(db_path)
            with patch('engine.reconciler.get_connection', return_value=temp_db_conn), \
                 patch('engine.database.get_connection', return_value=temp_db_conn), \
                 patch('config.settings.config.REQUIRE_HUMAN_APPROVAL', False):

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

    def test_reset_not_blocked_by_sibling_position(self):
        """
        Verify that resetting a bot (e.g. parent bot with size=0 in active_positions)
        is NOT blocked by a sibling bot's position on the same pair.
        """
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE bots (
                id INTEGER PRIMARY KEY,
                name TEXT,
                pair TEXT,
                normalized_pair TEXT,
                direction TEXT,
                is_active INTEGER,
                status TEXT,
                strategy_type TEXT,
                config TEXT,
                base_size REAL,
                martingale_multiplier REAL,
                rsi_limit REAL,
                manual_close_pct REAL DEFAULT 100.0,
                last_error TEXT,
                last_error_time INTEGER,
                pos_limit_hit INTEGER DEFAULT 0,
                cascade_started_at INTEGER DEFAULT 0
            )""")
            conn.execute("""CREATE TABLE trades (
                bot_id INTEGER PRIMARY KEY,
                current_step INTEGER DEFAULT 0,
                total_invested REAL DEFAULT 0,
                avg_entry_price REAL DEFAULT 0,
                target_tp_price REAL DEFAULT 0,
                last_exit_price REAL DEFAULT 0,
                last_exit_time INTEGER DEFAULT 0,
                basket_start_time INTEGER DEFAULT 0,
                entry_confirmed BOOLEAN DEFAULT 0,
                entry_order_id TEXT,
                tp_order_id TEXT,
                bot_position_id TEXT,
                close_type TEXT DEFAULT NULL,
                cycle_id INTEGER DEFAULT 1,
                cycle_phase TEXT DEFAULT 'ACTIVE',
                open_qty REAL DEFAULT 0,
                wipe_wall_ts INTEGER DEFAULT 0,
                hedge_qty REAL DEFAULT 0,
                cycle_start_time INTEGER DEFAULT 0,
                position_side TEXT DEFAULT 'BOTH'
            )""")
            conn.execute("""CREATE TABLE active_positions (
                bot_id INTEGER NOT NULL, pair TEXT NOT NULL, side TEXT NOT NULL, size REAL NOT NULL DEFAULT 0, entry_price REAL DEFAULT 0, last_checked INTEGER, PRIMARY KEY (bot_id, pair, side)
            )""")
            conn.execute("""CREATE TABLE bot_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER,
                step INTEGER DEFAULT 0,
                order_type TEXT,
                order_id TEXT,
                price REAL DEFAULT 0,
                amount REAL DEFAULT 0,
                filled_amount REAL DEFAULT 0,
                status TEXT DEFAULT 'open',
                created_at INTEGER,
                client_order_id TEXT,
                updated_at INTEGER DEFAULT 0,
                notes TEXT,
                wipe_proof_source TEXT,
                wipe_proof_snapshot TEXT,
                cycle_id INTEGER DEFAULT 1,
                position_side TEXT DEFAULT 'BOTH',
                filled_at INTEGER DEFAULT 0
            )""")
            conn.execute("""CREATE TABLE trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, timestamp INTEGER, action TEXT, symbol TEXT, price REAL, amount REAL, cost_usdc REAL, order_id TEXT, step INTEGER, notes TEXT, pnl REAL, position_side TEXT
            )""")
            conn.execute("""CREATE TABLE reconciliation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, bot_id INTEGER, pair TEXT, action TEXT, details TEXT, proof_order_id TEXT
            )""")

            # Bot 10021 (parent): LONG, in scanning/ready to reset, open_qty=0
            conn.execute("INSERT INTO bots (id, name, pair, direction, is_active, status) VALUES (10021, 'long eth', 'ETH/USDC:USDC', 'LONG', 1, 'Scanning')")
            conn.execute("INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, open_qty, cycle_id) VALUES (10021, 0.0, 0.0, 0, 0.0, 32)")

            # Sibling Bot 100316 (child): LONG, has open_qty=0.415, and has active_positions row
            conn.execute("INSERT INTO bots (id, name, pair, direction, is_active, status) VALUES (100316, 'eth_hedge', 'ETH/USDC:USDC', 'LONG', 1, 'In Trade')")
            conn.execute("INSERT INTO trades (bot_id, total_invested, avg_entry_price, entry_confirmed, open_qty, cycle_id) VALUES (100316, 800.0, 2000.0, 1, 0.415, 78)")
            conn.execute("INSERT INTO active_positions (bot_id, pair, side, size, entry_price) VALUES (100316, 'ETHUSDC', 'LONG', 0.415, 2000.0)")

            conn.commit()
            conn.close()

            temp_db_conn = sqlite3.connect(db_path)
            from engine.database import reset_bot_after_tp
            mock_exchange = MagicMock()
            mock_exchange.fetch_positions.return_value = [
                {'symbol': 'ETH/USDC:USDC', 'contracts': 0.415, 'side': 'long'}
            ]
            with patch('engine.database.get_connection', return_value=temp_db_conn):
                # Call reset_bot_after_tp for bot 10021. It should succeed and not raise WipeBlockedError.
                reset_bot_after_tp(10021, exit_price=2000.0, action_label='TP_HIT', exchange=mock_exchange)

            # Verify that bot 10021 cycle was incremented to 33
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT cycle_id FROM trades WHERE bot_id=10021")
            self.assertEqual(cursor.fetchone()[0], 33, "Parent bot cycle should be reset to 33 successfully without being blocked by sibling position")
            conn.close()

        finally:
            try:
                os.unlink(db_path)
            except OSError:
                pass


if __name__ == '__main__':
    unittest.main()
