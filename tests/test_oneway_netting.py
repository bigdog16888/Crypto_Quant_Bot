"""One-way cross-bot netting tests."""
import pytest
import uuid
import sqlite3
from engine import database
from engine.oneway_netting import (
    gate_oneway_opposite_entry,
    apply_oneway_entry_cross_reduction,
    get_pair_open_qty_net,
)


@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_oneway_{db_id}?mode=memory&cache=shared'
    persistent_conn = orig_connect(shared_uri, uri=True)

    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)

    sqlite3.connect = mock_connect
    if hasattr(database._local, 'connection'):
        database._local.connection = None

    database.DB_PATH = shared_uri
    database.init_db()
    yield database.get_connection()
    sqlite3.connect = orig_connect
    database.DB_PATH = orig_db_path
    database.backup_database = orig_backup
    persistent_conn.close()


def _seed_bot(conn, bot_id, pair, direction, open_qty=0.0, cycle=1):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (?, ?, ?, ?, ?, 1, 'IN TRADE')",
        (bot_id, f'bot_{bot_id}', pair, 'BTCUSDC', direction),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side) "
        "VALUES (?, ?, ?, 0, ?)",
        (bot_id, cycle, open_qty, direction),
    )
    conn.commit()


def test_gate_blocks_opposite_entry(memory_db):
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0)
    ok, reason = gate_oneway_opposite_entry(10022, 'BTC/USDC:USDC', 'SHORT')
    assert not ok
    assert 'LONG' in reason


def test_cross_reduction_on_short_entry(memory_db):
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008, cycle=6)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=4)
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, "
        "filled_amount, status, cycle_id, step, position_side) "
        "VALUES (10016, 'entry', 'e6', 'CQB_10016_ENTRY_6_1', 1.0, 0.008, 0.008, 'filled', 6, 1, 'LONG')"
    )
    memory_db.commit()
    cut = apply_oneway_entry_cross_reduction(
        10022, 'BTC/USDC:USDC', 'SHORT', 0.002, '348125134', 76816.1
    )
    assert cut == pytest.approx(0.002)
    oq = memory_db.execute(
        "SELECT open_qty FROM trades WHERE bot_id=10016"
    ).fetchone()[0]
    assert oq == pytest.approx(0.006)
    assert get_pair_open_qty_net('BTC/USDC:USDC') == pytest.approx(0.006)


def test_get_pair_virtual_net_uses_open_qty(memory_db):
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.006)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0)
    assert database.get_pair_virtual_net('BTC/USDC:USDC') == pytest.approx(0.006)
