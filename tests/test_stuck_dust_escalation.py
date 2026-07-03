"""Tests for stuck dust size adjustment and database phase escalation."""
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
from engine.bot_executor import BotExecutor, _DUST_FLUSH_COOLDOWN
from config.settings import config


@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH

    database.backup_database = lambda: None
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_stuck_{db_id}?mode=memory&cache=shared'
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


def test_dust_catchall_escalates_when_stuck(memory_db):
    _DUST_FLUSH_COOLDOWN.clear()
    bot_id = 999
    sibling_id = 888
    pair = 'ETH/USDC:USDC'
    
    # 1. Setup DB state: Active bot 999 (SHORT) and active sibling 888 (LONG)
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (?, 'eth short', ?, 'SHORT', 1, 'ETHUSDC')",
        (bot_id, pair),
    )
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (?, 'eth long', ?, 'LONG', 1, 'ETHUSDC')",
        (sibling_id, pair),
    )
    # Bot 999 has small dust position
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (?, 1, 1, 0.012, 18.94, 1578.33, 'ACTIVE', 0, 'SHORT')",
        (bot_id,),
    )
    # Sibling bot 888 has larger position so sibling_count > 0
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (?, 1, 2, 0.409, 680.0, 1662.59, 'ACTIVE', 0, 'LONG')",
        (sibling_id,),
    )
    memory_db.commit()

    # 2. Setup Mock Runner, Mock Exchange, Mock Strategy
    mock_runner = MagicMock()
    mock_runner.get_thread_exchange = MagicMock()
    
    mock_exchange = MagicMock()
    mock_exchange.fetch_open_orders = MagicMock(return_value=[])
    mock_exchange.get_symbol_precision = MagicMock(return_value={'tick_size': 0.01, 'step_size': 0.001, 'min_notional': 20.0})
    mock_exchange.round_to_step = MagicMock(side_effect=lambda v, step: round(v / step) * step)
    mock_exchange.get_best_bid_ask = MagicMock(return_value=(1578.0, 1578.5))
    mock_exchange.validate_order = MagicMock(side_effect=lambda p, s, qty, px, *args, **kwargs: (True, qty, px, ""))
    
    # Force create_order to raise an exception simulating catch-22 rejection
    mock_exchange.create_order = MagicMock(side_effect=Exception("Binance API 400: ReduceOnly Order is rejected."))
    
    mock_runner.get_thread_exchange.return_value = mock_exchange
    
    # Mock strategy
    mock_strategy = MagicMock()
    mock_strategy.params = {'base_size': 25.0}
    mock_strategy.calculate_take_profit_price = MagicMock(return_value=1578.0)
    mock_strategy.calculate_take_profit_amount = MagicMock(return_value=0.012)
    mock_strategy.calculate_grid_order_price = MagicMock(return_value=(1500.0, "ATR offset"))
    mock_strategy.calculate_grid_order_amount = MagicMock(return_value=0.01)
    mock_strategy.max_steps = 8
    
    executor = BotExecutor(mock_runner)
    executor._get_strategy_instance = MagicMock(return_value=mock_strategy)
    executor._is_order_net_reducing = MagicMock(return_value=False)
    
    # 3. Execute maintain_orders
    bot_config = {'id': bot_id, 'name': 'eth short', 'pair': pair, 'direction': 'SHORT', 'is_active': 1, 'base_size': 25.0}
    bot_status = database.get_bot_status(bot_id)
    market_snapshot = {
        'positions': [],
        'market_data': {},
        'multi_tf_data': {}
    }
    executor.maintain_orders(bot_id, 'eth short', pair, 'SHORT', bot_status, 1578.0, mock_exchange, market_snapshot, bot_config)
    
    # 4. Verify DB state was escalated to STUCK_DUST_NO_EXIT
    cursor = memory_db.execute("SELECT cycle_phase FROM trades WHERE bot_id = ?", (bot_id,))
    res = cursor.fetchone()
    assert res is not None
    assert res[0] == 'STUCK_DUST_NO_EXIT'


def test_dust_retry_without_reduce_only(memory_db):
    _DUST_FLUSH_COOLDOWN.clear()
    bot_id = 999
    sibling_id = 888
    pair = 'ETH/USDC:USDC'
    
    # 1. Setup DB state
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (?, 'eth short', ?, 'SHORT', 1, 'ETHUSDC')",
        (bot_id, pair),
    )
    memory_db.execute(
        "INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair) "
        "VALUES (?, 'eth long', ?, 'LONG', 1, 'ETHUSDC')",
        (sibling_id, pair),
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (?, 1, 1, 0.012, 18.94, 1578.33, 'ACTIVE', 0, 'SHORT')",
        (bot_id,),
    )
    memory_db.execute(
        "INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, "
        "avg_entry_price, cycle_phase, wipe_wall_ts, position_side) "
        "VALUES (?, 1, 2, 0.409, 680.0, 1662.59, 'ACTIVE', 0, 'LONG')",
        (sibling_id,),
    )
    memory_db.commit()

    mock_runner = MagicMock()
    mock_runner.get_thread_exchange = MagicMock()
    
    mock_exchange = MagicMock()
    mock_exchange.fetch_open_orders = MagicMock(return_value=[])
    mock_exchange.get_symbol_precision = MagicMock(return_value={'tick_size': 0.01, 'step_size': 0.001, 'min_notional': 20.0})
    
    def mock_round(val, step):
        return round(val / step) * step
    mock_exchange.round_to_step = MagicMock(side_effect=mock_round)
    
    mock_exchange.get_best_bid_ask = MagicMock(return_value=(1578.0, 1578.5))
    mock_exchange.validate_order = MagicMock(side_effect=lambda p, s, qty, px, *args, **kwargs: (True, qty, px, ""))
    mock_exchange.create_order = MagicMock(return_value={'id': 'dust_exit_id', 'average': 1578.0, 'status': 'closed'})
    
    mock_runner.get_thread_exchange.return_value = mock_exchange
    
    mock_strategy = MagicMock()
    mock_strategy.params = {'base_size': 25.0}
    mock_strategy.calculate_take_profit_price = MagicMock(return_value=1578.0)
    mock_strategy.calculate_take_profit_amount = MagicMock(return_value=0.012)
    mock_strategy.calculate_grid_order_price = MagicMock(return_value=(1500.0, "ATR offset"))
    mock_strategy.calculate_grid_order_amount = MagicMock(return_value=0.01)
    mock_strategy.max_steps = 8
    
    executor = BotExecutor(mock_runner)
    executor._get_strategy_instance = MagicMock(return_value=mock_strategy)
    executor._is_order_net_reducing = MagicMock(return_value=False)
    
    # Execute maintain_orders
    bot_config = {'id': bot_id, 'name': 'eth short', 'pair': pair, 'direction': 'SHORT', 'is_active': 1, 'base_size': 25.0}
    bot_status = database.get_bot_status(bot_id)
    market_snapshot = {
        'positions': [],
        'market_data': {},
        'multi_tf_data': {}
    }
    executor.maintain_orders(bot_id, 'eth short', pair, 'SHORT', bot_status, 1578.0, mock_exchange, market_snapshot, bot_config)
    
    # Verify that create_order was called with the adjusted quantity and without reduceOnly
    assert mock_exchange.create_order.call_count >= 1
    first_call = mock_exchange.create_order.call_args_list[0]
    args, kwargs = first_call
    assert args[0] == pair
    assert args[1] == 'market'
    assert args[2] == 'buy'  # SHORT bot buys to close
    assert abs(args[3] - 0.013) < 1e-5
    
    params = kwargs.get('params', {})
    assert 'reduceOnly' not in params
