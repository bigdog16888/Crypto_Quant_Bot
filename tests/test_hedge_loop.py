import pytest
import sqlite3
import time
from unittest.mock import MagicMock, patch
from engine.bot_executor import BotExecutor

class ConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn
    def __getattr__(self, name):
        return getattr(self.conn, name)
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.conn.__exit__(exc_type, exc_val, exc_tb)
    def close(self):
        pass

@pytest.fixture
def in_memory_db():
    conn = sqlite3.connect(':memory:')
    conn.executescript("""
        CREATE TABLE bots (id INTEGER PRIMARY KEY, name TEXT, pair TEXT, direction TEXT, is_active INTEGER DEFAULT 1, status TEXT DEFAULT 'Scanning', config TEXT DEFAULT '{}');
        CREATE TABLE trades (bot_id INTEGER PRIMARY KEY, total_invested REAL DEFAULT 0, avg_entry_price REAL DEFAULT 0, current_step INTEGER DEFAULT 0, cycle_phase TEXT DEFAULT 'ACTIVE', entry_confirmed INTEGER DEFAULT 0, cycle_id INTEGER DEFAULT 1, open_qty REAL DEFAULT 0, basket_start_time INTEGER DEFAULT 0, tp_order_id TEXT, target_tp_price REAL DEFAULT 0);
        CREATE TABLE bot_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER, order_type TEXT, order_id TEXT, client_order_id TEXT, step INTEGER DEFAULT 0, status TEXT DEFAULT 'open', amount REAL DEFAULT 0, filled_amount REAL DEFAULT 0, price REAL DEFAULT 0, position_side TEXT, cycle_id INTEGER DEFAULT 1, notes TEXT, created_at INTEGER DEFAULT (strftime('%s','now')), updated_at INTEGER DEFAULT (strftime('%s','now')));
        CREATE TABLE active_positions (pair TEXT, side TEXT, size REAL, bot_id INTEGER, entry_price REAL, liq_price REAL, unrealized_pnl REAL, updated_at INTEGER DEFAULT (strftime('%s','now')), PRIMARY KEY (pair, side));
    """)
    conn.execute("INSERT INTO bots (id, name, pair, direction) VALUES (999, 'test_long', 'SUI/USDC:USDC', 'LONG')")
    conn.execute("INSERT INTO trades (bot_id, total_invested, avg_entry_price, current_step, cycle_phase, entry_confirmed, cycle_id, open_qty, basket_start_time) VALUES (999, 1012.4, 1.2060, 5, 'ACTIVE', 1, 1, 840.0, 0)")
    conn.commit()
    return conn

@pytest.fixture
def mock_exchange():
    ex = MagicMock()
    ex.get_symbol_precision.return_value = {'price_precision': 4, 'qty_precision': 1, 'step_size': 0.1, 'tick_size': 0.0001, 'min_notional': 5.0}
    ex.round_to_step.side_effect = lambda qty, step: round(qty, 1)
    ex.create_order.return_value = {'id': 'TEST_ORDER_001', 'status': 'open'}
    ex.validate_order.side_effect = lambda symbol, side, amount, price=None, is_closing=False: (True, amount, price, "")
    return ex

@pytest.fixture
def executor(in_memory_db):
    runner = MagicMock()
    ex = BotExecutor(runner)
    wrapped_db = ConnectionWrapper(in_memory_db)
    with patch('engine.database.get_connection', return_value=wrapped_db):
        yield ex, wrapped_db

# 🚀 v3.3.0 REGRESSION TESTS



def test_reduce_only_false_when_sell_increases_account_net_short(executor, mock_exchange):
    ex, db = executor
    # Net: 0.011 (LONG) - 0.026 (SHORT) = -0.015 (NET SHORT)
    db.execute("INSERT INTO bots (id, name, pair, direction) VALUES (998, 'short_bot', 'SUI/USDC:USDC', 'SHORT')")
    db.execute("INSERT INTO trades (bot_id, total_invested, open_qty) VALUES (998, 500.0, 0.026)")
    db.execute("INSERT INTO active_positions (pair, side, size) VALUES ('SUIUSDC', 'LONG', 0.011), ('SUIUSDC', 'SHORT', 0.026)")
    db.commit()

    with patch('engine.bot_executor.normalize_symbol', return_value='SUIUSDC'):
        result = ex._is_order_net_reducing('SUI/USDC:USDC', 'sell', 0.011, bot_id=999, bot_direction='LONG')

    # Selling 0.011 LONG increases net SHORT from 0.015 to 0.026. Result must be False.
    assert result is False

def test_reduce_only_true_when_sole_bot(executor, mock_exchange):
    ex, db = executor
    db.execute("DELETE FROM active_positions")
    db.execute("INSERT INTO active_positions (pair, side, size) VALUES ('SUIUSDC', 'LONG', 0.011)")
    db.commit()

    with patch('engine.bot_executor.normalize_symbol', return_value='SUIUSDC'):
        # sibling_count=0 is automatically calculated by the DB query inside _is_order_net_reducing
        result = ex._is_order_net_reducing('SUI/USDC:USDC', 'sell', 0.011, bot_id=999, bot_direction='LONG')

    assert result is True


