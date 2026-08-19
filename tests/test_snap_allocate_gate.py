"""
Tests for SNAP-ALLOCATE zero-attribution-guard fix.
Covers the forensic attribution gate in update_active_positions_snapshot.
"""

import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db, update_active_positions_snapshot
from engine.parity_gates import forensic_adopt_allowed, qty_tolerance


class TestSnapAllocateGate(unittest.TestCase):
    """Test the forensic attribution gate for multi-bot auto-split."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        db_path = os.path.join(self.test_dir, 'test_snap_allocate.db')
        database.DB_PATH = db_path
        database._local = database.threading.local()
        init_db()
        self.conn = get_connection()

    def tearDown(self):
        if self.conn:
            self.conn.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _insert_bot_full(self, bot_id, name, pair, norm_pair, direction,
                         status='IN TRADE', bot_type='standard', is_active=1,
                         parent_bot_id=None, hedge_child_bot_id=None, hedge_trigger_step=None,
                         total_invested=0.0, open_qty=0.0, avg_entry_price=0.0, position_side=None):
        """Insert a bot with trades row and proper position_side."""
        if position_side is None:
            position_side = direction
        
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO bots (id, name, pair, normalized_pair, direction,
                              status, bot_type, is_active, parent_bot_id,
                              hedge_child_bot_id, hedge_trigger_step,
                              rsi_limit, martingale_multiplier, base_size, strategy_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 0, 'Martingale')
        """, (bot_id, name, pair, norm_pair, direction,
              status, bot_type, is_active, parent_bot_id,
              hedge_child_bot_id, hedge_trigger_step))
        
        cursor.execute("""
            INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                                total_invested, avg_entry_price, current_step, entry_confirmed, cycle_phase)
            VALUES (?, ?, 1, ?, ?, ?, 1, 1, 'ACTIVE')
        """, (bot_id, open_qty, position_side, total_invested, avg_entry_price))
        self.conn.commit()

    def _insert_bot_order(self, bot_id, order_type, filled_amount, price=1.0, status='filled', cycle_id=1, step=1, position_side=None):
        """Insert a bot_order with filled amount."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, 
                                    filled_amount, status, step, cycle_id, created_at, position_side)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (bot_id, order_type, f'{order_type}_{bot_id}', f'CQB_{bot_id}_{order_type}_1', 
              price, filled_amount, filled_amount, status, step, cycle_id, 12345, position_side or 'LONG'))
        self.conn.commit()

    def _setup_longs(self, bots_config):
        """Setup multiple LONG bots with orders that match their invested quantities.
        bots_config = [(bot_id, name, invested_qty, avg_price), ...]
        """
        for bot_id, name, invested_qty, avg_price in bots_config:
            # Insert bot
            self._insert_bot_full(bot_id, name, 'BTC/USDC:USDC', 'BTCUSDC', 'LONG',
                                  total_invested=invested_qty * avg_price, 
                                  open_qty=invested_qty, avg_entry_price=avg_price,
                                  position_side='LONG')
            
            # Insert entry order with filled amount = invested_qty
            if invested_qty > 0:
                self._insert_bot_order(bot_id, 'entry', invested_qty, avg_price, position_side='LONG')

    def _setup_shorts(self, bots_config):
        """Setup multiple SHORT bots with orders that match their invested quantities.
        bots_config = [(bot_id, name, invested_qty, avg_price), ...]
        Note: invested_qty is positive magnitude, open_qty will be negative
        """
        for bot_id, name, invested_qty, avg_price in bots_config:
            # Insert bot - for SHORT, open_qty is negative, total_invested positive
            self._insert_bot_full(bot_id, name, 'BTC/USDC:USDC', 'BTCUSDC', 'SHORT',
                                  total_invested=invested_qty * avg_price, 
                                  open_qty=-invested_qty, avg_entry_price=avg_price,
                                  position_side='SHORT')
            
            # Insert entry order with filled amount = invested_qty
            if invested_qty > 0:
                self._insert_bot_order(bot_id, 'entry', invested_qty, avg_price, position_side='SHORT')

    def test_single_contributor_passes_gate_when_forensic_disabled(self):
        """Single bot with invested qty should be assigned without forensic gate."""
        import config.settings as settings
        settings.config.ALLOW_FORENSIC_ADOPT = False
        
        # Setup: ONE bot with invested qty, one with zero
        self._setup_longs([(1001, 'bot1', 1.0, 50000.0), (1002, 'bot2', 0.0, 0.0)])
        
        # Snapshot with net matching: 1.0 LONG
        mock_positions = [{
            'symbol': 'BTC/USDC:USDC',
            'side': 'long',
            'contracts': 1.0,
            'entryPrice': 50000.0,
        }]
        update_active_positions_snapshot(mock_positions)
        
        # Check active_positions
        cursor = self.conn.cursor()
        rows = cursor.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE pair='BTCUSDC'").fetchall()
        
        # Should have exactly 1 row assigned to bot1
        self.assertEqual(len(rows), 1, f"Expected 1 active position, got {len(rows)}: {rows}")
        self.assertEqual(rows[0][0], 1001, f"Expected bot_id=1001, got {rows[0][0]}")
        self.assertEqual(rows[0][3], 1.0, f"Expected size=1.0, got {rows[0][3]}")

    def test_multi_bot_blocked_when_forensic_disabled(self):
        """Multiple bots with invested qty should be blocked when forensic disabled."""
        import config.settings as settings
        settings.config.ALLOW_FORENSIC_ADOPT = False
        
        # Setup: TWO bots with invested qty
        self._setup_longs([(1001, 'bot1', 1.0, 50000.0), (1002, 'bot2', 0.5, 50000.0)])
        
        # Snapshot with net matching: 1.5 LONG
        mock_positions = [{
            'symbol': 'BTC/USDC:USDC',
            'side': 'long',
            'contracts': 1.5,
            'entryPrice': 50000.0,
        }]
        update_active_positions_snapshot(mock_positions)
        
        # Check active_positions - should be EMPTY (blocked, falls through to mismatch path)
        cursor = self.conn.cursor()
        rows = cursor.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE pair='BTCUSDC'").fetchall()
        
        # With forensic disabled, multi-bot split is blocked - no active_positions inserted here
        self.assertEqual(len(rows), 0, f"Expected 0 active positions (blocked), got {len(rows)}: {rows}")

    def test_multi_bot_allowed_when_forensic_enabled(self):
        """Multi-bot split should work when forensic adoption is enabled."""
        import config.settings as settings
        settings.config.ALLOW_FORENSIC_ADOPT = True

        # Setup: TWO bots with invested qty
        self._setup_longs([(1001, 'bot1', 1.0, 50000.0), (1002, 'bot2', 0.5, 50000.0)])

        # Snapshot with net matching: 1.5 LONG (1.0 + 0.5 = 1.5)
        mock_positions = [{
            'symbol': 'BTC/USDC:USDC',
            'side': 'long',
            'contracts': 1.5,  # This should be the SUM of both bots' qty
            'entryPrice': 50000.0,
        }]
        update_active_positions_snapshot(mock_positions)

        # Check active_positions - should have BOTH bots
        cursor = self.conn.cursor()
        rows = cursor.execute("SELECT bot_id, size FROM active_positions WHERE pair='BTCUSDC' ORDER BY bot_id").fetchall()

        self.assertEqual(len(rows), 2, f"Expected 2 active positions, got {len(rows)}: {rows}")
        self.assertEqual(rows[0][0], 1001)
        self.assertEqual(rows[1][0], 1002)
        # Size should be proportional to their quantities (1.0 and 0.5 out of 1.5 total)
        self.assertAlmostEqual(rows[0][1], 1.0, places=4)  # 1.0/1.5 * 1.5 = 1.0
        self.assertAlmostEqual(rows[1][1], 0.5, places=4)  # 0.5/1.5 * 1.5 = 0.5

    def test_zero_contributors_falls_through(self):
        """Zero bots with invested qty should fall through to mismatch path (assigns to active bot)."""
        import config.settings as settings
        settings.config.ALLOW_FORENSIC_ADOPT = False
        
        # Setup: bots with 0 invested qty but bot 1001 is active
        self._setup_longs([(1001, 'bot1', 0.0, 0.0), (1002, 'bot2', 0.0, 0.0)])
        
        # Snapshot with net matching
        mock_positions = [{
            'symbol': 'BTC/USDC:USDC',
            'side': 'long',
            'contracts': 1.0,
            'entryPrice': 50000.0,
        }]
        update_active_positions_snapshot(mock_positions)
        
        # Check active_positions - falls through to mismatch path, assigns to active bot 1001
        cursor = self.conn.cursor()
        rows = cursor.execute("SELECT bot_id, pair, side, size FROM active_positions WHERE pair='BTCUSDC'").fetchall()
        
        # Falls through to mismatch path which assigns to active bot (1001)
        self.assertEqual(len(rows), 1, f"Expected 1 active position (mismatch path), got {len(rows)}: {rows}")
        self.assertEqual(rows[0][0], 1001, f"Expected bot_id=1001 (active bot), got {rows[0][0]}")
        self.assertEqual(rows[0][3], 1.0, f"Expected size=1.0, got {rows[0][3]}")

    def test_short_position_single_contributor(self):
        """SHORT position with single contributor should work (abs(qty) > tolerance)."""
        import config.settings as settings
        settings.config.ALLOW_FORENSIC_ADOPT = False
        
        # Setup: SHORT bot with invested qty, LONG bot with zero
        self._setup_shorts([(2001, 'bot_short', 1.0, 50000.0)])
        self._setup_longs([(2002, 'bot_long', 0.0, 0.0)])
        
        # Snapshot with SHORT net matching: -1.0 SHORT
        mock_positions = [{
            'symbol': 'BTC/USDC:USDC',
            'side': 'short',
            'contracts': -1.0,
            'entryPrice': 50000.0,
        }]
        update_active_positions_snapshot(mock_positions)
        
        # Check active_positions
        cursor = self.conn.cursor()
        rows = cursor.execute("SELECT bot_id, side, size FROM active_positions WHERE pair='BTCUSDC'").fetchall()
        
        self.assertEqual(len(rows), 1, f"Expected 1 active position, got {len(rows)}: {rows}")
        self.assertEqual(rows[0][0], 2001, f"Expected bot_id=2001, got {rows[0][0]}")
        self.assertEqual(rows[0][1], 'SHORT', f"Expected SHORT, got {rows[0][1]}")
        # For SHORT, size is stored as positive in active_positions, side='SHORT' indicates direction
        self.assertEqual(abs(rows[0][2]), 1.0, f"Expected size magnitude=1.0, got {rows[0][2]}")

    def test_tiny_qty_below_tolerance_excluded(self):
        """Bot with qty below qty_tolerance() should NOT be counted as contributor."""
        import config.settings as settings
        settings.config.ALLOW_FORENSIC_ADOPT = False
        
        # Setup: 
        # - bot1: invested_qty = 1.0 (above tolerance 0.002) -> SHOULD be contributor
        # - bot2: invested_qty = 0.001 (BELOW tolerance 0.002) -> should be EXCLUDED
        self._setup_longs([(1001, 'bot1', 1.0, 50000.0), (1002, 'bot2', 0.001, 50000.0)])
        
        # Snapshot with net matching: 1.0 LONG (bot1's 1.0 + bot2's 0.001 ≈ 1.0)
        mock_positions = [{
            'symbol': 'BTC/USDC:USDC',
            'side': 'long',
            'contracts': 1.001,  # exchange reports combined
            'entryPrice': 50000.0,
        }]
        update_active_positions_snapshot(mock_positions)
        
        # Check active_positions - ONLY bot1 should get the position
        cursor = self.conn.cursor()
        rows = cursor.execute("SELECT bot_id, size FROM active_positions WHERE pair='BTCUSDC' ORDER BY bot_id").fetchall()
        
        # Only bot1 (above tolerance) should be assigned - bot2 (0.001 < 0.002) excluded
        self.assertEqual(len(rows), 1, f"Expected 1 active position (tiny bot excluded), got {len(rows)}: {rows}")
        self.assertEqual(rows[0][0], 1001, f"Expected bot_id=1001 (above tolerance), got {rows[0][0]}")
        self.assertEqual(rows[0][1], 1.0, f"Expected size=1.0 (bot1's share), got {rows[0][1]}")


if __name__ == '__main__':
    unittest.main()