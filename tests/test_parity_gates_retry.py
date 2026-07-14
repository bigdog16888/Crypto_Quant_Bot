import pytest
from unittest.mock import MagicMock, patch
import sqlite3
import time

from engine.parity_gates import (
    get_exchange_signed_net,
    pair_parity_ok,
    clear_bot_require_manual_proof
)
from engine.ledger import seal_trade_state
from engine.database import get_connection

@pytest.fixture
def memory_db():
    conn = sqlite3.connect(':memory:')
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE bots (
            id INTEGER PRIMARY KEY,
            name TEXT,
            pair TEXT,
            direction TEXT,
            bot_type TEXT DEFAULT 'standard',
            status TEXT DEFAULT 'Stopped',
            is_active INTEGER DEFAULT 1,
            config TEXT DEFAULT '{}',
            parent_bot_id INTEGER,
            hedge_child_bot_id INTEGER,
            cascade_started_at INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE trades (
            bot_id INTEGER PRIMARY KEY,
            current_step INTEGER DEFAULT 0,
            total_invested REAL DEFAULT 0.0,
            avg_entry_price REAL DEFAULT 0.0,
            target_tp_price REAL DEFAULT 0.0,
            last_exit_price REAL DEFAULT 0.0,
            last_exit_time INTEGER DEFAULT 0,
            basket_start_time INTEGER DEFAULT 0,
            entry_confirmed INTEGER DEFAULT 0,
            entry_order_id TEXT,
            tp_order_id TEXT,
            bot_position_id TEXT,
            close_type TEXT,
            cycle_id INTEGER DEFAULT 1,
            cycle_phase TEXT DEFAULT 'IDLE',
            position_side TEXT DEFAULT 'LONG',
            open_qty REAL DEFAULT 0.0,
            wipe_wall_ts INTEGER DEFAULT 0,
            cycle_start_time INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE fill_claims (
            bot_id INTEGER,
            order_id TEXT,
            caller TEXT,
            claimed_at INTEGER,
            PRIMARY KEY (bot_id, order_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE bot_orders (
            id INTEGER PRIMARY KEY,
            bot_id INTEGER,
            step INTEGER,
            order_type TEXT,
            order_id TEXT,
            price REAL,
            amount REAL,
            filled_amount REAL,
            status TEXT,
            created_at INTEGER,
            client_order_id TEXT,
            notes TEXT,
            cycle_id INTEGER,
            position_side TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE active_positions (
            bot_id INTEGER,
            pair TEXT,
            side TEXT,
            size REAL,
            entry_price REAL,
            updated_at INTEGER,
            updated_hkt TEXT
        )
    """)
    conn.commit()
    with patch('engine.database.get_connection', return_value=conn):
        yield conn
    conn.close()

def test_get_exchange_signed_net_retry_success(memory_db):
    mock_ex = MagicMock()
    # Fails 2 times, then returns a list on the 3rd attempt
    mock_ex.fetch_positions.side_effect = [
        Exception("Timeout"),
        Exception("Rate Limit"),
        [{"symbol": "BTCUSDC", "net_qty": 1.5}]
    ]
    
    with patch('config.settings.config.TESTING_MODE', True):
        net = get_exchange_signed_net(mock_ex, "BTC/USDC")
        assert net == 1.5
        assert mock_ex.fetch_positions.call_count == 3

def test_get_exchange_signed_net_persistent_failure(memory_db):
    mock_ex = MagicMock()
    # Fails all 3 times
    mock_ex.fetch_positions.side_effect = Exception("Persistent API Error")
    
    with patch('config.settings.config.TESTING_MODE', True):
        net = get_exchange_signed_net(mock_ex, "BTC/USDC")
        assert net is None
        assert mock_ex.fetch_positions.call_count == 3

def test_pair_parity_ok_persistent_failure_gating(memory_db):
    mock_ex = MagicMock()
    mock_ex.fetch_positions.side_effect = Exception("API Down")
    
    with patch('config.settings.config.TESTING_MODE', True):
        ok, virt, phys, delta = pair_parity_ok("BTC/USDC", exchange=mock_ex, virtual=0.0)
        assert not ok
        assert phys == 0.0

def test_clear_bot_require_manual_proof(memory_db):
    cursor = memory_db.cursor()
    cursor.execute("INSERT INTO bots (id, name, pair, direction, bot_type, status) VALUES (?, ?, ?, ?, ?, ?)",
                   (10018, 'sui long', 'SUI/USDC:USDC', 'LONG', 'standard', 'REQUIRE_MANUAL_PROOF'))
    cursor.execute("INSERT INTO trades (bot_id, open_qty, cycle_id) VALUES (?, ?, ?)",
                   (10018, 0.0, 151))
    memory_db.commit()
    
    # Standard bot, open_qty = 0.0 -> Should clear to 'Scanning'
    with patch('engine.database.get_connection', return_value=memory_db):
        res = clear_bot_require_manual_proof(10018, "manual clear test", conn=memory_db)
        assert res
        
        status = cursor.execute("SELECT status FROM bots WHERE id = 10018").fetchone()[0]
        assert status == 'Scanning'

def test_clear_bot_require_manual_proof_with_qty(memory_db):
    cursor = memory_db.cursor()
    cursor.execute("INSERT INTO bots (id, name, pair, direction, bot_type, status) VALUES (?, ?, ?, ?, ?, ?)",
                   (10018, 'sui long', 'SUI/USDC:USDC', 'LONG', 'standard', 'REQUIRE_MANUAL_PROOF'))
    cursor.execute("INSERT INTO trades (bot_id, open_qty, cycle_id) VALUES (?, ?, ?)",
                   (10018, 7.2, 151))
    memory_db.commit()
    
    # Standard bot, open_qty = 7.2 -> Should clear to 'IN TRADE'
    with patch('engine.database.get_connection', return_value=memory_db):
        res = clear_bot_require_manual_proof(10018, "manual clear test", conn=memory_db)
        assert res
        
        status = cursor.execute("SELECT status FROM bots WHERE id = 10018").fetchone()[0]
        assert status == 'IN TRADE'

def test_seal_trade_state_clears_fill_claims_and_increments_cycle(memory_db):
    cursor = memory_db.cursor()
    cursor.execute("INSERT INTO bots (id, name, pair, direction, bot_type, status) VALUES (?, ?, ?, ?, ?, ?)",
                   (10018, 'sui long', 'SUI/USDC:USDC', 'LONG', 'standard', 'IN TRADE'))
    cursor.execute("INSERT INTO trades (bot_id, open_qty, cycle_id, total_invested, avg_entry_price) VALUES (?, ?, ?, ?, ?)",
                   (10018, 0.0, 151, 10.0, 1.0))
    # Insert some fill claims
    cursor.execute("INSERT INTO fill_claims (bot_id, order_id, caller, claimed_at) VALUES (?, ?, ?, ?)",
                   (10018, '147779440', 'ws', int(time.time())))
    cursor.execute("INSERT INTO fill_claims (bot_id, order_id, caller, claimed_at) VALUES (?, ?, ?, ?)",
                   (10018, 'STEP_1_151', 'step_lock_ws', int(time.time())))
    memory_db.commit()
    
    with patch('engine.database.get_connection', return_value=memory_db), \
         patch('engine.database.recompute_invested_from_orders', return_value=(0.0, 0.0, 0.0, 0)):
        
        # When seal_trade_state is called, since cost and qty are 0.0, the bot transitions to Scanning.
        # This should trigger cycle increment (to 152) and delete the fill claims.
        res = seal_trade_state(10018)
        assert res['status'] == 'Scanning'
        
        row_trade = cursor.execute("SELECT cycle_id, total_invested, open_qty FROM trades WHERE bot_id = 10018").fetchone()
        assert row_trade[0] == 152  # Cycle incremented
        assert row_trade[1] == 0.0  # Reset
        assert row_trade[2] == 0.0  # Reset
        
        claims = cursor.execute("SELECT count(*) FROM fill_claims WHERE bot_id = 10018").fetchone()[0]
        assert claims == 0  # Claims deleted
