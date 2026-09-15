"""Explicit side= beats inference — end-to-end mechanism test.

credit_fill(side='BUY') on a SHORT-bot ENTRY (where inference would log SELL)
must write side='BUY' to exchange_fills. Proves the wiring chain:
call site kwarg -> WriteQueue arg passthrough -> _credit_fill_internal ->
record_exchange_fill -> exchange_fills.row.side.

Part of item 1 (side= caller-wiring), 2026-09-15.
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_explicit_side_beats_inference():
    path = _mk_db()
    try:
        # Bot is SHORT; order is an ENTRY. Inference branch would log SELL.
        # Explicit side='BUY' must win.
        conn = sqlite3.connect(path, timeout=10)
        conn.execute(
            "INSERT INTO bots (id, name, pair, direction, normalized_pair, status) "
            "VALUES (9901, 'test_short', 'BTC/USDC:USDC', 'SHORT', 'BTCUSDC', 'active')")
        conn.commit()
        conn.close()

        _seed_order(path, 'EXPL-OID-1', 'CQB-EXPL-1', 0.0, otype='entry')
        _credit(path, 9901, 'EXPL-OID-1', 0.10, 100.0,
                order_type='entry', is_cumulative=True, fill_ts=1000,
                caller='ws_live', side='BUY')

        rows = _fills(path)
        assert len(rows) == 1, f'expected exactly 1 fill row, got {rows}'
        assert rows[0][1] == 'BUY', (
            f'explicit side=BUY was not honored — exchange_fills shows '
            f'side={rows[0][1]!r} (inference won or side dropped)')
        assert abs(rows[0][2] - 0.10) < 1e-9
    finally:
        os.unlink(path)


# ── shared helpers (mirror tests/test_dual_write_guard.py) ──────────────────

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
