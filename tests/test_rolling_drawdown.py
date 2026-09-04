"""tests/test_rolling_drawdown.py — O-3 rolling-window drawdown breaker.

Exercises the 3 O-3 helpers in engine/database.py:
  - record_equity_snapshot
  - get_equity_snapshot_series
  - compute_rolling_drawdown

Pure-function core + mock.patch for get_connection. Minimal in-memory schema.
"""
import unittest
import sqlite3
import time
from unittest import mock

from engine import database as db


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE equity_snapshots (ts REAL PRIMARY KEY, equity REAL)"
    )
    return conn


class _DBTestBase(unittest.TestCase):
    def setUp(self):
        # Patch DB_PATH to a temp file, then use the real get_connection
        import tempfile
        self._tmp_file = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmp_file.close()
        self._db_path = self._tmp_file.name
        
        # Patch the module's DB_PATH constant
        self._path_patch = mock.patch.object(db, "DB_PATH", self._db_path)
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        self.addCleanup(lambda: __import__('os').remove(self._db_path))
        
        # Initialize the table via real get_connection
        conn = db.get_connection()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS equity_snapshots (ts REAL PRIMARY KEY, equity REAL)"
        )
        conn.close()

    def _clear(self):
        conn = db.get_connection()
        conn.execute("DELETE FROM equity_snapshots")
        conn.commit()
        conn.close()


class RecordEquitySnapshot(_DBTestBase):
    def test_inserts_and_prunes(self):
        now = time.time()
        db.record_equity_snapshot(10000.0, ts=now - 10 * 3600)  # 10h ago
        db.record_equity_snapshot(9900.0, ts=now - 5 * 3600)    # 5h ago
        db.record_equity_snapshot(9800.0, ts=now)                # now

        conn = db.get_connection()
        rows = conn.execute("SELECT ts, equity FROM equity_snapshots ORDER BY ts").fetchall()
        conn.close()
        assert len(rows) == 3
        assert rows[0][1] == 10000.0
        assert rows[1][1] == 9900.0
        assert rows[2][1] == 9800.0

    def test_prunes_old_points(self):
        now = time.time()
        # Insert one old point (60h ago) and two recent
        db.record_equity_snapshot(10000.0, ts=now - 60 * 3600)  # older than 48h default
        db.record_equity_snapshot(9900.0, ts=now - 10 * 3600)
        db.record_equity_snapshot(9800.0, ts=now)

        # Default max_age_hours=48 should prune the 60h-old point
        conn = db.get_connection()
        rows = conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()
        conn.close()
        assert rows[0] == 2, "Old point should be pruned"


class GetEquitySnapshotSeries(_DBTestBase):
    def test_returns_ordered_by_ts(self):
        now = time.time()
        db.record_equity_snapshot(10000.0, ts=now - 10 * 3600)
        db.record_equity_snapshot(9900.0, ts=now - 5 * 3600)
        db.record_equity_snapshot(9800.0, ts=now)

        series = db.get_equity_snapshot_series()
        assert len(series) == 3
        assert series[0][1] == 10000.0
        assert series[1][1] == 9900.0
        assert series[2][1] == 9800.0
        # Ensure timestamps ascending
        assert series[0][0] < series[1][0] < series[2][0]

    def test_ts_from_cutoff(self):
        now = time.time()
        db.record_equity_snapshot(10000.0, ts=now - 20 * 3600)
        db.record_equity_snapshot(9900.0, ts=now - 10 * 3600)
        db.record_equity_snapshot(9800.0, ts=now)

        series = db.get_equity_snapshot_series(ts_from=now - 15 * 3600)
        assert len(series) == 2
        assert series[0][1] == 9900.0
        assert series[1][1] == 9800.0


class ComputeRollingDrawdown(_DBTestBase):
    def test_basic_drawdown(self):
        # 2026-09-04 contract: the live caller records the current read into
        # the series BEFORE computing, so the current side is the median of
        # the last-3 snapshots (mirrored here: 8500 is both the scalar and
        # the tail point). Peak = median of top-3 [10000, 9900, 9000] = 9900;
        # current = median of last-3 [9900, 9000, 8500] = 9000.
        now = time.time()
        series = [
            (now - 300, 10000.0),
            (now - 200, 9900.0),
            (now - 100, 9000.0),
            (now, 8500.0),
        ]
        drawdown, peak = db.compute_rolling_drawdown(series, 8500.0)
        assert peak == 9900.0
        assert abs(drawdown - 9.0909) < 0.01  # (9900-9000)/9900

    def test_zero_base_returns_zero(self):
        # New behavior: zero reads are excluded from peak candidates.
        # series: [0 (bad read), 5000], current=1000
        # candidates = [5000] -> peak = 5000, drawdown = (5000-1000)/5000 = 80%
        series = [(time.time(), 0.0), (time.time() + 3600, 5000.0)]
        drawdown, peak = db.compute_rolling_drawdown(series, 1000.0)
        assert peak == 5000.0
        assert abs(drawdown - 80.0) < 0.01

    def test_empty_series_returns_zero(self):
        drawdown, base = db.compute_rolling_drawdown([], 8500.0)
        assert drawdown == 0.0
        assert base is None

    def test_single_point_returns_zero(self):
        series = [(time.time(), 10000.0)]
        drawdown, base = db.compute_rolling_drawdown(series, 8500.0)
        assert drawdown == 0.0
        assert base is None

    def test_no_drawdown_when_current_higher(self):
        # series: [10000, 10500], current=11000
        # top 3 = [10500, 10000] → median = 10250
        # current > peak → drawdown = 0
        series = [(time.time(), 10000.0), (time.time() + 3600, 10500.0)]
        drawdown, peak = db.compute_rolling_drawdown(series, 11000.0)
        assert drawdown == 0.0
        assert peak == 10250.0

    def test_gap_detection_splits_window(self):
        # Restart gap: 11h between t0 and t1. Post-gap data must be used
        # exclusively; pre-gap 10000 must NOT set the baseline.
        # Live-caller pattern: current (8000) recorded as the tail point.
        now = time.time()
        series = [
            (now - 12*3600, 10000.0),  # pre-gap (would raise peak if leaked)
            (now - 1*3600, 9000.0),    # post-gap
            (now - 0.5*3600, 9100.0), # post-gap
            (now - 360, 8200.0),      # post-gap
            (now, 8000.0),            # post-gap (current)
        ]
        # post-gap peak = median top-3 [9100, 9000, 8200] = 9000
        # current = median last-3 [9100, 8200, 8000] = 8200
        # If pre-gap 10000 leaked in: peak would be median [10000, 9100, 9000]
        #   = 9100 -> dd 9.89% != 8.89% -> assertion catches the leak.
        drawdown, peak = db.compute_rolling_drawdown(series, 8000.0, gap_threshold_h=1.5)
        assert peak == 9000.0
        assert abs(drawdown - 8.8889) < 0.05  # (9000-8200)/9000

    def test_single_post_gap_point_no_drawdown(self):
        # A restart leaves ONE new snapshot: baseline cannot be established.
        # The breaker must NOT measure against pre-restart data (real incident:
        # 33,931 pre-restart spike + 14,845 post-restart current = false 42% fire).
        now = time.time()
        series = [
            (now - 25*3600, 33931.0),  # pre-restart spike (bad read)
            (now - 0.1*3600, 14832.0), # first snapshot of new run
        ]
        drawdown, peak = db.compute_rolling_drawdown(series, 14845.0, gap_threshold_h=1.5)
        assert drawdown == 0.0
        assert peak is None

    def test_outlier_ignored_median_of_top3(self):
        # Single-tick spike 33931 among normal ~21000 values, then a REAL
        # decline to ~14800 confirmed by two reads (live-caller pattern:
        # the decline must appear in the last-3 tail to be believed).
        now = time.time()
        series = [
            (now - 5*3600, 21000.0),
            (now - 4*3600, 21100.0),
            (now - 3*3600, 33931.0),  # outlier UP-spike
            (now - 2*3600, 21050.0),
            (now - 1*3600, 20950.0),
            (now - 600, 14845.0),     # decline, read 1
            (now, 14700.0),           # decline, read 2 (current)
        ]
        current = 14700.0
        # peak = median top-3 [33931, 21100, 21050] = 21100 (spike ignored)
        # current = median last-3 [20950, 14845, 14700] = 14845
        drawdown, peak = db.compute_rolling_drawdown(series, current, gap_threshold_h=100)
        assert peak == 21100.0
        assert abs(drawdown - 29.64) < 0.01  # (21100-14845)/21100


class O3Hardening20260904(_DBTestBase):
    """O-3 hardening (docs/O3_FALSE_FIRE_ROOT_CAUSE_20260904.md), 2026-09-04.

    Four parts approved by the operator:
      A1: O-3 equity excludes DB Cost (double-count fix) — covered by
          tests/test_balance_fetch_fix.py equity assertions.
      B:  median-of-last-3 CURRENT side (transient read immune).
      C:  2-consecutive-check confirmation before triggering.
      D:  rebased window — this run's snapshots only (ENGINE_STARTED_AT).
    """

    # ---- B: transient current read (live 09:08:29 replay) ----

    def test_b_transient_current_read_no_fire(self):
        # EXACT live incident series: post-gap [15094.10, 15088.81, 9485.24].
        # The 9485.24 was a mid-transition read recorded as the latest snapshot.
        # Old contract: trusted scalar current -> (15088.81-9485.24)/15088.81
        #   = 37.14% -> FALSE FIRE (live 2026-09-04 09:08:29).
        # New contract: current = median of last-3 = 15088.81 -> 0.00%.
        now = time.time()
        series = [
            (now - 20*3600, 14832.11),   # pre-gap (Sep 3 13:57)
            (now - 18*3600, 14845.53),   # pre-gap (Sep 3 14:41)
            (now - 60, 15094.10),        # post-gap, this run
            (now - 30, 15088.81),        # post-gap, this run
            (now, 9485.24),              # TRANSIENT (the live false-fire read)
        ]
        drawdown, peak = db.compute_rolling_drawdown(series, 9485.24, gap_threshold_h=1.5)
        assert peak == 15088.81, f"peak should be median of top-3, got {peak}"
        assert drawdown == 0.0, f"transient current must NOT fire, got {drawdown}"

    def test_b_scalar_fallback_only_when_tail_short(self):
        # Cold-start: only ONE positive snapshot in the tail exists alongside
        # one zero read. The scalar current is used as fallback and a real
        # drawdown is still measurable (no blind spot introduced by B).
        now = time.time()
        series = [
            (now - 60, 10000.0),
            (now, 0.0),  # bad read recorded as zero
        ]
        drawdown, peak = db.compute_rolling_drawdown(series, 8500.0, gap_threshold_h=1.5)
        assert peak == 10000.0
        # tail positives = [10000] -> len<2 -> scalar 8500 used
        assert abs(drawdown - 15.0) < 0.01  # (10000-8500)/10000

    # ---- B: genuine decline still fires ----

    def test_b_genuine_decline_still_fires(self):
        # A REAL -25% decline shows in MULTIPLE snapshots, not one. The
        # median-of-last-3 current tracks it, and O-3 must still cross 20%.
        now = time.time()
        series = [
            (now - 300, 10000.0),
            (now - 200, 9900.0),
            (now - 100, 7600.0),   # decline confirmed by next read
            (now, 7500.0),         # -25% vs peak median 9900
        ]
        drawdown, peak = db.compute_rolling_drawdown(series, 7500.0, gap_threshold_h=1.5)
        # peak = median of top-3 [10000, 9900, 7600] = 9900
        assert peak == 9900.0
        # current = median of last-3 [9900, 7600, 7500] = 7600
        assert abs(drawdown - 23.23) < 0.05  # (9900-7600)/9900 = 23.23% >= 20

    # ---- C: consecutive-check confirmation ----

    def test_c_first_breach_pends_second_triggers(self):
        # O-3 state machine: a >=20% breach on ONE check must NOT trigger;
        # the SAME condition on the NEXT check must fire. Replays the live
        # transient against the hardened pipeline end-to-end.
        import tempfile, os
        from unittest import mock
        from engine.runner import BotRunner

        tmp = tempfile.NamedTemporaryFile(suffix=".ts", delete=False)
        tmp.close()
        self.addCleanup(os.remove, tmp.name)

        with mock.patch.object(db, "DB_PATH", self._db_path):
            db.get_connection().execute("CREATE TABLE IF NOT EXISTS system_equity (key TEXT PRIMARY KEY, value TEXT)")
            conn = db.get_connection()
            conn.execute("INSERT OR REPLACE INTO system_equity (key, value) VALUES ('ENGINE_STARTED_AT', ?)", (str(1788000000),))
            conn.commit(); conn.close()

        runner = BotRunner.__new__(BotRunner)
        runner.circuit_breaker_triggered = False
        runner._o3_consecutive_hits = 0

        now = time.time()
        # First check: transient 9485 recorded on top of healthy 1509x tail.
        # Median-of-3 current -> drawdown 0 -> hits reset/stay 0.
        with mock.patch.object(db, "DB_PATH", self._db_path), \
             mock.patch.object(db, "get_engine_started_at", return_value=0.0), \
             mock.patch.object(db, "record_equity_snapshot"), \
             mock.patch.object(db, "get_equity_snapshot_series", return_value=[
                 (now - 60, 15094.10), (now - 30, 15088.81), (now, 9485.24),
             ]):
            runner._check_rolling_drawdown(9485.24)
        assert runner.circuit_breaker_triggered is False
        assert runner._o3_consecutive_hits == 0

        # Genuine decline: 4-point post-gap series so peak-median and
        # current-median draw from different triples (a 3-point series
        # structurally yields peak == current == median of the same 3).
        with mock.patch.object(db, "DB_PATH", self._db_path), \
             mock.patch.object(db, "get_engine_started_at", return_value=0.0), \
             mock.patch.object(db, "record_equity_snapshot"), \
             mock.patch.object(db, "get_equity_snapshot_series", return_value=[
                 (now - 90, 15094.10), (now - 60, 10000.0),
                 (now - 30, 7600.0), (now, 7500.0),
             ]), \
             mock.patch("builtins.open", mock.mock_open()) as m_open, \
             mock.patch.object(runner, "handle_emergency_liquidation") as m_liq:
            # check 1: pending
            runner._check_rolling_drawdown(7500.0)
            assert runner.circuit_breaker_triggered is False
            assert runner._o3_consecutive_hits == 1
            m_liq.assert_not_called()
            # check 2: confirmed -> trigger
            runner._check_rolling_drawdown(7500.0)
            assert runner._o3_consecutive_hits == 2
            assert runner.circuit_breaker_triggered is True
            m_liq.assert_called_once()
            m_open.assert_called()

    # ---- D: restart rebase ----

    def test_d_rebase_excludes_previous_run_snapshots(self):
        # Restart-without-long-gap scenario: engine bounces in <1.5h, so gap
        # detection alone canNOT isolate. D filters to ts >= ENGINE_STARTED_AT.
        # Previous run held equity 15094/15088 (pre-flatten double-count
        # reads); new run reads ~9048. WITHOUT D the new run fires ~40%
        # against the old baseline. WITH D: new-run-only series = healthy.
        now = time.time()
        started_at = now - 120  # restarted 2 minutes ago
        full_series = [
            (now - 4000, 15094.10),  # previous run (pre-flatten Cost read)
            (now - 3970, 15088.81),  # previous run
            (now - 100, 9048.11),    # this run (true wallet+uPnL equity)
            (now - 50, 9042.55),     # this run
        ]
        # Simulate the D filter as _check_rolling_drawdown applies it:
        rebased = [(ts, eq) for ts, eq in full_series if ts >= started_at]
        assert len(rebased) == 2
        drawdown, peak = db.compute_rolling_drawdown(rebased, 9042.55, gap_threshold_h=1.5)
        # post-gap(=all rebased) candidates top-3 = [9048.11, 9042.55] median = 9045.33
        assert abs(peak - 9045.33) < 0.01
        assert drawdown == 0.0, f"rebased window must not fire, got {drawdown}%"

    def test_d_rebase_via_runner_uses_engine_started_at(self):
        # End-to-end: _check_rolling_drawdown queries get_engine_started_at
        # and filters the series BEFORE computing. Old-run high snapshots
        # (15094) must not be in the series handed to compute_rolling_drawdown.
        from unittest import mock
        from engine.runner import BotRunner

        runner = BotRunner.__new__(BotRunner)
        runner.circuit_breaker_triggered = False
        runner._o3_consecutive_hits = 0

        now = time.time()
        old_run = [(now - 4000, 15094.10), (now - 3970, 15088.81)]
        this_run = [(now - 100, 9048.11), (now - 50, 9042.55)]
        full_series = old_run + this_run

        with mock.patch.object(db, "record_equity_snapshot") as m_rec, \
             mock.patch.object(db, "get_equity_snapshot_series", return_value=full_series) as m_ser, \
             mock.patch.object(db, "get_engine_started_at", return_value=now - 120), \
             mock.patch.object(db, "compute_rolling_drawdown", return_value=(0.0, None)) as m_comp:
            runner._check_rolling_drawdown(9042.55)

        m_rec.assert_called_once()
        series_arg = m_comp.call_args[0][0]
        assert all(ts >= now - 120 for ts, _ in series_arg), \
            f"D rebase must exclude old-run snapshots, got {series_arg}"
        assert len(series_arg) == 2


if __name__ == "__main__":
    unittest.main()