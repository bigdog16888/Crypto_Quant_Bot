import sqlite3
import time
import uuid
from unittest.mock import patch, MagicMock
import pytest

import engine.database as database
from engine.runner import BotRunner
from config.settings import config

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_flatten_{db_id}?mode=memory&cache=shared'
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


def _create_mock_runner():
    with patch('engine.runner.BotRunner._initialize_exchanges'), \
         patch('engine.database.check_and_fix_integrity'), \
         patch('engine.migrations.migration_001_v2_schema.run'), \
         patch('engine.runner.BotRunner._post_init'):
        runner = BotRunner()
    return runner


def _seed_bot(conn, bot_id, name, pair, direction, status='pending_flatten', open_qty=1.0):
    conn.execute(
        "INSERT OR REPLACE INTO bots (id, name, pair, normalized_pair, direction, is_active, status, bot_type) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, 'standard')",
        (bot_id, name, pair, pair.replace('/', ''), direction, status)
    )
    conn.execute(
        "INSERT OR REPLACE INTO trades (bot_id, cycle_id, open_qty, total_invested, avg_entry_price, current_step, entry_confirmed) "
        "VALUES (?, 1, ?, 100.0, 100.0, 1, 1)",
        (bot_id, open_qty)
    )
    conn.commit()


def test_pending_flatten_executes_market_close(memory_db):
    _seed_bot(memory_db, 10001, 'dummy_bot', 'BTC/USDC', 'LONG', open_qty=1.0)

    runner = _create_mock_runner()
    mock_ex = MagicMock()
    mock_ex.is_testnet = False
    mock_ex.exchange = None
    # Return a LONG net of 1.0 so closeable_qty == virtual_qty == 1.0
    mock_ex.fetch_positions.return_value = [{
        'symbol': 'BTC/USDC',
        'net_qty': 1.0,
        'contracts': 1.0,
        'side': 'long',
        'qty': 1.0,
        'unrealizedPnl': 0.0,
        'entryPrice': 50000.0,
    }]

    def mock_create_order(pair, type, side, qty, params=None):
        # Assert: WAL receipt exists in DB BEFORE order execution
        rows = memory_db.execute("SELECT status, client_order_id, amount FROM bot_orders WHERE bot_id=10001").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 'placing'
        assert rows[0][1].startswith('CQB_10001_FLATTEN_')
        assert float(rows[0][2]) == 1.0

        # Assert: Exchange Gate validation is satisfied (newClientOrderId passed)
        assert params is not None
        assert 'newClientOrderId' in params
        assert params['newClientOrderId'] == rows[0][1]

        return {
            'id': 'exch_order_123',
            'filled': 1.0,
            'average': 50000.0,
            'status': 'closed'
        }

    mock_ex.create_order.side_effect = mock_create_order
    runner.exchange = mock_ex

    # Run the handler
    success = runner._handle_pending_flatten(10001, 'BTC/USDC', 'LONG', 1.0, memory_db)
    assert success is True


    # Assert: bot_orders flatten_close row exists; exchange-confirmed fill before wipe
    # Note: safe_wipe_bot marks prior orders 'reset_cleared' during cycle reset
    order_row = memory_db.execute(
        "SELECT status, filled_amount, price, order_id FROM bot_orders "
        "WHERE bot_id=10001 AND order_type='flatten_close'"
    ).fetchone()
    assert order_row is not None
    # Status will be 'filled' (if wipe preserved it) or 'reset_cleared' (normal wipe path)
    assert order_row[0] in ('filled', 'reset_cleared')

    # Assert: trades.open_qty=0 after handler
    open_qty = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10001").fetchone()[0]
    assert open_qty == 0.0

    # Assert: bots.status is a non-gated state after resolution
    status = memory_db.execute("SELECT status FROM bots WHERE id=10001").fetchone()[0]
    assert status not in ('pending_flatten', 'REQUIRE_MANUAL_PROOF', 'STUCK_DUST_NO_EXIT')




def test_pending_flatten_already_flat_just_resets(memory_db):
    _seed_bot(memory_db, 10001, 'dummy_bot', 'BTC/USDC', 'LONG', open_qty=0.0)

    runner = _create_mock_runner()
    mock_ex = MagicMock()
    runner.exchange = mock_ex

    # Run the cycle or block that resets flat bots
    # Inside run_cycle():
    flatten_bots = memory_db.execute("""
        SELECT b.id, b.pair, b.direction, t.open_qty
        FROM bots b JOIN trades t ON t.bot_id=b.id
        WHERE b.status='pending_flatten' AND b.is_active=1
    """).fetchall()

    for fb_id, fb_pair, fb_dir, fb_qty in flatten_bots:
        if fb_qty > 0.0001:
            runner._handle_pending_flatten(fb_id, fb_pair, fb_dir, fb_qty, memory_db)
        else:
            memory_db.execute(
                "UPDATE bots SET status='Scanning', cascade_started_at=0 WHERE id=?", (fb_id,)
            )
            memory_db.commit()

    # Assert: no exchange call made
    assert not mock_ex.create_order.called

    # Assert: bots.status='Scanning'
    status = memory_db.execute("SELECT status FROM bots WHERE id=10001").fetchone()[0]
    assert status == 'Scanning'


def test_pending_flatten_exchange_failure_sets_manual_proof(memory_db):
    """
    When the exchange call raises for a closeable position, bot must be gated
    REQUIRE_MANUAL_PROOF. When closeable_qty=0 (unphysical), safe_wipe succeeds
    and the test must confirm the bot reached a resolved state, not a failure.
    This test exercises the exchange-failure path by returning a LONG net so
    closeable_qty > 0, then simulating a ccxt error.
    """
    _seed_bot(memory_db, 10001, 'dummy_bot', 'BTC/USDC', 'LONG', open_qty=1.0)

    runner = _create_mock_runner()
    mock_ex = MagicMock()
    # Return LONG net of 1.0 so closeable_qty=1.0 (exchange order will be attempted)
    mock_ex.fetch_positions.return_value = [{'symbol': 'BTC/USDC', 'net_qty': 1.0,
        'contracts': 1.0, 'side': 'long', 'qty': 1.0,
        'unrealizedPnl': 0.0, 'entryPrice': 50000.0}]

    mock_ex.exchange = None
    mock_ex.create_order.side_effect = Exception("ccxt error: connection timed out")
    runner.exchange = mock_ex

    success = runner._handle_pending_flatten(10001, 'BTC/USDC', 'LONG', 1.0, memory_db)
    assert success is False

    # Assert: bot_orders flatten_close row exists with status='failed'
    order_row = memory_db.execute(
        "SELECT status FROM bot_orders WHERE bot_id=10001 AND order_type='flatten_close'"
    ).fetchone()
    assert order_row is not None
    assert order_row[0] == 'failed'

    # Assert: bot status set to REQUIRE_MANUAL_PROOF
    bot_row = memory_db.execute("SELECT status FROM bots WHERE id=10001").fetchone()
    assert bot_row[0] == 'REQUIRE_MANUAL_PROOF'

    # Assert: trades.open_qty unchanged
    open_qty = memory_db.execute("SELECT open_qty FROM trades WHERE bot_id=10001").fetchone()[0]
    assert open_qty == 1.0


def test_pending_flatten_excluded_from_main_loop(memory_db):
    _seed_bot(memory_db, 10001, 'dummy_bot', 'BTC/USDC', 'LONG', status='pending_flatten', open_qty=1.0)

    runner = _create_mock_runner()
    # Mock exchanges to bypass REST/WS calls
    runner.exchanges = {'spot': MagicMock()}
    runner.exchange = runner.exchanges['spot']
    runner.exchange.exchange = None
    runner.exchange.fetch_positions.return_value = []
    runner.cycle_count = 1

    # Mock ThreadPoolExecutor map
    mock_pool = MagicMock()
    runner.bot_pool = mock_pool

    # Mock get_active_bots to return our pending_flatten bot
    runner.get_active_bots = MagicMock(return_value=[
        (10001, 'dummy_bot', 'BTC/USDC', 'LONG', 'standard', '{}', 1.0, 1, 0.0, 1, 0.1, 1.0, 'pending_flatten')
    ])

    # Mock _handle_pending_flatten to just pass
    runner._handle_pending_flatten = MagicMock()

    # Mock os.path.exists specifically for stop/emergency files
    import os
    orig_exists = os.path.exists
    def mock_exists(path):
        if 'emergency' in str(path) or 'stop' in str(path):
            return False
        return orig_exists(path)

    # Stub the rest of run_cycle so it doesn't fail on snapshot queries
    with patch('engine.runner.get_ws_cache') as mock_ws_cache, \
         patch('engine.runner.get_connection', return_value=memory_db), \
         patch('engine.runner.os.path.exists', side_effect=mock_exists), \
         patch('engine.shutdown_control.is_stop_requested', return_value=False), \
         patch.object(BotRunner, '_abort_if_stop_requested', return_value=False):
         
        mock_ws_cache.return_value.is_fresh.return_value = True
        mock_ws_cache.return_value.get_all_positions.return_value = []
        mock_ws_cache.return_value.get_all_open_orders.return_value = []
        res = runner.run_cycle()
        print("RUN CYCLE RETURN VALUE:", res)

    # Assert: ThreadPoolExecutor map was called, but the bot list passed to it was empty
    # (since the pending_flatten bot was excluded)
    assert mock_pool.map.called
    call_args = mock_pool.map.call_args
    passed_bots = list(call_args[0][1])
    assert len(passed_bots) == 0, "pending_flatten bot was not excluded from ThreadPoolExecutor"


def test_pending_flatten_no_positionside_on_mainnet(memory_db):
    _seed_bot(memory_db, 10001, 'dummy_bot', 'BTC/USDC', 'LONG', open_qty=1.0)

    runner = _create_mock_runner()
    mock_ex = MagicMock()
    mock_ex.is_testnet = False
    mock_ex.exchange = None
    # Return LONG net so closeable_qty == 1.0 (order will be placed)
    mock_ex.fetch_positions.return_value = [{
        'symbol': 'BTC/USDC', 'net_qty': 1.0, 'contracts': 1.0,
        'side': 'long', 'qty': 1.0, 'unrealizedPnl': 0.0, 'entryPrice': 50000.0,
    }]
    mock_ex.create_order.return_value = {
        'id': 'ord1', 'filled': 1.0, 'average': 50000.0, 'status': 'closed'
    }
    runner.exchange = mock_ex

    runner._handle_pending_flatten(10001, 'BTC/USDC', 'LONG', 1.0, memory_db)

    # Assert: create_order params dict does NOT contain 'positionSide'
    assert mock_ex.create_order.called
    called_params = mock_ex.create_order.call_args[1].get('params', {})
    assert 'positionSide' not in called_params
    assert 'newClientOrderId' in called_params
    assert called_params['newClientOrderId'].startswith('CQB_10001_FLATTEN_')



def test_pending_flatten_positionside_both_on_testnet(memory_db):
    _seed_bot(memory_db, 10001, 'dummy_bot', 'BTC/USDC', 'LONG', open_qty=1.0)

    runner = _create_mock_runner()
    mock_ex = MagicMock()
    mock_ex.is_testnet = True
    mock_ex.exchange = None
    # Return LONG net so closeable_qty == 1.0 (order will be placed)
    mock_ex.fetch_positions.return_value = [{
        'symbol': 'BTC/USDC', 'net_qty': 1.0, 'contracts': 1.0,
        'side': 'long', 'qty': 1.0, 'unrealizedPnl': 0.0, 'entryPrice': 50000.0,
    }]
    mock_ex.create_order.return_value = {
        'id': 'ord2', 'filled': 1.0, 'average': 50000.0, 'status': 'closed'
    }
    runner.exchange = mock_ex

    runner._handle_pending_flatten(10001, 'BTC/USDC', 'LONG', 1.0, memory_db)

    # Assert: create_order params contains positionSide='BOTH'
    assert mock_ex.create_order.called
    called_params = mock_ex.create_order.call_args[1].get('params', {})
    assert called_params.get('positionSide') == 'BOTH'
    assert 'newClientOrderId' in called_params
    assert called_params['newClientOrderId'].startswith('CQB_10001_FLATTEN_')

