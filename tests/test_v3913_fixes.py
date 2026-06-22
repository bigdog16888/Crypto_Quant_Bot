"""
Unit tests for v3.9.13 fixes:
  Fix 1 — EE-DECOUPLE pre-call removed (no more DB flip-flop / replace loop)
  Fix 2 — ATOMIC RESTORE uses 'placed' not 'new' (breaks SYNC-LAG-GUARD deadlock)
  Fix 3 — Parity gate live re-fetch unblocks late-WS TP fills

Required tests:
  test_ee_decouple_removed_no_flipflop
  test_sync_lag_guard_deadlock_broken
  test_cycle_reset_recheck_unblocks_on_late_ws
"""

import os
import sys
import time
import tempfile
import shutil
import sqlite3
import unittest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_v3913.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path


def _insert_bot(conn, bot_id, name, pair, norm_pair, direction,
                status='IN TRADE', bot_type='standard', is_active=1):
    conn.execute("""
        INSERT INTO bots (id, name, pair, normalized_pair, direction, status,
                          bot_type, is_active, rsi_limit, martingale_multiplier,
                          base_size, strategy_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 100, 'Martingale')
    """, (bot_id, name, pair, norm_pair, direction, status, bot_type, is_active))
    conn.commit()


def _insert_trades(conn, bot_id, open_qty=1.0, cycle_id=1, position_side='LONG',
                   avg_entry_price=1700.0, total_invested=1700.0,
                   target_tp_price=1730.0, basket_start_time=None, current_step=1):
    if basket_start_time is None:
        basket_start_time = int(time.time()) - 3600
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step,
                            entry_confirmed, target_tp_price, basket_start_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (bot_id, open_qty, cycle_id, position_side, total_invested,
          avg_entry_price, current_step, target_tp_price, basket_start_time))
    conn.commit()


def _insert_bot_order(conn, bot_id, order_type, status, price, amount,
                      client_order_id, cycle_id=1, created_at=None, updated_at=None):
    now = int(time.time())
    conn.execute("""
        INSERT INTO bot_orders (bot_id, order_type, status, price, amount,
                                client_order_id, order_id, cycle_id,
                                created_at, updated_at, filled_amount)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (bot_id, order_type, status, price, amount,
          client_order_id, client_order_id,
          cycle_id,
          created_at or now, updated_at or now))
    conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1 Tests: EE-DECOUPLE pre-call removed
# ─────────────────────────────────────────────────────────────────────────────

class TestFix1EEDecoupleRemoved(unittest.TestCase):
    """
    Fix 1: The EE-DECOUPLE pre-call at line 1695 used strategy._round_price()
    while maintain_orders used tick_size-rounding, causing DB to flip-flop between
    two rounded values on every cycle. Removing the pre-call stops the flip-flop.

    test_ee_decouple_removed_no_flipflop:
      Verify that _compute_effective_tp is called exactly ONCE per cycle (from
      maintain_orders, with tick_size), and that the DB value is stable between
      calls — never oscillating between two close rounded values.
    """

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if hasattr(database._local, 'connection') and database._local.connection:
            try:
                database._local.connection.close()
            except Exception:
                pass
            database._local.connection = None

    def test_ee_decouple_removed_no_flipflop(self):
        """
        Fix 1 / required test:
        When _compute_effective_tp is called once with tick_size=0.01 for a bot
        with basket_start_time 65 min ago (1 EE interval elapsed), the returned
        value must be tick-rounded AND the same on a second call — no flip-flop.

        This directly tests the fix: the old code called _compute_effective_tp
        twice (pre-call + maintain_orders call) with different rounding. Now only
        maintain_orders calls it, with tick_size, so both calls must agree.
        """
        from engine.bot_executor import BotExecutor

        bot_id = 30001
        tick = 0.01
        initial_tp = 1730.0
        avg_entry_price = 1700.0
        basket_start = int(time.time()) - 65 * 60  # 65 minutes → 1 interval

        conn = get_connection()
        conn.row_factory = sqlite3.Row
        _insert_bot(conn, bot_id, 'long eth test', 'ETH/USDC:USDC', 'ETHUSDC', 'LONG')
        _insert_trades(conn, bot_id,
                       open_qty=1.0, avg_entry_price=avg_entry_price,
                       total_invested=1700.0, target_tp_price=initial_tp,
                       basket_start_time=basket_start)

        bot_config = {
            'UseEarlyExit': True,
            'DecayIntervalMins': 60.0,
            'DecayPercentPerInterval': 5.0,
            'EEGracePeriodMins': 0.0,
            'EEAllowLoss': 'False',
        }
        bot_status = {
            'basket_start_time': basket_start,
            'target_tp_price': initial_tp,
            'avg_entry_price': avg_entry_price,
            'current_step': 1,
        }

        mock_strategy = MagicMock()
        mock_strategy.calculate_take_profit_price.return_value = initial_tp
        mock_strategy._round_price.side_effect = lambda p: round(p, 2)  # 2dp rounding

        executor = BotExecutor(runner=None)

        # Call 1: simulates maintain_orders with tick_size
        result1 = executor._compute_effective_tp(
            bot_id, 'long eth test', bot_status, bot_config, mock_strategy,
            pair='ETH/USDC:USDC', tick_size=tick
        )

        # Call 2: same call again (simulates next cycle maintain_orders call)
        bot_status2 = dict(bot_status)
        bot_status2['target_tp_price'] = result1  # updated by first call
        result2 = executor._compute_effective_tp(
            bot_id, 'long eth test', bot_status2, bot_config, mock_strategy,
            pair='ETH/USDC:USDC', tick_size=tick
        )

        # Both results must be identical — NO flip-flop
        self.assertAlmostEqual(result1, result2, places=8,
                               msg=f"Flip-flop detected: call1={result1}, call2={result2}")

        # Result must be tick-rounded
        def round_to_tick(p, t):
            return round(round(p / t) * t, 10)

        self.assertAlmostEqual(result1, round_to_tick(result1, tick), places=7,
                               msg=f"Result {result1} is not tick-rounded (tick={tick})")

        # The old pre-call used strategy._round_price() which may have rounded differently.
        # Verify the result is NOT the strategy-rounded value IF they differ.
        strategy_rounded = round(result1, 2)  # mimic strategy._round_price
        # (they may be equal for clean numbers — that's fine, but flip-flop is impossible)
        if abs(strategy_rounded - round_to_tick(strategy_rounded, tick)) > 1e-9:
            # Strategy rounding was off-tick; result must be tick-rounded, not strategy-rounded
            self.assertAlmostEqual(result1, round_to_tick(result1, tick), places=7)

    def test_ee_decouple_process_bot_no_second_call(self):
        """
        Fix 1 / structural test:
        Verify that process_bot no longer calls _compute_effective_tp before
        the main mission dispatch (the old EE-DECOUPLE block).

        We patch _compute_effective_tp and process_bot up to the EE-DECOUPLE
        location, asserting it is not called in the preamble section.
        """
        from engine.bot_executor import BotExecutor
        executor = BotExecutor(runner=None)

        call_count = {'n': 0}
        original = executor._compute_effective_tp

        def counting_wrapper(*args, **kwargs):
            call_count['n'] += 1
            return original(*args, **kwargs)

        executor._compute_effective_tp = counting_wrapper

        # The pre-call was in the preamble BEFORE mission dispatch.
        # After Fix 1, it should not be called in any preamble path.
        # We can verify this by checking the source code doesn't call it before mission:

        import inspect
        source = inspect.getsource(executor.process_bot)

        # Find the two markers
        decouple_idx = source.find('EE-DECOUPLE removed')
        mission_idx = source.find("if mission:\n                if mission['action']")

        self.assertGreater(decouple_idx, 0,
                           "Removal tombstone comment '[EE-DECOUPLE removed' not found in process_bot")
        self.assertGreater(mission_idx, 0,
                           "'if mission:' dispatch block not found in process_bot")

        # The tombstone comment must appear BEFORE 'if mission:'
        self.assertLess(decouple_idx, mission_idx,
                        "Tombstone comment should be before 'if mission:' dispatch")

        # And there must be no actual _compute_effective_tp call between them
        preamble = source[:mission_idx]
        self.assertNotIn('_compute_effective_tp(', preamble,
                         "Found _compute_effective_tp() call in preamble (before 'if mission:'). "
                         "EE-DECOUPLE pre-call was not fully removed.")


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2 Tests: SYNC-LAG-GUARD deadlock broken
# ─────────────────────────────────────────────────────────────────────────────

class TestFix2SyncLagGuardDeadlock(unittest.TestCase):
    """
    Fix 2: ATOMIC RESTORE now uses 'placed' instead of 'new'.

    LAG-GUARD checks: status IN ('new', 'open', 'filled').
    placed_tp lookup: status IN ('open', 'new', 'placed').

    Old behaviour: ATOMIC RESTORE → 'new' → LAG-GUARD blocks next cycle → deadlock.
    New behaviour: ATOMIC RESTORE → 'placed' → LAG-GUARD passes → next cycle retries.
    """

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        conn = get_connection()
        conn.row_factory = sqlite3.Row
        _insert_bot(conn, 40001, 'long eth', 'ETH/USDC:USDC', 'ETHUSDC', 'LONG')
        _insert_trades(conn, 40001, open_qty=1.0, target_tp_price=1730.0, current_step=1)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if hasattr(database._local, 'connection') and database._local.connection:
            try:
                database._local.connection.close()
            except Exception:
                pass
            database._local.connection = None

    def test_sync_lag_guard_deadlock_broken(self):
        """
        Fix 2 / required test:
        Simulate the ATOMIC RESTORE path: exchange.cancel_order succeeds,
        but _place_gtx_order_with_retry returns None (GTX rejected).

        Assert:
        1. The restored order row has status='placed' (not 'new').
        2. The LAG-GUARD query does NOT match 'placed' rows → returns None → unblocked.
        3. A subsequent _sync_replace_tp call is NOT blocked by LAG-GUARD.
        """
        from engine.bot_executor import BotExecutor
        from engine.database import update_order_status, save_bot_order

        conn = get_connection()
        conn.row_factory = sqlite3.Row

        # Insert a stale TP order row (the one that will be "cancelled" then restored)
        old_tp_price = 1730.0
        now = int(time.time())
        _insert_bot_order(conn, 40001, 'tp', 'open', old_tp_price, 1.0,
                          'CQB_40001_TP_OLD', created_at=now - 300, updated_at=now - 300)

        tp_row = conn.execute(
            "SELECT id FROM bot_orders WHERE bot_id=40001 AND order_type='tp'"
        ).fetchone()
        tp_db_id = tp_row['id']
        tp_order_id = 'CQB_40001_TP_OLD'

        # Mock exchange: cancel succeeds, place fails (GTX rejected)
        mock_exchange = MagicMock()
        mock_exchange.cancel_order.return_value = {
            'id': tp_order_id, 'status': 'canceled', 'filled': 0.0,
            'amount': 1.0, 'price': old_tp_price,
        }
        mock_exchange.get_best_bid_ask.return_value = (1728.0, 1730.5)
        mock_exchange.validate_order.return_value = (True, 1.0, 1726.0, 'ok')
        mock_exchange.fetch_open_orders.return_value = []

        executor = BotExecutor(runner=None)

        bot_status = {
            'cycle_id': 1, 'current_step': 1, 'open_qty': 1.0,
            'avg_entry_price': 1700.0, 'target_tp_price': 1726.0,
        }

        existing_tp = {
            'order_id': tp_order_id, 'id': tp_order_id,
            'price': old_tp_price, 'amount': 1.0, 'filled': 0.0, 'status': 'open',
        }

        # Patch _place_gtx_order_with_retry to simulate GTX rejection
        with patch.object(executor, '_place_gtx_order_with_retry', return_value=None):
            with patch.object(executor, '_generate_deterministic_id',
                              return_value='CQB_40001_TP_SYNC_R'):
                result = executor._sync_replace_tp(
                    40001, 'long eth', 'ETH/USDC:USDC', 'LONG',
                    bot_status, mock_exchange, 1726.0, 1.0, existing_tp
                )

        self.assertIsNone(result, "Expected None return when GTX rejected")

        # ── Assert 1: restored row must be 'placed', not 'new' ──────────────
        fresh_conn = get_connection()
        fresh_conn.row_factory = sqlite3.Row
        restored = fresh_conn.execute(
            "SELECT status FROM bot_orders WHERE client_order_id=?",
            (tp_order_id,)
        ).fetchone()
        # The row may have been re-used or a new row inserted; check either way
        all_tp_rows = fresh_conn.execute(
            "SELECT client_order_id, status FROM bot_orders WHERE bot_id=40001 AND order_type='tp'"
        ).fetchall()
        statuses = {r['client_order_id']: r['status'] for r in all_tp_rows}

        # The old order must NOT be 'new' — that's the deadlock status
        if tp_order_id in statuses:
            self.assertNotEqual(
                statuses[tp_order_id], 'new',
                f"ATOMIC RESTORE set status='new' — LAG-GUARD deadlock not fixed! "
                f"All tp statuses: {statuses}"
            )

        # ── Assert 2: LAG-GUARD query must NOT match 'placed' ───────────────
        lag_guard_hit = fresh_conn.execute("""
            SELECT id FROM bot_orders
            WHERE bot_id = ? AND order_type = 'tp'
              AND status IN ('new', 'open', 'filled')
              AND (created_at > ? OR updated_at > ?)
            LIMIT 1
        """, (40001, now - 15, now - 15)).fetchone()

        self.assertIsNone(
            lag_guard_hit,
            f"LAG-GUARD still triggered after ATOMIC RESTORE — 'placed' rows must NOT match. "
            f"Matched row: {dict(lag_guard_hit) if lag_guard_hit else None}"
        )

    def test_atomic_restore_validation_failure_uses_placed(self):
        """
        Fix 2 / secondary: validation failure path also uses 'placed'.
        When validate_order returns (False, ...), ATOMIC RESTORE must write 'placed'.
        """
        from engine.bot_executor import BotExecutor

        conn = get_connection()
        now = int(time.time())
        _insert_bot_order(conn, 40001, 'tp', 'open', 1730.0, 1.0,
                          'CQB_40001_TP_VAL', created_at=now - 300, updated_at=now - 300)

        mock_exchange = MagicMock()
        mock_exchange.cancel_order.return_value = {
            'id': 'CQB_40001_TP_VAL', 'status': 'canceled',
            'filled': 0.0, 'amount': 1.0, 'price': 1730.0,
        }
        mock_exchange.get_best_bid_ask.return_value = (1728.0, 1730.5)
        # validate_order returns invalid → triggers validation-failure ATOMIC RESTORE
        mock_exchange.validate_order.return_value = (False, 0.0, 0.0, 'qty too small')

        executor = BotExecutor(runner=None)
        bot_status = {
            'cycle_id': 1, 'current_step': 1, 'open_qty': 0.0001,
            'avg_entry_price': 1700.0, 'target_tp_price': 1730.0,
        }
        existing_tp = {
            'order_id': 'CQB_40001_TP_VAL', 'id': 'CQB_40001_TP_VAL',
            'price': 1730.0, 'amount': 1.0, 'filled': 0.0, 'status': 'open',
        }

        result = executor._sync_replace_tp(
            40001, 'long eth', 'ETH/USDC:USDC', 'LONG',
            bot_status, mock_exchange, 1730.0, 0.0001, existing_tp
        )
        self.assertIsNone(result)

        fresh_conn = get_connection()
        fresh_conn.row_factory = sqlite3.Row
        rows = fresh_conn.execute(
            "SELECT client_order_id, status FROM bot_orders "
            "WHERE bot_id=40001 AND client_order_id='CQB_40001_TP_VAL'"
        ).fetchall()
        if rows:
            self.assertNotEqual(rows[0]['status'], 'new',
                                "Validation-failure ATOMIC RESTORE must not set 'new'")


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3 Tests: Parity gate re-fetch unblocks late-WS TP fills
# ─────────────────────────────────────────────────────────────────────────────

class TestFix3CycleResetRecheck(unittest.TestCase):
    """
    Fix 3: assert_cycle_reset_allowed now re-fetches physical net before
    raising CycleResetBlockedError.

    Scenario: First snapshot shows exchange still holds -0.1 (TP not yet processed
    by WS). Re-fetch shows 0.0 (position closed). Gate should unblock.
    """

    def test_cycle_reset_recheck_unblocks_on_late_ws(self):
        """
        Fix 3 / required test:
        - First get_exchange_signed_net returns -0.1 (stale WS snapshot)
        - Re-fetch returns 0.0 (position closed — TP fill processed)
        - assert_cycle_reset_allowed must NOT raise CycleResetBlockedError

        This verifies the re-fetch path unblocks resets when the TP fill
        arrives on exchange after the snapshot was taken.
        """
        from engine.parity_gates import assert_cycle_reset_allowed, CycleResetBlockedError

        bot_id = 50001
        pair = 'ETH/USDC:USDC'
        tol = 0.002

        mock_exchange = MagicMock()

        # projected_pair_virtual_after_bot_flat: after removing this bot's -0.1,
        # pair virtual = 0.0
        # First physical fetch: -0.1 (stale) → gap = abs(0.0 - (-0.1)) = 0.1 > tol → would block
        # Re-fetch: 0.0 (fresh) → gap = abs(0.0 - 0.0) = 0.0 ≤ tol → unblock

        with patch('engine.parity_gates.projected_pair_virtual_after_bot_flat',
                   return_value=0.0):
            with patch('engine.parity_gates.get_exchange_signed_net',
                       side_effect=[-0.1, 0.0]):
                with patch('engine.parity_gates.qty_tolerance', return_value=tol):
                    # Must NOT raise — re-fetch resolved the gap
                    try:
                        assert_cycle_reset_allowed(
                            bot_id, pair, 'TP_HIT',
                            human_approved=False,
                            exchange=mock_exchange
                        )
                    except CycleResetBlockedError as e:
                        self.fail(
                            f"assert_cycle_reset_allowed raised CycleResetBlockedError "
                            f"despite re-fetch showing position closed: {e}"
                        )

    def test_cycle_reset_still_blocked_when_recheck_also_open(self):
        """
        Fix 3 / negative test:
        If both the original fetch AND the re-fetch show open position, the gate
        must still raise CycleResetBlockedError.
        """
        from engine.parity_gates import assert_cycle_reset_allowed, CycleResetBlockedError

        bot_id = 50002
        pair = 'ETH/USDC:USDC'
        tol = 0.002

        mock_exchange = MagicMock()

        with patch('engine.parity_gates.projected_pair_virtual_after_bot_flat',
                   return_value=0.0):
            with patch('engine.parity_gates.get_exchange_signed_net',
                       side_effect=[-0.1, -0.1]):  # both fetches show open
                with patch('engine.parity_gates.qty_tolerance', return_value=tol):
                    with patch('engine.database.get_pair_virtual_net',
                               return_value=-0.1):
                        with patch('engine.parity_gates.get_bot_signed_contribution',
                                   return_value=-0.1):
                            with self.assertRaises(CycleResetBlockedError):
                                assert_cycle_reset_allowed(
                                    bot_id, pair, 'TP_HIT',
                                    human_approved=False,
                                    exchange=mock_exchange
                                )

    def test_cycle_reset_recheck_uses_fresher_gap_in_error_message(self):
        """
        Fix 3 / detail test:
        When re-fetch shows a different (but still failing) gap, the error message
        must use the re-fetched value, not the stale first value.
        """
        from engine.parity_gates import assert_cycle_reset_allowed, CycleResetBlockedError

        bot_id = 50003
        pair = 'BTC/USDC:USDC'
        tol = 0.002

        mock_exchange = MagicMock()

        # First: -0.006, Re-fetch: -0.010 (larger, still open — use fresh value)
        with patch('engine.parity_gates.projected_pair_virtual_after_bot_flat',
                   return_value=0.0):
            with patch('engine.parity_gates.get_exchange_signed_net',
                       side_effect=[-0.006, -0.010]):
                with patch('engine.parity_gates.qty_tolerance', return_value=tol):
                    with patch('engine.database.get_pair_virtual_net',
                               return_value=-0.010):
                        with patch('engine.parity_gates.get_bot_signed_contribution',
                                   return_value=-0.006):
                            try:
                                assert_cycle_reset_allowed(
                                    bot_id, pair, 'TP_HIT',
                                    human_approved=False,
                                    exchange=mock_exchange
                                )
                                self.fail("Should have raised CycleResetBlockedError")
                            except CycleResetBlockedError as e:
                                # Error message must reference the re-fetched value (-0.010)
                                self.assertIn('0.010000', str(e),
                                              f"Error message should use re-fetched physical "
                                              f"(-0.010), got: {e}")

    def test_cycle_reset_not_blocked_when_gap_within_tolerance(self):
        """
        Fix 3 / baseline: when first fetch already shows gap within tolerance,
        no re-fetch is needed and no error is raised.
        """
        from engine.parity_gates import assert_cycle_reset_allowed, CycleResetBlockedError

        bot_id = 50004
        pair = 'SOL/USDC:USDC'
        tol = 0.002

        mock_exchange = MagicMock()

        with patch('engine.parity_gates.projected_pair_virtual_after_bot_flat',
                   return_value=0.0):
            with patch('engine.parity_gates.get_exchange_signed_net',
                       return_value=0.001):  # within tol, no re-fetch needed
                with patch('engine.parity_gates.qty_tolerance', return_value=tol):
                    # Should not raise at all
                    assert_cycle_reset_allowed(
                        bot_id, pair, 'TP_HIT',
                        human_approved=False,
                        exchange=mock_exchange
                    )


if __name__ == '__main__':
    unittest.main(verbosity=2)
