import time
import uuid
import sqlite3
import pytest
from unittest.mock import MagicMock

from engine import database
from engine.reconciler import StateReconciler

# Shared Uri memory DB fixture
@pytest.fixture
def memory_db():
    orig_connect  = sqlite3.connect
    orig_backup   = database.backup_database
    orig_db_path  = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_grid_audit_{db_id}?mode=memory&cache=shared'
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

def test_audit_pending_grids_filled(memory_db):
    """
    Verifies that a grid order confirmed filled on the exchange:
      1. Is credited via credit_fill.
      2. The trade state is sealed and recomputed.
    """
    BOT_ID = 10017
    PAIR = 'XRP/USDC:USDC'
    NORM_PAIR = 'XRPUSDC'
    GRID_ORDER_ID = '172648101'
    CLIENT_ORDER_ID = 'CQB_10017_GRID_133_2'
    CYCLE_ID = 133
    STEP = 2

    # Seed bot, trades, and an open grid order + filled entry order
    memory_db.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status, config) "
        "VALUES (?, 'XRP Bot', ?, ?, 'LONG', 1, 'IN TRADE', '{\"market_type\": \"future\"}')",
        (BOT_ID, PAIR, NORM_PAIR)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed, tp_order_id, position_side) "
        "VALUES (?, ?, 4.7, 5.17752, 1.1016, 1, 1, 'CQB_10017_TP_133_1', 'LONG')",
        (BOT_ID, CYCLE_ID)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, position_side) "
        "VALUES (?, 'entry', 'entry_123', 'CQB_10017_ENTRY_133_1', 1.1016, 4.7, 4.7, 'filled', 1, ?, 'LONG')",
        (BOT_ID, CYCLE_ID)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, position_side) "
        "VALUES (?, 'grid', ?, ?, 1.0978, 10.0, 0.0, 'new', ?, ?, 'LONG')",
        (BOT_ID, GRID_ORDER_ID, CLIENT_ORDER_ID, STEP, CYCLE_ID)
    )
    memory_db.commit()

    # Mock CCXT
    mock_exchange = MagicMock()
    mock_exchange.fetch_order.return_value = {
        'id': GRID_ORDER_ID,
        'clientOrderId': CLIENT_ORDER_ID,
        'status': 'filled',
        'filled': 10.0,
        'amount': 10.0,
        'average': 1.0978,
        'price': 1.0978,
        'lastTradeTimestamp': int(time.time() * 1000)
    }
    mock_exchange.fetch_positions.return_value = [
        {
            'symbol': 'XRP/USDC:USDC',
            'contracts': 14.7,
            'net_qty': 14.7,
            'side': 'long'
        }
    ]

    reconciler = StateReconciler(exchanges={'future': mock_exchange})

    # Run continuous grid fill audit
    reconciler._audit_pending_grids()

    # Assert bot_orders was updated
    order_row = memory_db.execute("SELECT status, filled_amount FROM bot_orders WHERE order_id = ?", (GRID_ORDER_ID,)).fetchone()
    assert order_row[0] == 'filled'
    assert float(order_row[1]) == 10.0

    # Assert trade open_qty was sealed and recomputed correctly
    trade_row = memory_db.execute("SELECT open_qty, avg_entry_price, current_step FROM trades WHERE bot_id = ?", (BOT_ID,)).fetchone()
    assert float(trade_row[0]) == pytest.approx(14.7)
    assert float(trade_row[1]) == pytest.approx(1.09901497)
    assert trade_row[2] == 2

def test_audit_pending_grids_open_unchanged(memory_db):
    """
    Verifies that a grid order still open on the exchange is left unchanged in the DB.
    """
    BOT_ID = 10017
    PAIR = 'XRP/USDC:USDC'
    NORM_PAIR = 'XRPUSDC'
    GRID_ORDER_ID = '172648101'
    CLIENT_ORDER_ID = 'CQB_10017_GRID_133_2'
    CYCLE_ID = 133
    STEP = 2

    # Seed bot, trades, and an open grid order
    memory_db.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status, config) "
        "VALUES (?, 'XRP Bot', ?, ?, 'LONG', 1, 'IN TRADE', '{\"market_type\": \"future\"}')",
        (BOT_ID, PAIR, NORM_PAIR)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed, tp_order_id, position_side) "
        "VALUES (?, ?, 4.7, 5.17752, 1.1016, 1, 1, 'CQB_10017_TP_133_1', 'LONG')",
        (BOT_ID, CYCLE_ID)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, position_side) "
        "VALUES (?, 'grid', ?, ?, 1.0978, 10.0, 0.0, 'new', ?, ?, 'LONG')",
        (BOT_ID, GRID_ORDER_ID, CLIENT_ORDER_ID, STEP, CYCLE_ID)
    )
    memory_db.commit()

    # Mock CCXT to return 'open' status
    mock_exchange = MagicMock()
    mock_exchange.fetch_order.return_value = {
        'id': GRID_ORDER_ID,
        'clientOrderId': CLIENT_ORDER_ID,
        'status': 'open',
        'filled': 0.0,
        'amount': 10.0,
        'price': 1.0978
    }

    reconciler = StateReconciler(exchanges={'future': mock_exchange})

    # Run continuous grid fill audit
    reconciler._audit_pending_grids()

    # Assert bot_orders status remains 'new' (or whatever it was in DB)
    order_row = memory_db.execute("SELECT status, filled_amount FROM bot_orders WHERE order_id = ?", (GRID_ORDER_ID,)).fetchone()
    assert order_row[0] == 'new'
    assert float(order_row[1]) == 0.0

    # Assert trade open_qty remains unchanged
    trade_row = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id = ?", (BOT_ID,)).fetchone()
    assert float(trade_row[0]) == pytest.approx(4.7)

def test_audit_pending_grids_cancelled(memory_db):
    """
    Verifies that a grid order cancelled on the exchange is marked cancelled in the DB
    without crediting any filled qty.
    """
    BOT_ID = 10017
    PAIR = 'XRP/USDC:USDC'
    NORM_PAIR = 'XRPUSDC'
    GRID_ORDER_ID = '172648101'
    CLIENT_ORDER_ID = 'CQB_10017_GRID_133_2'
    CYCLE_ID = 133
    STEP = 2

    # Seed bot, trades, and an open grid order
    memory_db.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status, config) "
        "VALUES (?, 'XRP Bot', ?, ?, 'LONG', 1, 'IN TRADE', '{\"market_type\": \"future\"}')",
        (BOT_ID, PAIR, NORM_PAIR)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed, tp_order_id, position_side) "
        "VALUES (?, ?, 4.7, 5.17752, 1.1016, 1, 1, 'CQB_10017_TP_133_1', 'LONG')",
        (BOT_ID, CYCLE_ID)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, position_side) "
        "VALUES (?, 'grid', ?, ?, 1.0978, 10.0, 0.0, 'new', ?, ?, 'LONG')",
        (BOT_ID, GRID_ORDER_ID, CLIENT_ORDER_ID, STEP, CYCLE_ID)
    )
    memory_db.commit()

    # Mock CCXT to return 'canceled' status
    mock_exchange = MagicMock()
    mock_exchange.fetch_order.return_value = {
        'id': GRID_ORDER_ID,
        'clientOrderId': CLIENT_ORDER_ID,
        'status': 'canceled',
        'filled': 0.0,
        'amount': 10.0,
        'price': 1.0978
    }

    reconciler = StateReconciler(exchanges={'future': mock_exchange})

    # Run continuous grid fill audit
    reconciler._audit_pending_grids()

    # Assert bot_orders status is updated to cancelled
    order_row = memory_db.execute("SELECT status FROM bot_orders WHERE order_id = ?", (GRID_ORDER_ID,)).fetchone()
    assert order_row[0] in ('cancelled', 'canceled')

    # Assert trade open_qty remains unchanged
    trade_row = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id = ?", (BOT_ID,)).fetchone()
    assert float(trade_row[0]) == pytest.approx(4.7)
