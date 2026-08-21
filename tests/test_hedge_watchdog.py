"""
O-10: Unit tests for the hedge-engagement watchdog (verify_hedge_engagement).
Mirrors the isolated-pure-function test style used for O-9's plausibility gate.
Uses an in-memory SQLite DB with a minimal bots/trades schema matching the
columns the watchdog reads.
"""
import unittest
import sqlite3
from unittest import mock

from engine.hedge_watchdog import (
    verify_hedge_engagement,
    _child_offset_ok,
    _count_engine_hedge_failures,
    FREEZE_MARKER,
)
from engine import hedge_watchdog  # for constant defaults


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            direction TEXT,
            status TEXT,
            hedge_child_bot_id INTEGER,
            last_error TEXT,
            last_error_time REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY,
            open_qty REAL
        )"""
    )
    # bot_orders table for hedge_watchdog's _child_hedge_qty_from_orders
    conn.execute(
        """CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER,
            step INTEGER,
            order_type TEXT,
            status TEXT DEFAULT 'open',
            amount REAL,
            filled_amount REAL DEFAULT 0,
            created_at INTEGER,
            filled_at INTEGER DEFAULT 0,
            cycle_id INTEGER,
            position_side TEXT DEFAULT 'BOTH',
            FOREIGN KEY (bot_id) REFERENCES bots (id)
        )"""
    )
    return conn


def seed_bot(conn, bid, direction="LONG", status="IN TRADE",
             hedge_child=None, last_error=None, last_error_time=None):
    conn.execute(
        "INSERT INTO bots (id, direction, status, hedge_child_bot_id, last_error, last_error_time) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (bid, direction, status, hedge_child, last_error, last_error_time),
    )
    conn.commit()


def seed_trade(conn, bid, open_qty):
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, open_qty) VALUES (?, ?)",
        (bid, open_qty),
    )
    conn.commit()


def seed_bot_order(conn, bot_id, step, order_type, status, amount, filled_amount=0, created_at=1000, filled_at=0, cycle_id=1, position_side='LONG'):
    """Insert a bot_orders row for testing."""
    conn.execute(
        """INSERT INTO bot_orders (bot_id, step, order_type, status, amount, filled_amount, created_at, filled_at, cycle_id, position_side)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (bot_id, step, order_type, status, amount, filled_amount, created_at, filled_at, cycle_id, position_side)
    )
    conn.commit()


class TestHedgeWatchdog(unittest.TestCase):

    def test_engaged_when_child_offsetting(self):
        """Parent LONG -> child SHORT with open_qty => engaged, no freeze."""
        conn = make_conn()
        seed_bot(conn, 200, direction="LONG", hedge_child=100, status="IN TRADE")
        seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
        # Child has filled entry order for step 1
        seed_bot_order(conn, 100, 1, 'entry', 'filled', 0.5, 0.5, created_at=1000, filled_at=1000, cycle_id=1)
        res = verify_hedge_engagement(200, "LONG", conn, config={"child_step": 1, "parent_cycle_id": 1})
        self.assertTrue(res["engaged"])
        self.assertFalse(res["freeze_parent"])
        self.assertFalse(res["engine_halt"])
        self.assertEqual(res["child_bot_id"], 100)

    def test_no_child_configured_freezes(self):
        """Parent reaching trigger with NO hedge_child_bot_id => freeze_parent."""
        conn = make_conn()
        seed_bot(conn, 200, direction="LONG", hedge_child=None, status="IN TRADE")
        res = verify_hedge_engagement(200, "LONG", conn)
        self.assertFalse(res["engaged"])
        self.assertTrue(res["freeze_parent"])
        self.assertEqual(res["reason"], "no_hedge_child_bot_id")

    def test_child_not_offsetting_freezes(self):
        """Child holds SAME-direction position (not offsetting) => freeze."""
        conn = make_conn()
        seed_bot(conn, 200, direction="LONG", hedge_child=100, status="IN TRADE")
        seed_bot(conn, 100, direction="LONG", status="HEDGE_STANDBY", hedge_child=None)  # wrong: same dirn
        # Child has filled entry order but wrong direction
        seed_bot_order(conn, 100, 1, 'entry', 'filled', 0.5, 0.5, created_at=1000, filled_at=1000, cycle_id=1)
        res = verify_hedge_engagement(200, "LONG", conn, config={"child_step": 1, "parent_cycle_id": 1})
        self.assertFalse(res["engaged"])
        self.assertTrue(res["freeze_parent"])
        self.assertEqual(res["reason"], "wrong_direction")

    def test_child_zero_qty_freezes(self):
        """Child has no open position yet => not offsetting => freeze."""
        conn = make_conn()
        seed_bot(conn, 200, direction="LONG", hedge_child=100, status="IN TRADE")
        seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
        # No bot_orders entry = no position attempt
        res = verify_hedge_engagement(200, "LONG", conn, config={"child_step": 1, "parent_cycle_id": 1})
        self.assertFalse(res["engaged"])
        self.assertTrue(res["freeze_parent"])
        self.assertEqual(res["reason"], "no_entry_order")

    def test_escalation_over_threshold(self):
        """>=2 parents frozen in window => engine_halt. Fresh parent + 1 existing."""
        conn = make_conn()
        # existing frozen parent
        seed_bot(conn, 299, direction="LONG", hedge_child=101,
                 status="REQUIRE_MANUAL_PROOF",
                 last_error="HEDGE_ENGAGE_FAILURE:no_hedge_child_bot_id",
                 last_error_time=100.0)
        # current parent also failing config
        seed_bot(conn, 200, direction="LONG", hedge_child=None, status="IN TRADE")
        # now/time far beyond window for existing (fail_window passed as small via config)
        now = 100000.0
        res = verify_hedge_engagement(200, "LONG", conn, now=now,
                                      config={"HEDGE_FAIL_WINDOW_SECONDS": 1000000})
        self.assertTrue(res["freeze_parent"])
        self.assertTrue(res["engine_halt"])

    def test_no_escalation_single_failure_in_window(self):
        """Single failing parent within window => freeze only, no engine halt."""
        conn = make_conn()
        seed_bot(conn, 200, direction="LONG", hedge_child=None, status="IN TRADE")
        res = verify_hedge_engagement(200, "LONG", conn, now=100000.0)
        self.assertTrue(res["freeze_parent"])
        self.assertFalse(res["engine_halt"])

    def test_escalation_window_excludes_old_failures(self):
        """A pre-existing frozen parent outside window => no engine halt for fresh one."""
        conn = make_conn()
        # frozen parent 2 windows in the past (outsized)
        seed_bot(conn, 299, direction="LONG", hedge_child=101,
                 status="REQUIRE_MANUAL_PROOF",
                 last_error="HEDGE_ENGAGE_FAILURE:no_hedge_child_bot_id",
                 last_error_time=100.0)
        seed_bot(conn, 200, direction="LONG", hedge_child=None, status="IN TRADE")
        now = 100000.0
        # fail_window tiny => existing failure is OLD, excluded
        res = verify_hedge_engagement(200, "LONG", conn, now=now,
                                      config={"HEDGE_FAIL_WINDOW_SECONDS": 50})
        self.assertTrue(res["freeze_parent"])
        self.assertFalse(res["engine_halt"],
                         "old failure outside window must not count toward escalation")

    def test_child_require_proof_not_engaged(self):
        """Child in REQUIRE_MANUAL_PROOF does not count as engaged."""
        conn = make_conn()
        seed_bot(conn, 200, direction="LONG", hedge_child=100, status="IN TRADE")
        seed_bot(conn, 100, direction="SHORT", status="REQUIRE_MANUAL_PROOF", hedge_child=None)
        seed_bot_order(conn, 100, 1, 'entry', 'filled', 0.5, 0.5, created_at=1000, filled_at=1000, cycle_id=1)
        res = verify_hedge_engagement(200, "LONG", conn, config={"child_step": 1, "parent_cycle_id": 1})
        self.assertFalse(res["engaged"])
        self.assertTrue(res["freeze_parent"])

    def test_counter_respects_marker_and_time(self):
        """_count_engine_hedge_failures only counts marked + in-window freezes."""
        conn = make_conn()
        # marked, in-window
        seed_bot(conn, 1, status="REQUIRE_MANUAL_PROOF",
                 last_error="HEDGE_ENGAGE_FAILURE:no_hedge_child_bot_id",
                 last_error_time=150.0)
        # marked but out-of-window
        seed_bot(conn, 2, status="REQUIRE_MANUAL_PROOF",
                 last_error="HEDGE_ENGAGE_FAILURE:no_hedge_child_bot_id",
                 last_error_time=10.0)
        # different reason, not a hedge freeze
        seed_bot(conn, 3, status="REQUIRE_MANUAL_PROOF",
                 last_error="SNAP_ALLOCATE:divergence", last_error_time=200.0)
        n = _count_engine_hedge_failures(conn, now=200.0, fail_window=100.0)
        self.assertEqual(n, 1)  # only bot 1

    def test_child_offset_ok_opposite_signs(self):
        conn = make_conn()
        seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
        # Add a filled order for the child
        seed_bot_order(conn, 100, 1, 'entry', 'filled', 0.5, 0.5, created_at=1000, filled_at=1000, cycle_id=1)
        self.assertTrue(_child_offset_ok(100, "LONG", conn, child_step=1, parent_cycle_id=1)[0])    # parent LONG -> child SHORT ok
        self.assertFalse(_child_offset_ok(100, "SHORT", conn, child_step=1, parent_cycle_id=1)[0])  # parent SHORT -> child SHORT not ok

    def test_unfilled_entry_within_engage_timeout_no_freeze(self):
        """Entry placed this cycle but unfilled, within engage_timeout => no freeze."""
        conn = make_conn()
        seed_bot(conn, 200, direction="LONG", hedge_child=100, status="IN TRADE")
        seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
        # Child's entry order still open (unfilled), placed ~30s ago
        seed_bot_order(conn, 100, 1, 'entry', 'open', 0.5, 0.0,
                       created_at=970, filled_at=0, cycle_id=1)
        now = 1000.0
        res = verify_hedge_engagement(200, "LONG", conn, now=now,
                                      config={"child_step": 1, "parent_cycle_id": 1,
                                              "HEDGE_ENGAGE_TIMEOUT_SECONDS": 60})
        # Not yet engaged, but within grace window -> no freeze, no halt
        self.assertFalse(res["engaged"])
        self.assertFalse(res["freeze_parent"])
        self.assertFalse(res["engine_halt"])

    def test_unfilled_entry_past_engage_timeout_freezes(self):
        """Entry placed this cycle, still unfilled past engage_timeout => freeze."""
        conn = make_conn()
        seed_bot(conn, 200, direction="LONG", hedge_child=100, status="IN TRADE")
        seed_bot(conn, 100, direction="SHORT", status="HEDGE_STANDBY", hedge_child=None)
        # Child's entry order still open (unfilled), placed 100s ago
        seed_bot_order(conn, 100, 1, 'entry', 'open', 0.5, 0.0,
                       created_at=900, filled_at=0, cycle_id=1)
        now = 1000.0
        res = verify_hedge_engagement(200, "LONG", conn, now=now,
                                      config={"child_step": 1, "parent_cycle_id": 1,
                                              "HEDGE_ENGAGE_TIMEOUT_SECONDS": 60})
        self.assertFalse(res["engaged"])
        self.assertTrue(res["freeze_parent"])
        self.assertFalse(res["engine_halt"])


if __name__ == "__main__":
    unittest.main()