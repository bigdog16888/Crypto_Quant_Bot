"""
Regression test for §5 A7 RESURRECTED_GHOST via deflate_pair_ledger_overcount.

Background (this investigation, 2026-07-17):
  deflate_pair_ledger_overcount() trims bot_orders.filled_amount to remove a
  phantom ledger over-count (virtual > exchange on the SAME sign). Previously it
  only adjusted filled_amount and LEFT status='filled', so on the next
  seal_trade_state / reconciler cycle the row was re-adopted as a real fill and
  the phantom net resurrects.

NOTE on the live mismatch this investigation found:
  BTC pair was virtual -0.064 vs exchange +0.064 (OPPOSITE signs) and SUI was
  virtual -6.4 vs exchange 0.0 (flat). Those take the purge/orphan branches of
  reconcile_pair_to_exchange, NOT deflate. This test hardens the SAME-SIGN
  over-count branch (virtual +0.284 vs exchange +0.100), which is a genuine
  latent A7 vector that deflate handles.

Rule 10: a fully-consumed row must be terminal-statused (reset_cleared); a
  partially-trimmed row with genuine remaining exposure must stay 'filled'
  (the negative case — the fix must not overreach).

Rule 11: zero-comparison uses the codebase's exact-zero convention
  (filled_amount <= 0), not an ad-hoc epsilon.

This test asserts BOTH directions:
  POSITIVE: a fully-consumed row flips to status='reset_cleared' (and is not
            re-adopted by a subsequent recompute).
  NEGATIVE: a partially-trimmed row (still real remaining exposure) stays
            status='filled'.
"""
import os
import sys
import time
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.database as database
from engine.database import get_connection, init_db
from engine.parity_gates import deflate_pair_ledger_overcount
from engine.ledger import seal_trade_state


def _make_temp_db():
    d = tempfile.mkdtemp()
    db_path = os.path.join(d, 'test_deflate_a7.db')
    database.DB_PATH = db_path
    database._local = database.threading.local()
    init_db()
    return d, db_path


def _insert_bot(conn, bot_id, name, pair, norm_pair, direction,
                status='IN TRADE', bot_type='standard', is_active=1):
    conn.execute(
        """INSERT INTO bots (id, name, pair, normalized_pair, direction,
                             status, bot_type, is_active,
                             rsi_limit, martingale_multiplier, base_size, strategy_type, cascade_started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, 0, 'Martingale', 0)""",
        (bot_id, name, pair, norm_pair, direction, status, bot_type, is_active),
    )
    conn.commit()


def _insert_trades(conn, bot_id, open_qty=0.0, cycle_id=1, position_side='LONG',
                   avg_entry_price=0.0):
    conn.execute(
        """INSERT INTO trades (bot_id, open_qty, cycle_id, position_side,
                                total_invested, avg_entry_price, current_step, entry_confirmed, basket_start_time)
           VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)""",
        (bot_id, open_qty, cycle_id, position_side, open_qty * avg_entry_price,
         avg_entry_price, int(time.time())),
    )
    conn.commit()


def _insert_order(conn, bot_id, order_type, amount, filled_amount, price, status,
                  cycle_id=1, position_side='LONG'):
    conn.execute(
        """INSERT INTO bot_orders (bot_id, order_type, amount, filled_amount, price,
                                    status, cycle_id, created_at, updated_at, position_side)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (bot_id, order_type, amount, filled_amount, price, status, cycle_id,
         int(time.time()), int(time.time()), position_side),
    )
    conn.commit()


class TestDeflateA7ResetCleared(unittest.TestCase):
    def setUp(self):
        self._d, self._db = _make_temp_db()
        self.conn = get_connection()

    def tearDown(self):
        self.conn.close()
        try:
            os.remove(self._db)
        except OSError:
            pass
        try:
            os.rmdir(self._d)
        except OSError:
            pass

    def _row_status(self, order_id):
        return self.conn.execute(
            "SELECT status FROM bot_orders WHERE id=?", (order_id,)
        ).fetchone()[0]

    # bot 10016 holds two real grid fills: 0.200 (big) + 0.084 (small) = 0.284.
    # Exchange net patched to +0.100 (same sign, beyond tol) -> excess = 0.184.
    # Deflate trims 0.184: small row (0.084) FULLY consumed -> reset_cleared;
    # big row retains 0.100 real residual -> stays filled.
    _VIRTUAL = 0.284
    _PHYSICAL = 0.100

    def _setup_two_rows(self):
        _insert_bot(self.conn, 10016, 'long btc price', 'BTC/USDC', 'BTCUSDC', 'LONG')
        _insert_trades(self.conn, 10016, open_qty=self._VIRTUAL, cycle_id=68, position_side='LONG')
        # insert big row first (lower id) so ORDER BY id DESC processes small first
        _insert_order(self.conn, 10016, 'grid', 0.200, 0.200, 64200.0,
                      'filled', cycle_id=68, position_side='LONG')
        _insert_order(self.conn, 10016, 'grid', 0.084, 0.084, 64300.0,
                      'filled', cycle_id=68, position_side='LONG')

    # ---- POSITIVE: a fully-consumed row must become reset_cleared ----
    def test_fully_consumed_row_becomes_reset_cleared(self):
        self._setup_two_rows()
        small_id = self.conn.execute(
            "SELECT id FROM bot_orders WHERE bot_id=10016 AND filled_amount=0.084"
        ).fetchone()[0]

        with patch('engine.parity_gates.get_exchange_signed_net', return_value=self._PHYSICAL):
            msg = deflate_pair_ledger_overcount(None, 'BTC/USDC')

        self.assertIsNotNone(msg)
        self.assertEqual(self._row_status(small_id), 'reset_cleared',
                         "fully-consumed orphaned fill must be reset_cleared (Rule 10)")
        small_fill = self.conn.execute(
            "SELECT filled_amount FROM bot_orders WHERE id=?", (small_id,)
        ).fetchone()[0]
        self.assertEqual(small_fill, 0.0)

    def test_fully_consumed_row_not_re_adopted_on_recompute(self):
        self._setup_two_rows()
        with patch('engine.parity_gates.get_exchange_signed_net', return_value=self._PHYSICAL):
            deflate_pair_ledger_overcount(None, 'BTC/USDC')
        # Simulate the next recompute cycle reading the (now reset_cleared) row.
        seal_trade_state(10016)
        new_open_qty = self.conn.execute(
            "SELECT open_qty FROM trades WHERE bot_id=10016"
        ).fetchone()[0]
        # small row reset_cleared (0); big row retains 0.100 real residual.
        # virtual = 0.100, matches patched exchange net (0.100).
        self.assertAlmostEqual(new_open_qty, self._PHYSICAL, delta=0.002,
                                msg="phantom must NOT resurrect after recompute (A7)")

    # ---- NEGATIVE: partially-trimmed row must STAY 'filled' ----
    def test_partially_trimmed_row_stays_filled(self):
        self._setup_two_rows()
        big_id = self.conn.execute(
            "SELECT id FROM bot_orders WHERE bot_id=10016 AND filled_amount=0.200"
        ).fetchone()[0]

        with patch('engine.parity_gates.get_exchange_signed_net', return_value=self._PHYSICAL):
            deflate_pair_ledger_overcount(None, 'BTC/USDC')

        # Big row partially trimmed (0.200 -> 0.100 real residual) -> MUST stay 'filled'
        self.assertEqual(self._row_status(big_id), 'filled',
                         "partially-trimmed row with real remaining exposure must stay 'filled' (no overreach)")
        big_fill = self.conn.execute(
            "SELECT filled_amount FROM bot_orders WHERE id=?", (big_id,)
        ).fetchone()[0]
        self.assertAlmostEqual(big_fill, 0.100, delta=0.002)


if __name__ == '__main__':
    unittest.main()
