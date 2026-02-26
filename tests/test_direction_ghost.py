"""
Test Direction-Aware Ghost Detection (Fix #1, #2, #3, #4)

Tests the reconciler's ability to detect and reset:
1. Direction ghosts: bots claiming LONG when only SHORT exists
2. Phantom entries: bots with invested > 0 but entry_confirmed=0, avg_entry=0
3. Valid bots: should NOT be reset when directions match
4. Mixed directions: only ghost-direction bots should be reset
"""

import unittest
import sys
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass

sys.path.append(os.getcwd())

from engine.reconciler import (
    StateReconciler, ReconciliationAction, ReconciliationResult,
    BotState, ExchangePosition
)


class TestDirectionGhostDetection(unittest.TestCase):
    """Test Fix #1: Direction-aware ghost detection in resolve_net_mismatch"""

    def _make_bot(self, bot_id, name, pair, direction, invested, 
                  avg_entry=100.0, confirmed=True, step=1):
        return BotState(
            bot_id=bot_id,
            name=name,
            pair=pair,
            direction=direction,
            is_active=True,
            in_trade=invested > 0,
            total_invested=invested,
            avg_entry_price=avg_entry,
            target_tp_price=avg_entry * 1.01,
            current_step=step,
            basket_start_time=1000000,
            entry_order_id=None,
            tp_order_id=None,
            has_confirmed_entry=confirmed
        )

    def _make_position(self, pair, side, size, entry_price):
        return ExchangePosition(
            symbol=pair,
            side=side,
            size=size,
            entry_price=entry_price,
            notional=size * entry_price
        )

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    def test_long_bot_with_only_short_position_is_ghost(
        self, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """Bot claims LONG $134, but exchange only has SHORT. Should be reset."""
        reconciler = StateReconciler()
        
        bots = [
            self._make_bot(10004, "Ghost_Long", "BTC/USDC", "LONG", 134.40, avg_entry=67200),
            self._make_bot(10011, "Valid_Short", "BTC/USDC", "SHORT", 134.44, avg_entry=67221),
        ]
        positions = [
            self._make_position("BTC/USDC", "SHORT", 0.016, 67211.84),
        ]
        
        results = reconciler.resolve_net_mismatch(bots, positions)
        
        # Ghost_Long should be fixed, Valid_Short should NOT
        ghost_results = [r for r in results if r.bot_id == 10004 
                        and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertTrue(len(ghost_results) > 0, 
                       "LONG bot should be detected as direction ghost when only SHORT exists")
        
        # Valid_Short should not have been touched
        valid_results = [r for r in results if r.bot_id == 10011 
                        and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(valid_results), 0, 
                        "SHORT bot should NOT be reset when SHORT position exists")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    def test_matching_direction_bot_is_not_ghost(
        self, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """Bot claims LONG $149, exchange has LONG. Should NOT be reset."""
        reconciler = StateReconciler()
        
        bots = [
            self._make_bot(10010, "Valid_Long", "ETH/USDC", "LONG", 149.19, avg_entry=1937),
        ]
        positions = [
            self._make_position("ETH/USDC", "LONG", 0.385, 1937.32),
        ]
        
        results = reconciler.resolve_net_mismatch(bots, positions)
        
        ghost_results = [r for r in results if r.bot_id == 10010 
                        and r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(ghost_results), 0, 
                        "LONG bot should NOT be reset when LONG position exists")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    def test_multiple_long_ghosts_all_reset(
        self, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """Multiple LONG bots on same pair, only SHORT exists. ALL should be reset."""
        reconciler = StateReconciler()
        
        bots = [
            self._make_bot(10002, "Ghost_1", "BTC/USDC", "LONG", 133.62, avg_entry=0, confirmed=False),
            self._make_bot(10004, "Ghost_2", "BTC/USDC", "LONG", 134.40, avg_entry=67200),
            self._make_bot(10015, "Ghost_3", "BTC/USDC", "LONG", 134.40, avg_entry=67200),
            self._make_bot(10011, "Valid_Short", "BTC/USDC", "SHORT", 134.44, avg_entry=67221),
        ]
        positions = [
            self._make_position("BTC/USDC", "SHORT", 0.016, 67211.84),
        ]
        
        results = reconciler.resolve_net_mismatch(bots, positions)
        
        # All 3 LONG ghosts should be reset
        ghost_ids = {r.bot_id for r in results 
                    if r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE}
        
        for ghost_id in [10002, 10004, 10015]:
            self.assertIn(ghost_id, ghost_ids, 
                         f"Ghost bot {ghost_id} should be reset")
        
        self.assertNotIn(10011, ghost_ids, 
                        "Valid SHORT bot should NOT be reset")


class TestPhantomEntryDetection(unittest.TestCase):
    """Test Fix #2: Phantom entry auto-reset"""

    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.log_reconciliation')
    def test_phantom_entry_detected_and_reset(self, mock_log_recon, mock_log_trade):
        """Bot with invested > 0, confirmed=0, avg_entry=0 should be auto-reset."""
        # Create a temp DB with a phantom entry
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
            
            # Insert phantom bot
            conn.execute("INSERT INTO bots VALUES (10002, 'Phantom_Bot', 'BTC/USDC:USDC', 'LONG', 1, 'Scanning', 'martingale', '{}', 0.002, 2.0)")
            conn.execute("INSERT INTO trades VALUES (10002, 133.62, 0.0, 0, 2, 0, 0, NULL, NULL, NULL, 0, 0, NULL)")
            
            # Insert valid bot (should NOT be reset)
            conn.execute("INSERT INTO bots VALUES (10010, 'Valid_Bot', 'ETH/USDC:USDC', 'LONG', 1, 'In Trade', 'martingale', '{}', 0.077, 2.0)")
            conn.execute("INSERT INTO trades VALUES (10010, 149.19, 1937.53, 1, 1, 1000000, 1960, NULL, NULL, NULL, 0, 0, NULL)")
            
            conn.commit()
            conn.close()
            
            # Patch get_connection to use our temp DB
            with patch('engine.reconciler.get_connection') as mock_get_conn:
                mock_get_conn.return_value = sqlite3.connect(db_path)
                
                reconciler = StateReconciler()
                reconciler._cleanup_phantom_entries()
            
            # Verify phantom was reset
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT total_invested, entry_confirmed FROM trades WHERE bot_id=10002")
            phantom_trade = cursor.fetchone()
            self.assertEqual(phantom_trade[0], 0.0, "Phantom bot invested should be reset to 0")
            self.assertEqual(phantom_trade[1], 0, "Phantom bot confirmed should be 0")
            
            # Verify valid bot was NOT touched
            cursor.execute("SELECT total_invested, entry_confirmed FROM trades WHERE bot_id=10010")
            valid_trade = cursor.fetchone()
            self.assertEqual(valid_trade[0], 149.19, "Valid bot should NOT be modified")
            self.assertEqual(valid_trade[1], 1, "Valid bot confirmed should still be 1")
            
            conn.close()
            
        finally:
            os.unlink(db_path)


if __name__ == '__main__':
    unittest.main()
