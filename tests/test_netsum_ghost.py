"""
Test Net-Sum Ghost Detection (Revised Fix #1)

Tests the reconciler's ability to:
1. Allow valid virtual hedging (Long + Short bots) when Net matches Physical.
2. Detect ghosts using Net-Sum Error (Virtual - Physical).
3. Reset only bots contributing to the error that lack 'Proof of Life' (open orders).
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

class TestNetSumGhostDetection(unittest.TestCase):
    
    def _make_bot(self, bot_id, name, pair, direction, invested, confirmed=True):
        return BotState(
            bot_id=bot_id, name=name, pair=pair, direction=direction,
            is_active=True, in_trade=invested > 0, total_invested=invested,
            avg_entry_price=100.0, target_tp_price=101.0, current_step=1,
            basket_start_time=1000, entry_order_id=f"entry_{bot_id}", 
            tp_order_id=None, has_confirmed_entry=confirmed
        )

    def _make_position(self, pair, side, size, price):
        return ExchangePosition(
            symbol=pair, side=side, size=size, entry_price=price, 
            mark_price=price, unrealized_pnl=0.0
        )

    def _make_order(self, bot_id, pair, side):
        return ExchangeOrder(
            order_id=f"ord_{bot_id}", symbol=pair, side=side, 
            order_type='limit', price=100.0, amount=0.1, status='open',
            client_order_id=f"CQB_{bot_id}_12345"
        )

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.normalize_symbol', side_effect=lambda x: x)
    def test_valid_virtual_hedging_no_reset(
        self, mock_norm, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario: 
        - Bot A: LONG $1000
        - Bot B: SHORT $1000
        - Virtual Net: $0
        - Physical Exchange: $0 (Flat)
        
        Result: No mismatch. Both bots should survive.
        """
        reconciler = StateReconciler()
        
        bots = [
            self._make_bot(101, "LongBot", "BTC/USDC", "LONG", 1000.0),
            self._make_bot(102, "ShortBot", "BTC/USDC", "SHORT", 1000.0)
        ]
        positions = { "BTC/USDC": [] } # Flat
        orders = { "BTC/USDC": [] }

        results = reconciler.resolve_net_mismatch(bots, positions, orders)
        
        resets = [r for r in results if r.action_taken == ReconciliationAction.SYSTEM_FIX_ZOMBIE]
        self.assertEqual(len(resets), 0, "Valid hedged bots should NOT be reset")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.normalize_symbol', side_effect=lambda x: x)
    def test_ghost_long_reset(
        self, mock_norm, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario:
        - Bot A: LONG $1000 (No orders)
        - Physical Exchange: $0
        - Net Error: +$1000 (Virtual too LONG)
        
        Result: Bot A is on the 'Wrong Side' (Long) and has NO orders -> Reset.
        """
        reconciler = StateReconciler()
        
        bots = [ self._make_bot(101, "GhostLong", "BTC/USDC", "LONG", 1000.0) ]
        positions = { "BTC/USDC": [] } # Flat
        orders = { "BTC/USDC": [] } # No open orders

        results = reconciler.resolve_net_mismatch(bots, positions, orders)
        
        resets = [r for r in results if r.bot_id == 101]
        self.assertEqual(len(resets), 1, "Ghost Long should be reset")
        self.assertIn("Net-Sum ghost", resets[0].details)

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.normalize_symbol', side_effect=lambda x: x)
    def test_valid_mismatch_with_orders(
        self, mock_norm, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario:
        - Bot A: LONG $1000
        - Physical Exchange: $0
        - Net Error: +$1000 (Virtual too LONG)
        - Bot A HAS open orders (Proof of Life)
        
        Result: Bot A is contributing to error, BUT has orders -> NO Reset.
        """
        reconciler = StateReconciler()
        
        bots = [ self._make_bot(101, "ActiveBot", "BTC/USDC", "LONG", 1000.0) ]
        positions = { "BTC/USDC": [] } 
        # Bot has an open order
        orders = { "BTC/USDC": [self._make_order(101, "BTC/USDC", "buy")] }

        results = reconciler.resolve_net_mismatch(bots, positions, orders)
        
        resets = [r for r in results if r.bot_id == 101]
        self.assertEqual(len(resets), 0, "Bot with open orders should NOT be reset despite mismatch")

    @patch('engine.reconciler.logger')
    @patch('engine.reconciler.get_connection')
    @patch('engine.reconciler.log_reconciliation')
    @patch('engine.reconciler.log_trade')
    @patch('engine.reconciler.reset_bot_after_tp')
    @patch('engine.reconciler.normalize_symbol', side_effect=lambda x: x)
    def test_mixed_bots_ghost_identification(
        self, mock_norm, mock_reset, mock_log_trade, mock_log_recon, mock_conn, mock_logger
    ):
        """
        Scenario:
        - Bot A: LONG $1000 (No orders) -> Ghost
        - Bot B: SHORT $1000 (Active)
        - Virtual Net: $0
        - Physical Net: -$1000 (Short only)
        - Net Error: Virtual($0) - Physical(-$1000) = +$1000
        - Error positive -> Virtual is too LONG relative to physical.
        
        Result:
        - Suspects are LONG bots.
        - Bot A (Long) found. No orders -> Reset.
        - Bot B (Short) ignored (not on error side).
        """
        reconciler = StateReconciler()
        
        bots = [
            self._make_bot(101, "GhostLong", "BTC/USDC", "LONG", 1000.0),
            self._make_bot(102, "ValidShort", "BTC/USDC", "SHORT", 1000.0)
        ]
        
        # Physical is Short 1000 (matches Bot B)
        positions = { 
            "BTC/USDC": [self._make_position("BTC/USDC", "SHORT", 10.0, 100.0)] 
        }
        
        orders = { "BTC/USDC": [] } # No orders for GhostLong

        results = reconciler.resolve_net_mismatch(bots, positions, orders)
        
        ghost_resets = [r for r in results if r.bot_id == 101]
        self.assertEqual(len(ghost_resets), 1, "Ghost Long should be reset (it's causing the drift)")
        
        short_resets = [r for r in results if r.bot_id == 102]
        self.assertEqual(len(short_resets), 0, "Valid Short should NOT be reset")


if __name__ == '__main__':
    unittest.main()
