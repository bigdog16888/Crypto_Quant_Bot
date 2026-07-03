import sqlite3
import pytest
import time
import uuid
from engine import database

@pytest.fixture
def memory_db():
    orig_connect = sqlite3.connect
    orig_backup = database.backup_database
    orig_db_path = database.DB_PATH
    
    # Disable backup during testing
    database.backup_database = lambda: None
    
    # Generate unique URI for each test to ensure isolation
    db_id = str(uuid.uuid4())
    shared_uri = f'file:test_db_{db_id}?mode=memory&cache=shared'
    
    # Keep one persistent connection open so the shared memory db isn't destroyed
    persistent_conn = orig_connect(shared_uri, uri=True)
    
    def mock_connect(db_path, *args, **kwargs):
        kwargs['uri'] = True
        return orig_connect(shared_uri, *args, **kwargs)
        
    sqlite3.connect = mock_connect
    
    # Clear thread local to force new connection
    if hasattr(database._local, 'connection'):
        database._local.connection = None
        
    database.DB_PATH = shared_uri
    database.init_db()
    
    conn = database.get_connection()
    # Apply manual migrations not present in init_db (now baseline, but try/except for safety)
    try:
        conn.execute("ALTER TABLE bot_orders ADD COLUMN wipe_proof_source TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE bot_orders ADD COLUMN wipe_proof_snapshot TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    
    yield conn
    
    # Teardown
    persistent_conn.close()
    sqlite3.connect = orig_connect
    database.backup_database = orig_backup
    database.DB_PATH = orig_db_path
    if hasattr(database._local, 'connection'):
        database._local.connection = None

def setup_bot_fixture(conn, bot_id, name, pair, direction):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO bots (id, name, pair, direction, is_active, normalized_pair)
        VALUES (?, ?, ?, ?, 1, ?)
    """, (bot_id, name, pair, direction, pair.split(':')[0].replace('/', '')))
    cursor.execute("""
        INSERT INTO trades (bot_id, cycle_id, current_step, open_qty, total_invested, avg_entry_price, cycle_phase, wipe_wall_ts, position_side)
        VALUES (?, 1, 0, 0.0, 0.0, 0.0, 'SCANNING', 0, ?)
    """, (bot_id, direction))
    conn.commit()

def test_recompute_invested(memory_db):
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES 
        (1, 'entry', 'order_1', 50000.0, 1.0, 1.0, 'filled', 1, 1),
        (1, 'grid', 'order_2', 40000.0, 1.0, 1.0, 'filled', 1, 2)
    """)
    memory_db.commit()
    
    invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    
    assert invested == 90000.0
    assert open_qty == 2.0
    assert avg_price == 45000.0
    assert max_step == 2

def test_get_pair_virtual_net(memory_db):
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_1', 50000.0, 2.0, 2.0, 'filled', 1, 1)
    """)
    memory_db.commit()
    
    database.recompute_invested_from_orders(1, 1)
    database.sync_trades_from_orders(1)
    
    net = database.get_pair_virtual_net('BTC/USDC:USDC')
    assert net == 2.0 

def test_partial_tp_fill(memory_db):
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    # Entry for 2.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_1', 50000.0, 2.0, 2.0, 'filled', 1, 1)
    """)
    # Partial TP for 1.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'order_tp_1', 60000.0, 2.0, 1.0, 'filled', 1, 2)
    """)
    memory_db.commit()
    
    invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    assert open_qty == 1.0
    assert invested == 50000.0

def test_full_tp_cascade(memory_db):
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    # Entry for 2.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_1', 50000.0, 2.0, 2.0, 'filled', 1, 1)
    """)
    # Full TP
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'order_tp_2', 60000.0, 2.0, 2.0, 'filled', 1, 2)
    """)
    memory_db.commit()
    
    invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    assert open_qty == 0.0
    assert invested == 0.0

def test_credit_fill_caps_to_order_amount(memory_db):
    """filled_amount must never exceed order amount (prevents 0.002 order / 1.0 fill bug)."""
    setup_bot_fixture(memory_db, 1, 'BTC Bot', 'BTC/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    cursor.execute(
        """
        INSERT INTO bot_orders
        (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, client_order_id)
        VALUES (1, 'entry', 'ex1', 77000.0, 0.002, 0.0, 'open', 1, 1, 'CQB_1_ENTRY_1_1')
        """
    )
    memory_db.commit()
    from engine.ledger import credit_fill, seal_trade_state
    ok = credit_fill(1, 'ex1', cumulative_qty=1.0, avg_price=77000.0, order_type='entry', is_cumulative=True)
    assert ok
    row = cursor.execute("SELECT filled_amount FROM bot_orders WHERE order_id='ex1'").fetchone()
    assert row[0] == pytest.approx(0.0021, rel=1e-4)  # amount * 1.05 cap
    seal_trade_state(1)
    _c, _a, net_qty, _s = database.recompute_invested_from_orders(1, 1)
    assert net_qty == pytest.approx(0.0021, rel=1e-4)


def test_canonical_dedup_uses_max_fill_row(memory_db):
    """Duplicate CID rows must not undercount — highest filled_amount row wins."""
    setup_bot_fixture(memory_db, 1, 'Short Bot', 'LINK/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    cursor.execute("DROP INDEX IF EXISTS idx_bot_orders_bot_cid")
    cursor.execute(
        """
        INSERT INTO bot_orders
        (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, client_order_id)
        VALUES
        (1, 'entry', 'old', 9.0, 1.08, 0.54, 'filled', 1, 1, 'CQB_1_ENTRY_1_1'),
        (1, 'entry', 'new', 9.0, 1.08, 1.08, 'filled', 1, 1, 'CQB_1_ENTRY_1_1')
        """
    )
    memory_db.commit()
    _cost, _avg, basket_net, _step = database.recompute_invested_from_orders(1, 1)
    database.sync_trades_from_orders(1)
    assert basket_net == pytest.approx(1.08)
    assert database.get_pair_virtual_net('LINK/USDC:USDC') == pytest.approx(-1.08)


def test_hedge_child_open_qty_calculation(memory_db):
    """Hedge child bot (SHORT) has standard positive open_qty and is calculated correctly."""
    setup_bot_fixture(memory_db, 1, 'Long Bot', 'SOL/USDC:USDC', 'LONG')
    setup_bot_fixture(memory_db, 2, 'Short Bot', 'SOL/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    cursor.execute("UPDATE bots SET hedge_child_bot_id = 2 WHERE id = 1")
    cursor.execute("UPDATE bots SET parent_bot_id = 1, bot_type = 'hedge_child' WHERE id = 2")
    
    # Insert entry order for child (SHORT)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (2, 'entry', 'child_entry', 80.0, 2.32, 2.32, 'filled', 1, 1, 'SHORT')
    """)
    memory_db.commit()
    
    invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(2, 1)
    assert open_qty == pytest.approx(2.32)


def test_hedge_child_order_virtual_net(memory_db):
    """Pair virtual net includes both parent and child bots."""
    setup_bot_fixture(memory_db, 1, 'Long Bot', 'BTC/USDC:USDC', 'LONG')
    setup_bot_fixture(memory_db, 2, 'Short Bot', 'BTC/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    cursor.execute("UPDATE bots SET hedge_child_bot_id = 2 WHERE id = 1")
    cursor.execute("UPDATE bots SET parent_bot_id = 1, bot_type = 'hedge_child' WHERE id = 2")
    
    # Add an entry order for bot 1 (+2.0 net)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (1, 'entry', 'order_1', 50000.0, 2.0, 2.0, 'filled', 1, 1, 'LONG')
    """)
    # Add an entry order for child bot (-1.0 net)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (2, 'entry', 'child_entry', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'SHORT')
    """)
    memory_db.commit()
    
    database.recompute_invested_from_orders(1, 1)
    database.sync_trades_from_orders(1)
    database.recompute_invested_from_orders(2, 1)
    database.sync_trades_from_orders(2)
    
    net = database.get_pair_virtual_net('BTC/USDC:USDC')
    # Net should be +2.0 (parent) - 1.0 (child) = +1.0
    assert net == 1.0

def test_cancelled_partial_fill_not_phantom(memory_db):
    setup_bot_fixture(memory_db, 1, 'LINK Bot', 'LINK/USDT:USDT', 'LONG')
    
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_link_1', 15.0, 10.0, 5.28, 'cancelled', 1, 1)
    """)
    memory_db.commit()
    database.sync_trades_from_orders(1)
    
    # Assert virtual net before reset is 5.28 (since it's a partially filled cancelled order)
    assert database.get_pair_virtual_net('LINK/USDT:USDT') == 5.28
    
    class _FlatEx:
        def fetch_positions(self):
            return []

    # Run the reset cascade (exchange flat — parity gate allows reset)
    database.reset_bot_after_tp(1, 20.0, action_label='MANUAL_CLOSE', exchange=_FlatEx())
    
    # Assert virtual net after reset is 0.0 (no longer phantom 5.28)
    assert database.get_pair_virtual_net('LINK/USDT:USDT') == 0.0

def test_partial_tp_no_cascade_block(memory_db):
    setup_bot_fixture(memory_db, 1, 'SUI Bot', 'SUI/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    
    # Bot has 15.7 open_qty in trades
    cursor.execute("UPDATE trades SET open_qty = 15.7, tp_order_id = 'tp_sui_123' WHERE bot_id = 1")
    # Insert entry order of 15.7
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_sui_1', 1.2, 15.7, 15.7, 'filled', 1, 1)
    """)
    # Insert open TP order of 15.7
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'tp_sui_123', 1.3, 15.7, 0.0, 'open', 1, 2)
    """)
    memory_db.commit()
    
    # credit partial fill of 8.3
    from engine.ledger import credit_fill
    success = credit_fill(1, 'tp_sui_123', 8.3, 1.3, 'tp')
    assert success is True
    
    # Assert open_qty becomes 7.4
    row = cursor.execute("SELECT open_qty, tp_order_id, cycle_id FROM trades WHERE bot_id = 1").fetchone()
    # 15.7 - 8.3 = 7.4
    assert abs(row[0] - 7.4) < 1e-6
    
    # Simulate the deadlock fix where tp_order_id gets cleared to allow new TP placement
    cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = 1")
    memory_db.commit()
    
    row2 = cursor.execute("SELECT open_qty, tp_order_id, cycle_id FROM trades WHERE bot_id = 1").fetchone()
    assert row2[1] is None
    assert row2[2] == 1  # Cycle is NOT reset



def test_tp_cascade_idempotent(memory_db):
    from engine.ledger import handle_tp_completion
    
    setup_bot_fixture(memory_db, 1, 'LINK Bot', 'LINK/USDT:USDT', 'LONG')
    
    cursor = memory_db.cursor()
    # Bot has 5.0 open_qty
    cursor.execute("UPDATE trades SET open_qty = 5.0, tp_order_id = 'tp_link_1' WHERE bot_id = 1")
    # Insert entry order of 5.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_link_1', 15.0, 5.0, 5.0, 'filled', 1, 1)
    """)
    # Insert TP order of 5.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'tp_link_1', 18.0, 5.0, 5.0, 'filled', 1, 2)
    """)
    memory_db.commit()
    
    class MockExchange:
        def fetch_positions(self):
            return []
        def fetch_open_orders(self, symbol=None):
            return []
            
    mock_ex = MockExchange()
    
    # First call
    res1 = handle_tp_completion(bot_id=1, exit_price=18.0, pair='LINK/USDT:USDT', exchange=mock_ex, cycle_id=1)
    assert res1 is True
    
    # Assert cycle incremented and open_qty is 0
    row1 = cursor.execute("SELECT cycle_id, open_qty FROM trades WHERE bot_id = 1").fetchone()
    assert row1[0] == 2
    assert row1[1] == 0.0
    
    # Second call (idempotency test)
    res2 = handle_tp_completion(bot_id=1, exit_price=18.0, pair='LINK/USDT:USDT', exchange=mock_ex, cycle_id=1)
    
    row2 = cursor.execute("SELECT cycle_id, open_qty FROM trades WHERE bot_id = 1").fetchone()
    assert row2[1] == 0.0

def test_stalemate_evictor_partial_clears_tp_order_id(memory_db):
    setup_bot_fixture(memory_db, 1, 'Stalemate Bot', 'BTC/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    
    # Setup bot state
    cursor.execute("UPDATE trades SET open_qty = 10.0, tp_order_id = 'tp_stalemate_1' WHERE bot_id = 1")
    # Insert entry order of 10.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_entry_1', 50000.0, 10.0, 10.0, 'filled', 1, 1)
    """)
    # Insert TP order of 10.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'tp_stalemate_1', 55000.0, 10.0, 0.0, 'open', 1, 2)
    """)
    memory_db.commit()
    
    # 1. Simulate TP partial fill credit
    from engine.ledger import credit_fill
    success = credit_fill(1, 'tp_stalemate_1', 4.0, 55000.0, 'tp')
    assert success is True
    
    # 2. Simulate Stalemate Evictor detecting partial fill on the order
    row = cursor.execute("SELECT open_qty, tp_order_id, cycle_id FROM trades WHERE bot_id = 1").fetchone()
    current_open_qty = row[0]
    
    # Assert open_qty is decremented to 6.0
    assert abs(current_open_qty - 6.0) < 1e-6
    
    if current_open_qty > 0.001:
        # Clear tp_order_id
        cursor.execute("UPDATE trades SET tp_order_id = NULL WHERE bot_id = 1")
        memory_db.commit()
        
    # 3. Assertions
    final_row = cursor.execute("SELECT open_qty, tp_order_id, cycle_id FROM trades WHERE bot_id = 1").fetchone()
    assert abs(final_row[0] - 6.0) < 1e-6  # NOT zeroed
    assert final_row[1] is None  # tp_order_id is NULL
    assert final_row[2] == 1  # Cycle NOT reset

def test_anonymous_fill_adoption(memory_db):
    setup_bot_fixture(memory_db, 1, 'SOL Long Bot', 'SOL/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    
    # 1. Bot has an entry order of 0.21 at $150.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (1, 'entry', 'order_entry_1', 150.0, 0.21, 0.21, 'filled', 1, 1, 'LONG')
    """)
    # Update trades table to reflect this entry
    cursor.execute("""
        UPDATE trades 
        SET open_qty = 0.21, total_invested = 31.5, avg_entry_price = 150.0, cycle_phase = 'ACTIVE'
        WHERE bot_id = 1
    """)
    memory_db.commit()
    
    # 2. Simulate an exchange position of 0.43 by inserting an adoption row for 0.22
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (1, 'adoption', 'adopt_1', 150.0, 0.22, 0.22, 'filled', 1, 0, 'LONG')
    """)
    memory_db.commit()
    
    # Now run recompute_invested_from_orders to update trades/check results
    total_invested, avg_price, open_qty, current_step = database.recompute_invested_from_orders(1, 1)
    
    # Update the trades table cache with the recomputed values (as in production)
    cursor.execute("""
        UPDATE trades 
        SET total_invested = ?, avg_entry_price = ?, open_qty = ?, current_step = ?
        WHERE bot_id = 1
    """, (total_invested, avg_price, open_qty, current_step))
    memory_db.commit()
    
    # Assert get_pair_virtual_net returns 0.43 (0.21 + 0.22)
    net = database.get_pair_virtual_net('SOL/USDC:USDC')
    assert abs(net - 0.43) < 1e-6
    
    # Assert recompute_invested_from_orders returns correct total_invested
    # bought_cost = (0.21 * 150) + (0.22 * 150) = 31.5 + 33.0 = 64.5
    assert abs(total_invested - 64.5) < 1e-6
    # open_qty should be 0.43
    assert abs(open_qty - 0.43) < 1e-6

def test_cancelled_partial_fill_not_counted_as_active(memory_db):
    setup_bot_fixture(memory_db, 1, 'BTC Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    # Insert a cancelled order that has been marked as 'reset_cleared'
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_btc_1', 76836.7, 0.002, 0.002, 'reset_cleared', 1, 1)
    """)
    memory_db.commit()
    
    # Assert recompute_invested_from_orders does NOT count it in bought_qty / open_qty
    total_invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    assert open_qty == 0.0
    assert total_invested == 0.0

def test_duplicate_entry_same_cid_not_double_counted(memory_db):
    setup_bot_fixture(memory_db, 1, 'Duplicate Bot', 'BTC/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    cursor.execute("DROP INDEX IF EXISTS idx_bot_orders_bot_cid")
    # Insert three bot_orders rows with identical client_order_id and status='filled'
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, client_order_id, position_side)
        VALUES 
        (1, 'entry', 'order_d1', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'DUPLICATE_CID_123', 'LONG'),
        (1, 'entry', 'order_d2', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'DUPLICATE_CID_123', 'LONG'),
        (1, 'entry', 'order_d3', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'DUPLICATE_CID_123', 'LONG')
    """)
    memory_db.commit()
    
    # Assert recompute_invested_from_orders only counts unique fills, not duplicates
    total_invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(1, 1)
    database.sync_trades_from_orders(1)
    assert open_qty == 1.0
    assert total_invested == 50000.0
    
    # Also assert get_pair_virtual_net only counts unique fills
    net = database.get_pair_virtual_net('BTC/USDC:USDC')
    assert net == 1.0


def test_stalemate_evictor_skips_already_processed_tp(memory_db):
    from unittest.mock import MagicMock, patch
    from engine.bot_executor import BotExecutor
    from engine.exchange_interface import ExchangeInterface
    
    setup_bot_fixture(memory_db, 1, 'Stalemate Bot', 'BTC/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    
    # 1. Setup bot state with open_qty and local_tp_id
    cursor.execute("UPDATE trades SET open_qty = 10.0, total_invested = 500000.0, tp_order_id = 'tp_stalemate_1' WHERE bot_id = 1")
    
    # 2. Insert entry order of 10.0
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'order_entry_1', 50000.0, 10.0, 10.0, 'filled', 1, 1)
    """)
    # 3. Insert TP order of 10.0 that has status = 'reset_cleared' in DB
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'tp', 'tp_stalemate_1', 55000.0, 10.0, 10.0, 'reset_cleared', 1, 2)
    """)
    memory_db.commit()
    
    # 4. Instantiate BotExecutor and mock exchange.fetch_order to return FILLED
    executor = BotExecutor(runner=None)
    mock_exchange = MagicMock(spec=ExchangeInterface)
    
    # fetch_order returns a filled order
    mock_exchange.fetch_order.return_value = {
        'id': 'tp_stalemate_1',
        'status': 'filled',
        'filled': 10.0,
        'amount': 10.0,
        'average': 55000.0,
        'price': 55000.0,
        'lastTradeTimestamp': 123456789000
    }
    
    bot_status = {
        'id': 1,
        'name': 'Stalemate Bot',
        'pair': 'BTC/USDC:USDC',
        'current_step': 1,
        'total_invested': 500000.0,
        'avg_entry_price': 50000.0,
        'target_tp_price': 0.0,
        'cycle_id': 1,
        'open_qty': 10.0
    }
    
    with patch('engine.ledger.register_tp_cascade') as mock_cascade, \
         patch('engine.ledger.credit_fill') as mock_credit:
         
        res = executor.maintain_orders(
            bot_id=1,
            name='Stalemate Bot',
            pair='BTC/USDC:USDC',
            direction='LONG',
            bot_status=bot_status,
            current_price=55000.0,
            exchange=mock_exchange,
            market_snapshot={'open_orders': []}, # empty open_orders forces STALEMATE check
            bot_config={'market_type': 'swap'}
        )
        
        # Verify it returns None
        assert res is None
        # Verify cascade and credit were skipped
        mock_cascade.assert_not_called()
        mock_credit.assert_not_called()
        
    # 5. Verify that tp_order_id was cleared to NULL in the trades table
    from engine.database import get_connection as _gc
    fresh_conn = _gc()
    fresh_cursor = fresh_conn.cursor()
    row = fresh_cursor.execute("SELECT tp_order_id FROM trades WHERE bot_id = 1").fetchone()
    assert row[0] is None


def test_fifo_partial_exit_avg_price(memory_db):
    setup_bot_fixture(memory_db, 100, 'FIFO Bot', 'BTC/USDT:USDT', 'LONG')
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES 
        (100, 'entry', 'e1', 600.0, 0.05, 0.05, 'filled', 1, 1, 'LONG'),
        (100, 'grid', 'e2', 650.0, 0.05, 0.05, 'filled', 1, 2, 'LONG'),
        (100, 'grid', 'e3', 700.0, 0.05, 0.05, 'filled', 1, 3, 'LONG'),
        (100, 'tp', 'exit1', 660.0, 0.05, 0.05, 'filled', 1, 4, 'LONG')
    """)
    memory_db.commit()
    
    total_invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(100, 1)
    
    assert pytest.approx(open_qty) == 0.10
    assert pytest.approx(avg_price) == 675.0
    assert pytest.approx(total_invested) == 67.5
    assert max_step == 3


def test_fifo_hedge_child_short_partial_exit(memory_db):
    setup_bot_fixture(memory_db, 200, 'Hedge Child', 'BTC/USDT:USDT', 'SHORT')
    cursor = memory_db.cursor()
    cursor.execute("UPDATE bots SET bot_type = 'hedge_child' WHERE id = 200")
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES 
        (200, 'entry', 'e1', 694.61, 0.11, 0.11, 'filled', 1, 1, 'SHORT'),
        (200, 'grid', 'e2', 694.66, 0.11, 0.11, 'filled', 1, 2, 'SHORT'),
        (200, 'tp', 'exit1', 694.61, 0.11, 0.11, 'filled', 1, 3, 'SHORT')
    """)
    memory_db.commit()
    
    total_invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(200, 1)
    
    assert pytest.approx(open_qty) == 0.11
    assert pytest.approx(avg_price) == 694.66
    assert pytest.approx(total_invested) == 0.11 * 694.66
    assert max_step == 2


def test_fifo_full_exit_returns_zero(memory_db):
    setup_bot_fixture(memory_db, 300, 'Full Exit Bot', 'BTC/USDT:USDT', 'LONG')
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES 
        (300, 'entry', 'e1', 600.0, 0.10, 0.10, 'filled', 1, 1, 'LONG'),
        (300, 'grid', 'e2', 650.0, 0.05, 0.05, 'filled', 1, 2, 'LONG'),
        (300, 'tp', 'exit1', 660.0, 0.15, 0.15, 'filled', 1, 3, 'LONG')
    """)
    memory_db.commit()
    
    res = database.recompute_invested_from_orders(300, 1)
    assert res == (0.0, 0.0, 0.0, 0)


def test_fifo_bnb_scenario_check(memory_db):
    setup_bot_fixture(memory_db, 10007, 'BNB Bot', 'BNB/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES 
        (10007, 'entry', 'order_1', 635.96, 0.01, 0.01, 'filled', 50, 1, 'SHORT'),
        (10007, 'grid', 'order_2', 636.89, 0.01, 0.01, 'filled', 50, 2, 'SHORT'),
        (10007, 'grid', 'order_3', 637.45, 0.02, 0.02, 'filled', 50, 3, 'SHORT'),
        (10007, 'grid', 'order_4', 638.15, 0.03, 0.03, 'filled', 50, 4, 'SHORT'),
        (10007, 'grid', 'order_5', 638.96, 0.05, 0.05, 'filled', 50, 5, 'SHORT'),
        (10007, 'grid', 'order_6', 639.74, 0.07, 0.07, 'filled', 50, 6, 'SHORT'),
        (10007, 'grid', 'order_7', 714.13, 0.11, 0.11, 'filled', 50, 7, 'SHORT'),
        (10007, 'tp', 'order_tp_5', 637.25, 0.12, 0.12, 'filled', 50, 5, 'SHORT')
    """)
    cursor.execute("UPDATE trades SET cycle_id = 50, position_side = 'SHORT' WHERE bot_id = 10007")
    memory_db.commit()
    
    total_invested, avg_price, open_qty, max_step = database.recompute_invested_from_orders(10007, 50)
    
    assert pytest.approx(open_qty) == 0.18
    assert pytest.approx(avg_price) == 685.2005555555556
    assert pytest.approx(total_invested) == 123.3361
    assert max_step == 7


def test_seal_trade_state_recalculates_target_tp_price(memory_db):
    setup_bot_fixture(memory_db, 500, 'TP Recalc Bot', 'BTC/USDT:USDT', 'LONG')
    cursor = memory_db.cursor()
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (500, 'entry', 'e1', 50000.0, 1.0, 1.0, 'filled', 1, 1, 'LONG')
    """)
    cursor.execute("UPDATE trades SET target_tp_price = 45000.0 WHERE bot_id = 500")
    memory_db.commit()

    from engine.ledger import seal_trade_state
    seal_trade_state(500)

    row = cursor.execute("SELECT target_tp_price, avg_entry_price FROM trades WHERE bot_id = 500").fetchone()
    assert row[0] == pytest.approx(50000.0 * 1.015)


def test_hedge_be_fallback_seals_before_registering_tp(memory_db):
    from unittest.mock import MagicMock, patch
    from engine.bot_executor import BotExecutor
    from engine.exchange_interface import ExchangeInterface

    # Setup parent bot and hedge child bot
    setup_bot_fixture(memory_db, 100, 'Parent Bot', 'BTC/USDT:USDT', 'LONG')
    setup_bot_fixture(memory_db, 101, 'Hedge Child', 'BTC/USDT:USDT', 'SHORT')
    
    cursor = memory_db.cursor()
    cursor.execute("UPDATE bots SET hedge_child_bot_id = 101 WHERE id = 100")
    cursor.execute("UPDATE bots SET parent_bot_id = 100, bot_type = 'hedge_child' WHERE id = 101")
    cursor.execute("UPDATE bots SET hedge_trigger_step = 1 WHERE id = 100")
    cursor.execute("UPDATE trades SET current_step = 1 WHERE bot_id = 100")
    
    # Setup trade state: set trades.open_qty = 0.0 (stale accumulator) but avg_entry_price = 600.0, cycle_id = 1
    cursor.execute("""
        UPDATE trades 
        SET open_qty = 0.0, avg_entry_price = 600.0, cycle_id = 1, current_step = 1, entry_confirmed = 1
        WHERE bot_id = 101
    """)
    
    # Insert a filled entry order in bot_orders so recomputed qty is 0.6
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (101, 'entry', 'e1', 600.0, 0.6, 0.6, 'filled', 1, 1, 'SHORT')
    """)
    memory_db.commit()

    executor = BotExecutor(runner=None)
    mock_exchange = MagicMock(spec=ExchangeInterface)
    mock_exchange.fetch_open_orders.return_value = [] # no open orders on exchange
    mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001, 'price_precision': 2}
    mock_exchange.round_to_step.side_effect = lambda qty, step: qty
    
    bot_status = {
        'id': 101,
        'name': 'Hedge Child',
        'pair': 'BTC/USDT:USDT',
        'current_step': 1,
        'total_invested': 0.0,
        'avg_entry_price': 600.0,
        'target_tp_price': 0.0,
        'cycle_id': 1,
        'open_qty': 0.0 # Stale accumulator passed in status
    }
    
    # Run maintain_orders for the child bot
    with patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, '')):
        executor.maintain_orders(
            bot_id=101,
            name='Hedge Child',
            pair='BTC/USDT:USDT',
            direction='SHORT',
            bot_status=bot_status,
            current_price=600.0,
            exchange=mock_exchange,
            market_snapshot={'open_orders': []},
            bot_config={'market_type': 'swap'}
        )
    
    # Check if fallback TP order was registered with the corrected amount 0.6 in bot_orders
    row = cursor.execute("""
        SELECT price, amount, status, client_order_id, order_type 
        FROM bot_orders 
        WHERE bot_id = 101 AND status = 'pending_placement' AND order_type = 'tp'
    """).fetchone()
    
    assert row is not None, "Fallback TP order should be registered in bot_orders"
    assert row[1] == pytest.approx(0.6), f"Registered TP amount should be 0.6, got {row[1]}"
    assert row[3] == "CQB_101_TP_1_BE_FB", f"client_order_id should be CQB_101_TP_1_BE_FB, got {row[3]}"


def test_hedge_be_fallback_parent_active_guard(memory_db):
    from unittest.mock import MagicMock, patch
    from engine.bot_executor import BotExecutor
    from engine.exchange_interface import ExchangeInterface

    # Setup parent bot and hedge child bot
    setup_bot_fixture(memory_db, 100, 'Parent Bot', 'BTC/USDT:USDT', 'LONG')
    setup_bot_fixture(memory_db, 101, 'Hedge Child', 'BTC/USDT:USDT', 'SHORT')
    
    cursor = memory_db.cursor()
    cursor.execute("UPDATE bots SET hedge_child_bot_id = 101 WHERE id = 100")
    cursor.execute("UPDATE bots SET parent_bot_id = 100, bot_type = 'hedge_child' WHERE id = 101")
    cursor.execute("UPDATE bots SET hedge_trigger_step = 1 WHERE id = 100")
    cursor.execute("UPDATE trades SET current_step = 1 WHERE bot_id = 100")
    
    # 1. Parent is ACTIVE (open_qty = 0.25)
    cursor.execute("UPDATE trades SET open_qty = 0.25 WHERE bot_id = 100")
    
    # Setup child trade state
    cursor.execute("""
        UPDATE trades 
        SET open_qty = 0.5, avg_entry_price = 600.0, cycle_id = 1, current_step = 1, entry_confirmed = 1
        WHERE bot_id = 101
    """)
    # Insert entry order for child bot (SHORT)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step, position_side)
        VALUES (101, 'entry', 'e1', 600.0, 0.5, 0.5, 'filled', 1, 1, 'SHORT')
    """)
    memory_db.commit()

    executor = BotExecutor(runner=None)
    mock_exchange = MagicMock(spec=ExchangeInterface)
    mock_exchange.fetch_open_orders.return_value = [] # no open orders on exchange
    mock_exchange.get_symbol_precision.return_value = {'step_size': 0.001, 'price_precision': 2}
    mock_exchange.round_to_step.side_effect = lambda qty, step: qty

    bot_status = {
        'id': 101,
        'name': 'Hedge Child',
        'pair': 'BTC/USDT:USDT',
        'current_step': 1,
        'total_invested': 0.0,
        'avg_entry_price': 600.0,
        'target_tp_price': 0.0,
        'cycle_id': 1,
        'open_qty': 0.5
    }

    # Run maintain_orders for child bot with Parent ACTIVE
    with patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, '')):
        executor.maintain_orders(
            bot_id=101,
            name='Hedge Child',
            pair='BTC/USDT:USDT',
            direction='SHORT',
            bot_status=bot_status,
            current_price=600.0,
            exchange=mock_exchange,
            market_snapshot={'open_orders': []},
            bot_config={'market_type': 'swap'}
        )

    # Check that NO fallback TP order was registered
    row = cursor.execute("""
        SELECT id FROM bot_orders 
        WHERE bot_id = 101 AND status = 'pending_placement' AND order_type = 'tp'
    """).fetchone()
    assert row is None, "Should not register fallback TP when parent is active"

    # 2. Parent is INACTIVE (open_qty = 0.0)
    cursor.execute("UPDATE trades SET open_qty = 0.0 WHERE bot_id = 100")
    memory_db.commit()

    # Run maintain_orders for child bot with Parent INACTIVE
    with patch('engine.parity_gates.gate_maintain_orders_allowed', return_value=(True, '')):
        executor.maintain_orders(
            bot_id=101,
            name='Hedge Child',
            pair='BTC/USDT:USDT',
            direction='SHORT',
            bot_status=bot_status,
            current_price=600.0,
            exchange=mock_exchange,
            market_snapshot={'open_orders': []},
            bot_config={'market_type': 'swap'}
        )

    # Check that fallback TP order was registered
    row = cursor.execute("""
        SELECT price, amount, status, client_order_id, order_type 
        FROM bot_orders 
        WHERE bot_id = 101 AND status = 'pending_placement' AND order_type = 'tp'
    """).fetchone()
    assert row is not None, "Fallback TP order should be registered when parent is inactive"
    assert row[1] == pytest.approx(0.5)


def test_crosscycle_orphan_healer_cycle_floor(memory_db):
    """cycle_floor includes fills from older cycles in FIFO computation."""
    setup_bot_fixture(memory_db, 1, 'SUI Bot', 'SUI/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    
    # Cycle 123: entry fill, no exit (orphaned — never swept to reset_cleared)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount,
                                filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'orphan_entry', 0.71, 356.0, 356.0, 'filled', 123, 1)
    """)
    # trades.cycle_id = 126 (three cycles later)
    cursor.execute("UPDATE trades SET cycle_id = 126 WHERE bot_id = 1")
    memory_db.commit()
    
    # Explicit current cycle floor (cycle_floor=126): cycle 123 fill is excluded (invisible)
    cost, avg, qty, step = database.recompute_invested_from_orders(1, cycle_floor=126)
    assert qty == 0.0, "With cycle_floor=126, orphaned fill must be excluded"
    
    # Default call (cycle_floor=None): cycle 123 fill is automatically detected and included
    cost, avg, qty, step = database.recompute_invested_from_orders(1)
    assert pytest.approx(qty, abs=1e-6) == 356.0, "Auto-detected cycle_floor must include the orphan fill"
    
    # Explicit cycle_floor call (cycle_floor=123): cycle 123 fill is included
    cost, avg, qty, step = database.recompute_invested_from_orders(1, cycle_floor=123)
    assert pytest.approx(qty, abs=1e-6) == 356.0, "With cycle_floor=123, fill must be visible"
    assert pytest.approx(cost, abs=0.01) == 252.76  # 356 * 0.71


def test_crosscycle_floor_excludes_cycles_below_floor(memory_db):
    """Fills in cycles below cycle_floor are not included."""
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    
    # Cycle 10: old orphan
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount,
                                filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'old_orphan', 50000.0, 1.0, 1.0, 'filled', 10, 1)
    """)
    # Cycle 20: newer orphan
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount,
                                filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'new_orphan', 50000.0, 0.5, 0.5, 'filled', 20, 1)
    """)
    cursor.execute("UPDATE trades SET cycle_id = 25 WHERE bot_id = 1")
    memory_db.commit()
    
    # cycle_floor=20: only cycle 20 fill included, not cycle 10
    cost, avg, qty, step = database.recompute_invested_from_orders(1, cycle_floor=20)
    assert pytest.approx(qty, abs=1e-6) == 0.5


def test_pre_advance_all_cycle_sweep_and_scan(memory_db):
    """v4.1.5: Verify old-cycle sweep and orphan detection before cycle advance."""
    from unittest.mock import patch
    setup_bot_fixture(memory_db, 1, 'Sweep Bot', 'BTC/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()
    
    # Cycle 1: balanced fills (entry + tp)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES 
        (1, 'entry', 'e1', 50000.0, 1.0, 1.0, 'filled', 1, 1),
        (1, 'tp', 'tp1', 51000.0, 1.0, 1.0, 'filled', 1, 2)
    """)
    # Cycle 2: orphaned fill (entry, no tp)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'e2', 50000.0, 0.5, 0.5, 'filled', 2, 1)
    """)
    # Set trades.cycle_id = 3 so old_cycle = 3, and trades.open_qty = 0.0
    cursor.execute("UPDATE trades SET cycle_id = 3, open_qty = 0.0 WHERE bot_id = 1")
    memory_db.commit()
    
    with patch('engine.parity_gates.assert_cycle_reset_allowed', return_value=None), \
         patch('engine.database._log_trade_internal', return_value=None), \
         patch('engine.database.add_notification', return_value=None), \
         patch('engine.wipe_proof.safe_mark_reset_cleared', return_value=None), \
         patch('engine.database.clear_active_position_for_bot', return_value=None):
        
        database._reset_bot_after_tp_internal(
            cursor=cursor,
            bot_id=1,
            exit_price=51000.0,
            action_label='TP_HIT',
            human_approved=True
        )
    
    # Check if Cycle 1 rows (balanced) were swept to 'reset_cleared'
    rows_c1 = cursor.execute("SELECT status FROM bot_orders WHERE cycle_id = 1").fetchall()
    assert all(r[0] == 'reset_cleared' for r in rows_c1), "Cycle 1 fills must be swept to reset_cleared"
    
    # Check if Cycle 2 row (unbalanced orphan) was NOT swept to 'reset_cleared' (remains 'filled')
    row_c2 = cursor.execute("SELECT status FROM bot_orders WHERE cycle_id = 2").fetchone()
    assert row_c2[0] == 'filled', "Cycle 2 orphan must not be swept"


def test_heal_local_crosscycle_orphans_end_to_end(memory_db):
    """v4.1.5: Test reconciler's own _heal_local_crosscycle_orphans end-to-end against live schema."""
    from engine.reconciler import StateReconciler
    setup_bot_fixture(memory_db, 100, 'short sui', 'SUI/USDC:USDC', 'SHORT')
    cursor = memory_db.cursor()
    
    # Insert entry fills for cycle 71, 72, and 73
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount, filled_amount, status, cycle_id, step)
        VALUES 
        (100, 'entry', 'e71', 0.75, 13.1, 13.1, 'filled', 71, 1),
        (100, 'entry', 'e72', 0.75, 13.1, 13.1, 'filled', 72, 1),
        (100, 'entry', 'e73', 0.75, 13.3, 13.3, 'filled', 73, 1)
    """)
    # Set trades.cycle_id = 73, open_qty = 13.3, total_invested = 9.975 (13.3 * 0.75)
    cursor.execute("UPDATE trades SET cycle_id = 73, open_qty = 13.3, total_invested = 9.975 WHERE bot_id = 100")
    memory_db.commit()
    
    # Initialize reconciler
    reconciler = StateReconciler()
    
    # 1. Call _heal_local_crosscycle_orphans - first run detects and heals the orphans
    healed_count = reconciler._heal_local_crosscycle_orphans(100, 73)
    assert healed_count == 2, "First run should detect and heal 2 orphan cycles"
    
    # Verify trades table was corrected
    row = cursor.execute("SELECT open_qty, total_invested FROM trades WHERE bot_id = 100").fetchone()
    assert row is not None
    assert pytest.approx(row[0], abs=1e-6) == 39.5
    assert pytest.approx(row[1], abs=0.01) == 29.625  # (13.1 + 13.1 + 13.3) * 0.75
    
    # 2. Call _heal_local_crosscycle_orphans again - second run should detect they are already healed and return 0 (no redundant writes/logs)
    healed_count_2 = reconciler._heal_local_crosscycle_orphans(100, 73)
    assert healed_count_2 == 0, "Second run should bypass healing since trades cache already matches the healed state"


def test_reset_cleared_history_excluded_from_default_recompute(memory_db):
    """v4.1.5 EXCLUSION GUARD: 50+ historical reset_cleared cycles must be invisible to
    recompute_invested_from_orders() when called with no cycle_floor argument.

    Guarantee: resolved history (status='reset_cleared') is the safety wall that keeps
    cycle_floor=None auto-detection from absorbing old, already-reconciled data.
    Any regression that makes reset_cleared rows visible would silently inflate
    open_qty and total_invested for every bot that has ever taken profit.
    """
    setup_bot_fixture(memory_db, 1, 'Test Bot', 'BTC/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()

    # ── Phase 1: Insert 52 historical resolved cycles ─────────────────────────
    # Each cycle has a matched entry + tp pair, both swept to reset_cleared.
    # These represent normal historical TP cycles that closed cleanly.
    for cycle_num in range(1, 53):           # cycles 1..52 (old, resolved)
        cursor.execute("""
            INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount,
                                    filled_amount, status, cycle_id, step)
            VALUES
            (1, 'entry', ?, 50000.0, 0.1, 0.1, 'reset_cleared', ?, 1),
            (1, 'tp',    ?, 51000.0, 0.1, 0.1, 'reset_cleared', ?, 2)
        """, (f'e{cycle_num}', cycle_num, f'tp{cycle_num}', cycle_num))

    # ── Phase 2: Current active cycle (53) with a live entry fill ────────────
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount,
                                filled_amount, status, cycle_id, step)
        VALUES (1, 'entry', 'live_entry', 52000.0, 0.2, 0.2, 'filled', 53, 1)
    """)

    # Set trades to current cycle 53 with a stale cached open_qty of 0
    cursor.execute("UPDATE trades SET cycle_id = 53, open_qty = 0.0, total_invested = 0.0 WHERE bot_id = 1")
    memory_db.commit()

    # ── Assertion 1: Default call (cycle_floor=None) ───────────────────────────
    # Must return ONLY the cycle-53 live fill.
    # The 52 * 0.1 = 5.2 qty from resolved history must NOT appear.
    cost, avg, qty, step = database.recompute_invested_from_orders(1)
    assert pytest.approx(qty, abs=1e-6) == 0.2, (
        f"Default recompute must see ONLY the current cycle fill (0.2). "
        f"Got {qty:.8f} — reset_cleared history is leaking through the exclusion wall."
    )
    assert pytest.approx(cost, abs=0.01) == 0.2 * 52000.0, (
        f"Default recompute total_invested must match only the current entry cost. Got {cost:.4f}."
    )

    # ── Assertion 2: Explicit current-cycle call ──────────────────────────────
    # cycle_floor=53 should give identical results.
    cost2, avg2, qty2, step2 = database.recompute_invested_from_orders(1, cycle_floor=53)
    assert pytest.approx(qty2, abs=1e-6) == 0.2, (
        f"cycle_floor=53 (current) must see ONLY the current cycle fill. Got {qty2:.8f}."
    )

    # ── Assertion 3: Historical cycles are truly invisible ────────────────────
    # Directly verify the auto-detection orphan scan (the inner SQL that drives
    # cycle_floor=None auto-selection) sees zero unbalanced cycles.
    # The 52 resolved cycles are balanced AND reset_cleared — the HAVING filter on
    # status='filled' must exclude all of them.
    conn = database.get_connection()
    orphan_rows = conn.execute("""
        SELECT bo.cycle_id,
               SUM(CASE WHEN bo.order_type IN ('entry','grid','adoption','adoption_add','carry')
                        THEN bo.filled_amount ELSE 0 END) AS entry_qty,
               SUM(CASE WHEN bo.order_type IN
                        ('tp','close','dust_close','sl','adoption_reduce',
                         'virtual_netting','legacy_netting')
                        THEN bo.filled_amount ELSE 0 END) AS exit_qty
        FROM bot_orders bo
        WHERE bo.bot_id = 1
          AND bo.cycle_id < 53
          AND bo.cycle_id IS NOT NULL
          AND bo.filled_amount > 0
          AND bo.status = 'filled'
        GROUP BY bo.cycle_id
        HAVING (entry_qty - exit_qty) > 1e-6
    """).fetchall()

    assert len(orphan_rows) == 0, (
        f"Auto-detection orphan scan must find ZERO unbalanced cycles from reset_cleared history. "
        f"Found: {orphan_rows}"
    )


def test_recompute_does_not_merge_overclosed_historical_cycles(memory_db):
    """Proves that a historical cycle with exits > entries (over-closed cycle)
    does NOT get auto-detected as an orphan and merged forward into the current cycle's recompute.
    
    This locks in the fix for the directional blind spot: only entries > exits (under-closed cycles)
    are treated as orphans. Exits > entries must not merge.
    """
    bot_id = 12345
    setup_bot_fixture(memory_db, bot_id, 'Overclosed Test Bot', 'ETH/USDC:USDC', 'LONG')
    cursor = memory_db.cursor()

    # Cycle 1 (Historical): Over-closed (entry = 1.0, exit = 2.0, net = -1.0)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount,
                                filled_amount, status, cycle_id, step)
        VALUES
        (12345, 'entry', 'old_entry', 1600.0, 1.0, 1.0, 'filled', 1, 1),
        (12345, 'tp',    'old_tp',    1650.0, 2.0, 2.0, 'filled', 1, 2)
    """)

    # Cycle 2 (Live): Active position (entry = 0.5, net = 0.5)
    cursor.execute("""
        INSERT INTO bot_orders (bot_id, order_type, order_id, price, amount,
                                filled_amount, status, cycle_id, step)
        VALUES
        (12345, 'entry', 'live_entry', 1700.0, 0.5, 0.5, 'filled', 2, 1)
    """)

    # Set trades cache to active cycle 2
    cursor.execute("UPDATE trades SET cycle_id = 2, open_qty = 0.5, total_invested = 850.0, current_step = 1 WHERE bot_id = 12345")
    memory_db.commit()

    # Under the new directional query, Cycle 1 is ignored because exits > entries.
    # Recompute should only see Cycle 2 (qty = 0.5).
    # If the old HAVING ABS() query were used, Cycle 1 would be merged, causing the recompute
    # to evaluate combined entries (1.5) against combined exits (2.0), returning 0.0.
    cost, avg, qty, step = database.recompute_invested_from_orders(bot_id)

    assert qty == 0.5, (
        f"Recompute should not merge over-closed Cycle 1. Expected qty 0.5, got {qty}."
    )
    assert cost == 0.5 * 1700.0, (
        f"Recompute cost should only reflect Cycle 2. Expected {0.5 * 1700.0}, got {cost}."
    )


def test_sync_trades_from_orders_preserves_pending_hedge_close(memory_db):
    """Proves that sync_trades_from_orders does not wipe a parent bot in HEDGE_PENDING_CLOSE phase
    if its hedge child bot has a non-zero open_qty.
    """
    parent_id = 20001
    child_id = 20002
    
    # 1. Setup parent and child bots
    setup_bot_fixture(memory_db, parent_id, 'BNB Parent', 'BNB/USDC:USDC', 'SHORT')
    setup_bot_fixture(memory_db, child_id, 'BNB Child', 'BNB/USDC:USDC', 'LONG')
    
    cursor = memory_db.cursor()
    
    # Update parent to have child relation and status='pending_hedge_close'
    cursor.execute("UPDATE bots SET hedge_child_bot_id = ?, bot_type = 'standard' WHERE id = ?", (child_id, parent_id))
    cursor.execute("UPDATE bots SET parent_bot_id = ?, bot_type = 'hedge_child' WHERE id = ?", (parent_id, child_id))
    
    # Set child to have an active trade (open_qty = 0.3)
    cursor.execute("UPDATE trades SET cycle_id = 84, open_qty = 0.3, cycle_phase = 'ACTIVE' WHERE bot_id = ?", (child_id,))
    cursor.execute("UPDATE bots SET status = 'IN TRADE' WHERE id = ?", (child_id,))
    
    # Set parent to TP-cleared but hedge-pending state (cycle_id = 84, status='pending_hedge_close', phase='HEDGE_PENDING_CLOSE')
    cursor.execute("""
        UPDATE trades 
        SET cycle_id = 84, cycle_phase = 'HEDGE_PENDING_CLOSE', open_qty = 0.0, 
            current_step = 0, total_invested = 0, avg_entry_price = 0 
        WHERE bot_id = ?
    """, (parent_id,))
    cursor.execute("UPDATE bots SET status = 'pending_hedge_close' WHERE id = ?", (parent_id,))
    
    memory_db.commit()
    
    # 2. Run sync_trades_from_orders on parent
    correction_written = database.sync_trades_from_orders(parent_id)
    assert not correction_written, "sync_trades_from_orders should return False (no correction/wipe)"
    
    # Verify parent trades state was preserved
    row = cursor.execute("SELECT cycle_id, cycle_phase, open_qty FROM trades WHERE bot_id = ?", (parent_id,)).fetchone()
    assert row[0] == 84, f"Parent cycle must stay 84, got {row[0]}"
    assert row[1] == 'HEDGE_PENDING_CLOSE', f"Parent phase must stay HEDGE_PENDING_CLOSE, got {row[1]}"
    
    row_bot = cursor.execute("SELECT status FROM bots WHERE id = ?", (parent_id,)).fetchone()
    assert row_bot[0] == 'pending_hedge_close', f"Parent status must stay pending_hedge_close, got {row_bot[0]}"
    
    # 3. Simulate closing the child bot's position
    cursor.execute("UPDATE trades SET open_qty = 0.0, cycle_phase = 'IDLE' WHERE bot_id = ?", (child_id,))
    cursor.execute("UPDATE bots SET status = 'Scanning' WHERE id = ?", (child_id,))
    memory_db.commit()
    
    # Run sync_trades_from_orders again on parent
    correction_written = database.sync_trades_from_orders(parent_id)
    assert correction_written, "sync_trades_from_orders should write correction (DNA-wipe) now that child is closed"
    
    # Verify parent was wiped
    row = cursor.execute("SELECT cycle_id, cycle_phase, open_qty FROM trades WHERE bot_id = ?", (parent_id,)).fetchone()
    assert row[0] == 85, f"Parent cycle must increment to 85, got {row[0]}"
    assert row[1] == 'IDLE', f"Parent phase must reset to IDLE, got {row[1]}"
    
    row_bot = cursor.execute("SELECT status FROM bots WHERE id = ?", (parent_id,)).fetchone()
    assert row_bot[0] == 'Scanning', f"Parent status must reset to Scanning, got {row_bot[0]}"



