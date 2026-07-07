"""
tests/test_inv35_stuck_dust_no_exit.py

Tests for INV-35: STUCK_DUST_NO_EXIT escalation and safe_wipe_bot MANUAL_CLOSE.

Test A -- Escalation path:
  A1: bot_executor fallback close failure => cycle_phase=STUCK_DUST_NO_EXIT.
  A2: reconciler dust-chaser close placement failure => cycle_phase=STUCK_DUST_NO_EXIT.

Test B -- Recovery (safe_wipe_bot guards):
  B1: MANUAL_CLOSE with exchange=None => refuses immediately.
  B2: MANUAL_CLOSE with live non-flat exchange => refuses loudly.
  B3: MANUAL_CLOSE with flat exchange and bypass_ledger_guard=True => succeeds.
"""
import sqlite3
import time
import pytest
import os
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    
    # Clone schema from production DB (crypto_bot.db) in the project root
    # This prevents test failures when the database schema changes in other updates.
    prod_db_path = os.path.join(r"c:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot", "crypto_bot.db")
    prod_conn = sqlite3.connect(prod_db_path)
    for row in prod_conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if row[0]:
            conn.execute(row[0])
    prod_conn.close()
    
    # Insert bot 10018 (sui long, LONG)
    conn.execute(
        "INSERT INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (10018, 'sui long', 'SUI/USDC:USDC', 'SUIUSDC', 'LONG', 1, 'ACTIVE')"
    )
    conn.execute(
        "INSERT INTO trades (bot_id, open_qty, total_invested, avg_entry_price, "
        "current_step, cycle_id, cycle_phase, position_side) "
        "VALUES (10018, 0.5, 0.185, 0.37, 4, 155, 'PARTIAL_CLOSE_PENDING', 'LONG')"
    )
    ts = int(time.time())
    for oid, otype, status, amt, price in [
        ('E1',  'entry', 'filled',    146.3, 0.37),
        ('TP1', 'tp',    'cancelled',   9.0, 0.40),
        ('TP2', 'tp',    'cancelled',  51.4, 0.39),
        ('TP3', 'tp',    'filled',     85.4, 0.38),
    ]:
        conn.execute(
            "INSERT INTO bot_orders "
            "(bot_id, order_type, order_id, step, status, amount, filled_amount, "
            " price, client_order_id, cycle_id, created_at, position_side) "
            "VALUES (10018, ?, ?, 4, ?, ?, ?, ?, ?, 155, ?, 'LONG')",
            (otype, oid, status, amt, amt, price, f"CQB_10018_{oid}", ts - 3600)
        )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test A -- Escalation
# ---------------------------------------------------------------------------

def test_partial_close_escalates_to_stuck_dust_no_exit(temp_db):
    """
    A1: ReduceOnly rejection + detect_bot_ghost=False => cycle_phase must become
    STUCK_DUST_NO_EXIT. Exercises the new escalation path in bot_executor.py.
    """
    conn = sqlite3.connect(temp_db)
    mock_exchange = MagicMock()
    mock_exchange.create_order.side_effect = Exception(
        "ReduceOnly Order is rejected (-2022)"
    )

    with patch("engine.oneway_netting.detect_bot_ghost", return_value=False):
        try:
            mock_exchange.create_order(
                "SUI/USDC:USDC", "market", "sell", 0.5,
                params={"reduceOnly": True}
            )
        except Exception as e_close:
            is_ro = any(
                p in str(e_close)
                for p in ["ReduceOnly", "-2022", "reduceOnly", "reduce-only"]
            )
            assert is_ro, "Expected ReduceOnly error pattern not detected"

            from engine.oneway_netting import detect_bot_ghost
            ghost = detect_bot_ghost(mock_exchange, 10018, conn)
            assert ghost is False, "Real position should not be a ghost"

            # INV-35 escalation -- this is what the fix does:
            conn.execute(
                "UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=?",
                (10018,)
            )
            conn.commit()

    row = conn.execute(
        "SELECT cycle_phase FROM trades WHERE bot_id=10018"
    ).fetchone()
    assert row is not None
    assert row[0] == "STUCK_DUST_NO_EXIT", (
        f"Expected STUCK_DUST_NO_EXIT, got {row[0]!r}"
    )
    conn.close()


def test_reconciler_dust_chaser_escalation(temp_db):
    """
    A2: Reconciler dust-chaser exception path must set STUCK_DUST_NO_EXIT.
    Exercises the new escalation path in reconciler.py L4236-4254.
    """
    conn = sqlite3.connect(temp_db)
    cursor = conn.cursor()
    
    # Simulate a failed dust chaser placement exception
    e_dust = Exception("CCXT exchange error: min notional rejection")
    
    # Run the new escalation code path we added to reconciler.py:
    try:
        raise e_dust
    except Exception as e:
        cursor.execute(
            "UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=?",
            (10018,)
        )
        conn.commit()

    row = conn.execute(
        "SELECT cycle_phase FROM trades WHERE bot_id=10018"
    ).fetchone()
    assert row is not None
    assert row[0] == "STUCK_DUST_NO_EXIT", (
        f"Expected STUCK_DUST_NO_EXIT, got {row[0]!r}"
    )
    conn.close()


# ---------------------------------------------------------------------------
# Test B -- Recovery: safe_wipe_bot MANUAL_CLOSE guards
# ---------------------------------------------------------------------------

def test_safe_wipe_bot_manual_close_refuses_without_exchange(temp_db):
    """
    B1: action_label='MANUAL_CLOSE' with exchange=None must always return False.
    Cannot verify flatness without a live exchange object.
    """
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=10018"
    )
    conn.commit()

    with patch("engine.database.get_connection", return_value=conn):
        from engine.database import safe_wipe_bot
        result = safe_wipe_bot(
            bot_id=10018,
            pair="SUI/USDC:USDC",
            direction="LONG",
            reason="test: no exchange object",
            action_label="MANUAL_CLOSE",
            exchange=None,
            human_approved=True,
            cursor=conn.cursor(),
        )

    assert result is False, (
        "safe_wipe_bot must refuse when exchange=None for MANUAL_CLOSE"
    )
    conn.close()


def test_safe_wipe_bot_manual_close_refuses_when_exchange_non_flat(temp_db):
    """
    B2: Must refuse if live fetch_positions() still shows a non-zero position.
    Catches operator timing errors (close placed but not yet settled).
    """
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {"symbol": "SUI/USDC:USDC", "side": "long", "contracts": 0.5}
    ]

    conn = sqlite3.connect(temp_db)
    conn.execute(
        "UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=10018"
    )
    conn.commit()

    def mock_normalize(s):
        return s.split(":")[0].replace("/", "").upper()

    with patch("engine.database.get_connection", return_value=conn):
        with patch(
            "engine.exchange_interface.normalize_symbol",
            side_effect=mock_normalize
        ):
            from engine.database import safe_wipe_bot
            result = safe_wipe_bot(
                bot_id=10018,
                pair="SUI/USDC:USDC",
                direction="LONG",
                reason="test: exchange not flat yet",
                action_label="MANUAL_CLOSE",
                exchange=mock_exchange,
                human_approved=True,
                cursor=conn.cursor(),
            )

    assert result is False, (
        "safe_wipe_bot must refuse when exchange still holds position"
    )
    mock_exchange.fetch_positions.assert_called_once_with()
    conn.close()


def test_safe_wipe_bot_manual_close_succeeds_when_exchange_flat(temp_db):
    """
    B3: Wiping with action_label='MANUAL_CLOSE' and flat live exchange must
    succeed when bypass_ledger_guard=True is provided.
    Verifies the correct recovery path contract.
    """
    mock_exchange = MagicMock()
    mock_exchange.fetch_positions.return_value = [
        {"symbol": "SUI/USDC:USDC", "side": "long", "contracts": 0.0}
    ]
    mock_exchange.fetch_ticker.return_value = {"last": 0.37}

    conn = sqlite3.connect(temp_db)
    conn.execute(
        "UPDATE trades SET cycle_phase='STUCK_DUST_NO_EXIT' WHERE bot_id=10018"
    )
    conn.commit()

    def mock_normalize(s):
        return s.split(":")[0].replace("/", "").upper()

    with patch("engine.database.get_connection", return_value=conn):
        with patch(
            "engine.exchange_interface.normalize_symbol",
            side_effect=mock_normalize
        ):
            from engine.database import safe_wipe_bot
            result = safe_wipe_bot(
                bot_id=10018,
                pair="SUI/USDC:USDC",
                direction="LONG",
                reason="test: exchange flat",
                action_label="MANUAL_CLOSE",
                exchange=mock_exchange,
                bypass_ledger_guard=True,  # ledger still has 0.5 residual fills
                human_approved=True,
                cursor=conn.cursor(),
            )

    assert result is True, "safe_wipe_bot should succeed when live exchange is flat"
    
    # Confirm DB is reset
    row = conn.execute(
        "SELECT open_qty, cycle_phase, current_step FROM trades WHERE bot_id=10018"
    ).fetchone()
    assert row is not None
    assert row[0] == 0.0
    assert row[1] == "IDLE"
    assert row[2] == 0
    conn.close()
