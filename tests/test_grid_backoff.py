"""Tests for grid placement exponential backoff on network/408 errors."""
import pytest
import sqlite3
import time
import uuid
import sys
import os
from unittest.mock import MagicMock, patch

# Add root directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import database
from engine.bot_executor import BotExecutor
from config.settings import config

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_backoff_{db_id}?mode=memory&cache=shared'
    persistent_conn = orig_connect(shared_uri, uri=True)

    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)

    sqlite3.connect = mock_connect
    if hasattr(database._local, 'connection'):
        database._local.connection = None

    database.DB_PATH = shared_uri
    database.init_db()
    conn = database.get_connection()
    conn.commit()
    yield conn

    persistent_conn.close()
    sqlite3.connect = orig_connect
    database.backup_database = orig_backup
    database.DB_PATH = orig_db_path
    if hasattr(database._local, 'connection'):
        database._local.connection = None


def test_grid_placement_backoff_and_recovery(memory_db):
    bot_id = 999
    pair = 'SOL/USDC:USDC'
    
    # 1. Setup DB state: Active bot, in-trade, step 1, needs a grid order
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (?, 'test bot', ?, 'LONG', 1, 'SOLUSDC')",
        (bot_id, pair),
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (?, 1, 1, 0.1, 10.0, 100.0, 'ACTIVE', 0, 'LONG')",
        (bot_id,),
    )
    # Entry fill exists
    memory_db.execute(
        "INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, "
        "status, cycle_id, step, position_side) "
        "VALUES (?, 'entry', 'ent1', 100.0, 0.1, 0.1, 'filled', 1, 1, 'LONG')",
        (bot_id,),
    )
    memory_db.commit()

    # 2. Setup Mock Runner, Mock Exchange, Mock Strategy
    mock_runner = MagicMock()
    mock_runner.get_thread_exchange = MagicMock()
    
    # Mock exchange returns active TP order but no grid order
    mock_exchange = MagicMock()
    mock_exchange.fetch_open_orders = MagicMock(return_value=[
        {'id': 'tp_oid', 'clientOrderId': 'CQB_999_TP_1_1', 'price': 110.0, 'amount': 0.1, 'status': 'open', 'info': {'positionSide': 'LONG'}}
    ])
    mock_exchange.get_symbol_precision = MagicMock(return_value={'tick_size': 0.01, 'amount_step': 0.01})
    mock_exchange.round_to_step = MagicMock(side_effect=lambda v, step: round(v, 2))
    mock_exchange.get_best_bid_ask = MagicMock(return_value=(99.0, 100.0))
    mock_exchange.validate_order = MagicMock(side_effect=lambda p, s, qty, px, *args, **kwargs: (True, qty, px, ""))
    
    mock_runner.get_thread_exchange.return_value = mock_exchange
    
    # Mock strategy computes grid target and TP targets
    mock_strategy = MagicMock()
    mock_strategy.params = {'base_size': 150.0, 'martingale_multiplier': 2.0}
    mock_strategy.calculate_grid_order_price = MagicMock(return_value=(98.0, "ATR offset"))
    mock_strategy.calculate_grid_order_amount = MagicMock(return_value=0.1)
    mock_strategy.calculate_take_profit_price = MagicMock(return_value=110.0)
    mock_strategy.calculate_take_profit_amount = MagicMock(return_value=0.1)
    mock_strategy.max_steps = 10
    
    executor = BotExecutor(mock_runner)
    executor.strategies[bot_id] = mock_strategy

    # Mock market snapshot and bot status
    market_snapshot = {
        'positions': [{'symbol': pair, 'contracts': 0.1}],
        'market_data': {},
        'multi_tf_data': {}
    }
    
    # Mock create_order to raise CCXT request timeout (408)
    mock_exchange.create_order = MagicMock(side_effect=Exception("ccxt.RequestTimeout: Binance API 408"))

    # 3. First execution - expect placement attempt, catch, and backoff setup
    bot_status = database.get_bot_status(bot_id)
    executor.maintain_orders(bot_id, 'test bot', pair, 'LONG', bot_status, 100.0, mock_exchange, market_snapshot, {'base_size': 150.0})
    
    # Assert exchange.create_order was called
    assert mock_exchange.create_order.call_count == 1
    
    # Assert bot is backed off (fail_count=1)
    assert bot_id in executor._grid_backoff
    last_fail_ts, fail_count = executor._grid_backoff[bot_id]
    assert fail_count == 1
    
    # 4. Second execution (elapsed < delay) - expect skip, no create_order call
    mock_exchange.create_order.reset_mock()
    executor.maintain_orders(bot_id, 'test bot', pair, 'LONG', bot_status, 100.0, mock_exchange, market_snapshot, {'base_size': 150.0})
    
    assert mock_exchange.create_order.call_count == 0  # skipped!
    
    # 5. Third execution (elapsed > delay) - mock time to be in the future, expect retry
    mock_exchange.create_order.reset_mock()
    with patch('time.time', return_value=last_fail_ts + 3.0):
        executor.maintain_orders(bot_id, 'test bot', pair, 'LONG', bot_status, 100.0, mock_exchange, market_snapshot, {'base_size': 150.0})
    
    # Should attempt create_order again
    assert mock_exchange.create_order.call_count == 1
    # fail_count incremented to 2
    assert executor._grid_backoff[bot_id][1] == 2

    # 6. Fourth execution (elapsed > delay) with successful placement
    mock_exchange.create_order.reset_mock()
    mock_exchange.create_order.side_effect = None
    mock_exchange.create_order.return_value = {'id': 'grid_oid', 'status': 'open'}
    
    with patch('time.time', return_value=last_fail_ts + 10.0):
        executor.maintain_orders(bot_id, 'test bot', pair, 'LONG', bot_status, 100.0, mock_exchange, market_snapshot, {'base_size': 150.0})
        
    assert mock_exchange.create_order.call_count == 1
    # Backoff registry should be cleared on success
    assert bot_id not in executor._grid_backoff
