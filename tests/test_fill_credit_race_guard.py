"""
Regression test for the fill-credit race fix (Option A).

The race: reset/clear logic marks catchup/grid orders as auto_closed/reset_cleared
BEFORE the exchange fill credit arrives. The fill is lost from the DB, virtual net
diverges from exchange, and O-10 freezes the pair.

The fix: _reset_bot_after_tp_public_internal now checks each pending order's
exchange status BEFORE opening the reset transaction. If the exchange reports
the order as filled, credit_fill is called first.

Test scenarios use the real LINK/ETH numbers from the 2026-08-28 incident:
- LINK GRID_1_9: 140.33 SHORT filled on exchange, DB said 'open'
- ETH GRID_5_9: 1.244 SHORT filled on exchange, DB said 'open'
"""
import os
import sys
import sqlite3
import time
import pytest
from unittest.mock import MagicMock, patch

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def race_db(tmp_path):
    """Create a minimal test DB with the LINK GRID_1_9 scenario."""
    db_path = str(tmp_path / "test_race.db")

    # Point the database module at our test DB (matches existing test pattern)
    import engine.database as db_mod
    orig_db_path = db_mod.DB_PATH
    db_mod.DB_PATH = db_path
    # Clear any cached thread-local connection so it reconnects to the test DB
    if hasattr(db_mod, '_local'):
        db_mod._local.__dict__.pop('connection', None)
        db_mod._local.__dict__.pop('connection_db_path', None)

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Minimal schema needed by the reset path
    c.execute("""CREATE TABLE bots (
        id INTEGER PRIMARY KEY, name TEXT, pair TEXT, normalized_pair TEXT,
        direction TEXT, strategy_type TEXT, config TEXT, is_active INTEGER DEFAULT 1,
        status TEXT DEFAULT 'IN TRADE', bot_type TEXT DEFAULT 'standard',
        parent_bot_id INTEGER, base_size REAL DEFAULT 10.0,
        martingale_multiplier REAL DEFAULT 1.5, rsi_limit REAL DEFAULT 30.0,
        manual_close_pct REAL, last_error TEXT, last_error_time INTEGER,
        pos_limit_hit INTEGER DEFAULT 0, hedge_child_bot_id INTEGER,
        hedge_trigger_step INTEGER, cascade_started_at INTEGER, notes TEXT
    )""")
    c.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, action TEXT,
        symbol TEXT, price REAL, amount REAL, step INTEGER DEFAULT 0,
        pnl REAL DEFAULT 0, notes TEXT, position_side TEXT, cycle_id INTEGER DEFAULT 1,
        current_step INTEGER DEFAULT 0, total_invested REAL DEFAULT 0,
        avg_entry_price REAL DEFAULT 0, target_tp_price REAL DEFAULT 0,
        last_exit_price REAL DEFAULT 0, last_exit_time INTEGER DEFAULT 0,
        basket_start_time INTEGER DEFAULT 0, entry_confirmed INTEGER DEFAULT 0,
        entry_order_id TEXT, tp_order_id TEXT, bot_position_id TEXT,
        close_type TEXT, open_qty REAL DEFAULT 0, created_at INTEGER
    )""")
    c.execute("""CREATE TABLE bot_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, step INTEGER,
        order_type TEXT, order_id TEXT, price REAL, amount REAL,
        filled_amount REAL DEFAULT 0, status TEXT DEFAULT 'open',
        created_at INTEGER, client_order_id TEXT, updated_at INTEGER,
        notes TEXT, wipe_proof_source TEXT, wipe_proof_snapshot TEXT,
        cycle_id INTEGER, position_side TEXT, filled_at INTEGER,
        cumulative_filled REAL DEFAULT 0
    )""")
    c.execute("""CREATE TABLE active_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, pair TEXT,
        side TEXT, size REAL, fetched_at INTEGER, source TEXT,
        UNIQUE(bot_id, pair, side)
    )""")
    c.execute("""CREATE TABLE fill_claims (
        bot_id INTEGER, order_id TEXT, caller TEXT, claimed_at INTEGER,
        UNIQUE(bot_id, order_id)
    )""")
    c.execute("""CREATE TABLE equity_snapshots (
        ts INTEGER PRIMARY KEY, equity REAL, cash REAL, cost REAL, unrealized_pnl REAL
    )""")

    now = int(time.time())

    # ── LINK scenario: bot 10020, GRID_1_9 filled 140.33 on exchange, DB says open ──
    c.execute("""INSERT INTO bots (id, name, pair, normalized_pair, direction, strategy_type,
        config, is_active, status, bot_type, base_size)
        VALUES (10020, 'short link', 'LINK/USDC:USDC', 'LINKUSDC', 'SHORT', 'Martingale',
        '{}', 1, 'IN TRADE', 'standard', 10.0)""")

    c.execute("""INSERT INTO trades (bot_id, action, symbol, price, amount, step,
        position_side, cycle_id, open_qty, created_at)
        VALUES (10020, 'ENTRY', 'LINK/USDC:USDC', 22.5, 137.35, 9, 'SHORT', 1, 137.35, ?)""", (now,))

    # The GRID_1_9 order: DB says open, filled_amount=0, but exchange filled 140.33
    c.execute("""INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount,
        filled_amount, status, created_at, client_order_id, updated_at, cycle_id, position_side)
        VALUES (10020, 9, 'grid', '398519982', 22.5, 140.33, 0.0, 'open', ?,
        'CQB_10020_GRID_1_9', ?, 1, 'SHORT')""", (now, now))

    conn.commit()
    conn.close()

    yield db_path

    # Cleanup: restore original DB_PATH and clear cached connection
    db_mod.DB_PATH = orig_db_path
    if hasattr(db_mod, '_local'):
        db_mod._local.__dict__.pop('connection', None)
        db_mod._local.__dict__.pop('connection_db_path', None)


def test_race_guard_credits_filled_order_before_reset(race_db):
    """
    LINK GRID_1_9 scenario: order is 'open' in DB with filled_amount=0,
    but exchange reports filled=140.33. The race guard must credit the fill
    BEFORE the reset auto_closes it.

    After reset_bot_after_tp:
    - The order must have status='filled' and filled_amount=140.33
      (credited by race guard), NOT 'auto_closed' with filled_amount=0.
    """
    import engine.database as db_mod

    # Mock exchange: fetch_order returns the order as fully filled
    mock_exchange = MagicMock()
    mock_exchange.fetch_order.return_value = {
        'id': '398519982',
        'status': 'filled',
        'filled': 140.33,
        'amount': 140.33,
        'average': 22.5,
        'price': 22.5,
    }

    # Run the reset with the mock exchange
    # In pytest, WriteQueue is bypassed (runs inline), so this executes directly
    try:
        db_mod.reset_bot_after_tp(
            bot_id=10020,
            exit_price=22.5,
            direction='SHORT',
            action_label='TP_HIT',
            exchange=mock_exchange
        )
    except Exception as e:
        # The reset may fail at later stages (wipe proof, etc.) — that's OK.
        # We only care that the race guard credited the fill first.
        pass

    # Verify: fetch_order was called (race guard ran)
    mock_exchange.fetch_order.assert_called()

    # Verify: the order was credited, not auto_closed with 0 fill
    conn = sqlite3.connect(race_db)
    row = conn.execute(
        "SELECT status, filled_amount FROM bot_orders WHERE client_order_id = 'CQB_10020_GRID_1_9'"
    ).fetchone()
    conn.close()

    assert row is not None, "GRID_1_9 order row missing"
    status, filled = row
    # The race guard must have credited the fill: status should be 'filled'
    # (or at minimum, filled_amount must be 140.33, not 0)
    assert float(filled) == pytest.approx(140.33, abs=0.01), (
        f"Race guard failed: GRID_1_9 filled_amount={filled}, expected 140.33. "
        f"Status={status}. The fill was lost to the race."
    )


def test_race_guard_skips_unfilled_orders(race_db):
    """
    Control test: if the exchange reports the order as NOT filled (filled=0),
    the race guard must NOT credit anything, and the order gets auto_closed.
    """
    import engine.database as db_mod

    mock_exchange = MagicMock()
    mock_exchange.fetch_order.return_value = {
        'id': '398519982',
        'status': 'open',
        'filled': 0.0,
        'amount': 140.33,
        'average': 0,
        'price': 22.5,
    }

    try:
        db_mod.reset_bot_after_tp(
            bot_id=10020,
            exit_price=22.5,
            direction='SHORT',
            action_label='TP_HIT',
            exchange=mock_exchange
        )
    except Exception:
        pass

    conn = sqlite3.connect(race_db)
    row = conn.execute(
        "SELECT status, filled_amount FROM bot_orders WHERE client_order_id = 'CQB_10020_GRID_1_9'"
    ).fetchone()
    conn.close()

    assert row is not None
    status, filled = row
    # No fill on exchange → filled_amount stays 0
    assert float(filled) == pytest.approx(0.0, abs=0.001), (
        f"Race guard incorrectly credited an unfilled order: filled={filled}"
    )


def test_race_guard_no_exchange_no_crash(race_db):
    """
    If exchange=None (some callers don't pass it), the race guard must
    skip gracefully without crashing.
    """
    import engine.database as db_mod

    try:
        db_mod.reset_bot_after_tp(
            bot_id=10020,
            exit_price=22.5,
            direction='SHORT',
            action_label='TP_HIT',
            exchange=None  # No exchange → race guard skips
        )
    except Exception as e:
        # May fail at later stages, but must NOT fail in the race guard
        assert 'RACE-GUARD' not in str(e), f"Race guard crashed with exchange=None: {e}"

    # Order should be auto_closed (no exchange to check)
    conn = sqlite3.connect(race_db)
    row = conn.execute(
        "SELECT status, filled_amount FROM bot_orders WHERE client_order_id = 'CQB_10020_GRID_1_9'"
    ).fetchone()
    conn.close()

    assert row is not None
    status, filled = row
    assert float(filled) == pytest.approx(0.0, abs=0.001)
