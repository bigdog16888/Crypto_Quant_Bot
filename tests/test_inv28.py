import pytest
import sqlite3
import uuid
import time
from unittest.mock import MagicMock, patch
from engine import database
from engine.oneway_netting import apply_oneway_entry_cross_reduction

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_inv28_{db_id}?mode=memory&cache=shared'
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

def test_inv28a_stale_tp_cancelled(memory_db):
    # Seed a LONG bot with open_qty 0.008 (target) and a SHORT bot (filler)
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008, cycle=6)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=4)

    # Seed an entry order in bot_orders for the target bot 10016 to allow correct sealing
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, "
        "filled_amount, status, cycle_id, step, position_side) "
        "VALUES (10016, 'entry', 'e6', 'CQB_10016_ENTRY_6_1', 60000.0, 0.008, 0.008, 'filled', 6, 1, 'LONG')"
    )
    # Seed an open TP order for the target bot 10016
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, "
        "filled_amount, status, cycle_id, step, position_side) "
        "VALUES (10016, 'tp', 'tp6', 'CQB_10016_TP_6_1', 61000.0, 0.008, 0.0, 'open', 6, 2, 'LONG')"
    )
    memory_db.commit()

    mock_exchange = MagicMock()
    mock_exchange.cancel_order = MagicMock()

    # Call cross-reduction
    cut = apply_oneway_entry_cross_reduction(
        10022, 'BTC/USDC:USDC', 'SHORT', 0.002, 'test_source_id_1', 60000.0, exchange=mock_exchange
    )
    assert cut == pytest.approx(0.002)

    # Check target bot open_qty reduced to 0.006
    oq = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10016").fetchone()[0]
    assert oq == pytest.approx(0.006)

    # Check that cancel_order was called with correct order_id and pair
    mock_exchange.cancel_order.assert_called_once_with('tp6', 'BTC/USDC:USDC')

    # Assert the sibling's TP order in bot_orders has status='cancelled' and notes contains '[CROSS-REDUCE-CANCEL'
    tp_row = memory_db.execute("SELECT status, notes FROM bot_orders WHERE order_id='tp6'").fetchone()
    assert tp_row[0] == 'cancelled'
    assert '[CROSS-REDUCE-CANCEL' in tp_row[1]

def test_inv28b_orphan_detected(memory_db):
    # Seed a LONG bot with open_qty 0.008 (sibling) and a SHORT bot with open_qty 0.002 (source)
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008, cycle=6)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.002, cycle=4)

    # Seed the entry order for sibling bot 10016 and filler bot 10022
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, "
        "filled_amount, status, cycle_id, step, position_side) "
        "VALUES (10016, 'entry', 'e6', 'CQB_10016_ENTRY_6_1', 60000.0, 0.008, 0.008, 'filled', 6, 1, 'LONG')"
    )
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, "
        "filled_amount, status, cycle_id, step, position_side) "
        "VALUES (10022, 'entry', 'e4', 'CQB_10022_ENTRY_4_1', 60000.0, 0.002, 0.002, 'filled', 4, 1, 'SHORT')"
    )
    memory_db.commit()

    mock_exchange = MagicMock()

    # We mock get_exchange_signed_net to return -0.002 (meaning physical net is still -0.002 SHORT, which is an orphan since the bot 10022 went flat)
    with patch('engine.parity_gates.get_exchange_signed_net', return_value=-0.002) as mock_get_net:
        # Call cross-reduction (SHORT entry on bot 10022 reduces LONG bot 10016 by 0.002)
        cut = apply_oneway_entry_cross_reduction(
            10022, 'BTC/USDC:USDC', 'SHORT', 0.002, 'test_source_id_2', 60000.0, exchange=mock_exchange
        )
        assert cut == pytest.approx(0.002)

        # Check that filling bot (10022) went flat
        oq = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10022").fetchone()[0]
        assert oq == 0.0

        # Assert status is set to 'pending_flatten' on the reduced-to-flat source bot (10022)
        status = memory_db.execute("SELECT status FROM bots WHERE id=10022").fetchone()[0]
        assert status == 'pending_flatten'

def test_inv28b_no_false_positive(memory_db):
    # Seed a LONG bot with open_qty 0.008 (sibling) and a SHORT bot with open_qty 0.0 (source, already flat)
    _seed_bot(memory_db, 10016, 'BTC/USDC:USDC', 'LONG', open_qty=0.008, cycle=6)
    _seed_bot(memory_db, 10022, 'BTC/USDC:USDC', 'SHORT', open_qty=0.0, cycle=4)

    # Seed the entry order for sibling bot 10016
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, "
        "filled_amount, status, cycle_id, step, position_side) "
        "VALUES (10016, 'entry', 'e6', 'CQB_10016_ENTRY_6_1', 60000.0, 0.008, 0.008, 'filled', 6, 1, 'LONG')"
    )
    memory_db.commit()

    mock_exchange = MagicMock()

    # Mock get_exchange_signed_net to return -0.002 (physical SHORT position exists)
    with patch('engine.parity_gates.get_exchange_signed_net', return_value=-0.002) as mock_get_net:
        # Call cross-reduction (SHORT entry on bot 10022)
        cut = apply_oneway_entry_cross_reduction(
            10022, 'BTC/USDC:USDC', 'SHORT', 0.002, 'test_source_id_3', 60000.0, exchange=mock_exchange
        )
        # Sibling has 0.008, remaining delta is 0.002, so sibling is reduced by 0.002.
        assert cut == pytest.approx(0.002)

        # Assert status is NOT set to 'pending_flatten' because it was already 0 before
        status = memory_db.execute("SELECT status FROM bots WHERE id=10022").fetchone()[0]
        assert status != 'pending_flatten'
