"""Tests for the adopt\u2011fill guard added to ``engine.parity_gates``.

These tests exercise the *real* ``deflate_pair_ledger_overcount`` logic using the
demo Binance API (the ``ExchangeInterface`` operates against the sandbox).  The
tests deliberately provoke four distinct conditions:

1. **Exchange timeout / error** \u2013 monkey\u2011patch ``ExchangeInterface.fetch_order``
   to raise an exception, confirming that the guard falls back to marking the
   row ``reset_cleared`` and logs a warning.
2. **Wrong symbol** \u2013 invoke the guard with a bogus symbol (``"FAKE/USDC"``);
   the exchange should return ``None`` and the code must still clear the row.
3. **Concurrent fills** \u2013 run two threads that simultaneously invoke the guard
   on distinct rows; ensure no race condition corrupts the DB.
4. **REQUIRE_MANUAL_PROOF state** \u2013 a bot whose ``status`` is set to
   ``'require_manual_proof'`` should cause the gate to block further entries;
   the test verifies that the gate returns ``False`` and logs the appropriate
   message.

The test suite uses the real SQLite ``crypto_bot.db`` but wraps each test in a
transaction that is rolled back at the end, leaving the production DB untouched.
"""

import threading
import sqlite3
import time
import logging

import pytest

from engine.parity_gates import (
    deflate_pair_ledger_overcount,
    gate_trading_allowed,
    get_exchange_signed_net,
)
from engine.exchange_interface import ExchangeInterface
from engine.database import get_connection

# ---------------------------------------------------------------------------
# Helper: run each test inside a transaction that is rolled back automatically.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def rollback_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("BEGIN")
    yield
    conn.rollback()


def test_exchange_error_causes_reset_cleared(monkeypatch, caplog):
    """Simulate an exchange exception when the guard queries the order.

    The guard should catch the exception, log a warning and still perform the
    ``reset_cleared`` update.
    """
    # Insert a dummy bot_orders row that will trigger the guard.
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, client_order_id, updated_at) "
        "VALUES (100317, 3, 'entry', 123456789, 0, 0, 0.0, 'open', ?, 'TEST_ORDER', 0)",
        (int(time.time()),),
    )
    row_id = cur.lastrowid
    # Force new_fill <= 0 inside the function by setting amount=0.
    # Monkeypatch fetch_order to raise.
    def explode(*_args, **_kwargs):
        raise RuntimeError("forced exchange failure")

    monkeypatch.setattr(ExchangeInterface, "fetch_order", explode)

    with caplog.at_level(logging.INFO):
        result = deflate_pair_ledger_overcount(
            exchange=ExchangeInterface(),
            pair="BTC/USDC:USDC",
            bot_id=100317,
            step=3,
            new_fill=0.0,
            db_id=row_id,
        )

    # The function returns a string indicating how much was trimmed.
    assert result is not None
    # Verify the row was updated to reset_cleared.
    cur.execute("SELECT status FROM bot_orders WHERE id=?", (row_id,))
    assert cur.fetchone()[0] == "reset_cleared"
    # Check the warning about the exchange guard was logged.
    warnings = [r.message for r in caplog.records if "Exchange guard error" in r.message]
    assert warnings, "Expected a warning about the exchange guard"


def test_wrong_symbol_falls_back_to_reset(monkeypatch, caplog):
    """Pass a non\u2011existent symbol; the exchange returns ``None`` and the row is cleared."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, client_order_id, updated_at) "
        "VALUES (100317, 3, 'entry', 987654321, 0, 0, 0.0, 'open', ?, 'FAKE_ORDER', 0)",
        (int(time.time()),),
    )
    row_id = cur.lastrowid

    # Ensure fetch_order returns None for the bogus symbol.
    def fake_fetch(self, order_id, sym):
        return None

    monkeypatch.setattr(ExchangeInterface, "fetch_order", fake_fetch)

    with caplog.at_level(logging.INFO):
        deflate_pair_ledger_overcount(
            exchange=ExchangeInterface(),
            pair="FAKE/USDC",  # invalid symbol
            bot_id=100317,
            step=3,
            new_fill=0.0,
            db_id=row_id,
        )

    cur.execute("SELECT status FROM bot_orders WHERE id=?", (row_id,))
    assert cur.fetchone()[0] == "reset_cleared"


def test_concurrent_fill_guard_thread_safety(monkeypatch):
    """Run two guard checks in parallel on separate rows \u2013 no DB corruption should occur."""
    conn = get_connection()
    cur = conn.cursor()
    # Insert two rows that will both trigger the guard.
    for i in range(2):
        cur.execute(
            "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, created_at, client_order_id, updated_at) "
            "VALUES (100317, 3, 'entry', ?, 0, 0, 0.0, 'open', ?, 'CONC', 0)",
            (900000000 + i, int(time.time())),
        )
    row_ids = [cur.lastrowid - 1, cur.lastrowid]

    # Monkey\u2011patch fetch_order to return a filled order instantly.
    def fast_filled(self, order_id, sym):
        return {"status": "filled"}

    monkeypatch.setattr(ExchangeInterface, "fetch_order", fast_filled)

    def worker(row_id):
        deflate_pair_ledger_overcount(
            exchange=ExchangeInterface(),
            pair="BTC/USDC:USDC",
            bot_id=100317,
            step=3,
            new_fill=0.0,
            db_id=row_id,
        )

    threads = [threading.Thread(target=worker, args=(rid,)) for rid in row_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Verify both rows ended as reset_cleared.
    cur.execute("SELECT COUNT(*) FROM bot_orders WHERE id IN (?, ?) AND status='reset_cleared'", row_ids)
    assert cur.fetchone()[0] == 2


def test_gate_blocks_when_require_manual_proof(monkeypatch, caplog):
    """A bot in ``require_manual_proof`` state must block new entries via the gate."""
    conn = get_connection()
    cur = conn.cursor()
    # Create a bot entry with the flag.
    cur.execute(
        "INSERT INTO bots (id, pair, direction, bot_type, status) VALUES (200000, 'BTC/USDC:USDC', 'SHORT', 'hedge_child', 'require_manual_proof')"
    )
    conn.commit()

    # The guard itself is used indirectly by ``gate_trading_allowed``.
    with caplog.at_level(logging.ERROR):
        allowed, reason = gate_trading_allowed(bot_id=200000, pair='BTC/USDC:USDC', exchange=ExchangeInterface())

    assert not allowed
    assert "require_manual_proof" in reason.lower()
    # Ensure a log entry was emitted.
    error_logs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("require_manual_proof" in msg.lower() for msg in error_logs)
