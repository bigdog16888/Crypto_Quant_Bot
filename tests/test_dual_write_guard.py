"""RED/GREEN behavioral test for the ledger.py dual-write delta<=0 guard.

Proves (against a TEMP database, never the live crypto_bot.db):
1. Exit (tp) cumulative credit (delta>0)        -> exactly ONE exchange_fills row, qty=delta.
2. WS-oid + catchup-CID sync-reduction replay on a tp order -> must NOT add a second row.
   This is the genuinely reachable double-count: exit types bypass BOTH the step lock
   and the saturation guard (both are entry-only), and the two calls use different
   fill_claims keys (exchange oid vs client_order_id), so both proceed to the
   dual-write. Without the guard the replay writes qty=cumulative_qty on top of the
   first delta row (UNIQUE(exchange_order_id, fill_ts, qty, price) does not dedupe:
   qty differs) -> position ledger over-counts exits.
3. Incremental call (is_cumulative=False)       -> still logged, qty=increment
   (proves the guard exemption: a bare `delta<=0` skip would silently kill these).

Note: entry-type replays are separately protected by the STEP_x_y claim lock and are
not the exposed path; test 2 therefore uses order_type='tp'.
"""

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SCHEMA = """
CREATE TABLE bots (
    id INTEGER PRIMARY KEY, name TEXT, pair TEXT, direction TEXT,
    normalized_pair TEXT, status TEXT DEFAULT 'active', is_active INTEGER DEFAULT 1,
    bot_type TEXT, parent_bot_id INTEGER, hedge_child_bot_id INTEGER,
    hedge_trigger_step INTEGER, config TEXT
);
CREATE TABLE trades (
    bot_id INTEGER PRIMARY KEY, open_qty REAL DEFAULT 0, cycle_id INTEGER,
    total_invested REAL DEFAULT 0, avg_entry_price REAL DEFAULT 0
);
CREATE TABLE bot_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, step INTEGER,
    order_type TEXT, order_id TEXT, client_order_id TEXT, price REAL,
    amount REAL, filled_amount REAL DEFAULT 0, status TEXT DEFAULT 'new',
    created_at INTEGER DEFAULT 0, cycle_id INTEGER, filled_at INTEGER DEFAULT 0,
    updated_at INTEGER DEFAULT 0
);
CREATE TABLE fill_claims (
    bot_id INTEGER NOT NULL, order_id TEXT NOT NULL, caller TEXT NOT NULL DEFAULT '',
    claimed_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bot_id, order_id)
);
CREATE TABLE exchange_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange_order_id TEXT NOT NULL, client_order_id TEXT,
    symbol TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    qty REAL NOT NULL, price REAL NOT NULL, fee REAL DEFAULT 0, fee_asset TEXT,
    fill_ts INTEGER NOT NULL, source TEXT NOT NULL, bot_id INTEGER, order_type TEXT,
    step INTEGER, cycle_id INTEGER, raw_json TEXT,
    created_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(exchange_order_id, fill_ts, qty, price)
);
"""


def _mk_db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO bots (id, name, pair, direction, normalized_pair, status) "
        "VALUES (9901, 'test_long', 'BTC/USDC:USDC', 'LONG', 'BTCUSDC', 'active')")
    conn.commit()
    conn.close()
    return path


def _seed_order(path, order_id, cid, filled, otype='tp', step=1, cycle=1, amount=1.0):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute(
        "INSERT INTO bot_orders (bot_id, step, order_type, order_id, client_order_id, "
        "price, amount, filled_amount, status, created_at, cycle_id) "
        "VALUES (9901, ?, ?, ?, ?, 100.0, ?, ?, 'partially_filled', 1, ?)",
        (step, otype, order_id, cid, amount, filled, cycle))
    conn.execute("INSERT OR IGNORE INTO trades (bot_id, open_qty) VALUES (9901, 0)")
    conn.commit()
    conn.close()


def _fills(path):
    conn = sqlite3.connect(path, timeout=10)
    rows = conn.execute(
        "SELECT exchange_order_id, side, qty, price, fill_ts FROM exchange_fills "
        "WHERE bot_id = 9901 ORDER BY id").fetchall()
    conn.close()
    return rows


def _credit(path, *args, **kwargs):
    """Call _credit_fill_internal with get_connection bound to the temp DB.

    Every connection the function opens is tracked and closed before returning,
    so the temp file can be unlinked on Windows.
    """
    import engine.database as db_mod
    orig = db_mod.get_connection
    opened = []

    def _bound_get_connection():
        c = sqlite3.connect(path, timeout=10)
        opened.append(c)
        return c

    db_mod.get_connection = _bound_get_connection
    try:
        from engine.ledger import _credit_fill_internal
        return _credit_fill_internal(*args, **kwargs)
    finally:
        db_mod.get_connection = orig
        for c in opened:
            try:
                c.close()
            except Exception:
                pass


def test_tp_sync_reduction_replay_does_not_double_write():
    """WS credits a tp exit (exchange oid); catchup replays it via CID with a
    REDUCED cumulative (exchange-truth sync). The replay carries delta<0 on a
    cumulative call — nothing NEW to log. Must stay at ONE exchange_fills row."""
    path = _mk_db()
    try:
        _seed_order(path, 'EXCH-OID-9', 'CQB-X-9', 0.0, otype='tp')
        _credit(path, 9901, 'EXCH-OID-9', 0.10, 100.0, order_type='tp',
                is_cumulative=True, fill_ts=1000, caller='ws_live')
        rows = _fills(path)
        assert len(rows) == 1 and abs(rows[0][2] - 0.10) < 1e-9, (
            f"normal tp credit wrong: {rows}")

        _credit(path, 9901, 'CQB-X-9', 0.08, 100.0, order_type='tp',
                is_cumulative=True, sync_to_exchange=True, fill_ts=2000,
                caller='catchup_sync')
        rows = _fills(path)
        assert len(rows) == 1, (
            f"DOUBLE-COUNT: sync-reduction replay added a second exchange_fills "
            f"row on a tp order: {rows}")
    finally:
        os.unlink(path)


def test_incremental_call_still_logged():
    """is_cumulative=False (incremental delta): the guard must NOT suppress the
    write — cumulative_qty IS the new increment and delta<=0 is expected here."""
    path = _mk_db()
    try:
        _seed_order(path, 'EXCH-OID-I', 'CQB-I-9', 0.10, otype='tp')
        _credit(path, 9901, 'CQB-I-9', 0.05, 100.0, order_type='tp',
                is_cumulative=False, fill_ts=3000, caller='ws_incremental')
        rows = _fills(path)
        assert len(rows) == 1 and abs(rows[0][2] - 0.05) < 1e-9, (
            f"incremental fill was not logged correctly: {rows}")
    finally:
        os.unlink(path)
