# tests/test_live_guard_inv30_saturation_guard.py
"""
Test that LIVE_GUARD_INV30 reconciliation markers don't create a saturation guard gap.

These markers represent TRUE exchange position (reconciliation rows), not exchange fills.
They should:
1. Be EXCLUDED from the step saturation guard (prevent false saturation)
2. Be INCLUDED in position computation (recompute_invested_from_orders)
3. NOT block legitimate fills for the same step
"""
import pytest
import sqlite3
import time
import sys
sys.path.insert(0, 'D:/Crypto_Quant_Bot')

from engine.ledger import credit_fill
from engine.database import get_connection, init_db, recompute_invested_from_orders


class TestLiveGuardInv30SaturationGuard:
    """Test that LIVE_GUARD_INV30 markers don't create a saturation guard gap."""

    def setup_method(self):
        # Use real DB for integration test - get fresh connection each test
        init_db()
        self.conn = get_connection()
        self.bot_id = 100999  # Unique test bot
        
        # Insert bot and trades row
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO bots (id, name, status, direction, pair, normalized_pair, is_active)
            VALUES (?, 'test_live_guard_bot', 'IN TRADE', 'LONG', 'BTC/USDC:USDC', 'BTCUSDC', 1)
        """, (self.bot_id,))
        cursor.execute("""
            INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, 
                                           current_step, entry_confirmed, cycle_phase, position_side)
            VALUES (?, 1, 0.0, 0.0, 0.0, 0, 0, 'IDLE', 'LONG')
        """, (self.bot_id,))
        self.conn.commit()

    def teardown_method(self):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM bot_orders WHERE bot_id = ?", (self.bot_id,))
        cursor.execute("DELETE FROM fill_claims WHERE bot_id = ?", (self.bot_id,))
        cursor.execute("DELETE FROM trades WHERE bot_id = ?", (self.bot_id,))
        cursor.execute("DELETE FROM bots WHERE id = ?", (self.bot_id,))
        self.conn.commit()

    def test_live_guard_marker_excluded_from_saturation_guard(self):
        """
        Verify that LIVE_GUARD_INV30 markers are EXCLUDED from the step saturation guard's
        already_credited sum. This is the core fix - they should NOT cause false saturation.
        """
        cursor = self.conn.cursor()
        
        # Insert only LIVE_GUARD_INV30 marker (reconciliation status)
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, 
                                    status, created_at, updated_at, client_order_id, cycle_id, position_side)
            VALUES (?, 1, 'entry', 'EXCH_LIVE_GUARD_123', 50000.0, 0.1, 0.1, 'reconciliation', 
                    ?, ?, 'CQB_100999_LIVE_GUARD_INV30_1_1', 1, 'LONG')
        """, (self.bot_id, int(time.time()), int(time.time())))
        self.conn.commit()
        
        # Insert a legitimate order for same step (different order_id)
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, 
                                    status, created_at, updated_at, client_order_id, cycle_id, position_side)
            VALUES (?, 1, 'entry', 'EXCH_LEGIT_789', 50000.0, 0.05, 0.0, 'open', 
                    ?, ?, 'CQB_100999_ENTRY_1_2', 1, 'LONG')
        """, (self.bot_id, int(time.time()), int(time.time())))
        legit_order_id = cursor.lastrowid
        self.conn.commit()
        
        # Credit the legitimate order (smaller qty - this is a real new fill)
        # With the fix: already_credited=0 (reconciliation excluded), capacity=0.05*1.05=0.0525
        # 0 + 0.05 = 0.05 <= 0.0525 → succeeds
        # WITHOUT fix: already_credited=0.1 (reconciliation included), 0.1 + 0.05 = 0.15 > 0.0525 → FALSE saturation!
        result = credit_fill(
            bot_id=self.bot_id,
            order_id='EXCH_LEGIT_789',
            cumulative_qty=0.05,     # Different qty - not a duplicate
            avg_price=50000.0,
            order_type='entry',
            is_cumulative=True,
            caller='test_legit_fill',
            side='buy',
        )
        
        # Should succeed - live guard marker excluded, so already_credited=0
        assert result is True, "Legitimate fill should be credited (live guard excluded from saturation)"

    def test_recompute_includes_reconciliation_markers(self):
        """
        Verify that recompute_invested_from_orders includes LIVE_GUARD_INV30 markers
        in position computation (they represent true exchange position).
        """
        cursor = self.conn.cursor()
        
        # Insert LIVE_GUARD_INV30 marker
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, 
                                    status, created_at, updated_at, client_order_id, cycle_id, position_side)
            VALUES (?, 1, 'entry', 'EXCH_LIVE_GUARD_123', 50000.0, 0.1, 0.1, 'reconciliation', 
                    ?, ?, 'CQB_100999_LIVE_GUARD_INV30_1_1', 1, 'LONG')
        """, (self.bot_id, int(time.time()), int(time.time())))
        self.conn.commit()
        
        # recompute_invested_from_orders should include the reconciliation marker
        total_invested, avg_price, total_qty, max_step = recompute_invested_from_orders(self.bot_id)
        
        # Should see the 0.1 qty from the reconciliation marker
        assert total_qty == 0.1, f"Expected 0.1 qty from reconciliation marker, got {total_qty}"
        assert total_invested == 5000.0, f"Expected 5000.0 invested, got {total_invested}"
        assert avg_price == 50000.0, f"Expected 50000.0 avg price, got {avg_price}"

    def test_saturation_guard_catches_historical_duplicate(self):
        """
        Verify step saturation guard catches duplicates for fills that bypass step lock.
        
        Scenario: First fill happened in a previous cycle (step lock already released),
        or was credited by a different code path. Now a duplicate fill attempt comes in
        for the SAME step from a DIFFERENT order_id (GTX chase replay).
        
        This test uses the SAME step but sequential fills (step lock released after first).
        """
        cursor = self.conn.cursor()
        
        # 1. Insert LIVE_GUARD_INV30 marker for step 1 (excluded from saturation)
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, 
                                    status, created_at, updated_at, client_order_id, cycle_id, position_side)
            VALUES (?, 1, 'entry', 'EXCH_LIVE_GUARD_123', 50000.0, 0.1, 0.1, 'reconciliation', 
                    ?, ?, 'CQB_100999_LIVE_GUARD_INV30_1_1', 1, 'LONG')
        """, (self.bot_id, int(time.time()), int(time.time())))
        
        # 2. Insert order A for step 2
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, 
                                    status, created_at, updated_at, client_order_id, cycle_id, position_side)
            VALUES (?, 2, 'entry', 'EXCH_ORDER_A_456', 50000.0, 0.1, 0.0, 'open', 
                    ?, ?, 'CQB_100999_ENTRY_2_A', 1, 'LONG')
        """, (self.bot_id, int(time.time()), int(time.time())))
        order_a_id = cursor.lastrowid
        
        # 3. Insert order B for step 2 (GTX chase duplicate)
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, 
                                    status, created_at, updated_at, client_order_id, cycle_id, position_side)
            VALUES (?, 2, 'entry', 'EXCH_ORDER_B_789', 50000.0, 0.1, 0.0, 'open', 
                    ?, ?, 'CQB_100999_ENTRY_2_B', 1, 'LONG')
        """, (self.bot_id, int(time.time()), int(time.time())))
        order_b_id = cursor.lastrowid
        self.conn.commit()
        
        # 4. Order A gets filled - legitimate
        result_a = credit_fill(
            bot_id=self.bot_id,
            order_id='EXCH_ORDER_A_456',
            cumulative_qty=0.1,
            avg_price=50000.0,
            order_type='entry',
            is_cumulative=True,
            caller='test_order_a_fill',
            side='buy',
        )
        assert result_a is True, "Order A should be credited"
        
        # 5. NOW: simulate step lock being released (by clearing fill_claims for this step)
        # This simulates a historical fill that already completed, then a replay attempt
        cursor.execute("DELETE FROM fill_claims WHERE bot_id = ? AND order_id LIKE 'STEP_2_1'", (self.bot_id,))
        self.conn.commit()
        
        # 6. Order B (GTX chase replay) tries to fill - should be caught by step saturation
        # already_credited = 0.1 (from order A, step 2, status='filled')
        # LIVE_GUARD_INV30 at step 1 is EXCLUDED (different step)
        # capacity = 0.105, already_credited + delta = 0.1 + 0.1 = 0.2 > 0.105
        result_b = credit_fill(
            bot_id=self.bot_id,
            order_id='EXCH_ORDER_B_789',
            cumulative_qty=0.1,
            avg_price=50000.0,
            order_type='entry',
            is_cumulative=True,
            caller='test_order_b_fill',
            side='buy',
        )
        
        # Should be caught by step saturation guard (step lock released)
        assert result_b is False, "Step saturation should catch GTX chase duplicate (order B)"
        
        # Verify order B was marked auto_closed
        cursor.execute("SELECT status FROM bot_orders WHERE id = ?", (order_b_id,))
        status = cursor.fetchone()[0]
        assert status == 'auto_closed', f"Expected auto_closed for order B, got {status}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])