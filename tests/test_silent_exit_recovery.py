import time
import uuid
import sqlite3
import pytest
from unittest.mock import MagicMock, patch

from engine import database
from engine.reconciler import StateReconciler
from engine.ledger import seal_trade_state

# Shared Uri memory DB fixture
@pytest.fixture
def memory_db():
    orig_connect  = sqlite3.connect
    orig_backup   = database.backup_database
    orig_db_path  = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_audit_{db_id}?mode=memory&cache=shared'
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

def test_continuous_fill_audit_tp_filled(memory_db):
    """
    Verifies that _audit_pending_exits correctly:
      1. Detects a bot with an active TP order.
      2. Queries the exchange via REST.
      3. If status='filled', calls credit_fill with the exchange's actual price and quantity.
      4. Registers the TP cascade and seals the trade.
    """
    BOT_ID = 100002
    PAIR = 'ETH/USDC:USDC'
    NORM_PAIR = 'ETHUSDC'
    TP_ORDER_ID = 'tp_12345'
    OPEN_QTY = 0.066
    AVG_ENTRY = 2000.0
    CYCLE_ID = 33
    STEP = 1

    # Seed bot, trades, and an open bot_order
    memory_db.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status, config) "
        "VALUES (?, 'ETH Bot', ?, ?, 'SHORT', 1, 'IN TRADE', '{\"market_type\": \"future\"}')",
        (BOT_ID, PAIR, NORM_PAIR)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed, tp_order_id, position_side) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'SHORT')",
        (BOT_ID, CYCLE_ID, OPEN_QTY, OPEN_QTY * AVG_ENTRY, AVG_ENTRY, STEP, TP_ORDER_ID)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, position_side) "
        "VALUES (?, 'tp', ?, 'CQB_100002_TP_abc', ?, ?, 0.0, 'open', ?, ?, 'SHORT')",
        (BOT_ID, TP_ORDER_ID, AVG_ENTRY, OPEN_QTY, STEP, CYCLE_ID)
    )
    memory_db.commit()

    # Verify initial trade and order state
    trade_row = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id = ?", (BOT_ID,)).fetchone()
    assert float(trade_row[0]) == pytest.approx(OPEN_QTY)

    order_row = memory_db.execute("SELECT status, filled_amount FROM bot_orders WHERE order_id = ?", (TP_ORDER_ID,)).fetchone()
    assert order_row[0] == 'open'
    assert float(order_row[1]) == 0.0

    # Mock exchange
    mock_exchange = MagicMock()
    mock_exchange.fetch_order.return_value = {
        'id': TP_ORDER_ID,
        'status': 'filled',
        'filled': OPEN_QTY,
        'amount': OPEN_QTY,
        'average': 2016.37,
        'price': 2016.37,
        'lastTradeTimestamp': 1717200000000
    }

    reconciler = StateReconciler(exchanges={'future': mock_exchange})

    # Run continuous fill audit
    reconciler._audit_pending_exits()

    # Check that bot_orders was updated to filled
    order_row_after = memory_db.execute("SELECT status, filled_amount, price FROM bot_orders WHERE order_id = ?", (TP_ORDER_ID,)).fetchone()
    assert order_row_after[0] == 'filled'
    assert float(order_row_after[1]) == pytest.approx(OPEN_QTY)
    assert float(order_row_after[2]) == pytest.approx(2016.37) # actual exchange price

    # Check that trades.open_qty was updated to 0 (after seal)
    trade_row_after = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id = ?", (BOT_ID,)).fetchone()
    assert float(trade_row_after[0]) == pytest.approx(0.0)

    # Check that TP cascade was registered
    from engine.ledger import drain_tp_cascade
    cascades = drain_tp_cascade()
    assert len(cascades) == 1
    assert list(cascades)[0][0] == BOT_ID

def test_flat_position_guard_b_stale_snapshot(memory_db):
    """
    Verifies that Guard B defers global flatten if the snapshot in active_positions is stale (>60s).
    """
    BOT_ID = 100003
    PAIR = 'ETH/USDC:USDC'
    NORM_PAIR = 'ETHUSDC'

    # Seed bot, trade (open position in DB)
    memory_db.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status, config) "
        "VALUES (?, 'ETH Bot 2', ?, ?, 'LONG', 1, 'IN TRADE', '{\"market_type\": \"future\"}')",
        (BOT_ID, PAIR, NORM_PAIR)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed, cycle_phase, position_side) "
        "VALUES (?, 1, 0.05, 100.0, 2000.0, 1, 1, 'ACTIVE', 'LONG')",
        (BOT_ID,)
    )
    # Seed matching entry order to prevent DNA-WIPE
    memory_db.execute(
        "INSERT OR REPLACE INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, position_side, created_at) "
        "VALUES (?, 'entry', 'entry_123456', 'CQB_100003_ENTRY_abc', 2000.0, 0.05, 0.05, 'filled', 1, 1, 'LONG', ?)",
        (BOT_ID, int(time.time()))
    )
    # Seed a stale active_positions snapshot marker (90 seconds old)
    stale_ts = int(time.time()) - 90
    memory_db.execute(
        "INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (0, 'GLOBAL', 'FLAT', 0.0, 0.0, stale_ts)
    )
    memory_db.commit()

    # Mock exchange
    mock_exchange = MagicMock()
    # Exchange returns empty list (flat)
    mock_exchange.fetch_positions.return_value = []
    mock_exchange.fetch_open_orders.return_value = []

    reconciler = StateReconciler(exchanges={'future': mock_exchange})

    # Run reconcile
    reconciler.reconcile_all()

    # Verify that the bot is still in trade (NOT wiped/reset because of stale snapshot freshness guard)
    bot_status = memory_db.execute("SELECT status FROM bots WHERE id = ?", (BOT_ID,)).fetchone()[0]
    assert bot_status == 'IN TRADE'

    trade_qty = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id = ?", (BOT_ID,)).fetchone()[0]
    assert float(trade_qty) == pytest.approx(0.05)

def test_flat_position_guard_a_consecutive_counts(memory_db):
    """
    Verifies that Guard A defers global flatten until we see 3 consecutive flat snapshots.
    """
    BOT_ID = 100004
    PAIR = 'ETH/USDC:USDC'
    NORM_PAIR = 'ETHUSDC'

    # Seed bot, trade (open position in DB)
    memory_db.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status, config) "
        "VALUES (?, 'ETH Bot 3', ?, ?, 'LONG', 1, 'IN TRADE', '{\"market_type\": \"future\"}')",
        (BOT_ID, PAIR, NORM_PAIR)
    )
    memory_db.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed, cycle_phase, position_side) "
        "VALUES (?, 1, 0.05, 100.0, 2000.0, 1, 1, 'ACTIVE', 'LONG')",
        (BOT_ID,)
    )
    # Seed matching entry order to prevent DNA-WIPE
    memory_db.execute(
        "INSERT OR REPLACE INTO bot_orders (bot_id, order_type, order_id, client_order_id, price, amount, filled_amount, status, step, cycle_id, position_side, created_at) "
        "VALUES (?, 'entry', 'entry_123457', 'CQB_100004_ENTRY_abc', 2000.0, 0.05, 0.05, 'filled', 1, 1, 'LONG', ?)",
        (BOT_ID, int(time.time()))
    )
    # Seed a fresh active_positions snapshot marker (fresh)
    fresh_ts = int(time.time())
    memory_db.execute(
        "INSERT INTO active_positions (bot_id, pair, side, size, entry_price, last_checked) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (0, 'GLOBAL', 'FLAT', 0.0, 0.0, fresh_ts)
    )
    memory_db.commit()

    # Mock exchange
    mock_exchange = MagicMock()
    # Exchange returns empty list (flat)
    mock_exchange.fetch_positions.return_value = []
    mock_exchange.fetch_open_orders.return_value = []

    reconciler = StateReconciler(exchanges={'future': mock_exchange})

    # Cycle 1: flat snapshot count -> 1. Should defer.
    with patch('config.settings.config.REQUIRE_HUMAN_APPROVAL', False):
        reconciler.reconcile_all()
        assert reconciler._flat_snapshots_counts.get(NORM_PAIR) == 1
        assert memory_db.execute("SELECT status FROM bots WHERE id = ?", (BOT_ID,)).fetchone()[0] == 'IN TRADE'

        # Cycle 2: flat snapshot count -> 2. Should defer.
        # Update active_positions snapshot ts so it is still fresh
        fresh_ts2 = int(time.time())
        memory_db.execute("UPDATE active_positions SET last_checked = ? WHERE pair='GLOBAL'", (fresh_ts2,))
        memory_db.commit()

        reconciler.reconcile_all()
        assert reconciler._flat_snapshots_counts.get(NORM_PAIR) == 2
        assert memory_db.execute("SELECT status FROM bots WHERE id = ?", (BOT_ID,)).fetchone()[0] == 'IN TRADE'

        # Cycle 3: flat snapshot count -> 3. Should act (wipe)!
        fresh_ts3 = int(time.time())
        memory_db.execute("UPDATE active_positions SET last_checked = ? WHERE pair='GLOBAL'", (fresh_ts3,))
        memory_db.commit()

        reconciler.reconcile_all()
        assert reconciler._flat_snapshots_counts.get(NORM_PAIR) == 3
        # The bot should be wiped (status='Scanning', open_qty=0.0, total_invested=0.0)
        assert memory_db.execute("SELECT status FROM bots WHERE id = ?", (BOT_ID,)).fetchone()[0] == 'Scanning'
        trade_row = memory_db.execute("SELECT open_qty, total_invested FROM trades WHERE bot_id = ?", (BOT_ID,)).fetchone()
        assert float(trade_row[0]) == 0.0
        assert float(trade_row[1]) == 0.0
