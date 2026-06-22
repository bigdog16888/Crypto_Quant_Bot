"""
Unit tests for v3.9.12 INV-21 fix:
EE (Early Exit) tick-size precision in maintain_orders comparison.

Tests:
    1. test_ee_no_replace_subtick
       new_ee_tp=1796.3033, placed_tp=1796.30, tick=0.10
       → ee_interval_fired must be False  (sub-tick diff is noise, not a real step)

    2. test_ee_replaces_on_full_tick
       new_ee_tp=1796.40, placed_tp=1796.30, tick=0.10
       → ee_interval_fired must be True   (full tick step has advanced)

    3. test_compute_effective_tp_persists_rounded
       _compute_effective_tp must write the exchange tick-rounded value to
       trades.target_tp_price, NOT the raw float from calculate_early_exit_decay.

INV-21 Codebase Rule:
    All TP price comparisons in maintain_orders must use exchange tick-size-rounded
    values on both sides. _compute_effective_tp must persist tick-rounded values to
    trades.target_tp_price. Sub-tick floating-point differences are not meaningful
    price changes and must never trigger a cancel/replace cycle.
"""

import os
import sys
import time
import math
import tempfile
import shutil
import sqlite3
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.bot_executor import BotExecutor


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_v3912.db')
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
                   avg_entry_price=1790.0, total_invested=1790.0,
                   target_tp_price=1796.30, basket_start_time=None):
    if basket_start_time is None:
        basket_start_time = int(time.time()) - 3600  # 1 hour ago
    conn.execute("""
        INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                            total_invested, avg_entry_price, current_step,
                            entry_confirmed, target_tp_price, basket_start_time)
        VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
    """, (bot_id, open_qty, cycle_id, position_side, total_invested,
          avg_entry_price, target_tp_price, basket_start_time))
    conn.commit()


def _round_to_tick(price: float, tick: float) -> float:
    """Reference implementation — mirrors the production helper in maintain_orders."""
    if tick > 0:
        return round(round(price / tick) * tick, 10)
    return price


# ---------------------------------------------------------------------------
# Test 1 & 2: ee_interval_fired logic (pure function, no DB required)
# ---------------------------------------------------------------------------

class TestEEIntervalFiredComparison(unittest.TestCase):
    """
    Validates the INV-21 tick-rounded comparison that guards ee_interval_fired.

    The production code now does:
        _rnd_new_ee = _round_to_tick_mo(new_ee_tp, _ee_tick)
        _rnd_placed  = _round_to_tick_mo(placed_tp,  _ee_tick)
        ee_interval_fired = (
            abs(_rnd_new_ee - _rnd_placed) >= max(_ee_tick, 1e-9)
            and _rnd_new_ee > 0
            and _rnd_placed > 0
        )
    These tests exercise that logic directly via the reference helper.
    """

    def _compute_ee_interval_fired(self, new_ee_tp: float, placed_tp: float,
                                    tick: float) -> bool:
        """Reproduce the production ee_interval_fired expression."""
        rnd_new = _round_to_tick(new_ee_tp, tick)
        rnd_placed = _round_to_tick(placed_tp, tick)
        return (
            abs(rnd_new - rnd_placed) >= max(tick, 1e-9)
            and rnd_new > 0
            and rnd_placed > 0
        )

    # ------------------------------------------------------------------
    # Test 1
    # ------------------------------------------------------------------
    def test_ee_no_replace_subtick(self):
        """
        INV-21 / test 1:
        new_ee_tp=1796.3033 (sub-tick float from EE decay),
        placed_tp=1796.30   (exchange-rounded value stored in bot_orders),
        tick=0.10           (XAUUSDT tick size).

        Rounding both to tick gives 1796.3 vs 1796.3 → difference is 0, which is
        < tick (0.10).  Therefore ee_interval_fired MUST be False.
        This was the root cause of the infinite cancel/replace loop before v3.9.12.
        """
        new_ee_tp  = 1796.3033
        placed_tp  = 1796.30
        tick       = 0.10

        fired = self._compute_ee_interval_fired(new_ee_tp, placed_tp, tick)

        self.assertFalse(
            fired,
            msg=(
                f"ee_interval_fired should be False for sub-tick difference: "
                f"new_ee_tp={new_ee_tp}, placed_tp={placed_tp}, tick={tick}. "
                f"Rounded: {_round_to_tick(new_ee_tp, tick)} vs "
                f"{_round_to_tick(placed_tp, tick)}"
            )
        )

    # ------------------------------------------------------------------
    # Test 2
    # ------------------------------------------------------------------
    def test_ee_replaces_on_full_tick(self):
        """
        INV-21 / test 2:
        new_ee_tp=1796.40 (EE has stepped down one full tick),
        placed_tp=1796.30,
        tick=0.10.

        Rounding: 1796.4 vs 1796.3 → difference = 0.10 == tick.
        Therefore ee_interval_fired MUST be True — a real EE interval fired.
        """
        new_ee_tp  = 1796.40
        placed_tp  = 1796.30
        tick       = 0.10

        fired = self._compute_ee_interval_fired(new_ee_tp, placed_tp, tick)

        self.assertTrue(
            fired,
            msg=(
                f"ee_interval_fired should be True for full-tick step: "
                f"new_ee_tp={new_ee_tp}, placed_tp={placed_tp}, tick={tick}. "
                f"Rounded: {_round_to_tick(new_ee_tp, tick)} vs "
                f"{_round_to_tick(placed_tp, tick)}"
            )
        )

    def test_ee_no_replace_zero_tick(self):
        """
        Edge case: tick_size=0 (symbol precision not available).
        The helper returns price unchanged (no rounding).
        new_ee_tp exactly equals placed_tp → no replace.
        """
        new_ee_tp  = 1796.30
        placed_tp  = 1796.30
        tick       = 0.0

        fired = self._compute_ee_interval_fired(new_ee_tp, placed_tp, tick)

        self.assertFalse(fired, "Equal prices with tick=0 must not fire.")

    def test_ee_replaces_multi_tick(self):
        """
        Two full ticks apart (e.g. 60-minute interval fired twice):
        new_ee_tp=1796.10, placed_tp=1796.30, tick=0.10 → fired=True.
        """
        fired = self._compute_ee_interval_fired(1796.10, 1796.30, 0.10)
        self.assertTrue(fired, "Two-tick difference should fire ee_interval_fired.")

    def test_ee_no_replace_sub_tick_variety(self):
        """
        Various sub-tick float differences all round to the same tick value.
        None of them should fire.
        """
        placed_tp = 1796.30
        tick      = 0.10
        sub_tick_variants = [
            1796.3001,
            1796.3099,
            1796.2999,
            1796.299999,
        ]
        for v in sub_tick_variants:
            with self.subTest(new_ee_tp=v):
                fired = self._compute_ee_interval_fired(v, placed_tp, tick)
                self.assertFalse(
                    fired,
                    f"Sub-tick variant {v} should NOT fire (tick={tick}, placed={placed_tp})"
                )


# ---------------------------------------------------------------------------
# Test 3: _compute_effective_tp persists tick-rounded value to DB
# ---------------------------------------------------------------------------

class TestComputeEffectiveTpPersistsRounded(unittest.TestCase):
    """
    INV-21 / test 3:
    Verify that when _compute_effective_tp is called with tick_size, it writes
    a tick-rounded value to trades.target_tp_price, NOT the raw sub-tick float
    returned by calculate_early_exit_decay.
    """

    def setUp(self):
        self.test_dir, self.db_path = _make_temp_db()
        self.conn = get_connection()
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if hasattr(database._local, 'connection') and database._local.connection:
            try:
                database._local.connection.close()
            except Exception:
                pass
            database._local.connection = None

    def test_compute_effective_tp_persists_rounded(self):
        """
        _compute_effective_tp must store a tick-rounded value.

        Setup:
        - bot has basket_start_time 65 minutes ago
        - DecayIntervalMins=60, DecayPercentPerInterval=30, tick_size=0.10
        - initial_tp=1796.30, avg_entry_price=1790.00

        After 65 minutes, intervals_passed = floor(65/60) = 1.
        decay_factor = 1.0 - 0.30 = 0.70
        adjusted_tp = 1790.00 + (1796.30 - 1790.00) * 0.70 = 1790.00 + 4.41 = 1794.41

        Raw float = 1794.41  (already on tick for 0.10 in this case, but the test
        asserts the DB value matches tick rounding, not a sub-tick artifact).

        We force a sub-tick scenario by using a non-0.10-aligned initial_tp:
        initial_tp=1796.33 → adjusted_tp = 1790.00 + 6.33*0.70 = 1790.00 + 4.431 = 1794.431
        Tick-rounded = 1794.40 (nearest 0.10).

        The DB must store 1794.40, NOT 1794.431.
        """
        bot_id = 20001
        pair   = 'XAUUSDT'
        tick   = 0.10
        initial_tp      = 1796.33   # deliberately NOT on tick boundary
        avg_entry_price = 1790.00
        target_stored   = initial_tp  # raw unrounded initial value in DB

        basket_start = int(time.time()) - 65 * 60  # 65 minutes ago

        _insert_bot(self.conn, bot_id, 'short gold', pair, pair, 'SHORT')
        _insert_trades(
            self.conn, bot_id,
            open_qty=0.1,
            avg_entry_price=avg_entry_price,
            total_invested=179.0,
            target_tp_price=target_stored,
            basket_start_time=basket_start
        )

        bot_config = {
            'UseEarlyExit': True,
            'DecayIntervalMins': 60.0,
            'DecayPercentPerInterval': 30.0,
            'EEGracePeriodMins': 0.0,
            'EEAllowLoss': 'False',
        }
        bot_status = {
            'basket_start_time': basket_start,
            'target_tp_price': target_stored,
            'avg_entry_price': avg_entry_price,
            'current_step': 0,
        }

        # Mock strategy: calculate_take_profit_price returns the initial_tp
        mock_strategy = MagicMock()
        mock_strategy.calculate_take_profit_price.return_value = initial_tp
        mock_strategy._round_price.side_effect = lambda p: p  # identity fallback

        executor = BotExecutor(runner=None)

        result = executor._compute_effective_tp(
            bot_id, 'short gold', bot_status, bot_config, mock_strategy,
            pair=pair, tick_size=tick
        )

        # --- Verify return value is tick-rounded ---
        # Use round-trip check: if the value is on the tick grid, rounding it again
        # produces the same value.  Modulo arithmetic is unreliable for binary floats
        # (e.g. 1794.4 % 0.1 ≈ 0.1 in IEEE 754 even though 1794.4 IS on the grid).
        self.assertAlmostEqual(
            result, _round_to_tick(result, tick), places=7,
            msg=f"Returned value {result} is not on the tick grid (tick={tick})"
        )

        # --- Verify DB was updated with the tick-rounded value ---
        # _compute_effective_tp opens and closes its own get_connection() call,
        # which closes the thread-local connection.  Re-open a fresh one.
        fresh_conn = get_connection()
        fresh_conn.row_factory = sqlite3.Row
        row = fresh_conn.execute(
            "SELECT target_tp_price FROM trades WHERE bot_id=?", (bot_id,)
        ).fetchone()
        self.assertIsNotNone(row, "trades row missing for bot_id")

        db_value = float(row[0])
        self.assertAlmostEqual(
            db_value, _round_to_tick(db_value, tick), places=7,
            msg=f"DB stored value {db_value} is not on the tick grid (tick={tick})"
        )


        # --- Verify stored value matches returned value ---
        self.assertAlmostEqual(
            db_value, result, places=7,
            msg=f"DB stored {db_value} does not match returned {result}"
        )

        # --- Verify the value changed from the raw initial (decay applied) ---
        self.assertNotAlmostEqual(
            db_value, target_stored, places=2,
            msg="DB should have been updated after 65 mins (1 EE interval fired)"
        )

        # --- Verify DB value is NOT the raw float (sub-tick guard) ---
        # intervals_passed=1, decay_factor=0.70
        raw_decayed = avg_entry_price + (initial_tp - avg_entry_price) * 0.70
        if abs(raw_decayed % tick) > 1e-9:
            # Only assert when the raw float is genuinely off-tick
            self.assertNotAlmostEqual(
                db_value, raw_decayed, places=6,
                msg=f"DB stored the raw float {raw_decayed} instead of tick-rounded value"
            )
        else:
            # Raw happens to be on-tick — just verify it matches the rounding
            expected = _round_to_tick(raw_decayed, tick)
            self.assertAlmostEqual(db_value, expected, places=7)

    def test_compute_effective_tp_no_update_when_same_step(self):
        """
        Between EE intervals, if the tick-rounded new value equals the tick-rounded
        stored value, the DB must NOT be updated (no spurious write).

        Scenario: basket_start_time is 30 minutes ago, interval=60 mins.
        intervals_passed = floor(30/60) = 0 → decay_factor = 1.0 → decayed_tp = initial_tp.
        Stored target_tp_price is already initial_tp → no DB write expected.
        """
        bot_id = 20002
        pair   = 'XAUUSDT'
        tick   = 0.10
        initial_tp      = 1796.30
        avg_entry_price = 1790.00
        target_stored   = initial_tp

        basket_start = int(time.time()) - 30 * 60  # only 30 minutes — no interval yet

        _insert_bot(self.conn, bot_id, 'short gold 2', pair, pair, 'SHORT')
        _insert_trades(
            self.conn, bot_id,
            open_qty=0.1,
            avg_entry_price=avg_entry_price,
            total_invested=179.0,
            target_tp_price=target_stored,
            basket_start_time=basket_start
        )

        bot_config = {
            'UseEarlyExit': True,
            'DecayIntervalMins': 60.0,
            'DecayPercentPerInterval': 30.0,
            'EEGracePeriodMins': 0.0,
            'EEAllowLoss': 'False',
        }
        bot_status = {
            'basket_start_time': basket_start,
            'target_tp_price': target_stored,
            'avg_entry_price': avg_entry_price,
            'current_step': 0,
        }

        mock_strategy = MagicMock()
        mock_strategy.calculate_take_profit_price.return_value = initial_tp
        mock_strategy._round_price.side_effect = lambda p: p

        executor = BotExecutor(runner=None)

        with patch('engine.bot_executor.get_connection') as mock_gc:
            mock_conn = MagicMock()
            mock_gc.return_value = mock_conn

            result = executor._compute_effective_tp(
                bot_id, 'short gold 2', bot_status, bot_config, mock_strategy,
                pair=pair, tick_size=tick
            )

            # No interval has fired — decayed_tp == initial_tp → no DB write
            mock_conn.execute.assert_not_called()

        self.assertAlmostEqual(result, initial_tp, places=4,
                               msg="Should return initial_tp when no interval fired")


if __name__ == '__main__':
    unittest.main(verbosity=2)
