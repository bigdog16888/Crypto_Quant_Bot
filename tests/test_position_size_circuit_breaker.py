"""tests/test_position_size_circuit_breaker.py — O-1 position-size circuit breaker.

O-1 (rebuild of the original untracked test that was lost). Exercises the 4 live
helpers in engine/database.py against a minimal in-memory schema — the exact
tables/columns each function reads. Pure-function core + mock.patch for get_connection.
"""
import unittest
import sqlite3
from unittest import mock

from engine import database as db


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE bots (id INTEGER PRIMARY KEY, base_size REAL, "
        "martingale_multiplier REAL, config TEXT, status TEXT, notes TEXT)"
    )
    conn.execute(
        "CREATE TABLE trades (bot_id INTEGER PRIMARY KEY, total_invested REAL)"
    )
    return conn


class _DBTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        self._patch = mock.patch.object(db, "get_connection", return_value=self.conn)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _clear(self):
        for t in ("trades", "bots"):
            self.conn.execute(f"DELETE FROM {t}")
        self.conn.commit()


class CalculateConfigMaxNotional(_DBTestBase):
    def test_geometric_sum(self):
        self.assertEqual(db._calculate_config_max_notional(100, 2, 3),
                         100 + 200 + 400 + 800)  # 1500

    def test_multiplier_one_is_linear(self):
        self.assertEqual(db._calculate_config_max_notional(5, 1, 4), 25)  # 5 steps

    def test_zero_base_returns_zero(self):
        self.assertEqual(db._calculate_config_max_notional(0, 2, 5), 0.0)

    def test_negative_base_returns_zero(self):
        self.assertEqual(db._calculate_config_max_notional(-10, 2, 5), 0.0)

    def test_negative_steps_returns_zero(self):
        self.assertEqual(db._calculate_config_max_notional(100, 2, -1), 0.0)

    def test_accepts_string_numeric(self):
        self.assertEqual(db._calculate_config_max_notional("100", "2", "3"), 1500.0)


class GetBotConfigParams(_DBTestBase):
    def test_row_present_reads_config_max_steps(self):
        self.conn.execute(
            "INSERT INTO bots (id, base_size, martingale_multiplier, config) "
            "VALUES (1, 100, 2, '{\"max_steps\": 5}')")
        self.conn.commit()
        self.assertEqual(db._get_bot_config_params(1), (100.0, 2.0, 5))

    def test_missing_bot_returns_zero_tuple(self):
        self.assertEqual(db._get_bot_config_params(999), (0.0, 0.0, 0))

    def test_config_without_max_steps_defaults_10(self):
        self.conn.execute(
            "INSERT INTO bots (id, base_size, martingale_multiplier, config) "
            "VALUES (2, 50, 3, '{\"other\": 1}')")
        self.conn.commit()
        self.assertEqual(db._get_bot_config_params(2), (50.0, 3.0, 10))

    def test_invalid_config_json_defaults_10(self):
        self.conn.execute(
            "INSERT INTO bots (id, base_size, martingale_multiplier, config) "
            "VALUES (3, 50, 3, 'not-json')")
        self.conn.commit()
        self.assertEqual(db._get_bot_config_params(3), (50.0, 3.0, 10))


class CheckPositionSizeCircuitBreaker(_DBTestBase):
    def _seed(self, base, mult, max_steps, invested):
        self.conn.execute(
            "INSERT INTO bots (id, base_size, martingale_multiplier, config) "
            "VALUES (10, ?, ?, ?)", (base, mult, '{"max_steps": %d}' % max_steps))
        self.conn.execute(
            "INSERT INTO trades (bot_id, total_invested) VALUES (10, ?)", (invested,))
        self.conn.commit()

    def test_above_2x_fires(self):
        self._seed(100, 2, 3, 3000.01)  # config_max=1500, 2x=3000 -> fires
        sf, cur, cmax = db.check_position_size_circuit_breaker(10)
        self.assertTrue(sf); self.assertAlmostEqual(cur, 3000.01); self.assertEqual(cmax, 1500)

    def test_exactly_2x_does_not_fire(self):
        self._seed(100, 2, 3, 3000.0)
        self.assertFalse(db.check_position_size_circuit_breaker(10)[0])

    def test_below_2x_no_fire(self):
        self._seed(100, 2, 3, 100.0)
        self.assertEqual(db.check_position_size_circuit_breaker(10), (False, 100.0, 1500.0))

    def test_no_trades_row_returns_zero_current(self):
        self.conn.execute(
            "INSERT INTO bots (id, base_size, martingale_multiplier, config) "
            "VALUES (11, 100, 2, '{\"max_steps\": 3}')")
        self.conn.commit()
        self.assertEqual(db.check_position_size_circuit_breaker(11), (False, 0.0, 1500.0))

    def test_invalid_params_never_freeze(self):
        self.conn.execute(
            "INSERT INTO bots (id, base_size, martingale_multiplier, config) "
            "VALUES (12, 0, 2, '{}')")
        self.conn.execute(
            "INSERT INTO trades (bot_id, total_invested) VALUES (12, 99999)")
        self.conn.commit()
        self.assertEqual(db.check_position_size_circuit_breaker(12), (False, 99999.0, 0.0))


class FreezeBotForPositionOversize(_DBTestBase):
    def test_freeze_sets_manual_proof_with_note(self):
        self.conn.execute(
            "INSERT INTO bots (id, base_size, martingale_multiplier, config, status) "
            "VALUES (20, 100, 2, '{\"max_steps\": 3}', 'running')")
        self.conn.commit()
        db.freeze_bot_for_position_oversize(20, 4000.0, 1500.0)
        row = self.conn.execute("SELECT status, notes FROM bots WHERE id=20").fetchone()
        self.assertEqual(row[0], "REQUIRE_MANUAL_PROOF")
        self.assertIn("POS-SIZE-CB", row[1])


if __name__ == "__main__":
    unittest.main()
