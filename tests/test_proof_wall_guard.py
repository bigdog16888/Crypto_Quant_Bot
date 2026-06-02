import pytest
import uuid
import sqlite3
from unittest.mock import MagicMock
from engine import database
from engine.reconciler import StateReconciler, BotState

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_reconciler_{db_id}?mode=memory&cache=shared'
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

def _seed_bot(conn, bot_id, name, pair, direction, total_invested=0.0, open_qty=0.0, status='IN TRADE'):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (bot_id, name, pair, pair.split(':')[0].replace('/', '').upper(), direction, status),
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (?, 1, ?, ?, ?, 1, 1, 'ACTIVE', 0, ?)",
        (bot_id, open_qty, total_invested, 1.0 if total_invested > 0 else 0.0, direction),
    )
    conn.commit()

def test_find_proof_of_exit_ignores_stale_tp_orders(memory_db):
    bot_id = 10018
    cycle_id = 1
    
    _seed_bot(
        memory_db,
        bot_id=bot_id,
        name="sui long",
        pair="SUI/USDC:USDC",
        direction="LONG",
        total_invested=100.0,
        open_qty=100.0,
        status="IN TRADE"
    )

    # 1. Insert an entry/grid fill with timestamp 1000
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, cycle_id, position_side, filled_at) "
        "VALUES (?, 1, 'entry', 'ENTRY_1', 1.0, 100.0, 100.0, 'filled', ?, 'LONG', 1000)",
        (bot_id, cycle_id)
    )
    
    # 2. Insert a TP order that has a filled_at timestamp 500 (stale/earlier than entry)
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, cycle_id, position_side, filled_at) "
        "VALUES (?, 1, 'tp', 'STALE_TP_1', 1.05, 10.0, 10.0, 'filled', ?, 'LONG', 500)",
        (bot_id, cycle_id)
    )
    memory_db.commit()

    # Create StateReconciler
    mock_exchange = MagicMock()
    # Mock return value of fetch_closed_orders to return the stale TP order (timestamp is in milliseconds)
    mock_exchange.fetch_closed_orders.return_value = [
        {
            'id': 'STALE_TP_1',
            'clientOrderId': f'CQB_{bot_id}_TP_{cycle_id}_1',
            'status': 'closed',
            'timestamp': 500 * 1000,
            'lastTradeTimestamp': 500 * 1000,
        }
    ]
    
    reconciler = StateReconciler({'future': mock_exchange})
    bot_state = reconciler.get_bot_states()[0]
    
    # Run _find_proof_of_exit
    proof = reconciler._find_proof_of_exit(bot_state, exchange=mock_exchange)
    
    # It must ignore the stale TP because its fill timestamp (500) is before the latest entry/grid fill (1000)
    assert proof is None

    # 3. Insert a valid TP order filled at timestamp 1200 (after the grid fill)
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, step, order_type, order_id, price, amount, filled_amount, status, cycle_id, position_side, filled_at) "
        "VALUES (?, 1, 'tp', 'VALID_TP_1', 1.05, 100.0, 100.0, 'filled', ?, 'LONG', 1200)",
        (bot_id, cycle_id)
    )
    memory_db.commit()

    # Update exchange mock to return the valid TP order
    mock_exchange.fetch_closed_orders.return_value = [
        {
            'id': 'VALID_TP_1',
            'clientOrderId': f'CQB_{bot_id}_TP_{cycle_id}_1',
            'status': 'closed',
            'timestamp': 1200 * 1000,
            'lastTradeTimestamp': 1200 * 1000,
        }
    ]

    proof_valid = reconciler._find_proof_of_exit(bot_state, exchange=mock_exchange)
    
    # It must find and return the valid TP order
    assert proof_valid is not None
    assert proof_valid['id'] == 'VALID_TP_1'
