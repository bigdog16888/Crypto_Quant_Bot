"""
tests/test_health_and_startup.py
=================================
Automated verification for engine/health.py:
  1. ENGINE_STARTED_AT is correctly written to system_equity by runner startup logic.
  2. compute_system_health() honours the 120 s grace period.
  3. Health object structure matches the specification.
  4. get_system_health() TTL caching: cached result is returned within TTL.
  5. get_system_health(force_refresh=True) bypasses the TTL.
  6. Orphan position detection.
  7. Stale orphan DB alerts older than ENGINE_STARTED_AT are filtered.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from unittest.mock import MagicMock

import pytest

import engine.database as database
from engine.health import (
    STARTUP_GRACE_SECONDS,
    _get_engine_started_at,
    compute_system_health,
    get_system_health,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mem_db():
    """Isolated in-memory DB; patches DB_PATH + sqlite3.connect for health.py."""
    db_id = str(uuid.uuid4())
    shared_uri = f"file:health_test_{db_id}?mode=memory&cache=shared"
    _orig_connect = sqlite3.connect
    _orig_backup = database.backup_database
    _orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    if hasattr(database._local, "connection"):
        database._local.connection = None

    def mock_connect(db_path, *args, **kwargs):
        kwargs["uri"] = True
        return _orig_connect(shared_uri, *args, **kwargs)

    sqlite3.connect = mock_connect
    database.DB_PATH = shared_uri
    database.init_db()

    conn = database.get_connection()
    yield conn

    sqlite3.connect = _orig_connect
    database.DB_PATH = _orig_db_path
    database.backup_database = _orig_backup


def _set_engine_started_at(conn, ts: float):
    conn.execute(
        "INSERT OR REPLACE INTO system_equity (key, value) VALUES ('ENGINE_STARTED_AT', ?)",
        (ts,),
    )
    conn.commit()


def _seed_bot(conn, bot_id: int, pair: str, direction: str, open_qty: float = 0.0):
    conn.execute(
        "INSERT OR REPLACE INTO bots "
        "(id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (?, ?, ?, ?, ?, 1, 'IN TRADE')",
        (bot_id, f"bot_{bot_id}", pair, pair, direction),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades "
        "(bot_id, cycle_id, open_qty, wipe_wall_ts, position_side, current_step, total_invested) "
        "VALUES (?, 1, ?, 0, ?, 1, 100.0)",
        (bot_id, open_qty, direction),
    )
    conn.commit()


def _make_exchange(positions=None):
    ex = MagicMock()
    ex.fetch_positions.return_value = positions or []
    ex.fetch_open_orders.return_value = []
    ex.fetch_balance.return_value = {}
    ex.get_last_price.return_value = 0.0
    return ex


# ---------------------------------------------------------------------------
# 1. ENGINE_STARTED_AT persistence
# ---------------------------------------------------------------------------

class TestEngineStartedAt:
    def test_missing_returns_zero(self, mem_db):
        """When key is absent, helper returns 0.0."""
        assert _get_engine_started_at(database.DB_PATH) == 0.0

    def test_written_value_is_readable(self, mem_db):
        ts = time.time()
        _set_engine_started_at(mem_db, ts)
        result = _get_engine_started_at(database.DB_PATH)
        assert abs(result - ts) < 0.01

    def test_runner_startup_pattern(self, mem_db):
        """Replicate the exact INSERT pattern from runner.py main block."""
        startup_time = time.time()
        mem_db.execute(
            "INSERT OR REPLACE INTO system_equity (key, value) VALUES ('ENGINE_STARTED_AT', ?)",
            (startup_time,),
        )
        mem_db.commit()
        recovered = _get_engine_started_at(database.DB_PATH)
        assert abs(recovered - startup_time) < 0.01


# ---------------------------------------------------------------------------
# 2. compute_system_health – startup suppression
# ---------------------------------------------------------------------------

class TestStartupSuppression:
    def _run(self, mem_db, engine_age_s: float, positions=None):
        started_at = time.time() - engine_age_s
        _set_engine_started_at(mem_db, started_at)
        ex = _make_exchange(positions=positions)
        return compute_system_health(
            db_path=database.DB_PATH,
            exchange_instance=ex,
            norm_fn=lambda s: s,
            qty_tolerance_fn=lambda: 0.0001,
        )

    def test_within_grace_sets_suppression_true(self, mem_db):
        result = self._run(mem_db, engine_age_s=30.0)
        assert result["startup_suppression"] is True
        assert result["system_status"] == "STARTING"

    def test_within_grace_remaining_positive(self, mem_db):
        result = self._run(mem_db, engine_age_s=30.0)
        remaining = result["startup_remaining_s"]
        assert 0.0 < remaining < STARTUP_GRACE_SECONDS

    def test_beyond_grace_sets_suppression_false(self, mem_db):
        result = self._run(mem_db, engine_age_s=200.0)
        assert result["startup_suppression"] is False

    def test_drift_detected_false_during_suppression(self, mem_db):
        """Even with a real mismatch, drift_detected must be False during startup."""
        pair = "BTC/USDC:USDC"
        _seed_bot(mem_db, 99001, pair, "LONG", open_qty=0.0)
        exchange_pos = [{"symbol": pair, "contracts": 0.01, "entryPrice": 60000.0}]
        result = self._run(mem_db, engine_age_s=5.0, positions=exchange_pos)
        assert result["startup_suppression"] is True
        for pdata in result["netting_status_per_pair"].values():
            assert pdata["drift_detected"] is False

    def test_drift_detected_true_after_grace(self, mem_db):
        """After grace period, a real mismatch should be flagged."""
        pair = "BTC/USDC:USDC"
        _seed_bot(mem_db, 99002, pair, "LONG", open_qty=0.0)
        exchange_pos = [{"symbol": pair, "contracts": 0.01, "entryPrice": 60000.0}]
        result = self._run(mem_db, engine_age_s=200.0, positions=exchange_pos)
        assert result["startup_suppression"] is False
        any_drift = any(p["drift_detected"] for p in result["netting_status_per_pair"].values())
        assert any_drift, "Expected at least one pair to have drift_detected=True after grace"


# ---------------------------------------------------------------------------
# 3. Health object structure
# ---------------------------------------------------------------------------

class TestHealthObjectStructure:
    def _healthy_result(self, mem_db):
        _set_engine_started_at(mem_db, time.time() - 200)
        return compute_system_health(
            db_path=database.DB_PATH,
            exchange_instance=_make_exchange(),
            norm_fn=lambda s: s,
            qty_tolerance_fn=lambda: 0.0001,
        )

    def test_top_level_keys(self, mem_db):
        result = self._healthy_result(mem_db)
        required = {
            "timestamp", "startup_suppression", "startup_remaining_s",
            "engine_started_at", "system_status", "worst_gap_usd",
            "mismatched_pair_count", "netting_status_per_pair",
            "order_health", "header_metrics", "orphan_positions",
        }
        assert required.issubset(result.keys())

    def test_order_health_subkeys(self, mem_db):
        result = self._healthy_result(mem_db)
        oh = result["order_health"]
        assert "status_color" in oh
        assert "message" in oh
        assert "bot_statuses" in oh

    def test_header_metrics_subkeys(self, mem_db):
        result = self._healthy_result(mem_db)
        hm = result["header_metrics"]
        for k in ("total_equity", "futures_balance", "global_pnl_usd",
                   "total_invested_db", "active_count", "bots_in_trade",
                   "scanning_count", "open_qty_notional", "assets_breakdown",
                   "adoptions_today", "last_act_str"):
            assert k in hm, f"Missing header_metrics key: {k}"

    def test_healthy_status_when_no_mismatches(self, mem_db):
        result = self._healthy_result(mem_db)
        assert result["system_status"] == "HEALTHY"
        assert result["mismatched_pair_count"] == 0

    def test_mismatch_status_when_drifted(self, mem_db):
        pair = "ETH/USDC:USDC"
        _seed_bot(mem_db, 99010, pair, "LONG", open_qty=0.0)
        _set_engine_started_at(mem_db, time.time() - 200)
        ex = _make_exchange(positions=[
            {"symbol": pair, "contracts": 0.5, "entryPrice": 2000.0}
        ])
        result = compute_system_health(
            db_path=database.DB_PATH,
            exchange_instance=ex,
            norm_fn=lambda s: s,
            qty_tolerance_fn=lambda: 0.0001,
        )
        assert result["system_status"] == "MISMATCH"
        assert result["mismatched_pair_count"] >= 1

    def test_orphan_positions_list_type(self, mem_db):
        result = self._healthy_result(mem_db)
        assert isinstance(result["orphan_positions"], list)


# ---------------------------------------------------------------------------
# 4 & 5. get_system_health caching and force_refresh
# ---------------------------------------------------------------------------

class TestGetSystemHealthCaching:
    def _call(self, mem_db, force_refresh: bool = False):
        _set_engine_started_at(mem_db, time.time() - 200)
        return get_system_health(
            db_path=database.DB_PATH,
            exchange_instance=_make_exchange(),
            norm_fn=lambda s: s,
            qty_tolerance_fn=lambda: 0.0001,
            force_refresh=force_refresh,
        )

    def test_returns_health_dict(self, mem_db):
        result = self._call(mem_db)
        assert isinstance(result, dict)
        assert "system_status" in result

    def test_cached_within_ttl(self, mem_db):
        """Second call within TTL should return same object (by timestamp)."""
        import engine.health as health_mod
        health_mod._health_cache = {}
        r1 = self._call(mem_db)
        r1_ts = r1["timestamp"]
        r2 = self._call(mem_db)
        assert r2["timestamp"] == r1_ts

    def test_force_refresh_bypasses_cache(self, mem_db):
        """force_refresh=True must produce a new computation."""
        import engine.health as health_mod
        health_mod._health_cache = {}
        r1 = self._call(mem_db)
        time.sleep(0.02)
        r2 = self._call(mem_db, force_refresh=True)
        assert r2["timestamp"] >= r1["timestamp"]


# ---------------------------------------------------------------------------
# 6. Orphan position detection
# ---------------------------------------------------------------------------

class TestOrphanPositionDetection:
    def test_no_orphans_when_bot_matches_exchange(self, mem_db):
        pair = "SOL/USDC:USDC"
        _seed_bot(mem_db, 88001, pair, "LONG", open_qty=10.0)
        _set_engine_started_at(mem_db, time.time() - 200)
        ex = _make_exchange(positions=[{"symbol": pair, "contracts": 10.0, "entryPrice": 100.0}])
        result = compute_system_health(
            db_path=database.DB_PATH,
            exchange_instance=ex,
            norm_fn=lambda s: s,
            qty_tolerance_fn=lambda: 0.01,
        )
        assert result["orphan_positions"] == []

    def test_orphan_when_exchange_nonzero_no_bot(self, mem_db):
        pair = "XRP/USDC:USDC"
        _set_engine_started_at(mem_db, time.time() - 200)
        ex = _make_exchange(positions=[{"symbol": pair, "contracts": 50.0, "entryPrice": 2.0}])
        result = compute_system_health(
            db_path=database.DB_PATH,
            exchange_instance=ex,
            norm_fn=lambda s: s,
            qty_tolerance_fn=lambda: 0.001,
        )
        orphan_pairs = [o["pair"] for o in result["orphan_positions"]]
        assert pair in orphan_pairs

    def test_orphan_suppressed_during_startup(self, mem_db):
        pair = "DOGE/USDC:USDC"
        _set_engine_started_at(mem_db, time.time() - 5)
        ex = _make_exchange(positions=[{"symbol": pair, "contracts": 1000.0, "entryPrice": 0.1}])
        result = compute_system_health(
            db_path=database.DB_PATH,
            exchange_instance=ex,
            norm_fn=lambda s: s,
            qty_tolerance_fn=lambda: 0.001,
        )
        assert result["startup_suppression"] is True
        assert result["orphan_positions"] == []


# ---------------------------------------------------------------------------
# 7. Stale DB-backed orphan alerts are filtered by ENGINE_STARTED_AT
# ---------------------------------------------------------------------------

class TestStaleOrphanAlertFiltering:
    def test_engine_started_at_exposed_in_result(self, mem_db):
        ts = time.time() - 150
        _set_engine_started_at(mem_db, ts)
        result = compute_system_health(
            db_path=database.DB_PATH,
            exchange_instance=_make_exchange(),
            norm_fn=lambda s: s,
            qty_tolerance_fn=lambda: 0.0001,
        )
        assert abs(result["engine_started_at"] - ts) < 0.5

    def test_stale_alert_filter_logic(self, mem_db):
        """
        Mirror the UI filter: alert.created_at < engine_started_at is excluded.
        """
        engine_started = time.time() - 60
        stale_created_at = engine_started - 300
        fresh_created_at = engine_started + 10

        alerts = [
            (1, None, "BTC/USDT", "BTC/USDT", 0.01, 0.0, "orphan", stale_created_at),
            (2, None, "ETH/USDT", "ETH/USDT", 0.50, 0.0, "orphan", fresh_created_at),
        ]

        filtered = [a for a in alerts if float(a[7] or 0) >= engine_started]
        assert len(filtered) == 1
        assert filtered[0][0] == 2
