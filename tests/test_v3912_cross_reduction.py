import pytest
import sqlite3
import uuid
import time
from unittest.mock import MagicMock
from engine import database
from engine.oneway_netting import apply_oneway_entry_cross_reduction

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_cross_reduction_{db_id}?mode=memory&cache=shared'
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

def _seed_bot(conn, bot_id, pair, direction, open_qty=0.0, cycle=1, status='IN TRADE'):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (bot_id, f'bot_{bot_id}', pair, 'BTCUSDC', direction, status),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, wipe_wall_ts, position_side) "
        "VALUES (?, ?, ?, 0, ?)",
        (bot_id, cycle, open_qty, direction),
    )
    conn.commit()

def test_cross_reduction_idempotency(memory_db):
    # Seed a LONG bot with open_qty 0.008 (target) and a SHORT bot (filler)
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008, cycle=6)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=4)
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, "
        "filled_amount, status, cycle_id, step, position_side) "
        "VALUES (10016, 'entry', 'e6_idem', 'CQB_10016_ENTRY_6_idem', 60000.0, 0.008, 0.008, 'filled', 6, 1, 'LONG')"
    )
    memory_db.commit()

    # First call: should apply reduction
    cut1 = apply_oneway_entry_cross_reduction(
        10022, 'BTC/USDC:USDC', 'SHORT', 0.002, 'test_source_id_1', 60000.0
    )
    assert cut1 == pytest.approx(0.002)

    # Check target bot open_qty reduced to 0.006
    oq1 = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
    assert oq1 == pytest.approx(0.006)

    # Second call with SAME source_order_id: should be skipped by idempotency check
    cut2 = apply_oneway_entry_cross_reduction(
        10022, 'BTC/USDC:USDC', 'SHORT', 0.002, 'test_source_id_1', 60000.0
    )
    assert cut2 == 0.0

    # Target bot open_qty remains 0.006
    oq2 = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
    assert oq2 == pytest.approx(0.006)

def test_cross_reduction_recency_skip(memory_db):
    # Seed a LONG bot with open_qty 0.008 (target) and a SHORT bot (filler)
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008, cycle=6)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=4)

    # Insert an entry filled recently (10s ago) on the target bot (10016, cycle 6)
    recent_time = int(time.time()) - 10
    memory_db.execute(
        """
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount,
        filled_amount, status, cycle_id, step, position_side, filled_at)
        VALUES (10016, 'entry', 'e_recent', 'CQB_10016_ENTRY_6_1', 60000.0, 0.008, 0.008, 'filled', 6, 1, 'LONG', ?)
        """,
        (recent_time,)
    )
    memory_db.commit()

    # Call should skip due to recent fill (< 30s)
    cut = apply_oneway_entry_cross_reduction(
        10022, 'BTC/USDC:USDC', 'SHORT', 0.002, 'test_source_id_2', 60000.0
    )
    assert cut == 0.0

    # open_qty of target bot remains 0.008
    oq = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
    assert oq == pytest.approx(0.008)

def test_cross_reduction_normal_aging(memory_db):
    # Seed a LONG bot with open_qty 0.008 (target) and a SHORT bot (filler)
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008, cycle=6)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=4)

    # Insert an entry filled outside the 30s window (40s ago) on the target bot (10016, cycle 6)
    aged_time = int(time.time()) - 40
    memory_db.execute(
        """
        INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount,
        filled_amount, status, cycle_id, step, position_side, filled_at)
        VALUES (10016, 'entry', 'e_aged', 'CQB_10016_ENTRY_6_2', 60000.0, 0.008, 0.008, 'filled', 6, 1, 'LONG', ?)
        """,
        (aged_time,)
    )
    memory_db.commit()

    # Call should succeed because age > 30s
    cut = apply_oneway_entry_cross_reduction(
        10022, 'BTC/USDC:USDC', 'SHORT', 0.002, 'test_source_id_3', 60000.0
    )
    assert cut == pytest.approx(0.002)

    # open_qty of target bot reduced to 0.006
    oq = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
    assert oq == pytest.approx(0.006)
