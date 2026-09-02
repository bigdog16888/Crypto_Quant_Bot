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
        # base = 10000 at t0, current = 8500 -> drawdown = 15%
        series = [(time.time(), 10000.0), (time.time() + 3600, 9500.0)]
        drawdown, base = db.compute_rolling_drawdown(series, 8500.0)
        assert base == 10000.0
        assert abs(drawdown - 15.0) < 0.001

    def test_zero_base_returns_zero(self):
        series = [(time.time(), 0.0), (time.time() + 3600, 5000.0)]
        drawdown, base = db.compute_rolling_drawdown(series, 1000.0)
        assert drawdown == 0.0
        assert base is None

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
        series = [(time.time(), 10000.0), (time.time() + 3600, 10500.0)]
        drawdown, base = db.compute_rolling_drawdown(series, 11000.0)
        assert drawdown == 0.0
        assert base == 10000.0


if __name__ == "__main__":
    unittest.main()